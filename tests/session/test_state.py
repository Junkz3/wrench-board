from pathlib import Path

from api.board.parser.test_link import BRDParser
from api.session.state import SessionState

FIXTURE_DIR = Path(__file__).parent.parent / "board" / "fixtures"


def test_new_session_has_no_board():
    s = SessionState()
    assert s.board is None
    assert s.layer == "top"
    assert s.highlights == set()
    assert s.net_highlight is None


def test_set_board_resets_view():
    s = SessionState()
    s.highlights.add("U1")
    s.net_highlight = "+3V3"

    board = BRDParser().parse_file(FIXTURE_DIR / "minimal.brd")
    s.set_board(board)

    assert s.board is board
    assert s.highlights == set()
    assert s.net_highlight is None
    assert s.layer == "top"
    assert s.annotations == {}
    assert s.arrows == {}


def test_session_tracks_annotations_and_arrows():
    s = SessionState()
    s.annotations["ann-1"] = {"refdes": "U7", "label": "PMIC"}
    s.arrows["arr-1"] = {"from": [0, 0], "to": [10, 10]}
    assert len(s.annotations) == 1
    assert len(s.arrows) == 1
