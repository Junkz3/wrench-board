"""Contract for the event relay in `_run_pipeline_with_events`.

The background task forwards every orchestrator event onto the per-slug WS bus
verbatim — including the live `phase_step` sub-steps the landing timeline
renders. The old Haiku `phase_narration` hook was removed (an extra LLM call
per phase for an after-the-fact sentence; the live sub-steps replaced it).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from api.pipeline import _run_pipeline_with_events, events


@pytest.mark.asyncio
async def test_phase_step_relayed_to_bus(tmp_path: Path):
    slug = "demo-relay-test"
    queue = events.subscribe(slug)
    try:
        async def fake_generate(device_label, *, on_event=None, **kw):
            if on_event:
                await on_event({"type": "phase_started", "phase": "writers"})
                await on_event({
                    "type": "phase_step", "phase": "writers",
                    "step": "writer_done", "writer": "graph", "count": 142,
                })
                await on_event({"type": "phase_finished", "phase": "writers", "elapsed_s": 0.1})

        with patch("api.pipeline.generate_knowledge_pack", new=fake_generate):
            await _run_pipeline_with_events("Demo Device", slug)
            for _ in range(5):
                await asyncio.sleep(0)

        seen = []
        while not queue.empty():
            seen.append(await queue.get())
        types = [e["type"] for e in seen]
        assert "phase_step" in types
        step = next(e for e in seen if e["type"] == "phase_step")
        assert step["writer"] == "graph"
        assert step["count"] == 142
    finally:
        events.unsubscribe(slug, queue)


@pytest.mark.asyncio
async def test_no_phase_narration_emitted(tmp_path: Path):
    """The Haiku narration hook is gone: phase_finished must NOT spawn narration."""
    slug = "demo-no-narration"
    queue = events.subscribe(slug)
    try:
        async def fake_generate(device_label, *, on_event=None, **kw):
            if on_event:
                await on_event({"type": "phase_finished", "phase": "scout", "elapsed_s": 0.1})

        with patch("api.pipeline.generate_knowledge_pack", new=fake_generate):
            await _run_pipeline_with_events("Demo Device", slug)
            for _ in range(10):
                await asyncio.sleep(0)

        seen = []
        while not queue.empty():
            seen.append(await queue.get())
        types = [e["type"] for e in seen]
        assert "phase_finished" in types
        assert "phase_narration" not in types
    finally:
        events.unsubscribe(slug, queue)


@pytest.mark.asyncio
async def test_narrate_phase_symbol_is_gone():
    """The narrator module + its re-export were deleted, not just unwired."""
    import api.pipeline as _pkg

    assert not hasattr(_pkg, "narrate_phase")
