# SPDX-License-Identifier: Apache-2.0
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_auto_seed_triggered_when_pack_drifted(tmp_path, monkeypatch):
    """When stale_files_for_pack returns non-empty, runtime must launch a seed task."""
    from api.agent import runtime_managed as rm
    from api.agent import memory_seed as ms

    slug = "demo"
    pack = tmp_path / slug
    pack.mkdir()
    (pack / "rules.json").write_text('{"rules": []}')

    class FakeSettings:
        anthropic_api_key = "sk-test"
        memory_root = str(tmp_path)
        ma_memory_store_enabled = True
    monkeypatch.setattr(rm, "get_settings", lambda: FakeSettings())

    # No marker yet — everything stale.
    triggered = asyncio.Event()
    seeded_files: list[str] = []

    async def fake_seed(*, client, device_slug, pack_dir, only_files=None):
        seeded_files.extend(only_files or [])
        triggered.set()
        return {"/knowledge/rules.json": "seeded"}
    monkeypatch.setattr(ms, "seed_memory_store_from_pack", fake_seed)

    client = MagicMock()
    await rm.maybe_auto_seed(client=client, device_slug=slug, memory_root=tmp_path)
    await asyncio.wait_for(triggered.wait(), timeout=2.0)
    assert "rules.json" in seeded_files


@pytest.mark.asyncio
async def test_auto_seed_noop_when_pack_clean(tmp_path, monkeypatch):
    """Marker matches disk → no seed call."""
    from api.agent import runtime_managed as rm
    from api.agent import memory_seed as ms

    slug = "demo"
    pack = tmp_path / slug
    pack.mkdir()
    (pack / "rules.json").write_text('{"rules": []}')
    ms.write_seed_marker(
        pack_dir=pack,
        store_id="memstore_any",
        seeded_files={"rules.json": (pack / "rules.json").stat().st_mtime},
    )

    class FakeSettings:
        anthropic_api_key = "sk-test"
        memory_root = str(tmp_path)
        ma_memory_store_enabled = True
    monkeypatch.setattr(rm, "get_settings", lambda: FakeSettings())

    calls: list[str] = []
    async def fake_seed(**kwargs):
        calls.append("called")
        return {}
    monkeypatch.setattr(ms, "seed_memory_store_from_pack", fake_seed)

    await rm.maybe_auto_seed(client=MagicMock(), device_slug=slug, memory_root=tmp_path)
    # Give any stray task a chance to run.
    await asyncio.sleep(0.05)
    assert calls == []


@pytest.mark.asyncio
async def test_auto_seed_uses_session_mirrors_when_provided(tmp_path, monkeypatch):
    """When session_mirrors is passed, the task registers there for draining."""
    from api.agent import runtime_managed as rm
    from api.agent import memory_seed as ms

    slug = "demo"
    pack = tmp_path / slug
    pack.mkdir()
    (pack / "rules.json").write_text('{"rules": []}')

    class FakeSettings:
        anthropic_api_key = "sk-test"
        memory_root = str(tmp_path)
        ma_memory_store_enabled = True
    monkeypatch.setattr(rm, "get_settings", lambda: FakeSettings())

    async def fake_seed(*, client, device_slug, pack_dir, only_files=None):
        await asyncio.sleep(0.05)
        return {}
    monkeypatch.setattr(ms, "seed_memory_store_from_pack", fake_seed)

    mirrors = rm._SessionMirrors()
    task = await rm.maybe_auto_seed(
        client=MagicMock(), device_slug=slug, memory_root=tmp_path,
        session_mirrors=mirrors,
    )
    assert task is not None
    # The task must be tracked by the mirrors instance.
    assert task in mirrors._pending
    await mirrors.wait_drain(timeout=2.0)
    assert task.done()
