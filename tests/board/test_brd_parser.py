"""Parser for OpenBoardView .brd (Test_Link) format."""

from pathlib import Path

import pytest

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
