# SPDX-License-Identifier: Apache-2.0
"""Tests for the `mb_schematic_graph` runtime tool.

The tool is a deterministic reader over `memory/{slug}/electrical_graph.json`
— no LLM calls, no mutation, no session state. Every test writes a synthetic
ElectricalGraph to a tmp memory root then exercises one query shape.
"""

from __future__ import annotations

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
