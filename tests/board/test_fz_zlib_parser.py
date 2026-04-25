"""Tests for the FZ-zlib variant — the most common real-world `.fz` layout
(Quanta, ASRock, ASUS Prime, Gigabyte boards). Synthetic fixtures only;
real-world files are exercised by `test_real_files_runner.py`."""

from __future__ import annotations

import struct
import zlib

import pytest

from api.board.model import Layer
from api.board.parser._fz_zlib import looks_like_fz_zlib, parse_fz_zlib
from api.board.parser.base import InvalidBoardFile, MalformedHeaderError
from api.board.parser.fz import FZParser


def _wrap_fz_zlib(text: str) -> bytes:
    """Wrap plaintext as a real FZ-zlib container: 4-byte LE size + zlib body."""
    body = text.encode("utf-8")
    compressed = zlib.compress(body)
    header = struct.pack("<I", len(body))
    return header + compressed


_PLAINTEXT = """A!REFDES!COMP_INSERTION_CODE!SYM_NAME!SYM_MIRROR!SYM_ROTATE!
S!R1!1!RES_0402!NO!0!
S!C1!1!CAP_0402!YES!90!
S!U1!1!QFN32!NO!180!
A!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!PIN_X!PIN_Y!TEST_POINT!RADIUS!
S!+3V3!R1!0!1!100!200!!10!
S!GND!R1!0!2!200!200!!10!
S!+3V3!C1!0!1!300.5!400.5!!10!
S!GND!C1!0!2!400!400!!10!
S!CLK!U1!0!1!500!500!!10!
S!DATA!U1!0!2!600!500!!10!
A!TESTVIA!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!VIA_X!VIA_Y!TEST_POINT!RADIUS!
S!1!+3V3!R1!0!1!100!200!!10!
"""


def test_looks_like_fz_zlib_detects_zlib_magic_at_offset_4():
    raw = _wrap_fz_zlib("hello")
    assert looks_like_fz_zlib(raw) is True
    # Reject random garbage
    assert looks_like_fz_zlib(b"random text without zlib") is False


def test_parses_synthetic_fz_zlib():
    raw = _wrap_fz_zlib(_PLAINTEXT)
    board = parse_fz_zlib(raw, file_hash="sha256:x", board_id="demo")
    assert board.source_format == "fz"
    assert [p.refdes for p in board.parts] == ["R1", "C1", "U1"]
    assert len(board.pins) == 6

    # Layer mapping: SYM_MIRROR YES → BOTTOM, NO → TOP
    assert board.part_by_refdes("R1").layer == Layer.TOP
    assert board.part_by_refdes("C1").layer == Layer.BOTTOM
    assert board.part_by_refdes("U1").layer == Layer.TOP

    # Footprint enrichment from SYM_NAME
    assert board.part_by_refdes("R1").footprint == "RES_0402"
    assert board.part_by_refdes("U1").rotation_deg == 180.0

    # Float pin positions are rounded to int
    c1_pin1 = next(pin for pin in board.pins if pin.part_refdes == "C1" and pin.index == 1)
    assert c1_pin1.pos.x in (300, 301)  # 300.5 rounds to nearest even/up
    assert c1_pin1.pos.y in (400, 401)

    # Power/ground classification
    assert board.net_by_name("+3V3").is_power is True
    assert board.net_by_name("GND").is_ground is True

    # 1 nail from TESTVIA
    assert len(board.nails) == 1


def test_fz_dispatcher_routes_zlib_variant_without_key():
    """FZParser() without a key still parses zlib-flavoured `.fz`."""
    raw = _wrap_fz_zlib(_PLAINTEXT)
    board = FZParser().parse(raw, file_hash="sha256:x", board_id="x")
    assert board.source_format == "fz"
    assert len(board.parts) == 3


def test_invalid_zlib_body_raises_clean_error():
    """A 4-byte header but garbage zlib stream must surface a clear error,
    not a Python zlib traceback."""
    bad = struct.pack("<I", 100) + b"this is not zlib"
    # Should fall through dispatcher to xor path → MissingFZKeyError, but
    # parse_fz_zlib called directly raises InvalidBoardFile.
    with pytest.raises(InvalidBoardFile):
        parse_fz_zlib(bad, file_hash="sha256:x", board_id="x")


def test_missing_required_section_raises_malformed():
    """A zlib body without REFDES or NET_NAME schemas must raise."""
    raw = _wrap_fz_zlib("A!OTHER!a!b!\nS!x!y!z!\n")
    with pytest.raises(MalformedHeaderError):
        parse_fz_zlib(raw, file_hash="sha256:x", board_id="x")


def test_orphan_pin_referencing_unknown_refdes_is_dropped():
    """A pin row whose REFDES isn't in the parts section must be silently
    dropped — we never fabricate a part to satisfy a pin (anti-hallucination)."""
    text = (
        "A!REFDES!COMP_INSERTION_CODE!SYM_NAME!SYM_MIRROR!SYM_ROTATE!\n"
        "S!R1!1!FOO!NO!0!\n"
        "A!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!PIN_X!PIN_Y!TEST_POINT!RADIUS!\n"
        "S!+3V3!R1!0!1!10!10!!1!\n"
        "S!+5V!UNKNOWN_PART!0!1!50!50!!1!\n"
    )
    board = parse_fz_zlib(_wrap_fz_zlib(text), file_hash="sha256:x", board_id="x")
    assert len(board.parts) == 1
    assert len(board.pins) == 1


def test_pin_index_falls_back_to_sequential_when_pin_name_alphanumeric():
    """PIN_NAME like 'A1' → fallback to 1-based sequential per part."""
    text = (
        "A!REFDES!COMP_INSERTION_CODE!SYM_NAME!SYM_MIRROR!SYM_ROTATE!\n"
        "S!U1!1!BGA!NO!0!\n"
        "A!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!PIN_X!PIN_Y!TEST_POINT!RADIUS!\n"
        "S!N1!U1!0!A1!0!0!!1!\n"
        "S!N2!U1!0!A2!10!0!!1!\n"
        "S!N3!U1!0!B1!0!10!!1!\n"
    )
    board = parse_fz_zlib(_wrap_fz_zlib(text), file_hash="sha256:x", board_id="x")
    indices = [p.index for p in board.pins]
    assert indices == [1, 2, 3]
