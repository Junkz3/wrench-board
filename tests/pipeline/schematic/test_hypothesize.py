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
    HypothesizeResult,
    ObservedMetric,
    Observations,
    PruningStats,
    _empty_cascade,
    _simulate_dead,
    _simulate_failure,
    hypothesize,
)
from api.pipeline.schematic.schemas import (
    AnalyzedBootPhase,
    AnalyzedBootSequence,
    AnalyzedBootTrigger,
    BootPhase,
    ComponentNode,
    ElectricalGraph,
    NetNode,
    PagePin,
    PowerRail,
    SchematicQualityReport,
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


def test_hypothesize_stub_raises_not_implemented():
    eg = ElectricalGraph(
        device_slug="demo",
        components={}, nets={}, power_rails={}, typed_edges=[],
        boot_sequence=[], designer_notes=[], ambiguities=[],
        quality=SchematicQualityReport(total_pages=0, pages_parsed=0),
    )
    with pytest.raises(NotImplementedError):
        hypothesize(eg, observations=Observations())


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


def test_simulate_failure_shorted_pending():
    with pytest.raises(NotImplementedError):
        _simulate_failure(_mini_graph(), _mini_boot(), "U7", "shorted")


from api.pipeline.schematic.hypothesize import _propagate_signal_downstream
from api.pipeline.schematic.schemas import TypedEdge


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
