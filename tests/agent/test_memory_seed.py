"""Tests for the memory_store seeding hook invoked by the pipeline orchestrator
after an APPROVED verdict.

Every test monkeypatches `settings` via the config module so that the
feature flag can be toggled independently of the dev environment.

The seed call now goes through `upsert_memory` (shared helper in
`api.agent.memory_stores`), which itself multi-plexes SDK / HTTP. Tests
patch `upsert_memory` directly at the `memory_seed` module binding — the
helper itself is exercised by tests on its own module.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

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

    calls: list[dict] = []

    async def fake_upsert(*_args, **kwargs):
        calls.append(kwargs)
        return "sha_ignored"

    monkeypatch.setattr("api.agent.memory_seed.upsert_memory", fake_upsert)

    status = await seed_memory_store_from_pack(
        client=client, device_slug="demo-pi", pack_dir=pack_dir
    )

    assert all(v == "skipped:flag_disabled" for v in status.values())
    # Flag off = no upsert call reaches the wire, period.
    assert calls == []


async def test_seed_skipped_when_ensure_memory_store_returns_none(
    pack_dir, monkeypatch
):
    monkeypatch.setenv("MA_MEMORY_STORE_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    # Force ensure_memory_store to return None (SDK down, API denied, etc.).
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

    upserts: list[dict] = []

    async def fake_upsert(_client, *, store_id, path, content):
        upserts.append({"store_id": store_id, "path": path, "bytes": len(content)})
        return "sha_" + path

    monkeypatch.setattr("api.agent.memory_seed.upsert_memory", fake_upsert)

    client = MagicMock()
    status = await seed_memory_store_from_pack(
        client=client, device_slug="demo-pi", pack_dir=pack_dir
    )

    assert status == {
        "/knowledge/registry.json": "seeded",
        "/knowledge/knowledge_graph.json": "seeded",
        "/knowledge/rules.json": "seeded",
        "/knowledge/dictionary.json": "seeded",
    }
    assert len(upserts) == 4
    assert {u["path"] for u in upserts} == set(status.keys())
    assert all(u["store_id"] == "memstore_test123" for u in upserts)


async def test_seed_reports_missing_file(pack_dir, monkeypatch):
    monkeypatch.setenv("MA_MEMORY_STORE_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    # Drop one of the expected files.
    (pack_dir / "rules.json").unlink()

    async def fake_ensure(_client, _slug):
        return "memstore_x"

    async def fake_upsert(_client, **_kwargs):
        return "ok"

    monkeypatch.setattr("api.agent.memory_seed.ensure_memory_store", fake_ensure)
    monkeypatch.setattr("api.agent.memory_seed.upsert_memory", fake_upsert)

    client = MagicMock()
    status = await seed_memory_store_from_pack(
        client=client, device_slug="demo-pi", pack_dir=pack_dir
    )
    assert status["/knowledge/rules.json"] == "skipped:missing_file"
    assert status["/knowledge/registry.json"] == "seeded"


async def test_seed_records_per_file_upsert_failure(pack_dir, monkeypatch):
    """One file failing to upload must not abort the rest."""
    monkeypatch.setenv("MA_MEMORY_STORE_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    async def fake_ensure(_client, _slug):
        return "memstore_x"

    monkeypatch.setattr("api.agent.memory_seed.ensure_memory_store", fake_ensure)

    calls = 0

    async def flaky_upsert(_client, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return None  # mimic the shared helper's failure mode
        return "sha_ok"

    monkeypatch.setattr("api.agent.memory_seed.upsert_memory", flaky_upsert)

    client = MagicMock()
    status = await seed_memory_store_from_pack(
        client=client, device_slug="demo-pi", pack_dir=pack_dir
    )
    assert sum(1 for v in status.values() if v == "seeded") == 3
    assert sum(1 for v in status.values() if v.startswith("error:")) == 1
