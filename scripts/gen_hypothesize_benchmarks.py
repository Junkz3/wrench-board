#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Auto-generate ground-truth scenarios for the reverse-diagnostic benchmark.

For each rail source + top-20-blast-radius component in the target device,
simulate its death via the forward simulator, then sample 3 partial
observations from the resulting cascade. Yields ~135 scenarios per device.

Usage:
    .venv/bin/python scripts/gen_hypothesize_benchmarks.py \\
        --slug mnt-reform-motherboard \\
        --out tests/pipeline/schematic/fixtures/hypothesize_scenarios.json
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph
from api.pipeline.schematic.simulator import SimulationEngine


def pick_sample(
    pool: set, k_min: int, k_max: int, rng: random.Random,
) -> list[str]:
    """Sample k items from pool, clamped between k_min and k_max."""
    if not pool:
        return []
    k = rng.randint(min(k_min, len(pool)), min(k_max, len(pool)))
    return sorted(rng.sample(sorted(pool), k))


def generate(slug: str, memory_root: Path, seed: int = 42) -> list[dict]:
    """Generate scenario fixture for a device.

    Iterates every rail source + top-20 cascade-size components, simulates
    each as a ground-truth kill, then samples 3 partial-observation variants
    (rails-only, comps-only, mixed) per kill.
    """
    pack = memory_root / slug
    eg = ElectricalGraph.model_validate_json(
        (pack / "electrical_graph.json").read_text()
    )
    ab_path = pack / "boot_sequence_analyzed.json"
    ab = (
        AnalyzedBootSequence.model_validate_json(ab_path.read_text())
        if ab_path.exists() else None
    )

    # Candidate kill set: every rail source + top-20 components by
    # cascade-size (computed by killing each once).
    rail_sources = {
        r.source_refdes for r in eg.power_rails.values() if r.source_refdes
    }
    cascade_size: dict[str, int] = {}
    for refdes in eg.components:
        tl = SimulationEngine(eg, analyzed_boot=ab, killed_refdes=[refdes]).run()
        cascade_size[refdes] = (
            len(tl.cascade_dead_components) + len(tl.cascade_dead_rails)
        )
    top_blast = {
        refdes for refdes, _ in sorted(
            cascade_size.items(), key=lambda kv: -kv[1]
        )[:20]
    }
    candidates = sorted(rail_sources | top_blast)

    rng = random.Random(seed)
    all_rails = set(eg.power_rails.keys())
    all_comps = set(eg.components.keys())
    scenarios: list[dict] = []
    for refdes in candidates:
        tl = SimulationEngine(eg, analyzed_boot=ab, killed_refdes=[refdes]).run()
        dead_comps_full = set(tl.cascade_dead_components) | {refdes}
        dead_rails_full = set(tl.cascade_dead_rails)
        alive_comps_full = all_comps - dead_comps_full
        alive_rails_full = all_rails - dead_rails_full

        # Three variants per kill: rails-only, comps-only, mixed.
        for variant in ("rails", "comps", "mixed"):
            if variant == "rails":
                dc, ac = [], []
                dr = pick_sample(dead_rails_full, 1, 3, rng)
                ar = pick_sample(alive_rails_full, 1, 3, rng)
            elif variant == "comps":
                dc = pick_sample(dead_comps_full - {refdes}, 2, 5, rng) + [refdes]
                ac = pick_sample(alive_comps_full, 2, 4, rng)
                dr, ar = [], []
            else:
                dc = pick_sample(dead_comps_full - {refdes}, 1, 3, rng) + [refdes]
                ac = pick_sample(alive_comps_full, 1, 3, rng)
                dr = pick_sample(dead_rails_full, 1, 2, rng)
                ar = pick_sample(alive_rails_full, 1, 2, rng)
            scenarios.append({
                "id": f"{slug}-kill-{refdes}-{variant}",
                "slug": slug,
                "ground_truth_kill": [refdes],
                "sample_strategy": variant,
                "observations": {
                    "dead_comps": sorted(set(dc)),
                    "alive_comps": sorted(set(ac)),
                    "dead_rails": sorted(set(dr)),
                    "alive_rails": sorted(set(ar)),
                },
            })
    return scenarios


def main() -> None:
    """Parse args and generate fixture."""
    p = argparse.ArgumentParser()
    p.add_argument("--slug", required=True)
    p.add_argument(
        "--out",
        default="tests/pipeline/schematic/fixtures/hypothesize_scenarios.json",
    )
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    root = Path(__file__).resolve().parents[1]
    scenarios = generate(args.slug, root / "memory", seed=args.seed)
    out = root / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(scenarios, indent=2))
    print(f"wrote {len(scenarios)} scenarios to {out}")


if __name__ == "__main__":
    main()
