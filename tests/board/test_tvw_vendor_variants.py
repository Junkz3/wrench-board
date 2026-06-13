"""Tests for the vendor variants of the production-binary `.tvw` format.

Production-binary `.tvw` files share one signature + version header but are
emitted by several CAD tools, each writing a different vendor Pascal string.
The dominant binary variant opens with prefix `\\x13 4f 39 35` — the standard
encoded format signature + version — but carries a different vendor string and
a build-DATE string where another vendor stores a build hash.

The body grammar is byte-for-byte identical across vendors — only the encoded
header strings differ — so the fix is to key the production-binary magic on the
vendor-independent signature + version and let the existing walker decode the
rest. These tests pin that behaviour:

  * a synthetic vendor-flavoured header round-trips through the magic check and
    the walker's header reader (our own bytes);
  * a synthetic end-to-end fixture (header + one layer + pins + network names)
    maps to a structurally-valid `Board` with the expected parts / pins / nets
    and power-ground classification;
  * a corpus-gated smoke test parses several real `\\x13 4f 39 35` files and
    asserts non-zero parts / pins / nets — skipped cleanly when the local-only
    corpus is absent (no third-party files committed).
"""
from __future__ import annotations

import glob
import os
import struct

import pytest

from api.board.parser._tvw_engine.cipher import decode, encode
from api.board.parser._tvw_engine.magic import is_production_binary
from api.board.parser._tvw_engine.walker import _read_file_header
from api.board.parser.tvw import TVWParser

# Magic signature shared by EVERY production-binary `.tvw`, vendor-agnostic.
_SIG = bytes([19]) + b"O95w-28ps49m 02v9o."
_VERSION = (1).to_bytes(4, "little")

# The dominant variant's distinguishing 4-byte file prefix.
_VARIANT_MAGIC4 = bytes.fromhex("134f3935")


def _pascal(b: bytes) -> bytes:
    return bytes([len(b)]) + b


def _variant_header(date: str = "August 01, 2015") -> bytes:
    """Build a synthetic vendor-flavoured TVW production-binary header.

    Mirrors `walker._read_file_header`'s expected layout: 4 stored
    encoded Pascal strings (magic / vendor / product / date),
    3 discarded Pascal strings, a 12-byte gap, then 2 uint32
    (layer_count + a discarded word).
    """
    out = bytearray()
    out += _SIG                          # field 1: format magic
    out += _VERSION                      # consumed u32 (version)
    out += _pascal(encode("VendorB"))    # field 2: vendor
    out += _pascal(encode(""))           # field 3: product (empty)
    out += _pascal(encode(date))         # field 4: build date
    for _ in range(3):                   # 3 discarded Pascal strings
        out += _pascal(b"")
    out += b"\x00" * 12                  # 12-byte gap
    out += (0).to_bytes(4, "little")     # layer_count (no layer markers below)
    out += (0).to_bytes(4, "little")     # discarded u32
    return bytes(out)


def test_variant_first_four_bytes_are_the_known_magic():
    """The header's first 4 bytes are the `13 4f 39 35` we key detection on."""
    assert _variant_header()[:4] == _VARIANT_MAGIC4


def test_magic_accepts_variant():
    """The relaxed magic accepts the vendor-B variant."""
    assert is_production_binary(_variant_header() + b"\x00" * 64)


def test_magic_still_accepts_other_vendor_variant():
    """No regression: an other-vendor-flavoured header still matches."""
    other = (
        _SIG + _VERSION
        + _pascal(b"G5u9k8s")   # vendor A (encoded)
        + _pascal(b"B!Z@6sob")  # build hash
    )
    assert is_production_binary(other + b"\x00" * 64)


def test_magic_rejects_wrong_version():
    """A variant-shaped header with version != 1 is rejected."""
    bad = _SIG + (2).to_bytes(4, "little") + _pascal(encode("VendorB"))
    assert not is_production_binary(bad + b"\x00" * 64)


def test_magic_rejects_wrong_signature():
    """Same vendor string, wrong format signature → not production-binary."""
    bad = bytes([19]) + b"X" * 19 + _VERSION + _pascal(encode("VendorB"))
    assert not is_production_binary(bad + b"\x00" * 64)


def test_walker_decodes_vendor_and_date():
    """The header reader decodes the vendor + build date."""
    raw = _variant_header(date="February 18, 2016") + b"\x00" * 64
    fh, _off = _read_file_header(raw, 0)
    assert fh["magic"] == decode(_SIG[1:])  # decoded format signature
    assert fh["vendor"] == "VendorB"
    assert fh["date"] == "February 18, 2016"


def test_encoding_round_trips_header_strings():
    """Sanity: our encode/decode round-trips the variant's header strings."""
    for s in ("VendorB", "August 01, 2015", "x-y_z 0.1"):
        assert decode(encode(s)) == s


# --- Synthetic end-to-end Board fixture (our own bytes) ---


def _pin(part_idx: int, x: int, y: int) -> bytes:
    """A 19-byte base pin record (no extension). `part_idx` doubles as the
    0-based net index in the mapper's pin→net association."""
    return struct.pack("<IIiiBB", part_idx, 1, x, y, 0, 0) + bytes([0])


def _layer_body_with_pins(pins: list[bytes]) -> bytes:
    """A minimal TOP layer: header (marker + path) + dcodes(0) + a/b gate
    + a pin section. Just enough for the walker to land pins.
    """
    out = bytearray()
    # Layer header: layer_type, sub1, sub2, name1, name2, source_path,
    # body_kind, 2 discarded u32. Use a TOP marker so _next_layer_marker
    # snaps onto it; the header starts 12 bytes (3×u32) before name1.
    out += struct.pack("<III", 0, 0, 0)          # layer_type, sub1, sub2
    out += _pascal(b"TOP")                        # name1 (layer marker)
    out += _pascal(b"TOP")                        # name2 (redundant)
    out += _pascal(b"top.gbr")                    # source path
    out += struct.pack("<III", 0, 0, 0)           # body_kind=0 + 2 discarded
    # dcode table: count = 0
    out += (0).to_bytes(4, "little")
    # a/b gate ints — set a>0 so the walker reads pins even with 0 dcodes
    out += struct.pack("<ii", 1, 0)
    # pin section header: first_count, pin_count, gap
    out += struct.pack("<III", len(pins), len(pins), 0)
    for p in pins:
        out += p
    return bytes(out)


# Net names indexed by the pins' `part_index` field. The list is longer
# than any incidental Pascal run elsewhere in the fixture (e.g. the layer
# header's "TOP"/"top.gbr"), so `_try_read_network_names` snaps onto it.
_NET_NAMES = [b"+5V", b"GND", b"+3V3", b"PCIE_RX0", b"USB_DP", b"CLK_24M"]


def _synthetic_variant_board() -> bytes:
    """Header + one TOP layer with pins + a trailing net-name list."""
    out = bytearray(_variant_header())
    out += _layer_body_with_pins([
        _pin(0, 1000, 1000),   # net index 0 → "+5V"
        _pin(1, 2000, 1000),   # net index 1 → "GND"
        _pin(0, 3000, 1000),   # net index 0 → "+5V"
        _pin(2, 4000, 1000),   # net index 2 → "+3V3"
        _pin(3, 5000, 1000),   # net index 3 → "PCIE_RX0"
    ])
    # Trailing network-name list: (a, b, count) header + Pascal strings.
    # _try_read_network_names scans the tail for the longest plausible run,
    # so this list must out-length any incidental run above it.
    out += struct.pack("<III", 0, 0, len(_NET_NAMES))
    for name in _NET_NAMES:
        out += _pascal(name)
    out += b"\x00" * 8  # terminator / tail padding
    return bytes(out)


def test_synthetic_variant_maps_to_board():
    """End-to-end: synthetic variant bytes → structurally-valid Board."""
    raw = _synthetic_variant_board()
    assert is_production_binary(raw)
    board = TVWParser().parse(raw, file_hash="hash", board_id="bid")
    assert board.source_format == "tvw"
    # The net-name list surfaces both nets.
    names = {n.name for n in board.nets}
    assert "+5V" in names
    assert "GND" in names
    assert "+3V3" in names
    # Power / ground classification mirrors the ASCII dialects.
    by_name = {n.name: n for n in board.nets}
    assert by_name["+5V"].is_power
    assert by_name["+3V3"].is_power
    assert by_name["GND"].is_ground
    # Pads were decoded from the layer body. Without trailing component
    # records the mapper routes them to `test_pads` (PASS B — the format is
    # fundamentally a probe-target database), so count both channels.
    assert len(board.pins) + len(board.test_pads) >= 1


def test_synthetic_variant_pins_and_nets_proportional():
    """Net names exceed nets-with-pins; pins are non-zero — structural sanity."""
    board = TVWParser().parse(
        _synthetic_variant_board(), file_hash="h", board_id="b"
    )
    assert len(board.nets) >= 2
    assert len(board.pins) + len(board.test_pads) >= 1


# --- Corpus-gated real-file smoke test (no third-party files committed) ---


def _find_variant_corpus(limit: int = 4) -> list[str]:
    roots = [
        os.environ.get("WB_TVW_CORPUS"),
        os.path.expanduser("~/Documents"),
    ]
    hits: list[str] = []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for p in glob.glob(os.path.join(root, "**", "*.tvw"), recursive=True):
            try:
                with open(p, "rb") as fh:
                    if fh.read(4) == _VARIANT_MAGIC4:
                        hits.append(p)
            except OSError:
                continue
        if hits:
            break
    hits.sort(key=lambda p: os.path.getsize(p))
    return hits[:limit]


_CORPUS = _find_variant_corpus()


@pytest.mark.skipif(not _CORPUS, reason="no production-binary .tvw corpus available")
def test_real_variant_files_parse_with_nonzero_structure():
    """Several real `13 4f 39 35` files yield non-zero parts / pins / nets."""
    parser = TVWParser()
    for path in _CORPUS:
        raw = open(path, "rb").read()
        assert is_production_binary(raw), path
        board = parser.parse(raw, file_hash="h", board_id=os.path.basename(path))
        assert board.parts, f"no parts in {path}"
        assert board.pins, f"no pins in {path}"
        assert board.nets, f"no nets in {path}"
        # Self-consistency: real component-class refdes prefixes dominate.
        prefixes = {p.refdes[:1] for p in board.parts}
        assert prefixes & set("CRLUDQJTPFXYKSVBM"), (
            f"no component-class refdes in {path}: {prefixes}"
        )
