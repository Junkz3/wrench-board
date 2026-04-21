"""Parser for OpenBoardView .brd (Test_Link) format."""

from pathlib import Path

import pytest

from api.board.model import Layer
from api.board.parser.base import (
    MalformedHeaderError,
    ObfuscatedFileError,
)
from api.board.parser.brd import BRDParser

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_parses_minimal_outline():
    board = BRDParser().parse_file(FIXTURE_DIR / "minimal.brd")
    assert board.board_id == "minimal"
    assert board.source_format == "brd"
    assert len(board.outline) == 4
    assert board.outline[0].x == 0
    assert board.outline[0].y == 0
    assert board.outline[2].x == 1000
    assert board.outline[2].y == 500


def test_rejects_obfuscated_file(tmp_path: Path):
    f = tmp_path / "obf.brd"
    # OBV obfuscation signature: 0x23 0xe2 0x63 0x28 at byte 0.
    f.write_bytes(b"\x23\xe2\x63\x28" + b"\x00" * 64)
    with pytest.raises(ObfuscatedFileError):
        BRDParser().parse_file(f)


def test_malformed_header_raises(tmp_path: Path):
    f = tmp_path / "bad.brd"
    f.write_text("str_length: 0\nvar_data: not-a-number 2 4 1\n")
    with pytest.raises(MalformedHeaderError):
        BRDParser().parse_file(f)


def test_parses_var_data_without_space_after_colon(tmp_path: Path):
    """Real-world .brd files sometimes omit the space between 'var_data:' and the first int."""
    f = tmp_path / "tight.brd"
    f.write_text("str_length: 0\nvar_data:4 0 0 0\nFormat:\n0 0\n10 0\n10 10\n0 10\n")
    board = BRDParser().parse_file(f)
    assert len(board.outline) == 4


def test_parses_parts_block_with_layer_bits():
    board = BRDParser().parse_file(FIXTURE_DIR / "minimal.brd")
    assert len(board.parts) == 2
    r1 = board.part_by_refdes("R1")
    c1 = board.part_by_refdes("C1")
    assert r1 is not None
    assert c1 is not None
    assert r1.layer == Layer.TOP
    assert r1.is_smd is True
    assert c1.layer == Layer.BOTTOM
    assert c1.is_smd is False  # type_layer 10 has bit 0x2 (bottom) without bit 0x4 (SMD)
