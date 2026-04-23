# SPDX-License-Identifier: Apache-2.0
"""Tests for the reverse-diagnostic hypothesis engine."""

from __future__ import annotations

import pytest  # noqa: F401 — used by later parametrised tests

from api.pipeline.schematic.hypothesize import (
    Hypothesis,
    HypothesisDiff,
    HypothesisMetrics,
    HypothesizeResult,
    Observations,
    PENALTY_WEIGHTS,
    PruningStats,
    TOP_K_SINGLE,
    _score_candidate,
    hypothesize,
)


def test_observations_shape_minimal():
    obs = Observations()
    assert obs.dead_comps == frozenset()
    assert obs.alive_comps == frozenset()
    assert obs.dead_rails == frozenset()
    assert obs.alive_rails == frozenset()


def test_observations_accepts_sets():
    obs = Observations(
        dead_comps=frozenset({"U1", "U9"}),
        alive_comps=frozenset({"U7"}),
        dead_rails=frozenset({"+3V3"}),
        alive_rails=frozenset({"+5V"}),
    )
    assert "U1" in obs.dead_comps
    assert "U7" in obs.alive_comps


def test_hypothesis_shape_minimal():
    h = Hypothesis(
        kill_refdes=["U7"],
        score=3.0,
        metrics=HypothesisMetrics(
            tp_comps=2, tp_rails=1, fp_comps=0, fp_rails=0, fn_comps=0, fn_rails=0,
        ),
        diff=HypothesisDiff(contradictions=[], under_explained=[], over_predicted=[]),
        narrative="",
        cascade_preview={"dead_rails": ["+5V"], "dead_comps_count": 4},
    )
    assert h.kill_refdes == ["U7"]
    assert h.score == 3.0
    assert h.metrics.tp_comps == 2


def test_hypothesize_result_shape_minimal():
    r = HypothesizeResult(
        device_slug="demo",
        observations_echo=Observations(),
        hypotheses=[],
        pruning=PruningStats(
            single_candidates_tested=0, two_fault_pairs_tested=0, wall_ms=0.0,
        ),
    )
    assert r.device_slug == "demo"
    assert r.hypotheses == []


def test_module_constants_present():
    # Constants tuned by the benchmark — test ensures they exist and are the
    # documented defaults. bench scripts import them at module load.
    assert PENALTY_WEIGHTS == (10, 2)
    assert TOP_K_SINGLE == 20


def test_hypothesize_stub_raises_not_implemented(tmp_path):
    # Until Task 3, the public `hypothesize` function raises — shape tests
    # alone must still pass independently.
    from api.pipeline.schematic.schemas import ElectricalGraph, SchematicQualityReport
    eg = ElectricalGraph(
        device_slug="demo",
        components={}, nets={}, power_rails={}, typed_edges=[],
        boot_sequence=[], designer_notes=[], ambiguities=[],
        quality=SchematicQualityReport(total_pages=0, pages_parsed=0),
    )
    with pytest.raises(NotImplementedError):
        hypothesize(eg, observations=Observations())


def test_score_perfect_match():
    """Hypothesis kills exactly what was observed dead — score = tp, 0 penalty."""
    obs = Observations(
        dead_comps=frozenset({"U1", "U9"}),
        alive_comps=frozenset({"U7"}),
        dead_rails=frozenset({"+3V3"}),
        alive_rails=frozenset({"+5V"}),
    )
    predicted = {
        "dead_comps": frozenset({"U1", "U9"}),
        "dead_rails": frozenset({"+3V3"}),
    }
    score, metrics, diff = _score_candidate(predicted, obs)
    # tp_comps=2 (U1, U9), tp_rails=1 (+3V3), tp_alive_comps=1 (U7), tp_alive_rails=1 (+5V)
    assert metrics.tp_comps == 3   # 2 dead matches + 1 alive-correct match
    assert metrics.tp_rails == 2   # 1 dead match + 1 alive-correct match
    assert metrics.fp_comps == 0
    assert metrics.fp_rails == 0
    assert metrics.fn_comps == 0
    assert metrics.fn_rails == 0
    # score = tp(5) - 10*fp(0) - 2*fn(0) = 5
    assert score == 5.0
    assert diff.contradictions == []
    assert diff.under_explained == []


def test_score_contradiction_costs_10x():
    """Hypothesis kills a component the tech observes alive — heavy penalty."""
    obs = Observations(
        dead_comps=frozenset({"U1"}),
        alive_comps=frozenset({"U7"}),
    )
    predicted = {
        "dead_comps": frozenset({"U1", "U7"}),  # U7 contradicts observation
        "dead_rails": frozenset(),
    }
    score, metrics, diff = _score_candidate(predicted, obs)
    assert metrics.tp_comps == 1    # U1 dead match
    assert metrics.fp_comps == 1    # U7 was observed alive
    # score = tp(1) - 10*fp(1) - 2*fn(0) = -9
    assert score == -9.0
    assert diff.contradictions == ["U7"]


def test_score_under_explanation_costs_2x():
    """Hypothesis leaves an observed-dead component alive — mild penalty."""
    obs = Observations(
        dead_comps=frozenset({"U1", "U9"}),
    )
    predicted = {
        "dead_comps": frozenset({"U1"}),  # misses U9
        "dead_rails": frozenset(),
    }
    score, metrics, diff = _score_candidate(predicted, obs)
    assert metrics.tp_comps == 1
    assert metrics.fn_comps == 1
    # score = tp(1) - 10*fp(0) - 2*fn(1) = -1
    assert score == -1.0
    assert diff.under_explained == ["U9"]


def test_score_over_predicted_not_penalised():
    """Hypothesis kills things not in any observation set — zero penalty.

    Over-prediction is only visible as informational diff, not a score cost.
    The tech may simply not have checked those components.
    """
    obs = Observations(dead_comps=frozenset({"U1"}))
    predicted = {
        "dead_comps": frozenset({"U1", "U99"}),  # U99 not in any obs set
        "dead_rails": frozenset({"+99V"}),
    }
    score, metrics, diff = _score_candidate(predicted, obs)
    assert metrics.fp_comps == 0  # U99 not in alive_comps
    assert score == 1.0           # tp=1, no penalty
    # But it DOES appear in the over_predicted diff.
    assert "U99" in diff.over_predicted
    assert "+99V" in diff.over_predicted


def test_score_empty_observations_gives_zero():
    obs = Observations()
    predicted = {"dead_comps": frozenset({"U1"}), "dead_rails": frozenset()}
    score, metrics, diff = _score_candidate(predicted, obs)
    assert score == 0.0
    assert metrics.tp_comps == 0
