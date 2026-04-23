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


def test_simulate_failure_anomalous_and_hot_pending():
    for mode in ("anomalous", "hot", "shorted"):
        with pytest.raises(NotImplementedError):
            _simulate_failure(_mini_graph(), _mini_boot(), "U7", mode)
