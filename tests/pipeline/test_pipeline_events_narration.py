"""End-to-end tests for the narrator hook in _run_pipeline_with_events."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api.pipeline import _run_pipeline_with_events, events


@pytest.mark.asyncio
async def test_phase_finished_triggers_phase_narration(tmp_path: Path):
    slug = "demo-narration-test"
    pack = tmp_path / slug
    pack.mkdir()
    (pack / "raw_research_dump.md").write_text("# Demo\nSTM32, dead screen.", encoding="utf-8")

    queue = events.subscribe(slug)
    try:
        # Stub generate_knowledge_pack so it just emits one phase_finished and returns.
        async def fake_generate(device_label, *, on_event=None, **kw):
            if on_event:
                await on_event({"type": "phase_started", "phase": "scout"})
                await on_event({"type": "phase_finished", "phase": "scout", "elapsed_s": 0.1})

        # Stub the narrator to return a known string.
        async def fake_narrate(phase, slug_arg, *, client, memory_root=None):
            assert phase == "scout"
            assert slug_arg == slug
            return "I found an STM32."

        # Force a fake API key so narrator_client gets instantiated.
        with patch("api.pipeline.generate_knowledge_pack", new=fake_generate), \
             patch("api.pipeline.narrate_phase", new=fake_narrate), \
             patch("api.pipeline.get_settings") as mock_settings:
            settings_obj = MagicMock()
            settings_obj.anthropic_api_key = "sk-ant-stub"
            settings_obj.anthropic_max_retries = 2
            mock_settings.return_value = settings_obj

            await _run_pipeline_with_events("Demo Device", slug)
            # Allow the fire-and-forget task one tick to complete.
            for _ in range(10):
                await asyncio.sleep(0)

        # Drain the queue.
        seen = []
        while not queue.empty():
            seen.append(await queue.get())
        types = [e["type"] for e in seen]
        assert "phase_started" in types
        assert "phase_finished" in types
        assert "phase_narration" in types
        narration = next(e for e in seen if e["type"] == "phase_narration")
        assert narration["phase"] == "scout"
        assert narration["text"] == "I found an STM32."
    finally:
        events.unsubscribe(slug, queue)


@pytest.mark.asyncio
async def test_narration_skipped_when_no_api_key(tmp_path: Path):
    slug = "demo-no-key"

    queue = events.subscribe(slug)
    try:
        async def fake_generate(device_label, *, on_event=None, **kw):
            if on_event:
                await on_event({"type": "phase_finished", "phase": "scout", "elapsed_s": 0.0})

        async def fake_narrate(*a, **kw):
            raise AssertionError("narrate_phase should not be called when no API key")

        with patch("api.pipeline.generate_knowledge_pack", new=fake_generate), \
             patch("api.pipeline.narrate_phase", new=fake_narrate), \
             patch("api.pipeline.get_settings") as mock_settings:
            settings_obj = MagicMock()
            settings_obj.anthropic_api_key = ""  # no key
            settings_obj.anthropic_max_retries = 2
            mock_settings.return_value = settings_obj

            await _run_pipeline_with_events("Demo Device", slug)
            for _ in range(5):
                await asyncio.sleep(0)

        seen = []
        while not queue.empty():
            seen.append(await queue.get())
        types = [e["type"] for e in seen]
        assert "phase_finished" in types
        assert "phase_narration" not in types
    finally:
        events.unsubscribe(slug, queue)
