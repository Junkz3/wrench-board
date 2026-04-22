from pathlib import Path

import pytest

from api.board.parser.test_link import BRDParser
from api.session.state import SessionState
from api.tools.boardview import flip_board, focus_component, highlight_component, highlight_net, reset_view

FIXTURE_DIR = Path(__file__).parent.parent / "board" / "fixtures"


@pytest.fixture
def session() -> SessionState:
    s = SessionState()
    s.set_board(BRDParser().parse_file(FIXTURE_DIR / "minimal.brd"))
    return s


def test_highlight_component_happy_path(session):
    result = highlight_component(session, refdes="R1")
    assert result["ok"] is True
    assert result["event"].type == "boardview.highlight"
    assert result["event"].refdes == ["R1"]
    assert "R1" in session.highlights


def test_highlight_component_accepts_list(session):
    result = highlight_component(session, refdes=["R1", "C1"])
    assert result["ok"] is True
    assert set(session.highlights) == {"R1", "C1"}


def test_highlight_component_invalid_refdes_returns_suggestions(session):
    result = highlight_component(session, refdes="R2")
    assert result["ok"] is False
    assert result["reason"] == "unknown-refdes"
    assert "R1" in result["suggestions"]
    assert "R1" not in session.highlights  # state untouched


def test_highlight_component_additive(session):
    highlight_component(session, refdes="R1")
    highlight_component(session, refdes="C1", additive=True)
    assert session.highlights == {"R1", "C1"}


def test_highlight_component_non_additive_replaces(session):
    highlight_component(session, refdes="R1")
    highlight_component(session, refdes="C1", additive=False)
    assert session.highlights == {"C1"}


def test_focus_component_happy_path(session):
    result = focus_component(session, refdes="R1", zoom=2.5)
    assert result["ok"] is True
    ev = result["event"]
    assert ev.type == "boardview.focus"
    assert ev.refdes == "R1"
    assert ev.zoom == 2.5


def test_focus_component_auto_flips_to_other_side(session):
    # C1 is on bottom layer in the fixture. Session starts on "top".
    assert session.layer == "top"
    result = focus_component(session, refdes="C1")
    assert result["ok"] is True
    assert result["event"].auto_flipped is True
    assert session.layer == "bottom"


def test_focus_component_unknown(session):
    result = focus_component(session, refdes="UFOO")
    assert result["ok"] is False
    assert result["reason"] == "unknown-refdes"


def test_reset_view_clears_everything(session):
    session.highlights.add("R1")
    session.net_highlight = "+3V3"
    session.dim_unrelated = True
    result = reset_view(session)
    assert result["ok"] is True
    assert session.highlights == set()
    assert session.net_highlight is None
    assert session.dim_unrelated is False
    assert result["event"].type == "boardview.reset_view"


def test_highlight_net_happy_path(session):
    result = highlight_net(session, net="+3V3")
    assert result["ok"] is True
    ev = result["event"]
    assert ev.type == "boardview.highlight_net"
    assert ev.net == "+3V3"
    assert session.net_highlight == "+3V3"
    assert len(ev.pin_refs) >= 1


def test_highlight_net_unknown(session):
    result = highlight_net(session, net="MISSING")
    assert result["ok"] is False
    assert result["reason"] == "unknown-net"


def test_flip_board_toggles_side(session):
    assert session.layer == "top"
    result = flip_board(session)
    assert result["ok"] is True
    assert result["event"].new_side == "bottom"
    assert session.layer == "bottom"
    flip_board(session)
    assert session.layer == "top"
