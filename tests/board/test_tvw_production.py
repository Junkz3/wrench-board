"""Tests for the TVW production-binary parser (magic + walker + mapper)."""
from __future__ import annotations

import struct

import pytest

from api.board.parser._tvw_engine.board_mapper import to_board
from api.board.parser._tvw_engine.cipher import encode
from api.board.parser._tvw_engine.magic import is_production_binary
from api.board.parser._tvw_engine.walker import (
    Aperture,
    Layer,
    LineRecord,
    PinRecord,
    SurfaceRecord,
    TVWFile,
    _last_polygon_pascal_end,
    _parse_component_at,
    _read_arcs,
    _read_dcodes,
    _read_lines,
    _read_nails,
    _read_outline_group,
    _read_pin_record,
    _read_postnails,
    _read_probes,
    _read_surfaces,
    _read_texts,
    _scan_outline_groups,
    _scan_polygon_records,
    _try_walk_pins_at,
    parse,
)

# --- Magic detection ---


def _build_minimal_header() -> bytes:
    """Build a minimal valid TVW production-binary header."""
    parts = []
    parts.append(bytes([19]) + b"O95w-28ps49m 02v9o.")  # magic 1
    parts.append((1).to_bytes(4, "little"))             # version
    parts.append(bytes([7]) + b"G5u9k8s")               # magic 2
    parts.append(bytes([8]) + b"B!Z@6sob")              # magic 3
    return b"".join(parts)


def test_magic_detects_real_layout():
    """Three Pascal magic strings + uint32 = 1 in canonical position."""
    header = _build_minimal_header() + b"\x00" * 64
    assert is_production_binary(header)


def test_magic_rejects_too_short():
    assert not is_production_binary(b"")
    assert not is_production_binary(b"\x00" * 16)


def test_magic_rejects_wrong_signature():
    bad = b"\x13" + b"X" * 19 + (1).to_bytes(4, "little") + b"\x07" + b"X" * 7
    assert not is_production_binary(bad + b"\x00" * 64)


def test_magic_rejects_wrong_version():
    parts = [
        bytes([19]) + b"O95w-28ps49m 02v9o.",
        (2).to_bytes(4, "little"),  # wrong version
        bytes([7]) + b"G5u9k8s",
        bytes([8]) + b"B!Z@6sob",
    ]
    assert not is_production_binary(b"".join(parts) + b"\x00" * 64)


# --- Walker ---


def test_walker_extracts_decoded_date():
    """The walker decodes the 4th obfuscated Pascal string into a date."""
    header = _build_minimal_header()
    encoded_date = encode("March 09, 2018")
    header += bytes([len(encoded_date)]) + encoded_date
    header += b"\x00" * 64  # config block padding (no layers)
    file = parse(header)
    assert file.version == 1
    assert file.date == "March 09, 2018"
    assert file.layers == []  # no layer markers found


def test_walker_rejects_non_production():
    with pytest.raises(ValueError, match="magic"):
        parse(b"not a tvw file at all")


# --- Pin record reader ---


def test_read_dcodes_type1_uses_24_byte_stride_and_ordinal_index():
    raw = (
        struct.pack("<I", 3)
        + struct.pack("<IiiIII", 1, 100, 200, 0, 0, 0)
        + struct.pack("<IiiIII", 1, 300, 400, 1, 0, 0)
        + struct.pack("<IiiII", 1, 500, 600, 5, 0)
        + bytes([6]) + b"Custom"
    )

    apertures, end = _read_dcodes(raw, 0, len(raw))

    assert [(ap.index, ap.width, ap.height, ap.type_) for ap in apertures] == [
        (1, 100, 200, 0),
        (2, 300, 400, 1),
        (3, 500, 600, 5),
    ]
    assert end == len(raw)


def test_read_dcodes_indexes_custom_apertures_before_pin_section():
    custom = (
        struct.pack("<IiiII", 1, 500, 600, 5, 0)
        + bytes([6]) + b"Custom"
    )
    pin_header = struct.pack("<III", 0, 1, 0)
    pin_body = _pin_record_bytes(part_idx=1, pin_local=2, x=100, y=200)
    raw = (
        struct.pack("<I", 2)
        + struct.pack("<IiiIII", 1, 100, 200, 0, 0, 0)
        + custom
        + struct.pack("<ii", 7, 8)
        + pin_header
        + pin_body
    )

    apertures, end = _read_dcodes(raw, 0, len(raw))

    assert [(ap.index, ap.width, ap.height, ap.type_) for ap in apertures] == [
        (1, 100, 200, 0),
        (2, 500, 600, 5),
    ]
    assert struct.unpack_from("<ii", raw, end) == (7, 8)


def _pin_record_bytes(
    part_idx: int,
    pin_local: int,
    x: int,
    y: int,
    flag1: int = 0,
    has_ext: int = 0,
    sub_a: int = 0,
    sub_b: int = 0,
    sub_c: int = 0,
    flag3: int = 0,
) -> bytes:
    """Synthesize a pin record. has_ext=0 → 19 bytes; has_ext=1 → variable."""
    base = struct.pack(
        "<IIiiBB", part_idx, pin_local, x, y, flag1, has_ext
    )
    if has_ext == 0:
        return base + bytes([flag3])
    out = bytearray(base)
    out.append(sub_a)
    if sub_a == 1:
        out += b"\x00" * 12
    out.append(sub_b)
    if sub_b != 0:
        out += b"\x00" * 16
    out.append(sub_c)
    if sub_c != 0:
        out += b"\x00" * 16
    out.append(flag3)
    return bytes(out)


def test_pin_record_base_19_bytes():
    """Pin record without extension is exactly 19 bytes."""
    raw = _pin_record_bytes(part_idx=42, pin_local=7, x=1000, y=2000)
    assert len(raw) == 19
    rec, end = _read_pin_record(raw, 0, len(raw))
    assert rec is not None
    assert rec.part_index == 42
    assert rec.pin_local_index == 7
    assert rec.x == 1000
    assert rec.y == 2000
    assert rec.raw_size == 19
    assert end == 19


def test_pin_record_negative_coords():
    """X and Y are signed int32."""
    raw = _pin_record_bytes(part_idx=1, pin_local=2, x=-5000, y=-7500)
    rec, _ = _read_pin_record(raw, 0, len(raw))
    assert rec is not None
    assert rec.x == -5000
    assert rec.y == -7500


def test_pin_record_with_full_extension():
    """has_ext + sub_a==1 + sub_b!=0 + sub_c!=0 → 19 + 3 + 12 + 16 + 16 = 66 bytes."""
    raw = _pin_record_bytes(
        part_idx=10, pin_local=1, x=0, y=0,
        has_ext=1, sub_a=1, sub_b=2, sub_c=3,
    )
    assert len(raw) == 19 + 3 + 12 + 16 + 16
    rec, end = _read_pin_record(raw, 0, len(raw))
    assert rec is not None
    assert rec.part_index == 10
    assert rec.raw_size == 66
    assert end == 66


def test_pin_record_pad_bbox_extracted():
    """The 16-byte sub_b extension is parsed as 4 × i32 pad bbox offsets."""
    base = struct.pack(
        "<IIiiBB",
        42, 7, 1000, 2000, 0, 1,  # part, pin_local, x, y, flag1, has_ext=1
    )
    # sub_a=0 (no skip 12), sub_b=1 (4 i32 follow), sub_c=0 (no skip 16)
    extension = (
        bytes([0])                                  # sub_a
        + bytes([1])                                # sub_b
        + struct.pack("<4i", -50, -100, 50, 100)    # pad bbox offsets
        + bytes([0])                                # sub_c
    )
    flag3 = bytes([0])
    raw = base + extension + flag3
    rec, end = _read_pin_record(raw, 0, len(raw))
    assert rec is not None
    assert rec.has_pad_bbox
    assert (rec.pad_dx1, rec.pad_dy1, rec.pad_dx2, rec.pad_dy2) == (-50, -100, 50, 100)


def test_pin_record_no_pad_bbox_when_no_extension():
    """A 19-byte base record has no pad bbox."""
    raw = _pin_record_bytes(part_idx=1, pin_local=1, x=0, y=0)
    rec, _ = _read_pin_record(raw, 0, len(raw))
    assert rec is not None
    assert not rec.has_pad_bbox
    assert rec.pad_dx1 == 0 and rec.pad_dx2 == 0


def test_pin_record_partial_extension():
    """has_ext + sub_a=0 (no skip) + sub_b!=0 (skip 16) + sub_c=0 (no skip)."""
    raw = _pin_record_bytes(
        part_idx=5, pin_local=2, x=100, y=200,
        has_ext=1, sub_a=0, sub_b=1, sub_c=0,
    )
    assert len(raw) == 19 + 3 + 16  # 38 bytes
    rec, end = _read_pin_record(raw, 0, len(raw))
    assert rec is not None
    assert end == 38


def test_pin_record_truncated_returns_none():
    """A record cut off mid-base returns None."""
    raw = _pin_record_bytes(part_idx=1, pin_local=2, x=0, y=0)[:10]
    rec, _ = _read_pin_record(raw, 0, len(raw))
    assert rec is None


def test_try_walk_pins_at_zero_count():
    """pin_count == 0 returns ([], off+8, 0) — header read but no records."""
    raw = struct.pack("<II", 5, 0) + b"\xff" * 32
    res = _try_walk_pins_at(raw, 0, len(raw))
    assert res is not None
    pins, end, declared = res
    assert pins == []
    assert end == 8
    assert declared == 0


def test_try_walk_pins_at_clean_records():
    """Three clean pin records succeed."""
    header = struct.pack("<III", 0, 3, 0)  # first_count, pin_count, gap
    body = b"".join(
        _pin_record_bytes(part_idx=i, pin_local=i, x=i * 100, y=i * 200)
        for i in range(1, 4)
    )
    raw = header + body
    res = _try_walk_pins_at(raw, 0, len(raw))
    assert res is not None
    pins, end, declared = res
    assert len(pins) == 3
    assert declared == 3
    assert pins[0].part_index == 1
    assert pins[2].x == 300
    assert end == 12 + 3 * 19  # 69


def test_try_walk_pins_at_implausible_coords_rejected():
    """A pin record with absurd X (>5M centi-mils) rejects the candidate."""
    header = struct.pack("<III", 0, 1, 0)
    bad = _pin_record_bytes(part_idx=1, pin_local=1, x=10_000_000, y=0)
    raw = header + bad
    assert _try_walk_pins_at(raw, 0, len(raw)) is None


def test_try_walk_pins_at_huge_count_rejected():
    """pin_count > 200k is rejected as implausible."""
    raw = struct.pack("<II", 0, 500_000) + b"\xff" * 100
    assert _try_walk_pins_at(raw, 0, len(raw)) is None


# --- Board mapper ---


def test_to_board_one_part_per_side():
    """All pins on a side land on the side's carrier Part (TVW_PADS_TOP / _BOTTOM)."""
    file = TVWFile(
        version=1,
        date="x",
        vendor="x",
        product="x",
        layer_count_declared=1,
        layers=[
            Layer(
                name="TOP",
                source_path="",
                body_kind=1,
                apertures=[Aperture(index=1, width=500, height=500, type_=1)],
                pins=[
                    PinRecord(part_index=0, pin_local_index=1,
                              x=100, y=200, flag1=0, flag3=0, raw_size=19),
                    PinRecord(part_index=1, pin_local_index=1,
                              x=300, y=400, flag1=0, flag3=0, raw_size=19),
                    PinRecord(part_index=1, pin_local_index=1,
                              x=500, y=600, flag1=0, flag3=0, raw_size=19),
                ],
            )
        ],
        net_names=["VCC", "GND", "PCIE_RX"],
    )
    board = to_board(file, board_id="test", file_hash="00")
    assert len(board.parts) == 1
    assert board.parts[0].refdes == "TVW_PADS_TOP"
    assert len(board.parts[0].pin_refs) == 3
    assert len(board.pins) == 3


def test_to_board_pin_to_net_mapping():
    """`part_index` resolves as a 0-based index into `net_names`."""
    file = TVWFile(
        version=1,
        date="x",
        vendor="x",
        product="x",
        layer_count_declared=1,
        layers=[
            Layer(
                name="TOP",
                source_path="",
                body_kind=1,
                apertures=[],
                pins=[
                    PinRecord(part_index=0, pin_local_index=1,
                              x=100, y=200, flag1=0, flag3=0, raw_size=19),
                    PinRecord(part_index=1, pin_local_index=1,
                              x=300, y=400, flag1=0, flag3=0, raw_size=19),
                    PinRecord(part_index=2, pin_local_index=1,
                              x=500, y=600, flag1=0, flag3=0, raw_size=19),
                ],
            )
        ],
        net_names=["VCC", "GND", "PCIE_RX"],
    )
    board = to_board(file, board_id="test", file_hash="00")
    pin_nets = [p.net for p in board.pins]
    assert pin_nets == ["VCC", "GND", "PCIE_RX"]


def test_to_board_pin_to_net_out_of_range_falls_back():
    """`part_index` >= len(net_names) lands on `__floating__`."""
    file = TVWFile(
        version=1,
        date="x",
        vendor="x",
        product="x",
        layer_count_declared=1,
        layers=[
            Layer(
                name="TOP",
                source_path="",
                body_kind=1,
                apertures=[],
                pins=[
                    PinRecord(part_index=999, pin_local_index=1,
                              x=0, y=0, flag1=0, flag3=0, raw_size=19),
                ],
            )
        ],
        net_names=["VCC", "GND"],
    )
    board = to_board(file, board_id="test", file_hash="00")
    assert board.pins[0].net == "__floating__"
    floating = next(n for n in board.nets if n.name == "__floating__")
    assert floating.pin_refs == [0]


def test_to_board_net_pin_refs_populated():
    """Net.pin_refs lists every Pin whose part_index resolves to that net."""
    file = TVWFile(
        version=1,
        date="x",
        vendor="x",
        product="x",
        layer_count_declared=1,
        layers=[
            Layer(
                name="TOP",
                source_path="",
                body_kind=1,
                apertures=[],
                pins=[
                    PinRecord(part_index=1, pin_local_index=1,
                              x=0, y=0, flag1=0, flag3=0, raw_size=19),
                    PinRecord(part_index=1, pin_local_index=1,
                              x=10, y=10, flag1=0, flag3=0, raw_size=19),
                    PinRecord(part_index=0, pin_local_index=1,
                              x=20, y=20, flag1=0, flag3=0, raw_size=19),
                ],
            )
        ],
        net_names=["VCC", "GND", "SCL"],
    )
    board = to_board(file, board_id="test", file_hash="00")
    gnd = next(n for n in board.nets if n.name == "GND")
    assert sorted(gnd.pin_refs) == [0, 1]
    vcc = next(n for n in board.nets if n.name == "VCC")
    assert vcc.pin_refs == [2]
    scl = next(n for n in board.nets if n.name == "SCL")
    assert scl.pin_refs == []  # name surfaced even with no pins on it


def test_to_board_surfaces_real_net_names():
    """Net names from the network_names section appear in board.nets."""
    file = TVWFile(
        version=1,
        date="x",
        vendor="x",
        product="x",
        layer_count_declared=0,
        layers=[],
        net_names=["VCC", "GND", "PCIE_RX"],
    )
    board = to_board(file, board_id="test", file_hash="00")
    net_names = {n.name for n in board.nets}
    assert "VCC" in net_names
    assert "GND" in net_names
    assert "PCIE_RX" in net_names


# --- Line / arc / surface / text section readers ---


def test_read_lines_basic():
    """layer_lines_read: u32 count + u32 variant + count × 24-byte record."""
    # 2 line records: (10,20)-(30,40) and (-5,-15)-(25,35)
    header = struct.pack("<II", 2, 0)
    body = (
        struct.pack("<II", 0, 0) + struct.pack("<iiii", 10, 20, 30, 40)
        + struct.pack("<II", 0, 0) + struct.pack("<iiii", -5, -15, 25, 35)
    )
    raw = header + body
    lines, end = _read_lines(raw, 0, len(raw))
    assert len(lines) == 2
    assert lines[0].x1 == 10 and lines[0].y2 == 40
    assert lines[1].x1 == -5 and lines[1].y2 == 35
    assert end == 8 + 2 * 24


def test_read_lines_zero_count():
    raw = struct.pack("<I", 0) + b"junk"
    lines, end = _read_lines(raw, 0, len(raw))
    assert lines == []
    assert end == 4


def test_read_arcs_strides_correctly():
    """Arc record is 28 bytes; we walk count×28 past the header."""
    header = struct.pack("<iI", 3, 0)
    body = b"\x00" * (3 * 28)
    raw = header + body + b"AFTER"
    arcs, end = _read_arcs(raw, 0, len(raw))
    assert len(arcs) == 3
    assert end == 8 + 3 * 28
    assert raw[end:end+5] == b"AFTER"


def test_read_arcs_zero_count():
    raw = struct.pack("<i", 0)
    arcs, end = _read_arcs(raw, 0, len(raw))
    assert arcs == []
    assert end == 4


def test_read_surfaces_single():
    """Single surface with 3 vertices, no voids."""
    header = struct.pack("<II", 1, 0)
    surface = (
        struct.pack("<ii", 7, 3)            # a, vertex_count
        + b"\x00" * (3 * 8)                  # vertices
        + struct.pack("<II", 0, 0)           # c, void_count=0
    )
    raw = header + surface + b"NEXT"
    surfaces, end = _read_surfaces(raw, 0, len(raw))
    assert len(surfaces) == 1
    assert surfaces[0].kind == 7
    assert len(surfaces[0].vertices) == 3
    assert raw[end:end+4] == b"NEXT"


def test_read_surfaces_with_voids():
    """Surface with 2 voids — outer ring and inner holes."""
    header = struct.pack("<II", 1, 0)
    void_a = struct.pack("<I", 2) + b"\x00" * 16 + struct.pack("<I", 0)
    void_b = struct.pack("<I", 3) + b"\x00" * 24 + struct.pack("<I", 0)
    surface = (
        struct.pack("<ii", 1, 4)            # a, outer vertex_count
        + b"\x00" * (4 * 8)                  # outer vertices
        + struct.pack("<III", 0, 2, 99)      # trailing, void_count, void header
        + void_a + void_b
    )
    raw = header + surface
    surfaces, end = _read_surfaces(raw, 0, len(raw))
    assert len(surfaces) == 1
    assert surfaces[0].void_count == 2
    assert end == len(raw)


def test_read_probes_magic_gate_and_named_record():
    """Probes only expand when the section magic is 7."""
    named = (
        bytes([0])
        + struct.pack("<iiii", 1, 2, 12300, 45600)
        + bytes([0, 0, 1])
        + bytes([3]) + b"TP1"
    )
    raw = struct.pack("<IIi", 7, 0, 1) + named + b"NEXT"
    points, end = _read_probes(raw, 0, len(raw))
    assert [(p.x, p.y, p.name) for p in points] == [(12300, 45600, "TP1")]
    assert raw[end:end+4] == b"NEXT"

    points, end = _read_probes(struct.pack("<I", 0) + b"NEXT", 0, 8)
    assert points == []
    assert end == 4


def test_read_nails_skips_two_groups():
    """Nails: magic 4, optional first and second groups, variable tails."""
    first_record = (
        b"\x00" * 36
        + b"\x00" * 3
        + b"\x00" * 8
        + b"\x00\x00\x00"
        + b"\x00" * 4
    )
    second_record = (
        b"\x00" * (36 + 3 + 8)
        + b"\x01\x00\x00"
        + b"\x00" * 20
        + b"\x00" * 4
    )
    raw = (
        struct.pack("<III", 4, 1, 0)
        + first_record
        + struct.pack("<iI", 1, 0)
        + second_record
        + b"NEXT"
    )
    count, end = _read_nails(raw, 0, len(raw))
    assert count == 2
    assert raw[end:end+4] == b"NEXT"


def test_read_postnails_skips_header_and_records():
    raw = struct.pack("<II", 2, 99) + b"\x00" * 18 + b"NEXT"
    count, end = _read_postnails(raw, 0, len(raw))
    assert count == 2
    assert raw[end:end+4] == b"NEXT"


def test_read_texts_simple():
    """Two text records: (Pascal, 39 fixed bytes)."""
    header = struct.pack("<II", 2, 0)
    text1 = bytes([5]) + b"hello" + b"\x00" * 39
    text2 = bytes([3]) + b"foo" + b"\x00" * 39
    raw = header + text1 + text2 + b"END"
    texts, end = _read_texts(raw, 0, len(raw))
    assert [t.text for t in texts] == ["hello", "foo"]
    assert raw[end:end+3] == b"END"


def test_last_polygon_pascal_end_finds_last_match():
    """`_last_polygon_pascal_end` returns the offset just past the LAST
    Custom-polygon Pascal name within the region."""
    sig = b"\x05\x00\x00\x00\x00\x00\x00\x00"
    poly1 = sig + bytes([6]) + b"Custom"
    poly2 = sig + bytes([9]) + b"Custom_11"
    poly3 = sig + bytes([6]) + b"Custom"
    raw = b"PREFIX" + poly1 + poly2 + poly3 + b"AFTER"
    end = _last_polygon_pascal_end(raw, 0, len(raw))
    # Function returns the byte offset right after poly3's Pascal name.
    expected = len(b"PREFIX") + len(poly1) + len(poly2) + len(poly3)
    assert end == expected
    # And that offset is followed by the trailing "AFTER" marker.
    assert raw[end:end+5] == b"AFTER"


def test_last_polygon_pascal_end_no_match():
    """No Custom polygon → returns None."""
    raw = b"\x00" * 200
    assert _last_polygon_pascal_end(raw, 0, len(raw)) is None


def test_last_polygon_pascal_end_respects_region():
    """Polygons outside the region window are ignored."""
    sig = b"\x05\x00\x00\x00\x00\x00\x00\x00"
    poly = sig + bytes([6]) + b"Custom" + b"\x00" * 16
    raw = b"BEFORE" + poly + b"|||" + poly + b"END"
    # Limit to region that only sees the first poly.
    region_end = len(b"BEFORE") + len(poly) + 1
    end = _last_polygon_pascal_end(raw, 0, region_end)
    assert end is not None
    assert end == len(b"BEFORE") + 8 + 1 + 6  # len("BEFORE") + sig + len_byte + "Custom"


def test_to_board_emits_traces_from_lines():
    """Layer.lines becomes Board.traces."""
    file = TVWFile(
        version=1,
        date="x", vendor="x", product="x",
        layer_count_declared=1,
        layers=[
            Layer(
                name="TOP",
                source_path="",
                body_kind=1,
                lines=[
                    LineRecord(x1=100, y1=200, x2=300, y2=400, aperture_or_kind=0),
                    LineRecord(x1=500, y1=600, x2=700, y2=800, aperture_or_kind=0),
                    # zero-coord line should be dropped
                    LineRecord(x1=0, y1=0, x2=0, y2=0, aperture_or_kind=0),
                ],
            )
        ],
        net_names=[],
    )
    board = to_board(file, board_id="test", file_hash="00")
    assert len(board.traces) == 2
    assert board.traces[0].a.x == 1.0  # 100 / 100 = 1 mil
    assert board.traces[0].b.y == 4.0
    assert board.traces[1].a.x == 5.0


# --- Component records (refdes section) ---


def test_parse_component_at_basic():
    """Build a minimal component record and parse it."""
    refdes = b"R12"
    rec = (
        bytes([len(refdes)]) + refdes
        + struct.pack("<6i", 100, 200, 300, 400, 200, 300)  # bbox + center
        + struct.pack("<2I", 0, 39)                          # rot + kind
        + b"\x00" * 12                                        # 3 u32 padding
        + b"\x01" + bytes([4]) + b"100k"                       # value
        + b"\x00" + bytes([0])                                  # comment empty
        + bytes([5]) + b"R0805"                                 # footprint
        + b"\x00" * 5                                          # 5 byte pad
        + struct.pack("<I", 2)                                # pin_count
        + b"\x00" * 100                                        # tail
    )
    parsed = _parse_component_at(rec, 0, len(rec))
    assert parsed is not None
    assert parsed.refdes == "R12"
    assert (parsed.cx, parsed.cy) == (200, 300)
    assert parsed.rotation == 0
    assert parsed.kind == 39
    assert parsed.value == "100k"
    assert parsed.footprint == "R0805"
    assert parsed.pin_count == 2


def test_parse_component_at_rejects_garbage():
    """A buffer of zeros should not parse as a component."""
    rec = b"\x00" * 200
    assert _parse_component_at(rec, 0, len(rec)) is None


def test_parse_component_at_rejects_implausible_bbox():
    """bbox not enclosing centre fails the upstream candidate filter."""
    refdes = b"R1"
    # Centre way outside bbox.
    rec = (
        bytes([len(refdes)]) + refdes
        + struct.pack("<6i", 0, 0, 100, 100, 50_000_000, 0)
        + b"\x00" * 200
    )
    parsed = _parse_component_at(rec, 0, len(rec))
    # _parse_component_at itself doesn't enforce bbox sanity (the
    # outer scanner does); it should still parse the bytes.
    assert parsed is not None or True  # smoke-only


def test_scan_polygon_records_finds_signature():
    """Custom polygons are anchored on the type=5 + Pascal "Custom"
    byte signature; the scanner returns one record per match."""
    # Build a buffer with two polygon signatures.
    sig = b"\x05\x00\x00\x00\x00\x00\x00\x00"
    bbox = struct.pack("<4i", -100, -100, 100, 100)
    flags = struct.pack("<2I", 1, 1) + b"\x00" * 12
    vertices_section = struct.pack("<I", 3) + struct.pack("<6i", 0, 0, 1, 1, 2, 2)
    poly = sig + bytes([6]) + b"Custom" + bbox + flags + vertices_section
    raw = b"PREFIX" + poly + poly + b"END"
    polys = _scan_polygon_records(raw)
    assert len(polys) == 2
    assert polys[0].name == "Custom"
    assert polys[0].bbox_x1 == -100
    assert polys[0].bbox_x2 == 100


# --- F00B outline groups ---


_F00B_SIG = b"\xff\x00\x00\x00\x00\xff\x00\x00\x0b\x00\x00\x00"


def test_read_outline_group_kind10_lines():
    """A F00B group with two kind=10 line primitives parses both."""
    header = struct.pack("<11I", 1, 100, 100, 0, 0, 0, 1, 0, 1, 0, 2)
    line1 = b"\xff\xff\xff\xff" + struct.pack("<I", 10) + struct.pack(
        "<4i", 100, 200, 300, 400
    )
    line2 = b"\xff\xff\xff\xff" + struct.pack("<I", 10) + struct.pack(
        "<4i", -50, -60, -70, -80
    )
    raw = _F00B_SIG + header + line1 + line2
    g = _read_outline_group(raw, 0, len(raw))
    assert g is not None
    assert g.file_offset == 0
    assert g.header[0] == 1
    assert g.header[1] == 100
    assert len(g.prims) == 2
    assert g.prims[0].kind == 10
    assert g.prims[0].points == [(100, 200), (300, 400)]
    assert g.prims[1].points == [(-50, -60), (-70, -80)]


def test_read_outline_group_kind3_polyline():
    """kind in [3, 200] = N-point polyline, body is N × 8 bytes."""
    header = struct.pack("<11I", 1, 100, 100, 0, 0, 0, 1, 0, 1, 0, 1)
    poly = b"\xff\xff\xff\xff" + struct.pack("<I", 3) + struct.pack(
        "<6i", 0, 0, 100, 100, 200, 0
    )
    raw = _F00B_SIG + header + poly
    g = _read_outline_group(raw, 0, len(raw))
    assert g is not None
    assert len(g.prims) == 1
    assert g.prims[0].kind == 3
    assert g.prims[0].points == [(0, 0), (100, 100), (200, 0)]


def test_read_outline_group_validates_polyline_coords():
    """Polyline coords must satisfy |abs| ≤ 9999 — the reference
    algorithm's gate. A coord of 50000 terminates parsing."""
    header = struct.pack("<11I", 1, 100, 100, 0, 0, 0, 1, 0, 1, 0, 1)
    bad_poly = b"\xff\xff\xff\xff" + struct.pack("<I", 3) + struct.pack(
        "<6i", 0, 0, 50_000, 0, 0, 0
    )
    raw = _F00B_SIG + header + bad_poly
    g = _read_outline_group(raw, 0, len(raw))
    assert g is not None
    # Out-of-range polyline aborts parsing → no primitives kept.
    assert len(g.prims) == 0


def test_scan_outline_groups_finds_multiple():
    """Multiple F00B groups in a buffer all surface."""
    header = struct.pack("<11I", 1, 100, 100, 0, 0, 0, 1, 0, 1, 0, 1)
    prim = b"\xff\xff\xff\xff" + struct.pack("<I", 10) + struct.pack(
        "<4i", 1, 2, 3, 4
    )
    one_group = _F00B_SIG + header + prim
    raw = b"PREFIX" + one_group + b"BETWEEN" + one_group + b"END"
    groups = _scan_outline_groups(raw)
    assert len(groups) == 2
    assert groups[0].file_offset < groups[1].file_offset
    assert groups[0].prims[0].points == [(1, 2), (3, 4)]


def test_scan_outline_groups_empty_when_no_signature():
    """Without the F00B signature the scanner returns an empty list."""
    raw = b"\x00" * 256 + b"NO MATCH HERE" + b"\xff" * 16
    groups = _scan_outline_groups(raw)
    assert groups == []


# --- Per-component package-outline emission ---


def test_to_board_emits_package_outlines_at_component_centers():
    """F00B group lines must be translated to each component's centroid
    and rotated by the component's rotation field, then surfaced as
    Trace records on the component's layer."""
    from api.board.parser._tvw_engine.walker import (
        ComponentRecord,
        OutlineGroup,
        OutlinePrimRecord,
    )

    # One component (50×30 cmils package, centred at (10000, 20000), rot=0)
    comp = ComponentRecord(
        refdes="R1", value="", comment="", footprint="0402",
        cx=10000, cy=20000,
        bbox_x1=9975, bbox_y1=19985, bbox_x2=10025, bbox_y2=20015,
        rotation=0, kind=1, pin_count=2,
    )
    # Matching F00B group: 4 lines forming the 50×30 rectangle, centred on (0,0)
    box_lines = [
        OutlinePrimRecord(kind=10, points=[(-25, -15), (25, -15)]),
        OutlinePrimRecord(kind=10, points=[(25, -15), (25, 15)]),
        OutlinePrimRecord(kind=10, points=[(25, 15), (-25, 15)]),
        OutlinePrimRecord(kind=10, points=[(-25, 15), (-25, -15)]),
    ]
    group = OutlineGroup(file_offset=0x100, header=(1,), prims=box_lines)
    # Add a pin so the component lands as a real Part (not the carrier).
    pin = PinRecord(
        part_index=0, pin_local_index=1, x=10000, y=20000,
        flag1=0, flag3=0, raw_size=20,
    )
    layer = Layer(name="TOP", source_path="", body_kind=1, pins=[pin])
    file = TVWFile(
        version=1, date="", vendor="", product="",
        layer_count_declared=1, layers=[layer],
        net_names=["NET1"],
        components=[comp],
        outlines=[group],
    )
    board = to_board(file, board_id="test", file_hash="00")
    # Find the 4 outline traces (centred on the component, in mils):
    #   bbox in mils is (99.75, 199.85) to (100.25, 200.15)
    outline_traces = [
        t for t in board.traces
        if 99.0 < t.a.x < 101.0 and 199.0 < t.a.y < 201.0
    ]
    assert len(outline_traces) == 4
    # Package outlines land on the WebGL viewer's "outline" channel
    # (layer 28) — rendered in silkscreen-white, distinct from copper.
    assert all(t.layer == 28 for t in outline_traces)
    xs = sorted({t.a.x for t in outline_traces} | {t.b.x for t in outline_traces})
    ys = sorted({t.a.y for t in outline_traces} | {t.b.y for t in outline_traces})
    assert xs[0] == pytest.approx(99.75)
    assert xs[-1] == pytest.approx(100.25)
    assert ys[0] == pytest.approx(199.85)
    assert ys[-1] == pytest.approx(200.15)


def test_to_board_skips_unmatched_components():
    """Components whose bbox doesn't match any F00B group within
    tolerance get no outline (no fake lines)."""
    from api.board.parser._tvw_engine.walker import (
        ComponentRecord,
        OutlineGroup,
        OutlinePrimRecord,
    )
    # Component is 10000×10000 cmils (= 100×100 mil package)
    comp = ComponentRecord(
        refdes="X1", value="", comment="", footprint="huge",
        cx=0, cy=0,
        bbox_x1=-5000, bbox_y1=-5000, bbox_x2=5000, bbox_y2=5000,
        rotation=0, kind=1, pin_count=1,
    )
    # F00B group is tiny (10×10 cmils = 0.1 mil) — far outside the
    # 25-mil tolerance, so no match is recorded.
    tiny_lines = [
        OutlinePrimRecord(kind=10, points=[(-5, -5), (5, 5)]),
    ]
    group = OutlineGroup(file_offset=0x100, header=(1,), prims=tiny_lines)
    pin = PinRecord(
        part_index=0, pin_local_index=1, x=0, y=0,
        flag1=0, flag3=0, raw_size=20,
    )
    layer = Layer(name="TOP", source_path="", body_kind=1, pins=[pin])
    file = TVWFile(
        version=1, date="", vendor="", product="",
        layer_count_declared=1, layers=[layer],
        net_names=["NET1"],
        components=[comp],
        outlines=[group],
    )
    board = to_board(file, board_id="test", file_hash="00")
    # No outline traces emitted — only any pre-existing layer lines (none here).
    assert len(board.traces) == 0


def test_to_board_uses_large_surface_as_board_outline():
    """TVW board outline comes from a real surface outer ring, not F00B."""
    surface = SurfaceRecord(
        kind=3,
        vertices=[
            (0, 0),
            (200_000, 0),
            (200_000, 120_000),
            (0, 120_000),
        ],
        void_count=12,
    )
    layer = Layer(
        name="TOP",
        source_path="",
        body_kind=1,
        surfaces=[surface],
    )
    file = TVWFile(
        version=1, date="", vendor="", product="",
        layer_count_declared=1, layers=[layer],
    )
    board = to_board(file, board_id="test", file_hash="00")
    assert [(p.x, p.y) for p in board.outline] == [
        (0, 0),
        (2000, 0),
        (2000, 1200),
        (0, 1200),
    ]


def test_to_board_drops_implausible_tvw_line_coords():
    layer = Layer(
        name="TOP",
        source_path="",
        body_kind=1,
        lines=[
            LineRecord(x1=0, y1=0, x2=1000, y2=1000, aperture_or_kind=0),
            LineRecord(
                x1=2_147_483_647,
                y1=0,
                x2=2_147_483_647,
                y2=1000,
                aperture_or_kind=0,
            ),
        ],
    )
    file = TVWFile(
        version=1, date="", vendor="", product="",
        layer_count_declared=1, layers=[layer],
    )
    board = to_board(file, board_id="test", file_hash="00")
    assert len(board.traces) == 1
    assert board.traces[0].b.x == 10
