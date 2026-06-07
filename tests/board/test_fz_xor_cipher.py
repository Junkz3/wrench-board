"""Tests for the FZ-xor cipher engine.

Round-trip the cipher against a synthetic plaintext to lock down the
round structure. The cipher round-trip is enough to catch regressions —
a real `.fz` integration test (when fixtures are available) lives in
`board_assets/`. Tests use a synthetic test key (`TEST_KEY` below); the
production key is loaded at runtime from the `WRENCH_BOARD_FZ_KEY`
environment variable and is never embedded in the repo.
"""

from __future__ import annotations

import struct
import zlib

import pytest

from api.board.parser._fz_engine.cipher import (
    FZKeyNotConfigured,
    decrypt_fz_xor,
    looks_like_fz_xor,
)

# Deterministic synthetic key for round-trip tests. Any 44-uint32 tuple
# is a valid input to the cipher — the cipher is invertible regardless
# of key value.
TEST_KEY: tuple[int, ...] = tuple((i * 0x9E3779B9) & 0xFFFFFFFF for i in range(1, 45))


def _encrypt(plain: bytes, key: tuple[int, ...] = TEST_KEY) -> bytes:
    """Encrypt — inverse of decrypt_fz_xor.

    Symmetry note: the per-byte scheme XORs a keystream byte derived from
    state-then-window-of-ciphertext. Because the window absorbs the
    ciphertext byte (not the plaintext), encryption and decryption are NOT
    a simple `xor(input, keystream)` swap — the encoder must use the byte
    it just emitted to slide the window. That's what this routine does.
    """
    from api.board.parser._fz_engine.cipher import _rol32

    K = key
    window = bytearray(16)
    n5 = n4 = n3 = n2 = 0
    out = bytearray(len(plain))
    for i, p in enumerate(plain):
        n4 = (n4 + K[0]) & 0xFFFFFFFF
        n2 = (n2 + K[1]) & 0xFFFFFFFF
        for r in range(1, 21):
            t4 = (n4 * (((n4 << 1) + 1) & 0xFFFFFFFF)) & 0xFFFFFFFF
            mix4 = _rol32(t4, 5)
            t2 = (n2 * (((n2 << 1) + 1) & 0xFFFFFFFF)) & 0xFFFFFFFF
            mix2 = _rol32(t2, 5)
            new_n5 = (_rol32(n5 ^ mix4, mix2 & 0xFF) + K[r * 2]) & 0xFFFFFFFF
            new_n3 = (_rol32(n3 ^ mix2, mix4 & 0xFF) + K[r * 2 + 1]) & 0xFFFFFFFF
            saved_n5 = new_n5
            n5 = n4
            n4 = new_n3
            n3 = n2
            n2 = saved_n5
        n5 = (n5 + K[42]) & 0xFFFFFFFF
        c = p ^ (n5 & 0xFF)
        out[i] = c
        window[:15] = window[1:]
        window[15] = c
        n5, n4, n3, n2 = struct.unpack_from("<4I", window, 0)
    return bytes(out)


def test_test_key_is_44_uint32():
    assert len(TEST_KEY) == 44
    assert all(0 <= w <= 0xFFFFFFFF for w in TEST_KEY)


def test_decrypt_roundtrip_short():
    plain = b"hello world"
    cipher = _encrypt(plain)
    assert cipher != plain
    assert decrypt_fz_xor(cipher, TEST_KEY) == plain


def test_decrypt_roundtrip_zero_buffer():
    # All-zero plaintext exposes the keystream itself.
    plain = bytes(64)
    cipher = _encrypt(plain)
    assert decrypt_fz_xor(cipher, TEST_KEY) == plain


def test_decrypt_roundtrip_random_payload():
    # 1024 bytes of varied content — exercises the window after it's
    # been fully populated and rolls past the initial all-zero state.
    plain = bytes((i * 17 + 3) & 0xFF for i in range(1024))
    cipher = _encrypt(plain)
    assert decrypt_fz_xor(cipher, TEST_KEY) == plain


def test_decrypt_empty_is_empty():
    assert decrypt_fz_xor(b"", TEST_KEY) == b""


def test_decrypt_round_trips_with_zlib_container():
    """End-to-end: build a valid FZ-zlib container in plaintext, encrypt,
    then verify decrypt + zlib gives the original text."""
    text = "A!REFDES!COMP_INSERTION_CODE!\nS!R1!1!RES!NO!0!\n"
    body = zlib.compress(text.encode())
    container = struct.pack("<I", len(text)) + body  # plaintext shape
    cipher = _encrypt(container)
    recovered = decrypt_fz_xor(cipher, TEST_KEY)
    assert recovered == container
    # the looks-like-fz-zlib helper accepts the recovered plaintext
    assert recovered[4:6] in (b"\x78\x9c", b"\x78\xda", b"\x78\x01")


def test_looks_like_fz_xor_rejects_zlib_magic():
    # 78 9c at offset 4 → zlib variant, not xor.
    assert not looks_like_fz_xor(b"\x00\x00\x00\x00\x78\x9c\x00\x00")
    assert looks_like_fz_xor(b"\xea\xf0\xf2\x9d\xca\xae\x3d\x67")
    assert not looks_like_fz_xor(b"")  # too short


def test_decrypt_with_wrong_key_does_not_match():
    plain = b"hello world this is plaintext"
    cipher = _encrypt(plain)
    bogus_key = tuple([0] * 44)
    with_bogus = decrypt_fz_xor(cipher, bogus_key)
    assert with_bogus != plain


def test_decrypt_rejects_non_44_word_key():
    with pytest.raises(ValueError):
        decrypt_fz_xor(b"abc", key=(0,) * 10)


def test_decrypt_raises_when_no_key_configured(monkeypatch):
    """When neither an explicit key nor the env var is set, decryption
    raises a clear `FZKeyNotConfigured` error rather than silently
    failing or using a hardcoded default."""
    monkeypatch.delenv("WRENCH_BOARD_FZ_KEY", raising=False)
    # Force module-level KEY_WORDS to None for this call.
    import api.board.parser._fz_engine.cipher as cipher_mod
    monkeypatch.setattr(cipher_mod, "KEY_WORDS", None)
    with pytest.raises(FZKeyNotConfigured, match="WRENCH_BOARD_FZ_KEY"):
        decrypt_fz_xor(b"\x00\x00\x00\x00\xea\xf0")
