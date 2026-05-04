"""Tests for the TVW header substitution cipher."""
from __future__ import annotations

import pytest

from api.board.parser._tvw_engine.cipher import decode, encode


def test_special_chars_pass_through():
    """Non-alphanumeric characters are unchanged by the cipher."""
    for c in b"-. !@#$%^&*()_+":
        assert decode(bytes([c])) == chr(c)


def test_roundtrip_letters_and_digits():
    """Encoding then decoding returns the input across all character classes."""
    samples = [
        "Hello World",
        "abcXYZ123",
        "0123456789",
        "MixedCASE-with.punct",
        "  spaces  in  middle  ",
    ]
    for s in samples:
        assert decode(encode(s)) == s


def test_roundtrip_pseudo_random():
    """Round-trip a longer pseudo-random alphanumeric string. Catches any
    structural bug in the position-dependent table (off-by-one, wrong
    modulus, swapped row direction)."""
    sample = "".join(chr(0x41 + (i * 7) % 26) for i in range(80))
    sample += "".join(chr(0x30 + (i * 3) % 10) for i in range(80))
    assert decode(encode(sample)) == sample


def test_position_dependent():
    """Same input char at different positions decodes to different outputs."""
    # 'M' at pos 0 → 'R' (table row for 'M' starts RSTUVWXYZA, [0]='R')
    assert decode(b"M") == "R"          # pos 0
    assert decode(b"\x00M")[1] == "S"   # pos 1, prefix byte passes through unchanged
    assert decode(b"\x00\x00M")[2] == "T"  # pos 2


def test_digit_modulus_is_three():
    """Digits use a period-3 cycle; verify by decoding the same digit at
    positions 0, 1, 2, then 3 (which should equal pos 0 output)."""
    out_p0 = decode(b"0")[0]
    out_p1 = decode(b"\x00" + b"0")[1]
    out_p2 = decode(b"\x00\x00" + b"0")[2]
    out_p3 = decode(b"\x00\x00\x00" + b"0")[3]
    assert out_p3 == out_p0
    assert {out_p0, out_p1, out_p2} == {"e", "f", "g"}


def test_alpha_modulus_is_ten():
    """Latin letters use a period-10 cycle; verify by decoding 'A' at
    positions 0 and 10."""
    out_p0 = decode(b"A")[0]
    out_p10 = decode(b"\x00" * 10 + b"A")[10]
    assert out_p10 == out_p0


def test_empty_input():
    assert decode(b"") == ""
    assert encode("") == b""
