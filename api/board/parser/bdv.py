# SPDX-License-Identifier: Apache-2.0
"""HONHAN BoardViewer .bdv parser — written from scratch.

HONHAN wraps the Test_Link ASCII grammar in a trivial arithmetic
obfuscation layer. On decode: `clear = (key - cipher) & 0xFF`, where
`key` starts at 160, increments by 1 for every decoded byte, and wraps
from 286 back to 159. Line-break bytes (`\r` = 13, `\n` = 10) pass
through unchanged and do not advance the counter — this preserves the
line grammar of the underlying boardview payload.

After deobfuscation the bytes carry plaintext Test_Link-shape ASCII,
which we route through the shared `parse_test_link_shape` helper.

The obfuscation algorithm itself is documented publicly (e.g. piernov's
2018 reverse-engineering gist). This implementation is written from
scratch — no code was copied.
"""

from __future__ import annotations

from api.board.model import Board
from api.board.parser._ascii_boardview import DialectMarkers, parse_test_link_shape
from api.board.parser.base import BoardParser, ObfuscatedFileError, register

_KEY_START = 160
_KEY_RESET = 159
_KEY_MAX = 285  # after increment past this, wrap to _KEY_RESET


def _deobfuscate(raw: bytes) -> bytes:
    """Invert the HONHAN arithmetic cipher.

    The cipher is symmetric — `encode == decode` with the same key
    schedule. We keep this as the public decode path; the inverse is
    exposed via `_obfuscate` for test fixtures only.
    """
    out = bytearray()
    key = _KEY_START
    for b in raw:
        if b in (10, 13):
            out.append(b)
            continue
        out.append((key - b) & 0xFF)
        key = _KEY_RESET if key >= _KEY_MAX else key + 1
    return bytes(out)


def _obfuscate(text: str) -> bytes:
    """Encode plaintext ASCII into the `.bdv` arithmetic cipher.

    Used only by tests that need to synthesize a fixture — runtime
    never calls this. Kept next to the decoder so both halves of the
    round-trip live in one place and stay in sync.
    """
    data = text.encode("utf-8")
    out = bytearray()
    key = _KEY_START
    for c in data:
        if c in (10, 13):
            out.append(c)
            continue
        out.append((key - c) & 0xFF)
        key = _KEY_RESET if key >= _KEY_MAX else key + 1
    return bytes(out)


@register
class BDVParser(BoardParser):
    extensions = (".bdv",)

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        try:
            plain = _deobfuscate(raw).decode("utf-8", errors="replace")
        except Exception as exc:  # pragma: no cover — defensive
            raise ObfuscatedFileError(f"bdv: decoding failed ({exc})") from exc
        return parse_test_link_shape(
            plain,
            markers=DialectMarkers(),
            source_format="bdv",
            board_id=board_id,
            file_hash=file_hash,
        )
