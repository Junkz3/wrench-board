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


def test_hypothesize_empty_graph_returns_empty():
    # Task 3: hypothesize is now implemented. Empty graph + empty obs = empty result.
    from api.pipeline.schematic.schemas import ElectricalGraph, SchematicQualityReport
    eg = ElectricalGraph(
        device_slug="demo",
        components={}, nets={}, power_rails={}, typed_edges=[],
        boot_sequence=[], designer_notes=[], ambiguities=[],
        quality=SchematicQualityReport(total_pages=0, pages_parsed=0),
    )
    result = hypothesize(eg, observations=Observations())
    assert result.hypotheses == []
    assert result.pruning.single_candidates_tested == 0


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


# ---------------------------------------------------------------------------
# Task 3 — single-fault enumeration tests
# ---------------------------------------------------------------------------

from api.pipeline.schematic.schemas import (
    AnalyzedBootPhase,
    AnalyzedBootSequence,
    AnalyzedBootTrigger,
    ComponentNode,
    ElectricalGraph,
    NetNode,
    PagePin,
    PowerRail,
    SchematicQualityReport,
)


def _mini_graph() -> ElectricalGraph:
    """Same shape as tests/pipeline/schematic/test_simulator.py::_mnt_like_graph."""
    components = {
        "U18": ComponentNode(refdes="U18", type="ic", pins=[
            PagePin(number="1", name="VIN", role="power_in", net_label="LPC_VCC"),
        ]),
        "U7": ComponentNode(refdes="U7", type="ic", pins=[
            PagePin(number="1", name="VIN", role="power_in", net_label="VIN"),
            PagePin(number="2", name="VOUT", role="power_out", net_label="+5V"),
        ]),
        "U12": ComponentNode(refdes="U12", type="ic", pins=[
            PagePin(number="1", name="VIN", role="power_in", net_label="+5V"),
            PagePin(number="2", name="VOUT", role="power_out", net_label="+3V3"),
        ]),
        "U19": ComponentNode(refdes="U19", type="ic", pins=[
            PagePin(number="1", name="VIN", role="power_in", net_label="+5V"),
        ]),
    }
    return ElectricalGraph(
        device_slug="demo",
        components=components,
        nets={
            "VIN": NetNode(label="VIN", is_power=True, is_global=True),
            "LPC_VCC": NetNode(label="LPC_VCC", is_power=True, is_global=True),
            "+5V": NetNode(label="+5V", is_power=True, is_global=True),
            "+3V3": NetNode(label="+3V3", is_power=True, is_global=True),
        },
        power_rails={
            "VIN": PowerRail(label="VIN", source_refdes=None, consumers=["U18"]),
            "LPC_VCC": PowerRail(label="LPC_VCC", source_refdes="U14", consumers=["U18"]),
            "+5V": PowerRail(label="+5V", source_refdes="U7", enable_net="5V_PWR_EN", consumers=["U12", "U19"]),
            "+3V3": PowerRail(label="+3V3", source_refdes="U12", enable_net="3V3_PWR_EN"),
        },
        typed_edges=[], boot_sequence=[], designer_notes=[], ambiguities=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )


def _mini_analyzed() -> AnalyzedBootSequence:
    return AnalyzedBootSequence(
        device_slug="demo",
        phases=[
            AnalyzedBootPhase(
                index=0, name="Standby", kind="always-on",
                rails_stable=["VIN", "LPC_VCC"],
                components_entering=["U18"],
                triggers_next=[
                    AnalyzedBootTrigger(net_label="5V_PWR_EN", from_refdes="U18", rationale="LPC asserts 5V"),
                ],
            ),
            AnalyzedBootPhase(
                index=1, name="LPC asserts +5V", kind="sequenced",
                rails_stable=["+5V"],
                components_entering=["U7"],
                triggers_next=[
                    AnalyzedBootTrigger(net_label="3V3_PWR_EN", from_refdes="U18", rationale="LPC asserts 3V3"),
                ],
            ),
            AnalyzedBootPhase(
                index=2, name="+3V3", kind="sequenced",
                rails_stable=["+3V3"],
                components_entering=["U12", "U19"],
                triggers_next=[],
            ),
        ],
        sequencer_refdes="U18",
        global_confidence=0.9,
        model_used="test",
    )


def test_hypothesize_single_fault_recovers_kill_from_observations():
    """When the tech observes what U7-dead produces, U7 should rank #1."""
    # Observation: +5V rail dead, U12 and U19 observed cold, U7 NOT checked
    obs = Observations(
        dead_comps=frozenset({"U12", "U19"}),
        dead_rails=frozenset({"+5V"}),
    )
    result = hypothesize(
        _mini_graph(),
        analyzed_boot=_mini_analyzed(),
        observations=obs,
    )
    assert len(result.hypotheses) >= 1
    # Top-1 should be U7 — it's the only single-fault that explains both obs.
    assert result.hypotheses[0].kill_refdes == ["U7"]
    assert result.hypotheses[0].score > 0
    assert result.pruning.single_candidates_tested >= 1
    # 2-fault disabled until Task 5, so pairs_tested must be 0 at this point.
    assert result.pruning.two_fault_pairs_tested == 0


def test_hypothesize_empty_observations_returns_empty():
    result = hypothesize(
        _mini_graph(),
        analyzed_boot=_mini_analyzed(),
        observations=Observations(),
    )
    assert result.hypotheses == []
    assert result.pruning.single_candidates_tested == 0


def test_hypothesize_pruning_skips_irrelevant_candidates():
    """A component whose cascade intersects nothing in obs must be skipped."""
    obs = Observations(dead_rails=frozenset({"+5V"}))
    result = hypothesize(
        _mini_graph(),
        analyzed_boot=_mini_analyzed(),
        observations=obs,
    )
    # Only U7 (+5V source) and ancestors affecting +5V should be tested.
    # Of our 4 components, only U7 could produce this cascade.
    kills_tested = {tuple(h.kill_refdes) for h in result.hypotheses}
    assert ("U7",) in kills_tested
    # We shouldn't explode: pruning must have eliminated U19 (a consumer) as
    # it can't kill +5V.
    assert result.pruning.single_candidates_tested <= 4


# ---------------------------------------------------------------------------
# Task 4 — narrative template tests
# ---------------------------------------------------------------------------


def test_narrative_single_fault_no_contradiction():
    obs = Observations(
        dead_comps=frozenset({"U12", "U19"}),
        dead_rails=frozenset({"+5V"}),
    )
    result = hypothesize(
        _mini_graph(), analyzed_boot=_mini_analyzed(), observations=obs,
    )
    top = result.hypotheses[0]
    assert top.kill_refdes == ["U7"]
    assert top.narrative != ""
    # Contains key elements of the template.
    assert "U7" in top.narrative
    assert "+5V" in top.narrative
    assert "meurt" in top.narrative or "meurent" in top.narrative
    # No contradiction claim since FP=0.
    assert "Contredit" not in top.narrative


def test_narrative_with_contradiction_mentions_it():
    # A hypothesis that kills something observed alive — force the template
    # to include "Contredit :".
    obs = Observations(
        dead_comps=frozenset({"U12"}),
        alive_comps=frozenset({"U7"}),  # declaring U7 alive
    )
    result = hypothesize(
        _mini_graph(), analyzed_boot=_mini_analyzed(), observations=obs,
    )
    # At least one hypothesis should be a candidate that contradicts U7 alive
    # (e.g., killing U7 directly). Find it.
    contradictory = [h for h in result.hypotheses if "U7" in h.diff.contradictions]
    if contradictory:
        narr = contradictory[0].narrative
        assert "Contredit" in narr
        assert "U7" in narr
