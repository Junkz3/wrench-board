# SPDX-License-Identifier: Apache-2.0
"""Tebo IctView .tvw parser — written from scratch.

**Reality check: there are two different TVW layouts in circulation.**

1. Some TVW redistributions carry a Test_Link-shape ASCII body wrapped
   in a symmetric per-character rotation cipher: digits rotate by 3
   within `0-9`, Latin letters rotate by 10 within their case class.
   Literal separators pass through untouched. This parser handles
   that layout — subtract the shift on decode, add it on encode.

2. The *production* `.tvw` format emitted by Tebo IctView 3.0 / 4.0
   is a **binary** container with little-endian integers, Pascal
   strings, layer sections (marker 0x33), D-code tables, and pad /
   line / arc / surface lists per layer. See the reverse-engineering
   notes at https://github.com/inflex/teboviewformat for the full
   `fileformat-tvw.txt`. This parser does **not** decode the binary
   layout — proper support requires walking the layer sections and
   is out of scope for a clean-room ASCII-helper-based parser.

When a binary-layout TVW file is uploaded, the first bytes decoded
through the rotation cipher will not spell any Test_Link marker —
the helper raises `InvalidBoardFile` with a specific "binary TVW"
hint so the user knows to look for a viewer that handles the binary
container. When a rotation-cipher TVW is uploaded, the parser
decodes and parses end-to-end.

Written from scratch — no code copied from any external codebase.
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


def _looks_binary_tvw(raw: bytes) -> bool:
    """Detect the production binary-layout TVW container.

    The rotation cipher maps every alphanumeric input byte to another
    alphanumeric in the same class, so a cipher-encoded plaintext
    Test_Link payload stays overwhelmingly printable-ASCII. The
    binary TVW container (per `fileformat-tvw.txt`) packs
    little-endian 32-bit integers, RGBA colour values, and Pascal-
    string length prefixes outside the printable range. Anything with
    more than ~35 % non-printable bytes in the first 2 KB is almost
    certainly the binary layout.

    Line-break bytes (`\n`, `\r`, `\t`) count as printable here — the
    rotation cipher preserves them, so their presence is neutral.
    """
    if not raw:
        return False
    sample = raw[: min(len(raw), 2048)]
    printable = sum(
        1 for b in sample if 32 <= b <= 126 or b in (9, 10, 13)
    )
    non_printable_ratio = 1.0 - (printable / len(sample))
    return non_printable_ratio > 0.35


@register
class TVWParser(BoardParser):
    extensions = (".tvw",)

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        if _looks_binary_tvw(raw):
            raise ObfuscatedFileError(
                "tvw: this file looks like the production binary-layout TVW "
                "container (Tebo IctView 3.0/4.0). Current parser only "
                "handles the rotation-cipher ASCII variant. See "
                "docs/superpowers/specs/2026-04-25-boardview-formats-v1.md "
                "for the scope rationale and the teboviewformat reference."
            )
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
