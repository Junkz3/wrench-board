"""Tests for the memory_store seeding hook invoked by the pipeline orchestrator
after an APPROVED verdict.

Every test monkeypatches `settings` via the config module so that the
feature flag can be toggled independently of the dev environment.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from api import config as config_mod
from api.agent.memory_seed import seed_memory_store_from_pack


@pytest.fixture
def pack_dir(tmp_path: Path) -> Path:
    d = tmp_path / "demo-pi"
    d.mkdir()
    (d / "registry.json").write_text(json.dumps({"device_label": "Demo"}))
    (d / "knowledge_graph.json").write_text(json.dumps({"nodes": []}))
    (d / "rules.json").write_text(json.dumps({"rules": []}))
    (d / "dictionary.json").write_text(json.dumps({"entries": []}))
    return d


@pytest.fixture(autouse=True)
def reset_settings_cache(monkeypatch):
    monkeypatch.setattr(config_mod, "_settings", None)
    yield
    monkeypatch.setattr(config_mod, "_settings", None)


async def test_seed_no_op_when_flag_disabled(pack_dir, monkeypatch):
    monkeypatch.setenv("MA_MEMORY_STORE_ENABLED", "false")
    client = MagicMock()

    status = await seed_memory_store_from_pack(
        client=client, device_slug="demo-pi", pack_dir=pack_dir
    )

    assert all(v == "skipped:flag_disabled" for v in status.values())
    # And importantly, no SDK surface was touched.
    client.beta.memory_stores.memories.create.assert_not_called()


async def test_seed_skipped_when_ensure_memory_store_returns_none(
    pack_dir, monkeypatch
):
    monkeypatch.setenv("MA_MEMORY_STORE_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    # Force ensure_memory_store to return None (unsupported SDK / denied beta).
    async def fake_ensure(_client, _slug):
        return None

    monkeypatch.setattr("api.agent.memory_seed.ensure_memory_store", fake_ensure)

    client = MagicMock()
    status = await seed_memory_store_from_pack(
        client=client, device_slug="demo-pi", pack_dir=pack_dir
    )
    assert all(v == "skipped:no_store" for v in status.values())


async def test_seed_creates_one_memory_per_file(pack_dir, monkeypatch):
    monkeypatch.setenv("MA_MEMORY_STORE_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    async def fake_ensure(_client, _slug):
        return "memstore_test123"

    monkeypatch.setattr("api.agent.memory_seed.ensure_memory_store", fake_ensure)

    client = MagicMock()
    create_mock = AsyncMock()
    client.beta.memory_stores.memories.create = create_mock

    status = await seed_memory_store_from_pack(
        client=client, device_slug="demo-pi", pack_dir=pack_dir
    )

    assert status == {
        "/knowledge/registry.json": "seeded",
        "/knowledge/knowledge_graph.json": "seeded",
        "/knowledge/rules.json": "seeded",
        "/knowledge/dictionary.json": "seeded",
    }
    assert create_mock.await_count == 4
    paths_sent = [call.kwargs["path"] for call in create_mock.await_args_list]
    assert set(paths_sent) == set(status.keys())
    # All calls targeted the store returned by ensure_memory_store.
    assert all(
        call.kwargs["memory_store_id"] == "memstore_test123"
        for call in create_mock.await_args_list
    )


async def test_seed_reports_missing_file(pack_dir, monkeypatch):
    monkeypatch.setenv("MA_MEMORY_STORE_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    # Drop one of the expected files.
    (pack_dir / "rules.json").unlink()

    async def fake_ensure(_client, _slug):
        return "memstore_x"

    monkeypatch.setattr("api.agent.memory_seed.ensure_memory_store", fake_ensure)

    client = MagicMock()
    client.beta.memory_stores.memories.create = AsyncMock()

    status = await seed_memory_store_from_pack(
        client=client, device_slug="demo-pi", pack_dir=pack_dir
    )
    assert status["/knowledge/rules.json"] == "skipped:missing_file"
    assert status["/knowledge/registry.json"] == "seeded"


async def test_seed_swallows_per_file_errors(pack_dir, monkeypatch):
    """One file failing to upload must not abort the rest."""
    monkeypatch.setenv("MA_MEMORY_STORE_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    async def fake_ensure(_client, _slug):
        return "memstore_x"

    monkeypatch.setattr("api.agent.memory_seed.ensure_memory_store", fake_ensure)

    calls = 0

    async def flaky_create(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("transient beta 503")
        return MagicMock()

    client = MagicMock()
    client.beta.memory_stores.memories.create = flaky_create

    status = await seed_memory_store_from_pack(
        client=client, device_slug="demo-pi", pack_dir=pack_dir
    )
    assert sum(1 for v in status.values() if v == "seeded") == 3
    assert sum(1 for v in status.values() if v.startswith("error:")) == 1
