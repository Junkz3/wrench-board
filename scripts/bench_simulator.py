#!/usr/bin/env python3
"""Benchmark the SimulationEngine against an on-disk electrical graph.

Usage:
    .venv/bin/python scripts/bench_simulator.py
    .venv/bin/python scripts/bench_simulator.py --slug demo-pi --iterations 500 --kill U12

Emits a JSON summary with mean/p50/p95/p99 timings in ms.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph
from api.pipeline.schematic.simulator import SimulationEngine


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", default="mnt-reform-motherboard")
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--kill", default="")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    pack = root / "memory" / args.slug
    eg = ElectricalGraph.model_validate_json((pack / "electrical_graph.json").read_text())
    ab_path = pack / "boot_sequence_analyzed.json"
    ab = AnalyzedBootSequence.model_validate_json(ab_path.read_text()) if ab_path.exists() else None
    killed = [r for r in args.kill.split(",") if r]

    # 5 warm-up runs.
    for _ in range(5):
        SimulationEngine(eg, analyzed_boot=ab, killed_refdes=killed).run()

    samples: list[float] = []
    for _ in range(args.iterations):
        t0 = time.perf_counter_ns()
        SimulationEngine(eg, analyzed_boot=ab, killed_refdes=killed).run()
        samples.append((time.perf_counter_ns() - t0) / 1e6)

    samples.sort()

    def pct(p: float) -> float:
        return samples[max(0, int(len(samples) * p) - 1)]

    out = {
        "slug": args.slug,
        "iterations": args.iterations,
        "killed": killed,
        "components": len(eg.components),
        "rails": len(eg.power_rails),
        "phases": len(ab.phases) if ab else len(eg.boot_sequence),
        "ms": {
            "mean": round(statistics.fmean(samples), 3),
            "p50": round(pct(0.50), 3),
            "p95": round(pct(0.95), 3),
            "p99": round(pct(0.99), 3),
        },
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
