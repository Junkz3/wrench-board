# SPDX-License-Identifier: Apache-2.0
"""`.fz` boardview parser — written from scratch.

Two real-world flavours of `.fz` exist in the repair community and
this parser dispatches between them:

1. **FZ-zlib** (most common in the field — Quanta, ASRock, ASUS Prime,
   Gigabyte, MSI). 4-byte LE int32 size header followed by a zlib
   stream that decompresses to a pipe-delimited (`!`) section format
   with `A!schema` / `S!data` rows. Implemented in `_fz_zlib.py`,
   verified against a real Quanta BKL boardview. No key required.

2. **FZ-xor** (ASUS PCB Repair Tool original). Test_Link-shape ASCII
   payload wrapped in a 16-byte sliding-window XOR cipher seeded by
   a 44×32-bit ASUS-shipped key. Implementation here exposes a
   symmetric cipher structure; real files require the matching
   ASUS key derivation. Falls back to `MissingFZKeyError` when no
   key is configured.

Dispatch: peek the first 6 bytes — if bytes 4-5 carry a zlib magic
(`78 9c` / `78 da` / `78 01`) we route to FZ-zlib (no key needed).
Otherwise we route to FZ-xor (key required).

Written from scratch against public format descriptions and direct
inspection of a real Quanta boardview. No code copied from any
external codebase.
"""

from __future__ import annotations

import os

from api.board.model import Board
from api.board.parser._ascii_boardview import DialectMarkers, parse_test_link_shape
from api.board.parser._fz_zlib import looks_like_fz_zlib, parse_fz_zlib
from api.board.parser.base import BoardParser, MissingFZKeyError, register

_ENV_KEY = "WRENCH_BOARD_FZ_KEY"
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
        # Variant dispatch: zlib-flavoured `.fz` files don't need a key.
        if looks_like_fz_zlib(raw):
            return parse_fz_zlib(
                raw, file_hash=file_hash, board_id=board_id, source_format="fz"
            )
        # Otherwise fall through to the XOR-cipher path (ASUS PCB Repair Tool).
        if self.key is None:
            raise MissingFZKeyError(
                f"fz: no zlib magic at offset 4 and no XOR decryption key "
                f"configured. Set {_ENV_KEY} (44 space-separated 32-bit ints) "
                f"or pass key=... to FZParser()."
            )
        plain = _decrypt(raw, self.key).decode("utf-8", errors="replace")
        return parse_test_link_shape(
            plain,
            markers=DialectMarkers(),
            source_format="fz",
            board_id=board_id,
            file_hash=file_hash,
        )
