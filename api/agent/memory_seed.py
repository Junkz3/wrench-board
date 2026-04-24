# SPDX-License-Identifier: Apache-2.0
"""Seed a device's Managed-Agents memory store from its on-disk knowledge pack.

Called from the pipeline orchestrator right after an APPROVED verdict. The
diagnostic conversation for this device can then consult the canonical
knowledge (registry, rules, dictionary, knowledge graph) natively via the
built-in memory tools instead of re-hydrating it from disk on every tool
call.

Feature-gated behind `settings.ma_memory_store_enabled`. Every error path
degrades to a log warning: the pipeline must never fail because memory
seeding failed.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from anthropic import AsyncAnthropic

from api.agent.memory_stores import ensure_memory_store, upsert_memory
from api.config import get_settings

logger = logging.getLogger("microsolder.agent.memory_seed")

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
    """Write the marker. `seeded_files` maps filename → mtime-at-seed-time.

    Merges with any existing `managed.json` so the `memory_store_id` +
    `device_slug` keys written by `ensure_memory_store` survive a re-seed
    — otherwise subsequent `ensure_memory_store` calls would recreate the
    store and orphan the first one.
    """
    pack_dir.mkdir(parents=True, exist_ok=True)
    path = pack_dir / MARKER_FILENAME
    existing: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (json.JSONDecodeError, OSError):
            existing = {}
    existing.update({
        "seeded_at": datetime.now(UTC).isoformat(),
        "store_id": store_id,
        "files": seeded_files,
    })
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


# Files we push into the store and the memory path they land on. Path scheme
# `/knowledge/*` is reserved for pipeline-authored memories; `/field_reports/*`
# is for write-backs from diagnostic sessions (see record_field_report).
_SEED_FILES = (
    ("registry.json", "/knowledge/registry.json"),
    ("knowledge_graph.json", "/knowledge/knowledge_graph.json"),
    ("rules.json", "/knowledge/rules.json"),
    ("dictionary.json", "/knowledge/dictionary.json"),
    ("electrical_graph.json", "/knowledge/electrical_graph.json"),
    ("boot_sequence_analyzed.json", "/knowledge/boot_sequence_analyzed.json"),
    ("nets_classified.json", "/knowledge/nets_classified.json"),
)


def stale_files_for_pack(pack_dir: Path) -> list[str]:
    """Return the filenames in `_SEED_FILES` that need re-seeding.

    A file is stale when:
      - it exists on disk AND
      - either the marker is missing, or the marker's recorded mtime for
        that file is older than the current on-disk mtime.

    Files absent from disk are ignored (nothing to seed).
    """
    marker = read_seed_marker(pack_dir)
    marker_files = (marker or {}).get("files", {})

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
        merged = dict((existing or {}).get("files") or {})
        merged.update(seeded_mtimes)
        write_seed_marker(
            pack_dir=pack_dir,
            store_id=store_id,
            seeded_files=merged,
        )
    return status
