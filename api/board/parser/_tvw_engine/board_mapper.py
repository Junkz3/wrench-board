"""Map a parsed `TVWFile` into a wrench-board `Board` Pydantic model.

The `.tvw` format stores four independent record sets:
  1. Per-layer pin records (TOP, BOTTOM) — placement primitives
     (X, Y, net index, aperture). The TOP layer's pin array is the
     authoritative pad table for top-side parts; BOTTOM is the same
     for bottom-side parts. Inner-layer arrays carry only via probes.
  2. A global `network_names` list — net name table.
  3. A trailing component-record section — refdes + value + footprint
     + position + rotation + an explicit `PINS` array, one entry per
     pin with `(pad_index, pin_index_in_part, pin_name)`. The
     `pad_index` is the canonical link: `pad_index // 8` is a direct
     index into either TOP or BOTTOM `layer.pins[]`.
  4. F00B outline groups — per-package package outlines.

The pin record's `part_index` field is a **0-based net index** into
`network_names` (verified empirically: indices map to canonical power
rails with pad counts proportional to rail density).

Mapping conventions:
  * **Canonical pin-to-part mapping** (PASS A) uses `ComponentPin.pad_index`
    when components carry an explicit `pins` array (real fixtures).
    For each ComponentPin, the resolver indexes both TOP and BOTTOM
    `layer.pins[pad_index // 8]` and picks the layer whose pin-record
    position falls inside (or near) the component's bbox. This yields
    a deterministic, exact mapping with side resolved by which layer
    hosts the matching record — no `kind & 1` heuristic needed.
  * **Component side** is the majority vote of its canonical pins
    (TOP wins vs BOTTOM wins). Side from `kind & 1` is used as a
    last-resort fallback when no canonical mapping is available.
  * **Bbox-spatial fallback** (PASS B) runs for any pin records that
    PASS A did not claim: components without an explicit `pins` array
    (synthetic test fixtures, malformed sections), and pin records
    that don't appear in any component's `pins` list. The fallback
    uses the existing smallest-bbox-wins + per-component tolerance.
  * **Test pads**: pin records that pass through both passes without
    being claimed become `Board.test_pads` (probe targets / vias /
    isolated test points). The format is a probe-target database,
    so a typical `.tvw` has roughly as many of these stray pads as
    real SMD pads.
  * One `Net` per name in `network_names`. Each net's `pin_refs`
    contains the indices of every Pin attached to a real component
    that maps to it. Stray pins (now `TestPad` entries) carry their
    net inline so connectivity is preserved on click without
    polluting `Net.pin_refs` with non-Pin indices.
"""
from __future__ import annotations

from collections import defaultdict

from api.board.model import Arc, Board, Layer, Net, Part, Pin, Point, Segment, TestPad, Trace
from api.board.parser._ascii_boardview import GROUND_RE, POWER_RE

from .walker import TVWFile

# TVW coordinates are signed centi-mils. `Board` uses mils.
_COORD_DIVISOR = 100

# TVW pad bboxes encode the aperture envelope (solder mask / stencil
# opening), which is consistently larger than the actual copper pad.
# Rendering them at full size makes adjacent BGA balls overlap visibly
# (0.8 mm pitch = 31.5 mils, encoded pad bbox ~24 mils → 76 % of pitch
# → balls touch). The 0.65 shrink puts a 24-mil aperture at ~16 mils,
# giving clean separation at 0.8 mm pitch and matching the empirical
# copper-vs-mask ratio on graphics-card fixtures.
_PAD_RENDER_SHRINK = 0.65

# Test_pad max visual radius — the 40-mil ICT probe targets (the format
# main payload) drown out real SMD pads at full size. Cap
# to 4 mils so they stay identifiable / clickable but recede visually
# behind real components. The probe target's actual physical extent
# is no longer preserved here; use the boardview's hover panel for
# that.
_TEST_PAD_MAX_RADIUS_MILS = 4.0

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


# Per-component near-bbox tolerance is
#   max(bbox_extent / DIVISOR, FLOOR), capped at MAX.
#
# - DIVISOR=8 + MAX=50 mils gives a 423-mil BGA the full 50-mil reach
#   it needs to capture every edge ball (recovers 169.6/170 on the
#   graphics-card 170-pin BGAs).
# - FLOOR=15 mils ensures small placed components (60-mil 0402 caps
#   would otherwise get ~7 mils) keep enough reach to recover their
#   pads when the bbox is slightly tighter than the land pattern.
#   This trade is worth ~+440 caps now mapping with 2/2 vs the
#   no-floor variant, at the cost of ~+170 caps with 3+ pins —
#   1-pin slop is benign vs a missing component.
_NEAR_BBOX_TOLERANCE_CMILS_MAX = 50 * 100
_NEAR_BBOX_TOLERANCE_CMILS_FLOOR = 15 * 100
_NEAR_BBOX_TOLERANCE_DIVISOR = 8


def _build_bbox_index(components: list):
    """Build per-side coarse spatial grids for pin → component bbox lookup.

    Returns (top_grid, bottom_grid). Each grid cell is
    `_GRID_CELL_CMILS` square (centi-mils). A component is registered
    in every cell its bbox touches, on its own side only — so a
    BOTTOM via under a TOP cap won't be attributed to the TOP cap.

    Each bucket entry is `(x1, y1, x2, y2, refdes, area, tol_x, tol_y)`.
    The trailing area is precomputed so `_find_component` can pick
    the smallest-bbox match in O(1) per candidate — this resolves
    the overlap case where a generic shield/heatsink bbox covers
    smaller placed components and was previously winning by
    first-match order. The trailing tol_x/tol_y are the per-component
    near-bbox tolerances, scaled to component size so a BGA gets
    real reach without poaching pins from a 0402 cap two rows over.
    """
    top_grid: dict[tuple[int, int], list] = {}
    bot_grid: dict[tuple[int, int], list] = {}
    for c in components:
        target = top_grid if (c.kind & 1) == 1 else bot_grid
        x1, y1, x2, y2 = c.bbox_x1, c.bbox_y1, c.bbox_x2, c.bbox_y2
        area = max(1, (x2 - x1)) * max(1, (y2 - y1))
        tol_x = min(
            max(
                (x2 - x1) // _NEAR_BBOX_TOLERANCE_DIVISOR,
                _NEAR_BBOX_TOLERANCE_CMILS_FLOOR,
            ),
            _NEAR_BBOX_TOLERANCE_CMILS_MAX,
        )
        tol_y = min(
            max(
                (y2 - y1) // _NEAR_BBOX_TOLERANCE_DIVISOR,
                _NEAR_BBOX_TOLERANCE_CMILS_FLOOR,
            ),
            _NEAR_BBOX_TOLERANCE_CMILS_MAX,
        )
        # Index cells covered by the bbox PLUS the tolerance margin so
        # pass-2 lookup with extended bbox can find candidates whose
        # bbox edge lies in the next cell.
        gx0 = (x1 - tol_x) // _GRID_CELL_CMILS
        gx1 = (x2 + tol_x) // _GRID_CELL_CMILS
        gy0 = (y1 - tol_y) // _GRID_CELL_CMILS
        gy1 = (y2 + tol_y) // _GRID_CELL_CMILS
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                target.setdefault((gx, gy), []).append(
                    (x1, y1, x2, y2, c.refdes, area, tol_x, tol_y)
                )
    return top_grid, bot_grid


def _find_component(
    grid: dict, x_cmils: int, y_cmils: int,
) -> str | None:
    """Grid lookup with smallest-bbox-wins + per-component near-bbox tolerance.

    Two passes:
      1. **Strict**: any bbox containing (x, y). Among matches, pick the
         smallest-area bbox so a placed cap inside a shield outline
         beats the shield. Resolves the SP18-style "ghost component"
         pattern where overlapping bboxes used to drop pins on the
         carrier under first-match order.
      2. **Near-bbox**: if the strict pass finds nothing, expand each
         candidate bbox by its per-component tolerance (scaled to
         bbox extent, capped at `_NEAR_BBOX_TOLERANCE_CMILS_MAX`).
         BGAs gain real reach for their edge balls; 0402 caps gain
         only ~7 mils so they don't poach pins from neighbours.
    """
    cell_key = (x_cmils // _GRID_CELL_CMILS, y_cmils // _GRID_CELL_CMILS)
    bucket = grid.get(cell_key)
    if not bucket:
        return None

    best_refdes: str | None = None
    best_area = 0
    for x1, y1, x2, y2, refdes, area, _tx, _ty in bucket:
        if x1 <= x_cmils <= x2 and y1 <= y_cmils <= y2:
            if best_refdes is None or area < best_area:
                best_refdes = refdes
                best_area = area
    if best_refdes is not None:
        return best_refdes

    for x1, y1, x2, y2, refdes, area, tx, ty in bucket:
        if (x1 - tx) <= x_cmils <= (x2 + tx) and (y1 - ty) <= y_cmils <= (y2 + ty):
            if best_refdes is None or area < best_area:
                best_refdes = refdes
                best_area = area
    return best_refdes


def _resolve_canonical_pin(
    idx: int,
    component,
    top_arr: list,
    bot_arr: list,
) -> tuple[Layer, object] | None:
    """Pick TOP or BOTTOM `layer.pins[idx]` whose position best matches
    the component's bbox.

    The TVW production binary indexes pin records by `pad_index // 8`
    into a per-side pin array. A component's pins all live in the same
    array — TOP for top-side parts, BOTTOM for bottom-side. We don't
    know the side a priori (the `kind` u32's LSB gets it right ~85% of
    the time, not enough), so we test both arrays at the same `idx`
    and pick the one whose record falls inside (or near) the component.

    Returns (side, pin_record) or None when neither side plausibly
    contains a record at this index.
    """
    top_pr = top_arr[idx] if 0 <= idx < len(top_arr) else None
    bot_pr = bot_arr[idx] if 0 <= idx < len(bot_arr) else None

    if top_pr is None and bot_pr is None:
        return None

    cx, cy = component.cx, component.cy
    bx1, by1, bx2, by2 = (
        component.bbox_x1, component.bbox_y1,
        component.bbox_x2, component.bbox_y2,
    )

    def in_bbox(pr) -> bool:
        return pr is not None and bx1 <= pr.x <= bx2 and by1 <= pr.y <= by2

    top_in = in_bbox(top_pr)
    bot_in = in_bbox(bot_pr)

    def dist2(pr):
        if pr is None:
            return float("inf")
        return (pr.x - cx) ** 2 + (pr.y - cy) ** 2

    if top_in and not bot_in:
        return Layer.TOP, top_pr
    if bot_in and not top_in:
        return Layer.BOTTOM, bot_pr
    if top_in and bot_in:
        return (Layer.TOP, top_pr) if dist2(top_pr) <= dist2(bot_pr) else (Layer.BOTTOM, bot_pr)

    # Neither strictly inside — try with tolerance proportional to bbox size.
    # Connectors (PCIe, DVI, …) carry a tight body bbox while their pads
    # extend well beyond it. The tolerance scales with bbox so a 0402 cap
    # gets a tight reach (~7 mils of slack) and a PCIe slot gets metres.
    tol = max(
        (bx2 - bx1) // 4,
        (by2 - by1) // 4,
        50 * 100,  # 50 mils floor, in cmils
    )

    def near_bbox(pr) -> bool:
        return (
            pr is not None
            and (bx1 - tol) <= pr.x <= (bx2 + tol)
            and (by1 - tol) <= pr.y <= (by2 + tol)
        )

    top_near = near_bbox(top_pr)
    bot_near = near_bbox(bot_pr)

    if top_near and not bot_near:
        return Layer.TOP, top_pr
    if bot_near and not top_near:
        return Layer.BOTTOM, bot_pr
    if top_near and bot_near:
        return (Layer.TOP, top_pr) if dist2(top_pr) <= dist2(bot_pr) else (Layer.BOTTOM, bot_pr)

    # Neither in bbox nor near it. Connectors (PCIe / DVI / J-rails)
    # carry a body bbox much smaller than their pad fan-out; a few of
    # their pads sit several hundred mils outside even the tolerance
    # band. Fall back to nearest-distance: pick whichever side's record
    # is closer to the component centroid. This is the canonical-link
    # tiebreaker — both records exist, the format committed to one,
    # and on the connector cases we tested the right side wins by an
    # order of magnitude in `dist2`. If both records are missing, we
    # still return None.
    if top_pr is None and bot_pr is None:
        return None
    if top_pr is None:
        return Layer.BOTTOM, bot_pr
    if bot_pr is None:
        return Layer.TOP, top_pr
    return (Layer.TOP, top_pr) if dist2(top_pr) <= dist2(bot_pr) else (Layer.BOTTOM, bot_pr)


def _pad_metrics(pin_record, layer_obj) -> tuple[float, float, str]:
    """Resolve (width_mils, height_mils, pad_shape) from a pin record.

    Prefer the per-pin `pad_dx/dy` extension when available (carries the
    real pad rectangle); otherwise fall back to the layer's aperture
    table indexed by `pin_local_index`. The aperture's `type_` field
    selects the shape token: 0=Round, 1/2=Rectangle, 3=Oblong, 5=Custom.
    """
    if pin_record.has_pad_bbox:
        w_cmils = max(1, pin_record.pad_dx2 - pin_record.pad_dx1)
        h_cmils = max(1, pin_record.pad_dy2 - pin_record.pad_dy1)
        w_mils = max(1.0, w_cmils / _COORD_DIVISOR)
        h_mils = max(1.0, h_cmils / _COORD_DIVISOR)
        pad_shape = "circle" if w_cmils == h_cmils else "rect"
        return w_mils, h_mils, pad_shape

    apertures = layer_obj.apertures if layer_obj else []
    ap_by_idx = {ap.index: ap for ap in apertures}
    ap = ap_by_idx.get(pin_record.pin_local_index)
    if ap is None:
        return _DEFAULT_APERTURE_MILS, _DEFAULT_APERTURE_MILS, "circle"

    w_mils = max(1.0, ap.width / _COORD_DIVISOR)
    h_mils = max(1.0, ap.height / _COORD_DIVISOR)
    # Aperture type → shape token. We use the canonical names the
    # WebGL viewer's `kindMap` understands (rect / oval / circle /
    # square) — same vocabulary as the KiCad and XZZ parsers.
    #
    # The "Custom" type (5) is the trickier one: per file inspection
    # the named polygon table (file.polygons) carries 1:1 records
    # (TOP customs first, then BOTTOM, in dcode-table order) whose
    # bounding box exactly matches the aperture's width × height.
    # The vertices describe rounded-corner rectangles (PCIe pads,
    # capsule SMDs, …) — i.e. ovals. Rendering them as "oval" gets
    # the rounded-corner outline for free in the viewer; the small
    # subset of custom apertures that are actually chamfered
    # rectangles (4–8 vertex rings on big mounting pads) still
    # render as a rounded rect, which is visually close enough.
    if ap.type_ == 0:
        pad_shape = "circle"
    elif ap.type_ in (1, 2):
        pad_shape = "rect"
    elif ap.type_ == 3:
        pad_shape = "oval"
    elif ap.type_ == 5:
        pad_shape = "oval"
    else:
        pad_shape = "circle" if ap.width == ap.height else "rect"
    return w_mils, h_mils, pad_shape


def to_board(file: TVWFile, *, board_id: str, file_hash: str) -> Board:
    parts: list[Part] = []
    pins: list[Pin] = []
    test_pads: list[TestPad] = []
    pin_idxs_for_part: dict[str, list[int]] = defaultdict(list)
    pin_idxs_for_net: dict[str, list[int]] = defaultdict(list)
    pin_count_per_part: dict[str, int] = defaultdict(int)
    side_pin_count: dict[Layer, int] = defaultdict(int)

    # Identify the canonical TOP / BOTTOM pin arrays (used by PASS A).
    top_layer = None
    bot_layer = None
    for layer in file.layers:
        upper = layer.name.upper()
        if upper == "TOP" and top_layer is None:
            top_layer = layer
        elif (upper == "BOTTOM" or upper.startswith("BOT")) and bot_layer is None:
            bot_layer = layer
    top_arr = top_layer.pins if top_layer else []
    bot_arr = bot_layer.pins if bot_layer else []

    # PASS A — Canonical mapping via ComponentPin.pad_index.
    # claimed_top[i] / claimed_bot[i] = True when index `i` of the
    # corresponding layer's pin array has been bound to a component
    # via the pad_index canonical link.
    claimed_top: set[int] = set()
    claimed_bot: set[int] = set()
    component_canonical_side: dict[str, Layer] = {}
    # Buffer of (refdes, side, pin_record, ComponentPin, pad_idx_in_layer)
    # — we resolve all components first so component_canonical_side is
    # complete before we emit Pins (component side decides Pin.layer).
    canonical_buf: list[tuple[str, Layer, object, object, int]] = []

    for c in file.components:
        if not c.pins:
            continue
        votes_top = 0
        votes_bot = 0
        per_component: list[tuple[Layer, object, object, int]] = []
        for cp in c.pins:
            idx = cp.pad_index // 8
            chosen = _resolve_canonical_pin(idx, c, top_arr, bot_arr)
            if chosen is None:
                continue
            side, pr = chosen
            per_component.append((side, pr, cp, idx))
            if side is Layer.TOP:
                votes_top += 1
            else:
                votes_bot += 1
        if not per_component:
            continue
        majority_side = Layer.TOP if votes_top >= votes_bot else Layer.BOTTOM
        component_canonical_side[c.refdes] = majority_side
        for side, pr, cp, idx in per_component:
            canonical_buf.append((c.refdes, majority_side, pr, cp, idx))
            if side is Layer.TOP:
                claimed_top.add(idx)
            else:
                claimed_bot.add(idx)

    # Emit Pins from PASS A buffer.
    for refdes, side, pr, cp, _idx in canonical_buf:
        layer_obj = top_layer if side is Layer.TOP else bot_layer
        w_mils, h_mils, pad_shape = _pad_metrics(pr, layer_obj)
        net_name = _net_for(pr.part_index, file.net_names)
        side_pin_count[side] += 1
        pin_count_per_part[refdes] += 1
        pin_global = len(pins)
        pins.append(
            Pin(
                part_refdes=refdes,
                index=cp.pin_index,
                pos=Point(x=pr.x / _COORD_DIVISOR, y=pr.y / _COORD_DIVISOR),
                net=net_name,
                layer=side,
                pad_shape=pad_shape,
                pad_size=(
                    w_mils * _PAD_RENDER_SHRINK,
                    h_mils * _PAD_RENDER_SHRINK,
                ),
                name=cp.name or None,
            )
        )
        pin_idxs_for_part[refdes].append(pin_global)
        pin_idxs_for_net[net_name].append(pin_global)

    # PASS B — Bbox-spatial fallback for components without explicit
    # `pins` arrays AND for pin records that PASS A did not claim.
    # The bbox grid is built only from components that did NOT receive
    # any canonical mapping in PASS A — otherwise canonical pins would
    # be re-attached spatially with potentially worse accuracy.
    bbox_components = [c for c in file.components if c.refdes not in component_canonical_side]
    top_grid, bot_grid = _build_bbox_index(bbox_components)

    for layer in file.layers:
        side = _layer_to_side(layer.name)
        is_outer = _is_outer_layer(layer.name)
        ap_by_idx = {ap.index: ap for ap in layer.apertures}
        # Pick the per-side bbox grid: pins on TOP layer attach only
        # to TOP-side components (kind & 1 == 1); pins on BOTTOM
        # layer attach only to BOTTOM-side components. Inner-layer
        # pins (vias) skip both — they go straight to the test_pad
        # channel (they're plated through-holes, not SMD pads).
        if not is_outer:
            grid = None
        elif side is Layer.TOP:
            grid = top_grid
        else:
            grid = bot_grid
        # Skip pin records claimed by PASS A — they're already attached
        # to a real component via the canonical pad_index link.
        is_top_layer = layer is top_layer
        is_bot_layer = layer is bot_layer
        for pin_idx, pin_record in enumerate(layer.pins):
            if is_top_layer and pin_idx in claimed_top:
                continue
            if is_bot_layer and pin_idx in claimed_bot:
                continue
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
            # are vias, not SMD pads.) Tolerance is now per-component,
            # baked into the grid index — no extra args needed.
            if grid is not None:
                owning_refdes = _find_component(
                    grid, pin_record.x, pin_record.y,
                )
            else:
                owning_refdes = None

            if owning_refdes is None:
                # Stray — vias / ICT probe pads / isolated test points.
                # Route to the test_pad channel so they render as a
                # discreet secondary layer in the WebGL viewer instead
                # of a carrier Part full of fake "pins" that drown out
                # real SMD pads (the screenshot-reported issue on TVW
                # files: the BOTTOM aperture #22 polygon is a 40-mil
                # ICT probe template that ~half the BOT pins fall on).
                # Net + position are preserved so the user can still
                # click the test pad and see what it's connected to.
                # Visual radius is capped: the 40-mil probe targets
                # would otherwise drown out real SMD pads. We render
                # them as tiny markers instead.
                physical_radius = max(w_mils, h_mils) / 2.0
                radius = min(physical_radius, _TEST_PAD_MAX_RADIUS_MILS)
                test_pads.append(
                    TestPad(
                        pos=Point(x=x_mils, y=y_mils),
                        radius=radius,
                        layer=side,
                        net=net_name if net_name != _FLOATING_NET else None,
                    )
                )
                continue

            pin_count_per_part[owning_refdes] += 1
            pin_index_in_part = pin_count_per_part[owning_refdes]
            pin_global = len(pins)

            # Apply the aperture-vs-copper shrink (see _PAD_RENDER_SHRINK
            # rationale at top). Without this, BGA balls at 0.8 mm pitch
            # render as touching disks; the empirical copper-vs-mask
            # ratio on real boards calls for this shrink.
            pins.append(
                Pin(
                    part_refdes=owning_refdes,
                    index=pin_index_in_part,
                    pos=Point(x=x_mils, y=y_mils),
                    net=net_name,
                    layer=side,
                    pad_shape=pad_shape,
                    pad_size=(
                        w_mils * _PAD_RENDER_SHRINK,
                        h_mils * _PAD_RENDER_SHRINK,
                    ),
                )
            )
            pin_idxs_for_part[owning_refdes].append(pin_global)
            pin_idxs_for_net[net_name].append(pin_global)

    # Real component Parts — one per `ComponentRecord` that has pins on it.
    for c in file.components:
        if c.refdes not in pin_idxs_for_part:
            continue
        # Side: prefer the canonical-mapping majority vote (PASS A).
        # When PASS A produced no canonical pins for this component
        # (synthetic test fixtures with empty `c.pins`, or PASS B
        # spatial-fallback attribution), fall back to `kind & 1` —
        # the imperfect ~85% heuristic that's still better than
        # picking a side at random.
        side = component_canonical_side.get(
            c.refdes,
            Layer.TOP if (c.kind & 1) == 1 else Layer.BOTTOM,
        )
        # Silkscreen body outline: pull the per-footprint segment list
        # from the PACKAGE table when the component's `footprint` name
        # matches an entry. Each segment is centred on (0, 0); we
        # rotate by `c.rotation` and translate by `(c.cx, c.cy)` to
        # land on the board. This replaces the raw bbox-rectangle that
        # the WebGL viewer would otherwise draw — gives BGAs their
        # polygonal outline, edge connectors their key cut, passives
        # their actual body rectangle, etc.
        body_lines: list[Segment] = []
        pkg = file.packages.get(c.footprint) if c.footprint else None
        if pkg:
            for x1, y1, x2, y2 in pkg.segments:
                rx1, ry1 = _rotate_cmils(x1, y1, c.rotation)
                rx2, ry2 = _rotate_cmils(x2, y2, c.rotation)
                body_lines.append(
                    Segment(
                        a=Point(
                            x=(c.cx + rx1) / _COORD_DIVISOR,
                            y=(c.cy + ry1) / _COORD_DIVISOR,
                        ),
                        b=Point(
                            x=(c.cx + rx2) / _COORD_DIVISOR,
                            y=(c.cy + ry2) / _COORD_DIVISOR,
                        ),
                    )
                )
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
                body_lines=body_lines,
            )
        )

    # Stray pins (vias / ICT probe pads / isolated test points) are
    # routed to `test_pads` above instead of being aggregated under
    # `TVW_PADS_TOP` / `TVW_PADS_BOTTOM` carrier Parts. Carrier-as-Part
    # used to flood the boardview with thousands of large fake "pins"
    # — particularly visible on the bottom side where the dominant
    # aperture is a 40-mil polygon (the format's probe-pad template).
    # The test_pad channel lets the WebGL viewer render them as a
    # discreet secondary layer.

    # Build the Net list. Surface every name from network_names with its
    # pin membership; include the floating-net carrier only if any pin
    # actually lands on it.
    nets: list[Net] = []
    for name in file.net_names:
        if not name:
            continue
        nets.append(
            Net(
                name=name,
                pin_refs=pin_idxs_for_net.get(name, []),
                is_power=bool(POWER_RE.match(name)),
                is_ground=bool(GROUND_RE.match(name)),
            )
        )
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
    # sample fixture the global board edge candidate is a large TOP
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
        test_pads=test_pads,
        traces=traces,
        arcs=arcs,
    )
