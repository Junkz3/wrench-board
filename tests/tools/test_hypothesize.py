# tests/tools/test_hypothesize.py
# SPDX-License-Identifier: Apache-2.0
"""Tests for the mb_hypothesize tool wrapper."""

from __future__ import annotations

from pathlib import Path

import pytest

from api.pipeline.schematic.schemas import (
    ComponentNode,
    ElectricalGraph,
    NetNode,
    PagePin,
    PowerRail,
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
        dead_rails=["+5V"], dead_comps=["U12"],
    )
    assert result["found"] is True
    assert result["device_slug"] == SLUG
    assert len(result["hypotheses"]) >= 1
    # U7 (source of +5V) should rank top.
    top = result["hypotheses"][0]
    assert top["kill_refdes"] == ["U7"]


def test_mb_hypothesize_unknown_refdes_rejected(memory_root: Path, graph: ElectricalGraph):
    _write_graph(memory_root, graph)
    result = mb_hypothesize(
        device_slug=SLUG, memory_root=memory_root,
        dead_comps=["Z999"],
    )
    assert result["found"] is False
    assert result["reason"] == "unknown_refdes"
    assert "Z999" in result["invalid_refdes"]


def test_mb_hypothesize_unknown_rail_rejected(memory_root: Path, graph: ElectricalGraph):
    _write_graph(memory_root, graph)
    result = mb_hypothesize(
        device_slug=SLUG, memory_root=memory_root,
        dead_rails=["NOT_A_RAIL"],
    )
    assert result["found"] is False
    assert result["reason"] == "unknown_rail"
    assert "NOT_A_RAIL" in result["invalid_rails"]


def test_mb_hypothesize_no_pack(memory_root: Path):
    result = mb_hypothesize(
        device_slug="nonexistent", memory_root=memory_root,
        dead_rails=["+5V"],
    )
    assert result["found"] is False
    assert result["reason"] == "no_schematic_graph"


def test_mb_hypothesize_empty_observations_returns_empty(memory_root: Path, graph: ElectricalGraph):
    _write_graph(memory_root, graph)
    result = mb_hypothesize(
        device_slug=SLUG, memory_root=memory_root,
    )
    assert result["found"] is True
    assert result["hypotheses"] == []


def test_manifest_exposes_mb_hypothesize():
    """Agent manifest must advertise the new tool so Claude knows to call it."""
    from api.agent import manifest
    from api.session.state import SessionState
    names: list[str] = []
    if hasattr(manifest, "TOOLS"):
        names = [t["name"] for t in manifest.TOOLS]
    elif hasattr(manifest, "build_tools_manifest"):
        session = SessionState()
        tools = manifest.build_tools_manifest(session=session)
        names = [t["name"] for t in tools]
    assert "mb_hypothesize" in names
