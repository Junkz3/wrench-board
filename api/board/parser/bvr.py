"""`.bvr` boardview parser.

Binary Board Viewer file. Compact little-endian layout with a four-byte
ASCII magic header, four ordered sections (outline / parts / pins / nails),
each prefixed by a uint32 count.

Layout (all integers little-endian):

    magic           : 4 bytes  = b"BVR\\0"
    version         : uint32
    n_format_pts    : uint32
    [format_pt]:
        x : int32
        y : int32
    n_parts         : uint32
    [part]:
        name_len    : uint8
        name        : utf-8 (name_len bytes)
        x           : int32
        y           : int32
        rotation    : float32
        flags       : uint8        (bit 0 = TOP, bit 1 = SMD)
        width       : float32
        height      : float32
    n_pins          : uint32
    [pin]:
        x           : int32
        y           : int32
        probe       : int32
        part_index  : uint32
        side        : uint8        (1=BOTH, 2=BOTTOM, 3=TOP)
        net_len     : uint8
        net         : utf-8 (net_len bytes)
        number_len  : uint8
        number      : utf-8 (number_len bytes, may be empty)
        name_len    : uint8
        name        : utf-8 (name_len bytes, may be empty)
    n_nails         : uint32
    [nail]:
        probe       : int32
        x           : int32
        y           : int32
        side        : uint8        (0=BOTH, 1=BOTTOM, 2=TOP)
        net_len     : uint8
        net         : utf-8 (net_len bytes)
"""

from __future__ import annotations

import re
import struct

from api.board.model import Board, Layer, Nail, Net, Part, Pin, Point
from api.board.parser._ascii_boardview import GROUND_RE, POWER_RE
from api.board.parser.base import (
    BoardParser,
    InvalidBoardFile,
    PinPartMismatchError,
    register,
)

MAGIC = b"BVR\x00"

_PROBE_NULL = -99  # convention shared with the Test_Link parser

_PIN_SIDE_TO_LAYER: dict[int, Layer] = {
    1: Layer.BOTH,
    2: Layer.BOTTOM,
    3: Layer.TOP,
}
_NAIL_SIDE_TO_LAYER: dict[int, Layer] = {
    0: Layer.BOTH,
    1: Layer.BOTTOM,
    2: Layer.TOP,
}

_VALID_REFDES = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")


class _Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        end = self.pos + n
        if end > len(self.data):
            raise InvalidBoardFile(f"bvr: unexpected EOF at offset {self.pos} (need {n} bytes)")
        out = self.data[self.pos : end]
        self.pos = end
        return out

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self.take(4))[0]

    def lstr(self) -> str:
        n = self.u8()
        if n == 0:
            return ""
        return self.take(n).decode("utf-8", errors="replace")


def _derive_nets(pins: list[Pin]) -> list[Net]:
    by_name: dict[str, list[int]] = {}
    for i, pin in enumerate(pins):
        if pin.net is None:
            continue
        by_name.setdefault(pin.net, []).append(i)
    return [
        Net(
            name=name,
            pin_refs=refs,
            is_power=bool(POWER_RE.match(name)),
            is_ground=bool(GROUND_RE.match(name)),
        )
        for name, refs in sorted(by_name.items())
    ]


@register
class BVRParser(BoardParser):
    extensions = (".bvr",)

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        if len(raw) < 4 or raw[:4] != MAGIC:
            raise InvalidBoardFile(f"bvr: bad magic header: {raw[:4]!r}")

        r = _Reader(raw)
        r.take(4)  # consume magic
        r.u32()  # version — read for layout, currently unused

        outline = [Point(x=r.i32(), y=r.i32()) for _ in range(r.u32())]

        n_parts = r.u32()
        raw_parts: list[tuple[str, int, int, float, Layer, bool, float, float]] = []
        for _ in range(n_parts):
            name = r.lstr()
            if not name or not _VALID_REFDES.match(name):
                raise InvalidBoardFile(f"bvr: invalid part refdes {name!r}")
            x = r.i32()
            y = r.i32()
            rotation = r.f32()
            flags = r.u8()
            layer = Layer.TOP if (flags & 0x01) else Layer.BOTTOM
            is_smd = bool(flags & 0x02)
            w = r.f32()
            h = r.f32()
            raw_parts.append((name, x, y, rotation, layer, is_smd, w, h))

        n_pins = r.u32()
        raw_pins: list[tuple[int, int, int, int, int, str]] = []
        for _ in range(n_pins):
            x = r.i32()
            y = r.i32()
            probe = r.i32()
            part_index = r.u32()
            side = r.u8()
            net = r.lstr()
            r.lstr()  # number — kept for layout, not surfaced in Pin model
            r.lstr()  # name   — idem
            if part_index >= n_parts:
                raise PinPartMismatchError(len(raw_pins))
            raw_pins.append((x, y, probe, part_index, side, net))

        nails: list[Nail] = []
        for _ in range(r.u32()):
            probe = r.i32()
            x = r.i32()
            y = r.i32()
            side = r.u8()
            net = r.lstr()
            nails.append(
                Nail(
                    probe=probe,
                    pos=Point(x=x, y=y),
                    layer=_NAIL_SIDE_TO_LAYER.get(side, Layer.BOTTOM),
                    net=net,
                )
            )

        pin_refs_by_part: list[list[int]] = [[] for _ in range(n_parts)]
        pins: list[Pin] = []
        for i, (x, y, probe, part_index, side, net) in enumerate(raw_pins):
            owner_layer = raw_parts[part_index][4]
            pin_layer = _PIN_SIDE_TO_LAYER.get(side, owner_layer)
            pin_refs_by_part[part_index].append(i)
            pins.append(
                Pin(
                    part_refdes=raw_parts[part_index][0],
                    index=len(pin_refs_by_part[part_index]),  # 1-based within part
                    pos=Point(x=x, y=y),
                    net=(net or None),
                    probe=(probe if probe != _PROBE_NULL else None),
                    layer=pin_layer,
                )
            )

        parts: list[Part] = []
        for k, (name, px, py, rotation, layer, is_smd, pw, ph) in enumerate(raw_parts):
            refs = pin_refs_by_part[k]
            if refs:
                xs = [pins[j].pos.x for j in refs]
                ys = [pins[j].pos.y for j in refs]
                bbox: tuple[Point, Point] = (
                    Point(x=min(xs), y=min(ys)),
                    Point(x=max(xs), y=max(ys)),
                )
            elif pw > 0 or ph > 0:
                hw = round(pw / 2)
                hh = round(ph / 2)
                bbox = (Point(x=px - hw, y=py - hh), Point(x=px + hw, y=py + hh))
            else:
                bbox = (Point(x=px, y=py), Point(x=px, y=py))
            parts.append(
                Part(
                    refdes=name,
                    layer=layer,
                    is_smd=is_smd,
                    bbox=bbox,
                    pin_refs=refs,
                    rotation_deg=(rotation if rotation else None),
                )
            )

        return Board(
            board_id=board_id,
            file_hash=file_hash,
            source_format="bvr",
            outline=outline,
            parts=parts,
            pins=pins,
            nets=_derive_nets(pins),
            nails=nails,
        )
