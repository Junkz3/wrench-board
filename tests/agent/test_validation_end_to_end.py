"""End-to-end: diagnose → validate → outcome + WS event."""

from pathlib import Path

from api.agent.diagnosis_log import load_diagnosis_log
from api.agent.validation import load_outcome
from api.tools.validation import mb_validate_finding, set_ws_emitter


# Import hypothesize only after other imports to avoid circular dependency
# when run from tests/agent/
def _get_mb_hypothesize():
    from api.tools.hypothesize import mb_hypothesize
    return mb_hypothesize


def _write_graph(mr: Path, slug: str = "demo") -> None:
    from api.pipeline.schematic.schemas import (
        ComponentNode,
        ElectricalGraph,
        NetNode,
        PagePin,
        PowerRail,
        SchematicQualityReport,
    )
    g = ElectricalGraph(
        device_slug=slug,
        components={
            "U7": ComponentNode(refdes="U7", type="ic", pins=[
                PagePin(number="1", role="power_in", net_label="VIN"),
                PagePin(number="2", role="power_out", net_label="+5V"),
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
    pack = mr / slug
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "electrical_graph.json").write_text(g.model_dump_json(indent=2))


def test_full_diagnose_validate_loop(tmp_path: Path):
    mr = tmp_path / "memory"
    _write_graph(mr)

    captured = []
    set_ws_emitter(lambda ev: captured.append(ev))
    try:
        # Tech diagnoses with an observation.
        mb_hypothesize = _get_mb_hypothesize()
        hyp = mb_hypothesize(
            device_slug="demo", memory_root=mr, repair_id="r1",
            state_rails={"+5V": "dead"},
        )
        assert hyp["found"] is True

        # Diagnosis log was written.
        log = load_diagnosis_log(memory_root=mr, device_slug="demo", repair_id="r1")
        assert len(log) == 1

        # Tech clicks Marquer fix → agent validates.
        val = mb_validate_finding(
            device_slug="demo", repair_id="r1", memory_root=mr,
            fixes=[{"refdes": "U7", "mode": "dead", "rationale": "replaced buck"}],
        )
        assert val["validated"] is True

        # Outcome on disk.
        oc = load_outcome(memory_root=mr, device_slug="demo", repair_id="r1")
        assert oc is not None
        assert oc.fixes[0].refdes == "U7"

        # WS event fired.
        assert any(ev["type"] == "simulation.repair_validated" for ev in captured)
    finally:
        set_ws_emitter(None)
