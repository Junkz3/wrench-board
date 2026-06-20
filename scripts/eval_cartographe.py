"""CLI: scorecard of knowledge-graph FICTIONS per device.

A fiction is a `knowledge_graph` component node attested by neither the
compiled electrical graph nor the raw vision/OCR — i.e. the Cartographe
propagated a web-registry phantom (the U6903-on-MacBook class). This is the
metric the registry-reconciliation fix must drive toward zero, WITHOUT
regressing the packs already at zero (iphone-8/11).

Scored only on packs that have BOTH knowledge_graph.json and
electrical_graph.json (a schematic makes the graph the existence authority).

Usage:
  .venv/bin/python -m scripts.eval_cartographe
  .venv/bin/python -m scripts.eval_cartographe --devices macbook-pro-13-2016-2017
  .venv/bin/python -m scripts.eval_cartographe --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from api.pipeline.graph_truth import GraphTruth
from api.pipeline.reconcile import find_kg_fictions, load_seen_refdes
from api.pipeline.schemas import KnowledgeGraph
from api.pipeline.schematic.schemas import ElectricalGraph

REPO_ROOT = Path(__file__).resolve().parent.parent
MEMORY = REPO_ROOT / "memory"


def score_pack(pack: Path) -> dict | None:
    kg_path = pack / "knowledge_graph.json"
    eg_path = pack / "electrical_graph.json"
    if not (kg_path.exists() and eg_path.exists()):
        return None
    try:
        kg = KnowledgeGraph.model_validate_json(kg_path.read_text())
        gt = GraphTruth(ElectricalGraph.model_validate_json(eg_path.read_text()))
    except Exception as exc:  # noqa: BLE001
        return {"device": pack.name, "error": f"{type(exc).__name__}: {exc}"}
    seen = load_seen_refdes(pack)
    comps = [n for n in kg.nodes if n.kind == "component"]
    fictions = find_kg_fictions(kg, gt, seen)
    n = len(comps)
    return {
        "device": pack.name,
        "kg_components": n,
        "fictions": len(fictions),
        "fiction_ratio": round(len(fictions) / n, 3) if n else 0.0,
        "examples": fictions[:8],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--devices", nargs="*", help="subset of slugs (default: all)")
    ap.add_argument("--json", action="store_true", help="machine-readable line")
    args = ap.parse_args()

    packs = (
        [MEMORY / d for d in args.devices]
        if args.devices
        else sorted(p.parent for p in MEMORY.glob("*/knowledge_graph.json"))
    )
    rows = [r for r in (score_pack(p) for p in packs) if r is not None]
    scored = [r for r in rows if "error" not in r]

    total_c = sum(r["kg_components"] for r in scored)
    total_f = sum(r["fictions"] for r in scored)
    mean_ratio = round(sum(r["fiction_ratio"] for r in scored) / len(scored), 3) if scored else 0.0

    if args.json:
        print(json.dumps({"mean_fiction_ratio": mean_ratio, "total_fictions": total_f, "devices": rows}))
        return 0

    print(f"{'device':34}{'kg_comp':>8}{'fiction':>8}{'ratio':>7}   examples")
    print("-" * 78)
    for r in rows:
        if "error" in r:
            print(f"{r['device']:34}  {r['error']}")
            continue
        print(f"{r['device']:34}{r['kg_components']:>8}{r['fictions']:>8}{r['fiction_ratio']:>7.0%}   {', '.join(r['examples'])}")
    print("-" * 78)
    print(f"{'MEAN / TOTAL':34}{total_c:>8}{total_f:>8}{mean_ratio:>7.0%}   ({len(scored)} packs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
