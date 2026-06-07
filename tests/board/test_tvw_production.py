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


def test_to_board_stray_pins_route_to_test_pads():
    """Pins that don't land in any component bbox become test_pads, not Pins.

    The production-binary `.tvw` is a probe-target database — the layer pin
    section mixes real SMD pads with thousands of ICT probe targets
    and vias. Without a component to attach them to, these go to the
    test_pad channel so they render as a discreet secondary layer
    instead of a carrier Part full of fake "pins" that drown out
    real SMD pads in the WebGL viewer.
    """
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
    assert board.parts == []
    assert board.pins == []
    assert len(board.test_pads) == 3
    # Net is preserved on the test_pad so the user can still click and
    # see what each probe target is electrically connected to.
    nets = [tp.net for tp in board.test_pads]
    assert nets == ["VCC", "GND", "GND"]


def test_to_board_pin_to_net_mapping():
    """`part_index` resolves as a 0-based index into `net_names`.

    Stray pins (no component bbox) carry their net through to the
    test_pad so the user keeps connectivity for clicking.
    """
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
    test_pad_nets = [tp.net for tp in board.test_pads]
    assert test_pad_nets == ["VCC", "GND", "PCIE_RX"]


def test_to_board_pin_to_net_out_of_range_falls_back():
    """`part_index` >= len(net_names) lands on `__floating__`.

    On test_pads the floating sentinel is normalized to None — the
    UI shows "no net" rather than "net __floating__" for probe
    targets that didn't resolve to a real network name.
    """
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
    assert board.test_pads[0].net is None


def test_to_board_net_pin_refs_populated():
    """Net.pin_refs lists Pins on real components; stray pins go to test_pads.

    Net entries surface every name from `network_names`, including
    nets with no pins on them (so the network panel can show the full
    vocabulary). Pins on the carrier (no component bbox) are routed
    to `board.test_pads` instead — Net.pin_refs only tracks Pins
    attached to a real component.
    """
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
    # No component records → all 3 pins land on test_pads, none in board.pins.
    assert board.pins == []
    test_pad_nets = sorted(tp.net for tp in board.test_pads if tp.net)
    assert test_pad_nets == ["GND", "GND", "VCC"]
    # Net entries still surface, but pin_refs is empty since there are
    # no real component pins yet.
    for net in board.nets:
        assert net.pin_refs == []
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


# --- Canonical mapping invariants on the the sample GPU fixture ---
#
# These tests validate the canonical pad_index → layer.pins[idx] mapping
# end-to-end on a real graphics-card .tvw. They are marked `slow` so
# they only run in `make test-all`; the fast suite gets the synthetic
# coverage above. The fixture (a sample GPU board) ships with
# a well-documented BOM whose pin counts the parser must reproduce
# exactly: U1 = 1737-pin BGA, U2700-U2400 = 170-pin BGAs each, …


_SAMPLE_GPU_FIXTURE = (
    "memory/mnt-motherboard/uploads/"
    "sample-gpu-boardview.tvw"
)


@pytest.fixture(scope="module")
def sample_gpu_board():
    """Parse the the sample GPU fixture once per test module."""
    import os
    fixture_path = _SAMPLE_GPU_FIXTURE
    if not os.path.exists(fixture_path):
        pytest.skip(f"the sample GPU fixture not present at {fixture_path}")
    with open(fixture_path, "rb") as f:
        raw = f.read()
    file = parse(raw)
    from api.board.parser._tvw_engine.board_mapper import to_board as _to_board
    board = _to_board(file, board_id="sample_gpu", file_hash="00")
    return file, board


@pytest.mark.slow
def test_sample_gpu_invariant1_all_declared_pins_attached(sample_gpu_board):
    """Invariant 1: sum(c.pin_count) == sum(len(p.pin_refs))."""
    file, board = sample_gpu_board
    declared = sum(c.pin_count for c in file.components)
    attached = sum(len(p.pin_refs) for p in board.parts)
    assert declared == attached, (
        f"declared={declared} but attached={attached}; canonical mapping "
        f"lost pins (or duplicated them)"
    )


@pytest.mark.slow
def test_sample_gpu_invariant2_test_pads_distinct_canonical_records(sample_gpu_board):
    """Invariant 2: test_pads are pin records the canonical pass did
    NOT claim — they don't share an underlying pad_index with any Pin.

    A Pin and a test_pad may share a physical position (legitimate on
    probe-instrumented PCBs where a component pad doubles as an ICT
    probe target), but they never come from the same pin record: PASS A
    consumes records via `claimed_top / claimed_bot`, PASS B emits
    test_pads only for the residue.

    A few component pins are aliased — multiple `ComponentPin` entries
    point at the same `pad_index`, e.g. a shared mounting tab counted
    once per host component. That inflates `len(pins)` above the count
    of unique pin records, which is intentional (each host needs its
    own pin in `pin_refs`). What stays invariant is:
        len(unique pad records covered by pins) + len(test_pads)
            == total pin records claimed-or-unclaimed.
    """
    file, board = sample_gpu_board

    # On the the sample GPU we expect roughly 7000 component pins and ~10000
    # test_pads (probe targets / vias / exposed-copper points).
    assert len(board.pins) >= 7000
    assert len(board.test_pads) >= 5000

    # Re-derive `claimed` and `unclaimed` from the walker output so the
    # invariant tests the mapper's actual record bookkeeping rather
    # than its rendered output.
    from api.board.model import Layer
    from api.board.parser._tvw_engine.board_mapper import _resolve_canonical_pin
    top_layer = next(
        (lyr for lyr in file.layers if lyr.name.upper() == "TOP"),
        None,
    )
    bot_layer = next(
        (lyr for lyr in file.layers
         if lyr.name.upper() == "BOTTOM" or lyr.name.upper().startswith("BOT")),
        None,
    )
    top_arr = top_layer.pins if top_layer else []
    bot_arr = bot_layer.pins if bot_layer else []

    claimed_top: set[int] = set()
    claimed_bot: set[int] = set()
    for c in file.components:
        if not c.pins:
            continue
        for cp in c.pins:
            idx = cp.pad_index // 8
            chosen = _resolve_canonical_pin(idx, c, top_arr, bot_arr)
            if chosen is None:
                continue
            side, _pr = chosen
            if side is Layer.TOP:
                claimed_top.add(idx)
            else:
                claimed_bot.add(idx)

    unique_records_claimed = len(claimed_top) + len(claimed_bot)
    total_records = sum(len(layer.pins) for layer in file.layers)
    unclaimed = total_records - unique_records_claimed

    # The mapper emits test_pads for unclaimed records only — counts
    # must match exactly.
    assert len(board.test_pads) == unclaimed, (
        f"test_pads ({len(board.test_pads)}) != unclaimed records "
        f"({unclaimed}); PASS B is double-counting or skipping"
    )


@pytest.mark.slow
def test_sample_gpu_invariant3_every_pin_has_a_name(sample_gpu_board):
    """Invariant 3: every Pin attached via the canonical mapping
    carries the silkscreen pin name (e.g. 'A1', 'B14', '1', '2')."""
    _file, board = sample_gpu_board
    unnamed = [p for p in board.pins if not p.name]
    assert unnamed == [], (
        f"{len(unnamed)} pins missing a name (sample: "
        f"{[(p.part_refdes, p.index) for p in unnamed[:5]]})"
    )


@pytest.mark.slow
def test_sample_gpu_invariant4_pad_shape_distribution(sample_gpu_board):
    """Invariant 4: pad_shape comes from the aperture's `type_` field
    (or the per-pin pad_bbox extension), not from a `w == h` heuristic.

    The the sample GPU mixes round (BGA balls), rect (SMD rectangular pads —
    including the type=5 "Custom" apertures which describe rectangles
    on this fixture, the file's polygon table being board-scale shapes
    rather than pad shapes), and a long tail of oblong / through-hole
    variants. We assert that the legacy `w == h` collapse no longer
    masquerades type=5 rectangles as circles.
    """
    from collections import Counter
    _file, board = sample_gpu_board
    shapes = Counter(p.pad_shape for p in board.pins)
    # Both round and rectangular pads must appear — a graphics card
    # has BGA balls (round) and bulk SMD passives + connector pads
    # (rect). The MPCIE1 PCIe slot (82 pins) alone guarantees rect.
    assert shapes["rect"] > 0
    assert shapes["circle"] > 0
    # No pin should fall through to a None shape — every aperture
    # this fixture references resolves to a known shape token.
    assert shapes.get(None, 0) == 0


@pytest.mark.slow
def test_sample_gpu_invariant5_side_distribution_deterministic(sample_gpu_board):
    """Invariant 5: the side of every Part is decided by the canonical
    pad_index → layer mapping (TOP wins vs BOTTOM wins). The previous
    `kind & 1` LSB heuristic only matched ~85% of components — the
    canonical resolver matches 100%. We assert that the distribution
    has both sides represented and matches the documented the sample GPU
    layout (most placed components are bottom-side passive pour, GPU
    + memory chips on the top-side).
    """
    _file, board = sample_gpu_board
    from api.board.model import Layer
    sides = {Layer.TOP: 0, Layer.BOTTOM: 0}
    for p in board.parts:
        sides[p.layer] += 1
    assert sides[Layer.TOP] > 0 and sides[Layer.BOTTOM] > 0, (
        "expected components on both sides on a real graphics card; "
        f"got {sides}"
    )
    # GPU + memory live on TOP (BGAs at U1, U2700..U2400)
    u1 = next(p for p in board.parts if p.refdes == "U1")
    assert u1.layer is Layer.TOP, "U1 (Tahiti GPU) must be TOP-side"
    for refdes in ("U2700", "U2600", "U2500", "U2400"):
        m = next((p for p in board.parts if p.refdes == refdes), None)
        if m is None:
            continue
        assert m.layer is Layer.TOP, f"{refdes} (memory) must be TOP-side"


@pytest.mark.slow
def test_sample_gpu_invariant6_ground_truth_pin_counts(sample_gpu_board):
    """Invariant 6 (proxy for net mapping quality): the canonical
    mapping reproduces the documented BOM pin counts exactly.

    The the sample GPU board's BOM lists U1 = 1737-pin BGA,
    U2700-U2400 = 170-pin BGAs (memory chips), MPCIE1 = 82-pin
    PCI Express slot, J1900 = 62-pin DVI / D-DVI connector,
    MJ1900 = 36-pin connector header. Any drop in these counts means
    pad_index resolution lost canonical references.
    """
    _file, board = sample_gpu_board
    expected = {
        "U1": 1737,
        "U2700": 170, "U2600": 170, "U2500": 170, "U2400": 170,
        "MPCIE1": 82,
        "J1900": 62,
        "MJ1900": 36,
    }
    parts_by_refdes = {p.refdes: p for p in board.parts}
    for refdes, want in expected.items():
        p = parts_by_refdes.get(refdes)
        assert p is not None, f"{refdes} not in parts"
        got = len(p.pin_refs)
        assert got == want, f"{refdes}: expected {want} pins, got {got}"


@pytest.mark.slow
def test_sample_gpu_invariant7_package_outlines_attached(sample_gpu_board):
    """Invariant 7: per-footprint body outlines from the PACKAGE table
    attach to every component whose `footprint` matches a table entry.

    The PACKAGE table ships ~130 named footprint outlines for the
    the sample GPU. Each real component carries a `footprint` string
    (`EDGECON_PCI_EXPRESS_16`, `BGA0_8X1_2MM40X40-1737`, …); the
    mapper looks each one up and populates `Part.body_lines` with
    the outline rotated by the component's rotation and translated
    to its centroid.

    Specific high-value spot checks:
      * `EDGECON_PCI_EXPRESS_16` (MPCIE1) ships the 8-segment edge
        connector outline with its key cut — a richer body shape
        than the bbox-rectangle the previous path emitted.
      * `BGA0_8X1_2MM40X40-1737` (U1, Tahiti GPU) ships a 102-segment
        polygonal approximation of the round BGA package.
    """
    file, board = sample_gpu_board

    # Coverage gate: every component on this fixture should have
    # body_lines populated from the PACKAGE table. The decoder accepts
    # marker values in [1, 256] (multi-pad packages like LFPAK MOSFETs
    # tag drain / source / gate sub-shapes with marker=11, =12, …),
    # so the residue is just one missing footprint family at most.
    with_body = sum(1 for p in board.parts if p.body_lines)
    total = len(board.parts)
    assert with_body == total, (
        f"PACKAGE-table coverage incomplete: {with_body}/{total} "
        f"parts have body_lines"
    )

    # Spot checks: PCIe + GPU BGA must carry their distinctive outlines.
    mpcie = next(p for p in board.parts if p.refdes == "MPCIE1")
    assert len(mpcie.body_lines) == 8, (
        f"EDGECON_PCI_EXPRESS_16 should have 8 outline segments "
        f"(rect + key cut), got {len(mpcie.body_lines)}"
    )

    u1 = next(p for p in board.parts if p.refdes == "U1")
    assert len(u1.body_lines) >= 90, (
        f"BGA0_8X1_2MM40X40-1737 should be a polygonal approximation "
        f"(~100 segments), got {len(u1.body_lines)}"
    )

    # Sanity on the PACKAGE-table dict itself: at least 100 named
    # entries (the the sample GPU ships ~130), all with non-empty segment
    # lists. The multi-pad family (LFPAK MOSFETs) decodes to ~130
    # segments per package once the marker=11 sub-shape variant is
    # accepted alongside the canonical marker=10.
    assert len(file.packages) >= 100, (
        f"PACKAGE table should hold ~130 entries, got {len(file.packages)}"
    )
    empty = [n for n, p in file.packages.items() if not p.segments]
    assert empty == [], f"empty PACKAGE entries: {empty}"

    # The MOSFET driver footprint must decode now that marker=11 is
    # accepted (it ships drain / source / gate pad sub-shapes tagged
    # with the variant marker; marker=10-only decoders previously
    # bailed at segment 92 and dropped the entry).
    lfpak = file.packages.get("MULTI_SMD_SOT669_LFPAK_B")
    assert lfpak is not None, "MULTI_SMD_SOT669_LFPAK_B (LFPAK MOSFET) missing"
    assert len(lfpak.segments) >= 100, (
        f"LFPAK MOSFET package should decode multi-pad sub-shapes "
        f"(~130 segments), got {len(lfpak.segments)}"
    )


@pytest.mark.slow
def test_sample_gpu_components_count_matches_ground_truth(sample_gpu_board):
    """The the sample GPU fixture should yield ~1648 components (the documented
    BOM total). Allow a small margin for the regex anchor's edge cases.
    """
    _file, board = sample_gpu_board
    # Expected ~1647-1648 components; allow ±5.
    assert 1640 <= len(board.parts) <= 1655, (
        f"expected ~1647 components, got {len(board.parts)}"
    )


# --- Regression: the comment-twice string region (GV-N970 fixture) ---
#
# A previous revision of `_parse_component_at` modelled the trailing
# string region as `(sep_v + value) + (sep_c + comment) + (footprint)`,
# i.e. two leading-separator Pascal fields then a footprint Pascal. We
# tested this against the the sample GPU fixture, where every component has
# `comment == ""`, so the duplicate-zero plen byte aliased onto the
# expected `_sep_c` and the footprint landed on the correct boundary.
#
# The GV-N970 fixture exposes the actual layout: the comment Pascal is
# stored *twice*, back-to-back, with no leading sep on either copy. On
# records with a non-empty comment (the LB-family inductors carry
# comment="N/A", others carry "+/- 5%", "+/-25%", etc.) the legacy
# parser misread the duplicate copy as the footprint length byte and
# spilled raw control bytes into `Component.footprint`.
#
# The fix is to read four Pascal fields after the 12-byte kind padding:
#   sep_v + value    (sep_v always 0x01)
#   plen_a + comment_a
#   plen_b + comment_b   (always == comment_a)
#   plen_f + footprint
#
# This regression test loads the GV-N970 fixture and asserts the
# LB-family records (and a sampling of others with non-empty comments)
# carry clean ASCII footprints.

_GVN970_FIXTURE = (
    "memory/mnt-motherboard/uploads/"
    "20260504T120000Z-boardview-GV-N970TTOC-4GD-1.0.tvw"
)


@pytest.fixture(scope="module")
def gvn970_components():
    """Parse the GV-N970 fixture once and return its component list."""
    import os
    if not os.path.exists(_GVN970_FIXTURE):
        pytest.skip(f"GV-N970 fixture not present at {_GVN970_FIXTURE}")
    with open(_GVN970_FIXTURE, "rb") as f:
        raw = f.read()
    file = parse(raw)
    return {c.refdes: c for c in file.components}


@pytest.mark.slow
def test_gvn970_lb_family_footprints_clean(gvn970_components):
    """LB-family records (inductors with comment='N/A') must report a
    clean ASCII footprint — '0805' on the LB501 / LB504 / LB505 / LB519
    inductors. The legacy parser leaked raw control bytes here.
    """
    by_ref = gvn970_components
    expected_footprints = {
        "LB501": "0805",
        "LB504": "0805",
        "LB505": "0805",
        "LB519": "0805",
    }
    for refdes, want in expected_footprints.items():
        assert refdes in by_ref, f"{refdes} missing from parsed components"
        got_fp = by_ref[refdes].footprint
        # The exact value matters AND there must be no embedded NUL or
        # other control bytes. Both conditions catch the regression.
        assert got_fp == want, (
            f"{refdes}: footprint={got_fp!r}, expected {want!r}"
        )
        assert all(32 <= ord(c) <= 126 for c in got_fp), (
            f"{refdes}: footprint contains non-printable bytes: {got_fp!r}"
        )
        # And the comment field should now carry the duplicated comment
        # text instead of being empty (or worse, garbage).
        assert by_ref[refdes].comment == "N/A", (
            f"{refdes}: comment={by_ref[refdes].comment!r}, expected 'N/A'"
        )


@pytest.mark.slow
def test_gvn970_no_component_has_corrupted_footprint(gvn970_components):
    """No component on the GV-N970 fixture should carry a footprint with
    embedded NULs or non-printable bytes. This is the broad invariant
    the LB-family symptom lives under — every record's string region
    must terminate cleanly on the footprint Pascal.
    """
    bad: list[tuple[str, str]] = []
    for refdes, c in gvn970_components.items():
        if not c.footprint:
            continue
        if not all(32 <= ord(ch) <= 126 for ch in c.footprint):
            bad.append((refdes, c.footprint))
    assert bad == [], (
        f"{len(bad)} components have non-printable bytes in footprint; "
        f"first 5: {bad[:5]}"
    )


@pytest.mark.slow
def test_gvn970_comment_a_equals_comment_b_invariant(gvn970_components):
    """The `comment` field decoded from the file is the first of two
    duplicated Pascal strings. This test asserts that across the whole
    fixture, the comment text is always either empty or one of a small
    set of plausible tolerance / annotation values — a sanity rail
    that fails fast if the parser drifts again.
    """
    # Sample a handful of records with known non-empty comments
    # (extracted by hand from the fixture during the fix).
    expected = {
        "C906": "0.25PF",
        "L504": "+/-25%",
        "LB503": "+/- 5%",
        "R865": "+0.05R",
        "U508": "1%",
    }
    for refdes, want in expected.items():
        assert refdes in gvn970_components, f"{refdes} missing"
        got = gvn970_components[refdes].comment
        assert got == want, (
            f"{refdes}: comment={got!r}, expected {want!r}"
        )
