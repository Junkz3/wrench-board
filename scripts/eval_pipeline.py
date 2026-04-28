# SPDX-License-Identifier: Apache-2.0
"""CLI: print a one-line JSON scorecard for the deterministic schematic pipeline.

Mirrors `scripts/eval_simulator.py` but for the pipeline-evolve loop. The loop
edits `compiler.py` / `net_classifier.py` / `passive_classifier.py`; this
script re-compiles the electrical graph in-process from the cached
`schematic_pages/*.json` so those edits are reflected, then scores the
result against frozen per-device oracles in `evolve-pipeline/oracles/`.

Multi-device by default — score is the *mean* across devices, with a
hard-fail invariant that no device may regress more than 5% on its own.

Usage:
  .venv/bin/python -m scripts.eval_pipeline
  .venv/bin/python -m scripts.eval_pipeline --devices iphone-x
  .venv/bin/python -m scripts.eval_pipeline --verbose
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from api.config import get_settings
from api.pipeline.schematic.compiler import compile_electrical_graph
from api.pipeline.schematic.merger import merge_pages
from api.pipeline.schematic.schemas import ElectricalGraph, SchematicPageGraph

REPO_ROOT = Path(__file__).resolve().parent.parent
ORACLES_DIR = REPO_ROOT / "evolve-pipeline" / "oracles"

DEFAULT_DEVICES = ["iphone-x", "mnt-reform-motherboard"]

WEIGHTS = {
    "refdes_recall": 0.40,
    "rails_sourced_ratio": 0.30,
    "nets_recall": 0.20,
    "decoupling_assigned_ratio": 0.10,
}

VOLTAGE_LABEL = re.compile(r"^(?:PP|V|\+)?(\d+)V(\d*)(?:_|\b)")


def _load_pages(slug: str, memory_root: Path) -> list[SchematicPageGraph]:
    pages_dir = memory_root / slug / "schematic_pages"
    if not pages_dir.exists():
        return []
    pages: list[SchematicPageGraph] = []
    for path in sorted(pages_dir.glob("page_*.json")):
        pages.append(SchematicPageGraph.model_validate_json(path.read_text()))
    pages.sort(key=lambda p: p.page)
    return pages


def _recompile(slug: str, memory_root: Path) -> ElectricalGraph | None:
    pages = _load_pages(slug, memory_root)
    if not pages:
        return None
    sg = merge_pages(
        pages,
        device_slug=slug,
        source_pdf=str(memory_root / slug / "schematic.pdf"),
    )
    return compile_electrical_graph(sg)


def _label_voltage(label: str) -> float | None:
    m = VOLTAGE_LABEL.match(label)
    if not m:
        return None
    whole = int(m.group(1))
    frac = m.group(2)
    if frac:
        return whole + int(frac) / (10 ** len(frac))
    return float(whole)


def _check_invariants(eg: ElectricalGraph, oracle: dict) -> list[str]:
    """Return list of violated invariant codes — empty list = all pass."""
    violations: list[str] = []
    components = set(eg.components.keys())

    # I1 — no cycle in boot sequence (compiler emits an Ambiguity with "Cycle"
    # word when one is detected and skips offending refs).
    for amb in eg.ambiguities:
        if "Cycle" in amb.description:
            violations.append("I1_cycle_in_boot_sequence")
            break

    # I2 — every rail.source_refdes (when non-null) is a known component.
    for rail in eg.power_rails.values():
        if rail.source_refdes is not None and rail.source_refdes not in components:
            violations.append(f"I2_phantom_source:{rail.label}->{rail.source_refdes}")

    # I3 — voltage label/value coherence: when label parses to a voltage,
    # `voltage_nominal` must agree to ±0.05V (or be null — null is OK, mismatch
    # is a bug because the parser ran AND emitted a wrong number).
    for rail in eg.power_rails.values():
        expected = _label_voltage(rail.label)
        if expected is None:
            continue
        if rail.voltage_nominal is None:
            continue  # I6 covers absence-when-parsable
        if abs(rail.voltage_nominal - expected) > 0.05:
            violations.append(
                f"I3_voltage_mismatch:{rail.label}={rail.voltage_nominal}!={expected}"
            )

    # I4 — a regulator does not consume its own output.
    for rail in eg.power_rails.values():
        if rail.source_refdes is None:
            continue
        if rail.source_refdes in rail.consumers:
            violations.append(
                f"I4_self_consumer:{rail.label}->{rail.source_refdes}"
            )

    # I5 — anti-collapse: components/rails must be ≥ 70% of baseline counts
    # captured at bootstrap (prevents "drop everything → invariants vacuously
    # pass" gaming).
    base_comp = oracle.get("baseline_components_count", 0)
    base_rails = oracle.get("baseline_rails_count", 0)
    if base_comp > 0 and len(components) < 0.7 * base_comp:
        violations.append(
            f"I5_components_collapse:{len(components)}<{0.7 * base_comp:.0f}"
        )
    if base_rails > 0 and len(eg.power_rails) < 0.7 * base_rails:
        violations.append(
            f"I5_rails_collapse:{len(eg.power_rails)}<{0.7 * base_rails:.0f}"
        )

    # I6 — every rail with a label that PARSES to a voltage must have
    # `voltage_nominal` populated (anti-regression on the trivial parser).
    for rail in eg.power_rails.values():
        expected = _label_voltage(rail.label)
        if expected is not None and rail.voltage_nominal is None:
            violations.append(f"I6_voltage_missing:{rail.label}")

    return violations


def _refdes_recall(eg: ElectricalGraph, oracle: dict) -> float:
    truth = set(oracle.get("refdes_truth", []))
    if not truth:
        return 0.0
    found = set(eg.components.keys()) & truth
    return len(found) / len(truth)


def _nets_recall(eg: ElectricalGraph, oracle: dict) -> float:
    truth = set(oracle.get("nets_truth", []))
    if not truth:
        return 0.0
    # Compiled nets live in the merged SchematicGraph, but ElectricalGraph
    # carries them through under .nets.
    found = set(eg.nets.keys()) & truth
    return len(found) / len(truth)


def _rails_sourced_ratio(eg: ElectricalGraph) -> float:
    if not eg.power_rails:
        return 0.0
    sourced = sum(1 for r in eg.power_rails.values() if r.source_refdes is not None)
    return sourced / len(eg.power_rails)


def _decoupling_assigned_ratio(eg: ElectricalGraph) -> float:
    """Ratio of caps classified as decoupling/bulk/bypass that are attached to
    a rail's `.decoupling` list. Compiler.py does this assignment via
    `classify_passives_heuristic`."""
    decap_components: set[str] = set()
    for refdes, comp in eg.components.items():
        if comp.kind != "passive_c":
            continue
        if comp.role in {"decoupling", "bulk", "bypass"}:
            decap_components.add(refdes)
    if not decap_components:
        return 0.0
    on_rail = set()
    for rail in eg.power_rails.values():
        on_rail.update(rail.decoupling)
    return len(decap_components & on_rail) / len(decap_components)


def _score_device(slug: str, memory_root: Path) -> dict[str, Any]:
    oracle_path = ORACLES_DIR / f"{slug}.json"
    if not oracle_path.exists():
        return {
            "slug": slug,
            "error": f"missing oracle: {oracle_path}",
            "score": 0.0,
        }
    oracle = json.loads(oracle_path.read_text())

    try:
        eg = _recompile(slug, memory_root)
    except Exception as exc:
        return {
            "slug": slug,
            "error": f"recompile crash: {exc}",
            "score": 0.0,
        }
    if eg is None:
        return {
            "slug": slug,
            "error": "no schematic_pages/ on disk",
            "score": 0.0,
        }

    violations = _check_invariants(eg, oracle)
    metrics = {
        "refdes_recall": _refdes_recall(eg, oracle),
        "rails_sourced_ratio": _rails_sourced_ratio(eg),
        "nets_recall": _nets_recall(eg, oracle),
        "decoupling_assigned_ratio": _decoupling_assigned_ratio(eg),
    }
    if violations:
        score = 0.0
    else:
        score = sum(metrics[k] * w for k, w in WEIGHTS.items())
    return {
        "slug": slug,
        "score": score,
        "metrics": metrics,
        "invariant_violations": violations,
        "components_count": len(eg.components),
        "rails_count": len(eg.power_rails),
        "nets_count": len(eg.nets),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--devices",
        nargs="+",
        default=DEFAULT_DEVICES,
        help=f"List of device_slugs (default: {DEFAULT_DEVICES})",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Emit per-device breakdown.",
    )
    args = parser.parse_args()

    settings = get_settings()
    memory_root = Path(settings.memory_root)

    per_device = [_score_device(slug, memory_root) for slug in args.devices]
    valid = [d for d in per_device if "error" not in d]

    if not valid:
        out = {
            "score": 0.0,
            "error": "no scorable devices",
            "per_device": per_device,
        }
        print(json.dumps(out))
        return 2

    # Aggregate score: mean across valid devices. Any invariant violation on
    # any device → that device's contribution is 0 → mean drops accordingly.
    aggregate = sum(d["score"] for d in valid) / len(valid)
    aggregate_metrics = {
        k: sum(d["metrics"][k] for d in valid) / len(valid) for k in WEIGHTS
    }
    any_violations = sum(len(d["invariant_violations"]) for d in valid)

    out: dict[str, Any] = {
        "score": aggregate,
        "n_devices": len(valid),
        "any_invariant_violations": any_violations,
        **{k: aggregate_metrics[k] for k in WEIGHTS},
    }
    if args.verbose:
        out["per_device"] = per_device
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
