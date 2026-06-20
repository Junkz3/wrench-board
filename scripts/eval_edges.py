"""CLI: scorecard of knowledge-graph CONTRADICTED power edges per device.

A contradicted edge is a kg `powers`/`drives` claim `U_src → RAIL` that the
compiled electrical graph attributes to a DIFFERENT source-capable component —
the EDGE equivalent of a component fiction. This is the precision metric the
edge-reconciliation guard drives toward zero, WITHOUT touching the in-line-passive
artefacts (`who_powers` returning a fuse/series-resistor/rectifier) that look like
contradictions but are the kg's correct IC attribution.

Scored only on packs that have BOTH knowledge_graph.json and electrical_graph.json
(a schematic makes the graph the connectivity authority).

Usage:
  .venv/bin/python -m scripts.eval_edges
  .venv/bin/python -m scripts.eval_edges --devices macbook-pro-13-2016-2017 iphone-8
  .venv/bin/python -m scripts.eval_edges --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from api.pipeline.drift import compute_drift
from api.pipeline.graph_truth import GraphTruth
from api.pipeline.reconcile import find_contradicted_edges, prune_contradicted_edges
from api.pipeline.schemas import Dictionary, KnowledgeGraph, RulesSet
from api.pipeline.schematic.schemas import ElectricalGraph

REPO_ROOT = Path(__file__).resolve().parent.parent
MEMORY = REPO_ROOT / "memory"

_POWER_RELATIONS = {"powers", "drives"}


def score_pack(pack: Path) -> dict | None:
    kg_path = pack / "knowledge_graph.json"
    eg_path = pack / "electrical_graph.json"
    if not (kg_path.exists() and eg_path.exists()):
        return None
    try:
        kg = KnowledgeGraph.model_validate_json(kg_path.read_text())
        gt = GraphTruth(ElectricalGraph.model_validate_json(eg_path.read_text()))
    except Exception as exc:  # noqa: BLE001
        # First line only — legacy pre-T8 packs (comp:/net: node ids) raise a
        # multi-line Pydantic ValidationError that would swamp the table.
        summary = str(exc).splitlines()[0]
        return {"device": pack.name, "error": f"{type(exc).__name__}: {summary}"}
    power_edges = [
        e
        for e in kg.edges
        if e.relation in _POWER_RELATIONS and e.target_id.startswith("N-NET_")
    ]
    contras = find_contradicted_edges(kg, gt)
    n = len(power_edges)
    return {
        "device": pack.name,
        "power_edges": n,
        "contradicted": len(contras),
        "ratio": round(len(contras) / n, 3) if n else 0.0,
        "examples": [
            f"{c.src} {c.relation} {c.rail} (graph:{'/'.join(c.graph_sources)})"
            for c in contras[:6]
        ],
    }


def _orphans(drift) -> set[str]:
    """All node ids flagged as orphans across drift items (for the before/after
    collateral check — pruning must not strand a node)."""
    out: set[str] = set()
    for d in drift:
        if "orphan" in d.reason:
            out.update(d.mentions)
    return out


def simulate_hybrid(pack: Path) -> dict | None:
    """Deterministic floor of the hybrid: run drift, apply the backstop prune,
    re-run drift. Shows the shippable result the pipeline would converge to even
    if the LLM revise-loop fixed nothing — and surfaces any collateral (a node
    orphaned by the prune)."""
    kg_path = pack / "knowledge_graph.json"
    eg_path = pack / "electrical_graph.json"
    if not (kg_path.exists() and eg_path.exists()):
        return None
    try:
        kg = KnowledgeGraph.model_validate_json(kg_path.read_text())
        gt = GraphTruth(ElectricalGraph.model_validate_json(eg_path.read_text()))
    except Exception as exc:  # noqa: BLE001
        return {"device": pack.name, "error": f"{type(exc).__name__}: {str(exc).splitlines()[0]}"}

    # Real siblings when present, else empty — keeps the drift total faithful.
    from api.pipeline.schemas import Registry

    def _load(name, model, default):
        p = pack / name
        try:
            return model.model_validate_json(p.read_text()) if p.exists() else default
        except Exception:  # noqa: BLE001
            return default

    registry = _load("registry.json", Registry, Registry(device_label=pack.name, components=[], signals=[]))
    rules = _load("rules.json", RulesSet, RulesSet(rules=[]))
    dictionary = _load("dictionary.json", Dictionary, Dictionary(entries=[]))

    def _drift(g):
        return compute_drift(registry=registry, knowledge_graph=g, rules=rules, dictionary=dictionary, graph_truth=gt)

    before = _drift(kg)
    pruned_kg, removed = prune_contradicted_edges(kg, gt)
    after = _drift(pruned_kg)
    new_orphans = _orphans(after) - _orphans(before)
    return {
        "device": pack.name,
        "edges_before": len(kg.edges),
        "edges_after": len(pruned_kg.edges),
        "pruned": [f"{c.src} {c.relation} {c.rail}→{'/'.join(c.graph_sources)}" for c in removed],
        "contra_after": len(find_contradicted_edges(pruned_kg, gt)),
        "drift_before": len(before),
        "drift_after": len(after),
        "new_orphans": sorted(new_orphans),
    }


def run_hybrid(packs: list[Path]) -> int:
    rows = [r for r in (simulate_hybrid(p) for p in packs) if r is not None]
    print(f"{'device':34}{'edges':>12}{'pruned':>8}{'contra_aft':>12}{'drift b→a':>12}{'orphans+':>10}")
    print("-" * 100)
    for r in rows:
        if "error" in r:
            print(f"{r['device']:34}  {r['error']}")
            continue
        edges = f"{r['edges_before']}→{r['edges_after']}"
        drift = f"{r['drift_before']}→{r['drift_after']}"
        print(
            f"{r['device']:34}{edges:>12}{len(r['pruned']):>8}{r['contra_after']:>12}"
            f"{drift:>12}{len(r['new_orphans']):>10}"
        )
        for p in r["pruned"]:
            print(f"      pruned: {p}")
        if r["new_orphans"]:
            print(f"      !! NEW ORPHANS: {r['new_orphans']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--devices", nargs="*", help="subset of slugs (default: all)")
    ap.add_argument("--json", action="store_true", help="machine-readable line")
    ap.add_argument("--hybrid", action="store_true", help="simulate the drift→prune→drift hybrid result")
    args = ap.parse_args()

    packs = (
        [MEMORY / d for d in args.devices]
        if args.devices
        else sorted(p.parent for p in MEMORY.glob("*/knowledge_graph.json"))
    )
    if args.hybrid:
        return run_hybrid(packs)
    rows = [r for r in (score_pack(p) for p in packs) if r is not None]
    scored = [r for r in rows if "error" not in r]

    total_e = sum(r["power_edges"] for r in scored)
    total_c = sum(r["contradicted"] for r in scored)
    mean_ratio = (
        round(sum(r["ratio"] for r in scored) / len(scored), 3) if scored else 0.0
    )

    if args.json:
        print(json.dumps({"mean_ratio": mean_ratio, "total_contradicted": total_c, "devices": rows}))
        return 0

    print(f"{'device':34}{'pwr_edges':>10}{'contra':>8}{'ratio':>7}   examples")
    print("-" * 100)
    for r in rows:
        if "error" in r:
            print(f"{r['device']:34}  {r['error']}")
            continue
        print(
            f"{r['device']:34}{r['power_edges']:>10}{r['contradicted']:>8}{r['ratio']:>7.0%}"
            f"   {'; '.join(r['examples'])}"
        )
    print("-" * 100)
    print(f"{'TOTAL':34}{total_e:>10}{total_c:>8}{mean_ratio:>7.0%}   ({len(scored)} packs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
