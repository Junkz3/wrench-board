"""Packed-binary `.brd` *partial* decoder tests.

The format is a packed binary container (a serialized in-memory heap image; see
api/board/parser/brd_packed for the byte map + the documented blocker on
connectivity). No third-party files are committed. We test against:

  * a SYNTHETIC fixture we build ourselves in the documented on-disk shape
    (string heap + 64-byte component records) — a pure structure round-trip;
  * the dispatch sniff (prefix -> PackedBinaryBoardParser).

The synthetic builder mirrors EXACTLY what the decoder walks, so a passing
round-trip proves the heap-walk + record-scan logic with no real file involved.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from api.board.parser.base import MalformedHeaderError, parser_for
from api.board.parser.brd_packed import (
    _PACKED_BRD_MAGICS,
    _WATERMARK,
    PackedBinaryBoardParser,
    _extract_1300_parts,
    decode_packed_brd,
)

_MAGIC = bytes.fromhex("0a0a1200")  # one prefix family (..1200, has records)
_MAGIC_1300 = bytes.fromhex("03101300")  # a ..1300 family (single-array part records)
_PART_FIELD = 0x10
# A neutral synthetic tail: the recognition marker + filler. The prefix alone
# already routes these fixtures; the marker just exercises the fallback path.
_TAIL = _WATERMARK + b"!!\x03\x00\x00./synthetic.brd"


def _heap_entry(tag: int, low: int, s: str) -> bytes:
    """Encode one string-heap entry: 4-byte LE ptr (tag<<16 | low) + NUL str + pad."""
    ptr = (tag << 16) | (low & 0xFFFF)
    body = struct.pack("<I", ptr) + s.encode("latin-1") + b"\x00"
    pad = (-len(body)) % 4
    return body + b"\x00" * pad


def _component_record(refdes: str) -> bytes:
    """Encode one 64-byte component record: inline refdes [0:20] + 0x1c00 @ +0x38."""
    rec = bytearray(64)
    enc = refdes.encode("latin-1")
    rec[0 : len(enc)] = enc
    struct.pack_into("<I", rec, 0x38, 0x00001C00)
    return bytes(rec)


def _build_packed(
    nets: list[str],
    footprints: list[str],
    refdes: list[str],
) -> bytes:
    """Synthesize a minimal ..1200 file in the documented on-disk shape."""
    out = bytearray()
    out += _MAGIC
    out += b"\x00" * (0x1200 - len(out))  # pad header region up to the heap start

    # String heap: net names (tag 0x521), then footprint names (tag 0x522).
    low = 0x1000
    for s in nets:
        out += _heap_entry(0x521, low, s)
        low += 0x20
    for s in footprints:
        out += _heap_entry(0x522, low, s)
        low += 0x20

    # A >=256-byte gap of junk terminates the heap walk, then the records.
    out += b"\xee" * 512
    # Records must begin past 0xC000 (the decoder's scan start).
    if len(out) < 0xC000:
        out += b"\x00" * (0xC000 - len(out))
    for r in refdes:
        out += _component_record(r)

    # Tail marker (every file of this family carries it).
    out += _TAIL
    return bytes(out)


def _build_packed_1300(refdes: list[str], nets: list[str]) -> bytes:
    """Synthesize a minimal ..1300 file: a string heap + a single fixed-stride
    PART array whose +0x10 field is a self-address pointer to each owner refdes.

    Mirrors EXACTLY what ``_extract_1300_parts`` walks — a self-pointer record
    cluster (first dword = own heap address, +stride per record) where +0x10
    points at a heap entry's self-address. The decoder resolves +0x10 directly
    through the heap-address index, so a passing round-trip proves the
    address→part-record→refdes resolution chain with no real file.
    """
    out = bytearray()
    out += _MAGIC_1300
    out += b"\x00" * (0x1200 - len(out))

    # --- string heap: refdes + net names, each <4B self-addr><str><NUL> pad 4B.
    # We assign each string a self-address in a single segment (high16 = 0x066d,
    # the net/refdes band) and remember it so the part record can point at it.
    addr_of: dict[str, int] = {}
    seg = 0x066D
    low = 0x1000

    def emit(s: str) -> None:
        nonlocal low
        addr = (seg << 16) | (low & 0xFFFF)
        addr_of[s] = addr
        body = struct.pack("<I", addr) + s.encode("latin-1") + b"\x00"
        out.extend(body + b"\x00" * ((-len(body)) % 4))
        low += 0x20

    for s in refdes:
        emit(s)
    for s in nets:
        emit(s)

    # A >=256-byte gap so the heap walk in _net_names_from_heap terminates, but
    # _heap_addr_index walks the whole file so the part array below is still seen.
    out += b"\xee" * 512
    if len(out) < 0xC100:
        out += b"\x00" * (0xC100 - len(out))

    # --- PART array: a self-pointer cluster (stride 40). First dword = own heap
    # self-address (seg 0x0c10, +40 per record); +0x10 = pointer to owner refdes.
    st = 40
    part_seg = 0x0C10
    # the array's base heap address; record k's self-addr = base + k*st
    base = (part_seg << 16) | 0x1000
    for k, r in enumerate(refdes):
        rec = bytearray(st)
        struct.pack_into("<I", rec, 0x00, base + k * st)  # self-address
        struct.pack_into("<I", rec, _PART_FIELD, addr_of[r])  # +0x10 -> refdes
        out += rec
    out += _TAIL
    return bytes(out)


def test_all_magics_recognized():
    assert _MAGIC in _PACKED_BRD_MAGICS
    assert len(_PACKED_BRD_MAGICS) == 6


def test_dispatches_packed_magic(tmp_path: Path):
    f = tmp_path / "demo.brd"
    f.write_bytes(_build_packed(["GND"], ["VX_C0402_SMALL"], ["C1"]))
    assert isinstance(parser_for(f), PackedBinaryBoardParser)


def test_roundtrip_parts_and_nets():
    raw = _build_packed(
        nets=["GND", "+3V3", "VCC_CORE", "PM_SLP_S3#", "DMI_RXN1"],
        footprints=["VX_C0402_SMALL", "VX_R0402_SMALL", "DEV_317"],
        refdes=["C1", "C2", "R10", "U5", "Q3", "JP1"],
    )
    board = decode_packed_brd(raw, file_hash="sha256:test", board_id="synthetic")

    assert board.source_format == "brd-packed"

    # Parts come from the 64-byte records — exactly the refdes we put in.
    assert sorted(p.refdes for p in board.parts) == ["C1", "C2", "JP1", "Q3", "R10", "U5"]

    # Footprints must NOT leak into the parts list (they are heap-only).
    assert "VX_C0402_SMALL" not in {p.refdes for p in board.parts}

    # Nets come from the heap, minus refdes/footprints.
    net_names = {n.name for n in board.nets}
    assert {"GND", "+3V3", "VCC_CORE", "PM_SLP_S3#", "DMI_RXN1"} <= net_names
    assert "VX_C0402_SMALL" not in net_names  # footprint filtered out
    assert "C1" not in net_names  # refdes filtered out


def test_1300_parts_recovered_from_single_array():
    """A ..1300 file's parts come from the single self-pointer PART array whose
    +0x10 resolves to the owner refdes. Needs >= _PART_MIN_RECORDS records so the
    cluster detector locks on (the threshold filters small graph-node clusters)."""
    refdes = [f"C{i}" for i in range(1, 360)] + ["U1", "U2", "R10", "Q3"]
    raw = _build_packed_1300(refdes=refdes, nets=["GND", "+3V3", "VCC_CORE"])
    assert raw[2:4] == b"\x13\x00"  # this is a ..1300 family file

    # Unit: the extractor finds exactly the refdes we placed (de-duplicated).
    assert sorted(_extract_1300_parts(raw)) == sorted(set(refdes))

    # End-to-end: parts flow through the decoder; nets still come from the heap.
    board = decode_packed_brd(raw, file_hash="x", board_id="x")
    assert sorted(p.refdes for p in board.parts) == sorted(set(refdes))
    assert {"GND", "+3V3", "VCC_CORE"} <= {n.name for n in board.nets}
    assert board.pins == []  # connectivity still behind the unresolved graph


def test_1200_part_path_untouched_by_1300_logic():
    """The ..1200 sentinel path must be byte-for-byte unaffected: a ..1200 file
    is never routed through the ..1300 single-array extractor."""
    raw = _build_packed(
        nets=["GND", "+5V"], footprints=["VX_C0402_SMALL"], refdes=["C1", "R2", "U3"]
    )
    assert raw[2:4] == b"\x12\x00"  # ..1200, not ..1300
    # The ..1300 extractor must find nothing in a ..1200 file (no part array).
    assert _extract_1300_parts(raw) == []
    board = decode_packed_brd(raw, file_hash="x", board_id="x")
    assert sorted(p.refdes for p in board.parts) == ["C1", "R2", "U3"]


def test_power_and_ground_classified():
    raw = _build_packed(
        nets=["GND", "AGND", "+3V3", "+1.05V", "VCC_CORE", "SOME_SIGNAL"],
        footprints=[],
        refdes=["C1"],
    )
    board = decode_packed_brd(raw, file_hash="x", board_id="x")
    by = {n.name: n for n in board.nets}
    assert by["GND"].is_ground and not by["GND"].is_power
    assert by["AGND"].is_ground
    assert by["+3V3"].is_power and not by["+3V3"].is_ground
    assert by["+1.05V"].is_power
    assert by["VCC_CORE"].is_power
    assert not by["SOME_SIGNAL"].is_power and not by["SOME_SIGNAL"].is_ground


def test_no_pins_emitted():
    """Connectivity is behind unresolved heap pointers — the decoder is honest
    and emits no pins rather than fabricating coordinates."""
    raw = _build_packed(["GND"], ["VX_C0402_SMALL"], ["C1"])
    board = decode_packed_brd(raw, file_hash="x", board_id="x")
    assert board.pins == []
    assert board.nails == []


def test_not_packed_raises():
    with pytest.raises(MalformedHeaderError):
        decode_packed_brd(b"not a packed file at all", file_hash="x", board_id="x")


def test_recognized_but_empty_raises():
    """A file with the prefix but no heap/records is malformed, not a silent
    empty Board."""
    raw = _MAGIC + b"\x00" * 0x2000 + _WATERMARK
    with pytest.raises(MalformedHeaderError):
        decode_packed_brd(raw, file_hash="x", board_id="x")
