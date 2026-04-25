"""Generic BoardViewer .cad parser — umbrella over Test_Link + BRD2."""

from __future__ import annotations

from pathlib import Path

import pytest

from api.board.model import Layer
from api.board.parser.base import InvalidBoardFile, parser_for
from api.board.parser.cad import CADParser

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_dispatches_cad_extension(tmp_path: Path):
    f = tmp_path / "demo.cad"
    f.write_text("dummy")
    assert isinstance(parser_for(f), CADParser)


def test_parses_test_link_shape_with_uppercase_markers():
    board = CADParser().parse_file(FIXTURE_DIR / "minimal.cad")
    assert board.source_format == "cad"
    assert len(board.parts) == 2
    assert len(board.pins) == 4
    assert board.part_by_refdes("R1").layer == Layer.TOP
    assert board.net_by_name("GND").is_ground is True


def test_parses_brd2_shape_and_tags_source_format_cad():
    """A `.cad` file carrying BRDOUT: marker routes through BRD2Parser but
    the emitted Board keeps source_format='cad' so the UI knows the upload."""
    board = CADParser().parse_file(FIXTURE_DIR / "brd2_form.cad")
    assert board.source_format == "cad"
    assert len(board.parts) == 1
    assert len(board.pins) == 2


def test_rejects_non_boardview_payload(tmp_path: Path):
    f = tmp_path / "nope.cad"
    f.write_text("this is just prose, not a boardview\n")
    with pytest.raises(InvalidBoardFile):
        CADParser().parse_file(f)
