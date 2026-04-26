#!/usr/bin/env python3
"""Reset a device pack to its "demo state" for live demos.

Removes the active schematic_pdf + boardview source files (and their
active_sources.json pins) but KEEPS every derived artefact on disk
(electrical_graph.json, schematic_pages/, schematic_graph.json,
nets_classified.json, etc.).

After this runs, on the dashboard:
  - cards Schematic + Boardview flip back to "to import"
  - electrical_graph stays on disk → has_electrical_graph=True
  - re-uploading the same PDF hits the backend hash cache (instant)
    AND triggers the 12s frontend fake-ingest animation because the
    pre-upload snapshot sees has_electrical_graph=True
  - re-uploading the boardview is just a file persist (no pipeline)

Usage:
    python scripts/reset_demo.py SLUG [SLUG ...]

Example:
    python scripts/reset_demo.py iphone-x mnt-motherboard
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MEMORY_ROOT = REPO_ROOT / "memory"

# Kinds wiped from uploads/ and active_sources.json. Other kinds
# (datasheet, notes, other) are intentionally left intact — they're
# part of the pack knowledge, not the import-flow surface.
DEMO_KINDS = ("schematic_pdf", "boardview")

# Derived artefacts that must survive — these power the cache-hit path
# on the next upload (electrical_graph) and the schematic UI.
KEEP_PATHS = (
    "electrical_graph.json",
    "schematic_graph.json",
    "schematic_pages",
    "nets_classified.json",
    "passive_classification_llm.json",
    "boot_sequence_analyzed.json",
    "simulator_reliability.json",
)


def reset_pack_for_demo(slug: str) -> bool:
    pack = MEMORY_ROOT / slug
    if not pack.is_dir():
        print(f"  no pack at {pack}", file=sys.stderr)
        return False

    uploads = pack / "uploads"
    removed: list[str] = []
    if uploads.is_dir():
        for f in sorted(uploads.iterdir()):
            if not f.is_file():
                continue
            if any(f"-{kind}-" in f.name for kind in DEMO_KINDS):
                f.unlink()
                removed.append(f.name)
                # Also drop the sidecar description if present.
                desc = f.with_name(f.name + ".description.txt")
                if desc.is_file():
                    desc.unlink()

    # The "canonical post-ingest copy" — _apply_schematic_pin copies the
    # uploaded PDF here, and _detect_schematic_pdf checks for it before
    # checking uploads/. Without removing it, has_schematic_pdf stays
    # True and the card never flips back to "to import".
    # Safe to delete because the PDF is also in cache/schematic/{hash}/
    # and restore_from_cache re-materialises it on the next re-upload.
    in_place_pdf = pack / "schematic.pdf"
    if in_place_pdf.is_file():
        in_place_pdf.unlink()
        removed.append(in_place_pdf.name)

    active_path = pack / "active_sources.json"
    pins_dropped: list[str] = []
    if active_path.is_file():
        try:
            pins = json.loads(active_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pins = {}
        if not isinstance(pins, dict):
            pins = {}
        for kind in DEMO_KINDS:
            if kind in pins:
                pins_dropped.append(kind)
                pins.pop(kind)
        active_path.write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8")

    kept = [p for p in KEEP_PATHS if (pack / p).exists()]

    print(f"reset · {slug}")
    print(f"  uploads removed ({len(removed)}): {', '.join(removed) or '(none)'}")
    print(f"  pins dropped:                     {', '.join(pins_dropped) or '(none)'}")
    print(f"  derivatives kept ({len(kept)}):   {', '.join(kept) or '(none)'}")
    return True


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    ok = True
    for slug in argv:
        if not reset_pack_for_demo(slug):
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
