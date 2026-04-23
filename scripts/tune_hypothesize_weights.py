#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Sweep (fp_weight, fn_weight) pairs and report the pair that maximises
top-3 accuracy on the fixture corpus. Run once manually; commit the
resulting PENALTY_WEIGHTS change.
"""

from __future__ import annotations

import json
from pathlib import Path

import api.pipeline.schematic.hypothesize as hypothesize_mod
from api.pipeline.schematic.hypothesize import Observations

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests/pipeline/schematic/fixtures/hypothesize_scenarios.json"
)
MEMORY_ROOT = Path(__file__).resolve().parents[1] / "memory"


def evaluate(fp_w: int, fn_w: int) -> float:
    """Evaluate top-3 accuracy with given weights on fixture corpus."""
    from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph

    # Temporarily override weights
    hypothesize_mod.PENALTY_WEIGHTS = (fp_w, fn_w)

    scenarios = json.loads(FIXTURE.read_text())
    by_slug: dict[str, list[dict]] = {}
    for sc in scenarios:
        by_slug.setdefault(sc["slug"], []).append(sc)

    hit = 0
    total = 0
    for slug, group in by_slug.items():
        pack = MEMORY_ROOT / slug
        if not (pack / "electrical_graph.json").exists():
            continue
        eg = ElectricalGraph.model_validate_json((pack / "electrical_graph.json").read_text())
        ab_path = pack / "boot_sequence_analyzed.json"
        ab = (
            AnalyzedBootSequence.model_validate_json(ab_path.read_text())
            if ab_path.exists()
            else None
        )
        for sc in group:
            obs = Observations(
                dead_comps=frozenset(sc["observations"]["dead_comps"]),
                alive_comps=frozenset(sc["observations"]["alive_comps"]),
                dead_rails=frozenset(sc["observations"]["dead_rails"]),
                alive_rails=frozenset(sc["observations"]["alive_rails"]),
            )
            result = hypothesize_mod.hypothesize(eg, analyzed_boot=ab, observations=obs)
            gt = tuple(sorted(sc["ground_truth_kill"]))
            top3 = [tuple(sorted(h.kill_refdes)) for h in result.hypotheses[:3]]
            if gt in top3:
                hit += 1
            total += 1
    return hit / total if total else 0.0


def main() -> None:
    print("Tuning (fp_weight, fn_weight) pairs on fixture corpus...\n")
    candidates: list[tuple[int, int, float]] = []
    for fp_w in (5, 10, 15, 20, 30):
        for fn_w in (1, 2, 3, 5):
            acc = evaluate(fp_w, fn_w)
            print(f"(fp={fp_w:>2}, fn={fn_w}) → top-3 accuracy {acc:.3%}")
            candidates.append((fp_w, fn_w, acc))

    candidates.sort(key=lambda t: -t[2])
    best = candidates[0]
    print(f"\nBEST: (fp={best[0]}, fn={best[1]}) top-3={best[2]:.3%}")


if __name__ == "__main__":
    main()
