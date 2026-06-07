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
    ComponentNode,
    ElectricalGraph,
    NetNode,
    PagePin,
    PowerRail,
    SchematicQualityReport,
    TypedEdge,
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


# --------- capacitors ---------

def test_capacitor_decoupling_explicit_edge():
    graph = _graph_with_rails("+3V3")
    graph.nets["GND"] = NetNode(label="GND", is_global=True)
    c = ComponentNode(
        refdes="C156", type="capacitor",
        pins=[
            PagePin(number="1", role="unknown", net_label="+3V3"),
            PagePin(number="2", role="unknown", net_label="GND"),
        ],
    )
    graph.components["C156"] = c
    graph.typed_edges.append(TypedEdge(src="+3V3", dst="C156", kind="decouples"))
    kind, role, _ = classify_passive_refdes(graph, c)
    assert kind == "passive_c"
    assert role == "decoupling"


def test_capacitor_rail_to_gnd_heuristic_decoupling():
    """Without an explicit edge, a rail-to-GND cap still counts as decoupling
    when a consumer IC sits on the same rail."""
    graph = _graph_with_rails("+3V3")
    graph.nets["GND"] = NetNode(label="GND")
    graph.components["U7"] = ComponentNode(
        refdes="U7", type="ic",
        pins=[PagePin(number="1", role="power_in", net_label="+3V3")],
    )
    graph.power_rails["+3V3"].consumers = ["U7"]
    c = ComponentNode(
        refdes="C29", type="capacitor",
        pins=[
            PagePin(number="1", role="unknown", net_label="+3V3"),
            PagePin(number="2", role="unknown", net_label="GND"),
        ],
    )
    graph.components["C29"] = c
    kind, role, _ = classify_passive_refdes(graph, c)
    assert kind == "passive_c"
    assert role == "decoupling"


def test_capacitor_signal_to_signal_is_ac_coupling():
    graph = _graph_with_rails()
    graph.nets["AUDIO_L"] = NetNode(label="AUDIO_L")
    graph.nets["AUDIO_L_AC"] = NetNode(label="AUDIO_L_AC")
    c = ComponentNode(
        refdes="C77", type="capacitor",
        pins=[
            PagePin(number="1", role="unknown", net_label="AUDIO_L"),
            PagePin(number="2", role="unknown", net_label="AUDIO_L_AC"),
        ],
    )
    graph.components["C77"] = c
    kind, role, _ = classify_passive_refdes(graph, c)
    assert kind == "passive_c"
    assert role == "ac_coupling"


# --------- diodes ---------

def test_diode_flyback_edge_wins():
    graph = _graph_with_rails()
    d = ComponentNode(
        refdes="D5", type="diode",
        pins=[
            PagePin(number="1", role="unknown", net_label="SW_NODE"),
            PagePin(number="2", role="unknown", net_label="VBAT"),
        ],
    )
    graph.components["D5"] = d
    # Flyback convention — cathode on the inductor output, anode on return.
    # We detect it via the presence of an inductor across the same nets.
    graph.components["L2"] = ComponentNode(
        refdes="L2", type="inductor",
        pins=[
            PagePin(number="1", role="unknown", net_label="SW_NODE"),
            PagePin(number="2", role="unknown", net_label="VBAT"),
        ],
    )
    kind, role, _ = classify_passive_refdes(graph, d)
    assert kind == "passive_d"
    assert role == "flyback"


def test_diode_signal_to_gnd_is_esd():
    graph = _graph_with_rails()
    graph.nets["USB_DP"] = NetNode(label="USB_DP")
    graph.nets["GND"] = NetNode(label="GND")
    d = ComponentNode(
        refdes="D9", type="diode",
        pins=[
            PagePin(number="1", role="unknown", net_label="USB_DP"),
            PagePin(number="2", role="unknown", net_label="GND"),
        ],
    )
    graph.components["D9"] = d
    kind, role, _ = classify_passive_refdes(graph, d)
    assert kind == "passive_d"
    assert role == "esd"


# --------- ferrites ---------

def test_ferrite_between_rail_and_variant_is_filter():
    graph = _graph_with_rails("+3V3", "+3V3_AUDIO")
    fb = ComponentNode(
        refdes="FB2", type="ferrite",
        pins=[
            PagePin(number="1", role="unknown", net_label="+3V3"),
            PagePin(number="2", role="unknown", net_label="+3V3_AUDIO"),
        ],
    )
    graph.components["FB2"] = fb
    kind, role, _ = classify_passive_refdes(graph, fb)
    assert kind == "passive_fb"
    assert role == "filter"


# --------- transistors ---------

def test_transistor_load_switch_heuristic():
    """Q with upstream rail pin + downstream rail pin + gate on EN-labelled
    net = load_switch."""
    graph = _graph_with_rails("+5V", "+3V3_USB")
    graph.nets["5V_PWR_EN"] = NetNode(label="5V_PWR_EN")
    q = ComponentNode(
        refdes="Q5", type="transistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="+5V"),
            PagePin(number="2", role="unknown", net_label="+3V3_USB"),
            PagePin(number="3", role="unknown", net_label="5V_PWR_EN"),
        ],
    )
    graph.components["Q5"] = q
    kind, role, _conf = classify_passive_refdes(graph, q)
    assert kind == "passive_q"
    assert role == "load_switch"


def test_transistor_level_shifter_heuristic():
    """Q between two signal nets in different voltage domains = level_shifter."""
    graph = _graph_with_rails("+3V3", "+1V8")
    graph.nets["I2C1_3V3_SDA"] = NetNode(label="I2C1_3V3_SDA")
    graph.nets["I2C1_1V8_SDA"] = NetNode(label="I2C1_1V8_SDA")
    q = ComponentNode(
        refdes="Q2", type="transistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="I2C1_3V3_SDA"),
            PagePin(number="2", role="unknown", net_label="I2C1_1V8_SDA"),
            PagePin(number="3", role="unknown", net_label="+3V3"),
        ],
    )
    graph.components["Q2"] = q
    _kind, role, _ = classify_passive_refdes(graph, q)
    assert role == "level_shifter"


def test_transistor_inrush_limiter_heuristic():
    """Q in series from VIN to a regulator input, gate on RC soft-start."""
    graph = _graph_with_rails("VIN", "VIN_BUCK")
    graph.nets["SOFT_START"] = NetNode(label="SOFT_START")
    graph.components["U20"] = ComponentNode(
        refdes="U20", type="ic",
        pins=[PagePin(number="1", role="power_in", net_label="VIN_BUCK")],
    )
    graph.power_rails["VIN_BUCK"].consumers = ["U20"]
    q = ComponentNode(
        refdes="Q1", type="transistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="VIN"),
            PagePin(number="2", role="unknown", net_label="VIN_BUCK"),
            PagePin(number="3", role="unknown", net_label="SOFT_START"),
        ],
    )
    graph.components["Q1"] = q
    _kind, role, _ = classify_passive_refdes(graph, q)
    assert role == "inrush_limiter"


def test_transistor_unclassified_returns_none_role():
    """Q with no rail pins and no distinctive topology stays role=None."""
    graph = _graph_with_rails()
    graph.nets["RANDOM_A"] = NetNode(label="RANDOM_A")
    graph.nets["RANDOM_B"] = NetNode(label="RANDOM_B")
    q = ComponentNode(
        refdes="Q99", type="transistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="RANDOM_A"),
            PagePin(number="2", role="unknown", net_label="RANDOM_B"),
        ],
    )
    graph.components["Q99"] = q
    kind, role, _ = classify_passive_refdes(graph, q)
    assert kind == "passive_q"
    assert role is None


def test_heuristic_emits_passive_q_entry_in_whole_graph_pass():
    graph = _graph_with_rails("+5V", "+3V3_USB")
    graph.nets["EN_5V"] = NetNode(label="EN_5V")
    graph.components["Q5"] = ComponentNode(
        refdes="Q5", type="transistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="+5V"),
            PagePin(number="2", role="unknown", net_label="+3V3_USB"),
            PagePin(number="3", role="unknown", net_label="EN_5V"),
        ],
    )
    result = classify_passives_heuristic(graph)
    assert "Q5" in result
    assert result["Q5"][0] == "passive_q"


def test_transistor_flyback_switch_heuristic():
    """Q with pin on SW switching node + GND/PVIN = flyback_switch."""
    graph = _graph_with_rails("PVIN")
    graph.nets["SW1"] = NetNode(label="SW1")
    graph.nets["PGND"] = NetNode(label="PGND")
    q = ComponentNode(
        refdes="Q1", type="transistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="SW1"),
            PagePin(number="2", role="unknown", net_label="PGND"),
            PagePin(number="3", role="unknown", net_label="GATE_Q1"),
        ],
    )
    graph.components["Q1"] = q
    graph.nets["GATE_Q1"] = NetNode(label="GATE_Q1")
    _kind, role, _ = classify_passive_refdes(graph, q)
    assert role == "flyback_switch"


def test_transistor_flyback_switch_wins_over_load_switch():
    """When both patterns match, flyback_switch takes priority (it's the more
    specific topology — SW node is a strong signal)."""
    graph = _graph_with_rails("PVIN", "VOUT")
    graph.nets["SW1"] = NetNode(label="SW1")
    graph.nets["EN_SMPS"] = NetNode(label="EN_SMPS")
    q = ComponentNode(
        refdes="Q15", type="transistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="PVIN"),
            PagePin(number="2", role="unknown", net_label="SW1"),
            PagePin(number="3", role="unknown", net_label="EN_SMPS"),
        ],
    )
    graph.components["Q15"] = q
    _kind, role, _ = classify_passive_refdes(graph, q)
    # Even though we have 2 rails + EN-labelled net (load_switch signature),
    # the SW node presence wins — this is a flyback switch.
    assert role == "flyback_switch"


# --------- Q BMS roles (Phase 4.6) ---------

def test_transistor_cell_protection_heuristic():
    """Q with 2 distinct BAT-family nets, no GND pin = cell_protection."""
    graph = _graph_with_rails("BAT1", "BAT1FUSED")
    q = ComponentNode(
        refdes="Q5", type="transistor",
        pins=[
            PagePin(number="1", role="signal_in", net_label=None),
            PagePin(number="2", role="signal_in", net_label="BAT1FUSED"),
            PagePin(number="3", role="signal_out", net_label="BAT1"),
        ],
    )
    graph.components["Q5"] = q
    kind, role, conf = classify_passive_refdes(graph, q)
    assert kind == "passive_q"
    assert role == "cell_protection"
    assert conf == 0.75


def test_transistor_cell_protection_rejects_with_gnd():
    """Q with BAT+BAT+GND pattern falls through — grounded Qs aren't series-protection."""
    graph = _graph_with_rails("BAT1", "BAT1FUSED")
    graph.nets["GND"] = NetNode(label="GND", is_power=True, is_global=True)
    q = ComponentNode(
        refdes="Q5", type="transistor",
        pins=[
            PagePin(number="1", role="signal_in", net_label="GND"),
            PagePin(number="2", role="signal_in", net_label="BAT1FUSED"),
            PagePin(number="3", role="signal_out", net_label="BAT1"),
        ],
    )
    graph.components["Q5"] = q
    _kind, role, _ = classify_passive_refdes(graph, q)
    assert role != "cell_protection"


def test_transistor_cell_balancer_heuristic():
    """Q with exactly one BAT-family net repeated on 2+ pins = cell_balancer
    (vision-merged bleed resistor artefact)."""
    graph = _graph_with_rails("BAT2")
    q = ComponentNode(
        refdes="Q6", type="transistor",
        pins=[
            PagePin(number="1", role="signal_in", net_label=None),
            PagePin(number="2", role="signal_in", net_label="BAT2"),
            PagePin(number="3", role="signal_out", net_label="BAT2"),
        ],
    )
    graph.components["Q6"] = q
    _kind, role, conf = classify_passive_refdes(graph, q)
    assert role == "cell_balancer"
    assert conf == 0.65


def test_transistor_cell_balancer_rejects_foreign_net():
    """Q with BAT+BAT+EN falls through — a foreign control net means it's not a pure balancer."""
    graph = _graph_with_rails("BAT2")
    graph.nets["EN_BMS"] = NetNode(label="EN_BMS")
    q = ComponentNode(
        refdes="Q6", type="transistor",
        pins=[
            PagePin(number="1", role="signal_in", net_label="EN_BMS"),
            PagePin(number="2", role="signal_in", net_label="BAT2"),
            PagePin(number="3", role="signal_out", net_label="BAT2"),
        ],
    )
    graph.components["Q6"] = q
    _kind, role, _ = classify_passive_refdes(graph, q)
    assert role != "cell_balancer"


def test_cell_protection_priority_over_inrush_limiter():
    """Q with 2 BAT-family rails must resolve to cell_protection, NOT inrush_limiter.
    The inrush rule fires on any VIN/BAT-substring rail — without priority, Q5 would
    mis-classify because its rails both carry 'BAT'."""
    graph = _graph_with_rails("BAT", "BATFUSED")
    graph.components["U_BMS"] = ComponentNode(
        refdes="U_BMS", type="ic",
        pins=[PagePin(number="1", role="power_in", net_label="BATFUSED")],
    )
    graph.power_rails["BATFUSED"].consumers = ["U_BMS"]
    q = ComponentNode(
        refdes="Q5", type="transistor",
        pins=[
            PagePin(number="1", role="signal_in", net_label=None),
            PagePin(number="2", role="signal_in", net_label="BAT"),
            PagePin(number="3", role="signal_out", net_label="BATFUSED"),
        ],
    )
    graph.components["Q5"] = q
    _kind, role, _ = classify_passive_refdes(graph, q)
    assert role == "cell_protection"


@pytest.mark.parametrize("label,should_match", [
    # Accepted
    ("BAT",         True),
    ("BAT1",        True),
    ("BAT8",        True),
    ("BAT1FUSED",   True),
    ("BAT_PROT",    False),  # underscore prefix disallowed — regex has optional suffix without underscore
    ("VBAT",        True),
    ("VBAT1",       True),
    ("CHGBAT",      True),
    ("CELL1",       True),
    ("BATPACK",     True),
    # Rejected
    ("CR1220",      False),  # coin cell RTC, not pack
    ("PVIN",        False),
    ("VIN",         False),
    ("BATRANDOM",   False),  # suffix not in allowlist
    ("+3V3",        False),
    ("GND",         False),
])
def test_bat_family_pattern_coverage(label, should_match):
    from api.pipeline.schematic.passive_classifier import _BAT_FAMILY_PATTERN
    matched = _BAT_FAMILY_PATTERN.match(label) is not None
    assert matched is should_match, f"{label!r} match={matched} expected={should_match}"
