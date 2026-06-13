"""The upload-driven schematic ingest relays its page sub-steps onto the bus.

When a technician attaches a schematic from the home, the file POSTs to
`/packs/{slug}/documents` and (cache miss) ingestion runs out-of-band via
`_reingest_and_cache`. That background task forwards only the ingest's
`phase_step` events onto the slug's progress bus, so the landing timeline's
`schematic_ingest` row (whose started/finished bracket the pipeline wait-gate
owns) fills with "page N/M" while it polls for the electrical graph.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import api.pipeline as _pkg
from api.pipeline import events
from api.pipeline.routes import documents as docs


@pytest.mark.asyncio
async def test_reingest_relays_only_page_steps_to_bus(tmp_path: Path):
    slug = "demo-ingest-relay"
    pack = tmp_path / slug
    pack.mkdir()
    pdf = pack / "schematic.pdf"
    pdf.write_bytes(b"%PDF-1.4 demo")

    # Fake the vision pipeline: emit a phase_step (page) AND a non-step event,
    # to prove the relay forwards the former and drops the latter.
    async def fake_ingest(*, on_event=None, **_):
        if on_event:
            await on_event({"type": "phase_step", "phase": "schematic_ingest", "step": "page", "index": 1, "total": 2})
            await on_event({"type": "phase_finished", "phase": "schematic_ingest"})  # must NOT be relayed
            await on_event({"type": "phase_step", "phase": "schematic_ingest", "step": "page", "index": 2, "total": 2})
        return MagicMock()

    settings = MagicMock()
    settings.anthropic_api_key = "sk-ant-stub"
    settings.memory_root = str(tmp_path)

    queue = events.subscribe(slug)
    try:
        with (
            patch.object(_pkg, "ingest_schematic", new=fake_ingest),
            patch.object(docs._pkg, "get_settings", return_value=settings),
            patch.object(docs.sources, "write_through_cache", lambda *a, **k: None),
        ):
            await docs._reingest_and_cache(slug, pack, pdf, "deadbeef")

        seen = []
        while not queue.empty():
            seen.append(await queue.get())

        page_steps = [e for e in seen if e["type"] == "phase_step"]
        assert len(page_steps) == 2
        assert [e["index"] for e in page_steps] == [1, 2]
        assert all(e["phase"] == "schematic_ingest" for e in page_steps)
        # The non-step event (phase_finished) is owned by the wait-gate, not us.
        assert not any(e["type"] == "phase_finished" for e in seen)
    finally:
        events.unsubscribe(slug, queue)
