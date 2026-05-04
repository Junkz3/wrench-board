"""Tests for the mb_hypothesize tool wrapper (schema B)."""

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


def test_mb_hypothesize_writes_diagnosis_log(memory_root: Path, graph: ElectricalGraph):
    from api.agent.diagnosis_log import load_diagnosis_log
    _write_graph(memory_root, graph)
    result = mb_hypothesize(
        device_slug=SLUG, memory_root=memory_root, repair_id="r42",
        state_rails={"+5V": "dead"},
    )
    assert result["found"] is True
    entries = load_diagnosis_log(memory_root=memory_root, device_slug=SLUG, repair_id="r42")
    assert len(entries) == 1
    assert entries[0].observations["state_rails"] == {"+5V": "dead"}
    assert entries[0].hypotheses_top5[0]["kill_refdes"] == ["U7"]


def test_mb_hypothesize_no_log_when_no_repair_id(memory_root: Path, graph: ElectricalGraph):
    from api.agent.diagnosis_log import load_diagnosis_log
    _write_graph(memory_root, graph)
    result = mb_hypothesize(
        device_slug=SLUG, memory_root=memory_root,
        state_rails={"+5V": "dead"},
    )
    assert result["found"] is True
    # No repair_id → no diagnosis log entry written anywhere.
    assert load_diagnosis_log(memory_root=memory_root, device_slug=SLUG, repair_id="anything") == []


def test_mb_hypothesize_manifest_exposes_new_signature():
    from api.agent import manifest
    # Use MB_TOOLS directly (always present, no session needed).
    tools = manifest.MB_TOOLS
    names = [t["name"] for t in tools]
    assert "mb_hypothesize" in names
    # Verify schema B properties are present.
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


def test_mb_validate_finding_in_manifest():
    from api.agent.manifest import MB_TOOLS
    names = [t["name"] for t in MB_TOOLS]
    assert "mb_validate_finding" in names


def test_load_pack_caches_and_invalidates_on_mtime(
    memory_root: Path, graph: ElectricalGraph,
):
    """Pack cache returns the same ElectricalGraph object on repeated reads
    of an unchanged pack — so the per-graph memo in hypothesize.py fires —
    and invalidates automatically when the graph file's mtime advances."""
    import os

    from api.tools.hypothesize import _PACK_CACHE, _load_pack

    _PACK_CACHE.clear()
    _write_graph(memory_root, graph)
    pack = memory_root / graph.device_slug

    eg1, _ab1, err1 = _load_pack(pack)
    eg2, _ab2, err2 = _load_pack(pack)
    assert err1 is None and err2 is None
    assert eg1 is eg2, "cache hit must return the same object for memo to fire"

    # Advance mtime and rewrite — cache must rebuild.
    graph_path = pack / "electrical_graph.json"
    old_mtime = graph_path.stat().st_mtime_ns
    graph_path.write_text(graph.model_dump_json(indent=2))
    os.utime(graph_path, ns=(old_mtime + 10_000_000, old_mtime + 10_000_000))
    eg3, _ab3, err3 = _load_pack(pack)
    assert err3 is None
    assert eg3 is not eg1, "mtime change must invalidate the cache entry"
