"""Board data model — Pydantic v2 types for Point, Layer, Pin, Part, Net, Nail, and Board. Board carries private refdes/net indexes built in model_post_init ; see part_by_refdes() and net_by_name()."""

from __future__ import annotations

from enum import IntFlag

from pydantic import BaseModel, PrivateAttr


class Layer(IntFlag):
    TOP = 1
    BOTTOM = 2
    BOTH = TOP | BOTTOM


class Point(BaseModel):
    x: int  # mils (1 unit = 0.025 mm, per OBV convention)
    y: int


class Pin(BaseModel):
    part_refdes: str
    index: int
    pos: Point
    net: str | None = None
    probe: int | None = None
    layer: Layer


class Part(BaseModel):
    refdes: str
    layer: Layer
    is_smd: bool
    bbox: tuple[Point, Point]  # (min, max)
    pin_refs: list[int]


class Net(BaseModel):
    name: str
    pin_refs: list[int]
    is_power: bool = False
    is_ground: bool = False


class Nail(BaseModel):
    probe: int
    pos: Point
    layer: Layer
    net: str


class Board(BaseModel):
    board_id: str
    file_hash: str
    source_format: str
    outline: list[Point]
    parts: list[Part]
    pins: list[Pin]
    nets: list[Net]
    nails: list[Nail]

    # Private indexes built in model_post_init — excluded from serialization.
    # Pydantic v2 : PrivateAttr (NOT Field) for non-serialized state.
    _refdes_index: dict[str, Part] = PrivateAttr(default_factory=dict)
    _net_index: dict[str, Net] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context) -> None:
        object.__setattr__(self, "_refdes_index", {p.refdes: p for p in self.parts})
        object.__setattr__(self, "_net_index", {n.name: n for n in self.nets})

    def model_copy(self, *, update=None, deep=False):
        copy = super().model_copy(update=update, deep=deep)
        object.__setattr__(copy, "_refdes_index", {p.refdes: p for p in copy.parts})
        object.__setattr__(copy, "_net_index", {n.name: n for n in copy.nets})
        return copy

    def part_by_refdes(self, refdes: str) -> Part | None:
        return self._refdes_index.get(refdes)

    def net_by_name(self, name: str) -> Net | None:
        return self._net_index.get(name)
