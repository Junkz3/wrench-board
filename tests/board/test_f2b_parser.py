"""Unisoft ProntoPLACE .f2b parser — dispatch + happy path + annotation skip."""

from __future__ import annotations

from pathlib import Path

import pytest

from api.board.parser.base import InvalidBoardFile, parser_for
from api.board.parser.f2b import F2BParser

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_dispatches_f2b_extension(tmp_path: Path):
    f = tmp_path / "demo.f2b"
    f.write_text("dummy")
    assert isinstance(parser_for(f), F2BParser)


def test_parses_minimal_f2b_fixture_and_skips_annotations():
    """The fixture carries an `Annotations:` block between Pins: and Nails:;
    the parser must skip it gracefully and still resolve 4 pins + 1 nail."""
    board = F2BParser().parse_file(FIXTURE_DIR / "minimal.f2b")
    assert board.source_format == "f2b"
    assert [p.refdes for p in board.parts] == ["R1", "C1"]
    assert len(board.pins) == 4
    assert len(board.nails) == 1
    assert board.net_by_name("GND").is_ground is True


def test_rejects_unrelated_payload(tmp_path: Path):
    f = tmp_path / "nope.f2b"
    f.write_text("not a boardview file\n")
    with pytest.raises(InvalidBoardFile):
        F2BParser().parse_file(f)
