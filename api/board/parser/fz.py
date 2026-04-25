# SPDX-License-Identifier: Apache-2.0
"""ASUS PCB Repair Tool .fz parser — written from scratch.

`.fz` files ship a Test_Link-shape ASCII payload wrapped in an XOR
stream cipher seeded by a 44 × 32-bit key that ASUS distributes
separately. Without the key the file cannot be decoded — this is the
same situation every open-source reader (OpenBoardView, FlexBV) runs
into. We do two things:

1. Expose a clean descrambling structure: a 16-byte sliding cleartext
   window feeds a keystream byte via the first four window bytes mixed
   with the rotating 44-word key schedule. The operation is symmetric
   (encrypt == decrypt with the same key).
2. When no key is configured, raise `MissingFZKeyError` with a clear
   explanation. Users who have a legitimate key pass it via the
   `MICROSOLDER_FZ_KEY` environment variable (space-separated 32-bit
   ints, decimal or hex) or directly via `FZParser(key=tuple_of_44)`.

The exact keystream derivation used by ASUS is not publicly specified.
This implementation provides the structure and a symmetric cipher that
round-trips cleanly on synthetic data. Real ASUS `.fz` files will
decode correctly only when the shipped ASUS key schedule matches this
keystream derivation — in practice that requires the same
reverse-engineered algorithm used by the community decoder the user
already trusts. This code is written from scratch against the public
structural description (see OpenBoardView issue #10 discussion); no
code was copied.
"""

from __future__ import annotations

import os

from api.board.model import Board
from api.board.parser._ascii_boardview import DialectMarkers, parse_test_link_shape
from api.board.parser.base import BoardParser, MissingFZKeyError, register

_ENV_KEY = "MICROSOLDER_FZ_KEY"
_KEY_WORDS = 44  # 44 × 32-bit, per the ASUS format
_WINDOW_BYTES = 16


def _load_env_key() -> tuple[int, ...] | None:
    raw = os.environ.get(_ENV_KEY, "").strip()
    if not raw:
        return None
    try:
        words = tuple(int(tok, 0) & 0xFFFFFFFF for tok in raw.split())
    except ValueError:
        return None
    if len(words) != _KEY_WORDS:
        return None
    return words


def _init_window(key: tuple[int, ...]) -> bytearray:
    """Fill the 16-byte window from the first four key words, little-endian."""
    out = bytearray(_WINDOW_BYTES)
    for i in range(4):
        w = key[i]
        out[i * 4 + 0] = w & 0xFF
        out[i * 4 + 1] = (w >> 8) & 0xFF
        out[i * 4 + 2] = (w >> 16) & 0xFF
        out[i * 4 + 3] = (w >> 24) & 0xFF
    return out


def _keystream_byte(window: bytearray, key: tuple[int, ...], i: int) -> int:
    """Derive one keystream byte from the window + rotating key word."""
    a = window[0] | (window[1] << 8) | (window[2] << 16) | (window[3] << 24)
    mixed = (a ^ key[i % _KEY_WORDS]) & 0xFFFFFFFF
    return mixed & 0xFF


def _decrypt(data: bytes, key: tuple[int, ...]) -> bytes:
    """XOR-decrypt `data`. Window absorbs the decrypted (cleartext) byte."""
    window = _init_window(key)
    out = bytearray()
    for i, b in enumerate(data):
        ks = _keystream_byte(window, key, i)
        clear = b ^ ks
        out.append(clear)
        window[:15] = window[1:]
        window[15] = clear
    return bytes(out)


def _encrypt(data: bytes, key: tuple[int, ...]) -> bytes:
    """Encrypt `data` (cleartext). Symmetric counterpart — for tests only."""
    window = _init_window(key)
    out = bytearray()
    for i, b in enumerate(data):
        ks = _keystream_byte(window, key, i)
        out.append(b ^ ks)
        window[:15] = window[1:]
        window[15] = b  # cleartext drives the window during encrypt
    return bytes(out)


@register
class FZParser(BoardParser):
    extensions = (".fz",)

    def __init__(self, key: tuple[int, ...] | None = None):
        if key is None:
            key = _load_env_key()
        if key is not None and len(key) != _KEY_WORDS:
            key = None
        self.key = key

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        if self.key is None:
            raise MissingFZKeyError(
                f"fz: no decryption key configured. Set {_ENV_KEY} "
                f"(44 space-separated 32-bit ints) or pass key=... to FZParser()."
            )
        plain = _decrypt(raw, self.key).decode("utf-8", errors="replace")
        return parse_test_link_shape(
            plain,
            markers=DialectMarkers(),
            source_format="fz",
            board_id=board_id,
            file_hash=file_hash,
        )
