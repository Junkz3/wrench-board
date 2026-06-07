"""Substitution-encoded `.brd` parser — substitution round-trip + happy-path parse.

The encoding is a fixed byte-for-byte substitution (see api/board/parser/brd_subst
for how the table was derived). The fixture is synthesized from known plaintext
via the inverse table, so the round-trip is guaranteed by construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api.board.model import Layer
from api.board.parser.base import InvalidBoardFile, parser_for
from api.board.parser.brd_subst import (
    SubstEncodedBoardParser,
    _extract_outline,
    decode_subst,
    encode_subst,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
_MAGIC = b"\x23\xe2\x63\x28"


def test_extract_outline_picks_longest_coord_run_and_collapses_dups():
    # The outline is the longest contiguous block of bare "X Y" pairs. A lone
    # count pair, the 3-field part rows and 5-field nail rows must be ignored;
    # repeated shared vertices collapse to one.
    lines = [
        "5 28",  # stray count pair (run of 1 — must not win)
        "PARTS#",
        "R1 9 1",  # 3-field part row
        "OUTLINE#",
        "0 0",
        "100 0",
        "100 0",  # duplicate shared vertex -> collapsed
        "100 50",
        "0 50",
        "NAILS#",
        "10 20 1 1 GND",  # 5-field nail row
    ]
    pts = _extract_outline(lines)
    assert [(p.x, p.y) for p in pts] == [(0, 0), (100, 0), (100, 50), (0, 50)]


def test_extract_outline_returns_empty_without_a_real_run():
    # No run of >=4 coordinate pairs -> no board edge.
    assert _extract_outline(["5 28", "PARTS#", "R1 9 1", "1 2"]) == []


def test_dispatches_subst_magic(tmp_path: Path):
    f = tmp_path / "demo.brd"
    f.write_bytes(_MAGIC + b"anything")
    assert isinstance(parser_for(f), SubstEncodedBoardParser)


@pytest.mark.parametrize(
    "text",
    [
        "GND\r\n",
        "C100      9     1    \r\nNAILS#\r\n100 200 1 1 PP3V3\r\n",
        "PP3V3_A4\r\nGND\r\nU1234\r\n",
    ],
)
def test_decode_of_encode_is_identity(text: str):
    """The substitution is a bijection over the boardview alphabet, so
    decode(encode(x)) == x for any text built from that alphabet."""
    assert decode_subst(encode_subst(text)).decode("latin-1") == text


def test_cr_and_lf_pass_through_unchanged():
    """Line-break bytes must not be substituted — the grammar is line-based and
    CRLF endings have to survive the encoding verbatim."""
    enc = encode_subst("abc\r\ndef\n")
    assert enc[3:5] == b"\r\n"
    assert enc.count(b"\n") == 2
    assert enc.count(b"\r") == 1


def test_parses_minimal_subst_fixture():
    board = SubstEncodedBoardParser().parse_file(FIXTURE_DIR / "brd_subst_min.brd")
    assert board.source_format == "brd-subst"

    # Parts table: refdes + layer (odd layer code -> TOP, even -> BOTTOM).
    refdes = {p.refdes for p in board.parts}
    assert {"C100", "R200", "U300"} <= refdes
    assert board.part_by_refdes("C100").layer == Layer.TOP  # code 9 (odd)
    assert board.part_by_refdes("R200").layer == Layer.BOTTOM  # code 10 (even)

    # Nail rows -> pins with positions + nets + probe + side-derived layer.
    assert len(board.pins) == 4
    gnd_pin = next(p for p in board.pins if p.net == "GND")
    assert (gnd_pin.pos.x, gnd_pin.pos.y) == (100, 200)
    assert gnd_pin.probe == 1
    assert gnd_pin.layer == Layer.TOP  # side 1

    # Nets with power/ground classification.
    assert board.net_by_name("GND").is_ground is True
    assert board.net_by_name("PP3V3").is_power is True
    assert board.net_by_name("PP1V8").is_power is True
    assert board.net_by_name("SIGNAL_A").is_power is False
    assert board.net_by_name("SIGNAL_A").is_ground is False


def test_bad_magic_is_rejected():
    with pytest.raises(InvalidBoardFile):
        SubstEncodedBoardParser().parse(b"NOTMAGIC\r\nGND\r\n", file_hash="h", board_id="x")


def test_power_rail_prefix_classified():
    """Some rails use the PP / + prefixes; the parser flags them power even
    when they fall outside the generic VCC/VDD regex."""
    plain = "NAILS#\r\n0 0 1 1 PPBUS_G3H\r\n10 10 2 1 PPDCIN\r\n"
    enc = _MAGIC + encode_subst(plain)
    board = SubstEncodedBoardParser().parse(enc, file_hash="h", board_id="x")
    assert board.net_by_name("PPBUS_G3H").is_power is True
    assert board.net_by_name("PPDCIN").is_power is True
