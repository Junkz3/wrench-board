"""Decrypt FZ-xor container.

The cipher is RC6-shaped (Rivest-Robshaw-Sidney-Yin, 1998 — the AES
finalist; spec at <http://people.csail.mit.edu/rivest/pubs/RRSY98.pdf>)
applied per-byte over a rolling 16-byte ciphertext window:

  * State: four uint32 accumulators reloaded each iteration from a
    rolling 16-byte window of CIPHERTEXT bytes (so decryption is
    self-synchronising — corruption recovers within 16 bytes).
  * Per byte: add `K[0]` and `K[1]` into two accumulators, run 20
    Feistel-shaped mixing rounds that consume `K[2..41]`, then add
    `K[42]` and XOR the low byte of the resulting word into the
    ciphertext byte to recover plaintext.
  * After each byte the window slides left and the ciphertext byte
    occupies slot 15; the four accumulators are re-loaded as little-
    endian uint32s from the new window.

The 44 × uint32 expanded key is loaded at runtime from the
`FZ_RC6_SARRAY_HEX` environment variable (176 bytes, hex-encoded).
If the variable is unset, FZ-xor parsing is disabled and callers
receive a clear error message at parse time.
"""

from __future__ import annotations

import os
import struct


FZ_KEY_ENV = "WRENCH_BOARD_FZ_KEY"


def _load_key_words() -> tuple[int, ...] | None:
    """Load the 44 × uint32 expanded key from `WRENCH_BOARD_FZ_KEY`.

    Two formats are accepted for compatibility:
      * 176 bytes hex-encoded (preferred — single token, no whitespace)
      * 44 space-separated 32-bit integers (legacy)

    Returns `None` if the variable is unset or invalid; callers raise a
    clean error in that case. Aligns with the OpenBoardView convention
    of leaving cipher keys as runtime configuration.
    """
    raw = os.environ.get(FZ_KEY_ENV, "").strip()
    if not raw:
        return None
    if " " in raw or "," in raw:
        try:
            words = tuple(int(tok, 0) & 0xFFFFFFFF for tok in raw.replace(",", " ").split())
        except ValueError:
            return None
        return words if len(words) == 44 else None
    try:
        key_bytes = bytes.fromhex(raw)
    except ValueError:
        return None
    if len(key_bytes) != 176:
        return None
    return struct.unpack("<44I", key_bytes)


KEY_WORDS: tuple[int, ...] | None = _load_key_words()

_WINDOW = 16
_ROUNDS = 20


def _rol32(v: int, s: int) -> int:
    """Rotate a 32-bit unsigned value left by `s` bits.

    Mirrors C# `<<` on `uint`: only the low 5 bits of the count matter,
    and a count of 0 is the identity (avoids the undefined `>> 32`).
    """
    s &= 31
    if s == 0:
        return v & 0xFFFFFFFF
    return ((v << s) | (v >> (32 - s))) & 0xFFFFFFFF


class FZKeyNotConfigured(RuntimeError):
    """Raised when an FZ-xor file is encountered but no key is configured."""


def decrypt_fz_xor(cipher: bytes, key: tuple[int, ...] | None = None) -> bytes:
    """Return the plaintext for an XOR-flavoured `.fz` payload.

    The plaintext is the FZ-zlib container shape: 4-byte LE int32 holding
    the decompressed text length, followed by a zlib stream. Hand the
    result to `parse_fz_zlib` to finish the parse.
    """
    if key is None:
        key = KEY_WORDS
    if key is None:
        raise FZKeyNotConfigured(
            f"FZ-xor cipher key not configured. Set {FZ_KEY_ENV} in your .env "
            "(176-byte hex string, or 44 space-separated 32-bit ints) to enable "
            ".fz parsing."
        )
    if len(key) != 44:
        raise ValueError(f"FZ-xor key must be 44 uint32 words, got {len(key)}")
    K = key
    window = bytearray(_WINDOW)
    n5 = n4 = n3 = n2 = 0
    out = bytearray(len(cipher))
    for i, b in enumerate(cipher):
        n4 = (n4 + K[0]) & 0xFFFFFFFF
        n2 = (n2 + K[1]) & 0xFFFFFFFF
        for r in range(1, _ROUNDS + 1):
            t4 = (n4 * (((n4 << 1) + 1) & 0xFFFFFFFF)) & 0xFFFFFFFF
            mix4 = _rol32(t4, 5)
            t2 = (n2 * (((n2 << 1) + 1) & 0xFFFFFFFF)) & 0xFFFFFFFF
            mix2 = _rol32(t2, 5)
            new_n5 = (_rol32(n5 ^ mix4, mix2 & 0xFF) + K[r * 2]) & 0xFFFFFFFF
            new_n3 = (_rol32(n3 ^ mix2, mix4 & 0xFF) + K[r * 2 + 1]) & 0xFFFFFFFF
            # Rotate state: (n2, n3, n4, n5) ← (new_n5, old_n2, new_n3, old_n4)
            saved_n5 = new_n5
            n5 = n4
            n4 = new_n3
            n3 = n2
            n2 = saved_n5
        n5 = (n5 + K[42]) & 0xFFFFFFFF
        out[i] = b ^ (n5 & 0xFF)
        # Shift window left; new ciphertext byte at slot 15.
        window[: _WINDOW - 1] = window[1:_WINDOW]
        window[_WINDOW - 1] = b
        n5, n4, n3, n2 = struct.unpack_from("<4I", window, 0)
    return bytes(out)


def looks_like_fz_xor(raw: bytes) -> bool:
    """Heuristic dispatch: an XOR-flavoured file lacks the zlib magic at
    offset 4 (which signals the plain FZ-zlib container)."""
    if len(raw) < 8:
        return False
    return raw[4:6] not in (b"\x78\x9c", b"\x78\xda", b"\x78\x01")
