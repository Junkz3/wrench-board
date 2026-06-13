"""CPD "neutral file" `.cad` dialect parser.

A CPD3-era CAD toolchain
exports an ASCII "neutral file" that several Chinese boardview dumps
ship under the `.cad` extension. It is a `#`-commented, `###`-sectioned
text file produced by `mfg/neutral_file`. It shares NOTHING with the
generic `.cad` (BRDOUT/Test_Link) dialect, so it
gets its own parser, routed from `cad.py` by signature.

Decoded section grammar (verified on real CPD3 dumps):

  # file : <path>                     header comment carrying the CPD path
  # date : <...>
  ###Board Information
    BOARD <name> OFFSET x:.. y:.. ORIENTATION <deg>
    B_UNITS <Inches|Mm.|...>          board coordinate units
  ###Nets Information
    NET <netname>                     starts a net
    N_PROP (NET_TYPE,"POWER"|...)     optional net classification
    N_PIN  <refdes>-<pin> <x> <y> <padstack> <layer>
    N_VIA  <x> <y> <padstack> <a> <b>
  ###Component Information
    COMP <refdes> <partnum> <geom> <pkg> <x> <y> <side> <rot>
    C_PROP (...)                      optional component properties
    C_PIN <refdes>-<pin> <x> <y> <code> <side> <rot> <padstack> <net>
  ###Geometry Information               (footprint library — not needed)
  ###Hole Information                   HOLE <type> <x> <y> <dia>
  ###Pad Information                    PAD/P_SHAPE padstack library

The **Component Information** section is the richest, self-contained
source: every `COMP` carries the refdes + placement side, and every
`C_PIN` carries an absolute pin coordinate AND the net name inline.
We build Parts/Pins/Nets directly from it. We fall back to the Nets
section (`N_PIN`) when no `COMP` block is present (some exports ship
only connectivity). Net power/ground is taken from the explicit
`N_PROP (NET_TYPE,"POWER"|"GROUND")` when present, else inferred via
the shared `POWER_RE`/`GROUND_RE` heuristics.

Coordinates are board units (Inches or Mm.); we normalise to **mils**
(the Board model's convention) so the renderer and downstream pipeline
see one unit regardless of the source's `B_UNITS`. `side` 1 = top,
2 = bottom (mirrors the COMP placement layer). No code copied from any
external codebase.
"""

from __future__ import annotations

import math
import re

from api.board.model import (
    Board,
    Layer,
    MechanicalHole,
    Net,
    Part,
    Pin,
    Point,
    Segment,
)
from api.board.parser._ascii_boardview import GROUND_RE, POWER_RE
from api.board.parser.base import InvalidBoardFile

# Unit → mils conversion factor. The Board model stores mils
# (1 mil = 0.0254 mm). Inches × 1000 = mils; mm ÷ 0.0254 = mils.
_MM_PER_MIL = 0.0254


def looks_like_cpd_neutral(text: str) -> bool:
    """True if the ASCII payload is a CPD neutral file.

    Signature: the `# file :` / `# date :` comment header that the CPD
    `neutral_file` writer always emits, OR an explicit `###`-section
    header combined with a known CPD record keyword (`BOARD`, `COMP`,
    `NET`, `B_UNITS`). Checked over the first ~40 non-empty lines so a
    short / truncated dump still routes.
    """
    head = text[:4000]
    lower = head.lower()
    has_comment_header = "# file :" in lower or "mentor" in lower
    has_section = "###" in head
    has_keyword = any(
        re.search(rf"(?m)^{kw}\b", head)
        for kw in ("BOARD", "COMP ", "NET ", "B_UNITS")
    )
    if has_comment_header and (has_section or has_keyword):
        return True
    # Section header + a CPD record keyword is enough even without the
    # comment header (some redistributions strip the leading comments).
    return has_section and has_keyword


def _unit_scale(units: str | None) -> float:
    """Return the multiplier converting a board coordinate to mils.

    `B_UNITS Inches` → ×1000; `B_UNITS Mm.` (or `Mm`/`Millimeters`) →
    ÷0.0254. Unknown / missing units default to inches (the dominant
    CPD3 export unit) — coordinates stay self-consistent either way.
    """
    if not units:
        return 1000.0
    u = units.strip().lower().rstrip(".")
    if u.startswith("mm") or u.startswith("milli"):
        return 1.0 / _MM_PER_MIL
    # Inches (`Inches`, `in`) and anything unrecognised default to inches —
    # the dominant CPD3 export unit.
    return 1000.0


def _side_to_layer(side: str | int) -> Layer:
    """CPD placement side: 1 = top, 2 = bottom."""
    try:
        s = int(side)
    except (TypeError, ValueError):
        return Layer.TOP
    return Layer.BOTTOM if s == 2 else Layer.TOP


def _split_refdes_pin(token: str) -> tuple[str, str]:
    """Split a `R1036-1` / `U508-7` / `ADD1-A12` token into (refdes, pin).

    Splits on the LAST hyphen so refdes containing a hyphen (rare but
    possible) keeps its tail; the pin label is whatever follows. Falls
    back to (token, "1") when there is no hyphen.
    """
    idx = token.rfind("-")
    if idx <= 0:
        return token, "1"
    return token[:idx], token[idx + 1 :]


_NET_TYPE_RE = re.compile(r'NET_TYPE\s*,\s*"([^"]*)"', re.IGNORECASE)


def _classify_net(name: str, declared_type: str | None) -> tuple[bool, bool]:
    """Return (is_power, is_ground).

    Honours an explicit CPD `N_PROP (NET_TYPE,"POWER"|"GROUND")` first,
    then falls back to the shared regex heuristics on the net name. CPD
    net names carry a leading `/` (`/ANS_CHGVR_BG_MN`) and synthetic
    names look like `/N$8552`; we strip the `/` before the name-based
    heuristic so `+3.3V` style families still match.
    """
    bare = name.lstrip("/")
    if declared_type:
        dt = declared_type.strip().upper()
        if dt in ("POWER", "VCC", "PWR"):
            return True, False
        if dt in ("GROUND", "GND"):
            return False, True
    is_power = bool(POWER_RE.match(bare))
    is_ground = bool(GROUND_RE.match(bare))
    return is_power, is_ground


def _parse_float(tok: str) -> float | None:
    try:
        return float(tok)
    except (TypeError, ValueError):
        return None


def _floats(s: str) -> list[float]:
    out: list[float] = []
    for t in s.split():
        try:
            out.append(float(t))
        except ValueError:
            pass
    return out


def _parse_geometries(lines: list[str]) -> dict[str, list[tuple[float, float]]]:
    """Map each ``GEOM <name>`` block to its component placement outline.

    The Geometry section defines, per footprint geometry, a
    ``G_ATTR 'COMPONENT_PLACEMENT_OUTLINE' '' x1 y1 x2 y2 ...`` polygon in
    geometry-LOCAL coordinates (origin at the component centre, same units as
    the board). The coordinate list often wraps onto continuation lines (bare
    floats with no keyword), so we accumulate until the next keyword. Returns
    ``{geom_name: [(lx, ly), ...]}`` for geometries that carry an outline.
    """
    geoms: dict[str, list[tuple[float, float]]] = {}
    cur: str | None = None
    buf: list[float] | None = None
    buf_geom: str | None = None

    def flush() -> None:
        nonlocal buf, buf_geom
        if buf and buf_geom is not None and len(buf) >= 6:
            geoms[buf_geom] = [
                (buf[i], buf[i + 1]) for i in range(0, len(buf) - 1, 2)
            ]
        buf = None
        buf_geom = None

    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        if s.startswith("GEOM "):
            flush()
            cur = s.split(None, 1)[1].strip()
            continue
        if s.startswith("G_ATTR"):
            flush()
            if "'COMPONENT_PLACEMENT_OUTLINE'" in s and cur is not None:
                buf = _floats(s.split("'")[-1])
                buf_geom = cur
            continue
        if s.startswith(("G_PIN", "#", "GEOM")):
            flush()
            continue
        # A bare-float line continues the current outline; anything else ends it.
        if buf is not None:
            cont = _floats(s)
            if cont:
                buf.extend(cont)
            else:
                flush()
    flush()
    return geoms


def _placed_body_lines(
    outline: list[tuple[float, float]],
    cx: float,
    cy: float,
    rot_deg: float | None,
    side: str,
    scale: float,
) -> list[Segment]:
    """Place a geometry-local outline into board coordinates as silkscreen.

    Applies the component's rotation, bottom-side mirror (X flip), translation
    to its placement centre, then the board unit scale (→ mils). Consecutive
    points are joined into closed-polygon segments.
    """
    if len(outline) < 3:
        return []
    rad = math.radians(rot_deg or 0.0)
    cos, sin = math.cos(rad), math.sin(rad)
    mirror = -1.0 if str(side).strip() == "2" else 1.0
    world: list[Point] = []
    for lx, ly in outline:
        mx = lx * mirror
        rx = mx * cos - ly * sin
        ry = mx * sin + ly * cos
        world.append(Point(x=(cx + rx) * scale, y=(cy + ry) * scale))
    n = len(world)
    return [Segment(a=world[i], b=world[(i + 1) % n]) for i in range(n)]


def parse_cpd_neutral(
    text: str,
    *,
    file_hash: str,
    board_id: str,
    source_format: str = "cad",
) -> Board:
    """Parse a CPD neutral file into a Board.

    Strategy: read `B_UNITS` for the coordinate scale, then build
    parts+pins from the **Component Information** section (`COMP` /
    `C_PIN`), the densest and most self-contained block. Net
    classification (power/ground) is enriched from the **Nets
    Information** section's `N_PROP (NET_TYPE,...)` declarations.
    Mechanical holes come from the **Hole Information** section.

    Robust to short / odd lines: any line that doesn't have enough
    tokens to be meaningful is skipped rather than raising, because
    these files are large machine exports and a single malformed row
    must not lose the whole board.
    """
    lines = text.splitlines()

    # --- Pass 1: units + net-type declarations (and N_PIN fallback) ---
    units: str | None = None
    # net name -> declared NET_TYPE (from N_PROP), built while walking nets.
    net_declared_type: dict[str, str] = {}
    # Fallback connectivity from the Nets section: net -> list of (refdes,pin,x,y,side)
    net_pins_fallback: dict[str, list[tuple[str, str, float, float, str]]] = {}
    current_net: str | None = None

    holes: list[MechanicalHole] = []

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Cheap keyword dispatch on the first token.
        if line.startswith("B_UNITS"):
            parts = line.split(None, 1)
            if len(parts) == 2:
                units = parts[1].strip()
            continue
        if line.startswith("NET "):
            current_net = line[4:].strip()
            net_pins_fallback.setdefault(current_net, [])
            continue
        if line.startswith("N_PROP") and current_net is not None:
            m = _NET_TYPE_RE.search(line)
            if m:
                net_declared_type[current_net] = m.group(1)
            continue
        if line.startswith("N_PIN") and current_net is not None:
            # N_PIN <refdes>-<pin> <x> <y> <padstack> <layer>
            toks = line.split()
            if len(toks) >= 4:
                refdes, pin = _split_refdes_pin(toks[1])
                x = _parse_float(toks[2])
                y = _parse_float(toks[3])
                side = toks[5] if len(toks) >= 6 else "1"
                if x is not None and y is not None:
                    net_pins_fallback[current_net].append(
                        (refdes, pin, x, y, side)
                    )
            continue
        if line.startswith("HOLE"):
            # HOLE <type> <x> <y> <dia>
            toks = line.split()
            if len(toks) >= 5:
                x = _parse_float(toks[2])
                y = _parse_float(toks[3])
                dia = _parse_float(toks[4])
                if x is not None and y is not None and dia is not None:
                    holes.append((x, y, dia))  # raw; scaled after we know units
            continue

    scale = _unit_scale(units)
    geom_outlines = _parse_geometries(lines)

    # --- Pass 2: components (primary source) ---
    # part refdes -> (layer, rotation)
    comp_meta: dict[str, tuple[Layer, float | None]] = {}
    # part refdes -> (geom_name, cx, cy, side, rot) for silkscreen placement
    comp_place: dict[str, tuple[str, float, float, str, float | None]] = {}
    # ordered refdes list (preserve file order for deterministic output)
    comp_order: list[str] = []
    # pins collected from C_PIN, grouped per part
    pin_rows: list[tuple[str, str, float, float, str, str | None, str | None]] = []
    # (refdes, pinname, x, y, side, padstack, net)

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("COMP "):
            # COMP <refdes> <partnum> <device> <geom> <x> <y> <side> <rot>
            # The geometry name (matching the GEOM blocks) + placement centre
            # are the trailing fields: ... <geom> <x> <y> <side> <rot>.
            toks = line.split()
            if len(toks) >= 8:
                refdes = toks[1]
                side = toks[-2]
                rot = _parse_float(toks[-1])
                if refdes not in comp_meta:
                    comp_meta[refdes] = (_side_to_layer(side), rot)
                    comp_order.append(refdes)
                    geom = toks[-5]
                    cx = _parse_float(toks[-4])
                    cy = _parse_float(toks[-3])
                    if cx is not None and cy is not None:
                        comp_place[refdes] = (geom, cx, cy, side, rot)
            continue
        if line.startswith("C_PIN"):
            # C_PIN <refdes>-<pin> <x> <y> <code> <side> <rot> <padstack> <net>
            toks = line.split()
            if len(toks) >= 4:
                refdes, pin = _split_refdes_pin(toks[1])
                x = _parse_float(toks[2])
                y = _parse_float(toks[3])
                if x is None or y is None:
                    continue
                side = toks[5] if len(toks) >= 6 else "1"
                padstack = toks[7] if len(toks) >= 8 else None
                net = toks[8] if len(toks) >= 9 else None
                pin_rows.append((refdes, pin, x, y, side, padstack, net))
            continue

    # --- Build Parts + Pins from the component section ---
    parts: list[Part] = []
    pins: list[Pin] = []

    if pin_rows:
        # Group pins by part, preserving encounter order.
        pins_by_part: dict[str, list[tuple]] = {}
        order: list[str] = []
        for row in pin_rows:
            refdes = row[0]
            if refdes not in pins_by_part:
                pins_by_part[refdes] = []
                order.append(refdes)
            pins_by_part[refdes].append(row)

        # Parts that appear only in COMP (no pins) still get emitted.
        for refdes in comp_order:
            if refdes not in pins_by_part:
                pins_by_part.setdefault(refdes, [])
                order.append(refdes)

        for refdes in order:
            meta = comp_meta.get(refdes)
            part_layer = meta[0] if meta else Layer.TOP
            rotation = meta[1] if meta else None
            rows = pins_by_part[refdes]
            part_pin_refs: list[int] = []
            xs: list[float] = []
            ys: list[float] = []
            for i, (_, pinname, x, y, side, _padstack, net) in enumerate(rows, start=1):
                sx = x * scale
                sy = y * scale
                xs.append(sx)
                ys.append(sy)
                pin = Pin(
                    part_refdes=refdes,
                    index=i,
                    pos=Point(x=sx, y=sy),
                    net=(net or None),
                    # Prefer the part's placement side for the pin layer (SMD
                    # pins live on the part's side); fall back to the pin's own
                    # side when there's no owning component record.
                    layer=part_layer if meta else _side_to_layer(side),
                    pad_shape=None,
                    name=pinname,
                )
                part_pin_refs.append(len(pins))
                pins.append(pin)
            if xs and ys:
                bbox = (
                    Point(x=min(xs), y=min(ys)),
                    Point(x=max(xs), y=max(ys)),
                )
            else:
                bbox = (Point(x=0.0, y=0.0), Point(x=0.0, y=0.0))
            place = comp_place.get(refdes)
            body_lines = (
                _placed_body_lines(
                    geom_outlines.get(place[0], []),
                    place[1],
                    place[2],
                    place[4],
                    place[3],
                    scale,
                )
                if place
                else []
            )
            parts.append(
                Part(
                    refdes=refdes,
                    layer=part_layer,
                    is_smd=True,  # CPD neutral files are SMT placement exports
                    bbox=bbox,
                    pin_refs=part_pin_refs,
                    rotation_deg=rotation,
                    body_lines=body_lines,
                )
            )
    elif net_pins_fallback:
        # No component section — reconstruct parts/pins from the Nets
        # section connectivity (every N_PIN gives a refdes+pin+coord+net).
        pins_by_part: dict[str, list[tuple[str, str, float, float, str]]] = {}
        order = []
        for net_name, entries in net_pins_fallback.items():
            for refdes, pinname, x, y, side in entries:
                if refdes not in pins_by_part:
                    pins_by_part[refdes] = []
                    order.append(refdes)
                pins_by_part[refdes].append((pinname, x, y, side, net_name))
        for refdes in order:
            rows = pins_by_part[refdes]
            part_pin_refs = []
            xs, ys = [], []
            # Side comes from the first pin (parts sit on one side).
            part_layer = _side_to_layer(rows[0][3]) if rows else Layer.TOP
            for i, (pinname, x, y, _side, net_name) in enumerate(rows, start=1):
                sx, sy = x * scale, y * scale
                xs.append(sx)
                ys.append(sy)
                pins.append(
                    Pin(
                        part_refdes=refdes,
                        index=i,
                        pos=Point(x=sx, y=sy),
                        net=net_name or None,
                        layer=part_layer,
                        name=pinname,
                    )
                )
                part_pin_refs.append(len(pins) - 1)
            bbox = (
                (Point(x=min(xs), y=min(ys)), Point(x=max(xs), y=max(ys)))
                if xs
                else (Point(x=0.0, y=0.0), Point(x=0.0, y=0.0))
            )
            parts.append(
                Part(
                    refdes=refdes,
                    layer=part_layer,
                    is_smd=True,
                    bbox=bbox,
                    pin_refs=part_pin_refs,
                )
            )
    else:
        raise InvalidBoardFile(
            f"{source_format}: CPD neutral file carried no "
            "COMP/C_PIN component block and no N_PIN connectivity"
        )

    # --- Build Nets (group pins by net, classify) ---
    by_net: dict[str, list[int]] = {}
    for i, pin in enumerate(pins):
        if pin.net is None:
            continue
        by_net.setdefault(pin.net, []).append(i)

    nets: list[Net] = []
    for name, refs in sorted(by_net.items()):
        is_power, is_ground = _classify_net(name, net_declared_type.get(name))
        nets.append(
            Net(name=name, pin_refs=refs, is_power=is_power, is_ground=is_ground)
        )

    # --- Mechanical holes (scaled) ---
    mech_holes: list[MechanicalHole] = []
    for (hx, hy, dia) in holes:
        d = dia * scale
        mech_holes.append(
            MechanicalHole(
                pos=Point(x=hx * scale, y=hy * scale),
                diameter=d,
                is_fiducial=d <= 100.0,
            )
        )

    if not parts and not pins:
        raise InvalidBoardFile(
            f"{source_format}: CPD neutral file produced no parts or pins"
        )

    return Board(
        board_id=board_id,
        file_hash=file_hash,
        source_format=source_format,
        outline=[],
        parts=parts,
        pins=pins,
        nets=nets,
        nails=[],
        mech_holes=mech_holes,
    )
