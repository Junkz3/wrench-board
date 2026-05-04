"""Tests for the net classifier (regex fallback + Opus wrapper).

Deterministic — we never hit Anthropic in tests; the LLM path is mocked
at `call_with_forced_tool`. Regex path is exercised directly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from api.pipeline.schematic import net_classifier
from api.pipeline.schematic.net_classifier import (
    apply_power_rail_classification,
    classify_net_regex,
    classify_nets,
    classify_nets_llm,
    classify_nets_regex,
)
from api.pipeline.schematic.schemas import (
    ClassifiedNet,
    ComponentNode,
    ElectricalGraph,
    NetClassification,
    NetNode,
    PowerRail,
    SchematicGraph,
    SchematicQualityReport,
)


def _mnt_graph() -> ElectricalGraph:
    """Small graph that exercises every domain the regex covers."""
    nets = {
        "+5V":          NetNode(label="+5V",          is_power=True),
        "+3V3":         NetNode(label="+3V3",         is_power=True),
        "GND":          NetNode(label="GND",          is_power=True, is_global=True),
        "LPC_VCC":      NetNode(label="LPC_VCC",      is_power=True),
        "5V_PWR_EN":    NetNode(label="5V_PWR_EN"),
        "3V3_PG":       NetNode(label="3V3_PG"),
        "POR_RESET":    NetNode(label="POR_RESET"),
        "CLK_32K":      NetNode(label="CLK_32K"),
        "HDMI_HPD":     NetNode(label="HDMI_HPD"),
        "TMDS_D0_P":    NetNode(label="TMDS_D0_P"),
        "USB_DP":       NetNode(label="USB_DP"),
        "PCIE1_CLK_P":  NetNode(label="PCIE1_CLK_P"),
        "RGMII_TXC":    NetNode(label="RGMII_TXC"),
        "DAC_DOUT":     NetNode(label="DAC_DOUT"),
        "EDP_BL_VCC":   NetNode(label="EDP_BL_VCC"),  # tricky — power rail for backlight
        "SD_CMD":       NetNode(label="SD_CMD"),
        "JTAG_TCK":     NetNode(label="JTAG_TCK"),
        "I2C_SDA":      NetNode(label="I2C_SDA"),
        "RANDOM_STUFF": NetNode(label="RANDOM_STUFF"),
    }
    return ElectricalGraph(
        device_slug="mnt-demo",
        components={"U1": ComponentNode(refdes="U1", type="ic")},
        nets=nets,
        power_rails={
            "+5V": PowerRail(label="+5V", voltage_nominal=5.0),
        },
        typed_edges=[],
        boot_sequence=[],
        ambiguities=[],
        quality=SchematicQualityReport(
            total_pages=1, pages_parsed=1, confidence_global=0.9,
        ),
    )


# ----------------------------------------------------------------------
# Regex classifier — per-label
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "label, expected_domain",
    [
        # Power rails
        ("+5V",            "power_rail"),
        ("+3V3",           "power_rail"),
        ("VIN",            "power_rail"),
        ("VOUT",           "power_rail"),
        ("LPC_VCC",        "power_rail"),
        ("AVDD",           "power_rail"),
        # Ground variants
        ("GND",            "ground"),
        ("AGND",           "ground"),
        ("DGND_DIGITAL",   "ground"),
        # Power sequencing
        ("5V_PWR_EN",      "power_seq"),
        ("3V3_PG",         "power_seq"),
        ("POWER_GOOD",     "power_seq"),
        # Reset — generic reset lines only. Bus-scoped resets like USB_RST
        # belong to their bus (usb/hdmi/pcie) per rule priority.
        ("POR_RESET",      "reset"),
        ("XRESET",         "reset"),
        ("USB_RST",        "usb"),       # bus prefix wins
        # Clock
        ("CLK_32K",        "clock"),
        ("XTAL_P",         "clock"),
        ("REFCLK_N",       "clock"),
        # HDMI
        ("HDMI_HPD",       "hdmi"),
        ("TMDS_D0_P",      "hdmi"),
        ("HDMI_CEC",       "hdmi"),
        # USB
        ("USB_DP",         "usb"),
        ("USB_DM",         "usb"),
        ("USB_OC",         "usb"),
        # PCIe
        ("PCIE1_CLK_P",    "pcie"),
        ("PCIE_RX_N",      "pcie"),
        # Ethernet
        ("RGMII_TXC",      "ethernet"),
        ("PHY_INT",        "ethernet"),
        # Audio
        ("DAC_DOUT",       "audio"),
        ("I2S_LRCLK",      "audio"),
        ("SPDIF_OUT",      "audio"),
        # Display
        ("EDP_BL_EN",      "display"),
        ("DSI_D0_P",       "display"),
        ("BACKLIGHT_PWM",  "display"),
        # Storage
        ("SD_CMD",         "storage"),
        ("EMMC_CLK",       "storage"),
        # Debug
        ("JTAG_TCK",       "debug"),
        ("UART_TX",        "debug"),
        ("SWDIO",          "debug"),
        # Control bus
        ("I2C_SDA",        "control"),
        ("SPI_MOSI",       "control"),
        # Fallback
        ("RANDOM_STUFF",   "misc"),
        ("",               "misc"),
    ],
)
def test_regex_classifies_known_patterns(label, expected_domain):
    assert classify_net_regex(label) == expected_domain


def test_regex_power_rail_takes_priority_over_audio():
    # AVDD could match both audio and power_rail via substring — power_rail
    # comes first in the rules.
    assert classify_net_regex("AVDD") == "power_rail"


def test_regex_debug_takes_priority_over_control():
    # JTAG lines should classify as debug even if they look buslike.
    assert classify_net_regex("JTAG_TCK") == "debug"


# ----------------------------------------------------------------------
# Regex classifier — whole graph
# ----------------------------------------------------------------------


def test_classify_nets_regex_produces_one_entry_per_net():
    graph = _mnt_graph()
    result = classify_nets_regex(graph)
    assert isinstance(result, NetClassification)
    assert set(result.nets.keys()) == set(graph.nets.keys())
    assert result.model_used == "regex"
    assert all(isinstance(c, ClassifiedNet) for c in result.nets.values())


def test_classify_nets_regex_summary_counts_match_entries():
    graph = _mnt_graph()
    result = classify_nets_regex(graph)
    total_from_summary = sum(result.domain_summary.values())
    assert total_from_summary == len(result.nets)


def test_classify_nets_regex_confidence_is_conservative():
    # Regex path should advertise a moderate confidence — downstream code
    # should know these classifications are rule-based, not LLM-verified.
    graph = _mnt_graph()
    result = classify_nets_regex(graph)
    for n in result.nets.values():
        assert 0.5 <= n.confidence <= 0.7


# ----------------------------------------------------------------------
# LLM wrapper (mocked)
# ----------------------------------------------------------------------


def _mock_llm_output() -> NetClassification:
    return NetClassification(
        device_slug="mnt-demo",
        nets={
            "HDMI_HPD": ClassifiedNet(
                label="HDMI_HPD",
                domain="hdmi",
                description="HDMI Hot Plug Detect — logic high when a monitor is connected.",
                voltage_level="3V3 logic",
                confidence=0.98,
            ),
            "+5V": ClassifiedNet(
                label="+5V",
                domain="power_rail",
                description="Main +5V rail produced by the buck regulator.",
                voltage_level="rail 5V",
                confidence=0.99,
            ),
        },
        domain_summary={"hdmi": 1, "power_rail": 1},
        ambiguities=[],
        model_used="placeholder-will-be-overridden",
    )


@pytest.mark.asyncio
async def test_classify_nets_llm_stamps_model_and_merges_with_regex_fallback():
    fake = _mock_llm_output()  # returns only HDMI_HPD + +5V
    with patch.object(
        net_classifier, "call_with_forced_tool",
        new=AsyncMock(return_value=fake),
    ) as mocked:
        result = await classify_nets_llm(
            _mnt_graph(), client=None, model="claude-opus-4-7",  # type: ignore[arg-type]
        )
    assert result.model_used == "claude-opus-4-7"
    # 19-net fixture < 1 batch; one call should have fired.
    assert mocked.await_count == 1
    kw = mocked.await_args.kwargs
    assert kw["forced_tool_name"] == "submit_net_classification"
    assert kw["output_schema"] is NetClassification
    assert kw["model"] == "claude-opus-4-7"
    # Every input net is represented in the output — the ones the mock
    # didn't classify get filled in by the regex fallback.
    assert set(result.nets.keys()) == set(_mnt_graph().nets.keys())
    # The two LLM-classified nets keep the LLM's richer data.
    assert result.nets["HDMI_HPD"].description.startswith("HDMI Hot Plug")
    assert result.nets["+5V"].confidence == 0.99
    # The regex fallbacks have lower confidence (0.5 signal).
    assert result.nets["USB_DP"].confidence == 0.5
    assert result.nets["USB_DP"].domain == "usb"


@pytest.mark.asyncio
async def test_classify_nets_default_entry_point_falls_back_to_regex_on_llm_failure():
    with patch.object(
        net_classifier, "call_with_forced_tool",
        new=AsyncMock(side_effect=RuntimeError("Opus unreachable")),
    ):
        result = await classify_nets(_mnt_graph(), client=object())  # type: ignore[arg-type]
    # Fell back to regex — every net classified, deterministic confidence.
    assert result.model_used == "regex"
    assert set(result.nets.keys()) == set(_mnt_graph().nets.keys())


@pytest.mark.asyncio
async def test_classify_nets_without_client_uses_regex_directly():
    result = await classify_nets(_mnt_graph(), client=None)
    assert result.model_used == "regex"


# ----------------------------------------------------------------------
# Schema round-trip
# ----------------------------------------------------------------------


def test_net_classification_round_trip():
    original = _mock_llm_output()
    restored = NetClassification.model_validate(original.model_dump())
    assert restored.nets["HDMI_HPD"].domain == "hdmi"
    assert restored.nets["HDMI_HPD"].confidence == 0.98


def test_classified_net_confidence_bounded():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ClassifiedNet(label="X", domain="misc", confidence=1.5)


# ----------------------------------------------------------------------
# apply_power_rail_classification — promotes LLM-classified rails back
# into SchematicGraph.is_power so compile_electrical_graph sees them.
# ----------------------------------------------------------------------


def _minimal_schematic_graph(nets: dict[str, bool]) -> SchematicGraph:
    """Build a minimal SchematicGraph with net labels and their is_power flag."""
    return SchematicGraph(
        device_slug="test-device",
        source_pdf="nowhere.pdf",
        page_count=1,
        nets={
            label: NetNode(label=label, is_power=is_power)
            for label, is_power in nets.items()
        },
    )


def _classification(entries: dict[str, tuple[str, float]]) -> NetClassification:
    """Build a NetClassification from label → (domain, confidence)."""
    return NetClassification(
        device_slug="test-device",
        nets={
            label: ClassifiedNet(
                label=label,
                domain=domain,
                description="",
                voltage_level=None,
                confidence=conf,
            )
            for label, (domain, conf) in entries.items()
        },
        domain_summary={},
        ambiguities=[],
        model_used="test",
    )


def test_apply_power_rail_classification_promotes_high_conf_rail():
    sg = _minimal_schematic_graph({"VIN": True, "PVIN": False})
    cl = _classification({
        "VIN":  ("power_rail", 0.97),
        "PVIN": ("power_rail", 0.95),
    })

    promoted = apply_power_rail_classification(sg, cl)

    assert promoted == ["PVIN"]
    assert sg.nets["PVIN"].is_power is True
    # Already-is_power nets are untouched (no-op, not re-promoted).
    assert sg.nets["VIN"].is_power is True


def test_apply_power_rail_classification_skips_low_confidence():
    sg = _minimal_schematic_graph({"MAYBE_RAIL": False})
    cl = _classification({"MAYBE_RAIL": ("power_rail", 0.6)})

    promoted = apply_power_rail_classification(sg, cl)

    assert promoted == []
    assert sg.nets["MAYBE_RAIL"].is_power is False


def test_apply_power_rail_classification_custom_threshold():
    sg = _minimal_schematic_graph({"RAIL_A": False})
    cl = _classification({"RAIL_A": ("power_rail", 0.65)})

    promoted = apply_power_rail_classification(sg, cl, min_confidence=0.6)

    assert promoted == ["RAIL_A"]
    assert sg.nets["RAIL_A"].is_power is True


def test_apply_power_rail_classification_ignores_non_rail_domains():
    sg = _minimal_schematic_graph({
        "SIG_A": False,
        "SIG_B": False,
        "GND":   False,
    })
    cl = _classification({
        "SIG_A": ("control",  0.95),
        "SIG_B": ("clock",    0.99),
        "GND":   ("ground",   0.99),
    })

    promoted = apply_power_rail_classification(sg, cl)

    assert promoted == []
    assert all(sg.nets[label].is_power is False for label in sg.nets)


def test_apply_power_rail_classification_tolerates_unknown_nets():
    """Classification may name nets absent from the schematic graph."""
    sg = _minimal_schematic_graph({"+3V3": False})
    cl = _classification({
        "+3V3":         ("power_rail", 0.99),
        "GHOST_RAIL":   ("power_rail", 0.99),  # not in graph
    })

    promoted = apply_power_rail_classification(sg, cl)

    assert promoted == ["+3V3"]
    assert sg.nets["+3V3"].is_power is True
