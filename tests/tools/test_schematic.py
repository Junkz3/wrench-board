# SPDX-License-Identifier: Apache-2.0
"""Tests for the `mb_schematic_graph` runtime tool.

The tool is a deterministic reader over `memory/{slug}/electrical_graph.json`
— no LLM calls, no mutation, no session state. Every test writes a synthetic
ElectricalGraph to a tmp memory root then exercises one query shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.pipeline.schematic.schemas import (
    BootPhase,
    ComponentNode,
    ComponentValue,
    ElectricalGraph,
    NetNode,
    PagePin,
    PowerRail,
    SchematicQualityReport,
    TypedEdge,
)
from api.tools.schematic import mb_schematic_graph

SLUG = "demo-device"


def _write_graph(memory_root: Path, graph: ElectricalGraph) -> None:
    pack_dir = memory_root / graph.device_slug
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "electrical_graph.json").write_text(graph.model_dump_json(indent=2))


@pytest.fixture
def memory_root(tmp_path: Path) -> Path:
    return tmp_path / "memory"


@pytest.fixture
def graph() -> ElectricalGraph:
    """Minimal but realistic graph: 2 rails, 4 components, 2 boot phases."""
    components = {
        "U7": ComponentNode(
            refdes="U7",
            type="ic",
            value=ComponentValue(raw="LM2677SX-5", primary="LM2677SX-5", mpn="LM2677SX-5"),
            pages=[1],
            pins=[
                PagePin(number="1", name="VIN", role="power_in", net_label="24V_IN"),
                PagePin(number="2", name="EN", role="enable_in", net_label="5V_PWR_EN"),
                PagePin(number="3", name="SW", role="switch_node", net_label="+5V"),
                PagePin(number="4", name="GND", role="ground", net_label="GND"),
            ],
        ),
        "U1": ComponentNode(
            refdes="U1",
            type="ic",
            value=ComponentValue(raw="SoC_Foo"),
            pages=[1],
            pins=[
                PagePin(number="1", name="5V", role="power_in", net_label="+5V"),
                PagePin(number="2", name="3V3_OUT", role="power_out", net_label="+3V3"),
            ],
        ),
        "U3": ComponentNode(
            refdes="U3",
            type="ic",
            value=ComponentValue(raw="SDRAM"),
            pages=[2],
            pins=[
                PagePin(number="1", name="VDD", role="power_in", net_label="+3V3"),
            ],
        ),
        "C18": ComponentNode(
            refdes="C18",
            type="capacitor",
            value=ComponentValue(raw="100nF", primary="100nF"),
            pages=[1],
            pins=[
                PagePin(number="1", role="terminal", net_label="+5V"),
                PagePin(number="2", role="ground", net_label="GND"),
            ],
        ),
    }

    nets = {
        "+5V": NetNode(label="+5V", is_power=True, pages=[1], connects=["U7.3", "U1.1", "C18.1"]),
        "+3V3": NetNode(label="+3V3", is_power=True, pages=[1, 2], connects=["U1.2", "U3.1"]),
        "GND": NetNode(label="GND", is_power=True, is_global=True, pages=[1, 2]),
        "24V_IN": NetNode(label="24V_IN", is_power=True, pages=[1]),
        "5V_PWR_EN": NetNode(label="5V_PWR_EN", pages=[1]),
    }

    power_rails = {
        "24V_IN": PowerRail(
            label="24V_IN",
            voltage_nominal=24.0,
            source_refdes=None,
            source_type="external",
            consumers=["U7"],
        ),
        "+5V": PowerRail(
            label="+5V",
            voltage_nominal=5.0,
            source_refdes="U7",
            source_type="buck",
            enable_net="5V_PWR_EN",
            consumers=["U1", "C18"],
            decoupling=["C18"],
        ),
        "+3V3": PowerRail(
            label="+3V3",
            voltage_nominal=3.3,
            source_refdes="U1",
            source_type="ldo",
            consumers=["U3"],
        ),
    }

    boot_sequence = [
        BootPhase(
            index=1,
            name="PHASE 1 — always-on",
            rails_stable=["24V_IN"],
            components_entering=["U7"],
            triggers_next=["5V_PWR_EN"],
        ),
        BootPhase(
            index=2,
            name="PHASE 2 — 5V up",
            rails_stable=["+5V"],
            components_entering=["U1"],
            triggers_next=[],
        ),
        BootPhase(
            index=3,
            name="PHASE 3 — 3V3 up",
            rails_stable=["+3V3"],
            components_entering=["U3"],
            triggers_next=[],
        ),
    ]

    return ElectricalGraph(
        device_slug=SLUG,
        components=components,
        nets=nets,
        power_rails=power_rails,
        typed_edges=[
            TypedEdge(src="U7", dst="+5V", kind="powers"),
            TypedEdge(src="5V_PWR_EN", dst="U7", kind="enables"),
            TypedEdge(src="U1", dst="+3V3", kind="powers"),
        ],
        boot_sequence=boot_sequence,
        quality=SchematicQualityReport(
            total_pages=2,
            pages_parsed=2,
            confidence_global=0.95,
        ),
    )


# ----------------------------------------------------------------------
# query="rail"
# ----------------------------------------------------------------------


def test_rail_happy_path(memory_root, graph):
    _write_graph(memory_root, graph)
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="rail", label="+5V"
    )
    assert r["found"] is True
    assert r["query"] == "rail"
    assert r["label"] == "+5V"
    assert r["voltage_nominal"] == 5.0
    assert r["source_refdes"] == "U7"
    assert r["enable_net"] == "5V_PWR_EN"
    assert r["consumers"] == ["U1", "C18"]
    assert r["decoupling"] == ["C18"]
    assert r["boot_phase"] == 2  # +5V stabilises in PHASE 2


def test_rail_unknown_returns_closest_matches(memory_root, graph):
    _write_graph(memory_root, graph)
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="rail", label="+5v"
    )
    assert r["found"] is False
    assert r["reason"] == "unknown_rail"
    assert "+5V" in r["closest_matches"]


def test_rail_missing_label(memory_root, graph):
    _write_graph(memory_root, graph)
    r = mb_schematic_graph(device_slug=SLUG, memory_root=memory_root, query="rail")
    assert r["found"] is False
    assert r["reason"] == "missing_parameter"
    assert "label" in r["hint"]


# ----------------------------------------------------------------------
# query="component"
# ----------------------------------------------------------------------


def test_component_happy_path(memory_root, graph):
    _write_graph(memory_root, graph)
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="component", refdes="U7"
    )
    assert r["found"] is True
    assert r["refdes"] == "U7"
    assert r["type"] == "ic"
    assert r["value"]["raw"] == "LM2677SX-5"
    assert r["pages"] == [1]
    assert len(r["pins"]) == 4
    assert r["rails_produced"] == ["+5V"]
    assert "24V_IN" in r["rails_consumed"]
    assert r["populated"] is True
    assert r["boot_phase"] == 1  # U7 enters in PHASE 1


def test_component_consumer_only(memory_root, graph):
    _write_graph(memory_root, graph)
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="component", refdes="U3"
    )
    assert r["found"] is True
    assert r["rails_produced"] == []
    assert r["rails_consumed"] == ["+3V3"]


def test_component_unknown_returns_closest_matches(memory_root, graph):
    _write_graph(memory_root, graph)
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="component", refdes="U77"
    )
    assert r["found"] is False
    assert r["reason"] == "unknown_component"
    # same-prefix candidates come back
    assert any(c.startswith("U") for c in r["closest_matches"])


def test_component_missing_refdes(memory_root, graph):
    _write_graph(memory_root, graph)
    r = mb_schematic_graph(device_slug=SLUG, memory_root=memory_root, query="component")
    assert r["found"] is False
    assert r["reason"] == "missing_parameter"


# ----------------------------------------------------------------------
# query="downstream"
# ----------------------------------------------------------------------


def test_downstream_of_rail_source(memory_root, graph):
    _write_graph(memory_root, graph)
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="downstream", refdes="U7"
    )
    assert r["found"] is True
    assert r["refdes"] == "U7"
    # U7 produces +5V directly
    assert "+5V" in r["rails_direct"]
    # Direct consumers of +5V
    assert set(r["components_direct"]) == {"U1", "C18"}
    # Transitive: U1 also produces +3V3, so U3 loses power too
    assert "U3" in r["components_transitive"]
    assert "+3V3" in r["rails_transitive"]


def test_downstream_leaf_component(memory_root, graph):
    _write_graph(memory_root, graph)
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="downstream", refdes="U3"
    )
    assert r["found"] is True
    # U3 produces nothing, so no dependents
    assert r["rails_direct"] == []
    assert r["components_direct"] == []


def test_downstream_unknown_refdes(memory_root, graph):
    _write_graph(memory_root, graph)
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="downstream", refdes="U99"
    )
    assert r["found"] is False
    assert r["reason"] == "unknown_component"


# ----------------------------------------------------------------------
# query="boot_phase"
# ----------------------------------------------------------------------


def test_boot_phase_happy_path(memory_root, graph):
    _write_graph(memory_root, graph)
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="boot_phase", index=2
    )
    assert r["found"] is True
    assert r["index"] == 2
    assert r["name"] == "PHASE 2 — 5V up"
    assert r["rails_stable"] == ["+5V"]
    assert r["components_entering"] == ["U1"]
    assert r["total_phases"] == 3


def test_boot_phase_out_of_range(memory_root, graph):
    _write_graph(memory_root, graph)
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="boot_phase", index=99
    )
    assert r["found"] is False
    assert r["reason"] == "unknown_phase"
    assert r["total_phases"] == 3


# ----------------------------------------------------------------------
# query="list_rails" / "list_boot"
# ----------------------------------------------------------------------


def test_list_rails_brief(memory_root, graph):
    _write_graph(memory_root, graph)
    r = mb_schematic_graph(device_slug=SLUG, memory_root=memory_root, query="list_rails")
    assert r["found"] is True
    assert r["count"] == 3
    labels = {entry["label"] for entry in r["rails"]}
    assert labels == {"24V_IN", "+5V", "+3V3"}
    for entry in r["rails"]:
        assert "consumer_count" in entry
        assert "voltage_nominal" in entry


def test_list_boot_brief(memory_root, graph):
    _write_graph(memory_root, graph)
    r = mb_schematic_graph(device_slug=SLUG, memory_root=memory_root, query="list_boot")
    assert r["found"] is True
    assert r["count"] == 3
    indexes = [p["index"] for p in r["phases"]]
    assert indexes == [1, 2, 3]


# ----------------------------------------------------------------------
# error cases
# ----------------------------------------------------------------------


def test_invalid_query(memory_root, graph):
    _write_graph(memory_root, graph)
    r = mb_schematic_graph(device_slug=SLUG, memory_root=memory_root, query="nonsense")
    assert r["found"] is False
    assert r["reason"] == "invalid_query"
    assert "rail" in r["valid_queries"]
    assert "component" in r["valid_queries"]


def test_no_electrical_graph_on_disk(memory_root):
    # don't write anything
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="rail", label="+5V"
    )
    assert r["found"] is False
    assert r["reason"] == "no_schematic_graph"


def test_malformed_electrical_graph(memory_root):
    pack_dir = memory_root / SLUG
    pack_dir.mkdir(parents=True)
    (pack_dir / "electrical_graph.json").write_text("{not json")
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="rail", label="+5V"
    )
    assert r["found"] is False
    assert r["reason"] == "malformed_graph"


# ----------------------------------------------------------------------
# Analyzer preference — when boot_sequence_analyzed.json is on disk,
# the tool surfaces analyzer phases with kind/evidence/confidence fields.
# ----------------------------------------------------------------------


def _write_analyzed(memory_root, slug=SLUG):
    payload = {
        "schema_version": "1.0",
        "device_slug": slug,
        "phases": [
            {
                "index": 0, "name": "Always-on standby", "kind": "always-on",
                "rails_stable": ["+3V3_STANDBY"],
                "components_entering": ["U14"],
                "triggers_next": [],
                "evidence": ["designer note p4 U14: 'Standby always-on 3V3 power rail'"],
                "confidence": 0.95,
            },
            {
                "index": 1, "name": "LPC asserts main", "kind": "sequenced",
                "rails_stable": ["+5V"],
                "components_entering": ["U7"],
                "triggers_next": [{
                    "net_label": "5V_PWR_EN", "from_refdes": "LPC",
                    "rationale": "LPC drives 5V_PWR_EN",
                }],
                "evidence": ["edge: 5V_PWR_EN enables U7"],
                "confidence": 0.9,
            },
        ],
        "sequencer_refdes": "LPC",
        "global_confidence": 0.92,
        "ambiguities": [],
        "model_used": "claude-opus-4-7",
    }
    (memory_root / slug / "boot_sequence_analyzed.json").write_text(
        json.dumps(payload, indent=2)
    )


def test_boot_phase_prefers_analyzer_when_present(memory_root, graph):
    _write_graph(memory_root, graph)
    _write_analyzed(memory_root)
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="boot_phase", index=1
    )
    assert r["found"] is True
    assert r["source"] == "analyzer"
    assert r["kind"] == "sequenced"
    assert r["confidence"] == 0.9
    assert r["evidence"] == ["edge: 5V_PWR_EN enables U7"]


def test_boot_phase_falls_back_to_compiler_without_analyzer(memory_root, graph):
    _write_graph(memory_root, graph)
    # no analyzed file written
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="boot_phase", index=1
    )
    assert r["found"] is True
    assert r["source"] == "compiler"
    assert "kind" not in r          # analyzer-only field absent
    assert "evidence" not in r


def test_critical_path_ranks_spofs(memory_root, graph):
    _write_graph(memory_root, graph)
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="critical_path"
    )
    assert r["found"] is True
    assert r["query"] == "critical_path"
    # Root rail 24V_IN has the biggest cascade (U7 → +5V → U1 → +3V3 → U3 + C18).
    top = r["top_spofs"][0]
    assert top["label"] == "24V_IN"
    assert top["blast_radius"] >= 5
    assert top["impact_pct"] > 0
    # U7 should also rank high (produces +5V which is the cascade spine).
    labels = [s["label"] for s in r["top_spofs"][:5]]
    assert "U7" in labels
    assert "+5V" in labels
    assert r["total_nodes"] > 0


def test_critical_path_per_phase_surfaces_gate(memory_root, graph):
    _write_graph(memory_root, graph)
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="critical_path"
    )
    phases = r["per_phase"]
    assert len(phases) == 3
    # Phase 1 enters U7 — it should dominate that phase's critical list.
    phase1 = next(p for p in phases if p["index"] == 1)
    labels = [c["label"] for c in phase1["critical"]]
    assert "U7" in labels


def test_critical_path_works_without_analyzer(memory_root, graph):
    _write_graph(memory_root, graph)
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="critical_path"
    )
    assert r["source"] == "compiler"


def test_critical_path_uses_analyzer_phases_when_present(memory_root, graph):
    _write_graph(memory_root, graph)
    _write_analyzed(memory_root)
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="critical_path"
    )
    assert r["source"] == "analyzer"
    # Analyzer has phases with index 0 + 1 (different shape) — make sure per_phase tracks them
    indexes = [p["index"] for p in r["per_phase"]]
    assert indexes == [0, 1]


def _write_classified_nets(memory_root, slug=SLUG):
    payload = {
        "schema_version": "1.0",
        "device_slug": slug,
        "nets": {
            "+5V": {
                "label": "+5V", "domain": "power_rail",
                "description": "Main 5V rail.", "voltage_level": "rail 5V",
                "confidence": 0.98,
            },
            "+3V3": {
                "label": "+3V3", "domain": "power_rail",
                "description": "3V3 rail.", "voltage_level": "rail 3V3",
                "confidence": 0.98,
            },
            "5V_PWR_EN": {
                "label": "5V_PWR_EN", "domain": "power_seq",
                "description": "Enable line for +5V regulator from LPC.",
                "voltage_level": "3V3 logic", "confidence": 0.92,
            },
            "GND": {
                "label": "GND", "domain": "ground",
                "description": "Main ground.", "voltage_level": None,
                "confidence": 1.0,
            },
            "24V_IN": {
                "label": "24V_IN", "domain": "power_rail",
                "description": "Input 24V from barrel jack.",
                "voltage_level": "rail 24V", "confidence": 0.95,
            },
        },
        "domain_summary": {"power_rail": 3, "power_seq": 1, "ground": 1},
        "ambiguities": [],
        "model_used": "claude-opus-4-7",
    }
    (memory_root / slug / "nets_classified.json").write_text(
        json.dumps(payload, indent=2)
    )


def test_net_query_surfaces_classification(memory_root, graph):
    _write_graph(memory_root, graph)
    _write_classified_nets(memory_root)
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="net", label="+5V"
    )
    assert r["found"] is True
    assert r["domain"] == "power_rail"
    assert r["description"] == "Main 5V rail."
    assert r["voltage_level"] == "rail 5V"
    # Touching components: pins with net_label="+5V" in the fixture — U7, U1, C18.
    assert "U7" in r["touching_components"]
    assert "U1" in r["touching_components"]
    assert "C18" in r["touching_components"]


def test_net_query_unknown_net(memory_root, graph):
    _write_graph(memory_root, graph)
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="net", label="NOWHERE"
    )
    assert r["found"] is False
    assert r["reason"] == "unknown_net"


def test_net_query_missing_label(memory_root, graph):
    _write_graph(memory_root, graph)
    r = mb_schematic_graph(device_slug=SLUG, memory_root=memory_root, query="net")
    assert r["found"] is False
    assert r["reason"] == "missing_parameter"


def test_net_domain_lists_nets_and_ranks_components(memory_root, graph):
    _write_graph(memory_root, graph)
    _write_classified_nets(memory_root)
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="net_domain", domain="power_rail"
    )
    assert r["found"] is True
    assert r["domain"] == "power_rail"
    assert set(r["nets"]) == {"+5V", "+3V3", "24V_IN"}
    # Ranked components: each has touch_count (>=1), type, blast_radius.
    assert len(r["components_ranked"]) > 0
    for entry in r["components_ranked"]:
        assert "refdes" in entry
        assert entry["touch_count"] >= 1
    # suspects_top_3 is a short list.
    assert len(r["suspects_top_3"]) <= 3


def test_net_domain_without_classification_returns_hint(memory_root, graph):
    _write_graph(memory_root, graph)
    # no nets_classified.json on disk
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="net_domain", domain="hdmi"
    )
    assert r["found"] is False
    assert r["reason"] == "no_classification"


def test_net_domain_unknown_domain_returns_available_list(memory_root, graph):
    _write_graph(memory_root, graph)
    _write_classified_nets(memory_root)
    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="net_domain", domain="hdmi"
    )
    assert r["found"] is False
    assert r["reason"] == "unknown_domain"
    assert "power_rail" in r["available_domains"]


def test_net_domain_catches_cross_classified_nets_via_prefix(memory_root, graph):
    """USB_PWR classified as power_rail should still surface under domain=usb."""
    _write_graph(memory_root, graph)
    # Build a classified nets file where some USB-related nets are classified
    # as power_rail (Sonnet's correct-but-structural choice).
    payload = {
        "schema_version": "1.0",
        "device_slug": SLUG,
        "nets": {
            "USB_DP": {"label": "USB_DP", "domain": "usb",
                       "description": "USB data line", "voltage_level": "differential",
                       "confidence": 0.95},
            "USB_PWR": {"label": "USB_PWR", "domain": "power_rail",
                        "description": "USB 5V power rail",
                        "voltage_level": "rail 5V", "confidence": 0.95},
            "USBH_3V3": {"label": "USBH_3V3", "domain": "power_rail",
                         "description": "USB hub 3V3 supply",
                         "voltage_level": "rail 3V3", "confidence": 0.95},
        },
        "domain_summary": {"usb": 1, "power_rail": 2},
        "ambiguities": [],
        "model_used": "claude-sonnet-4-6",
    }
    (memory_root / SLUG / "nets_classified.json").write_text(
        json.dumps(payload, indent=2)
    )
    # Also add those labels to the graph.nets so they exist.
    graph_json = json.loads((memory_root / SLUG / "electrical_graph.json").read_text())
    for label in ("USB_DP", "USB_PWR", "USBH_3V3"):
        graph_json["nets"][label] = {
            "label": label, "is_power": "PWR" in label or "3V3" in label,
            "is_global": False, "pages": [], "connects": [],
        }
    (memory_root / SLUG / "electrical_graph.json").write_text(json.dumps(graph_json))

    r = mb_schematic_graph(
        device_slug=SLUG, memory_root=memory_root, query="net_domain", domain="usb"
    )
    assert r["found"] is True
    # USB_PWR and USBH_3V3 must be included even though their classified
    # domain is power_rail.
    assert set(r["nets"]) == {"USB_DP", "USB_PWR", "USBH_3V3"}


def test_net_domain_missing_param(memory_root, graph):
    _write_graph(memory_root, graph)
    _write_classified_nets(memory_root)
    r = mb_schematic_graph(device_slug=SLUG, memory_root=memory_root, query="net_domain")
    assert r["found"] is False
    assert r["reason"] == "missing_parameter"


# ----------------------------------------------------------------------
# query="simulate"
# ----------------------------------------------------------------------


def test_simulate_query_returns_compact_timeline(memory_root: Path, graph: ElectricalGraph):
    _write_graph(memory_root, graph)
    result = mb_schematic_graph(
        device_slug=SLUG,
        memory_root=memory_root,
        query="simulate",
    )
    assert result["found"] is True
    assert result["query"] == "simulate"
    assert result["killed_refdes"] == []
    assert result["final_verdict"] in ("completed", "cascade", "blocked")
    # Compact — no full per-phase state dump, just verdict + counts.
    assert "states" not in result
    assert "phase_count" in result


def test_simulate_query_with_killed_refdes_reports_blockage(
    memory_root: Path, graph: ElectricalGraph
):
    _write_graph(memory_root, graph)
    # Any rail source in the graph fixture will do — pick the first.
    source = next(
        (r.source_refdes for r in graph.power_rails.values() if r.source_refdes),
        None,
    )
    assert source is not None
    result = mb_schematic_graph(
        device_slug=SLUG,
        memory_root=memory_root,
        query="simulate",
        killed_refdes=[source],
    )
    assert result["found"] is True
    assert result["killed_refdes"] == [source]
    assert result["final_verdict"] in ("blocked", "cascade")


def test_simulate_query_unknown_refdes_rejected(memory_root: Path, graph: ElectricalGraph):
    _write_graph(memory_root, graph)
    result = mb_schematic_graph(
        device_slug=SLUG,
        memory_root=memory_root,
        query="simulate",
        killed_refdes=["Z999"],
    )
    assert result["found"] is False
    assert result["reason"] == "unknown_refdes"
    assert "Z999" in result["invalid_refdes"]


def test_list_boot_surfaces_analyzer_meta(memory_root, graph):
    _write_graph(memory_root, graph)
    _write_analyzed(memory_root)
    r = mb_schematic_graph(device_slug=SLUG, memory_root=memory_root, query="list_boot")
    assert r["source"] == "analyzer"
    assert r["analyzer_meta"]["sequencer_refdes"] == "LPC"
    assert r["analyzer_meta"]["model_used"] == "claude-opus-4-7"
    # The brief phases still carry kind + confidence when available.
    assert any("kind" in p for p in r["phases"])
