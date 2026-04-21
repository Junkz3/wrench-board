"""Board data model — Pydantic v2 types (Point, Layer, Pin, Part). Immutability is layered on in a later task alongside the Board index classes."""

from __future__ import annotations

from enum import IntFlag

from pydantic import BaseModel


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
