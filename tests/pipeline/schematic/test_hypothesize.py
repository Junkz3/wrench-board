# SPDX-License-Identifier: Apache-2.0
"""Tests for the reverse-diagnostic hypothesis engine (schema B)."""

from __future__ import annotations

import pytest

from api.pipeline.schematic.hypothesize import (
    MAX_PAIRS,
    MAX_RESULTS_DEFAULT,
    PENALTY_WEIGHTS,
    TOP_K_SINGLE,
    Hypothesis,
    HypothesisDiff,
    HypothesisMetrics,
    Observations,
    ObservedMetric,
    _empty_cascade,
    _propagate_signal_downstream,
    _score_candidate,
    _simulate_failure,
    hypothesize,
)
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
    TypedEdge,
)


def test_observations_shape_minimal():
    obs = Observations()
    assert obs.state_comps == {}
    assert obs.state_rails == {}
    assert obs.metrics_comps == {}
    assert obs.metrics_rails == {}
    assert obs.is_empty() is True


def test_observations_accepts_dicts():
    obs = Observations(
        state_comps={"U1": "dead", "U7": "anomalous", "Q17": "hot"},
        state_rails={"+3V3": "dead", "+5V": "shorted"},
        metrics_rails={"+3V3": ObservedMetric(measured=0.02, unit="V", nominal=3.3)},
    )
    assert obs.state_comps["U7"] == "anomalous"
    assert obs.state_rails["+5V"] == "shorted"
    assert obs.metrics_rails["+3V3"].measured == 0.02
    assert obs.is_empty() is False


def test_observations_cross_bucket_alias_rejected():
    with pytest.raises(ValueError, match="both component and rail"):
        Observations(state_comps={"X": "dead"}, state_rails={"X": "dead"})


def test_module_constants_present():
    assert PENALTY_WEIGHTS == (10, 2)
    assert TOP_K_SINGLE == 20
    assert MAX_PAIRS == 100
    assert MAX_RESULTS_DEFAULT == 5


def test_hypothesis_shape_minimal():
    h = Hypothesis(
        kill_refdes=["U7"],
        kill_modes=["dead"],
        score=3.0,
        metrics=HypothesisMetrics(
            tp_comps=2, tp_rails=1, fp_comps=0, fp_rails=0, fn_comps=0, fn_rails=0,
        ),
        diff=HypothesisDiff(),
        narrative="",
        cascade_preview={
            "dead_rails": ["+5V"],
            "shorted_rails": [],
            "dead_comps_count": 4,
            "anomalous_count": 0,
            "hot_count": 0,
        },
    )
    assert h.kill_modes == ["dead"]


def _mini_graph() -> ElectricalGraph:
    return ElectricalGraph(
        device_slug="demo",
        components={
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
        },
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
            "+3V3": PowerRail(label="+3V3", source_refdes="U12", enable_net="3V3_PWR_EN", consumers=[]),
        },
        typed_edges=[], boot_sequence=[], designer_notes=[], ambiguities=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )


def _mini_boot() -> AnalyzedBootSequence:
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
                index=1, name="+5V", kind="sequenced",
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
        sequencer_refdes="U18", global_confidence=0.9, model_used="test",
    )


def test_empty_cascade_has_all_buckets():
    c = _empty_cascade()
    for key in ("dead_comps", "dead_rails", "shorted_rails", "anomalous_comps", "hot_comps"):
        assert c[key] == frozenset()


def test_simulate_failure_dead_mirrors_legacy():
    c = _simulate_failure(_mini_graph(), _mini_boot(), "U7", "dead")
    # Killing U7 cascades +5V → dead downstream (+3V3 via U12, U19 directly).
    assert "U7" in c["dead_comps"]
    assert "+5V" in c["dead_rails"]
    assert c["shorted_rails"] == frozenset()
    assert c["anomalous_comps"] == frozenset()


def test_simulate_failure_unknown_mode_raises():
    with pytest.raises(ValueError, match="unknown failure mode"):
        _simulate_failure(_mini_graph(), _mini_boot(), "U7", "bogus")


def _mini_graph_with_signal_edges() -> ElectricalGraph:
    """MNT-like mini graph with signal edges: U10 → U11 → U17 chain."""
    g = _mini_graph()
    # Add 3 components in a signal chain on the DSI path.
    g.components["U10"] = ComponentNode(refdes="U10", type="ic", pins=[
        PagePin(number="1", name="DSI_IN", role="signal_in", net_label="DSI_D0"),
        PagePin(number="2", name="EDP_OUT", role="signal_out", net_label="EDP_D0"),
    ])
    g.components["U11"] = ComponentNode(refdes="U11", type="ic", pins=[
        PagePin(number="1", name="EDP_IN", role="signal_in", net_label="EDP_D0"),
        PagePin(number="2", name="PANEL_OUT", role="signal_out", net_label="PANEL_D0"),
    ])
    g.components["U17"] = ComponentNode(refdes="U17", type="ic", pins=[
        PagePin(number="1", name="PANEL_IN", role="signal_in", net_label="PANEL_D0"),
    ])
    g.typed_edges = [
        TypedEdge(src="U10", dst="EDP_D0", kind="produces_signal", page=1),
        TypedEdge(src="U11", dst="EDP_D0", kind="consumes_signal", page=1),
        TypedEdge(src="U11", dst="PANEL_D0", kind="produces_signal", page=1),
        TypedEdge(src="U17", dst="PANEL_D0", kind="consumes_signal", page=1),
        # Unrelated power edge — must NOT appear in anomalous BFS.
        TypedEdge(src="U10", dst="+5V", kind="powered_by", page=1),
        # Clock edge — included (`clocks` kind is in the allow-list).
        TypedEdge(src="U11", dst="CLK_P", kind="clocks", page=1),
    ]
    return g


def test_propagate_signal_downstream_reaches_consumers():
    g = _mini_graph_with_signal_edges()
    reached = _propagate_signal_downstream(g, "U10")
    # From U10 we reach EDP_D0 consumers (U11), then PANEL_D0 consumers (U17).
    assert "U11" in reached
    assert "U17" in reached
    # Clock target (U11 already reached, but CLK_P itself is a net not a comp)
    assert reached == {"U11", "U17"}  # no net names — we return refdes only


def test_propagate_signal_downstream_excludes_power_kinds():
    g = _mini_graph_with_signal_edges()
    # Add a power-only edge that should be IGNORED by the anomalous BFS.
    g.typed_edges.append(TypedEdge(src="U10", dst="+3V3", kind="powered_by", page=1))
    reached = _propagate_signal_downstream(g, "U10")
    # +3V3's consumers (U12, U19) must NOT appear — they're on the power side.
    assert "U12" not in reached
    assert "U19" not in reached


def test_simulate_failure_anomalous_contains_downstream_signal_comps():
    g = _mini_graph_with_signal_edges()
    c = _simulate_failure(g, _mini_boot(), "U10", "anomalous")
    assert "U10" in c["anomalous_comps"]
    assert "U11" in c["anomalous_comps"]
    assert "U17" in c["anomalous_comps"]
    # Power unaffected.
    assert c["dead_comps"] == frozenset()
    assert c["dead_rails"] == frozenset()


def test_simulate_failure_anomalous_isolated_component():
    g = _mini_graph()  # No signal edges at all.
    c = _simulate_failure(g, _mini_boot(), "U7", "anomalous")
    # U7 alone (no downstream signal) — only itself marked.
    assert c["anomalous_comps"] == frozenset({"U7"})


def test_simulate_failure_hot_is_self_only():
    g = _mini_graph()
    c = _simulate_failure(g, _mini_boot(), "U7", "hot")
    assert c["hot_comps"] == frozenset({"U7"})
    assert c["dead_comps"] == frozenset()
    assert c["dead_rails"] == frozenset()
    assert c["anomalous_comps"] == frozenset()
    assert c["shorted_rails"] == frozenset()


def test_simulate_failure_shorted_consumer_kills_rail_stresses_source():
    g = _mini_graph()
    # U12 is consumer of +5V. Shorting U12 shorts +5V to GND.
    c = _simulate_failure(g, _mini_boot(), "U12", "shorted")
    # The shorted rail is tagged separately (NOT in dead_rails).
    assert "+5V" in c["shorted_rails"]
    assert "+5V" not in c["dead_rails"]
    # The source of +5V (U7) goes into hot_comps (current-limit stress).
    assert "U7" in c["hot_comps"]
    # Downstream of the killed source propagates as dead (U19, +3V3, U12's own downstream).
    assert "+3V3" in c["dead_rails"]
    assert "U19" in c["dead_comps"]


def test_simulate_failure_shorted_orphan_consumer_returns_self_dead():
    g = _mini_graph()
    # A refdes with NO input power rail (no consumer record) falls back to self-dead.
    g.components["U99"] = ComponentNode(refdes="U99", type="ic", pins=[])
    c = _simulate_failure(g, _mini_boot(), "U99", "shorted")
    assert c["dead_comps"] == frozenset({"U99"})
    assert c["shorted_rails"] == frozenset()
    assert c["hot_comps"] == frozenset()


def test_score_perfect_match_dead():
    obs = Observations(
        state_comps={"U1": "dead", "U7": "alive"},
        state_rails={"+3V3": "dead", "+5V": "alive"},
    )
    cascade = _empty_cascade()
    cascade["dead_comps"] = frozenset({"U1"})
    cascade["dead_rails"] = frozenset({"+3V3"})
    score, metrics, diff = _score_candidate(cascade, obs)
    # 2 dead match + 2 alive match = 4 TP, 0 FP, 0 FN
    assert metrics.tp_comps == 2
    assert metrics.tp_rails == 2
    assert metrics.fp_comps == 0
    assert metrics.fp_rails == 0
    assert score == 4.0
    assert diff.contradictions == []


def test_score_contradiction_cross_mode_costs_10x():
    # Tech observes U7 anomalous, hypothesis predicts U7 dead — soft mismatch.
    obs = Observations(state_comps={"U7": "anomalous"})
    cascade = _empty_cascade()
    cascade["dead_comps"] = frozenset({"U7"})
    score, metrics, diff = _score_candidate(cascade, obs)
    assert metrics.fp_comps == 1
    assert ("U7", "anomalous", "dead") in diff.contradictions
    assert score == -10.0   # 0 TP - 10*1 FP - 0 FN


def test_score_alive_observed_dead_predicted_is_fn():
    obs = Observations(state_comps={"U7": "dead"})
    cascade = _empty_cascade()  # predicts alive
    score, metrics, diff = _score_candidate(cascade, obs)
    assert metrics.fn_comps == 1
    assert "U7" in diff.under_explained
    assert score == -2.0


def test_score_alive_observed_alive_predicted_is_tp():
    obs = Observations(state_comps={"U7": "alive"})
    cascade = _empty_cascade()  # predicts alive
    score, metrics, diff = _score_candidate(cascade, obs)
    assert metrics.tp_comps == 1
    assert score == 1.0


def test_score_shorted_rail_matches_predicted_shorted():
    obs = Observations(state_rails={"+5V": "shorted"})
    cascade = _empty_cascade()
    cascade["shorted_rails"] = frozenset({"+5V"})
    score, metrics, diff = _score_candidate(cascade, obs)
    assert metrics.tp_rails == 1
    assert score == 1.0
    assert diff.contradictions == []


def test_score_anomalous_rail_predicted_hot_comp_matches_hot_obs():
    obs = Observations(state_comps={"Q17": "hot"})
    cascade = _empty_cascade()
    cascade["hot_comps"] = frozenset({"Q17"})
    score, _, diff = _score_candidate(cascade, obs)
    assert score == 1.0
    assert diff.contradictions == []


def test_score_over_predicted_not_penalised():
    obs = Observations(state_comps={"U1": "dead"})
    cascade = _empty_cascade()
    cascade["dead_comps"] = frozenset({"U1", "U99"})  # U99 not in obs
    score, metrics, diff = _score_candidate(cascade, obs)
    assert metrics.fp_comps == 0
    assert ("U99", "dead") in diff.over_predicted
    assert score == 1.0


def test_hypothesize_end_to_end_dead_recovery():
    obs = Observations(
        state_comps={"U12": "dead", "U19": "dead"},
        state_rails={"+5V": "dead"},
    )
    result = hypothesize(
        _mini_graph(), analyzed_boot=_mini_boot(), observations=obs,
    )
    assert len(result.hypotheses) >= 1
    top = result.hypotheses[0]
    assert top.kill_refdes == ["U7"]
    assert top.kill_modes == ["dead"]
    assert top.score > 0
    assert top.narrative != ""
    assert "U7" in top.narrative
    assert "meurt" in top.narrative


def test_hypothesize_end_to_end_anomalous_recovery():
    g = _mini_graph_with_signal_edges()
    obs = Observations(state_comps={"U17": "anomalous"})
    result = hypothesize(
        g, analyzed_boot=_mini_boot(), observations=obs,
    )
    # U10 OR U11 should be in the top (both can explain U17 anomalous).
    top_refdes = {tuple(sorted(h.kill_refdes)) for h in result.hypotheses[:3]}
    assert ("U10",) in top_refdes or ("U11",) in top_refdes


def test_hypothesize_empty_obs_returns_empty():
    r = hypothesize(_mini_graph(), observations=Observations())
    assert r.hypotheses == []
    assert r.pruning.single_candidates_tested == 0


def test_hypothesize_narrative_cites_mode_and_metric():
    obs = Observations(
        state_rails={"+5V": "dead"},
        metrics_rails={
            "+5V": ObservedMetric(measured=0.02, unit="V", nominal=5.0),
        },
    )
    r = hypothesize(
        _mini_graph(), analyzed_boot=_mini_boot(), observations=obs,
    )
    top = r.hypotheses[0]
    # Metric cited in the narrative.
    assert "0.02" in top.narrative or "5.0" in top.narrative


def test_hypothesize_respects_max_results():
    obs = Observations(state_rails={"+5V": "dead", "+3V3": "dead"})
    r = hypothesize(
        _mini_graph(), analyzed_boot=_mini_boot(), observations=obs,
        max_results=1,
    )
    assert len(r.hypotheses) == 1


def test_hypothesize_rejects_ic_observation_with_passive_mode():
    """state_comps[U7] = "open" is meaningless — U7 is an IC."""
    from api.pipeline.schematic.hypothesize import Observations, hypothesize
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="coh-test",
        components={"U7": ComponentNode(refdes="U7", type="ic", kind="ic")},
        nets={}, power_rails={}, typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    with pytest.raises(ValueError, match="U7.*not a valid IC mode"):
        hypothesize(graph, observations=Observations(state_comps={"U7": "open"}))


def test_hypothesize_rejects_passive_observation_with_ic_mode():
    from api.pipeline.schematic.hypothesize import Observations, hypothesize
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="coh-test",
        components={
            "C156": ComponentNode(
                refdes="C156", type="capacitor",
                kind="passive_c", role="decoupling",
            ),
        },
        nets={}, power_rails={}, typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    with pytest.raises(ValueError, match="C156.*not a passive mode"):
        hypothesize(graph, observations=Observations(state_comps={"C156": "anomalous"}))


def test_hypothesize_accepts_coherent_observations():
    from api.pipeline.schematic.hypothesize import Observations, hypothesize
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="coh-test",
        components={
            "U7":   ComponentNode(refdes="U7", type="ic", kind="ic"),
            "C156": ComponentNode(
                refdes="C156", type="capacitor",
                kind="passive_c", role="decoupling",
            ),
        },
        nets={}, power_rails={}, typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    # Should not raise.
    hypothesize(graph, observations=Observations(
        state_comps={"U7": "dead", "C156": "short"},
    ))


def _fb_graph():
    """Simple graph: +3V3 → FB2 → LPC_VCC → U7."""
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, NetNode, PagePin, PowerRail,
        SchematicQualityReport,
    )
    return ElectricalGraph(
        device_slug="fb-test",
        components={
            "U1": ComponentNode(refdes="U1", type="ic", pins=[
                PagePin(number="1", role="power_out", net_label="+3V3"),
            ]),
            "FB2": ComponentNode(
                refdes="FB2", type="ferrite",
                kind="passive_fb", role="filter",
                pins=[
                    PagePin(number="1", role="unknown", net_label="+3V3"),
                    PagePin(number="2", role="unknown", net_label="LPC_VCC"),
                ],
            ),
            "U7": ComponentNode(refdes="U7", type="ic", pins=[
                PagePin(number="1", role="power_in", net_label="LPC_VCC"),
            ]),
        },
        nets={
            "+3V3":    NetNode(label="+3V3",    is_power=True),
            "LPC_VCC": NetNode(label="LPC_VCC", is_power=True),
        },
        power_rails={
            "+3V3":    PowerRail(label="+3V3",    source_refdes="U1", consumers=[]),
            "LPC_VCC": PowerRail(label="LPC_VCC", source_refdes=None, consumers=["U7"]),
        },
        typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )


def test_cascade_series_open_kills_downstream_rail():
    """A series R/D/FB open → downstream rail dead."""
    from api.pipeline.schematic.hypothesize import _cascade_series_open
    graph = _fb_graph()
    fb = graph.components["FB2"]
    result = _cascade_series_open(graph, fb)
    assert "LPC_VCC" in result["dead_rails"]
    # U7 is on that rail → dead by starvation.
    assert "U7" in result["dead_comps"]


def test_cascade_passive_alive_returns_empty():
    from api.pipeline.schematic.hypothesize import _cascade_passive_alive
    graph = _fb_graph()
    result = _cascade_passive_alive(graph, graph.components["FB2"])
    assert result["dead_comps"] == frozenset()
    assert result["dead_rails"] == frozenset()
    assert result["shorted_rails"] == frozenset()
    assert result["anomalous_comps"] == frozenset()
    assert result["hot_comps"] == frozenset()


def test_cascade_filter_open_identical_to_series_open():
    """FB filter open → same behavior as a series element open."""
    from api.pipeline.schematic.hypothesize import (
        _cascade_filter_open, _cascade_series_open,
    )
    graph = _fb_graph()
    fb = graph.components["FB2"]
    a = _cascade_filter_open(graph, fb)
    b = _cascade_series_open(graph, fb)
    assert a == b


def _mnt_like_graph():
    """A graph with: +3V3 source U1, decoupling C156 on U7 VCC, pull-up R11
    on I2C_SDA, feedback divider R43 on +5V regulator U3."""
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, NetNode, PagePin, PowerRail,
        SchematicQualityReport, TypedEdge,
    )
    return ElectricalGraph(
        device_slug="mnt-like",
        components={
            "U1": ComponentNode(refdes="U1", type="ic", pins=[
                PagePin(number="1", role="power_out", net_label="+3V3"),
            ]),
            "U7": ComponentNode(refdes="U7", type="ic", pins=[
                PagePin(number="1", role="power_in", net_label="+3V3"),
            ]),
            "U3": ComponentNode(refdes="U3", type="ic", pins=[
                PagePin(number="1", role="feedback_in", net_label="FB_5V"),
                PagePin(number="2", role="power_out", net_label="+5V"),
            ]),
            "C156": ComponentNode(
                refdes="C156", type="capacitor",
                kind="passive_c", role="decoupling",
                pins=[
                    PagePin(number="1", role="unknown", net_label="+3V3"),
                    PagePin(number="2", role="unknown", net_label="GND"),
                ],
            ),
            "R43": ComponentNode(
                refdes="R43", type="resistor",
                kind="passive_r", role="feedback",
                pins=[
                    PagePin(number="1", role="unknown", net_label="+5V"),
                    PagePin(number="2", role="unknown", net_label="FB_5V"),
                ],
            ),
            "R11": ComponentNode(
                refdes="R11", type="resistor",
                kind="passive_r", role="pull_up",
                pins=[
                    PagePin(number="1", role="unknown", net_label="+3V3"),
                    PagePin(number="2", role="unknown", net_label="I2C_SDA"),
                ],
            ),
            "U9": ComponentNode(refdes="U9", type="ic", pins=[
                PagePin(number="1", role="bus_pin", net_label="I2C_SDA"),
            ]),
        },
        nets={
            "+3V3":    NetNode(label="+3V3", is_power=True),
            "+5V":     NetNode(label="+5V",  is_power=True),
            "FB_5V":   NetNode(label="FB_5V"),
            "I2C_SDA": NetNode(label="I2C_SDA"),
            "GND":     NetNode(label="GND", is_global=True),
        },
        power_rails={
            "+3V3": PowerRail(label="+3V3", source_refdes="U1", consumers=["U7"]),
            "+5V":  PowerRail(label="+5V",  source_refdes="U3", consumers=[]),
        },
        typed_edges=[
            TypedEdge(src="U7", dst="+3V3", kind="powers"),
            TypedEdge(src="C156", dst="+3V3", kind="decouples"),
            TypedEdge(src="FB_5V", dst="R43", kind="feedback_in"),
            TypedEdge(src="U9", dst="I2C_SDA", kind="consumes_signal"),
        ],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )


def test_cascade_decoupling_short_kills_rail():
    from api.pipeline.schematic.hypothesize import _cascade_decoupling_short
    graph = _mnt_like_graph()
    c = _cascade_decoupling_short(graph, graph.components["C156"])
    assert "+3V3" in c["shorted_rails"]
    assert "U1" in c["hot_comps"]
    assert "U7" in c["dead_comps"]


def test_cascade_decoupling_open_marks_upstream_ic_anomalous():
    from api.pipeline.schematic.hypothesize import _cascade_decoupling_open
    graph = _mnt_like_graph()
    c = _cascade_decoupling_open(graph, graph.components["C156"])
    assert c["anomalous_comps"] == frozenset({"U7"})


def test_cascade_feedback_open_triggers_overvoltage():
    from api.pipeline.schematic.hypothesize import _cascade_feedback_open_overvolt
    graph = _mnt_like_graph()
    c = _cascade_feedback_open_overvolt(graph, graph.components["R43"])
    assert "+5V" in c["shorted_rails"]


def test_cascade_pull_up_open_marks_signal_consumers_anomalous():
    from api.pipeline.schematic.hypothesize import _cascade_pull_up_open
    graph = _mnt_like_graph()
    c = _cascade_pull_up_open(graph, graph.components["R11"])
    assert "U9" in c["anomalous_comps"]


def test_table_covers_all_resistor_and_capacitor_roles():
    from api.pipeline.schematic.hypothesize import _PASSIVE_CASCADE_TABLE
    # After T7, the table has all R + C entries.
    for r_role in ("series", "feedback", "pull_up", "pull_down"):
        for mode in ("open", "short"):
            assert ("passive_r", r_role, mode) in _PASSIVE_CASCADE_TABLE, (
                f"missing handler for passive_r/{r_role}/{mode}"
            )
    for c_role in ("decoupling", "bulk", "filter", "ac_coupling", "tank", "bypass"):
        for mode in ("open", "short"):
            assert ("passive_c", c_role, mode) in _PASSIVE_CASCADE_TABLE, (
                f"missing handler for passive_c/{c_role}/{mode}"
            )
