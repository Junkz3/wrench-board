# tests/pipeline/schematic/test_hypothesize_accuracy.py
# SPDX-License-Identifier: Apache-2.0
"""CI-gated accuracy + perf benchmarks for the hypothesize engine.

Uses the generated fixture corpus. Thresholds are starting points — if the
real data shows they're unreachable, lower them and note in the plan's
Open Questions section, not silently.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

import pytest

from api.pipeline.schematic.hypothesize import Observations, hypothesize
from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph

FIXTURE = Path(__file__).parent / "fixtures" / "hypothesize_scenarios.json"
MEMORY_ROOT = Path(__file__).resolve().parents[3] / "memory"

# CI thresholds — conservative starting values.
TOP1_ACCURACY_MIN = 0.50   # ≥ 50% top-1 accuracy
TOP3_ACCURACY_MIN = 0.75   # ≥ 75% top-3 accuracy
MRR_MIN = 0.65
P95_LATENCY_MS = 500.0


def _load_pack(slug: str) -> tuple[ElectricalGraph, AnalyzedBootSequence | None]:
    pack = MEMORY_ROOT / slug
    eg = ElectricalGraph.model_validate_json(
        (pack / "electrical_graph.json").read_text()
    )
    ab_path = pack / "boot_sequence_analyzed.json"
    ab = (
        AnalyzedBootSequence.model_validate_json(ab_path.read_text())
        if ab_path.exists() else None
    )
    return eg, ab


def _run_scenarios() -> list[dict]:
    if not FIXTURE.exists():
        pytest.skip("fixture not generated — run scripts/gen_hypothesize_benchmarks.py")
    scenarios = json.loads(FIXTURE.read_text())
    if not scenarios:
        pytest.skip("empty fixture")

    # Group by slug so we load each pack once.
    by_slug: dict[str, list[dict]] = {}
    for sc in scenarios:
        by_slug.setdefault(sc["slug"], []).append(sc)

    records: list[dict] = []
    for slug, group in by_slug.items():
        pack_path = MEMORY_ROOT / slug / "electrical_graph.json"
        if not pack_path.exists():
            continue  # skip scenarios for devices not on this checkout
        eg, ab = _load_pack(slug)
        for sc in group:
            obs = Observations(
                dead_comps=frozenset(sc["observations"]["dead_comps"]),
                alive_comps=frozenset(sc["observations"]["alive_comps"]),
                dead_rails=frozenset(sc["observations"]["dead_rails"]),
                alive_rails=frozenset(sc["observations"]["alive_rails"]),
            )
            t0 = time.perf_counter()
            result = hypothesize(eg, analyzed_boot=ab, observations=obs)
            wall_ms = (time.perf_counter() - t0) * 1000
            gt = tuple(sorted(sc["ground_truth_kill"]))
            # rank of ground truth (None if not in top-N).
            rank = None
            for i, h in enumerate(result.hypotheses, start=1):
                if tuple(sorted(h.kill_refdes)) == gt:
                    rank = i
                    break
            records.append({
                "id": sc["id"],
                "rank": rank,
                "wall_ms": wall_ms,
                "hypotheses_returned": len(result.hypotheses),
            })
    if not records:
        pytest.skip("no scenarios matched packs on this checkout")
    return records


def test_top1_accuracy_meets_threshold():
    records = _run_scenarios()
    top1 = sum(1 for r in records if r["rank"] == 1) / len(records)
    assert top1 >= TOP1_ACCURACY_MIN, (
        f"top-1 accuracy {top1:.2%} < threshold {TOP1_ACCURACY_MIN:.0%} "
        f"across {len(records)} scenarios"
    )


def test_top3_accuracy_meets_threshold():
    records = _run_scenarios()
    top3 = sum(1 for r in records if r["rank"] is not None and r["rank"] <= 3) / len(records)
    assert top3 >= TOP3_ACCURACY_MIN, (
        f"top-3 accuracy {top3:.2%} < threshold {TOP3_ACCURACY_MIN:.0%}"
    )


def test_mean_reciprocal_rank_meets_threshold():
    records = _run_scenarios()
    recs = [1.0 / r["rank"] if r["rank"] else 0.0 for r in records]
    mrr = statistics.fmean(recs)
    assert mrr >= MRR_MIN, (
        f"MRR {mrr:.3f} < threshold {MRR_MIN:.3f}"
    )


def test_p95_latency_under_budget():
    records = _run_scenarios()
    wall = sorted(r["wall_ms"] for r in records)
    p95 = wall[max(0, int(len(wall) * 0.95) - 1)]
    assert p95 < P95_LATENCY_MS, (
        f"p95 latency {p95:.1f} ms exceeds budget {P95_LATENCY_MS} ms"
    )
