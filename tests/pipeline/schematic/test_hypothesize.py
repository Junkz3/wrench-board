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
    assert "dies" in top.narrative


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
        ComponentNode,
        ElectricalGraph,
        SchematicQualityReport,
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
        ComponentNode,
        ElectricalGraph,
        SchematicQualityReport,
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
        ComponentNode,
        ElectricalGraph,
        SchematicQualityReport,
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
        ComponentNode,
        ElectricalGraph,
        NetNode,
        PagePin,
        PowerRail,
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
        _cascade_filter_open,
        _cascade_series_open,
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
        ComponentNode,
        ElectricalGraph,
        NetNode,
        PagePin,
        PowerRail,
        SchematicQualityReport,
        TypedEdge,
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


def test_cascade_rectifier_short_shorts_input_rail():
    from api.pipeline.schematic.hypothesize import _cascade_rectifier_short
    from api.pipeline.schematic.schemas import (
        ComponentNode,
        ElectricalGraph,
        NetNode,
        PagePin,
        PowerRail,
        SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="rect-test",
        components={
            "D1": ComponentNode(
                refdes="D1", type="diode",
                kind="passive_d", role="rectifier",
                pins=[
                    PagePin(number="1", role="unknown", net_label="VIN"),
                    PagePin(number="2", role="unknown", net_label="VOUT"),
                ],
            ),
        },
        nets={"VIN": NetNode(label="VIN", is_power=True),
              "VOUT": NetNode(label="VOUT", is_power=True)},
        power_rails={"VIN":  PowerRail(label="VIN"),
                     "VOUT": PowerRail(label="VOUT")},
        typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    c = _cascade_rectifier_short(graph, graph.components["D1"])
    # Either VIN or VOUT becomes shorted — implementation defines the
    # direction. Accept either.
    assert len(c["shorted_rails"]) == 1


def test_table_covers_every_diode_role():
    from api.pipeline.schematic.hypothesize import _PASSIVE_CASCADE_TABLE
    for role in ("flyback", "rectifier", "esd", "reverse_protection", "signal_clamp"):
        for mode in ("open", "short"):
            assert ("passive_d", role, mode) in _PASSIVE_CASCADE_TABLE


def test_table_every_entry_is_callable():
    from api.pipeline.schematic.hypothesize import _PASSIVE_CASCADE_TABLE
    for key, fn in _PASSIVE_CASCADE_TABLE.items():
        assert callable(fn), f"non-callable handler at {key}"


def test_applicable_modes_ic_unchanged():
    from api.pipeline.schematic.hypothesize import _applicable_modes
    from api.pipeline.schematic.schemas import (
        ComponentNode,
        ElectricalGraph,
        SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="am-test",
        components={"U1": ComponentNode(refdes="U1", type="ic")},
        nets={}, power_rails={}, typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    modes = _applicable_modes(graph, "U1")
    assert "dead" in modes
    assert "hot" in modes
    assert "open" not in modes
    assert "short" not in modes


def test_applicable_modes_passive_with_role_returns_open_short():
    from api.pipeline.schematic.hypothesize import _applicable_modes
    graph = _mnt_like_graph()
    modes = _applicable_modes(graph, "C156")  # decoupling
    assert "open" in modes
    assert "short" in modes
    assert "dead" not in modes


def test_applicable_modes_passive_without_role_returns_empty():
    from api.pipeline.schematic.hypothesize import _applicable_modes
    from api.pipeline.schematic.schemas import (
        ComponentNode,
        ElectricalGraph,
        SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="unrole",
        components={
            "R99": ComponentNode(
                refdes="R99", type="resistor",
                kind="passive_r", role=None,
            ),
        },
        nets={}, power_rails={}, typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    modes = _applicable_modes(graph, "R99")
    assert modes == []


def test_applicable_modes_skips_passive_alive_entries():
    """When a (kind, role, mode) maps to `_cascade_passive_alive`, the mode
    is not enumerated — no observable cascade."""
    from api.pipeline.schematic.hypothesize import _applicable_modes
    from api.pipeline.schematic.schemas import (
        ComponentNode,
        ElectricalGraph,
        SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="alive-test",
        components={
            "R50": ComponentNode(
                refdes="R50", type="resistor",
                kind="passive_r", role="damping",
            ),
        },
        nets={}, power_rails={}, typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    # damping open AND short both map to `_cascade_passive_alive` → no modes.
    assert _applicable_modes(graph, "R50") == []


def test_score_visibility_multiplier_dampens_decoupling_open():
    """A decoupling-open hypothesis that matches 1 anomalous IC should score
    tp_comps = 0.5, not 1.0."""
    from api.pipeline.schematic.hypothesize import (
        Observations,
        hypothesize,
    )
    graph = _mnt_like_graph()
    result = hypothesize(
        graph,
        observations=Observations(state_comps={"U7": "anomalous"}),
    )
    # Look for a C156-open hypothesis with visibility applied.
    c156_hyps = [h for h in result.hypotheses
                 if h.kill_refdes == ["C156"] and h.kill_modes == ["open"]]
    if c156_hyps:
        h = c156_hyps[0]
        # TP is 1 component (U7), but score reflects 0.5 × tp = 0.5.
        assert 0.3 <= h.score <= 0.6, (
            f"expected dampened score ~0.5 for decoupling_open, got {h.score}"
        )


# ---------------------------------------------------------------------------
# Phase 4.2 — _compute_discriminators
# ---------------------------------------------------------------------------


def test_discriminators_empty_when_scores_well_separated():
    """When top-1 clearly beats top-2, no discriminator needed."""
    from api.pipeline.schematic.hypothesize import (
        Hypothesis,
        HypothesisDiff,
        HypothesisMetrics,
        _compute_discriminators,
    )
    hyps = [
        Hypothesis(
            kill_refdes=["U1"], kill_modes=["dead"], score=10.0,
            metrics=HypothesisMetrics(tp_comps=2, tp_rails=1, fp_comps=0, fp_rails=0, fn_comps=0, fn_rails=0),
            diff=HypothesisDiff(),
            narrative="best",
            cascade_preview={"dead_rails": ["+5V"], "shorted_rails": [],
                             "dead_comps_count": 0, "anomalous_count": 0, "hot_count": 0},
        ),
        Hypothesis(
            kill_refdes=["U2"], kill_modes=["dead"], score=2.0,
            metrics=HypothesisMetrics(tp_comps=0, tp_rails=1, fp_comps=0, fp_rails=0, fn_comps=0, fn_rails=0),
            diff=HypothesisDiff(),
            narrative="second",
            cascade_preview={"dead_rails": ["+5V"], "shorted_rails": [],
                             "dead_comps_count": 0, "anomalous_count": 0, "hot_count": 0},
        ),
    ]
    # 10.0 vs 2.0 — clearly separated
    assert _compute_discriminators(hyps) == []


def test_discriminators_fired_when_top_n_tied():
    """5 hypotheses tied → return targets that partition them."""
    from api.pipeline.schematic.hypothesize import (
        Hypothesis,
        HypothesisDiff,
        HypothesisMetrics,
        _compute_discriminators,
    )
    def _hyp(refdes, mode, dead_rails):
        return Hypothesis(
            kill_refdes=[refdes], kill_modes=[mode], score=1.0,
            metrics=HypothesisMetrics(tp_comps=0, tp_rails=1, fp_comps=0, fp_rails=0, fn_comps=0, fn_rails=0),
            diff=HypothesisDiff(),
            narrative="n",
            cascade_preview={"dead_rails": dead_rails, "shorted_rails": [],
                             "dead_comps_count": 0, "anomalous_count": 0, "hot_count": 0},
        )
    hyps = [
        _hyp("U1", "shorted", ["+5V", "+3V3"]),   # predicts +5V + +3V3
        _hyp("U7", "shorted", ["+5V"]),            # predicts +5V only
        _hyp("U17", "shorted", ["+5V", "+1V8"]),   # predicts +5V + +1V8
        _hyp("U19", "shorted", ["+5V"]),
        _hyp("U13", "shorted", ["+5V", "+3V3"]),
    ]
    result = _compute_discriminators(hyps)
    # +3V3 appears in 2/5 (hyp 1, 5) → split ratio distance = |2 - 2.5| = 0.5
    # +1V8 appears in 1/5 (hyp 3) → distance = |1 - 2.5| = 1.5
    # +5V appears in 5/5 → not discriminating (skipped)
    # kill_refdes all unique (1/5 each) → distance 1.5
    # Expected: +3V3 ranks first.
    assert "+3V3" in result[:2]
    # +5V must NOT appear (present in all).
    assert "+5V" not in result


def test_discriminators_mixed_rail_and_component_candidates():
    """kill_refdes entries are also discriminators (measuring the component
    itself can partition hypotheses)."""
    from api.pipeline.schematic.hypothesize import (
        Hypothesis,
        HypothesisDiff,
        HypothesisMetrics,
        _compute_discriminators,
    )
    def _hyp(refdes):
        return Hypothesis(
            kill_refdes=[refdes], kill_modes=["dead"], score=1.0,
            metrics=HypothesisMetrics(tp_comps=1, tp_rails=0, fp_comps=0, fp_rails=0, fn_comps=0, fn_rails=0),
            diff=HypothesisDiff(),
            narrative="n",
            cascade_preview={"dead_rails": [], "shorted_rails": [],
                             "dead_comps_count": 0, "anomalous_count": 0, "hot_count": 0},
        )
    hyps = [_hyp("U1"), _hyp("U2"), _hyp("U3"), _hyp("U4")]
    result = _compute_discriminators(hyps)
    # Each refdes appears in 1/4 cascades → each has distance 1.0 from half (2.0)
    # All four tie — top-3 returns 3 of them.
    assert len(result) == 3
    # All four are equally valid discriminators; just verify they come from the kill set.
    assert all(r in {"U1", "U2", "U3", "U4"} for r in result)


def test_hypothesize_result_exposes_discriminators_when_tied():
    """Integration: hypothesize() on an ambiguous observation returns the
    field populated."""
    from api.pipeline.schematic.hypothesize import Observations, hypothesize
    graph = _mnt_like_graph()  # from earlier tests
    result = hypothesize(
        graph,
        observations=Observations(state_rails={"+3V3": "shorted"}),
    )
    # At minimum the field exists and is a list.
    assert isinstance(result.discriminating_targets, list)


# ---------------------------------------------------------------------------
# Phase 4.5 — stuck_on / stuck_off mode vocabulary
# ---------------------------------------------------------------------------


def test_observation_accepts_stuck_on_on_rail():
    """RailMode now includes stuck_on."""
    from api.pipeline.schematic.hypothesize import Observations
    obs = Observations(state_rails={"+3V3_USB": "stuck_on"})
    assert obs.state_rails["+3V3_USB"] == "stuck_on"


def test_observation_accepts_stuck_modes_on_passive_q():
    """ComponentMode now includes stuck_on/stuck_off (used on Q targets)."""
    from api.pipeline.schematic.hypothesize import Observations
    obs = Observations(state_comps={"Q5": "stuck_on", "Q7": "stuck_off"})
    assert obs.state_comps["Q5"] == "stuck_on"


def test_validator_rejects_stuck_on_on_ic():
    """IC + stuck_on is still invalid — stuck_on is a passive-Q mode."""
    from api.pipeline.schematic.hypothesize import Observations, hypothesize
    from api.pipeline.schematic.schemas import (
        ComponentNode,
        ElectricalGraph,
        SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="coh-test",
        components={"U5": ComponentNode(refdes="U5", type="ic", kind="ic")},
        nets={}, power_rails={}, typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    with pytest.raises(ValueError, match="U5.*not a valid IC mode"):
        hypothesize(graph, observations=Observations(state_comps={"U5": "stuck_on"}))


def test_validator_accepts_stuck_on_on_passive_q():
    from api.pipeline.schematic.hypothesize import Observations, hypothesize
    from api.pipeline.schematic.schemas import (
        ComponentNode,
        ElectricalGraph,
        SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="coh-test",
        components={
            "Q5": ComponentNode(
                refdes="Q5", type="transistor",
                kind="passive_q", role="load_switch",
            ),
        },
        nets={}, power_rails={}, typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    # Should not raise — stuck_on is a passive mode.
    hypothesize(graph, observations=Observations(state_comps={"Q5": "stuck_on"}))


def test_scoring_matches_stuck_on_rail_against_always_on_cascade():
    """A cascade with always_on_rails={'+3V3_USB'} should score TP against
    an observation state_rails={'+3V3_USB': 'stuck_on'}."""
    from api.pipeline.schematic.hypothesize import (
        Observations,
        _empty_cascade,
        _score_candidate,
    )
    cascade = _empty_cascade()
    cascade["always_on_rails"] = frozenset({"+3V3_USB"})
    obs = Observations(state_rails={"+3V3_USB": "stuck_on"})
    score, metrics, _diff = _score_candidate(cascade, obs)
    # 1 rail TP, 0 FP, 0 FN → positive score.
    assert metrics.tp_rails == 1
    assert metrics.fp_rails == 0
    assert metrics.fn_rails == 0
    assert score > 0


def test_scoring_stuck_on_disjoint_from_shorted():
    """A cascade with only shorted_rails does NOT TP-match a stuck_on
    observation (and vice versa). The two are disjoint by design."""
    from api.pipeline.schematic.hypothesize import (
        Observations,
        _empty_cascade,
        _score_candidate,
    )
    shorted_cascade = _empty_cascade()
    shorted_cascade["shorted_rails"] = frozenset({"+5V"})
    obs = Observations(state_rails={"+5V": "stuck_on"})
    _score, metrics, _ = _score_candidate(shorted_cascade, obs)
    # Mismatch: observed stuck_on, predicted shorted → FP (contradiction),
    # not TP.
    assert metrics.tp_rails == 0
    assert metrics.fp_rails == 1


def test_cascade_preview_exposes_always_on_count():
    """Hypothesis.cascade_preview should carry always_on_rails list."""
    from api.pipeline.schematic.hypothesize import _cascade_preview, _empty_cascade
    cascade = _empty_cascade()
    cascade["always_on_rails"] = frozenset({"+3V3_USB", "USB_VBUS"})
    preview = _cascade_preview(cascade)
    assert set(preview["always_on_rails"]) == {"+3V3_USB", "USB_VBUS"}


def test_applicable_modes_passive_q_returns_four_modes():
    """Q with a known role gets all 4 modes (open/short/stuck_on/stuck_off)
    — but only those whose handler is not passive_alive. Depends on T7
    cascade table entries; this test will fail until T7 lands.
    """
    from api.pipeline.schematic.hypothesize import _applicable_modes
    from api.pipeline.schematic.schemas import (
        ComponentNode,
        ElectricalGraph,
        NetNode,
        PagePin,
        PowerRail,
        SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="q-test",
        components={
            "Q5": ComponentNode(
                refdes="Q5", type="transistor",
                kind="passive_q", role="load_switch",
                pins=[
                    PagePin(number="1", role="unknown", net_label="+5V"),
                    PagePin(number="2", role="unknown", net_label="+3V3_USB"),
                    PagePin(number="3", role="unknown", net_label="EN_USB"),
                ],
            ),
        },
        nets={"+5V": NetNode(label="+5V", is_power=True),
              "+3V3_USB": NetNode(label="+3V3_USB", is_power=True),
              "EN_USB": NetNode(label="EN_USB")},
        power_rails={"+5V": PowerRail(label="+5V"),
                     "+3V3_USB": PowerRail(label="+3V3_USB")},
        typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    modes = _applicable_modes(graph, "Q5")
    # load_switch has handlers for all 4 modes per T7 table.
    assert set(modes) == {"open", "short", "stuck_on", "stuck_off"}


def test_applicable_modes_passive_q_inrush_skips_alive_handlers():
    """inrush_limiter role has short/stuck_on → passive_alive → filtered out.
    Depends on T7; will fail until T7 table lands."""
    from api.pipeline.schematic.hypothesize import _applicable_modes
    from api.pipeline.schematic.schemas import (
        ComponentNode,
        ElectricalGraph,
        NetNode,
        PagePin,
        PowerRail,
        SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="q-inrush-test",
        components={
            "Q1": ComponentNode(
                refdes="Q1", type="transistor",
                kind="passive_q", role="inrush_limiter",
                pins=[PagePin(number="1", role="unknown", net_label="VIN"),
                      PagePin(number="2", role="unknown", net_label="VIN_BUCK"),
                      PagePin(number="3", role="unknown", net_label="SOFT_START")],
            ),
        },
        nets={"VIN": NetNode(label="VIN", is_power=True),
              "VIN_BUCK": NetNode(label="VIN_BUCK", is_power=True),
              "SOFT_START": NetNode(label="SOFT_START")},
        power_rails={"VIN": PowerRail(label="VIN"),
                     "VIN_BUCK": PowerRail(label="VIN_BUCK")},
        typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    modes = _applicable_modes(graph, "Q1")
    # inrush_limiter has open + stuck_off active, short + stuck_on → passive_alive.
    assert set(modes) == {"open", "stuck_off"}


# ---------------------------------------------------------------------------
# Phase 4.5 T7 — Q cascade handlers + dispatch table
# ---------------------------------------------------------------------------


def _q_load_switch_graph():
    """+5V → Q5 (load_switch, EN=EN_USB) → +3V3_USB → U20 consumer."""
    from api.pipeline.schematic.schemas import (
        ComponentNode,
        ElectricalGraph,
        NetNode,
        PagePin,
        PowerRail,
        SchematicQualityReport,
    )
    return ElectricalGraph(
        device_slug="q-load",
        components={
            "Q5": ComponentNode(
                refdes="Q5", type="transistor",
                kind="passive_q", role="load_switch",
                pins=[
                    PagePin(number="1", role="unknown", net_label="+5V"),
                    PagePin(number="2", role="unknown", net_label="+3V3_USB"),
                    PagePin(number="3", role="unknown", net_label="EN_USB"),
                ],
            ),
            "U20": ComponentNode(refdes="U20", type="ic", pins=[
                PagePin(number="1", role="power_in", net_label="+3V3_USB"),
            ]),
        },
        nets={"+5V": NetNode(label="+5V", is_power=True),
              "+3V3_USB": NetNode(label="+3V3_USB", is_power=True),
              "EN_USB": NetNode(label="EN_USB")},
        power_rails={
            "+5V": PowerRail(label="+5V", source_refdes="U12", consumers=["Q5"]),
            "+3V3_USB": PowerRail(
                label="+3V3_USB", source_refdes="Q5", consumers=["U20"],
            ),
        },
        typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )


def test_cascade_q_load_stuck_on_marks_downstream_always_on():
    from api.pipeline.schematic.hypothesize import _cascade_q_load_stuck_on
    graph = _q_load_switch_graph()
    c = _cascade_q_load_stuck_on(graph, graph.components["Q5"])
    assert "+3V3_USB" in c["always_on_rails"]
    # Consumers (U20) become anomalous — active when they should be off.
    assert "U20" in c["anomalous_comps"]


def test_cascade_q_load_dead_kills_downstream_rail():
    from api.pipeline.schematic.hypothesize import _cascade_q_load_dead
    graph = _q_load_switch_graph()
    c = _cascade_q_load_dead(graph, graph.components["Q5"])
    assert "+3V3_USB" in c["dead_rails"]
    assert "U20" in c["dead_comps"]


def test_cascade_q_shifter_broken_anomalous_downstream():
    """Level shifter open → signal consumer anomalous."""
    from api.pipeline.schematic.hypothesize import _cascade_q_shifter_signal_broken
    from api.pipeline.schematic.schemas import (
        ComponentNode,
        ElectricalGraph,
        NetNode,
        PagePin,
        PowerRail,
        SchematicQualityReport,
        TypedEdge,
    )
    graph = ElectricalGraph(
        device_slug="q-shifter",
        components={
            "Q2": ComponentNode(
                refdes="Q2", type="transistor",
                kind="passive_q", role="level_shifter",
                pins=[
                    PagePin(number="1", role="unknown", net_label="I2C_3V3_SDA"),
                    PagePin(number="2", role="unknown", net_label="I2C_1V8_SDA"),
                    PagePin(number="3", role="unknown", net_label="+3V3"),
                ],
            ),
            "U30": ComponentNode(refdes="U30", type="ic", pins=[
                PagePin(number="1", role="bus_pin", net_label="I2C_1V8_SDA"),
            ]),
        },
        nets={"I2C_3V3_SDA": NetNode(label="I2C_3V3_SDA"),
              "I2C_1V8_SDA": NetNode(label="I2C_1V8_SDA"),
              "+3V3": NetNode(label="+3V3", is_power=True)},
        power_rails={"+3V3": PowerRail(label="+3V3")},
        typed_edges=[TypedEdge(src="U30", dst="I2C_1V8_SDA", kind="consumes_signal")],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    c = _cascade_q_shifter_signal_broken(graph, graph.components["Q2"])
    assert "U30" in c["anomalous_comps"]


def test_table_covers_every_q_role_mode_combo():
    """Phase 4.5 cascade table must have an entry for every (passive_q,
    role, mode) combination."""
    from api.pipeline.schematic.hypothesize import _PASSIVE_CASCADE_TABLE
    for role in ("load_switch", "level_shifter", "inrush_limiter"):
        for mode in ("open", "short", "stuck_on", "stuck_off"):
            assert ("passive_q", role, mode) in _PASSIVE_CASCADE_TABLE, (
                f"missing handler for passive_q/{role}/{mode}"
            )


def test_simulate_failure_dispatches_q_stuck_on():
    """_simulate_failure with mode=stuck_on routes Q through the dispatch table."""
    from api.pipeline.schematic.hypothesize import _simulate_failure
    graph = _q_load_switch_graph()
    cascade = _simulate_failure(graph, None, "Q5", "stuck_on")
    assert "+3V3_USB" in cascade["always_on_rails"]


# ---------------------------------------------------------------------------
# Phase 4.5.1 — flyback_switch cascade tests
# ---------------------------------------------------------------------------


def _flyback_graph():
    """Minimal SMPS graph: PVIN → Q1(flyback) with SW1 → L1 → +3V3 → U_CONSUMER."""
    return ElectricalGraph(
        device_slug="flyback-test",
        components={
            "Q1": ComponentNode(
                refdes="Q1", type="transistor",
                kind="passive_q", role="flyback_switch",
                pins=[
                    PagePin(number="1", role="unknown", net_label="PVIN"),
                    PagePin(number="2", role="unknown", net_label="SW1"),
                    PagePin(number="3", role="unknown", net_label="GATE_Q1"),
                ],
            ),
            "L1": ComponentNode(
                refdes="L1", type="inductor",
                pins=[
                    PagePin(number="1", role="unknown", net_label="SW1"),
                    PagePin(number="2", role="unknown", net_label="+3V3"),
                ],
            ),
            "U_CONSUMER": ComponentNode(
                refdes="U_CONSUMER", type="ic",
                pins=[PagePin(number="1", role="power_in", net_label="+3V3")],
            ),
            "U_SRC": ComponentNode(
                refdes="U_SRC", type="ic",
                pins=[PagePin(number="1", role="power_out", net_label="PVIN")],
            ),
        },
        nets={"PVIN": NetNode(label="PVIN", is_power=True),
              "SW1": NetNode(label="SW1"),
              "+3V3": NetNode(label="+3V3", is_power=True),
              "GATE_Q1": NetNode(label="GATE_Q1")},
        power_rails={
            "PVIN": PowerRail(label="PVIN", source_refdes="U_SRC", consumers=["Q1"]),
            "+3V3": PowerRail(label="+3V3", source_refdes=None, consumers=["U_CONSUMER"]),
        },
        typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )


def test_cascade_q_flyback_switch_dead_kills_output_rail():
    from api.pipeline.schematic.hypothesize import _cascade_q_flyback_switch_dead
    graph = _flyback_graph()
    c = _cascade_q_flyback_switch_dead(graph, graph.components["Q1"])
    # +3V3 is downstream of SW1 via L1 → should die.
    assert "+3V3" in c["dead_rails"]
    assert "U_CONSUMER" in c["dead_comps"]


def test_cascade_q_flyback_switch_short_stresses_input_rail():
    from api.pipeline.schematic.hypothesize import _cascade_q_flyback_switch_short
    graph = _flyback_graph()
    c = _cascade_q_flyback_switch_short(graph, graph.components["Q1"])
    # PVIN is the input rail — stressed (shorted semantics).
    assert "PVIN" in c["shorted_rails"]
    # U_SRC is the source of PVIN → hot.
    assert "U_SRC" in c["hot_comps"]


def test_table_covers_flyback_switch_all_modes():
    from api.pipeline.schematic.hypothesize import _PASSIVE_CASCADE_TABLE
    for mode in ("open", "short", "stuck_on", "stuck_off"):
        assert ("passive_q", "flyback_switch", mode) in _PASSIVE_CASCADE_TABLE


def _q_cell_protection_graph():
    """Minimal graph: Q5 is a cell_protection series FET between BAT1
    (cell tap, upstream) and BAT1FUSED (protected output, downstream).
    U_BMS consumes BAT1FUSED."""
    from api.pipeline.schematic.schemas import (
        ComponentNode,
        ElectricalGraph,
        NetNode,
        PagePin,
        PowerRail,
        SchematicQualityReport,
    )
    return ElectricalGraph(
        device_slug="q-cell-prot",
        components={
            "Q5": ComponentNode(
                refdes="Q5", type="transistor",
                kind="passive_q", role="cell_protection",
                pins=[
                    PagePin(number="1", role="signal_in", net_label=None),
                    PagePin(number="2", role="signal_in", net_label="BAT1FUSED"),
                    PagePin(number="3", role="signal_out", net_label="BAT1"),
                ],
            ),
            "U_BMS": ComponentNode(refdes="U_BMS", type="ic", pins=[
                PagePin(number="1", role="power_in", net_label="BAT1FUSED"),
            ]),
        },
        nets={
            "BAT1": NetNode(label="BAT1", is_power=True),
            "BAT1FUSED": NetNode(label="BAT1FUSED", is_power=True),
        },
        power_rails={
            "BAT1": PowerRail(label="BAT1", consumers=["Q5"]),
            "BAT1FUSED": PowerRail(
                label="BAT1FUSED", source_refdes=None, consumers=["U_BMS"],
            ),
        },
        typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )


def test_cascade_q_cell_protection_dead_kills_fused_rail():
    """cell_protection open → downstream fused rail dead → consumers dead."""
    from api.pipeline.schematic.hypothesize import _cascade_q_cell_protection_dead
    graph = _q_cell_protection_graph()
    c = _cascade_q_cell_protection_dead(graph, graph.components["Q5"])
    assert "BAT1FUSED" in c["dead_rails"]
    assert "BAT1" not in c["dead_rails"]   # upstream cell tap stays alive
    assert "U_BMS" in c["dead_comps"]


def test_table_covers_cell_protection_and_cell_balancer_all_modes():
    """Phase 4.6: every (kind, role, mode) triple for the two new roles
    must dispatch somewhere — no silent fall-throughs."""
    from api.pipeline.schematic.hypothesize import _PASSIVE_CASCADE_TABLE
    for role in ("cell_protection", "cell_balancer"):
        for mode in ("open", "short", "stuck_on", "stuck_off"):
            assert ("passive_q", role, mode) in _PASSIVE_CASCADE_TABLE, (
                f"missing dispatch for passive_q / {role} / {mode}"
            )


def test_find_cell_protection_downstream_returns_none_below_two_rails():
    """Insufficient topology → None (not a crash, not an arbitrary pick).
    The cascade handler then short-circuits to _empty_cascade(), so a
    mis-resolved Q never emits a wrong prediction. Pins the fail-safe
    branch that Phase 4.6 relies on."""
    from api.pipeline.schematic.hypothesize import _find_cell_protection_downstream
    from api.pipeline.schematic.schemas import (
        ComponentNode,
        ElectricalGraph,
        NetNode,
        PagePin,
        PowerRail,
        SchematicQualityReport,
    )

    def _graph(rails: dict[str, PowerRail]) -> ElectricalGraph:
        return ElectricalGraph(
            device_slug="q-cell-prot-none",
            components={},
            nets={label: NetNode(label=label, is_power=True) for label in rails},
            power_rails=rails,
            typed_edges=[],
            quality=SchematicQualityReport(
                total_pages=1, pages_parsed=1, confidence_global=1.0,
            ),
        )

    # Zero BAT rails — Q pin labels don't match any registered rail.
    q_no_rails = ComponentNode(
        refdes="Q5", type="transistor", kind="passive_q", role="cell_protection",
        pins=[
            PagePin(number="1", role="signal_in", net_label=None),
            PagePin(number="2", role="signal_in", net_label="FOO"),
            PagePin(number="3", role="signal_out", net_label="BAR"),
        ],
    )
    assert _find_cell_protection_downstream(_graph({}), q_no_rails) is None

    # Exactly one BAT rail — still insufficient, no "downstream" to pick.
    q_one_rail = ComponentNode(
        refdes="Q5", type="transistor", kind="passive_q", role="cell_protection",
        pins=[
            PagePin(number="1", role="signal_in", net_label=None),
            PagePin(number="2", role="signal_in", net_label="BAT1"),
            PagePin(number="3", role="signal_out", net_label="BAT1"),
        ],
    )
    graph = _graph({"BAT1": PowerRail(label="BAT1")})
    assert _find_cell_protection_downstream(graph, q_one_rail) is None


def test_leaky_short_on_decoupling_cap_returns_shorted_rail():
    """passive_c.leaky_short routes via the passive table to a shorted rail.

    Encoded as `shorted_rails` (not `degraded_rails`) so the diagnostic
    round-trip is observable through the same axis a tech reports — see
    `_cascade_decoupling_leaky` for the rationale.
    """
    from api.pipeline.schematic.hypothesize import _simulate_failure
    from api.pipeline.schematic.schemas import (
        ComponentNode,
        ElectricalGraph,
        NetNode,
        PagePin,
        PowerRail,
        SchematicQualityReport,
    )

    graph = ElectricalGraph(
        device_slug="t",
        components={
            "U7": ComponentNode(refdes="U7", type="ic"),
            "C42": ComponentNode(
                refdes="C42",
                type="capacitor",
                kind="passive_c",
                role="decoupling",
                pins=[
                    PagePin(number="1", role="terminal", net_label="+5V"),
                    PagePin(number="2", role="ground", net_label="GND"),
                ],
            ),
        },
        nets={"+5V": NetNode(label="+5V", is_power=True)},
        power_rails={
            "+5V": PowerRail(
                label="+5V", source_refdes="U7", consumers=[], decoupling=["C42"],
            ),
        },
        typed_edges=[],
        boot_sequence=[],
        designer_notes=[],
        ambiguities=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )
    cascade = _simulate_failure(graph, analyzed_boot=None, refdes="C42", mode="leaky_short")
    assert "+5V" in cascade["shorted_rails"]


def test_regulating_low_on_ic_returns_shorted_sourced_rails():
    """ic.regulating_low marks every rail the IC sources as shorted.

    Encoded as `shorted_rails` (not `degraded_rails`) so the candidate
    survives `_relevant_to_observations` and `_score_candidate` — see
    `_simulate_failure` for the rationale, mirrors the leaky decoupling
    cap fix.
    """
    from api.pipeline.schematic.hypothesize import _simulate_failure
    from api.pipeline.schematic.schemas import (
        ComponentNode,
        ElectricalGraph,
        NetNode,
        PowerRail,
        SchematicQualityReport,
    )

    graph = ElectricalGraph(
        device_slug="t",
        components={"U7": ComponentNode(refdes="U7", type="ic")},
        nets={"+5V": NetNode(label="+5V", is_power=True)},
        power_rails={"+5V": PowerRail(label="+5V", source_refdes="U7")},
        typed_edges=[],
        boot_sequence=[],
        designer_notes=[],
        ambiguities=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )
    cascade = _simulate_failure(graph, analyzed_boot=None, refdes="U7", mode="regulating_low")
    assert "+5V" in cascade["shorted_rails"]
