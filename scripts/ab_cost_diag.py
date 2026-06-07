#!/usr/bin/env python3
"""A/B cost harness — same scripted diagnostic, managed vs direct, compared.

The diagnostic runtime is picked by the engine's ``DIAGNOSTIC_MODE`` env var,
so this harness can't flip modes itself — it drives a server that's already
running in one mode, captures the per-turn token cost off the WS, and writes a
JSON report. Run it once per mode, then `--compare` the two reports for a
side-by-side table (tokens, cache-hit rate, $/turn).

It plays the same canonical MNT Reform U13 buck-dead scenario as
``bench_agent_flow`` (imported, not duplicated), so the only variable between
the two runs is the runtime. **It taps the real Anthropic API — each run costs
a few cents of tokens.**

Usage:
    # 1. Server in direct mode, then capture:
    DIAGNOSTIC_MODE=direct .venv/bin/python -m uvicorn api.main:app --port 8000
    .venv/bin/python scripts/ab_cost_diag.py --out /tmp/ab-direct.json

    # 2. Restart the server in managed mode, then capture:
    DIAGNOSTIC_MODE=managed .venv/bin/python -m uvicorn api.main:app --port 8000
    .venv/bin/python scripts/ab_cost_diag.py --out /tmp/ab-managed.json

    # 3. Compare:
    .venv/bin/python scripts/ab_cost_diag.py --compare /tmp/ab-direct.json /tmp/ab-managed.json

Tier defaults to `deep` (Opus 4.8) — the case where direct mode's effort=xhigh
should pull ahead on the 41-tool boardview surface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import websockets

REPO = Path(__file__).resolve().parents[1]
# Make `scripts.*` importable both as a file (python scripts/ab_cost_diag.py),
# where the repo root isn't on sys.path, and under pytest, where it already is.
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.bench_agent_flow import SCENARIO, _cleanup_bench_repair  # noqa: E402

DEFAULT_SLUG = "mnt-reform-motherboard"
DEFAULT_TIER = "deep"
DEFAULT_HOST = "http://localhost:8000"


# ---------------------------------------------------------------------------
# Pure metric aggregation (unit-tested in tests/test_ab_cost_diag.py)
# ---------------------------------------------------------------------------


@dataclass
class RunMetrics:
    """Aggregated token cost of one scripted diagnostic run."""

    label: str = ""
    mode: str = ""
    model: str = ""
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    assistant_messages: int = 0
    errors: int = 0
    duration_s: float = 0.0

    @property
    def cache_hit_rate(self) -> float:
        """cache_read / (input + cache_read + cache_creation). Output tokens are
        not a prompt-cache tier, so they're excluded from the denominator."""
        denom = (
            self.input_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )
        return (self.cache_read_input_tokens / denom) if denom else 0.0

    @property
    def cost_per_turn_usd(self) -> float:
        return (self.cost_usd / self.turns) if self.turns else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cache_hit_rate"] = round(self.cache_hit_rate, 4)
        d["cost_per_turn_usd"] = round(self.cost_per_turn_usd, 6)
        return d


def aggregate(
    frames: list[dict], *, label: str = "", duration_s: float = 0.0,
    mode: str = "", model: str = "",
) -> RunMetrics:
    """Fold a run's captured WS frames into a RunMetrics.

    Only live `turn_cost` frames are billed: a resumed session replays past
    turns flagged `replay=True`, and counting those would double-bill the
    comparison.
    """
    m = RunMetrics(label=label, duration_s=duration_s, mode=mode, model=model)
    for f in frames:
        t = f.get("type")
        if t == "turn_cost":
            if f.get("replay"):
                continue
            m.turns += 1
            m.input_tokens += int(f.get("input_tokens", 0) or 0)
            m.output_tokens += int(f.get("output_tokens", 0) or 0)
            m.cache_read_input_tokens += int(f.get("cache_read_input_tokens", 0) or 0)
            m.cache_creation_input_tokens += int(
                f.get("cache_creation_input_tokens", 0) or 0
            )
            m.cost_usd += float(f.get("cost_usd", 0.0) or 0.0)
        elif t == "tool_use":
            m.tool_calls += 1
        elif t == "message" and f.get("role") == "assistant":
            m.assistant_messages += 1
        elif t in ("error", "stream_error"):
            m.errors += 1
    return m


# ---------------------------------------------------------------------------
# Pretty output
# ---------------------------------------------------------------------------


def _color(s: str, code: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"\x1b[{code}m{s}\x1b[0m"


def GREEN(s: str) -> str: return _color(s, "32")
def RED(s: str) -> str: return _color(s, "31")
def YELLOW(s: str) -> str: return _color(s, "33")
def BOLD(s: str) -> str: return _color(s, "1")
def DIM(s: str) -> str: return _color(s, "2")


def _print_single(m: RunMetrics) -> None:
    print()
    print("━" * 64)
    print(f" A/B cost run — {BOLD(m.label or m.mode or '?')}  ({m.model})")
    print("━" * 64)
    print(f" turns                {m.turns}")
    print(f" input_tokens         {m.input_tokens:,}")
    print(f" output_tokens        {m.output_tokens:,}")
    print(f" cache_read           {m.cache_read_input_tokens:,}")
    print(f" cache_creation       {m.cache_creation_input_tokens:,}")
    print(f" cache hit rate       {m.cache_hit_rate * 100:.1f}%")
    print(f" tool calls           {m.tool_calls}")
    print(f" errors               {m.errors}")
    print(f" duration             {m.duration_s:.1f}s")
    print(f" {BOLD('cost')}                 {BOLD(f'${m.cost_usd:.4f}')}  "
          f"(${m.cost_per_turn_usd:.4f}/turn)")
    print("━" * 64)


def _fmt_delta(a: float, b: float, *, unit: str = "", pct: bool = True) -> str:
    """B relative to A. Lower-is-better metrics (cost, tokens): green when B<A."""
    if a == 0:
        return DIM("—")
    ratio = (b - a) / a
    s = f"{ratio * 100:+.0f}%" if pct else f"{b - a:+.2f}{unit}"
    return (GREEN(s) if ratio < 0 else RED(s)) if ratio != 0 else DIM(s)


def format_comparison(a: RunMetrics, b: RunMetrics) -> str:
    """Side-by-side table. Column A is the baseline; the delta is B vs A."""
    rows: list[tuple[str, str, str, str]] = [
        ("mode", a.mode or a.label, b.mode or b.label, ""),
        ("model", a.model, b.model, ""),
        ("turns", str(a.turns), str(b.turns), ""),
        ("input_tokens", f"{a.input_tokens:,}", f"{b.input_tokens:,}",
         _fmt_delta(a.input_tokens, b.input_tokens)),
        ("output_tokens", f"{a.output_tokens:,}", f"{b.output_tokens:,}",
         _fmt_delta(a.output_tokens, b.output_tokens)),
        ("cache_read", f"{a.cache_read_input_tokens:,}", f"{b.cache_read_input_tokens:,}", ""),
        ("cache_creation", f"{a.cache_creation_input_tokens:,}",
         f"{b.cache_creation_input_tokens:,}", ""),
        ("cache hit %", f"{a.cache_hit_rate * 100:.1f}%", f"{b.cache_hit_rate * 100:.1f}%", ""),
        ("tool calls", str(a.tool_calls), str(b.tool_calls), ""),
        ("errors", str(a.errors), str(b.errors), ""),
        ("duration s", f"{a.duration_s:.1f}", f"{b.duration_s:.1f}",
         _fmt_delta(a.duration_s, b.duration_s)),
        ("cost $", f"{a.cost_usd:.4f}", f"{b.cost_usd:.4f}",
         _fmt_delta(a.cost_usd, b.cost_usd)),
        ("$/turn", f"{a.cost_per_turn_usd:.4f}", f"{b.cost_per_turn_usd:.4f}",
         _fmt_delta(a.cost_per_turn_usd, b.cost_per_turn_usd)),
    ]
    la = a.label or a.mode or "A"
    lb = b.label or b.mode or "B"
    lines = [
        "",
        "━" * 72,
        f" {BOLD('A/B cost comparison')}   (delta = {lb} vs {la})",
        "━" * 72,
        f" {'metric':<16} {la:>18} {lb:>18} {'Δ':>12}",
        " " + "─" * 70,
    ]
    for label, va, vb, delta in rows:
        lines.append(f" {label:<16} {va:>18} {vb:>18} {delta:>12}")
    lines.append("━" * 72)
    if a.cost_usd and b.cost_usd:
        cheaper = la if a.cost_usd < b.cost_usd else lb
        lines.append(f" → {BOLD(cheaper)} is cheaper on this scenario.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Real WS run (real-I/O, like bench_agent_flow — not unit-tested)
# ---------------------------------------------------------------------------


async def _play_turn(ws, user_text: str, frames: list[dict], timeout: float) -> bool:
    """Send one user message, capture every frame until turn_complete.

    Echoes each tool call / reply / cost in real time (with elapsed seconds) so
    a long deep-tier turn is observable rather than a silent spinner. Returns
    False if the run hit an error frame (caller should stop)."""
    await ws.send(json.dumps({"type": "message", "text": user_text}))
    t0 = time.perf_counter()
    while True:
        el = time.perf_counter() - t0
        if el > timeout:
            raise TimeoutError(f"no turn_complete after {timeout:.0f}s")
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        ev = json.loads(raw)
        frames.append(ev)
        t = ev.get("type")
        el = time.perf_counter() - t0
        if t == "tool_use":
            inp = json.dumps(ev.get("input", {}), ensure_ascii=False)
            if len(inp) > 90:
                inp = inp[:90] + "…"
            print(f"    [{el:5.0f}s] {DIM('tool')} {ev.get('name', '?')} {DIM(inp)}", flush=True)
        elif t == "message" and ev.get("role") == "assistant":
            txt = (ev.get("text") or "").replace("\n", " ").strip()
            if len(txt) > 90:
                txt = txt[:90] + "…"
            if txt:
                print(f"    [{el:5.0f}s] {DIM('say ')} {txt}", flush=True)
        elif t == "turn_cost":
            print(
                f"    [{el:5.0f}s] {YELLOW('cost')} ${float(ev.get('cost_usd', 0)):.4f}"
                f"  in={ev.get('input_tokens', 0)} out={ev.get('output_tokens', 0)}"
                f" cr={ev.get('cache_read_input_tokens', 0)}"
                f" cw={ev.get('cache_creation_input_tokens', 0)}",
                flush=True,
            )
        elif t in ("error", "stream_error"):
            print(f"    [{el:5.0f}s] {RED('ERR ')} {ev.get('error') or ev.get('text')}", flush=True)
            return False
        if t == "turn_complete":
            print(f"    [{el:5.0f}s] {GREEN('turn_complete')}", flush=True)
            return True


async def _create_fresh_repair(host: str, slug: str, run_id: str) -> str:
    """POST /pipeline/repairs with a UNIQUE symptom so the engine never dedups
    onto a prior bench repair. A fixed symptom (bench_agent_flow's) makes the
    engine return the SAME repair_id across runs — the second run then replays
    the first's history instead of starting fresh, poisoning the comparison."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{host}/pipeline/repairs",
            data={
                "device_label": "MNT Reform motherboard (A/B bench)",
                "device_slug": slug,
                "symptom": f"ab-cost-bench {run_id} — dead board, no power",
                "force_rebuild": "false",
            },
        )
        resp.raise_for_status()
        return resp.json()["repair_id"]


async def run_once(
    host: str, slug: str, tier: str, *, keep_repair: bool, timeout_mult: float = 1.0,
    repair_id: str | None = None,
) -> RunMetrics:
    """Drive the canonical scenario once against the running server and return
    the aggregated metrics. The server's DIAGNOSTIC_MODE decides the runtime;
    we read the actual mode/model back off the session_ready frame.

    With `repair_id` set, reuse that pre-existing repair (its pack already
    expanded) and open a FRESH conversation (`conv=new`) — this is how to bench
    both modes on identical context without re-triggering the heavy pack expand
    (which starves the event loop) and without replaying prior history."""
    if repair_id is None:
        run_id = str(time.time_ns())
        repair_id = await _create_fresh_repair(host, slug, run_id)
        print(GREEN(f"✓ fresh repair: {repair_id}"))
        created = True
    else:
        print(GREEN(f"✓ reusing repair: {repair_id} (conv=new, no re-expand)"))
        created = False

    frames: list[dict] = []
    mode = model = ""
    ws_url = (
        host.replace("http://", "ws://").replace("https://", "wss://")
        + f"/ws/diagnostic/{slug}?tier={tier}&repair={repair_id}&conv=new"
    )
    t0 = time.perf_counter()
    try:
        async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
            while True:  # wait for session_ready
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                if ev.get("type") == "session_ready":
                    mode = ev.get("mode", "")
                    model = ev.get("model", "")
                    print(GREEN(f"✓ session_ready — {mode} · {model}"))
                    break
            for turn in SCENARIO:
                print(YELLOW(f"→ {turn['stage']}: {turn['text'][:70]}…"))
                ok = await _play_turn(
                    ws, turn["text"], frames, turn["wait_turn_end_s"] * timeout_mult,
                )
                if not ok:
                    print(RED("  ! error frame — stopping this run"))
                    break
    finally:
        # Only clean up a repair we created; a reused one belongs to the caller.
        if created and not keep_repair:
            _cleanup_bench_repair(slug, repair_id)
    duration = time.perf_counter() - t0
    return aggregate(frames, label=mode or tier, duration_s=duration, mode=mode, model=model)


def _load(path: str) -> RunMetrics:
    raw = json.loads(Path(path).read_text())
    known = {f for f in RunMetrics.__dataclass_fields__}  # type: ignore[attr-defined]
    return RunMetrics(**{k: v for k, v in raw.items() if k in known})


async def main_async(args) -> int:
    if args.compare:
        a, b = _load(args.compare[0]), _load(args.compare[1])
        print(format_comparison(a, b))
        return 0

    try:
        m = await run_once(
            args.host, args.slug, args.tier,
            keep_repair=args.keep_repair, timeout_mult=args.timeout_mult,
            repair_id=args.repair_id or None,
        )
    except Exception as exc:  # noqa: BLE001
        print(RED(f"Fatal: {type(exc).__name__}: {exc}"))
        return 2
    if args.label:
        m.label = args.label
    _print_single(m)
    if args.out:
        Path(args.out).write_text(json.dumps(m.to_dict(), indent=2))
        print(DIM(f" · saved {args.out}"))
    return 0 if m.errors == 0 else 1


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--slug", default=DEFAULT_SLUG)
    p.add_argument("--tier", default=DEFAULT_TIER, choices=["fast", "normal", "deep"])
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--label", default="", help="Override the run label (else the server mode).")
    p.add_argument("--out", default="", help="Write the run's metrics JSON here.")
    p.add_argument(
        "--compare", nargs=2, metavar=("A.json", "B.json"),
        help="Print a side-by-side comparison of two saved runs and exit.",
    )
    p.add_argument("--keep-repair", action="store_true", help="Skip cleanup of the ephemeral repair.")
    p.add_argument(
        "--repair-id", default="",
        help="Reuse a pre-existing (already-expanded) repair on a fresh conversation, "
             "instead of creating one — avoids re-triggering the pack expand mid-bench.",
    )
    p.add_argument(
        "--timeout-mult", type=float, default=1.0,
        help="Multiply each turn's wait budget (the scenario is tuned for Haiku/"
             "Sonnet; use ~6 for deep/Opus xhigh which is much slower).",
    )
    args = p.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
