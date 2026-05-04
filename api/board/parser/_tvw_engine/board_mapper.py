"""Map a parsed `TVWFile` into a wrench-board `Board` Pydantic model.

Tebo IctView stores three independent record sets:
  1. Per-layer pin records — placement primitives (X, Y, layer, net)
  2. A global `network_names` list — net name table
  3. A trailing component-record section — refdes + value + footprint
     + position + rotation, one record per real schematic component

The pin record's `part_index` field is a **0-based net index** into
`network_names` (verified empirically: indices map to canonical power
rails with pad counts proportional to rail density).

Mapping conventions:
  * One `Part` per `ComponentRecord` (real `C134`, `R200`, `U7`, …).
    Pins are spatially associated to the nearest component using each
    record's bounding box.
  * Pins outside any component bbox land on side carrier Parts
    (`TVW_PADS_TOP`, `TVW_PADS_BOTTOM`) so no pin is dropped.
  * One `Pin` per pin record, positioned at (X, Y) in mils,
    with `net = net_names[part_index]` (or `__floating__` when
    `part_index` lies outside `[0, len(net_names))`).
  * One `Net` per name in `network_names`. Each net's `pin_refs`
    contains the indices of every Pin that maps to it.
"""
from __future__ import annotations

from collections import defaultdict

from api.board.model import Arc, Board, Layer, Net, Part, Pin, Point, Trace

from .walker import TVWFile

# TVW coordinates are signed centi-mils. `Board` uses mils.
_COORD_DIVISOR = 100

# Default fallback aperture size when a pin's pin_local_index doesn't
# resolve to any aperture in the layer table.
_DEFAULT_APERTURE_MILS = 10.0
_MAX_DRAWING_COORD_CMILS = 2_000_000

# Carrier net for pin records whose `part_index` (= net_index) falls
# outside the parsed `network_names` range.
_FLOATING_NET = "__floating__"


def _layer_to_side(name: str) -> Layer:
    upper = name.upper()
    if "BOTTOM" in upper or upper.startswith("BOT"):
        return Layer.BOTTOM
    return Layer.TOP


def _is_outer_layer(name: str) -> bool:
    """True for the TOP / BOTTOM physical layers; False for inner signals."""
    upper = name.upper()
    return upper in ("TOP", "BOTTOM") or upper.startswith("BOT")


def _net_for(part_index: int, net_names: list[str]) -> str:
    """Resolve a pin record's `part_index` (0-based net index) to a net name."""
    if 0 <= part_index < len(net_names):
        return net_names[part_index] or _FLOATING_NET
    return _FLOATING_NET


_GRID_CELL_CMILS = 50_000  # 500 mils — fine enough for typical SMD spacing


def _f00b_extent(group) -> tuple[int, int] | None:
    """Bounding-box width/height (centi-mils) of a F00B group's kind=10 lines.

    Returns None for groups with no line primitives.
    """
    pts: list[tuple[int, int]] = []
    for prim in group.prims:
        if prim.kind == 10:
            pts.extend(prim.points)
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return max(xs) - min(xs), max(ys) - min(ys)


def _match_outline_to_component(
    cw: int, ch: int, f00b_extents: list,
) -> object | None:
    """Pick the F00B group whose extent best matches a component's bbox.

    The format does not encode an explicit component → outline link, so
    we match by package dimensions (centi-mils, both orientations
    considered). The closest extent within `2_500` cmils (= 25 mils,
    typical pad-vs-body slack on graphics-card 0402/0603 / SOT23
    packages) wins. None when no group is within tolerance — the
    component goes unoutlined rather than getting a wrong package.
    """
    best = None
    best_score = float("inf")
    for group, fw, fh in f00b_extents:
        s_direct = abs(fw - cw) + abs(fh - ch)
        s_rotated = abs(fw - ch) + abs(fh - cw)
        s = min(s_direct, s_rotated)
        if s < best_score:
            best_score = s
            best = group
    if best_score > 2_500:
        return None
    return best


def _surface_bbox(surface) -> tuple[int, int, int, int] | None:
    if not surface.vertices:
        return None
    xs = [x for x, _y in surface.vertices]
    ys = [y for _x, y in surface.vertices]
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_area(bbox: tuple[int, int, int, int]) -> int:
    x1, y1, x2, y2 = bbox
    return max(0, x2 - x1) * max(0, y2 - y1)


def _plausible_line(line) -> bool:
    return all(
        abs(v) <= _MAX_DRAWING_COORD_CMILS
        for v in (line.x1, line.y1, line.x2, line.y2)
    )


def _data_bbox(file: TVWFile) -> tuple[int, int, int, int] | None:
    pts: list[tuple[int, int]] = []
    for c in file.components:
        pts.append((c.bbox_x1, c.bbox_y1))
        pts.append((c.bbox_x2, c.bbox_y2))
    if not pts:
        for layer in file.layers:
            pts.extend((p.x, p.y) for p in layer.pins)
    if not pts:
        return None
    xs = [x for x, _y in pts]
    ys = [y for _x, y in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _select_surface_outline(file: TVWFile):
    """Pick the best real TVW surface outer ring as board-outline candidate."""
    data_bbox = _data_bbox(file)
    data_area = _bbox_area(data_bbox) if data_bbox is not None else 0
    best = None
    for layer in file.layers:
        for surface in layer.surfaces:
            if len(surface.vertices) < 4:
                continue
            bbox = _surface_bbox(surface)
            if bbox is None:
                continue
            width = bbox[2] - bbox[0]
            height = bbox[3] - bbox[1]
            # Board-scale gate: 500 mil minimum in both directions.
            if width < 50_000 or height < 50_000:
                continue
            area = _bbox_area(bbox)
            if data_area and area < data_area * 0.35:
                continue
            score = (area, len(surface.vertices), surface.void_count)
            if best is None or score > best[0]:
                best = (score, surface)
    return best[1] if best is not None else None


def _rotate_cmils(x: int, y: int, deg: int) -> tuple[int, int]:
    """Rotate (x, y) by 0/90/180/270 degrees CCW. Other angles → identity."""
    if deg == 90:
        return -y, x
    if deg == 180:
        return -x, -y
    if deg == 270:
        return y, -x
    return x, y


def _build_bbox_index(components: list):
    """Build per-side coarse spatial grids for pin → component bbox lookup.

    Returns (top_grid, bottom_grid). Each grid cell is
    `_GRID_CELL_CMILS` square (centi-mils). A component is registered
    in every cell its bbox touches, on its own side only — so a
    BOTTOM via under a TOP cap won't be attributed to the TOP cap.
    """
    top_grid: dict[tuple[int, int], list] = {}
    bot_grid: dict[tuple[int, int], list] = {}
    for c in components:
        target = top_grid if (c.kind & 1) == 1 else bot_grid
        x1, y1, x2, y2 = c.bbox_x1, c.bbox_y1, c.bbox_x2, c.bbox_y2
        gx0 = x1 // _GRID_CELL_CMILS
        gx1 = x2 // _GRID_CELL_CMILS
        gy0 = y1 // _GRID_CELL_CMILS
        gy1 = y2 // _GRID_CELL_CMILS
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                target.setdefault((gx, gy), []).append((x1, y1, x2, y2, c.refdes))
    return top_grid, bot_grid


def _find_component(grid: dict, x_cmils: int, y_cmils: int) -> str | None:
    """Grid lookup: O(1) cell access + linear scan within the cell."""
    cell_key = (x_cmils // _GRID_CELL_CMILS, y_cmils // _GRID_CELL_CMILS)
    bucket = grid.get(cell_key)
    if not bucket:
        return None
    for x1, y1, x2, y2, refdes in bucket:
        if x1 <= x_cmils <= x2 and y1 <= y_cmils <= y2:
            return refdes
    return None


def to_board(file: TVWFile, *, board_id: str, file_hash: str) -> Board:
    parts: list[Part] = []
    pins: list[Pin] = []
    pin_idxs_for_part: dict[str, list[int]] = defaultdict(list)
    pin_idxs_for_net: dict[str, list[int]] = defaultdict(list)
    pin_count_per_part: dict[str, int] = defaultdict(int)
    side_pin_count: dict[Layer, int] = defaultdict(int)

    side_refdes = {Layer.TOP: "TVW_PADS_TOP", Layer.BOTTOM: "TVW_PADS_BOTTOM"}
    top_grid, bot_grid = _build_bbox_index(file.components)

    for layer in file.layers:
        side = _layer_to_side(layer.name)
        is_outer = _is_outer_layer(layer.name)
        ap_by_idx = {ap.index: ap for ap in layer.apertures}
        carrier_refdes = side_refdes[side]
        # Pick the per-side bbox grid: pins on TOP layer attach only
        # to TOP-side components (kind & 1 == 1); pins on BOTTOM
        # layer attach only to BOTTOM-side components. Inner-layer
        # pins (vias) skip both — they fall on side carrier Parts.
        if not is_outer:
            grid = None
        elif side is Layer.TOP:
            grid = top_grid
        else:
            grid = bot_grid
        for pin_record in layer.pins:
            x_mils = pin_record.x / _COORD_DIVISOR
            y_mils = pin_record.y / _COORD_DIVISOR

            # Pad size: prefer the pin record's own pad bbox (from the
            # 16-byte sub_b extension) over the dcode aperture fallback.
            # The pad bbox describes this specific pad's shape, not the
            # generic aperture template.
            if pin_record.has_pad_bbox:
                w_cmils = max(1, pin_record.pad_dx2 - pin_record.pad_dx1)
                h_cmils = max(1, pin_record.pad_dy2 - pin_record.pad_dy1)
                w_mils = max(1.0, w_cmils / _COORD_DIVISOR)
                h_mils = max(1.0, h_cmils / _COORD_DIVISOR)
                pad_shape = "circle" if w_cmils == h_cmils else "rect"
            else:
                ap = ap_by_idx.get(pin_record.pin_local_index)
                if ap is None:
                    w_mils = h_mils = _DEFAULT_APERTURE_MILS
                    pad_shape = "circle"
                else:
                    w_mils = max(1.0, ap.width / _COORD_DIVISOR)
                    h_mils = max(1.0, ap.height / _COORD_DIVISOR)
                    pad_shape = "circle" if ap.width == ap.height else "rect"

            net_name = _net_for(pin_record.part_index, file.net_names)
            side_pin_count[side] += 1

            # Spatial association: which same-side component bbox
            # contains this pin? (None for inner layers — pins there
            # are vias, not SMD pads.)
            if grid is not None:
                owning_refdes = _find_component(
                    grid, pin_record.x, pin_record.y
                )
            else:
                owning_refdes = None
            if owning_refdes is None:
                owning_refdes = carrier_refdes

            pin_count_per_part[owning_refdes] += 1
            pin_index_in_part = pin_count_per_part[owning_refdes]
            pin_global = len(pins)

            pins.append(
                Pin(
                    part_refdes=owning_refdes,
                    index=pin_index_in_part,
                    pos=Point(x=x_mils, y=y_mils),
                    net=net_name,
                    layer=side,
                    pad_shape=pad_shape,
                    pad_size=(w_mils, h_mils),
                )
            )
            pin_idxs_for_part[owning_refdes].append(pin_global)
            pin_idxs_for_net[net_name].append(pin_global)

    # Real component Parts — one per `ComponentRecord` that has pins on it.
    for c in file.components:
        if c.refdes not in pin_idxs_for_part:
            continue
        # Side from the format itself: the LSB of the `kind` u32 is
        # the side flag (1 = TOP, 0 = BOTTOM). Confirmed empirically
        # on all 36 graphics-card fixtures we surveyed (~85% match
        # against an SMD-pad-spatial heuristic, which is itself
        # imperfect — the kind LSB is the canonical signal).
        side = Layer.TOP if (c.kind & 1) == 1 else Layer.BOTTOM
        parts.append(
            Part(
                refdes=c.refdes,
                layer=side,
                is_smd=True,
                bbox=(
                    Point(x=c.bbox_x1 / _COORD_DIVISOR, y=c.bbox_y1 / _COORD_DIVISOR),
                    Point(x=c.bbox_x2 / _COORD_DIVISOR, y=c.bbox_y2 / _COORD_DIVISOR),
                ),
                pin_refs=pin_idxs_for_part[c.refdes],
                value=c.value or None,
                footprint=c.footprint or None,
                rotation_deg=c.rotation if c.rotation in (0, 90, 180, 270) else None,
            )
        )

    # Side carrier Parts collect pins that didn't land inside any
    # component bbox (vias, isolated pads, fiducials).
    for side in (Layer.TOP, Layer.BOTTOM):
        carrier = side_refdes[side]
        if carrier not in pin_idxs_for_part:
            continue
        carrier_pin_idxs = pin_idxs_for_part[carrier]
        # Compute carrier bbox from its pins.
        xs = [pins[i].pos.x for i in carrier_pin_idxs]
        ys = [pins[i].pos.y for i in carrier_pin_idxs]
        if not xs:
            continue
        parts.append(
            Part(
                refdes=carrier,
                layer=side,
                is_smd=True,
                bbox=(Point(x=min(xs), y=min(ys)), Point(x=max(xs), y=max(ys))),
                pin_refs=carrier_pin_idxs,
                value=f"unmapped_pads={len(carrier_pin_idxs)}",
                footprint="TVW_LAYER",
            )
        )

    # Build the Net list. Surface every name from network_names with its
    # pin membership; include the floating-net carrier only if any pin
    # actually lands on it.
    nets: list[Net] = []
    for name in file.net_names:
        if not name:
            continue
        nets.append(Net(name=name, pin_refs=pin_idxs_for_net.get(name, [])))
    if pin_idxs_for_net.get(_FLOATING_NET):
        nets.append(Net(name=_FLOATING_NET, pin_refs=pin_idxs_for_net[_FLOATING_NET]))

    # Build Trace records from per-layer line records. Lines with both
    # endpoints at the origin are usually section terminators (zero
    # records emitted by the source tool) — drop them.
    traces: list[Trace] = []
    arcs: list[Arc] = []
    for layer in file.layers:
        side = _layer_to_side(layer.name)
        layer_idx = 0 if side is Layer.TOP else 1
        for line in layer.lines:
            if line.x1 == 0 and line.y1 == 0 and line.x2 == 0 and line.y2 == 0:
                continue
            if not _plausible_line(line):
                continue
            traces.append(
                Trace(
                    a=Point(x=line.x1 / _COORD_DIVISOR, y=line.y1 / _COORD_DIVISOR),
                    b=Point(x=line.x2 / _COORD_DIVISOR, y=line.y2 / _COORD_DIVISOR),
                    layer=layer_idx,
                    width=0.0,
                )
            )
        for arc in layer.arcs:
            if arc.radius <= 0:
                continue
            arcs.append(
                Arc(
                    center=Point(x=arc.cx / _COORD_DIVISOR, y=arc.cy / _COORD_DIVISOR),
                    radius=arc.radius / _COORD_DIVISOR,
                    angle_start=0.0,
                    angle_end=360.0,
                    layer=layer_idx,
                )
            )

    # Per-component package outlines from F00B groups. The format ships
    # ~166 F00B-anchored outline groups, each a unique package template
    # in local centi-mil coords centred on (0, 0). Components don't
    # carry an explicit F00B index, so we match by package dimensions
    # (closest extent wins, both orientations considered, ≤25-mil slack
    # for pad-vs-body discrepancy). Each match's kind=10 line primitives
    # get rotated by the component's `rotation` field and translated by
    # the component's centroid before emission. Without this step the
    # F00B data would never reach the viewer — F00B coords are local.
    f00b_extents = []
    for group in file.outlines:
        ext = _f00b_extent(group)
        if ext is not None:
            f00b_extents.append((group, ext[0], ext[1]))
    # Layer 28 is the WebGL viewer's "outline" channel — rendered in
    # silkscreen-white at higher opacity / thicker lines than copper
    # traces (`web/js/pcb_viewer.js`). Surfacing package outlines there
    # makes them visually distinct from copper / silkscreen lines instead
    # of blending in.
    _OUTLINE_LAYER = 28
    if f00b_extents:
        for c in file.components:
            if c.refdes not in pin_idxs_for_part:
                continue
            cw = c.bbox_x2 - c.bbox_x1
            ch = c.bbox_y2 - c.bbox_y1
            if cw <= 0 or ch <= 0:
                continue
            group = _match_outline_to_component(cw, ch, f00b_extents)
            if group is None:
                continue
            for prim in group.prims:
                if prim.kind != 10 or len(prim.points) != 2:
                    continue
                (lx1, ly1), (lx2, ly2) = prim.points
                # Drop degenerate (0,0)→(0,0) lines from corner-marker
                # F00B groups (`OUTLINE_TB_TPN`) — they'd render as
                # zero-length artifacts at the global origin.
                if lx1 == 0 and ly1 == 0 and lx2 == 0 and ly2 == 0:
                    continue
                rx1, ry1 = _rotate_cmils(lx1, ly1, c.rotation)
                rx2, ry2 = _rotate_cmils(lx2, ly2, c.rotation)
                gx1 = (c.cx + rx1) / _COORD_DIVISOR
                gy1 = (c.cy + ry1) / _COORD_DIVISOR
                gx2 = (c.cx + rx2) / _COORD_DIVISOR
                gy2 = (c.cy + ry2) / _COORD_DIVISOR
                traces.append(
                    Trace(
                        a=Point(x=gx1, y=gy1),
                        b=Point(x=gx2, y=gy2),
                        layer=_OUTLINE_LAYER,
                        width=0.0,
                    )
                )

    # Board outline: TVW's F00B groups are package outlines, but the
    # filled-surface section carries real outer-ring geometry. On the
    # GV-N970 fixture the global board edge candidate is a large TOP
    # surface outer ring; use that real geometry when it passes the
    # conservative coverage gates in `_select_surface_outline`.
    surface_outline = _select_surface_outline(file)
    outline = (
        [Point(x=x / _COORD_DIVISOR, y=y / _COORD_DIVISOR)
         for x, y in surface_outline.vertices]
        if surface_outline is not None
        else []
    )

    return Board(
        board_id=board_id,
        file_hash=file_hash,
        source_format="tvw",
        outline=outline,
        parts=parts,
        pins=pins,
        nets=nets,
        nails=[],
        traces=traces,
        arcs=arcs,
    )
