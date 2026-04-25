"""ASUS TSICT .asc parser — combined single-file + split-directory paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from api.board.model import Layer
from api.board.parser.asc import ASCParser
from api.board.parser.base import InvalidBoardFile, parser_for

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_dispatches_asc_extension(tmp_path: Path):
    f = tmp_path / "demo.asc"
    f.write_text("any")
    assert isinstance(parser_for(f), ASCParser)


def test_parses_combined_single_file_fixture():
    board = ASCParser().parse_file(FIXTURE_DIR / "tsict_combined.asc")
    assert board.source_format == "asc"
    assert [p.refdes for p in board.parts] == ["R1", "C1"]
    assert len(board.pins) == 4
    assert len(board.nails) == 1
    assert board.part_by_refdes("R1").layer == Layer.TOP


def test_parses_split_directory_by_reading_siblings(tmp_path: Path):
    """When pins.asc is uploaded on its own and format/parts/nails sit next
    to it, the parser stitches them together."""
    (tmp_path / "format.asc").write_text("0 0\n1000 0\n1000 500\n0 500\n")
    (tmp_path / "parts.asc").write_text("R1 5 2\nC1 10 4\n")
    (tmp_path / "pins.asc").write_text(
        "100 100 -99 1 +3V3\n"
        "100 200 -99 1 GND\n"
        "400 100 1 2 +3V3\n"
        "400 200 -99 2 GND\n"
    )
    (tmp_path / "nails.asc").write_text("1 400 100 1 +3V3\n")

    board = ASCParser().parse_file(tmp_path / "pins.asc")
    assert board.source_format == "asc"
    assert [p.refdes for p in board.parts] == ["R1", "C1"]
    assert len(board.pins) == 4
    assert board.nails[0].net == "+3V3"


def test_lone_pins_asc_with_no_siblings_returns_empty_board(tmp_path: Path):
    """Directory with only `pins.asc` (itself recognised as a TSICT sub-file)
    still assembles — missing blocks produce 0 counts and an empty Board."""
    (tmp_path / "pins.asc").write_text("")  # empty pins.asc is still a sub-file
    board = ASCParser().parse_file(tmp_path / "pins.asc")
    assert board.parts == []
    assert board.pins == []


def test_partial_payload_via_raw_parse_raises(tmp_path: Path):
    """When invoked via parse(raw, ...) without a path, only combined payloads
    succeed — a single sub-file has no path context to find siblings."""
    raw = b"100 100 -99 1 +3V3\n"
    with pytest.raises(InvalidBoardFile):
        ASCParser().parse(raw, file_hash="sha256:x", board_id="b")


def test_rejects_unrelated_combined_payload(tmp_path: Path):
    f = tmp_path / "nope.asc"
    f.write_text("just lorem ipsum prose, no markers at all\n")
    with pytest.raises(InvalidBoardFile):
        ASCParser().parse_file(f)
