# scripts/build_demo_pack.py
"""Author a sanitized demo-pack fixture from a live knowledge pack.

We ship the MNT Reform pack so the first-run example tour shows full tabs for
everyone (memory/ is gitignored → never shipped). Only the SHARED, non-tenant
baseline is published: the analyzed graph + rendered pages + indices. Private
or tenant-attributed layers (audit raw research, promoted/staged expansions,
_sources pins, owner_ref) are excluded/scrubbed so no PII or per-tenant data
leaks into git. Re-run after rebuilding the pack; commit the output.

Usage:
    python -m scripts.build_demo_pack \
        memory/mnt-reform-motherboard \
        fixtures/demo-packs/mnt-reform-motherboard
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

# Top-level entries copied verbatim when present (files or dirs).
INCLUDE = (
    "baseline",
    "schematic_pages",
    "electrical_graph.json",
    "schematic_graph.json",
    "parts_index.json",
    "nets_classified.json",
    "passive_classification_llm.json",
    "boot_sequence_analyzed.json",
    "refdes_attributions.json",
    "dictionary.json",
    "registry.json",
    "rules.json",
    "knowledge_graph.json",
)
# Fixed timestamp so the committed fixture is reproducible (no churn on rebuild).
_FIXED_CREATED_AT = "2026-06-01T00:00:00+00:00"


def _scrub_owner_ref(path: Path) -> None:
    """Drop any top-level owner_ref from a kept JSON (defense-in-depth)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(data, dict) and "owner_ref" in data:
        data.pop("owner_ref", None)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def build_demo_pack(
    src: Path,
    dest: Path,
    *,
    example_repair_id: str,
    device_label: str,
    symptom: str,
) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    for name in INCLUDE:
        s = src / name
        if not s.exists():
            continue
        d = dest / name
        if s.is_dir():
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)

    # Scrub owner_ref from every kept JSON (baseline/* and root files).
    for p in dest.rglob("*.json"):
        _scrub_owner_ref(p)

    # Write the single clean example repair (replaces any source repairs).
    repairs = dest / "repairs"
    repairs.mkdir(exist_ok=True)
    (repairs / f"{example_repair_id}.json").write_text(
        json.dumps(
            {
                "repair_id": example_repair_id,
                "device_slug": dest.name,
                "device_label": device_label,
                "symptom": symptom,
                "status": "open",
                "created_at": _FIXED_CREATED_AT,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Author a sanitized demo-pack fixture.")
    ap.add_argument("src", type=Path)
    ap.add_argument("dest", type=Path)
    ap.add_argument("--example-repair-id", default="example-mnt-reform")
    ap.add_argument("--device-label", default="MNT Reform Motherboard")
    ap.add_argument("--symptom", default="Ne démarre pas — aucune alimentation")
    args = ap.parse_args()
    build_demo_pack(
        args.src,
        args.dest,
        example_repair_id=args.example_repair_id,
        device_label=args.device_label,
        symptom=args.symptom,
    )
    print(f"[build_demo_pack] wrote {args.dest}")


if __name__ == "__main__":
    main()
