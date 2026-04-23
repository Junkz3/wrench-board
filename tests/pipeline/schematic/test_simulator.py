# tests/pipeline/schematic/test_simulator.py
"""Tests for the behavioral simulator — sync pure function over ElectricalGraph."""

from __future__ import annotations

from api.pipeline.schematic.schemas import (
    AnalyzedBootPhase,
    AnalyzedBootSequence,
    AnalyzedBootTrigger,
    ComponentNode,
    ElectricalGraph,
    NetNode,
    PowerRail,
    SchematicQualityReport,
)
from api.pipeline.schematic.simulator import (
    BoardState,
    SimulationEngine,  # noqa: F401 — verifies public export; tested in later tasks
    SimulationTimeline,
)


def test_board_state_shape_minimal():
    state = BoardState(
        phase_index=0,
        phase_name="Standby",
        rails={"LPC_VCC": "stable"},
        components={"U18": "on"},
        signals={},
        blocked=False,
        blocked_reason=None,
    )
    assert state.phase_index == 0
    assert state.rails["LPC_VCC"] == "stable"


def test_simulation_timeline_shape_minimal():
    tl = SimulationTimeline(
        device_slug="demo",
        killed_refdes=[],
        states=[],
        final_verdict="completed",
        blocked_at_phase=None,
        cascade_dead_components=[],
        cascade_dead_rails=[],
    )
    assert tl.final_verdict == "completed"
    assert tl.killed_refdes == []


def _mnt_like_graph() -> ElectricalGraph:
    """Minimal 3-phase MNT-like topology: battery→VIN, VIN→+5V(U7), +5V→+3V3(U12)."""
    components = {
        "U18": ComponentNode(refdes="U18", type="ic"),
        "U7":  ComponentNode(refdes="U7",  type="ic"),
        "U12": ComponentNode(refdes="U12", type="ic"),
        "U19": ComponentNode(refdes="U19", type="ic"),
    }
    nets = {
        "VIN":     NetNode(label="VIN",     is_power=True, is_global=True),
        "LPC_VCC": NetNode(label="LPC_VCC", is_power=True, is_global=True),
        "+5V":     NetNode(label="+5V",     is_power=True, is_global=True),
        "+3V3":    NetNode(label="+3V3",    is_power=True, is_global=True),
    }
    power_rails = {
        "VIN": PowerRail(label="VIN", voltage_nominal=24.0, source_refdes=None, consumers=["U18"]),
        "LPC_VCC": PowerRail(label="LPC_VCC", source_refdes="U14", consumers=["U18"]),
        "+5V":  PowerRail(label="+5V",  voltage_nominal=5.0, source_refdes="U7",  enable_net="5V_PWR_EN",  consumers=["U12", "U19"]),
        "+3V3": PowerRail(label="+3V3", voltage_nominal=3.3, source_refdes="U12", enable_net="3V3_PWR_EN", consumers=["U19"]),
    }
    return ElectricalGraph(
        device_slug="demo",
        components=components,
        nets=nets,
        power_rails=power_rails,
        typed_edges=[],
        boot_sequence=[],
        designer_notes=[],
        ambiguities=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )


def _mnt_like_analyzed() -> AnalyzedBootSequence:
    return AnalyzedBootSequence(
        device_slug="demo",
        phases=[
            AnalyzedBootPhase(
                index=0, name="Standby", kind="always-on",
                rails_stable=["VIN", "LPC_VCC"],
                components_entering=["U18"],
                triggers_next=[
                    AnalyzedBootTrigger(net_label="5V_PWR_EN", from_refdes="U18", rationale="LPC asserts 5V"),
                ],
            ),
            AnalyzedBootPhase(
                index=1, name="LPC asserts +5V", kind="sequenced",
                rails_stable=["+5V"],
                components_entering=["U7"],
                triggers_next=[
                    AnalyzedBootTrigger(net_label="3V3_PWR_EN", from_refdes="U18", rationale="LPC asserts 3V3"),
                ],
            ),
            AnalyzedBootPhase(
                index=2, name="LPC asserts +3V3", kind="sequenced",
                rails_stable=["+3V3"],
                components_entering=["U12", "U19"],
                triggers_next=[],
            ),
        ],
        sequencer_refdes="U18",
        global_confidence=0.9,
        model_used="test",
    )


def test_run_normal_boot_completes():
    engine = SimulationEngine(_mnt_like_graph(), analyzed_boot=_mnt_like_analyzed())
    tl = engine.run()
    assert tl.final_verdict == "completed"
    assert tl.blocked_at_phase is None
    # One state per phase.
    assert [s.phase_index for s in tl.states] == [0, 1, 2]
    # By the final phase, every rail the analyzer named is stable.
    last = tl.states[-1]
    assert last.rails["+5V"] == "stable"
    assert last.rails["+3V3"] == "stable"
    # Every component that entered a phase is on.
    assert last.components["U18"] == "on"
    assert last.components["U7"]  == "on"
    assert last.components["U12"] == "on"
    assert last.components["U19"] == "on"
    # The enable triggers asserted at each phase boundary show up as high.
    assert last.signals["5V_PWR_EN"] == "high"
    assert last.signals["3V3_PWR_EN"] == "high"
    assert tl.cascade_dead_rails == []
    assert tl.cascade_dead_components == []
