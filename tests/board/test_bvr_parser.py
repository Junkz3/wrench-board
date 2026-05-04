"""`.bvr` parser — synthetic-binary round-trip + error paths."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from api.board.model import Layer
from api.board.parser.base import (
    InvalidBoardFile,
    PinPartMismatchError,
    parser_for,
)
from api.board.parser.bvr import MAGIC, BVRParser

# --- Tiny BVR builder used by every test. ---------------------------------
#
# Returns a byte string that exactly matches the layout BVRParser expects.
# Keeping the writer in the test file (instead of a `bvr_writer` module in
# `api/`) keeps the parser one-directional and avoids carrying serializer
# code we'd never use in production.


def _lstr(s: str) -> bytes:
    enc = s.encode("utf-8")
    if len(enc) > 0xFF:
        raise ValueError("test fixture string too long for uint8 length prefix")
    return bytes([len(enc)]) + enc


def _u32(n: int) -> bytes:
    return struct.pack("<I", n)


def _i32(n: int) -> bytes:
    return struct.pack("<i", n)


def _f32(x: float) -> bytes:
    return struct.pack("<f", x)


def _build_bvr(
    *,
    outline: list[tuple[int, int]] | None = None,
    parts: list[dict] | None = None,
    pins: list[dict] | None = None,
    nails: list[dict] | None = None,
    version: int = 1,
) -> bytes:
    outline = outline or []
    parts = parts or []
    pins = pins or []
    nails = nails or []

    out = bytearray(MAGIC)
    out += _u32(version)

    out += _u32(len(outline))
    for x, y in outline:
        out += _i32(x) + _i32(y)

    out += _u32(len(parts))
    for p in parts:
        flags = 0
        if p.get("layer", Layer.TOP) == Layer.TOP:
            flags |= 0x01
        if p.get("smd", True):
            flags |= 0x02
        out += _lstr(p["refdes"])
        out += _i32(p["x"]) + _i32(p["y"])
        out += _f32(p.get("rotation", 0.0))
        out += bytes([flags])
        out += _f32(p.get("w", 0.0)) + _f32(p.get("h", 0.0))

    out += _u32(len(pins))
    for pin in pins:
        out += _i32(pin["x"]) + _i32(pin["y"])
        out += _i32(pin.get("probe", -99))
        out += _u32(pin["part_index"])
        out += bytes([pin.get("side", 3)])  # default TOP
        out += _lstr(pin.get("net", ""))
        out += _lstr(pin.get("number", ""))
        out += _lstr(pin.get("name", ""))

    out += _u32(len(nails))
    for n in nails:
        out += _i32(n["probe"]) + _i32(n["x"]) + _i32(n["y"])
        out += bytes([n.get("side", 2)])  # default TOP
        out += _lstr(n["net"])

    return bytes(out)


# --- Tests ----------------------------------------------------------------


def test_dispatches_bvr_extension(tmp_path: Path):
    f = tmp_path / "demo.bvr"
    f.write_bytes(b"\x00")
    assert isinstance(parser_for(f), BVRParser)


def test_rejects_bad_magic():
    with pytest.raises(InvalidBoardFile, match="bad magic"):
        BVRParser().parse(b"WRONG\x00\x00\x00", file_hash="x", board_id="b")


def test_truncated_file_raises():
    raw = MAGIC + _u32(1)  # version, then nothing else
    with pytest.raises(InvalidBoardFile, match="unexpected EOF"):
        BVRParser().parse(raw, file_hash="x", board_id="b")


def test_pin_part_index_out_of_range():
    raw = _build_bvr(
        parts=[
            {"refdes": "R1", "x": 0, "y": 0, "w": 10, "h": 10, "layer": Layer.TOP},
        ],
        pins=[
            {"x": 0, "y": 0, "probe": 1, "part_index": 5, "net": "GND"},
        ],
    )
    with pytest.raises(PinPartMismatchError):
        BVRParser().parse(raw, file_hash="x", board_id="b")


def test_invalid_refdes_rejected():
    raw = _build_bvr(
        parts=[{"refdes": "1bad", "x": 0, "y": 0, "w": 10, "h": 10}],
    )
    with pytest.raises(InvalidBoardFile, match="invalid part refdes"):
        BVRParser().parse(raw, file_hash="x", board_id="b")


def test_minimal_round_trip_structure():
    raw = _build_bvr(
        outline=[(0, 0), (100, 0), (100, 60), (0, 60)],
        parts=[
            {
                "refdes": "R1",
                "x": 10,
                "y": 10,
                "w": 8,
                "h": 4,
                "rotation": 90.0,
                "layer": Layer.TOP,
                "smd": True,
            },
            {
                "refdes": "C1",
                "x": 50,
                "y": 30,
                "w": 6,
                "h": 6,
                "layer": Layer.BOTTOM,
                "smd": False,
            },
        ],
        pins=[
            {"x": 6, "y": 10, "probe": 1, "part_index": 0, "side": 3, "net": "+3V3"},
            {"x": 14, "y": 10, "probe": 2, "part_index": 0, "side": 3, "net": "GND"},
            {"x": 47, "y": 30, "probe": 3, "part_index": 1, "side": 2, "net": "+3V3"},
            {"x": 53, "y": 30, "probe": -99, "part_index": 1, "side": 2, "net": ""},
        ],
        nails=[
            {"probe": 99, "x": 80, "y": 50, "side": 2, "net": "GND"},
        ],
    )

    board = BVRParser().parse(raw, file_hash="sha256:abc", board_id="bvr-min")

    assert board.source_format == "bvr"
    assert board.board_id == "bvr-min"
    assert len(board.outline) == 4

    assert len(board.parts) == 2
    r1 = board.part_by_refdes("R1")
    c1 = board.part_by_refdes("C1")
    assert r1.layer == Layer.TOP
    assert r1.is_smd is True
    assert r1.rotation_deg == pytest.approx(90.0)
    assert r1.bbox == (board.parts[0].bbox[0], board.parts[0].bbox[1])
    # bbox derived from pin span (x=6..14, y=10..10)
    assert (r1.bbox[0].x, r1.bbox[0].y) == (6, 10)
    assert (r1.bbox[1].x, r1.bbox[1].y) == (14, 10)
    assert c1.layer == Layer.BOTTOM
    assert c1.is_smd is False
    # Each part owns the right number of pins, in declaration order.
    assert r1.pin_refs == [0, 1]
    assert c1.pin_refs == [2, 3]

    assert len(board.pins) == 4
    p0 = board.pins[0]
    assert p0.part_refdes == "R1"
    assert p0.index == 1  # 1-based per part
    assert p0.layer == Layer.TOP
    assert p0.net == "+3V3"
    assert p0.probe == 1
    # probe == -99 is the "no probe" sentinel
    assert board.pins[3].probe is None
    assert board.pins[3].net is None  # blank net string normalised to None
    # bottom-side pin maps to Layer.BOTTOM
    assert board.pins[2].layer == Layer.BOTTOM

    assert len(board.nails) == 1
    nail = board.nails[0]
    assert nail.probe == 99
    assert nail.net == "GND"
    assert nail.layer == Layer.TOP  # nail side == 2 → TOP

    # Net classification routes through the shared regex helpers.
    assert board.net_by_name("+3V3").is_power is True
    assert board.net_by_name("+3V3").is_ground is False
    assert board.net_by_name("GND").is_ground is True
    # Nets sorted alphabetically for deterministic output.
    assert [n.name for n in board.nets] == sorted(n.name for n in board.nets)


def test_part_with_no_pins_uses_declared_bbox():
    raw = _build_bvr(
        parts=[
            {
                "refdes": "U1",
                "x": 100,
                "y": 200,
                "w": 40,
                "h": 20,
                "layer": Layer.TOP,
            },
        ],
    )
    board = BVRParser().parse(raw, file_hash="x", board_id="b")
    u1 = board.part_by_refdes("U1")
    # bbox derived from x±w/2, y±h/2 when no pins are present
    assert (u1.bbox[0].x, u1.bbox[0].y) == (80, 190)
    assert (u1.bbox[1].x, u1.bbox[1].y) == (120, 210)
    assert u1.pin_refs == []
