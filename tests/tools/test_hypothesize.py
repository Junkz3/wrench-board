# SPDX-License-Identifier: Apache-2.0
"""Tests for the mb_hypothesize tool wrapper (schema B)."""

from __future__ import annotations

from pathlib import Path

import pytest

from api.pipeline.schematic.schemas import (
    ComponentNode, ElectricalGraph, NetNode, PagePin, PowerRail,
    SchematicQualityReport,
)
from api.tools.hypothesize import mb_hypothesize

SLUG = "demo-device"


def _write_graph(memory_root: Path, graph: ElectricalGraph) -> None:
    pack = memory_root / graph.device_slug
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "electrical_graph.json").write_text(graph.model_dump_json(indent=2))


@pytest.fixture
def memory_root(tmp_path: Path) -> Path:
    return tmp_path / "memory"


@pytest.fixture
def graph() -> ElectricalGraph:
    return ElectricalGraph(
        device_slug=SLUG,
        components={
            "U7": ComponentNode(refdes="U7", type="ic", pins=[
                PagePin(number="1", role="power_in", net_label="VIN"),
                PagePin(number="2", role="power_out", net_label="+5V"),
            ]),
            "U12": ComponentNode(refdes="U12", type="ic", pins=[
                PagePin(number="1", role="power_in", net_label="+5V"),
            ]),
        },
        nets={
            "VIN": NetNode(label="VIN", is_power=True, is_global=True),
            "+5V": NetNode(label="+5V", is_power=True, is_global=True),
        },
        power_rails={
            "VIN": PowerRail(label="VIN", source_refdes=None),
            "+5V": PowerRail(label="+5V", source_refdes="U7", consumers=["U12"]),
        },
        typed_edges=[], boot_sequence=[], designer_notes=[], ambiguities=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )


def test_mb_hypothesize_happy_path(memory_root: Path, graph: ElectricalGraph):
    _write_graph(memory_root, graph)
    result = mb_hypothesize(
        device_slug=SLUG, memory_root=memory_root,
        state_comps={"U12": "dead"},
        state_rails={"+5V": "dead"},
    )
    assert result["found"] is True
    assert result["device_slug"] == SLUG
    assert len(result["hypotheses"]) >= 1
    top = result["hypotheses"][0]
    assert top["kill_refdes"] == ["U7"]
    assert top["kill_modes"] == ["dead"]


def test_mb_hypothesize_accepts_metrics(memory_root: Path, graph: ElectricalGraph):
    _write_graph(memory_root, graph)
    result = mb_hypothesize(
        device_slug=SLUG, memory_root=memory_root,
        state_rails={"+5V": "dead"},
        metrics_rails={"+5V": {"measured": 0.02, "unit": "V", "nominal": 5.0}},
    )
    assert result["found"] is True
    top = result["hypotheses"][0]
    # Measurement cited in the narrative.
    assert "0.02" in top["narrative"] or "5.0" in top["narrative"]


def test_mb_hypothesize_synthesise_from_repair_journal(
    memory_root: Path, graph: ElectricalGraph,
):
    from api.agent.measurement_memory import append_measurement
    _write_graph(memory_root, graph)
    # Tech recorded one measurement in the journal → mb_hypothesize reads it.
    append_measurement(
        memory_root=memory_root, device_slug=SLUG, repair_id="r1",
        target="rail:+5V", value=0.02, unit="V", nominal=5.0, source="ui",
    )
    result = mb_hypothesize(
        device_slug=SLUG, memory_root=memory_root, repair_id="r1",
    )
    assert result["found"] is True
    assert result["hypotheses"][0]["kill_refdes"] == ["U7"]


def test_mb_hypothesize_unknown_refdes_rejected(memory_root: Path, graph: ElectricalGraph):
    _write_graph(memory_root, graph)
    result = mb_hypothesize(
        device_slug=SLUG, memory_root=memory_root,
        state_comps={"Z999": "dead"},
    )
    assert result["found"] is False
    assert result["reason"] == "unknown_refdes"
    assert "Z999" in result["invalid_refdes"]


def test_mb_hypothesize_unknown_rail_rejected(memory_root: Path, graph: ElectricalGraph):
    _write_graph(memory_root, graph)
    result = mb_hypothesize(
        device_slug=SLUG, memory_root=memory_root,
        state_rails={"NOT_A_RAIL": "dead"},
    )
    assert result["found"] is False
    assert result["reason"] == "unknown_rail"
    assert "NOT_A_RAIL" in result["invalid_rails"]


def test_mb_hypothesize_no_pack(memory_root: Path):
    result = mb_hypothesize(
        device_slug="nonexistent", memory_root=memory_root,
        state_rails={"+5V": "dead"},
    )
    assert result["found"] is False
    assert result["reason"] == "no_schematic_graph"


def test_mb_hypothesize_empty_inputs(memory_root: Path, graph: ElectricalGraph):
    _write_graph(memory_root, graph)
    result = mb_hypothesize(
        device_slug=SLUG, memory_root=memory_root,
    )
    assert result["found"] is True
    assert result["hypotheses"] == []


def test_mb_hypothesize_manifest_exposes_new_signature():
    from api.agent import manifest
    names: list[str] = []
    if hasattr(manifest, "TOOLS"):
        names = [t["name"] for t in manifest.TOOLS]
    elif hasattr(manifest, "build_tools_manifest"):
        tools = manifest.build_tools_manifest(session=None)
        names = [t["name"] for t in tools]
    assert "mb_hypothesize" in names
    # Verify schema B properties are present.
    if hasattr(manifest, "build_tools_manifest"):
        tools = manifest.build_tools_manifest(session=None)
    else:
        tools = manifest.TOOLS
    hyp = next(t for t in tools if t["name"] == "mb_hypothesize")
    props = hyp["input_schema"]["properties"]
    assert "state_comps" in props
    assert "state_rails" in props
    assert "metrics_comps" in props
    assert "metrics_rails" in props
    assert "repair_id" in props
    # Verify the 6 new measurement/observation tools are registered.
    new_tools = [
        "mb_record_measurement",
        "mb_list_measurements",
        "mb_compare_measurements",
        "mb_observations_from_measurements",
        "mb_set_observation",
        "mb_clear_observations",
    ]
    for tool_name in new_tools:
        assert tool_name in names, f"{tool_name} not found in manifest"
