"""Un parse de boardview (CPU lourd) ne doit PAS bloquer l'event-loop du moteur
async single-worker : sinon un upload `.tvw`/`.pcb` de plusieurs secondes gèle
TOUTES les autres requêtes (diags, /health, WS) le temps du parse, et les parses
concurrents se sérialisent. Le parse doit donc être offloadé (asyncio.to_thread).

Ce test démontre la responsivité : pendant qu'un parse lent tourne, /health doit
répondre vite. Sans offload (parse sync dans un handler `async def`), /health
attend la fin du parse.
"""
from __future__ import annotations

import asyncio
import time

import httpx

from api.board.parser.base import UnsupportedFormatError
from api.main import app


class _SlowParser:
    """Parser bouchon qui simule un parse CPU lourd (bloquant ~0.6s)."""

    def parse(self, data, **kwargs):
        time.sleep(0.6)
        raise UnsupportedFormatError("slow stub")

    def parse_file(self, path):
        time.sleep(0.6)
        raise UnsupportedFormatError("slow stub")


async def test_board_parse_does_not_block_event_loop(monkeypatch):
    import api.board.router as R

    monkeypatch.setattr(R, "parser_for", lambda *a, **k: _SlowParser())

    # Heartbeat : mesure la responsivité de l'event-loop DIRECTEMENT. Mesurer la
    # latence d'une requête concurrente ne marche pas — elle tourne sur le même
    # loop, donc si le loop gèle, la mesure gèle aussi. Le heartbeat, lui, voit
    # un GAP énorme entre deux ticks quand le loop est bloqué par un parse sync.
    gaps = []

    async def heartbeat():
        last = time.perf_counter()
        try:
            while True:
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now
        except asyncio.CancelledError:
            pass

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://engine") as c:
        hb = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.02)  # amorce le heartbeat
        await c.post(
            "/api/board/parse",
            files={"file": ("x.brd", b"some board bytes", "application/octet-stream")},
        )
        hb.cancel()
        await asyncio.gather(hb, return_exceptions=True)

    max_gap_ms = max(gaps) * 1000 if gaps else 0.0
    # Parse bloquant (~600ms) → un gap ~600ms. Offloadé → gaps ~10ms.
    assert max_gap_ms < 200, (
        f"event-loop gelé {max_gap_ms:.0f}ms pendant le parse "
        "(le parse doit être offloadé via asyncio.to_thread)"
    )
