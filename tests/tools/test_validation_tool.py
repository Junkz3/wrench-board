"""Contract tests for mb_validate_finding."""

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
from api.tools.validation import mb_validate_finding

SLUG = "demo"


@pytest.fixture
def mr(tmp_path: Path) -> Path:
    return tmp_path / "memory"


def _write_graph(mr: Path) -> None:
    graph = ElectricalGraph(
        device_slug=SLUG,
        components={
            "U7": ComponentNode(refdes="U7", type="ic", pins=[
                PagePin(number="1", role="power_in", net_label="VIN"),
            ]),
            "U12": ComponentNode(refdes="U12", type="ic", pins=[
                PagePin(number="1", role="power_in", net_label="+5V"),
            ]),
        },
        nets={"VIN": NetNode(label="VIN", is_power=True, is_global=True),
              "+5V": NetNode(label="+5V", is_power=True, is_global=True)},
        power_rails={
            "VIN": PowerRail(label="VIN", source_refdes=None),
            "+5V": PowerRail(label="+5V", source_refdes="U7", consumers=["U12"]),
        },
        typed_edges=[], boot_sequence=[], designer_notes=[], ambiguities=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )
    pack = mr / SLUG
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "electrical_graph.json").write_text(graph.model_dump_json(indent=2))


def test_validate_finding_happy(mr: Path):
    _write_graph(mr)
    result = mb_validate_finding(
        device_slug=SLUG, repair_id="r1", memory_root=mr,
        fixes=[{"refdes": "U12", "mode": "dead", "rationale": "replaced"}],
        tech_note="reflow + swap",
    )
    assert result["validated"] is True
    assert result["fixes_count"] == 1
    from api.agent.validation import load_outcome
    oc = load_outcome(memory_root=mr, device_slug=SLUG, repair_id="r1")
    assert oc is not None
    assert oc.fixes[0].refdes == "U12"
    assert oc.tech_note == "reflow + swap"


def test_validate_finding_rejects_unknown_refdes(mr: Path):
    _write_graph(mr)
    result = mb_validate_finding(
        device_slug=SLUG, repair_id="r1", memory_root=mr,
        fixes=[{"refdes": "Z999", "mode": "dead", "rationale": "???"}],
    )
    assert result["validated"] is False
    assert result["reason"] == "unknown_refdes"
    assert "Z999" in result["invalid_refdes"]


def test_validate_finding_rejects_empty_fixes(mr: Path):
    _write_graph(mr)
    result = mb_validate_finding(
        device_slug=SLUG, repair_id="r1", memory_root=mr,
        fixes=[],
    )
    assert result["validated"] is False
    assert result["reason"] == "empty_fixes"


def test_validate_finding_rejects_invalid_mode(mr: Path):
    _write_graph(mr)
    result = mb_validate_finding(
        device_slug=SLUG, repair_id="r1", memory_root=mr,
        fixes=[{"refdes": "U7", "mode": "bogus", "rationale": "x"}],
    )
    assert result["validated"] is False
    assert result["reason"] == "invalid_fix"


def test_validate_finding_emits_ws_event(mr: Path, monkeypatch):
    _write_graph(mr)
    captured: list[dict] = []
    monkeypatch.setattr("api.tools.validation._emit", lambda ev: captured.append(ev))
    mb_validate_finding(
        device_slug=SLUG, repair_id="r1", memory_root=mr,
        fixes=[{"refdes": "U7", "mode": "dead", "rationale": "ok"}],
    )
    assert any(e["type"] == "simulation.repair_validated" for e in captured)
    evt = next(e for e in captured if e["type"] == "simulation.repair_validated")
    assert evt["repair_id"] == "r1"
    assert evt["fixes_count"] == 1


def test_validate_finding_no_graph_still_accepts(mr: Path):
    # If the device has no electrical_graph yet (fresh pack), validation
    # still accepts — refdes guardrail is advisory, not blocking.
    result = mb_validate_finding(
        device_slug="nonexistent", repair_id="r", memory_root=mr,
        fixes=[{"refdes": "U7", "mode": "dead", "rationale": "trust tech"}],
    )
    assert result["validated"] is True
