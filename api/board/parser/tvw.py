# SPDX-License-Identifier: Apache-2.0
"""Tebo IctView .tvw parser — written from scratch.

Tebo IctView (versions 3.0 and 4.0) wraps the Test_Link ASCII grammar
in a symmetric per-character rotation cipher: digits rotate by 3
within `0-9`, Latin letters rotate by 10 within their case class.
Literal separators (`-`, `.`, space, `+`, colon, slash, etc.) pass
through untouched. On decode we subtract the shift; on encode we
add it.

The cipher is documented publicly (reverse-engineering notes at
https://github.com/inflex/teboviewformat and EDA forum discussions).
This implementation is written from scratch — no code was copied.

Tebo files in the wild sometimes pair the cipher with additional
obfuscation on the net-name block or on a per-version prelude. When
this parser succeeds in decoding the ASCII but `parse_test_link_shape`
fails to recognise any block markers, the caller receives a plain
`InvalidBoardFile` from the helper — the symptom for the user is
"your `.tvw` looks like a variant we don't yet read", which is
honest and recoverable.
"""

from __future__ import annotations

from api.board.model import Board
from api.board.parser._ascii_boardview import DialectMarkers, parse_test_link_shape
from api.board.parser.base import BoardParser, ObfuscatedFileError, register

_DIGIT_SHIFT = 3
_ALPHA_SHIFT = 10


def _rot_digit(c: int, shift: int) -> int:
    return ((c - 0x30 + shift) % 10) + 0x30


def _rot_lower(c: int, shift: int) -> int:
    return ((c - 0x61 + shift) % 26) + 0x61


def _rot_upper(c: int, shift: int) -> int:
    return ((c - 0x41 + shift) % 26) + 0x41


def _apply(raw: bytes, dsign: int, asign: int) -> bytes:
    """Apply digit/alpha shifts. `dsign`/`asign` is +1 for encode, -1 for decode."""
    out = bytearray()
    for b in raw:
        if 0x30 <= b <= 0x39:
            out.append(_rot_digit(b, dsign * _DIGIT_SHIFT))
        elif 0x61 <= b <= 0x7A:
            out.append(_rot_lower(b, asign * _ALPHA_SHIFT))
        elif 0x41 <= b <= 0x5A:
            out.append(_rot_upper(b, asign * _ALPHA_SHIFT))
        else:
            out.append(b)
    return bytes(out)


def _deobfuscate(raw: bytes) -> bytes:
    return _apply(raw, dsign=-1, asign=-1)


def _obfuscate(text: str) -> bytes:
    """Encoder — used by tests to synthesize fixtures."""
    return _apply(text.encode("utf-8"), dsign=+1, asign=+1)


@register
class TVWParser(BoardParser):
    extensions = (".tvw",)

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        try:
            plain = _deobfuscate(raw).decode("utf-8", errors="replace")
        except Exception as exc:  # pragma: no cover — defensive
            raise ObfuscatedFileError(f"tvw: decoding failed ({exc})") from exc
        return parse_test_link_shape(
            plain,
            markers=DialectMarkers(),
            source_format="tvw",
            board_id=board_id,
            file_hash=file_hash,
        )
