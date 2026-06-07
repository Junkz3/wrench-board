#!/usr/bin/env python3
"""Re-derive `electrical_graph.json` from existing on-disk artefacts.

Offline equivalent of the orchestrator's post-classifier re-compile: reads
`schematic_graph.json` + `nets_classified.json` from `memory/{slug}/`,
promotes every net with `domain=power_rail` (≥ min-conf) to `is_power=True`
on the schematic graph, re-runs `compile_electrical_graph`, re-applies any
passive role fills found in `passive_classification_llm.json`, and
overwrites `electrical_graph.json`.

Use when the pipeline code's rail-promotion behaviour changes and existing
packs need to catch up without paying for a full LLM re-ingest.

Usage:
    .venv/bin/python scripts/regen_electrical_graph.py --slug mnt-reform-motherboard
    .venv/bin/python scripts/regen_electrical_graph.py --all
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from api.pipeline.schematic.compiler import compile_electrical_graph
from api.pipeline.schematic.net_classifier import apply_power_rail_classification
from api.pipeline.schematic.schemas import (
    NetClassification,
    SchematicGraph,
)

logger = logging.getLogger("wrench_board.scripts.regen_electrical_graph")


def regenerate(device_slug: str, memory_root: Path, *, min_confidence: float = 0.7) -> dict:
    """Re-compile the electrical graph for one device. Returns a summary dict."""
    device_dir = memory_root / device_slug
    sg_path = device_dir / "schematic_graph.json"
    eg_path = device_dir / "electrical_graph.json"
    cls_path = device_dir / "nets_classified.json"
    passive_llm_path = device_dir / "passive_classification_llm.json"

    if not sg_path.exists() or not eg_path.exists():
        return {"slug": device_slug, "status": "skipped", "reason": "missing_artefacts"}

    schematic_graph = SchematicGraph.model_validate_json(sg_path.read_text())

    page_confidences = {int(p): 1.0 for p in range(1, schematic_graph.page_count + 1)}
    # Honour the pages' recorded confidence when available.
    try:
        for page_idx in page_confidences:
            page_json = device_dir / "schematic_pages" / f"page_{page_idx:03d}.json"
            if page_json.exists():
                data = json.loads(page_json.read_text())
                page_confidences[page_idx] = float(data.get("confidence", 1.0))
    except Exception:  # noqa: BLE001
        logger.warning("could not read page confidences for %s", device_slug)

    # Self-contained derivation: re-run the baseline compile on the
    # pre-promotion schematic graph to recover the heuristic passive
    # roles, then layer passive_classification_llm.json on top to rebuild
    # the full pre-promotion role map. This is independent of whatever
    # the current electrical_graph.json contains, so the script is
    # idempotent even if a prior run wrote degraded data.
    baseline = compile_electrical_graph(
        schematic_graph, page_confidences=page_confidences
    )
    rails_before = len(baseline.power_rails)
    preserved_roles: dict[str, tuple[str, str]] = {
        refdes: (comp.kind, comp.role)
        for refdes, comp in baseline.components.items()
        if comp.kind != "ic" and comp.role is not None
    }
    if passive_llm_path.exists():
        try:
            payload = json.loads(passive_llm_path.read_text())
            for entry in payload.get("assignments", []):
                refdes = entry.get("refdes")
                kind = entry.get("kind")
                role = entry.get("role")
                if refdes and kind and role and refdes not in preserved_roles:
                    preserved_roles[refdes] = (kind, role)
        except Exception:  # noqa: BLE001
            logger.warning(
                "could not read passive_classification_llm.json for %s",
                device_slug, exc_info=True,
            )

    promoted: list[str] = []
    if cls_path.exists():
        classification = NetClassification.model_validate_json(cls_path.read_text())
        promoted = apply_power_rail_classification(
            schematic_graph, classification, min_confidence=min_confidence
        )

    recompiled = compile_electrical_graph(
        schematic_graph, page_confidences=page_confidences
    )

    # Restore the pre-regen roles onto components whose fresh heuristic
    # role is still None. We trust the old classification (heuristic + LLM)
    # over the freshly-re-run heuristic because the heuristic is topology-
    # sensitive and promotion may have shifted its rule path.
    reapplied = 0
    enriched = dict(recompiled.components)
    for refdes, (kind, role) in preserved_roles.items():
        node = enriched.get(refdes)
        if node is None or role is None:
            continue
        if node.role is None:
            enriched[refdes] = node.model_copy(update={"kind": kind, "role": role})
            reapplied += 1
    recompiled.__dict__["components"] = enriched

    eg_path.write_text(recompiled.model_dump_json(indent=2))
    rails_after = len(recompiled.power_rails)
    return {
        "slug": device_slug,
        "status": "ok",
        "rails_before": rails_before,
        "rails_after": rails_after,
        "rails_added": rails_after - rails_before,
        "promoted_nets": promoted,
        "passive_fills_reapplied": reapplied,
    }


def _iter_device_slugs(memory_root: Path) -> list[str]:
    slugs: list[str] = []
    for child in sorted(memory_root.iterdir()):
        if child.is_dir() and (child / "schematic_graph.json").exists():
            slugs.append(child.name)
    return slugs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", help="device slug under memory/")
    parser.add_argument("--all", action="store_true", help="regenerate every pack")
    parser.add_argument(
        "--memory-root", default="memory",
        help="memory root directory (default: memory)",
    )
    parser.add_argument(
        "--min-confidence", type=float, default=0.7,
        help="minimum classifier confidence to promote a net (default: 0.7)",
    )
    args = parser.parse_args()

    if not args.slug and not args.all:
        parser.error("either --slug or --all is required")

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    memory_root = Path(args.memory_root).resolve()

    slugs = _iter_device_slugs(memory_root) if args.all else [args.slug]

    results = []
    for slug in slugs:
        summary = regenerate(slug, memory_root, min_confidence=args.min_confidence)
        results.append(summary)
        print(json.dumps(summary, indent=2))

    if args.all:
        print("\n=== summary ===")
        total_added = sum(r.get("rails_added", 0) for r in results if r["status"] == "ok")
        print(f"devices processed: {len(results)}  rails added total: {total_added}")


if __name__ == "__main__":
    main()
