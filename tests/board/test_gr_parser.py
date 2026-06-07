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


def test_dispatches_brd2_shaped_gr_to_brd2_parser(tmp_path: Path):
    """Some `.gr` files in the wild are actually BRD2-format payloads.

    Real example: `vendor-board boardview.gr` is a BRD2 file
    (UPPERCASE `BRDOUT:` / `NETS:` / `PARTS:` / `PINS:` / `NAILS:` blocks),
    sometimes preceded by a one-line vendor title. The Test_Link-shape `.gr`
    grammar can't read those, so the GR parser delegates BRD2-shaped content
    to the BRD2 parser while keeping `source_format="gr"`.
    """
    f = tmp_path / "brd2shaped.gr"
    f.write_text(
        "3BATE/GREAT_GUO/RAIN_HE\n"  # vendor title line before BRDOUT:
        "BRDOUT: 4 100 100\n"
        "0 0\n100 0\n100 100\n0 100\n"
        "\n"
        "NETS: 1\n"
        "1 +3V3\n"
        "\n"
        "PARTS: 1\n"
        "R1 0 0 10 10 0 1\n"
        "\n"
        "PINS: 1\n"
        "5 5 1 1\n"
        "\n"
        "NAILS: 0\n"
    )
    board = GRParser().parse_file(f)
    assert board.source_format == "gr"
    assert [p.refdes for p in board.parts] == ["R1"]
    assert len(board.pins) == 1
    assert board.net_by_name("+3V3") is not None


def test_rejects_garbage_payload(tmp_path: Path):
    f = tmp_path / "nope.gr"
    f.write_text("hello world\n")
    with pytest.raises(InvalidBoardFile):
        GRParser().parse_file(f)
