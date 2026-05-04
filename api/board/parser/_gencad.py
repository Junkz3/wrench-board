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

from api.board.model import Arc, Board, Layer, MechanicalHole, Nail, Net, Part, Pin, Point, Trace, Via
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
    padstack: str    # padstack name referenced — resolves to pad shape/size
    rx: float
    ry: float
    rotation_deg: float = 0.0
    # Per-pin layer from the SHAPES section (`PIN num pad rx ry LAYER`).
    # Critical for dual-face footprints — a PCI Express edge connector
    # has half its pads on TOP and half on BOTTOM, even though the
    # component frame is placed on TOP. Without this the BOTTOM pads
    # land in `pad_shape=None` because `_pad_for_pin(comp.layer)` only
    # ever queries the TOP pad of the padstack.
    layer: str = "TOP"


@dataclass
class _Shape:
    name: str
    pins: list[_ShapePin] = field(default_factory=list)
    # `INSERT SMD` vs `INSERT TH` (or `PTH` / `THM`) on the shape declares
    # the mounting type for every component using it. Default True
    # (overwhelming majority of GenCAD components in modern boards are
    # surface-mount); only flipped to False when an explicit through-hole
    # marker is parsed.
    is_smd: bool = True


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
            padstack_name = toks[2]
            try:
                rx = float(toks[3])
                ry = float(toks[4])
            except ValueError:
                continue
            pin_layer = toks[5].upper()
            rotation = 0.0
            if len(toks) >= 7:
                try:
                    rotation = float(toks[6])
                except ValueError:
                    rotation = 0.0
            current.pins.append(
                _ShapePin(
                    name=pin_name,
                    padstack=padstack_name,
                    rx=rx,
                    ry=ry,
                    rotation_deg=rotation,
                    layer=pin_layer,
                )
            )
        elif toks[0] == "INSERT" and current is not None and len(toks) >= 2:
            # `INSERT SMD` or `INSERT TH` / `PTH` / `THM`. The exact
            # token spelling for through-hole varies by tool — accept
            # any prefix that begins with TH or PTH.
            kind = toks[1].upper()
            if kind.startswith("TH") or kind.startswith("PTH"):
                current.is_smd = False
            else:
                current.is_smd = True
        # MIRROR / other tokens ignored at the shape level — they live on
        # the component instance.
    return shapes


# ---------------------------------------------------------------------------
# $PADS — primitive pad shapes
# ---------------------------------------------------------------------------


@dataclass
class _Pad:
    name: str
    shape: str   # "circle" | "rect"
    width: float
    height: float


def _parse_pads(body: str) -> dict[str, _Pad]:
    """Parse `$PADS`. Each `PAD <name> ROUND|RECTANGULAR|FINGER -1` line
    opens an entry and the next geometry line carries the dimensions:
    `CIRCLE x y r` (round), `RECTANGLE x y w h` (rectangular), or a
    sequence of `LINE`/`ARC` lines for FINGER (oblong) — for FINGER we
    take the bbox of the LINE/ARC endpoints as a rectangular fallback.
    """
    pads: dict[str, _Pad] = {}
    current: str | None = None
    finger_xs: list[float] = []
    finger_ys: list[float] = []

    def _flush_finger() -> None:
        nonlocal current, finger_xs, finger_ys
        if current and finger_xs and finger_ys and current not in pads:
            w = max(finger_xs) - min(finger_xs)
            h = max(finger_ys) - min(finger_ys)
            pads[current] = _Pad(current, "rect", abs(w), abs(h))
        finger_xs = []
        finger_ys = []

    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        toks = line.split()
        if toks[0] == "PAD" and len(toks) >= 3:
            _flush_finger()
            current = toks[1]
        elif toks[0] == "CIRCLE" and current and len(toks) >= 4:
            try:
                r = float(toks[3])
            except ValueError:
                current = None
                continue
            pads[current] = _Pad(current, "circle", 2 * r, 2 * r)
            current = None
        elif toks[0] == "RECTANGLE" and current and len(toks) >= 5:
            try:
                w = float(toks[3])
                h = float(toks[4])
            except ValueError:
                current = None
                continue
            pads[current] = _Pad(current, "rect", abs(w), abs(h))
            current = None
        elif toks[0] in ("LINE", "ARC") and current and len(toks) >= 5:
            try:
                finger_xs.extend([float(toks[1]), float(toks[3])])
                finger_ys.extend([float(toks[2]), float(toks[4])])
            except ValueError:
                pass
    _flush_finger()
    return pads


# ---------------------------------------------------------------------------
# $PADSTACKS — per-layer pad assembly
# ---------------------------------------------------------------------------


def _parse_padstacks(
    body: str, pads: dict[str, _Pad]
) -> tuple[dict[str, dict[str, _Pad]], dict[str, float]]:
    """Map padstack_name → (`{layer: _Pad}`, drill_diameter).

    `PADSTACK <name> <drill>` opens a block, then `PAD <pad_name> <layer>
    <rot> <flags>` adds the resolved pad to the named layer. Layers we
    keep: TOP, BOTTOM, ALL (vias). SOLDERMASK_*/SOLDERPASTE_*/INNER are
    irrelevant to the boardview render.
    """
    by_layer: dict[str, dict[str, _Pad]] = {}
    drills: dict[str, float] = {}
    current: str | None = None
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        toks = line.split()
        if toks[0] == "PADSTACK" and len(toks) >= 2:
            current = toks[1]
            by_layer[current] = {}
            try:
                drills[current] = float(toks[2]) if len(toks) >= 3 else 0.0
            except ValueError:
                drills[current] = 0.0
        elif toks[0] == "PAD" and current and len(toks) >= 3:
            pad = pads.get(toks[1])
            if pad is None:
                continue
            layer = toks[2]
            if layer in ("TOP", "BOTTOM", "ALL"):
                by_layer[current][layer] = pad
    return by_layer, drills


def _pad_for_pin(
    comp_layer: Layer,
    padstacks: dict[str, dict[str, _Pad]],
    padstack_name: str,
) -> tuple[_Pad | None, str | None, bool]:
    """Pick the right `_Pad` for a pin given the component's layer.

    Returns `(pad, layer_key, dual_face)`. `dual_face` is True only when
    the padstack declares pads for BOTH TOP and BOTTOM (genuine dual-face
    footprint like a PCI Express edge connector). When False, the
    padstack is single-face (typical SMD: one PAD line on TOP) and
    `layer_key` is just the face the pad happens to live on in the
    library — it must NOT be used to override the pin's effective layer,
    otherwise every BOTTOM-mounted SMD ends up rendered on TOP because
    its padstack only ships a TOP entry.
    """
    layers = padstacks.get(padstack_name)
    if not layers:
        return None, None, False
    dual_face = ("TOP" in layers) and ("BOTTOM" in layers)
    order = ("TOP", "ALL", "BOTTOM") if comp_layer == Layer.TOP else ("BOTTOM", "ALL", "TOP")
    for key in order:
        pad = layers.get(key)
        if pad is not None:
            return pad, key, dual_face
    return None, None, False


# ---------------------------------------------------------------------------
# $BOARD — outline + silkscreen artwork (LINE / ARC primitives)
# ---------------------------------------------------------------------------


_BOARD_LAYER = 28  # Three.js renderer convention — layer 28 is silkscreen /
# board outline. `render.py` chains layer-28 lines + arcs into closed
# polygons for the green substrate fill.


def _parse_board_outline(body: str) -> tuple[list[Trace], list[Arc]]:
    """Convert each `LINE x1 y1 x2 y2` to `Trace(layer=28)` and each
    `ARC x1 y1 x2 y2 cx cy` (start, end, center — counter-clockwise) to
    `Arc(center, radius, angle_start, angle_end, layer=28)`.
    """
    traces: list[Trace] = []
    arcs: list[Arc] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        toks = line.split()
        if toks[0] == "LINE" and len(toks) >= 5:
            try:
                x1, y1, x2, y2 = (float(t) for t in toks[1:5])
            except ValueError:
                continue
            traces.append(
                Trace(
                    a=Point(x=x1, y=y1),
                    b=Point(x=x2, y=y2),
                    layer=_BOARD_LAYER,
                )
            )
        elif toks[0] == "ARC" and len(toks) >= 7:
            try:
                x1, y1, x2, y2, cx, cy = (float(t) for t in toks[1:7])
            except ValueError:
                continue
            radius = math.hypot(x1 - cx, y1 - cy)
            angle_start = math.degrees(math.atan2(y1 - cy, x1 - cx))
            angle_end = math.degrees(math.atan2(y2 - cy, x2 - cx))
            arcs.append(
                Arc(
                    center=Point(x=cx, y=cy),
                    radius=radius,
                    angle_start=angle_start,
                    angle_end=angle_end,
                    layer=_BOARD_LAYER,
                )
            )
        # ARTWORK / other tokens ignored — we don't track per-artwork grouping
        # at this stage; the renderer reconstructs polygons geometrically.
    return traces, arcs


# ---------------------------------------------------------------------------
# $ROUTES — per-net VIA + (eventually) routed copper segments
# ---------------------------------------------------------------------------


def _parse_tracks(body: str) -> dict[int, float]:
    """Parse `$TRACKS` body — `TRACK <id> <width>` lines define widths
    referenced inside `$ROUTES` by their numeric id."""
    out: dict[int, float] = {}
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        toks = line.split()
        if toks[0] == "TRACK" and len(toks) >= 3:
            try:
                out[int(toks[1])] = float(toks[2])
            except ValueError:
                continue
    return out


# Routing-layer convention used by the Three.js viewer (KiCad-style):
#   1  = top copper, 16 = bottom copper, 28 = board outline / silkscreen.
_COPPER_TOP = 1
_COPPER_BOTTOM = 16


def _route_layer_id(layer_token: str) -> int:
    """Map a $ROUTES `LAYER <name>` directive to the renderer's layer id."""
    upper = layer_token.upper()
    if upper == "TOP":
        return _COPPER_TOP
    if upper == "BOTTOM":
        return _COPPER_BOTTOM
    # INNER / unknown — keep on TOP so the trace is still rendered.
    return _COPPER_TOP


def _parse_routes(
    body: str,
    padstacks: dict[str, dict[str, _Pad]],
    drills: dict[str, float],
    tracks: dict[int, float],
) -> tuple[list[Via], list[Trace], list[Arc]]:
    """Walk `$ROUTES`. Each `ROUTE <net>` block scopes a net; inside, a
    `TRACK <id>` sets the current track width, `LAYER <name>` sets the
    current copper layer, and `VIA`/`LINE`/`ARC` emit one geometry each:

    - `VIA <padstack> x y …` → `Via(pos, radius, net)` with radius taken
      from the padstack's ALL/TOP CIRCLE pad.
    - `LINE x1 y1 x2 y2` → `Trace(a, b, layer, width, net)` on the current
      copper layer with the current track width.
    - `ARC x1 y1 x2 y2 cx cy` → `Arc(center, radius, angle_start, angle_end,
      layer)` on the current copper layer.
    """
    vias: list[Via] = []
    traces: list[Trace] = []
    arcs: list[Arc] = []
    current_net: str | None = None
    current_width = 0.0
    current_layer = _COPPER_TOP
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        toks = line.split()
        if toks[0] == "ROUTE" and len(toks) >= 2:
            current_net = " ".join(toks[1:])
            current_width = 0.0
            current_layer = _COPPER_TOP
        elif toks[0] == "TRACK" and len(toks) >= 2:
            try:
                current_width = tracks.get(int(toks[1]), 0.0)
            except ValueError:
                current_width = 0.0
        elif toks[0] == "LAYER" and len(toks) >= 2:
            current_layer = _route_layer_id(toks[1])
        elif toks[0] == "VIA" and len(toks) >= 5:
            padstack_name = toks[1]
            try:
                x = float(toks[2])
                y = float(toks[3])
            except ValueError:
                continue
            # GenCAD `VIA <padstack> <x> <y> <layer_span> ...` — token 4
            # is the layer span (`ALL` for through, `TOP`/`BOTTOM` for
            # blind, `INNERn-INNERm` for buried). Default to ALL when
            # absent so legacy fixtures that omit it still load.
            layer_span = toks[4] if len(toks) >= 5 else "ALL"
            layers = padstacks.get(padstack_name) or {}
            pad = layers.get("ALL") or layers.get("TOP") or layers.get("BOTTOM")
            if pad is not None:
                radius = pad.width / 2.0
            else:
                radius = drills.get(padstack_name, 0.0) / 2.0
            vias.append(
                Via(
                    pos=Point(x=x, y=y),
                    radius=radius,
                    net=current_net,
                    padstack=padstack_name,
                    layer_span=layer_span,
                )
            )
        elif toks[0] == "LINE" and len(toks) >= 5:
            try:
                x1, y1, x2, y2 = (float(t) for t in toks[1:5])
            except ValueError:
                continue
            traces.append(
                Trace(
                    a=Point(x=x1, y=y1),
                    b=Point(x=x2, y=y2),
                    layer=current_layer,
                    width=current_width,
                    net=current_net,
                )
            )
        elif toks[0] == "ARC" and len(toks) >= 7:
            try:
                x1, y1, x2, y2, cx, cy = (float(t) for t in toks[1:7])
            except ValueError:
                continue
            radius = math.hypot(x1 - cx, y1 - cy)
            angle_start = math.degrees(math.atan2(y1 - cy, x1 - cx))
            angle_end = math.degrees(math.atan2(y2 - cy, x2 - cx))
            arcs.append(
                Arc(
                    center=Point(x=cx, y=cy),
                    radius=radius,
                    angle_start=angle_start,
                    angle_end=angle_end,
                    layer=current_layer,
                )
            )
    return vias, traces, arcs


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
# $MECH — drilled mechanical holes (mounting + tooling + fiducials)
# ---------------------------------------------------------------------------

# Heuristic: holes ≤ 100 mils diameter are fiducial / centre dots,
# anything larger is a structural mounting hole. The MSI v300 ships
# 4 corner mounting holes at 157 mils + a 75 mil centre fiducial,
# the threshold cleanly separates them.
_FIDUCIAL_MAX_DIAM_MILS = 100.0


def _parse_mech(body: str) -> list[MechanicalHole]:
    """Walk a `$MECH` body. Each `HOLE x y diameter` becomes a
    `MechanicalHole`. Holes ≤ 100 mils ø are tagged as fiducials so the
    renderer can colour them differently from the larger fixation holes."""
    out: list[MechanicalHole] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        toks = line.split()
        if toks[0] == "HOLE" and len(toks) >= 4:
            try:
                x = float(toks[1])
                y = float(toks[2])
                d = float(toks[3])
            except ValueError:
                continue
            out.append(
                MechanicalHole(
                    pos=Point(x=x, y=y),
                    diameter=d,
                    is_fiducial=d <= _FIDUCIAL_MAX_DIAM_MILS,
                )
            )
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
    pads = _parse_pads(sections.get("PADS", ""))
    padstacks, drills = _parse_padstacks(sections.get("PADSTACKS", ""), pads)
    tracks = _parse_tracks(sections.get("TRACKS", ""))
    board_traces, board_arcs = _parse_board_outline(sections.get("BOARD", ""))
    vias, copper_traces, copper_arcs = _parse_routes(
        sections.get("ROUTES", ""), padstacks, drills, tracks
    )
    mech_holes = _parse_mech(sections.get("MECH", ""))

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
            # Effective layer for this pin = SHAPE-stated layer XOR
            # component flip (mirror=YES or comp on BOTTOM). For most
            # parts every pin's stated layer is TOP and only the
            # comp-level flip matters; PCI Express edge connectors and
            # similar dual-face footprints split half their pads to
            # BOTTOM via the SHAPES `PIN ... BOTTOM` directive.
            comp_flipped = comp.mirror or comp.layer == Layer.BOTTOM
            pin_top = (sp.layer == "TOP") ^ comp_flipped
            effective_layer = Layer.TOP if pin_top else Layer.BOTTOM
            pad, found_key, dual_face = _pad_for_pin(effective_layer, padstacks, sp.padstack)
            # Only override the pin's layer when the padstack genuinely
            # carries both TOP and BOTTOM entries (PEX edge-connector
            # pattern). For single-face padstacks (typical SMD), the
            # pin must stay on `effective_layer` — the library only
            # ships one face but the pad physically sits on whichever
            # side the component is mounted.
            if dual_face and found_key == "TOP":
                final_layer = Layer.TOP
            elif dual_face and found_key == "BOTTOM":
                final_layer = Layer.BOTTOM
            else:
                final_layer = effective_layer
            pin_kwargs: dict = {}
            if pad is not None:
                pin_kwargs["pad_shape"] = pad.shape
                # Pad dimensions are stored in the SHAPE's local frame
                # (width along the shape's X axis, height along Y). The
                # component's rotation orients the whole footprint in
                # world space, so a pad that's 24×80 in shape coords
                # becomes 80×24 in world coords when the component is
                # rotated 90° or 270°. Mirror flips don't swap the
                # dimensions, only the position. Per-pin rotation
                # (toks[6] in the SHAPE PIN line) adds on top of the
                # component rotation. Round to nearest 90° to decide
                # whether to swap — pads are always orthogonal in
                # GenCAD, fractional rotations would land in the same
                # bin as their nearest right angle.
                total_rot = (comp.rotation_deg + sp.rotation_deg) % 180
                swap = abs(total_rot - 90) < 45
                if swap:
                    pin_kwargs["pad_size"] = (pad.height, pad.width)
                else:
                    pin_kwargs["pad_size"] = (pad.width, pad.height)
            pins.append(
                Pin(
                    part_refdes=comp.refdes,
                    index=pin_idx,
                    pos=Point(x=x, y=y),
                    net=net_name,
                    probe=None,
                    layer=final_layer,
                    **pin_kwargs,
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
                is_smd=shape.is_smd,
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

    # GenCAD `$BOARD` ARTWORK blocks ship dispersed silkscreen / fiducial
    # / logo segments, NOT a closed PCB contour. The renderer's edge-
    # chaining heuristic latches onto whatever tiny fragment closes first
    # and returns a 46×46 mm pseudo-outline for a real 272×113 mm GPU PCB,
    # which then drives the camera frustum and makes the whole board
    # appear "immense" because the camera fits to the fragment instead of
    # the actual extent.
    #
    # Best signal we DO have: `$MECH HOLE` lists the 4 corner mounting
    # holes — these mark the actual PCB corners (the screws can't sit
    # outside the board). When 3+ non-fiducial holes are present, fit the
    # outline to their bbox; otherwise fall back to the bbox of every
    # spatial entity (pins + vias + traces) like the .fz parser does.
    outline = _synthesize_outline(
        pins, vias, board_traces + copper_traces, mech_holes
    )

    return Board(
        board_id=board_id,
        file_hash=file_hash,
        source_format=source_format,
        outline=outline,
        parts=parts,
        pins=pins,
        nets=nets,
        nails=nails,
        vias=vias,
        traces=board_traces + copper_traces,
        arcs=board_arcs + copper_arcs,
        mech_holes=mech_holes,
    )


_OUTLINE_MARGIN_MILS = 100.0


def _synthesize_outline(
    pins: list[Pin],
    vias: list[Via],
    traces: list[Trace],
    mech_holes: list[MechanicalHole],
) -> list[Point]:
    """Return a closed-rectangle outline. Prefers `$MECH HOLE`-derived
    bbox (the 4 corner mounting holes give the real PCB extent at ~1
    drill-radius accuracy) when at least 3 non-fiducial holes are
    present, otherwise falls back to a bbox over every pin/via/trace
    endpoint with a small margin."""
    fixation = [h for h in mech_holes if not h.is_fiducial]
    if len(fixation) >= 3:
        # Pad by half the largest hole diameter so the outline sits
        # outside the screw heads themselves.
        radius = max(h.diameter for h in fixation) / 2.0
        xs = [h.pos.x for h in fixation]
        ys = [h.pos.y for h in fixation]
        minx = min(xs) - radius
        maxx = max(xs) + radius
        miny = min(ys) - radius
        maxy = max(ys) + radius
        return [
            Point(x=minx, y=miny),
            Point(x=maxx, y=miny),
            Point(x=maxx, y=maxy),
            Point(x=minx, y=maxy),
            Point(x=minx, y=miny),
        ]

    xs: list[float] = []
    ys: list[float] = []
    for p in pins:
        xs.append(p.pos.x)
        ys.append(p.pos.y)
    for v in vias:
        xs.append(v.pos.x)
        ys.append(v.pos.y)
    for t in traces:
        xs.append(t.a.x)
        ys.append(t.a.y)
        xs.append(t.b.x)
        ys.append(t.b.y)
    if not xs:
        return []
    minx = min(xs) - _OUTLINE_MARGIN_MILS
    maxx = max(xs) + _OUTLINE_MARGIN_MILS
    miny = min(ys) - _OUTLINE_MARGIN_MILS
    maxy = max(ys) + _OUTLINE_MARGIN_MILS
    return [
        Point(x=minx, y=miny),
        Point(x=maxx, y=miny),
        Point(x=maxx, y=maxy),
        Point(x=minx, y=maxy),
        Point(x=minx, y=miny),
    ]


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
