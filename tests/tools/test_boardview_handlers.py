from pathlib import Path

import pytest

from api.board.parser.test_link import BRDParser
from api.session.state import SessionState
from api.tools.boardview import annotate, draw_arrow, filter_by_type, flip_board, focus_component, highlight_component, highlight_net, measure_distance, reset_view, show_pin

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


def test_annotate_adds_to_session(session):
    result = annotate(session, refdes="R1", label="Pull-up 10k")
    assert result["ok"] is True
    ann_id = result["event"].id
    assert ann_id in session.annotations
    assert session.annotations[ann_id]["label"] == "Pull-up 10k"


def test_annotate_invalid_refdes(session):
    result = annotate(session, refdes="UFOO", label="...")
    assert result["ok"] is False


def test_filter_by_type_sets_session(session):
    result = filter_by_type(session, prefix="R")
    assert result["ok"] is True
    assert result["event"].prefix == "R"
    assert session.filter_prefix == "R"


def test_filter_by_type_with_empty_prefix_clears(session):
    session.filter_prefix = "U"
    result = filter_by_type(session, prefix="")
    assert result["ok"] is True
    assert session.filter_prefix is None


def test_draw_arrow_between_parts(session):
    result = draw_arrow(session, from_refdes="R1", to_refdes="C1")
    assert result["ok"] is True
    arrow_id = result["event"].id
    assert arrow_id in session.arrows


def test_measure_distance_returns_mm(session):
    result = measure_distance(session, refdes_a="R1", refdes_b="C1")
    assert result["ok"] is True
    # Pin coords in fixture: R1 at (100,100)-(100,200) centered 100,150
    # C1 at (400,100)-(400,200) centered 400,150
    # Distance = 300 mils = 7.62 mm
    assert 7.0 < result["event"].distance_mm < 8.0


def test_show_pin_happy_path(session):
    result = show_pin(session, refdes="R1", pin=1)
    assert result["ok"] is True
    ev = result["event"]
    assert ev.refdes == "R1"
    assert ev.pin == 1
    assert ev.pos == (100, 100)


def test_show_pin_unknown_pin(session):
    result = show_pin(session, refdes="R1", pin=99)
    assert result["ok"] is False
    assert result["reason"] == "unknown-pin"
