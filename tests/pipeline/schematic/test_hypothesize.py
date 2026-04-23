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
