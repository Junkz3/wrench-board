"""HONHAN .bdv parser — decode round-trip + happy-path parse."""

from __future__ import annotations

from pathlib import Path

import pytest

from api.board.model import Layer
from api.board.parser.base import parser_for
from api.board.parser.bdv import BDVParser, _deobfuscate, _obfuscate

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_dispatches_bdv_extension(tmp_path: Path):
    f = tmp_path / "demo.bdv"
    f.write_bytes(b"anything")
    assert isinstance(parser_for(f), BDVParser)


@pytest.mark.parametrize(
    "text",
    [
        "hello world\n",
        "var_data: 0 1 1 0\nParts:\nR1 5 1\nPins:\n0 0 -99 1 +3V3\n",
        # Long enough to exercise the key wrap at 285 → 159 at least twice.
        "A" * 400 + "\n" + "B" * 400 + "\n",
    ],
)
def test_decode_of_encode_is_identity(text: str):
    assert _deobfuscate(_obfuscate(text)).decode("utf-8") == text


def test_cr_and_lf_pass_through_unchanged():
    """Line-break bytes must not advance the key counter — otherwise the
    keystream would desync with files that carry CRLF vs LF endings."""
    # Two equivalent plaintexts: one with LF, one with CRLF. The bodies
    # (non-newline bytes) must encode identically.
    lf = "abc\ndef\n"
    crlf = "abc\r\ndef\r\n"
    enc_lf = _obfuscate(lf)
    enc_crlf = _obfuscate(crlf)
    # Strip the \r / \n bytes (which encode to themselves) and compare
    # the non-newline payload — it should be identical in both.
    body_lf = bytes(b for b in enc_lf if b not in (10, 13))
    body_crlf = bytes(b for b in enc_crlf if b not in (10, 13))
    assert body_lf == body_crlf


def test_parses_minimal_bdv_fixture():
    board = BDVParser().parse_file(FIXTURE_DIR / "minimal.bdv")
    assert board.source_format == "bdv"
    assert len(board.parts) == 2
    assert len(board.pins) == 4
    assert len(board.nails) == 1
    assert board.part_by_refdes("R1").layer == Layer.TOP
    assert board.part_by_refdes("C1").layer == Layer.BOTTOM
    assert board.net_by_name("+3V3").is_power is True
    assert board.net_by_name("GND").is_ground is True


def test_fixture_is_genuinely_binary_not_ascii():
    """Guard: the committed fixture must actually be encoded — plain-ASCII
    regressions would silently make the parser look like it works without
    exercising the decoder."""
    raw = (FIXTURE_DIR / "minimal.bdv").read_bytes()
    # Most bytes should be outside the printable-ASCII range [32, 126].
    printable = sum(1 for b in raw if 32 <= b <= 126)
    assert printable < len(raw) / 2, "fixture reads as plaintext; decoder not exercised"
