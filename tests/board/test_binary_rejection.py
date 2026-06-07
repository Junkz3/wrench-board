"""Each speculative-ASCII parser must reject obvious binary payloads.

`.bv`, `.gr`, `.cst`, `.f2b`, `.cad` (Test_Link fallback path), and
`.tvw` all assume a Test_Link-shape ASCII grammar — a guess based on
public format catalog entries since none of these vendors ships a
documented file-format spec. Real production files are likely binary
containers. Until we have actual samples, the contract is: parse
Test_Link-shape ASCII when present, raise `ObfuscatedFileError`
with a clear hint when the payload is clearly binary, never silently
emit a half-empty Board on garbage input.

This file pins that contract explicitly with a generated binary blob
that no Test_Link-shape parser could honestly read.
"""

from __future__ import annotations

import pytest

from api.board.parser.base import ObfuscatedFileError
from api.board.parser.bv import BVParser
from api.board.parser.cad import CADParser
from api.board.parser.cst import CSTParser
from api.board.parser.f2b import F2BParser
from api.board.parser.gr import GRParser
from api.board.parser.tvw import TVWParser


def _binary_blob(size: int = 1024) -> bytes:
    """Generate a plausible binary container head: a Pascal string,
    LE int32s, RGBA colour bytes, and high-entropy padding."""
    head = bytes([8] + list(b"BoardVwr"))   # Pascal string
    head += bytes([0x00, 0x00, 0x01, 0x00])  # LE int32 = 256
    head += bytes([0xFF, 0x80, 0x40, 0xC0])  # RGBA-ish
    head += bytes([0x33, 0xA1, 0xB7, 0x52])  # section marker + entropy
    # Pad with bytes outside the printable ASCII range to push the
    # non-printable ratio over the 30 % threshold.
    pad = bytes(b for b in range(128, 256)) * (1 + size // 128)
    return (head + pad)[:size]


@pytest.mark.parametrize(
    "parser_cls,ext,name_in_msg",
    [
        (BVParser, ".bv", "ATE BoardView"),
        (GRParser, ".gr", "BoardView R5.0"),
        (CSTParser, ".cst", ".cst"),
        (F2BParser, ".f2b", "vendor"),
        (CADParser, ".cad", ".cad"),
        (TVWParser, ".tvw", "TVW"),
    ],
)
def test_binary_payload_is_rejected_with_clear_hint(parser_cls, ext, name_in_msg):
    raw = _binary_blob(2048)
    with pytest.raises(ObfuscatedFileError) as exc:
        parser_cls().parse(raw, file_hash="sha256:x", board_id="x")
    msg = str(exc.value)
    assert "binary" in msg.lower(), f"{ext}: error message missing 'binary' hint: {msg}"


def test_binary_blob_threshold_does_not_false_positive_on_dense_ascii():
    """A long string of printable ASCII (e.g. base64-ish output) must NOT
    trip the binary heuristic — that would block legitimate uploads."""
    from api.board.parser._ascii_boardview import looks_like_binary

    dense_ascii = (
        "var_data: 0 1 1 0\nParts:\nR1 5 1\nPins:\n0 0 -99 1 +3V3\n"
        + "Z" * 4000
    ).encode()
    assert looks_like_binary(dense_ascii) is False


def test_cad_brd2_path_is_not_blocked_by_binary_detector():
    """The real BRD2 sniff branch in CADParser must run BEFORE binary
    detection — a `.cad` with `BRDOUT:` is plain ASCII anyway, but we
    pin the dispatch order so a future change can't accidentally
    reject BRD2 uploads as 'binary'."""
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "brd2_form.cad"
    board = CADParser().parse_file(fixture)
    assert board.source_format == "cad"
    assert len(board.parts) == 1
