"""Tests for durable mirror outcomes — Task 5 (D1).

Covers:
  - mirror_outcome_to_memory retries on transient upsert failures.
  - _SessionMirrors tracks pending tasks and drains them on session close.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_mirror_outcome_retries_then_succeeds(monkeypatch, tmp_path: Path):
    """upsert fails twice then succeeds — mirror_outcome_to_memory must retry."""
    from api.tools.validation import mirror_outcome_to_memory

    class FakeSettings:
        ma_memory_store_enabled = True

    monkeypatch.setattr("api.config.get_settings", lambda: FakeSettings())

    outcome = MagicMock()
    outcome.model_dump_json = lambda indent=2: '{"ok": true}'
    monkeypatch.setattr("api.tools.validation.load_outcome", lambda **kw: outcome)

    async def fake_ensure(client, slug):
        return "memstore_123"

    # ensure_memory_store is imported inside mirror_outcome_to_memory; patch the source module.
    monkeypatch.setattr(
        "api.agent.memory_stores.ensure_memory_store",
        fake_ensure,
    )

    calls = {"n": 0}

    async def flaky_upsert(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        return {"id": "mem_abc"}

    monkeypatch.setattr(
        "api.agent.memory_stores.upsert_memory",
        flaky_upsert,
    )

    status = await mirror_outcome_to_memory(
        client=MagicMock(), device_slug="demo",
        repair_id="r1", memory_root=tmp_path,
    )
    assert status == "mirrored"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_mirror_task_awaited_on_session_close():
    """Runtime must await pending mirrors in its finally block."""
    from api.agent.runtime_managed import _SessionMirrors

    mirrors = _SessionMirrors()

    slow_calls = {"done": False}

    async def slow_mirror():
        await asyncio.sleep(0.1)
        slow_calls["done"] = True
        return "mirrored"

    mirrors.spawn(slow_mirror())
    await mirrors.wait_drain(timeout=2.0)
    assert slow_calls["done"] is True
