"""Field-calibrated accuracy gates — runs against real + legacy scenarios.

Distinct from test_hypothesize_accuracy.py (synthetic, self-referential
corpus). Reads the fixture produced by `make build-field-corpus` and
gates the solver against ground truth from actual validated repairs.

Scenarios with empty observations (legacy field_reports that predate
structured `state_*` dicts) are filtered at load time — they carry
ground truth but nothing to feed the solver, so they'd artificially
drag top-1 toward zero.

Starting thresholds are intentionally permissive (top-1 ≥ 30%, top-3
≥ 50%, MRR ≥ 0.40). Tighten manually as the corpus grows via explicit
commits — never auto-calibrate, that hides drift.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

import pytest

from api.pipeline.schematic.hypothesize import Observations, hypothesize
from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph

pytestmark = pytest.mark.slow

FIXTURE = Path(__file__).parent / "fixtures" / "hypothesize_field_scenarios.json"
MEMORY_ROOT = Path(__file__).resolve().parents[3] / "memory"

# Conservative starting thresholds per mode. Bump these in a dedicated
# commit when the corpus accuracy proves durable across more repairs.
FIELD_THRESHOLDS: dict[str, dict[str, float]] = {
    "dead":      {"top1": 0.30, "top3": 0.50, "mrr": 0.40},
    "anomalous": {"top1": 0.25, "top3": 0.45, "mrr": 0.35},
    "hot":       {"top1": 0.30, "top3": 0.50, "mrr": 0.40},
    "shorted":   {"top1": 0.20, "top3": 0.40, "mrr": 0.30},
}
MIN_SCENARIOS_PER_MODE = 3
P95_LATENCY_MS = 500.0


def _has_observations(obs_payload: dict) -> bool:
    return bool(obs_payload.get("state_comps") or obs_payload.get("state_rails"))


def _load_pack(slug: str) -> tuple[ElectricalGraph, AnalyzedBootSequence | None]:
    pack = MEMORY_ROOT / slug
    eg = ElectricalGraph.model_validate_json((pack / "electrical_graph.json").read_text())
    ab_path = pack / "boot_sequence_analyzed.json"
    ab = AnalyzedBootSequence.model_validate_json(ab_path.read_text()) if ab_path.exists() else None
    return eg, ab


def _run_field_scenarios() -> list[dict]:
    if not FIXTURE.exists():
        pytest.skip("field fixture not built — run `make build-field-corpus`")
    scenarios = json.loads(FIXTURE.read_text())
    scenarios = [sc for sc in scenarios if _has_observations(sc.get("observations", {}))]
    if not scenarios:
        pytest.skip("no scenario with structured observations — corpus too sparse")

    by_slug: dict[str, list[dict]] = {}
    for sc in scenarios:
        by_slug.setdefault(sc["slug"], []).append(sc)

    records: list[dict] = []
    for slug, group in by_slug.items():
        if not (MEMORY_ROOT / slug / "electrical_graph.json").exists():
            continue
        eg, ab = _load_pack(slug)
        for sc in group:
            obs = Observations(
                state_comps=sc["observations"].get("state_comps", {}),
                state_rails=sc["observations"].get("state_rails", {}),
                # metrics_* intentionally dropped — Phase 1 scoring is
                # discrete, numeric metrics aren't used yet (Phase 5).
            )
            t0 = time.perf_counter()
            result = hypothesize(eg, analyzed_boot=ab, observations=obs)
            wall_ms = (time.perf_counter() - t0) * 1000
            gt_refdes = tuple(sorted(sc["ground_truth_kill"]))
            gt_modes = tuple(sc["ground_truth_modes"])
            rank = None
            for i, h in enumerate(result.hypotheses, start=1):
                if (
                    tuple(sorted(h.kill_refdes)) == gt_refdes
                    and tuple(h.kill_modes) == gt_modes
                ):
                    rank = i
                    break
            records.append({
                "id": sc["id"],
                "source": sc.get("source", "unknown"),
                "mode": sc["ground_truth_modes"][0] if sc["ground_truth_modes"] else "unknown",
                "rank": rank,
                "wall_ms": wall_ms,
            })
    if not records:
        pytest.skip("no field scenarios matched local packs")
    return records


@pytest.mark.parametrize("mode", ["dead", "anomalous", "hot", "shorted"])
def test_field_top1_per_mode(mode: str):
    records = [r for r in _run_field_scenarios() if r["mode"] == mode]
    if len(records) < MIN_SCENARIOS_PER_MODE:
        pytest.skip(f"mode={mode}: only {len(records)} scenarios, need {MIN_SCENARIOS_PER_MODE}")
    top1 = sum(1 for r in records if r["rank"] == 1) / len(records)
    assert top1 >= FIELD_THRESHOLDS[mode]["top1"], (
        f"FIELD mode={mode} top-1 {top1:.2%} < threshold "
        f"{FIELD_THRESHOLDS[mode]['top1']:.0%} ({len(records)} scenarios)"
    )


@pytest.mark.parametrize("mode", ["dead", "anomalous", "hot", "shorted"])
def test_field_top3_per_mode(mode: str):
    records = [r for r in _run_field_scenarios() if r["mode"] == mode]
    if len(records) < MIN_SCENARIOS_PER_MODE:
        pytest.skip(f"mode={mode}: only {len(records)} scenarios, need {MIN_SCENARIOS_PER_MODE}")
    top3 = sum(1 for r in records if r["rank"] is not None and r["rank"] <= 3) / len(records)
    assert top3 >= FIELD_THRESHOLDS[mode]["top3"], (
        f"FIELD mode={mode} top-3 {top3:.2%} < threshold {FIELD_THRESHOLDS[mode]['top3']:.0%}"
    )


@pytest.mark.parametrize("mode", ["dead", "anomalous", "hot", "shorted"])
def test_field_mrr_per_mode(mode: str):
    records = [r for r in _run_field_scenarios() if r["mode"] == mode]
    if len(records) < MIN_SCENARIOS_PER_MODE:
        pytest.skip(f"mode={mode}: only {len(records)} scenarios, need {MIN_SCENARIOS_PER_MODE}")
    mrr = statistics.fmean([1.0 / r["rank"] if r["rank"] else 0.0 for r in records])
    assert mrr >= FIELD_THRESHOLDS[mode]["mrr"], (
        f"FIELD mode={mode} MRR {mrr:.3f} < threshold {FIELD_THRESHOLDS[mode]['mrr']:.3f}"
    )


def test_field_p95_latency_under_budget():
    records = _run_field_scenarios()
    wall = sorted(r["wall_ms"] for r in records)
    p95 = wall[max(0, int(len(wall) * 0.95) - 1)]
    assert p95 < P95_LATENCY_MS, f"p95 field latency {p95:.1f}ms exceeds budget"
