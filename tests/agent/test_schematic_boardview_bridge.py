# SPDX-License-Identifier: Apache-2.0
"""Coverage for api.agent.schematic_boardview_bridge."""

from __future__ import annotations

import pytest

from api.agent.schematic_boardview_bridge import (
    EnrichedTimeline,
    ProbePoint,
    enrich,
)
from api.board.model import Board, Layer, Part, Pin, Point
from api.pipeline.schematic.simulator import BoardState, SimulationTimeline


def _empty_board() -> Board:
    return Board(
        board_id="test",
        file_hash="sha256:x",
        source_format="test_link",
        outline=[],
        parts=[],
        pins=[],
        nets=[],
        nails=[],
    )


@pytest.fixture
def empty_timeline() -> SimulationTimeline:
    return SimulationTimeline(
        device_slug="test",
        killed_refdes=[],
        states=[BoardState(phase_index=1, phase_name="Phase 1")],
        final_verdict="completed",
    )


@pytest.fixture
def empty_board() -> Board:
    return _empty_board()


def test_enrich_returns_enriched_timeline_with_empty_route(
    empty_timeline, empty_board
):
    out = enrich(empty_timeline, empty_board)
    assert isinstance(out, EnrichedTimeline)
    assert out.timeline == empty_timeline
    assert out.probe_route == []
    assert out.unmapped_refdes == []


def test_probe_point_shape():
    pp = ProbePoint(
        refdes="U7",
        side="top",
        coords=(45.2, 23.1),
        bbox_mm=((40.0, 20.0), (50.0, 26.0)),
        reason="rail source",
        priority=1,
    )
    assert pp.refdes == "U7"
    assert pp.priority == 1


@pytest.fixture
def board_with_two_parts() -> Board:
    return Board(
        board_id="test",
        file_hash="sha256:x",
        source_format="test_link",
        outline=[],
        parts=[
            Part(
                refdes="U7",
                layer=Layer.TOP,
                is_smd=True,
                bbox=(Point(x=1000, y=1000), Point(x=2000, y=2000)),
                pin_refs=[0],
            ),
            Part(
                refdes="C42",
                layer=Layer.BOTTOM,
                is_smd=True,
                bbox=(Point(x=1100, y=1100), Point(x=1300, y=1300)),
                pin_refs=[1],
            ),
        ],
        pins=[
            Pin(part_refdes="U7", index=1, pos=Point(x=1500, y=1500), layer=Layer.TOP),
            Pin(part_refdes="C42", index=1, pos=Point(x=1200, y=1200), layer=Layer.BOTTOM),
        ],
        nets=[],
        nails=[],
    )


@pytest.fixture
def timeline_blocked_on_5v_with_dead_u7() -> SimulationTimeline:
    """Cascade includes U7 dead, blocked at phase 1 because +5V didn't stabilise."""
    state = BoardState(
        phase_index=1,
        phase_name="Phase 1",
        rails={"+5V": "off"},
        components={"U7": "dead"},
        blocked=True,
        blocked_reason="Rail +5V never stabilised — source U7 is dead",
    )
    return SimulationTimeline(
        device_slug="test",
        killed_refdes=["U7"],
        states=[state],
        final_verdict="blocked",
        blocked_at_phase=1,
        cascade_dead_components=["U7"],
        cascade_dead_rails=["+5V"],
    )


def test_enrich_priority_1_is_blocked_rail_source_ic(
    timeline_blocked_on_5v_with_dead_u7, board_with_two_parts
):
    out = enrich(timeline_blocked_on_5v_with_dead_u7, board_with_two_parts)
    assert len(out.probe_route) >= 1
    p1 = next((p for p in out.probe_route if p.priority == 1), None)
    assert p1 is not None
    assert p1.refdes == "U7"
    assert p1.side == "top"


def test_enrich_converts_mil_to_mm(
    timeline_blocked_on_5v_with_dead_u7, board_with_two_parts
):
    out = enrich(timeline_blocked_on_5v_with_dead_u7, board_with_two_parts)
    p1 = next(p for p in out.probe_route if p.refdes == "U7")
    # Center of (1000,1000)-(2000,2000) bbox = (1500, 1500) mils = (38.1, 38.1) mm
    assert pytest.approx(p1.coords[0], abs=0.01) == 38.1
    assert pytest.approx(p1.coords[1], abs=0.01) == 38.1


def test_enrich_appends_unmapped_refdes_when_part_missing():
    tl = SimulationTimeline(
        device_slug="test",
        killed_refdes=["U99"],
        states=[],
        final_verdict="cascade",
        cascade_dead_components=["U99"],
    )
    board = _empty_board()
    out = enrich(tl, board)
    assert "U99" in out.unmapped_refdes
