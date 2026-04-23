# tests/pipeline/schematic/test_simulator.py
"""Tests for the behavioral simulator — sync pure function over ElectricalGraph."""

from __future__ import annotations

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
