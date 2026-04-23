# SPDX-License-Identifier: Apache-2.0
"""Tests for the passive role classifier (heuristic + Opus post-pass).

Deterministic path exercised directly. LLM path mocked at
`call_with_forced_tool` — never hits Anthropic in tests.
"""

from __future__ import annotations

import pytest

from api.pipeline.schematic.passive_classifier import (
    classify_passive_refdes,
    classify_passives_heuristic,
)
from api.pipeline.schematic.schemas import (
    ComponentNode, ElectricalGraph, NetNode, PagePin, PowerRail,
    SchematicQualityReport, TypedEdge,
)


def _graph_with_rails(*rail_labels: str) -> ElectricalGraph:
    return ElectricalGraph(
        device_slug="passive-test",
        components={},
        nets={r: NetNode(label=r, is_power=True) for r in rail_labels},
        power_rails={r: PowerRail(label=r) for r in rail_labels},
        typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )


# --------- resistors ---------

def test_resistor_feedback_edge_wins():
    """R with an explicit `feedback_in` typed edge is feedback."""
    graph = _graph_with_rails("+5V")
    r = ComponentNode(
        refdes="R43", type="resistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="+5V"),
            PagePin(number="2", role="unknown", net_label="FB_5V"),
        ],
    )
    graph.components["R43"] = r
    graph.typed_edges.append(TypedEdge(src="FB_5V", dst="R43", kind="feedback_in"))
    kind, role, _conf = classify_passive_refdes(graph, r)
    assert kind == "passive_r"
    assert role == "feedback"


def test_resistor_pull_up_signal_to_rail():
    graph = _graph_with_rails("+3V3")
    graph.nets["I2C_SDA"] = NetNode(label="I2C_SDA")
    r = ComponentNode(
        refdes="R11", type="resistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="+3V3"),
            PagePin(number="2", role="unknown", net_label="I2C_SDA"),
        ],
    )
    graph.components["R11"] = r
    _kind, role, _ = classify_passive_refdes(graph, r)
    assert role == "pull_up"


def test_resistor_series_between_rail_and_consumer():
    graph = _graph_with_rails("VIN", "LPC_VCC")
    # LPC consumer
    graph.components["U7"] = ComponentNode(
        refdes="U7", type="ic",
        pins=[PagePin(number="1", role="power_in", net_label="LPC_VCC")],
    )
    graph.power_rails["LPC_VCC"].consumers = ["U7"]
    r = ComponentNode(
        refdes="R17", type="resistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="VIN"),
            PagePin(number="2", role="unknown", net_label="LPC_VCC"),
        ],
    )
    graph.components["R17"] = r
    _kind, role, _ = classify_passive_refdes(graph, r)
    assert role == "series"


def test_resistor_unclassified_returns_damping_role():
    graph = _graph_with_rails()
    graph.nets["SIG_A"] = NetNode(label="SIG_A")
    graph.nets["SIG_B"] = NetNode(label="SIG_B")
    r = ComponentNode(
        refdes="R99", type="resistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="SIG_A"),
            PagePin(number="2", role="unknown", net_label="SIG_B"),
        ],
    )
    graph.components["R99"] = r
    kind, role, _ = classify_passive_refdes(graph, r)
    assert kind == "passive_r"
    assert role == "damping"  # both signals, no rail → damping heuristic


def test_heuristic_classifier_assigns_every_passive():
    """Whole-graph pass emits one assignment per passive refdes."""
    graph = _graph_with_rails("+5V", "+3V3")
    graph.components["R43"] = ComponentNode(
        refdes="R43", type="resistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="+5V"),
            PagePin(number="2", role="unknown", net_label="GND"),
        ],
    )
    graph.components["U1"] = ComponentNode(refdes="U1", type="ic")  # IC, not passive
    result = classify_passives_heuristic(graph)
    # Only the passive is classified.
    assert "R43" in result
    assert result["R43"][0] == "passive_r"
    assert "U1" not in result
