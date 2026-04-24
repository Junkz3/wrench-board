# Auto-Seed Memory Store at Session Open Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax.

**Goal:** Close the gap where opening a repair on a device whose pack exists on disk but has never been seeded (or has drifted since the last seed) leaves the MA memory store empty. The diagnostic agent then can't find prior knowledge via `memory_search` / mount-based lookups, silently degrading UX.

**Architecture:**
- Per-device marker file at `memory/{slug}/managed.json` records `{seeded_at, store_id, files: {path: mtime}}`.
- At session open, `runtime_managed` compares disk pack mtimes against the marker. Any divergence triggers a background re-seed of **only the stale files**.
- Write is fire-and-forget with log warning on failure — next session retries. Zero API cost when the marker says everything is up to date.
- This feature closes Trou 1 (pack update never propagated to MA store). Trou 2 (store wipe on Anthropic side) is out of scope here — a manual `--verify` CLI is left for a future follow-up.

**Tech stack:** Python 3.11+, pytest, anthropic (beta `managed-agents-2026-04-01`).

**Files touched:**
- `api/agent/memory_seed.py` — extend with marker I/O + partial seed
- `api/agent/runtime_managed.py` — dispatch the auto-seed background task at session open
- `tests/agent/test_memory_seed.py` — extend with marker + stale-detection coverage

**Dependency on the main optimization plan:** Task 2 below touches `runtime_managed.py`. It MUST run after Task 1 (R1 pack cache) of `docs/superpowers/plans/2026-04-24-agent-pipeline-optimizations.md` is merged, since both rewrite near the same region. Task 1 below is isolated in `memory_seed.py` and can ship any time.

---

## Task 1: Seed marker + stale detection + partial re-seed

**Why:** The existing `seed_memory_store_from_pack` always seeds all four files. To make session-open auto-seed cheap and idempotent, we need (a) persistence of what was seeded when, and (b) the ability to seed only a subset.

**Files:**
- Modify: `api/agent/memory_seed.py`
- Test: `tests/agent/test_memory_seed.py`

- [ ] **Step 1: Write failing tests for marker I/O**

Append to `tests/agent/test_memory_seed.py` (create if missing — mirror the shape of existing memory_seed tests):

```python
import json
from pathlib import Path

from api.agent.memory_seed import (
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
```

- [ ] **Step 2: Run — FAIL**

Run: `.venv/bin/pytest tests/agent/test_memory_seed.py -v -k "marker"`
Expected: ImportError on `MARKER_FILENAME`/`read_seed_marker`/`write_seed_marker`.

- [ ] **Step 3: Add marker I/O to `memory_seed.py`**

Add to `api/agent/memory_seed.py` (after the existing imports, before `_SEED_FILES`):

```python
import json
from datetime import UTC, datetime

MARKER_FILENAME = "managed.json"


def read_seed_marker(pack_dir: Path) -> dict | None:
    """Return the seed marker dict, or None if missing/corrupt."""
    path = pack_dir / MARKER_FILENAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning(
            "[MemorySeed] marker at %s unreadable — treating as missing", path,
        )
        return None


def write_seed_marker(
    *,
    pack_dir: Path,
    store_id: str,
    seeded_files: dict[str, float],
) -> None:
    """Write the marker. `seeded_files` maps filename → mtime-at-seed-time."""
    pack_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "seeded_at": datetime.now(UTC).isoformat(),
        "store_id": store_id,
        "files": seeded_files,
    }
    (pack_dir / MARKER_FILENAME).write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
```

- [ ] **Step 4: Run — PASS**

Run: `.venv/bin/pytest tests/agent/test_memory_seed.py -v -k "marker"`
Expected: PASS on the three marker tests.

- [ ] **Step 5: Write failing tests for stale detection**

Append to `tests/agent/test_memory_seed.py`:

```python
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
    write_seed_marker(pack_dir=pack, store_id="memstore_x", seeded_files=files)

    # Simulate a later pipeline write to rules.json only.
    import time
    time.sleep(0.01)
    (pack / "rules.json").write_text('{"rules": []}')
    assert stale_files_for_pack(pack) == ["rules.json"]
```

- [ ] **Step 6: Run — FAIL**

Run: `.venv/bin/pytest tests/agent/test_memory_seed.py -v -k "stale"`
Expected: ImportError on `stale_files_for_pack`.

- [ ] **Step 7: Implement `stale_files_for_pack`**

Add to `api/agent/memory_seed.py` (after `write_seed_marker`):

```python
def stale_files_for_pack(pack_dir: Path) -> list[str]:
    """Return the filenames in `_SEED_FILES` that need re-seeding.

    A file is stale when:
      - it exists on disk AND
      - either the marker is missing, or the marker's recorded mtime for
        that file is older than the current on-disk mtime.

    Files absent from disk are ignored (nothing to seed).
    """
    marker = read_seed_marker(pack_dir)
    marker_files = marker["files"] if marker else {}

    stale: list[str] = []
    for file_name, _memory_path in _SEED_FILES:
        path = pack_dir / file_name
        if not path.exists():
            continue
        disk_mtime = path.stat().st_mtime
        recorded = marker_files.get(file_name)
        if recorded is None or disk_mtime > recorded:
            stale.append(file_name)
    return stale
```

- [ ] **Step 8: Run — PASS**

Run: `.venv/bin/pytest tests/agent/test_memory_seed.py -v -k "stale"`
Expected: PASS on all three stale tests.

- [ ] **Step 9: Extend `seed_memory_store_from_pack` to accept `only_files`**

Change the signature of `seed_memory_store_from_pack` and thread the filter through.

Replace the current body (lines ~39-98) with:

```python
async def seed_memory_store_from_pack(
    *,
    client: AsyncAnthropic,
    device_slug: str,
    pack_dir: Path,
    only_files: list[str] | None = None,
) -> dict[str, str]:
    """Upsert the pack's JSON artefacts into the device's memory store.

    When `only_files` is supplied, only those filenames (matching names in
    `_SEED_FILES`) are processed — used by the auto-seed path to re-push
    just the files that drifted since the last seed.

    Returns a mapping `{memory_path: "seeded"|"skipped"|"error:<reason>"}`.
    On full or partial successful upsert, a marker is written at
    `pack_dir/managed.json` with the per-file mtimes as-read. Never raises.
    """
    settings = get_settings()
    targets = _SEED_FILES
    if only_files is not None:
        wanted = set(only_files)
        targets = tuple(t for t in _SEED_FILES if t[0] in wanted)

    status: dict[str, str] = {memory_path: "pending" for _file, memory_path in targets}

    if not settings.ma_memory_store_enabled:
        for path in status:
            status[path] = "skipped:flag_disabled"
        logger.debug(
            "[MemorySeed] ma_memory_store_enabled=False — no-op for slug=%s",
            device_slug,
        )
        return status

    store_id = await ensure_memory_store(client, device_slug)
    if store_id is None:
        for path in status:
            status[path] = "skipped:no_store"
        return status

    seeded_mtimes: dict[str, float] = {}
    for file_name, memory_path in targets:
        on_disk = pack_dir / file_name
        if not on_disk.exists():
            status[memory_path] = "skipped:missing_file"
            logger.info(
                "[MemorySeed] Skip %s for slug=%s (no file on disk)",
                memory_path, device_slug,
            )
            continue
        mtime_before = on_disk.stat().st_mtime
        content = on_disk.read_text(encoding="utf-8")
        result = await upsert_memory(
            client, store_id=store_id, path=memory_path, content=content,
        )
        if result is None:
            status[memory_path] = "error:upsert_failed"
            continue
        status[memory_path] = "seeded"
        seeded_mtimes[file_name] = mtime_before
        logger.info(
            "[MemorySeed] Seeded slug=%s path=%s bytes=%d",
            device_slug, memory_path, len(content),
        )

    # Refresh the marker — merge with any existing entries so a partial
    # re-seed doesn't erase the mtimes of files we didn't touch.
    if seeded_mtimes:
        existing = read_seed_marker(pack_dir)
        merged = dict(existing["files"]) if existing else {}
        merged.update(seeded_mtimes)
        write_seed_marker(
            pack_dir=pack_dir,
            store_id=store_id,
            seeded_files=merged,
        )
    return status
```

- [ ] **Step 10: Write failing test for partial re-seed**

Append to `tests/agent/test_memory_seed.py`:

```python
import pytest
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_seed_only_files_uploads_subset(tmp_path: Path, monkeypatch):
    """only_files=['rules.json'] must upsert exactly one path and update the marker."""
    from api.agent import memory_seed as ms_mod

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
```

- [ ] **Step 11: Run — PASS**

Run: `.venv/bin/pytest tests/agent/test_memory_seed.py -v`
Expected: all green.

- [ ] **Step 12: Full tests**

Run: `.venv/bin/make test`
Expected: all green. If a pre-existing test in `test_memory_seed.py` relied on the old signature of `seed_memory_store_from_pack`, it still passes — `only_files=None` is the default and preserves the old behaviour.

- [ ] **Step 13: Commit**

```bash
git add api/agent/memory_seed.py tests/agent/test_memory_seed.py
git commit -m "$(cat <<'EOF'
feat(agent): seed marker + partial re-seed for memory store

Adds managed.json per-device marker (seeded_at, store_id, file mtimes)
and `stale_files_for_pack()` to detect which pack files have drifted
since the last seed. seed_memory_store_from_pack gains an `only_files`
kwarg that restricts the upsert set and merges the marker instead of
clobbering it — foundation for lazy auto-seed at session open.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/agent/memory_seed.py tests/agent/test_memory_seed.py
```

---

## Task 2: Auto-seed on session open

**Why:** with Task 1 in place, we can cheaply check at WS open whether any pack file drifted since the last seed and push just those files. Idempotent — a second session open on an up-to-date pack is a pure marker read plus four `os.stat()`.

**Depends on:** R1 (Task 1 of the main optimization plan) being merged first — both touch `runtime_managed.py` in similar regions.

**Files:**
- Modify: `api/agent/runtime_managed.py` (trigger background seed right after `ensure_memory_store`)
- Test: `tests/agent/test_runtime_managed_autoseed.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/agent/test_runtime_managed_autoseed.py`:

```python
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
```

- [ ] **Step 2: Run — FAIL**

Run: `.venv/bin/pytest tests/agent/test_runtime_managed_autoseed.py -v`
Expected: AttributeError (`maybe_auto_seed` does not exist).

- [ ] **Step 3: Implement `maybe_auto_seed` helper**

In `api/agent/runtime_managed.py`, add near the other module-level helpers (above `run_diagnostic_session_managed`):

```python
async def maybe_auto_seed(
    *,
    client: AsyncAnthropic,
    device_slug: str,
    memory_root: Path,
) -> asyncio.Task | None:
    """Launch a background re-seed of pack files that drifted since last seed.

    Returns the spawned task so callers can optionally await it (e.g. in tests).
    In the normal session path the task is fire-and-forget; its failure is
    logged and the next session open will retry.
    """
    from api.agent.memory_seed import (
        seed_memory_store_from_pack,
        stale_files_for_pack,
    )

    settings = get_settings()
    if not settings.ma_memory_store_enabled:
        return None
    pack_dir = memory_root / device_slug
    if not pack_dir.exists():
        return None
    stale = stale_files_for_pack(pack_dir)
    if not stale:
        return None

    async def _run():
        try:
            await seed_memory_store_from_pack(
                client=client,
                device_slug=device_slug,
                pack_dir=pack_dir,
                only_files=stale,
            )
            logger.info(
                "[Diag-MA] auto-seeded slug=%s files=%s", device_slug, stale,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Diag-MA] auto-seed failed slug=%s files=%s: %s",
                device_slug, stale, exc,
            )

    return asyncio.create_task(_run())
```

- [ ] **Step 4: Wire `maybe_auto_seed` into session open**

In `run_diagnostic_session_managed`, just after `memory_store_id = await ensure_memory_store(...)` (around line 410 in the current tree), add:

```python
    await maybe_auto_seed(client=client, device_slug=device_slug, memory_root=memory_root)
```

The `await` is on `maybe_auto_seed` itself (which finishes as soon as the background task is spawned) — it does NOT await the spawned task. Total added latency at session open = one `os.stat` per pack file (~µs on a warm FS).

If Task 5 (D1) of the main plan has already been merged by the time this is implemented, pass the task through `session_mirrors.spawn(...)` instead of a raw `asyncio.create_task` for durable cleanup. The spec for Task 2 here does NOT require that integration — leave a comment mentioning it:

```python
    # TODO(D1 follow-up): when session_mirrors exists, track this task there.
```

Drop the TODO in the same plan's subsequent commit if you're implementing both.

- [ ] **Step 5: Run — PASS**

Run: `.venv/bin/pytest tests/agent/test_runtime_managed_autoseed.py -v`
Expected: PASS on both tests.

- [ ] **Step 6: Full tests**

Run: `.venv/bin/make test`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add api/agent/runtime_managed.py tests/agent/test_runtime_managed_autoseed.py
git commit -m "$(cat <<'EOF'
feat(agent): auto-seed memory store at session open

Compares pack disk mtimes against managed.json marker; stale files are
pushed to the MA memory store via a background task. Idempotent:
up-to-date packs add one stat per file at session open, zero API calls.

Closes the gap where a pack existed on disk but had never been seeded
(or had been updated after the last seed) — previously the diagnostic
session opened with an empty memory store silently.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/agent/runtime_managed.py tests/agent/test_runtime_managed_autoseed.py
```

---

## Out of scope (intentional)

- **Trou 2 recovery** (store wiped on Anthropic side while marker says "seeded"). Future work: a CLI `python -m api.agent.memory_seed --verify --slug=<slug>` that calls the MA memory list API and force-reseeds on divergence. Not worth the session-open API call for 99% of users.
- **Cross-pack drift discovery.** Single device at a time; no batch sweep.
- **Marker format migration.** If the schema changes later, bump a `version` field and treat absence as v0 — trivial additive evolution.

## Verification checklist

- [x] Placeholder scan — no TBD/TODO (the one `TODO(D1 follow-up)` comment is intentional, called out in Task 2 Step 4).
- [x] Type consistency — `stale_files_for_pack`, `read_seed_marker`, `write_seed_marker`, `maybe_auto_seed` all used with matching signatures.
- [x] Dependency — Task 2 explicitly notes it runs after R1 is merged.
- [x] Scope — 2 tasks, 2 commits, ~120 LOC + tests. Focused side-quest.
