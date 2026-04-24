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
    (d / "electrical_graph.json").write_text(json.dumps({"components": {}, "power_rails": {}}))
    (d / "boot_sequence_analyzed.json").write_text(json.dumps({"phases": []}))
    (d / "nets_classified.json").write_text(json.dumps({"nets": {}}))
    (d / "simulator_reliability.json").write_text(json.dumps({"reliability_data": {}}))
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
        "/knowledge/electrical_graph.json": "seeded",
        "/knowledge/boot_sequence_analyzed.json": "seeded",
        "/knowledge/nets_classified.json": "seeded",
        "/knowledge/simulator_reliability.json": "seeded",
    }
    assert len(upserts) == 8
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
    # 1 file fails out of len(_SEED_FILES); the rest are seeded.
    assert sum(1 for v in status.values() if v == "seeded") == len(_SEED_FILES) - 1
    assert sum(1 for v in status.values() if v.startswith("error:")) == 1


# ---------------------------------------------------------------------------
# Marker I/O tests (Task 1)
# ---------------------------------------------------------------------------

from api.agent.memory_seed import (  # noqa: E402
    MARKER_FILENAME,
    read_seed_marker,
    write_seed_marker,
    stale_files_for_pack,
    _SEED_FILES,
)


def test_marker_roundtrip(tmp_path: Path):
    slug = "demo"
    pack = tmp_path / slug
    pack.mkdir()
    write_seed_marker(
        pack_dir=pack,
        store_id="memstore_abc",
        seeded_files={"registry.json": 123.0, "rules.json": 456.5},
    )
    marker_path = pack / MARKER_FILENAME
    assert marker_path.exists()
    data = read_seed_marker(pack)
    assert data is not None
    assert data["store_id"] == "memstore_abc"
    assert data["files"]["registry.json"] == 123.0


def test_read_marker_missing(tmp_path: Path):
    pack = tmp_path / "demo"
    pack.mkdir()
    assert read_seed_marker(pack) is None


def test_read_marker_corrupt(tmp_path: Path):
    pack = tmp_path / "demo"
    pack.mkdir()
    (pack / MARKER_FILENAME).write_text("{not json")
    assert read_seed_marker(pack) is None


def test_stale_files_no_marker_returns_all_present(tmp_path: Path):
    """No marker → every file that exists on disk is stale."""
    pack = tmp_path / "demo"
    pack.mkdir()
    (pack / "registry.json").write_text("{}")
    (pack / "rules.json").write_text("{}")
    # knowledge_graph.json + dictionary.json absent on purpose
    stale = stale_files_for_pack(pack)
    assert set(stale) == {"registry.json", "rules.json"}


def test_stale_files_all_synced(tmp_path: Path):
    """Marker has every file's mtime up-to-date → nothing stale."""
    pack = tmp_path / "demo"
    pack.mkdir()
    files = {}
    for name, _memory_path in _SEED_FILES:
        p = pack / name
        p.write_text("{}")
        files[name] = p.stat().st_mtime
    write_seed_marker(pack_dir=pack, store_id="memstore_x", seeded_files=files)
    assert stale_files_for_pack(pack) == []


def test_stale_files_partial_drift(tmp_path: Path):
    """rules.json touched after seed → only that one is stale."""
    pack = tmp_path / "demo"
    pack.mkdir()
    files = {}
    for name, _ in _SEED_FILES:
        p = pack / name
        p.write_text("{}")
        files[name] = p.stat().st_mtime
    # Back-date the marker's rules.json entry by 1 s so the rewrite below is
    # guaranteed to produce a strictly-newer stat mtime, regardless of the
    # filesystem's mtime resolution. Deterministic; no wallclock wait.
    files["rules.json"] = files["rules.json"] - 1.0
    write_seed_marker(pack_dir=pack, store_id="memstore_x", seeded_files=files)

    # Simulate a later pipeline write to rules.json only.
    (pack / "rules.json").write_text('{"rules": []}')
    assert stale_files_for_pack(pack) == ["rules.json"]


@pytest.mark.asyncio
async def test_seed_only_files_uploads_subset(tmp_path: Path, monkeypatch):
    """only_files=['rules.json'] must upsert exactly one path and update the marker."""
    from api.agent import memory_seed as ms_mod
    from unittest.mock import AsyncMock

    pack = tmp_path / "demo"
    pack.mkdir()
    for name, _ in ms_mod._SEED_FILES:
        (pack / name).write_text("{}")

    class FakeSettings:
        ma_memory_store_enabled = True
    monkeypatch.setattr(ms_mod, "get_settings", lambda: FakeSettings())

    async def fake_ensure(client, slug):
        return "memstore_xyz"
    monkeypatch.setattr(ms_mod, "ensure_memory_store", fake_ensure)

    calls: list[str] = []

    async def fake_upsert(client, *, store_id, path, content):
        calls.append(path)
        return {"id": "mem_1"}
    monkeypatch.setattr(ms_mod, "upsert_memory", fake_upsert)

    status = await ms_mod.seed_memory_store_from_pack(
        client=AsyncMock(), device_slug="demo", pack_dir=pack,
        only_files=["rules.json"],
    )

    assert calls == ["/knowledge/rules.json"]
    assert status["/knowledge/rules.json"] == "seeded"
    # Marker must contain rules.json plus merge with anything previously recorded.
    marker = ms_mod.read_seed_marker(pack)
    assert marker["store_id"] == "memstore_xyz"
    assert "rules.json" in marker["files"]


@pytest.mark.asyncio
async def test_seed_only_files_preserves_prior_marker_entries(tmp_path: Path, monkeypatch):
    """Partial re-seed must keep the mtimes of files NOT in only_files.

    Regression guard: a naive `merged = seeded_mtimes` (instead of
    `merged.update(...)`) would drop the three other entries from the
    marker, triggering re-seed-all on the next session open forever.
    """
    from api.agent import memory_seed as ms_mod
    from unittest.mock import AsyncMock

    pack = tmp_path / "demo"
    pack.mkdir()
    for name, _ in ms_mod._SEED_FILES:
        (pack / name).write_text("{}")

    # Pre-populate a marker with mtimes for ALL four files, but back-date
    # rules.json by 1 s so the rewrite below lands at a strictly-newer mtime
    # without relying on filesystem timing. The other three entries stay at
    # "now" — the test's point is that partial re-seed preserves them.
    prior_mtimes = {name: (pack / name).stat().st_mtime for name, _ in ms_mod._SEED_FILES}
    prior_mtimes["rules.json"] = prior_mtimes["rules.json"] - 1.0
    ms_mod.write_seed_marker(
        pack_dir=pack,
        store_id="memstore_xyz",
        seeded_files=prior_mtimes,
    )

    class FakeSettings:
        ma_memory_store_enabled = True
    monkeypatch.setattr(ms_mod, "get_settings", lambda: FakeSettings())

    async def fake_ensure(client, slug):
        return "memstore_xyz"
    monkeypatch.setattr(ms_mod, "ensure_memory_store", fake_ensure)

    async def fake_upsert(client, *, store_id, path, content):
        return {"id": "mem_1"}
    monkeypatch.setattr(ms_mod, "upsert_memory", fake_upsert)

    # Partial re-seed of rules.json only — its current stat mtime is newer
    # than the (back-dated) marker entry, so the merge path triggers.
    (pack / "rules.json").write_text('{"rules": [{"id": "new"}]}')

    await ms_mod.seed_memory_store_from_pack(
        client=AsyncMock(), device_slug="demo", pack_dir=pack,
        only_files=["rules.json"],
    )

    marker = ms_mod.read_seed_marker(pack)
    assert marker is not None
    # All four filenames must still be in the marker.
    expected_files = {name for name, _ in ms_mod._SEED_FILES}
    assert set(marker["files"].keys()) == expected_files, (
        f"merge must preserve entries for untouched files; got {set(marker['files'].keys())!r}"
    )
    # The three untouched files must keep their old mtimes.
    for name in expected_files - {"rules.json"}:
        assert marker["files"][name] == prior_mtimes[name], (
            f"{name} mtime must not change on partial re-seed"
        )
    # rules.json mtime must have advanced.
    assert marker["files"]["rules.json"] > prior_mtimes["rules.json"]


def test_stale_files_handles_legacy_marker_without_files_key(tmp_path: Path):
    """Legacy managed.json without 'files' key → every on-disk file stale."""
    pack = tmp_path / "demo"
    pack.mkdir()
    (pack / "registry.json").write_text("{}")
    (pack / "rules.json").write_text("{}")
    # Legacy marker: no `files` field.
    (pack / "managed.json").write_text(
        '{"memory_store_id": "memstore_legacy", "device_slug": "demo"}'
    )
    stale = stale_files_for_pack(pack)
    assert set(stale) == {"registry.json", "rules.json"}


@pytest.mark.asyncio
async def test_seed_merges_into_legacy_marker(tmp_path: Path, monkeypatch):
    """Partial seed against a legacy marker writes a well-formed marker back."""
    from api.agent import memory_seed as ms_mod
    from unittest.mock import AsyncMock

    pack = tmp_path / "demo"
    pack.mkdir()
    for name, _ in ms_mod._SEED_FILES:
        (pack / name).write_text("{}")
    # Legacy marker — no `files` key.
    (pack / "managed.json").write_text(
        '{"memory_store_id": "memstore_legacy", "device_slug": "demo"}'
    )

    class FakeSettings:
        ma_memory_store_enabled = True
    monkeypatch.setattr(ms_mod, "get_settings", lambda: FakeSettings())

    async def fake_ensure(client, slug):
        return "memstore_xyz"
    monkeypatch.setattr(ms_mod, "ensure_memory_store", fake_ensure)

    async def fake_upsert(client, *, store_id, path, content):
        return {"id": "mem_1"}
    monkeypatch.setattr(ms_mod, "upsert_memory", fake_upsert)

    await ms_mod.seed_memory_store_from_pack(
        client=AsyncMock(), device_slug="demo", pack_dir=pack,
        only_files=["rules.json"],
    )

    marker = ms_mod.read_seed_marker(pack)
    assert "files" in marker, "legacy marker should be upgraded to new schema"
    assert "rules.json" in marker["files"]
    assert marker["store_id"] == "memstore_xyz"
