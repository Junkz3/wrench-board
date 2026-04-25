"""BoardView R5.0 .gr parser — dispatch + happy path + fallback markers."""

from __future__ import annotations

from pathlib import Path

import pytest

from api.board.parser.base import InvalidBoardFile, parser_for
from api.board.parser.gr import GRParser

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_dispatches_gr_extension(tmp_path: Path):
    f = tmp_path / "demo.gr"
    f.write_text("dummy")
    assert isinstance(parser_for(f), GRParser)


def test_parses_minimal_gr_fixture_with_components_and_testpoints():
    board = GRParser().parse_file(FIXTURE_DIR / "minimal.gr")
    assert board.source_format == "gr"
    assert [p.refdes for p in board.parts] == ["U1", "R1"]
    assert len(board.pins) == 6
    assert len(board.nails) == 1
    assert board.net_by_name("+5V") is not None
    assert board.net_by_name("+5V").is_power is True


def test_accepts_canonical_parts_nails_spellings_too():
    """R5 files occasionally carry `Parts:` / `Nails:` — parser must accept both."""
    text = (
        "var_data: 0 1 2 1\n"
        "Parts:\n"
        "R1 5 2\n"
        "Pins:\n"
        "0 0 -99 1 +3V3\n"
        "10 0 1 1 GND\n"
        "Nails:\n"
        "1 10 0 1 GND\n"
    )
    board = GRParser().parse(text.encode(), file_hash="sha256:x", board_id="b")
    assert len(board.parts) == 1
    assert len(board.pins) == 2
    assert board.nails[0].net == "GND"


def test_rejects_garbage_payload(tmp_path: Path):
    f = tmp_path / "nope.gr"
    f.write_text("hello world\n")
    with pytest.raises(InvalidBoardFile):
        GRParser().parse_file(f)
