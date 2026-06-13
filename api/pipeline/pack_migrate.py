"""Idempotent in-place migration from the legacy layout to the provenance layout.

Triggered on first access to a slug (see _load_pack in api/agent/tools.py) or
explicitly by the admin CLI.

Strategy:
1. If .migrated_t8 exists → no-op
2. If registry.json exists at the root (legacy layout detected) → migrate:
   - init_pack_layout creates baseline/promoted/_staged/expansions/audit
   - the 4 pack JSONs are moved into baseline/, attaching a synthetic
     baseline-pre-T8 Provenance to each fact ({items: [...], _meta: {...}})
   - raw_research_dump.md is moved into audit/ (private / engine audit)
   - a 'baseline-pre-T8' line is appended to the journal (status=baseline,
     non-revocable by design — see revoke_expansion in pack_storage.py)
3. touch .migrated_t8

Idempotent: a crash mid-run can leave a pack half-migrated; the next call
detects the missing flag and resumes. Each file is handled with
write-then-rename (_atomic_write_json); if the destination already exists
(resume), the legacy source is simply removed.

Heterogeneous legacy formats are normalized to {items: [...]}.
List-bearing keys, verified against the real packs on disk:
  registry.json       : {"components": [...], "signals": [...]} → concatenated
  rules.json          : {"rules": [...]}
  knowledge_graph.json: {"nodes": [...], "edges": [...]} → concatenated
  dictionary.json     : {"entries": [...]}  ← 'entries', NOT 'components'

Non-list keys (schema_version, device_label, taxonomy, …) are preserved under
a _meta key in the migrated baseline file — zero loss. load_effective_pack
ignores _meta (it only reads items); the loader wires taxonomy/device_label
from _meta when it needs them.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

from api.pipeline.pack_storage import (
    JournalEntry,
    _atomic_write_json,
    append_journal,
    init_pack_layout,
    read_journal,
)

# Mapping: legacy filename → (target name in baseline/, items key in the legacy JSON)
_LEGACY_FILE_TO_T8: dict[str, tuple[str, str]] = {
    "registry.json":       ("registry.json",       "registry"),
    "rules.json":          ("rules.json",           "rules"),
    "knowledge_graph.json":("knowledge_graph.json", "knowledge_graph"),
    "dictionary.json":     ("dictionary.json",      "dictionary"),
}


def migrate_pack_if_needed(memory_root: Path, slug: str) -> None:
    """Idempotent entry point. Costs one stat() if already migrated.

    - If the slug directory does not exist → silent no-op.
    - If .migrated_t8 is present → no-op (pack already in the migrated format).
    - If no legacy file is detected (empty directory, or already partially
      migrated without the flag) → create the layout + set the flag with no
      journal write.
    """
    pack = memory_root / slug
    if not pack.is_dir():
        return

    flag = pack / ".migrated_t8"
    if flag.is_file():
        return

    # Create the layout subdirectories (idempotent).
    init_pack_layout(memory_root, slug)

    legacy_present = any((pack / fname).is_file() for fname in _LEGACY_FILE_TO_T8)

    if legacy_present:
        _migrate_legacy_files(pack)
        _migrate_raw_dump(pack)
        _create_baseline_journal_entry(memory_root, slug, pack)

    # The flag is set unconditionally — even for an empty directory.
    # Valid assumption: new-pipeline packs write directly in the native
    # format; legacy files, if any, are ALWAYS present before the first call
    # to migrate_pack_if_needed (which is triggered from _load_pack, after the
    # pack build has finished). An empty slug directory = new-pipeline pack,
    # no migration needed.
    flag.touch()


def _migrate_legacy_files(pack: Path) -> None:
    """Move the 4 legacy JSONs into baseline/, wrapping each entry with a
    synthetic baseline-pre-T8 Provenance.

    Precondition: at least one legacy file is present (checked by the caller
    via `legacy_present`), so max() runs over a non-empty iterator.
    """
    # Use the most recent file's mtime as the provenance timestamp
    # (an approximation of the original pack's creation date).
    file_mtime = max(
        (pack / fname).stat().st_mtime
        for fname in _LEGACY_FILE_TO_T8
        if (pack / fname).is_file()
    )
    file_mtime_iso = datetime.fromtimestamp(file_mtime, tz=UTC).isoformat()

    base_prov = {
        "expansion_id": "baseline-pre-T8",
        "added_at": file_mtime_iso,
        "added_by_tenant": None,
        "confidence": 1.0,
        "source_kind": "baseline",
        "sanitizer_actions": [],
        "status": "baseline",
    }

    for legacy_name, (t8_name, items_field) in _LEGACY_FILE_TO_T8.items():
        src = pack / legacy_name
        dst = pack / "baseline" / t8_name
        if not src.is_file():
            continue
        # Crash resume: the destination already exists → remove the source.
        if dst.is_file():
            src.unlink(missing_ok=True)
            continue
        try:
            legacy_data = json.loads(src.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # Corrupt JSON: remove it and leave baseline empty for this file.
            src.unlink(missing_ok=True)
            continue

        items = _flatten_legacy_payload(legacy_data, items_field)
        # Non-list keys to keep (schema_version, device_label, taxonomy, …).
        # _meta is preserved-but-not-yet-consumed by load_effective_pack;
        # the loader wires taxonomy/device_label from _meta later.
        list_keys = _LIST_KEYS[items_field]
        meta = {k: v for k, v in legacy_data.items() if k not in list_keys}

        for it in items:
            # Do not overwrite a provenance already present (half-migrated pack).
            # deep-copy: each fact gets its own sanitizer_actions list —
            # no shared alias between facts (avoids silent mutations).
            it.setdefault("_provenance", copy.deepcopy(base_prov))

        payload: dict = {"items": items}
        if meta:
            payload["_meta"] = meta
        _atomic_write_json(dst, payload)
        src.unlink()


# List-bearing keys for each file type.
# Verified against the real packs on disk.
# Any key absent from this tuple is treated as metadata and preserved in _meta.
_LIST_KEYS: dict[str, tuple[str, ...]] = {
    "registry":       ("components", "signals"),
    "rules":          ("rules",),
    "knowledge_graph": ("nodes", "edges"),
    "dictionary":     ("entries",),   # 'entries', NOT 'components' (bug fixed)
}


def _flatten_legacy_payload(legacy_data: dict, kind: str) -> list[dict]:
    """Normalize the heterogeneous legacy pack formats into a flat list of items.

    List-bearing keys (verified against the real packs):
      registry.json       → components + signals concatenated
      rules.json          → rules
      knowledge_graph.json→ nodes + edges concatenated
      dictionary.json     → entries  (NOT 'components', as originally assumed)
    """
    if kind == "registry":
        return (
            list(legacy_data.get("components") or [])
            + list(legacy_data.get("signals") or [])
        )
    if kind == "rules":
        return list(legacy_data.get("rules") or [])
    if kind == "knowledge_graph":
        return (
            list(legacy_data.get("nodes") or [])
            + list(legacy_data.get("edges") or [])
        )
    if kind == "dictionary":
        # BUG FIXED: the Dictionary schema and all real packs use 'entries',
        # not 'components'. Reading 'components' produced {items: []} (empty
        # list) and dropped the source → permanent loss of component entries.
        return list(legacy_data.get("entries") or [])
    # Exhaustive case — should never happen given _LEGACY_FILE_TO_T8.
    return []


def _migrate_raw_dump(pack: Path) -> None:
    """Move raw_research_dump.md into audit/ (raw data, engine-private)."""
    src = pack / "raw_research_dump.md"
    if not src.is_file():
        return
    dst = pack / "audit" / "raw_research_dump.md"
    if dst.is_file():
        # Resume: destination already present, source to clean up.
        src.unlink(missing_ok=True)
        return
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    src.unlink()


def _create_baseline_journal_entry(memory_root: Path, slug: str, pack: Path) -> None:
    """Append the baseline-pre-T8 line to the journal if not already there.

    This entry is non-revocable by design (see revoke_expansion in
    pack_storage.py, which explicitly refuses expansion_id == 'baseline-pre-T8').
    """
    existing = list(read_journal(memory_root, slug))
    if any(e.id == "baseline-pre-T8" for e in existing):
        return

    # The baseline files' mtime is our best approximation of the original
    # pack's creation time.
    baseline_dir = pack / "baseline"
    ts = datetime.now(UTC)
    for fname in _LEGACY_FILE_TO_T8:
        candidate = baseline_dir / fname
        if candidate.is_file():
            ts = datetime.fromtimestamp(candidate.stat().st_mtime, tz=UTC)
            break

    append_journal(
        memory_root,
        slug,
        JournalEntry(
            id="baseline-pre-T8",
            ts=ts,
            owner_ref=None,
            slug=slug,
            focus_symptoms=[],
            focus_refdes=[],
            delta_summary={
                "new_components": [],
                "new_rules": [],
                "new_nodes": [],
                "new_edges": [],
            },
            scout_dump_range={"start": 0, "end": 0},
            status="baseline",
        ),
    )
