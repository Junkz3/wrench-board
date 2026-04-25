"""IBM Castw .cst parser — bracketed section headers + no var_data."""

from __future__ import annotations

from pathlib import Path

import pytest

from api.board.model import Layer
from api.board.parser.base import InvalidBoardFile, parser_for
from api.board.parser.cst import CSTParser

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_dispatches_cst_extension(tmp_path: Path):
    f = tmp_path / "demo.cst"
    f.write_text("dummy")
    assert isinstance(parser_for(f), CSTParser)


def test_parses_minimal_cst_fixture():
    board = CSTParser().parse_file(FIXTURE_DIR / "minimal.cst")
    assert board.source_format == "cst"
    assert len(board.outline) == 4
    assert [p.refdes for p in board.parts] == ["R1", "C1"]
    assert len(board.pins) == 4
    assert len(board.nails) == 1
    assert board.part_by_refdes("R1").layer == Layer.TOP
    assert board.part_by_refdes("C1").layer == Layer.BOTTOM


def test_comment_lines_before_first_section_are_skipped():
    """Files often open with `;` comments before the first `[Format]` section."""
    text = (
        "; some preamble\n"
        "[Components]\n"
        "R1 5 1\n"
        "[Pins]\n"
        "0 0 -99 1 +3V3\n"
    )
    board = CSTParser().parse(text.encode(), file_hash="sha256:x", board_id="b")
    assert [p.refdes for p in board.parts] == ["R1"]


def test_rejects_empty_payload(tmp_path: Path):
    f = tmp_path / "empty.cst"
    f.write_bytes(b"")
    with pytest.raises(InvalidBoardFile):
        CSTParser().parse_file(f)


def test_rejects_unrelated_payload(tmp_path: Path):
    f = tmp_path / "bad.cst"
    f.write_text("just a readme file\n")
    with pytest.raises(InvalidBoardFile):
        CSTParser().parse_file(f)
