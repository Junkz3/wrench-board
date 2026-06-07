"""ATE BoardView .bv parser — happy path + dispatcher wiring + malformed guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from api.board.model import Layer
from api.board.parser.base import (
    InvalidBoardFile,
    MalformedHeaderError,
    parser_for,
)
from api.board.parser.bv import BVParser

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_dispatches_bv_extension(tmp_path: Path):
    f = tmp_path / "demo.bv"
    f.write_text("dummy")
    assert isinstance(parser_for(f), BVParser)


def test_parses_minimal_bv_fixture():
    board = BVParser().parse_file(FIXTURE_DIR / "minimal.bv")
    assert board.source_format == "bv"
    assert len(board.outline) == 4
    assert len(board.parts) == 2
    assert len(board.pins) == 4
    assert len(board.nails) == 1

    r1 = board.part_by_refdes("R1")
    assert r1 is not None and r1.layer == Layer.TOP and r1.is_smd is True
    c1 = board.part_by_refdes("C1")
    assert c1 is not None and c1.layer == Layer.BOTTOM

    gnd = board.net_by_name("GND")
    assert gnd is not None and gnd.is_ground is True
    v33 = board.net_by_name("+3V3")
    assert v33 is not None and v33.is_power is True


def test_banner_line_before_var_data_is_ignored():
    """The `BoardView 1.5` banner must not confuse the parser."""
    text = (
        "BoardView 1.5\n"
        "var_data: 0 1 1 0\n"
        "Parts:\n"
        "R1 5 1\n"
        "Pins:\n"
        "0 0 -99 1 NET1\n"
    )
    board = BVParser().parse(
        text.encode(), file_hash="sha256:x", board_id="b"
    )
    assert [p.refdes for p in board.parts] == ["R1"]


def test_rejects_unrelated_payload(tmp_path: Path):
    f = tmp_path / "bad.bv"
    f.write_text("this is not a boardview file at all\n")
    with pytest.raises(InvalidBoardFile):
        BVParser().parse_file(f)


def test_malformed_var_data_raises(tmp_path: Path):
    f = tmp_path / "bad2.bv"
    f.write_text("var_data: 1 not-an-int 2 0\nFormat:\n0 0\n")
    with pytest.raises(MalformedHeaderError):
        BVParser().parse_file(f)


# ---------------------------------------------------------------------------
# Binary JET4 (MS Access) `.bv` — the native ATE shape.
# ---------------------------------------------------------------------------

# jet4_min.bv is OUR OWN synthetic JET4 database (built by the engine's own page
# writer — not a third-party corpus file), holding a 2-part / 4-pin Pin table, a
# 1-row Nail table, and a 4-point Layout outline. It exercises the full
# binary-dispatch + table-mapping path that real exports use.


def test_parses_binary_jet4_bv_fixture():
    board = BVParser().parse_file(FIXTURE_DIR / "jet4_min.bv")
    assert board.source_format == "bv"
    assert {p.refdes for p in board.parts} == {"R1", "C1"}
    assert len(board.pins) == 4
    assert len(board.outline) == 4
    assert len(board.nails) == 1

    # Side mapping: (T) -> TOP, (B) -> BOTTOM.
    assert board.part_by_refdes("R1").layer == Layer.TOP
    assert board.part_by_refdes("C1").layer == Layer.BOTTOM

    # Power / ground classification rides the shared net heuristics.
    gnd = board.net_by_name("GND")
    assert gnd is not None and gnd.is_ground is True
    v33 = board.net_by_name("+3V3")
    assert v33 is not None and v33.is_power is True


def test_binary_jet4_dispatches_through_extension(tmp_path: Path):
    # A binary JET4 payload routed through the public parser entry point.
    raw = (FIXTURE_DIR / "jet4_min.bv").read_bytes()
    f = tmp_path / "demo.bv"
    f.write_bytes(raw)
    board = parser_for(f).parse_file(f)
    assert board.source_format == "bv"
    assert len(board.parts) == 2


def test_rejects_non_jet4_binary(tmp_path: Path):
    # Binary-looking but NOT the Standard Jet DB shape -> clear ObfuscatedFileError,
    # never a silent empty Board.
    from api.board.parser.base import ObfuscatedFileError

    f = tmp_path / "weird.bv"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8)
    with pytest.raises(ObfuscatedFileError):
        BVParser().parse_file(f)
