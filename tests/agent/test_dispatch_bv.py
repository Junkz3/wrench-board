"""Tests for dispatch_bv — the bv_* tool router."""

from __future__ import annotations

from api.agent.dispatch_bv import BV_DISPATCH, dispatch_bv
from api.board.model import Board, Layer, Part, Pin, Point
from api.session.state import SessionState


def _session_simple() -> SessionState:
    parts = [
        Part(refdes="U7", layer=Layer.TOP, is_smd=True,
             bbox=(Point(x=0, y=0), Point(x=20, y=20)), pin_refs=[0, 1]),
        Part(refdes="C29", layer=Layer.BOTTOM, is_smd=True,
             bbox=(Point(x=100, y=100), Point(x=110, y=110)), pin_refs=[2, 3]),
    ]
    pins = [
        Pin(part_refdes="U7", index=1, pos=Point(x=5, y=5), layer=Layer.TOP),
        Pin(part_refdes="U7", index=2, pos=Point(x=15, y=15), layer=Layer.TOP),
        Pin(part_refdes="C29", index=1, pos=Point(x=105, y=105), layer=Layer.BOTTOM),
        Pin(part_refdes="C29", index=2, pos=Point(x=108, y=108), layer=Layer.BOTTOM),
    ]
    board = Board(board_id="t", file_hash="sha256:x", source_format="t",
                  outline=[], parts=parts, pins=pins, nets=[], nails=[])
    s = SessionState()
    s.set_board(board)
    return s


def test_bv_dispatch_has_thirteen_entries() -> None:
    assert len(BV_DISPATCH) == 13
    assert set(BV_DISPATCH.keys()) == {
        "bv_highlight", "bv_focus", "bv_reset_view", "bv_flip",
        "bv_annotate", "bv_dim_unrelated", "bv_highlight_net",
        "bv_show_pin", "bv_draw_arrow", "bv_measure",
        "bv_filter_by_type", "bv_layer_visibility", "bv_scene",
    }


def test_dispatch_unknown_tool_returns_error() -> None:
    session = _session_simple()
    result = dispatch_bv(session, "bv_nonexistent", {})
    assert result["ok"] is False
    assert result["reason"] == "unknown-tool"


def test_dispatch_bv_highlight_known_refdes() -> None:
    session = _session_simple()
    result = dispatch_bv(session, "bv_highlight", {"refdes": "U7"})
    assert result["ok"] is True
    assert result["event"] is not None
    assert result["event"].type == "boardview.highlight"
    assert result["event"].refdes == ["U7"]


def test_dispatch_bv_highlight_unknown_refdes_no_event() -> None:
    session = _session_simple()
    result = dispatch_bv(session, "bv_highlight", {"refdes": "U999"})
    assert result["ok"] is False
    assert result["reason"] == "unknown-refdes"
    assert "event" not in result
    assert result["suggestions"]


def test_dispatch_bv_focus_auto_flip_when_layer_opposite() -> None:
    """Session layer=top, part on bottom → event.auto_flipped is True."""
    session = _session_simple()
    assert session.layer == "top"
    result = dispatch_bv(session, "bv_focus", {"refdes": "C29"})
    assert result["ok"] is True
    assert result["event"].auto_flipped is True
    assert session.layer == "bottom"


def test_dispatch_bv_reset_view_clears_agent_state() -> None:
    session = _session_simple()
    dispatch_bv(session, "bv_highlight", {"refdes": "U7"})
    assert session.highlights == {"U7"}
    result = dispatch_bv(session, "bv_reset_view", {})
    assert result["ok"] is True
    assert session.highlights == set()


def test_dispatch_bv_measure_returns_distance() -> None:
    session = _session_simple()
    result = dispatch_bv(session, "bv_measure",
                        {"refdes_a": "U7", "refdes_b": "C29"})
    assert result["ok"] is True
    assert result["event"].distance_mm > 0


def test_dispatch_bv_flip_toggles_layer() -> None:
    session = _session_simple()
    assert session.layer == "top"
    result = dispatch_bv(session, "bv_flip", {})
    assert result["ok"] is True
    assert session.layer == "bottom"
    assert result["event"].new_side == "bottom"


def test_dispatch_catches_handler_exception() -> None:
    """Malformed payload must return {ok: false, reason: handler-exception}."""
    session = _session_simple()
    result = dispatch_bv(session, "bv_highlight", {})
    assert result["ok"] is False
    assert result["reason"] == "handler-exception"


def test_dispatch_bv_annotate_requires_valid_refdes() -> None:
    session = _session_simple()
    result = dispatch_bv(session, "bv_annotate",
                        {"refdes": "U999", "label": "suspect"})
    assert result["ok"] is False
    assert result["reason"] == "unknown-refdes"


def test_dispatch_bv_highlight_net_unknown_net() -> None:
    session = _session_simple()
    result = dispatch_bv(session, "bv_highlight_net", {"net": "NO_SUCH_NET"})
    assert result["ok"] is False
    assert result["reason"] == "unknown-net"
