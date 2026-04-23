"""Tests for api.pipeline.subsystem.classify_nodes."""

from __future__ import annotations

from pathlib import Path

from api.pipeline.subsystem import classify_nodes

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "demo-pack"


def _net(id_: str, label: str) -> dict:
    return {"id": id_, "type": "net", "label": label,
            "description": "", "confidence": 0.7, "meta": {}}


def _cmp(id_: str, label: str, description: str = "") -> dict:
    return {"id": id_, "type": "component", "label": label,
            "description": description, "confidence": 0.7, "meta": {}}


def _sym(id_: str, label: str) -> dict:
    return {"id": id_, "type": "symptom", "label": label,
            "description": "", "confidence": 0.7, "meta": {}}


def _act(id_: str, label: str) -> dict:
    return {"id": id_, "type": "action", "label": label,
            "description": "", "confidence": 0.7, "meta": {}}


def _e(src: str, rel: str, tgt: str) -> dict:
    return {"source": src, "target": tgt, "relation": rel,
            "label": rel, "weight": 1.0}


def test_net_power_keyword():
    nodes = [_net("n1", "VBAT_SYS")]
    assert classify_nodes(nodes, [])["n1"] == "power"


def test_net_charge_beats_power():
    """'battery supply' contains both 'battery' (charge) and 'supply' (power).
    Charge rule MUST be listed before power so it wins."""
    nodes = [_net("n1", "battery supply")]
    assert classify_nodes(nodes, [])["n1"] == "charge"


def test_net_display_keyword():
    for label in ("HDMI_D0", "eDP lane", "LCD_BACKLIGHT", "DSI_CLK"):
        nodes = [_net("n1", label)]
        assert classify_nodes(nodes, [])["n1"] == "display", label


def test_net_usb_keyword():
    nodes = [_net("n1", "USB_D+"), _net("n2", "VBUS_5V"), _net("n3", "CC1")]
    result = classify_nodes(nodes, [])
    assert result == {"n1": "usb", "n2": "usb", "n3": "usb"}


def test_net_audio_keyword():
    nodes = [_net("n1", "I2S_BCLK"), _net("n2", "speaker_out")]
    result = classify_nodes(nodes, [])
    assert result["n1"] == "audio"
    assert result["n2"] == "audio"


def test_net_cpu_mem_keyword():
    nodes = [_net("n1", "CPU_CLK"), _net("n2", "DDR_DQ0"), _net("n3", "SPI_MOSI")]
    result = classify_nodes(nodes, [])
    assert result["n1"] == "cpu-mem"
    assert result["n2"] == "cpu-mem"
    assert result["n3"] == "cpu-mem"


def test_net_io_keyword():
    nodes = [_net("n1", "UART_TX"), _net("n2", "KEY_ROW0"), _net("n3", "LED_STATUS")]
    result = classify_nodes(nodes, [])
    assert result["n1"] == "io"
    assert result["n2"] == "io"
    assert result["n3"] == "io"


def test_net_rf_keyword():
    nodes = [_net("n1", "ANT_MAIN"), _net("n2", "PCIe_RX0"), _net("n3", "WiFi_RF")]
    result = classify_nodes(nodes, [])
    assert result["n1"] == "rf"
    assert result["n2"] == "rf"
    assert result["n3"] == "rf"


def test_net_unknown_when_no_match():
    nodes = [_net("n1", "MISC_SIGNAL_XYZ")]
    assert classify_nodes(nodes, [])["n1"] == "unknown"


def test_component_inherits_majority_from_nets():
    """U1 touches 2 power nets and 1 display net → 'power' majority."""
    nodes = [
        _cmp("c1", "U1"),
        _net("n1", "VBAT"),
        _net("n2", "V3P3"),
        _net("n3", "HDMI_D0"),
    ]
    edges = [
        _e("c1", "powers", "n1"),
        _e("c1", "powers", "n2"),
        _e("c1", "connects", "n3"),       # real schema relation (not "connected_to")
    ]
    assert classify_nodes(nodes, edges)["c1"] == "power"


def test_component_falls_back_to_description_when_no_nets():
    nodes = [_cmp("c1", "LPC controller",
                  "Embedded MCU that manages power sequencing and battery monitoring")]
    # Rule priority: charge beats power. Description contains 'battery' → charge.
    assert classify_nodes(nodes, [])["c1"] == "charge"


def test_component_unknown_when_no_nets_and_no_keywords():
    nodes = [_cmp("c1", "mystery chip", "unexplained silicon")]
    assert classify_nodes(nodes, [])["c1"] == "unknown"


def test_symptom_inherits_from_causing_component():
    nodes = [
        _cmp("c1", "U1"),
        _net("n1", "VBAT"),
        _sym("s1", "device won't boot"),
    ]
    edges = [
        _e("c1", "powers", "n1"),   # U1 → power subsystem
        _e("c1", "causes", "s1"),    # symptom caused by U1
    ]
    assert classify_nodes(nodes, edges)["s1"] == "power"


def test_action_inherits_from_resolved_component_chain():
    """Action → symptom via 'resolves'; symptom → component via 'causes' (reverse).
    Action inherits the majority subsystem of the component(s) that cause
    the symptom(s) it resolves."""
    nodes = [
        _cmp("c1", "U1"),
        _net("n1", "HDMI_CLK"),
        _sym("s1", "no display"),
        _act("a1", "Replace U1"),
    ]
    edges = [
        _e("c1", "connects", "n1"),       # U1 → display (real schema relation)
        _e("c1", "causes", "s1"),         # U1 causes symptom
        _e("a1", "resolves", "s1"),       # action resolves symptom
    ]
    result = classify_nodes(nodes, edges)
    assert result["c1"] == "display"
    assert result["s1"] == "display"
    assert result["a1"] == "display"


def test_orphan_symptom_is_unknown():
    nodes = [_sym("s1", "weird behavior")]
    assert classify_nodes(nodes, [])["s1"] == "unknown"


def test_every_node_gets_a_subsystem():
    """Contract: the returned dict has exactly one entry per node, no nulls."""
    nodes = [_cmp("c1", "foo"), _net("n1", "VBAT"), _sym("s1", "bug"),
             _act("a1", "Replace foo")]
    result = classify_nodes(nodes, [])
    assert set(result.keys()) == {"c1", "n1", "s1", "a1"}
    assert all(isinstance(v, str) and v for v in result.values())


def test_net_numbered_usb_lanes_classify_as_usb_not_display():
    """Regression: USB2_DP / USB3_RX would fall through to display (because
    of `\\bdp\\b`) or unknown unless the usb rule allows `usb\\d*`."""
    nodes = [
        _net("n1", "USB2_DP"),
        _net("n2", "USB2_DM"),
        _net("n3", "USB3_RX0"),
    ]
    result = classify_nodes(nodes, [])
    assert result == {"n1": "usb", "n2": "usb", "n3": "usb"}


def test_unrecognised_node_type_still_gets_entry():
    """Contract: if a node has an unexpected `type`, it still receives a
    subsystem entry (falls back to 'unknown')."""
    nodes = [{"id": "x1", "type": "cluster", "label": "group",
              "description": "", "confidence": 0.5, "meta": {}}]
    result = classify_nodes(nodes, [])
    assert result == {"x1": "unknown"}


def test_net_extra_power_and_display_aliases():
    """VDD / VSYS / PWR_* are common ARM/i.MX8 power rails; LVDS is a
    display-bus technology used on Reform's LCD connector."""
    nodes = [
        _net("n1", "VDD_ARM"),
        _net("n2", "VSYS"),
        _net("n3", "PWR_EN"),
        _net("n4", "LVDS_CLK"),
    ]
    result = classify_nodes(nodes, [])
    assert result == {"n1": "power", "n2": "power", "n3": "power", "n4": "display"}


def test_component_classification_uses_real_schema_relations():
    """Regression: the classifier must consume the relations literally emitted
    by the Cartographe (schemas.KnowledgeEdge.relation). `connects` is the
    generic net-adjacency relation and was previously ignored because the
    classifier looked for `connected_to` — a string that never appears in
    real packs."""
    nodes = [
        _cmp("c1", "U1"),
        _net("n1", "VBAT"),
        _net("n2", "V3P3"),
        _net("n3", "HDMI_CLK"),
    ]
    edges = [
        # All three net-adjacencies use real-schema relations (no "connected_to").
        _e("c1", "connects", "n1"),
        _e("c1", "decouples", "n2"),
        _e("c1", "measured_at", "n3"),
    ]
    # 2 power nets (VBAT, V3P3) + 1 display (HDMI_CLK) → majority = power.
    assert classify_nodes(nodes, edges)["c1"] == "power"
