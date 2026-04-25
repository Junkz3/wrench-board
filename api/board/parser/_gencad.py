# SPDX-License-Identifier: Apache-2.0
"""GenCAD 1.4 parser — used in the wild for `.cad` boardview files.

GenCAD is a public ASCII interchange format for PCB CAD data,
originally introduced by Mentor Graphics and now used by Cadence
Allegro Free Physical Viewer, BoardViewer 2.x, and various repair-
shop redistributions. Files start with `$HEADER` / `GENCAD 1.4` and
carry a sequence of `$SECTION ... $ENDSECTION` blocks.

Sections we care about (everything else is ignored):

- `$SHAPES`     — footprint library: relative pin layout per shape.
                  `SHAPE <name>` opens a block, then one or more
                  `PIN <num> <padstack> <rx> <ry> <layer> <rot> <flags>`
                  lines describe each pin in shape-local coordinates.
- `$COMPONENTS` — placed instances:
                  `COMPONENT <refdes>` opens a block, then
                  `PLACE <x> <y>`, `LAYER TOP|BOTTOM`,
                  `ROTATION <deg>`, `SHAPE <name> [MIRRORY FLIP]`,
                  `DEVICE <devname>`.
- `$SIGNALS`    — nets: `SIGNAL <name>` then one or more
                  `NODE <refdes> <pin_number>` lines.
- `$DEVICES`    — device library: `VALUE` enriches the part value.
- `$TESTPINS`   — test pin entries (mapped to nails). Optional.
- `$BOARD`      — board outline polygon. Optional and often empty.

Placement transform for a component on layer L with rotation R and
mirror flag M:
    world_pin_x = place_x + rx * cos(R) - ry_or_mirrored * sin(R)
    world_pin_y = place_y + rx * sin(R) + ry_or_mirrored * cos(R)
where `ry_or_mirrored` is `-ry` when MIRRORY is set OR the component
is on BOTTOM. Pin layer follows the component layer (TOP or BOTTOM).

Coordinates in GenCAD files are typically floats; we round to int
mils to match the unified `Board` model.

Written from scratch by inspecting real `.cad` files (ASUS Prime,
Granger). No code copied from any external codebase.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from api.board.model import Board, Layer, Nail, Net, Part, Pin, Point
from api.board.parser._ascii_boardview import GROUND_RE, POWER_RE
from api.board.parser.base import InvalidBoardFile, MalformedHeaderError


def looks_like_gencad(text: str) -> bool:
    """Sniff `$HEADER` / `GENCAD` markers in the first ~1 KB."""
    head = text[:1024]
    return "$HEADER" in head and "GENCAD" in head


# ---------------------------------------------------------------------------
# Section walker
# ---------------------------------------------------------------------------


_SECTION_RE = re.compile(
    r"^\$([A-Z]+)\s*$(.*?)^\$END\1\s*$", re.DOTALL | re.MULTILINE
)


def _split_sections(text: str) -> dict[str, str]:
    """Return `{section_name: body_text}` for every `$X ... $ENDX` block."""
    out: dict[str, str] = {}
    for m in _SECTION_RE.finditer(text):
        # First match wins per section name (real files don't repeat sections).
        out.setdefault(m.group(1), m.group(2))
    return out


# ---------------------------------------------------------------------------
# $SHAPES — footprint library
# ---------------------------------------------------------------------------


@dataclass
class _ShapePin:
    name: str        # GenCAD pin number/name as a string ("1", "A1", …)
    rx: float
    ry: float
    rotation_deg: float = 0.0


@dataclass
class _Shape:
    name: str
    pins: list[_ShapePin] = field(default_factory=list)


def _parse_shapes(body: str) -> dict[str, _Shape]:
    """Parse the `$SHAPES` body into a dict of shape_name → _Shape."""
    shapes: dict[str, _Shape] = {}
    current: _Shape | None = None
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("ATTRIBUTE"):
            continue
        toks = line.split()
        if toks[0] == "SHAPE" and len(toks) >= 2:
            name = " ".join(toks[1:]).strip()
            current = _Shape(name=name)
            shapes[name] = current
        elif toks[0] == "PIN" and current is not None and len(toks) >= 6:
            # PIN <num> <padstack> <rx> <ry> <layer> [rot] [flags]
            pin_name = toks[1]
            try:
                rx = float(toks[3])
                ry = float(toks[4])
            except ValueError:
                continue
            rotation = 0.0
            if len(toks) >= 7:
                try:
                    rotation = float(toks[6])
                except ValueError:
                    rotation = 0.0
            current.pins.append(
                _ShapePin(name=pin_name, rx=rx, ry=ry, rotation_deg=rotation)
            )
        # INSERT, MIRROR, etc. ignored at the shape level — they live on the
        # component instance.
    return shapes


# ---------------------------------------------------------------------------
# $COMPONENTS — placed instances
# ---------------------------------------------------------------------------


@dataclass
class _Component:
    refdes: str
    place_x: float = 0.0
    place_y: float = 0.0
    layer: Layer = Layer.TOP
    rotation_deg: float = 0.0
    shape_name: str = ""
    mirror: bool = False
    device: str | None = None


def _parse_components(body: str) -> list[_Component]:
    out: list[_Component] = []
    current: _Component | None = None

    def flush():
        nonlocal current
        if current is not None and current.refdes:
            out.append(current)
        current = None

    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("ATTRIBUTE"):
            continue
        toks = line.split()
        if toks[0] == "COMPONENT" and len(toks) >= 2:
            flush()
            current = _Component(refdes=toks[1])
        elif current is None:
            continue
        elif toks[0] == "PLACE" and len(toks) >= 3:
            try:
                current.place_x = float(toks[1])
                current.place_y = float(toks[2])
            except ValueError:
                pass
        elif toks[0] == "LAYER" and len(toks) >= 2:
            current.layer = Layer.BOTTOM if toks[1].upper() == "BOTTOM" else Layer.TOP
        elif toks[0] == "ROTATION" and len(toks) >= 2:
            try:
                current.rotation_deg = float(toks[1])
            except ValueError:
                pass
        elif toks[0] == "SHAPE" and len(toks) >= 2:
            # SHAPE <name> [MIRRORY|MIRRORX|FLIP] [optional numeric extras…]
            # The name is always the first token after SHAPE in observed
            # files (ASUS, GRANGER). Trailing tokens are flags or version
            # numbers — flags set `mirror`, numbers are dropped.
            current.shape_name = toks[1]
            mirror = False
            for t in toks[2:]:
                if t in ("MIRRORY", "MIRRORX", "FLIP"):
                    mirror = True
            current.mirror = mirror
        elif toks[0] == "DEVICE" and len(toks) >= 2:
            current.device = toks[1]

    flush()
    return out


# ---------------------------------------------------------------------------
# $DEVICES — for VALUE enrichment
# ---------------------------------------------------------------------------


def _parse_device_values(body: str) -> dict[str, str]:
    """Return `{device_name: value}` for VALUE lookups."""
    out: dict[str, str] = {}
    current: str | None = None
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        toks = line.split(maxsplit=1)
        if toks[0] == "DEVICE" and len(toks) == 2:
            current = toks[1]
        elif toks[0] == "VALUE" and len(toks) == 2 and current:
            out[current] = toks[1]
    return out


# ---------------------------------------------------------------------------
# $SIGNALS — nets
# ---------------------------------------------------------------------------


def _parse_signals(body: str) -> dict[tuple[str, str], str]:
    """Return `{(refdes, pin_name): net_name}`."""
    out: dict[tuple[str, str], str] = {}
    current_net: str | None = None
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("ATTRIBUTE"):
            continue
        toks = line.split()
        if toks[0] == "SIGNAL" and len(toks) >= 2:
            current_net = " ".join(toks[1:])
        elif toks[0] == "NODE" and len(toks) >= 3 and current_net:
            refdes = toks[1]
            pin_name = toks[2]
            out[(refdes, pin_name)] = current_net
    return out


# ---------------------------------------------------------------------------
# $TESTPINS — nails
# ---------------------------------------------------------------------------


def _parse_testpins(body: str) -> list[tuple[str, str]]:
    """Each TESTPIN line is `TESTPIN <signal> <refdes> <pin>` in observed files."""
    out: list[tuple[str, str]] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line.startswith("TESTPIN"):
            continue
        toks = line.split()
        if len(toks) >= 4:
            out.append((toks[2], toks[3]))  # (refdes, pin_name)
    return out


# ---------------------------------------------------------------------------
# Coordinate transform & assembly
# ---------------------------------------------------------------------------


def _world_pin_position(comp: _Component, sp: _ShapePin) -> tuple[int, int]:
    """Apply rotation + mirror + translate to get world-space pin coords."""
    rx = sp.rx
    ry = sp.ry
    # MIRRORY flag flips Y of the shape pin (mirror across X axis).
    # Component on BOTTOM also implies the part is flipped — same behavior.
    if comp.mirror or comp.layer == Layer.BOTTOM:
        ry = -ry
    theta = math.radians(comp.rotation_deg)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    wx = comp.place_x + rx * cos_t - ry * sin_t
    wy = comp.place_y + rx * sin_t + ry * cos_t
    return int(round(wx)), int(round(wy))


def parse_gencad(
    text: str, *, file_hash: str, board_id: str, source_format: str = "cad"
) -> Board:
    if not looks_like_gencad(text):
        raise InvalidBoardFile(f"{source_format}: not a GenCAD file ($HEADER/GENCAD missing)")

    sections = _split_sections(text)
    if "SHAPES" not in sections or "COMPONENTS" not in sections:
        raise MalformedHeaderError("gencad: missing $SHAPES or $COMPONENTS")

    shapes = _parse_shapes(sections["SHAPES"])
    components = _parse_components(sections["COMPONENTS"])
    device_values = _parse_device_values(sections.get("DEVICES", ""))
    signals = _parse_signals(sections.get("SIGNALS", ""))
    testpin_specs = _parse_testpins(sections.get("TESTPINS", ""))

    parts: list[Part] = []
    pins: list[Pin] = []
    pin_lookup_by_refdes_pinname: dict[tuple[str, str], int] = {}

    for comp in components:
        shape = shapes.get(comp.shape_name)
        if shape is None:
            # Component references an unknown shape — emit the part with no
            # pins rather than fabricating data (anti-hallucination rule).
            parts.append(
                Part(
                    refdes=comp.refdes,
                    layer=comp.layer,
                    is_smd=True,
                    bbox=(Point(x=int(round(comp.place_x)), y=int(round(comp.place_y))),
                          Point(x=int(round(comp.place_x)), y=int(round(comp.place_y)))),
                    pin_refs=[],
                    value=device_values.get(comp.device or "", None),
                    footprint=comp.shape_name or None,
                    rotation_deg=comp.rotation_deg,
                )
            )
            continue
        pin_refs: list[int] = []
        xs, ys = [], []
        for local_idx, sp in enumerate(shape.pins, start=1):
            x, y = _world_pin_position(comp, sp)
            net_name = signals.get((comp.refdes, sp.name))
            # Pin index: prefer numeric pin name, fallback to local order.
            try:
                pin_idx = int(sp.name)
                if pin_idx <= 0:
                    pin_idx = local_idx
            except ValueError:
                pin_idx = local_idx
            pins.append(
                Pin(
                    part_refdes=comp.refdes,
                    index=pin_idx,
                    pos=Point(x=x, y=y),
                    net=net_name,
                    probe=None,
                    layer=comp.layer,
                )
            )
            pin_refs.append(len(pins) - 1)
            pin_lookup_by_refdes_pinname[(comp.refdes, sp.name)] = len(pins) - 1
            xs.append(x)
            ys.append(y)
        if xs and ys:
            bbox = (Point(x=min(xs), y=min(ys)), Point(x=max(xs), y=max(ys)))
        else:
            bx = int(round(comp.place_x))
            by = int(round(comp.place_y))
            bbox = (Point(x=bx, y=by), Point(x=bx, y=by))
        parts.append(
            Part(
                refdes=comp.refdes,
                layer=comp.layer,
                is_smd=True,
                bbox=bbox,
                pin_refs=pin_refs,
                value=device_values.get(comp.device or "", None),
                footprint=comp.shape_name or None,
                rotation_deg=comp.rotation_deg,
            )
        )

    nets = _derive_nets(pins)

    nails: list[Nail] = []
    for probe_idx, (refdes, pin_name) in enumerate(testpin_specs, start=1):
        ref = pin_lookup_by_refdes_pinname.get((refdes, pin_name))
        if ref is None:
            continue
        pin = pins[ref]
        nails.append(
            Nail(
                probe=probe_idx,
                pos=pin.pos,
                layer=pin.layer,
                net=pin.net or "",
            )
        )

    return Board(
        board_id=board_id,
        file_hash=file_hash,
        source_format=source_format,
        outline=[],  # $BOARD section often empty in the wild
        parts=parts,
        pins=pins,
        nets=nets,
        nails=nails,
    )


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
