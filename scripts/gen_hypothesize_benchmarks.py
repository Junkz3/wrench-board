#!/usr/bin/env python3
"""Generate ground-truth scenarios for the reverse-diagnostic benchmark,
covering all applicable failure modes per refdes.

For each refdes in the device, enumerate its applicable modes via
_applicable_modes. For each mode, run _simulate_failure to produce the
cascade, then sample 2-3 observation variants from the cascade (each
variant picks a subset of the affected targets to present as
observations, with ground_truth = {refdes, mode}).
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from api.pipeline.schematic.hypothesize import (
    _applicable_modes,
    _simulate_failure,
)
from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph


def sample_subset(pool: set, k_min: int, k_max: int, rng: random.Random) -> list[str]:
    if not pool:
        return []
    k = rng.randint(min(k_min, len(pool)), min(k_max, len(pool)))
    return sorted(rng.sample(sorted(pool), k))


def generate(slug: str, memory_root: Path, seed: int = 42) -> list[dict]:
    pack = memory_root / slug
    eg = ElectricalGraph.model_validate_json(
        (pack / "electrical_graph.json").read_text()
    )
    ab_path = pack / "boot_sequence_analyzed.json"
    ab = (
        AnalyzedBootSequence.model_validate_json(ab_path.read_text())
        if ab_path.exists() else None
    )

    rng = random.Random(seed)
    scenarios: list[dict] = []

    # Cap per-mode scenario count so the corpus stays well balanced.
    MAX_PER_MODE = 30

    scenario_count_by_mode: dict[str, int] = {}
    for refdes in sorted(eg.components):
        for mode in _applicable_modes(eg, refdes):
            count = scenario_count_by_mode.get(mode, 0)
            if count >= MAX_PER_MODE:
                continue
            cascade = _simulate_failure(eg, ab, refdes, mode)
            # Build the target pools for sampling.
            affected_comps: set[str] = (
                set(cascade["dead_comps"])
                | set(cascade["anomalous_comps"])
                | set(cascade["hot_comps"])
            )
            affected_rails: set[str] = (
                set(cascade["dead_rails"]) | set(cascade["shorted_rails"])
            )
            # Skip degenerate cascades (nothing to observe).
            if not affected_comps and not affected_rails:
                continue

            # 2 variants per (refdes, mode).
            for variant in ("partial_comps", "partial_rails_plus_one_alive"):
                if variant == "partial_comps":
                    obs_comps = sample_subset(affected_comps, 1, 3, rng)
                    obs_rails = sample_subset(affected_rails, 0, 1, rng)
                else:
                    obs_rails = sample_subset(affected_rails, 1, 2, rng)
                    obs_comps = sample_subset(affected_comps, 1, 2, rng)
                    # Plus one alive observation for corroboration.
                    alive_candidates = set(eg.components) - affected_comps
                    if alive_candidates:
                        alive_refdes = rng.choice(sorted(alive_candidates))
                        obs_comps.append(alive_refdes)

                state_comps: dict[str, str] = {}
                state_rails: dict[str, str] = {}
                for c in obs_comps:
                    # Phase 4 — only emit observations with modes that are
                    # valid for the target's kind. Passives can only be
                    # observed open/short/alive; ICs dead/alive/anomalous/hot.
                    # A tech never reports "FB12 dead"; they report the rail
                    # death and let the engine propose the open FB.
                    comp_kind = getattr(eg.components.get(c), "kind", "ic")
                    if comp_kind != "ic":
                        if c in cascade["dead_comps"]:
                            # Passive downstream of a real kill — from the
                            # tech's side this is silent. Skip rather than
                            # synthesise a semantically-wrong mode.
                            continue
                        state_comps[c] = "alive"
                        continue
                    if c in cascade["dead_comps"]:
                        state_comps[c] = "dead"
                    elif c in cascade["anomalous_comps"]:
                        state_comps[c] = "anomalous"
                    elif c in cascade["hot_comps"]:
                        state_comps[c] = "hot"
                    else:
                        state_comps[c] = "alive"
                for r in obs_rails:
                    if r in cascade["shorted_rails"]:
                        state_rails[r] = "shorted"
                    elif r in cascade["dead_rails"]:
                        state_rails[r] = "dead"
                    else:
                        state_rails[r] = "alive"

                scenarios.append({
                    "id": f"{slug}-{refdes}-{mode}-{variant}",
                    "slug": slug,
                    "ground_truth_kill": [refdes],
                    "ground_truth_modes": [mode],
                    "sample_strategy": variant,
                    "observations": {
                        "state_comps": state_comps,
                        "state_rails": state_rails,
                    },
                })
                scenario_count_by_mode[mode] = scenario_count_by_mode.get(mode, 0) + 1
    return scenarios


def main() -> None:
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

    by_mode: dict[str, int] = {}
    for sc in scenarios:
        by_mode[sc["ground_truth_modes"][0]] = by_mode.get(sc["ground_truth_modes"][0], 0) + 1
    print(f"wrote {len(scenarios)} scenarios to {out}")
    for mode, n in sorted(by_mode.items()):
        print(f"  {mode:10s}  {n}")


if __name__ == "__main__":
    main()
