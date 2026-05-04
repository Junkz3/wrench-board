"""Tebo IctView `.tvw` parser.

**Two TVW layouts coexist in circulation, both supported here.**

1. Some redistributions carry a Test_Link-shape ASCII body wrapped in a
   symmetric per-character rotation cipher: digits rotate by 3 within
   `0-9`, Latin letters rotate by 10 within their case class. Literal
   separators pass through untouched. This parser decodes the cipher
   then hands off to the shared Test_Link ASCII shape walker.

2. The *production* `.tvw` format emitted by Tebo IctView 3.0 / 4.0 is
   a **binary** container with little-endian integers, Pascal-prefixed
   strings, per-layer aperture (D-code) tables, and per-layer placement
   records. We dispatch this flavour to `_tvw_engine` which produces a
   dimensionally-accurate `Board` with synthetic refdes (`PAD_NNN`) —
   Tebo IctView does not store netlist info at file level so connectivity
   must be inferred later.

Dispatch: a 3-Pascal-string magic at the head of the file (see
`_tvw_engine/magic.py`) routes the production-binary path; otherwise
we apply the rotation cipher and try the Test_Link walker.
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
        # Production binary first — magic detection is more discriminating
        # than the non-printable-byte heuristic.
        from api.board.parser._tvw_engine.board_mapper import to_board
        from api.board.parser._tvw_engine.magic import is_production_binary
        from api.board.parser._tvw_engine.walker import parse as walk_tvw

        if is_production_binary(raw):
            tvw_file = walk_tvw(raw)
            return to_board(tvw_file, board_id=board_id, file_hash=file_hash)

        # Fallback path 1: rotation-cipher ASCII variant.
        if _looks_binary_tvw(raw):
            raise ObfuscatedFileError(
                "tvw: looks like a binary-layout TVW container but does not "
                "match the Tebo IctView production-binary magic. Unknown TVW "
                "variant; the rotation-cipher ASCII parser cannot decode "
                "binary containers. See docs/superpowers/specs/"
                "2026-04-25-boardview-formats-v1.md."
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
