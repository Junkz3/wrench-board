"""Board data model — Pydantic v2 types for Point, Layer, Pin, Part, Net, Nail, and Board. Board carries private refdes/net indexes built in model_post_init ; see part_by_refdes() and net_by_name()."""

from __future__ import annotations

from enum import IntFlag

from pydantic import BaseModel, PrivateAttr


class Layer(IntFlag):
    TOP = 1
    BOTTOM = 2
    BOTH = TOP | BOTTOM


class Point(BaseModel):
    x: float  # mils (1 unit = 0.0254 mm, per OBV convention)
    y: float
    # Float — sub-mil precision is required for XZZ board-to-board
    # connector pads (XW8230 et al. land at ~0.025 mm, well under
    # the 1 mil grid). int truncation rounded their absolute position
    # by up to half a mil, leaving them visibly off-centre versus the
    # silkscreen body.


class Pin(BaseModel):
    part_refdes: str
    index: int
    pos: Point
    net: str | None = None
    probe: int | None = None
    layer: Layer
    pad_shape: str | None = None  # "rect" | "circle"
    pad_size: tuple[float, float] | None = None  # (width, height) in mils
    # Float — sub-mil pads on XZZ board-to-board connectors (e.g. XW8230
    # at ~0.9 mil) need fractional precision; int truncation collapsed
    # them to 1×1 mil = 0.0254×0.0254 mm.
    pad_rotation_deg: float | None = None


class Segment(BaseModel):
    """A 2-point line — used for component body silkscreen and global traces."""

    a: Point
    b: Point


class Part(BaseModel):
    refdes: str
    layer: Layer
    is_smd: bool
    bbox: tuple[Point, Point]  # (min, max)
    pin_refs: list[int]
    value: str | None = None
    footprint: str | None = None
    rotation_deg: float | None = None
    # Source-format-specific category tag. XZZ surfaces "TP" for genuine
    # test pads (single-pin probe lands at the manufacturer's tags,
    # e.g. TP-P5 on manufacturer-tagged exports). Kept format-opaque so
    # the render layer can branch on it (test-pad gold colouring)
    # without the parser leaking a hard-coded refdes-prefix list.
    category: str | None = None
    # Component silkscreen / outline segments. Populated by parsers that
    # surface this (XZZ via 0x05 sub-blocks); empty for parsers that don't.
    body_lines: list[Segment] = []
    # DFM alternate / DNP (Do Not Populate) tracking. Asus `.fz` dumps
    # encode pads physically shared by two alternative footprints (e.g.
    # `R54` jumper vs `C30` cap, or `PGCE30` 8x9.7 polymer cap vs
    # `PGCE12` 7x8 polymer cap) at the same physical position. Only one
    # of the two is actually soldered on the board — the populated one
    # appears in the BOM and carries `value`, the other is DNP. The
    # parser flags the unplaced ghost with `is_dnp=True` and attaches
    # its refdes to the placed sibling's `dnp_alternates`.
    is_dnp: bool = False
    dnp_alternates: list[str] = []


class Net(BaseModel):
    name: str
    pin_refs: list[int]
    is_power: bool = False
    is_ground: bool = False
    # Diagnostic expectations surfaced by the source format (XZZ
    # post-v6 block on manufacturer-tagged dumps). The probe
    # convention is "measure to GND on the powered-down board"; OL
    # / 开路 in the source becomes `expected_open=True` with no ohms
    # reading. None on both fields = no expectation in the file.
    expected_resistance_ohms: float | None = None
    expected_open: bool = False
    # Optional expected DC voltage measured on the powered board,
    # surfaced when the source format ships a 电压 (voltage) section.
    # In volts.
    expected_voltage_v: float | None = None


class Nail(BaseModel):
    probe: int
    pos: Point
    layer: Layer
    net: str


class Via(BaseModel):
    pos: Point
    radius: float  # mils
    net: str | None = None
    # Padstack name from the source format (GenCAD `VIA <padstack> ...`).
    # Lets the renderer / UI distinguish the 14+ via types a single MSI
    # board ships (PAD_VTH_048C028P standard through-hole vs PAD_VTB_*
    # top-to-buried microvia vs PAD_VSB_* surface-to-buried), which all
    # carry the same shape but different drill / pad geometry. None when
    # the source format doesn't carry per-via padstack identity.
    padstack: str | None = None
    # Layer span declared by the source: "ALL" (through-hole, anode→cathode),
    # "TOP" / "BOTTOM" (one face only — blind via), or a "L1-L4" style
    # range (buried microvia). Defaults to "ALL" when absent.
    layer_span: str | None = None


class MechanicalHole(BaseModel):
    """Drilled hole declared by the source format outside the routing
    layers — mounting holes, tooling holes, fiducial dots. GenCAD ships
    these in `$MECH` as `HOLE x y diameter`. Without them the renderer
    has no anchor for the actual board corners (the largest closed
    silkscreen artwork can be a logo, not the PCB edge)."""

    pos: Point
    diameter: float  # mils
    # True for small centre-marker dots (typically ≤100 mils); False
    # for the larger PCB-fixation through-holes at the corners.
    is_fiducial: bool = False


class TestPad(BaseModel):
    pos: Point
    radius: float  # mils
    layer: Layer
    net: str | None = None


class Trace(BaseModel):
    """A copper segment between two points on a given layer."""

    a: Point
    b: Point
    layer: int  # raw layer index from the source format (0/1/16/…)
    width: float = 0.0  # mils; 0 when unknown
    net: str | None = None


class Arc(BaseModel):
    """Circular arc — used for board outlines and rounded silkscreen.

    Angles in degrees; the arc is traced counter-clockwise from
    `angle_start` to `angle_end` around `center`.
    """

    center: Point
    radius: float  # mils
    angle_start: float  # degrees
    angle_end: float  # degrees
    layer: int


class Marker(BaseModel):
    """Manufacturer-tagged rectangular region on the board.

    XZZ ships these as type_03 blocks (only on diagnostic-tagged
    exports; absent on most boards). Likely "inspection zones" /
    fiducials the OEM marked for diagnostic guidance — they don't
    necessarily align with a single component bbox, often span a
    cluster. The viewer renders them as semi-transparent rectangles
    overlaid on the board.

    Coords in mils, post-board-translation (same frame as pins /
    parts / silkscreen).
    """

    centre: Point
    bbox: tuple[Point, Point]  # (min, max)
    marker_id: int = 0  # opaque tag from the source (=17 in the wild)


class Board(BaseModel):
    board_id: str
    file_hash: str
    source_format: str
    outline: list[Point]
    parts: list[Part]
    pins: list[Pin]
    nets: list[Net]
    nails: list[Nail]
    # Optional richer-rendering layers — empty when the parser doesn't
    # surface them. Consumed by api/board/render.py for the Three.js viewer.
    vias: list[Via] = []
    test_pads: list[TestPad] = []
    traces: list[Trace] = []
    arcs: list[Arc] = []
    markers: list[Marker] = []
    mech_holes: list[MechanicalHole] = []

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
