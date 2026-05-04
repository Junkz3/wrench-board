"""Board → Three.js render payload.

Pure function `to_render_payload(board)` converts the Pydantic `Board`
into the JSON shape consumed by `web/js/pcb_viewer.js`. The viewer is
calibrated for **millimetres**, so every coordinate is converted from
the parser's native mils on the way out.

Component colours follow the refdes-prefix family heuristic; pin
shapes / sizes / rotations come straight from the parser when available.
"""

from __future__ import annotations

import math
from typing import Any

from api.board.model import Board, Layer, Net, Part, Pin, Point

MIL_TO_MM = 0.0254  # 1 mil = 0.0254 mm — viewer expects mm world units


def _mm(v: float) -> float:
    return v * MIL_TO_MM


# refdes prefix → (component_type, hex colour)
# Order matters: longer prefixes win first ("FL" before "F" if we add it).
# Order matters: prefix matching uses startswith, so longer / more
# specific prefixes MUST come before shorter ones (TEST_PAD before TP
# before T, etc.) — otherwise 'TEST_PAD_xxx' would match 'T' and
# classify as TRANSISTOR.
_COMPONENT_TYPES: tuple[tuple[str, str, str], ...] = (
    ("TEST_PAD", "TEST_POINT", "#fbbf24"),
    ("TEST", "TEST_POINT", "#fbbf24"),
    ("FL", "FILTER", "#6b7280"),
    ("XW", "TEST_POINT", "#fbbf24"),
    ("IC", "IC", "#2563eb"),
    ("CN", "CONNECTOR", "#ea580c"),
    ("SH", "SHIELD", "#475569"),
    ("TP", "TEST_POINT", "#fbbf24"),
    ("U", "IC", "#2563eb"),
    ("C", "CAP", "#1d4ed8"),
    ("R", "RES", "#1e3a5f"),
    ("L", "IND", "#7c3aed"),
    ("D", "DIODE", "#dc2626"),
    ("Q", "TRANSISTOR", "#0891b2"),
    ("T", "TRANSISTOR", "#0891b2"),
    ("J", "CONNECTOR", "#ea580c"),
    ("Y", "CRYSTAL", "#db2777"),
    ("X", "CRYSTAL", "#db2777"),
)
_OTHER = ("OTHER", "#64748b")

_GND_NAMES = {"GND", "VSS", "AGND", "DGND", "PGND", "GROUND"}


def _classify(refdes: str, category: str | None = None) -> tuple[str, str]:
    # Format-level category wins over refdes-prefix heuristics. XZZ
    # tags genuine test/probe pads with category "TP" (irrespective of
    # the refdes prefix), so a part labelled "TP-P5" or even an
    # untagged single-pin probe lands as TEST_POINT regardless of
    # whether its refdes happens to start with TP / XW / TEST*.
    if category and category.upper() == "TP":
        return ("TEST_POINT", "#fbbf24")
    upper = refdes.upper()
    for prefix, ctype, colour in _COMPONENT_TYPES:
        if upper.startswith(prefix):
            return ctype, colour
    return _OTHER


def _layer_name(layer: Layer) -> str:
    if layer == Layer.TOP:
        return "top"
    if layer == Layer.BOTTOM:
        return "bottom"
    return "both"


def _is_ground(net: Net | None, name: str | None) -> bool:
    if net is not None and net.is_ground:
        return True
    if name and name.upper() in _GND_NAMES:
        return True
    return False


_MIN_PART_DIM_MM = 0.15  # 0402 long edge ≈ 1 mm; rescue degenerate bboxes
                          # at one decade smaller.


def _convert_part(
    part: Part, pins_for_part: list[Pin], net_index: dict[str, Net]
) -> dict[str, Any]:
    ctype, colour = _classify(part.refdes, part.category)
    bbox_min, bbox_max = part.bbox

    # Extend the part AABB so it encloses every pin's *pad* (centre +
    # pad_size / 2), not just the pin centres. Without this, a 2-pin
    # chip passive (R0402, C0603) where both pins sit on the same Y
    # collapses to a zero-height line — the viewer then renders a
    # ridiculously thin body with pads sticking out either side.
    if pins_for_part:
        xs, ys = [], []
        for pin in pins_for_part:
            half_w = pin.pad_size[0] / 2 if pin.pad_size else 0.0
            half_h = pin.pad_size[1] / 2 if pin.pad_size else 0.0
            xs.extend([pin.pos.x - half_w, pin.pos.x + half_w])
            ys.extend([pin.pos.y - half_h, pin.pos.y + half_h])
        if xs and ys:
            ext_min_x, ext_max_x = min(xs), max(xs)
            ext_min_y, ext_max_y = min(ys), max(ys)
            # Take the larger of the parser's bbox (which may include
            # silkscreen for XZZ) and the pad-extended bbox.
            bbox_min = Point(
                x=min(bbox_min.x, ext_min_x),
                y=min(bbox_min.y, ext_min_y),
            )
            bbox_max = Point(
                x=max(bbox_max.x, ext_max_x),
                y=max(bbox_max.y, ext_max_y),
            )

    aabb_w = bbox_max.x - bbox_min.x
    aabb_h = bbox_max.y - bbox_min.y
    aabb_cx = (bbox_min.x + bbox_max.x) / 2
    aabb_cy = (bbox_min.y + bbox_max.y) / 2

    # When the part has a rotation AND silkscreen body_lines, derotate
    # the body_lines around the AABB centre to recover the package's
    # LOCAL (pre-rotation) box. The viewer rotates the generic shape by
    # `rotation` around the group origin, so we pass it the local box —
    # this prevents 90/270 rotated rects from getting visually squashed
    # while still letting non-standard angles (29.6°, 29.3°, …) render
    # correctly oriented.
    rotation_deg = part.rotation_deg or 0.0
    # `emit_rotation` tracks whether the viewer should re-apply rotation_deg.
    # We only emit it when we successfully recovered the LOCAL pre-rotation
    # box from `body_lines` — otherwise the rectangle we hand the viewer is
    # the post-rotation AABB, which is already at its visual orientation
    # and would double-rotate if the viewer applied rotation_deg again.
    # This matters for KiCad/BRD/BVR/ASC/etc. — every parser that doesn't
    # populate `body_lines`. Only XZZ does, so only XZZ keeps rotation.
    emit_rotation = False
    if rotation_deg and part.body_lines:
        theta = math.radians(-rotation_deg)
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)
        local_xs: list[float] = []
        local_ys: list[float] = []
        for seg in part.body_lines:
            for p in (seg.a, seg.b):
                dx = p.x - aabb_cx
                dy = p.y - aabb_cy
                local_xs.append(cos_t * dx - sin_t * dy + aabb_cx)
                local_ys.append(sin_t * dx + cos_t * dy + aabb_cy)
        if local_xs:
            local_w = max(local_xs) - min(local_xs)
            local_h = max(local_ys) - min(local_ys)
            # Position so the group centre lands on the AABB centre when
            # the viewer does `group.position = (comp.x + w/2, comp.y + h/2)`.
            comp_x = aabb_cx - local_w / 2
            comp_y = aabb_cy - local_h / 2
            emit_rotation = True
        else:
            comp_x, comp_y = bbox_min.x, bbox_min.y
            local_w, local_h = aabb_w, aabb_h
    else:
        comp_x, comp_y = bbox_min.x, bbox_min.y
        local_w, local_h = aabb_w, aabb_h

    width_mm = _mm(local_w)
    height_mm = _mm(local_h)

    # Bbox produced by parsers without body_lines (BRD / KiCad) collapses
    # to a line for 2-pin SMDs. Rescue those by widening to a sensible
    # minimum, but parsers that DID surface a real silkscreen (XZZ)
    # carry authoritative dims — even sub-mm probe pads like XW8230
    # (0.079 x 0.039 mm body) — so skip the floor when body_lines is
    # populated. Inflating to 0.15 mm there made the actual pads look
    # tiny inside an over-large package.
    if not part.body_lines:
        if width_mm < _MIN_PART_DIM_MM:
            width_mm = _MIN_PART_DIM_MM
        if height_mm < _MIN_PART_DIM_MM:
            height_mm = _MIN_PART_DIM_MM

    ref_net = ""
    for pin in pins_for_part:
        if pin.net:
            ref_net = pin.net
            break
    return {
        "id": part.refdes,
        "type": ctype,
        "value": part.value or part.refdes,
        "x": _mm(comp_x),
        "y": _mm(comp_y),
        "width": width_mm,
        "height": height_mm,
        "color": colour,
        "net": ref_net,
        "layer": _layer_name(part.layer),
        "pin_count": len(part.pin_refs),
        "rotation": rotation_deg if emit_rotation else 0.0,
        # Source-format-specific hints from the parser. `footprint` is
        # the human-readable footprint name (e.g. "TP-P5", "CAP-0201",
        # "BGA-…"); `category` is the parser's role tag ("TP" for genuine
        # test/probe pads on XZZ). Both are optional — formats that
        # don't surface them just omit the keys.
        "footprint": part.footprint,
        "category": part.category,
        "body_lines": [
            {"x1": _mm(seg.a.x), "y1": _mm(seg.a.y),
             "x2": _mm(seg.b.x), "y2": _mm(seg.b.y)}
            for seg in part.body_lines
        ],
        # DFM-alternate / DNP tracking — see `Part.is_dnp`. The viewer
        # skips drawing parts where `is_dnp=true` (clean canvas) and
        # surfaces a small badge + alternates list on parts where
        # `dnp_alternates` is non-empty.
        "is_dnp": part.is_dnp,
        "dnp_alternates": list(part.dnp_alternates),
    }


_DEFAULT_PIN_DIA_MM = 0.2  # ≈ 8 mils — small visible dot when parser is silent


def _convert_pin(pin: Pin, pin_index: int, net_index: dict[str, Net],
                 part_type: str | None = None) -> dict[str, Any]:
    if pin.pad_size:
        width_mm = _mm(pin.pad_size[0])
        height_mm = _mm(pin.pad_size[1])
        shape = pin.pad_shape or "rect"
    else:
        width_mm = height_mm = _DEFAULT_PIN_DIA_MM
        shape = "circle"
    net = net_index.get(pin.net) if pin.net else None
    return {
        "id": f"{pin.part_refdes}_P{pin.index}",
        "component": pin.part_refdes,
        "component_type": part_type or "OTHER",
        "x": _mm(pin.pos.x),
        "y": _mm(pin.pos.y),
        # Floor at 0.01 mm (10 µm) — small enough to preserve actual
        # micro-pads on probe pads like XW8230 (raw 0.018-0.035 mm
        # after the xzz pad-shrink) without inflating them past the
        # silkscreen body bbox.
        "width": max(width_mm, 0.01),
        "height": max(height_mm, 0.01),
        "shape": shape,
        "rotation": pin.pad_rotation_deg or 0.0,
        "net": pin.net or "NC",
        "layer": _layer_name(pin.layer),
        "is_gnd": _is_ground(net, pin.net),
        "pin_index": pin_index,
    }


def _arc_to_points(arc, reverse: bool = False) -> list[dict[str, float]]:
    """Sample an arc into a polyline.

    Convention: XZZ stores `angle_start` / `angle_end` such that the arc
    is traversed CCW from start to end. When the raw `end - start` is
    negative (start > end, e.g. 180°→0° wrapping past 0°/360°), we add
    360 so the sweep stays positive — this catches the slot/cutout case
    where the arc goes the LONG way around (180° via 270° down). A
    short-path heuristic (`if span > 180: -=360`) inverts those slot
    arcs visually, so we keep the rule that leaves all other arc
    orientations untouched.
    """
    cx = _mm(arc.center.x)
    cy = _mm(arc.center.y)
    radius = _mm(arc.radius)
    start_angle = arc.angle_start
    end_angle = arc.angle_end
    if reverse:
        start_angle, end_angle = end_angle, start_angle

    span = end_angle - start_angle
    if span < 0:
        span += 360.0

    num_segments = max(16, int(abs(span) / 2))
    out: list[dict[str, float]] = []
    for i in range(num_segments + 1):
        t = i / num_segments if num_segments else 0
        theta = math.radians(start_angle + t * span)
        out.append({
            "x": cx + radius * math.cos(theta),
            "y": cy + radius * math.sin(theta),
        })
    return out


def _polygon_area(points: list[tuple[float, float]]) -> float:
    n = len(points)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += points[i][0] * points[j][1]
        area -= points[j][0] * points[i][1]
    return abs(area) / 2.0


def _convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Graham scan convex hull, used as a fallback when arc-sample drift
    prevents the chaining heuristic from closing a meaningful polygon."""
    if len(points) <= 1:
        return points

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    pts = sorted(set(points))
    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _trace_closed_paths(
    board: Board, tolerance: float
) -> list[list[tuple[float, float]]]:
    """Chain layer-28 lines + arcs into closed polygons by matching
    EDGE endpoints only (not every arc sample). Each edge carries its
    own intermediate-point payload so reconstructed polygons keep the
    full curvature of arcs while the chaining algorithm sees only one
    pair of endpoints per edge — eliminates the float-drift fragmentation
    that per-segment chaining suffers from on boards with many rounded
    corners."""
    edges: list[dict] = []

    for t in board.traces:
        if t.layer != 28:
            continue
        a = (_mm(t.a.x), _mm(t.a.y))
        b = (_mm(t.b.x), _mm(t.b.y))
        edges.append({"start": a, "end": b, "points": [a, b], "used": False})

    for arc in board.arcs:
        if arc.layer != 28:
            continue
        pts = _arc_to_points(arc)
        if len(pts) < 2:
            continue
        tup_pts = [(p["x"], p["y"]) for p in pts]
        edges.append({
            "start": tup_pts[0],
            "end": tup_pts[-1],
            "points": tup_pts,
            "used": False,
        })

    def points_match(p1, p2):
        return abs(p1[0] - p2[0]) < tolerance and abs(p1[1] - p2[1]) < tolerance

    def find_next(current_end):
        for e in edges:
            if e["used"]:
                continue
            if points_match(e["start"], current_end):
                return e, False
            if points_match(e["end"], current_end):
                return e, True
        return None, False

    closed_paths: list[list[tuple[float, float]]] = []
    while True:
        start_edge = next((e for e in edges if not e["used"]), None)
        if start_edge is None:
            break
        start_edge["used"] = True
        start_point = start_edge["start"]
        # Walk the edge in its natural direction first.
        path: list[tuple[float, float]] = list(start_edge["points"])
        current_point = path[-1]
        for _ in range(len(edges) + 10):
            if points_match(current_point, start_point) and len(path) > 3:
                # Drop the duplicate endpoint that closed the loop.
                closed_paths.append(path[:-1])
                break
            nxt, reverse = find_next(current_point)
            if nxt is None:
                break
            nxt["used"] = True
            seq = list(reversed(nxt["points"])) if reverse else list(nxt["points"])
            # Skip the first point — it's the same as `current_point`.
            path.extend(seq[1:])
            current_point = path[-1]
    return closed_paths


def _reconstruct_outline_polygons(board: Board) -> list[list[dict[str, float]]]:
    """Try several endpoint-matching tolerances and pick the one that
    yields the largest closed-area / fewest-paths score. Returns EVERY
    closed polygon found at the best-scoring tolerance (no area filter,
    so the green substrate fills both the board outline and any cutouts
    / mounting holes)."""
    if not board.traces and not board.arcs:
        return []

    # At each tolerance, only "significant" polygons (≥ 8 points & area
    # ≥ 0.5 mm²) count toward coverage — small fragments are arc-sample
    # noise that should be ignored. Score = covered_area / sqrt(N) so a
    # board with two real zones beats a board with one real zone + 16
    # noise dots, while not being as harsh as an n² penalty (which
    # collapses legit multi-zone outlines).
    MIN_PTS = 8
    MIN_AREA_MM2 = 0.5

    best_significant: list[list[tuple[float, float]]] = []
    best_score = -1.0
    for tol in (0.01, 0.05, 0.1, 0.2, 0.5, 1.0):
        paths = _trace_closed_paths(board, tol)
        if not paths:
            continue
        significant: list[list[tuple[float, float]]] = []
        for p in paths:
            if len(p) < MIN_PTS:
                continue
            area = _polygon_area(p)
            if area < MIN_AREA_MM2:
                continue
            significant.append(p)
        if not significant:
            continue
        total_area = sum(_polygon_area(p) for p in significant)
        n = len(significant)
        score = total_area / max(1.0, n ** 0.5)
        if score > best_score:
            best_score = score
            best_significant = significant

    best_paths = best_significant

    # Convex hull fallback ONLY when the edge-chaining heuristic returns
    # nothing at all (truly broken outline data). Multi-polygon boards
    # with cutouts are valid output and we keep them as-is.
    if not best_paths:
        all_points: set[tuple[float, float]] = set()
        for t in board.traces:
            if t.layer != 28:
                continue
            all_points.add((_mm(t.a.x), _mm(t.a.y)))
            all_points.add((_mm(t.b.x), _mm(t.b.y)))
        for arc in board.arcs:
            if arc.layer != 28:
                continue
            for pt in _arc_to_points(arc):
                all_points.add((pt["x"], pt["y"]))
        hull = _convex_hull(list(all_points))
        if len(hull) >= 3:
            return [[{"x": p[0], "y": p[1]} for p in hull]]
        return []

    return [
        [{"x": p[0], "y": p[1]} for p in path]
        for path in best_paths
        if len(path) >= 3
    ]


def _polygon_bbox(poly: list[dict[str, float]]) -> tuple[float, float, float, float]:
    xs = [p["x"] for p in poly]
    ys = [p["y"] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def _classify_dual_outlines(
    polygons: list[list[dict[str, float]]],
) -> dict[str, Any] | None:
    """Identify whether two outline polygons represent a single board
    laid out as TOP and BOTTOM views side-by-side (or stacked).

    XZZ manufacturer dumps encode both faces in the same coordinate
    space — the renderer needs to know which polygon is which to drive
    the side-toggle UI and recenter the camera on a single face.

    Returns a dict with `top` / `bottom` polygons, the split axis
    ("x" or "y"), the mid-gap split value on that axis, and bbox
    metadata for both faces. Returns None when there aren't exactly
    two polygons, or when the two polygons overlap on both axes
    (single board with internal cutouts).
    """
    if len(polygons) != 2:
        return None
    a = _polygon_bbox(polygons[0])
    b = _polygon_bbox(polygons[1])
    a_min_x, a_min_y, a_max_x, a_max_y = a
    b_min_x, b_min_y, b_max_x, b_max_y = b
    # Positive when the bboxes don't overlap on that axis.
    x_gap = max(b_min_x - a_max_x, a_min_x - b_max_x)
    y_gap = max(b_min_y - a_max_y, a_min_y - b_max_y)
    if x_gap <= 0 and y_gap <= 0:
        return None
    if x_gap >= y_gap:
        axis = "x"
        if a_max_x <= b_min_x:
            top_idx, bot_idx = 0, 1
            split = (a_max_x + b_min_x) / 2.0
        else:
            top_idx, bot_idx = 1, 0
            split = (b_max_x + a_min_x) / 2.0
    else:
        axis = "y"
        if a_max_y <= b_min_y:
            top_idx, bot_idx = 0, 1
            split = (a_max_y + b_min_y) / 2.0
        else:
            top_idx, bot_idx = 1, 0
            split = (b_max_y + a_min_y) / 2.0
    top_poly = polygons[top_idx]
    bot_poly = polygons[bot_idx]
    t_min_x, t_min_y, t_max_x, t_max_y = _polygon_bbox(top_poly)
    bo_min_x, bo_min_y, bo_max_x, bo_max_y = _polygon_bbox(bot_poly)
    return {
        "top": top_poly,
        "bottom": bot_poly,
        "axis": axis,
        "split": split,
        "bbox_top": {
            "x": t_min_x,
            "y": t_min_y,
            "w": t_max_x - t_min_x,
            "h": t_max_y - t_min_y,
        },
        "bbox_bottom": {
            "x": bo_min_x,
            "y": bo_min_y,
            "w": bo_max_x - bo_min_x,
            "h": bo_max_y - bo_min_y,
        },
    }


def _side_for(x: float, y: float, dual: dict[str, Any]) -> str:
    if dual["axis"] == "x":
        return "top" if x < dual["split"] else "bottom"
    return "top" if y < dual["split"] else "bottom"


def _bounds(*xs_ys_iters) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for it in xs_ys_iters:
        for x, y in it:
            xs.append(x)
            ys.append(y)
    if not xs:
        return 0.0, 0.0, 0.0, 0.0
    return min(xs), min(ys), max(xs), max(ys)


def _outline_payload_points(outline_payload: Any) -> list[tuple[float, float]]:
    """Flatten the emitted outline payload into points for view bounds."""
    if isinstance(outline_payload, list):
        return [
            (float(p["x"]), float(p["y"]))
            for p in outline_payload
            if isinstance(p, dict) and "x" in p and "y" in p
        ]
    if isinstance(outline_payload, dict):
        points: list[tuple[float, float]] = []
        for poly in outline_payload.get("polygons") or []:
            if not isinstance(poly, list):
                continue
            points.extend(
                (float(p["x"]), float(p["y"]))
                for p in poly
                if isinstance(p, dict) and "x" in p and "y" in p
            )
        return points
    return []


def to_render_payload(board: Board) -> dict[str, Any]:
    """Convert a parsed `Board` into the render JSON consumed by the viewer.

    All spatial coordinates are emitted in millimetres — the WebGL viewer
    in `web/js/pcb_viewer.js` runs its ortho camera, hover threshold and
    label scaling in mm.
    """
    net_index: dict[str, Net] = {n.name: n for n in board.nets}

    parts_payload = [
        _convert_part(part, [board.pins[i] for i in part.pin_refs], net_index)
        for part in board.parts
    ]

    # refdes -> ctype map so each pin payload knows its parent's type
    # (TEST_POINT pins recolour to orange in the WebGL viewer; other
    # pins stay net-category coloured).
    type_by_refdes = {p["id"]: p["type"] for p in parts_payload}

    # Tag pins whose parent part is a DFM-alternate ghost. The viewer
    # builds them in a dedicated InstancedMesh that doesn't touch the
    # placed-pin singletons (`_circularPinInstance` /
    # `_rectPinInstances`), keeping the side-filter loop intact, and
    # toggles their visibility via the same `_showDnp` flag that
    # controls the body outlines.
    dnp_refdes = {p.refdes for p in board.parts if p.is_dnp}
    pins_payload = []
    for i, pin in enumerate(board.pins):
        pin_dict = _convert_pin(pin, i, net_index, type_by_refdes.get(pin.part_refdes))
        if pin.part_refdes in dnp_refdes:
            pin_dict["is_dnp"] = True
        pins_payload.append(pin_dict)

    vias_payload = [
        {
            "id": f"VIA_{i}",
            "x": _mm(v.pos.x),
            "y": _mm(v.pos.y),
            "radius": max(_mm(v.radius), 0.03),
            "net": v.net or "",
            "padstack": v.padstack,
            "layer_span": v.layer_span,
        }
        for i, v in enumerate(board.vias)
    ]

    test_pads_payload = [
        {
            "id": f"TP{i + 1}",
            "x": _mm(tp.pos.x),
            "y": _mm(tp.pos.y),
            "radius": max(_mm(tp.radius), 0.05),
            "net": tp.net or "",
            "layer": _layer_name(tp.layer),
        }
        for i, tp in enumerate(board.test_pads)
    ]

    # The Three.js viewer expects traces as polylines. Layer 28 lines/arcs
    # are explicitly EXCLUDED here — they're already rendered as part of
    # the closed outline polygon (fill + border), so emitting them again
    # as separate polylines causes visible misalignments where individual
    # arc samples and straight-line endpoints drift by ~10 µm.
    traces_payload: list[dict[str, Any]] = [
        {
            "points": [
                {"x": _mm(t.a.x), "y": _mm(t.a.y)},
                {"x": _mm(t.b.x), "y": _mm(t.b.y)},
            ],
            "layer": t.layer,
            "width": _mm(t.width),
            "net": t.net or "",
        }
        for t in board.traces
        if t.layer != 28
    ]
    for arc in board.arcs:
        if arc.layer == 28:
            continue
        pts = _arc_to_points(arc)
        if len(pts) >= 2:
            traces_payload.append({
                "points": pts,
                "layer": arc.layer,
                "width": 0,
                "net": "",
            })

    # Outline emit strategy:
    #  1. If the parser already surfaced an outline polygon (kicad / brd …)
    #     use it directly.
    #  2. Otherwise reconstruct closed polygons from layer-28 lines + arcs
    #     by heuristic edge chaining (multi-tolerance, scored by area /
    #     penalty). When the algorithm finds multiple closed paths
    #     (board with cutouts), emit them as `outline.polygons` —
    #     the viewer's `createBoard` understands that shape.
    dual_outline: dict[str, Any] | None = None
    if board.outline:
        outline_payload: Any = [{"x": _mm(p.x), "y": _mm(p.y)} for p in board.outline]
    else:
        polygons = _reconstruct_outline_polygons(board)
        if len(polygons) == 1:
            outline_payload = polygons[0]
        elif polygons:
            outline_payload = {"polygons": polygons}
            dual_outline = _classify_dual_outlines(polygons)
        else:
            outline_payload = []

    # When the parser ships top + bottom views in the same coordinate
    # space (XZZ side-by-side / stacked dual layout), tag every entity
    # with the face it belongs to so the viewer can filter visibility
    # and recentre the camera on a single face.
    if dual_outline is not None:
        for part in parts_payload:
            cx = part["x"] + part["width"] / 2
            cy = part["y"] + part["height"] / 2
            part["_side"] = _side_for(cx, cy, dual_outline)
        for pin in pins_payload:
            pin["_side"] = _side_for(pin["x"], pin["y"], dual_outline)
        for via in vias_payload:
            via["_side"] = _side_for(via["x"], via["y"], dual_outline)
        for tp in test_pads_payload:
            tp["_side"] = _side_for(tp["x"], tp["y"], dual_outline)
        for tr in traces_payload:
            pts = tr["points"]
            if pts:
                xs = [p["x"] for p in pts]
                ys = [p["y"] for p in pts]
                cx = sum(xs) / len(xs)
                cy = sum(ys) / len(ys)
                tr["_side"] = _side_for(cx, cy, dual_outline)
    else:
        # Single-coordinate-space formats (TVW, KiCad, BRD, …) carry a
        # real `Part.layer` field already. Surface it as `_side` so the
        # viewer's TOP / BOTTOM filter actually filters; without this
        # the side toggle was a no-op for these formats.
        for part in parts_payload:
            part["_side"] = part.get("layer") or None
        # Pins / vias / test_pads are emitted with a `layer` field too;
        # mirror it onto `_side`.
        for pin in pins_payload:
            pin["_side"] = pin.get("layer") or None
        for via in vias_payload:
            via.setdefault("_side", None)
        for tp in test_pads_payload:
            tp["_side"] = tp.get("layer") or None
        for tr in traces_payload:
            tr.setdefault("_side", None)

    # Compute board bounds from every spatial source we have, in mm.
    pin_pts = ((_mm(p.pos.x), _mm(p.pos.y)) for p in board.pins)
    via_pts = ((_mm(v.pos.x), _mm(v.pos.y)) for v in board.vias)
    tp_pts = ((_mm(tp.pos.x), _mm(tp.pos.y)) for tp in board.test_pads)
    trace_pts: list[tuple[float, float]] = []
    for t in board.traces:
        trace_pts.append((_mm(t.a.x), _mm(t.a.y)))
        trace_pts.append((_mm(t.b.x), _mm(t.b.y)))
    body_pts: list[tuple[float, float]] = []
    for part in board.parts:
        for seg in part.body_lines:
            body_pts.append((_mm(seg.a.x), _mm(seg.a.y)))
            body_pts.append((_mm(seg.b.x), _mm(seg.b.y)))

    outline_bound_pts = _outline_payload_points(outline_payload)
    if outline_bound_pts:
        # Fit the camera to the actual board contour when available.
        # Some TVW files still expose malformed trace records while the
        # filled-surface outline is correct; including those outliers in
        # the fit makes the real board vanish at load.
        min_x, min_y, max_x, max_y = _bounds(outline_bound_pts)
    else:
        min_x, min_y, max_x, max_y = _bounds(
            pin_pts, via_pts, tp_pts, trace_pts, body_pts
        )
    width_mm = max(max_x - min_x, 1.0)
    height_mm = max(max_y - min_y, 1.0)

    # Net diagnostic expectations — emit only the nets that carry a
    # manufacturer-tagged value (resistance / open / voltage). Boards
    # without a post-v6 diagnostic block ship none, so this stays
    # absent and the frontend skips the inspector overlay.
    net_diagnostics = [
        {
            "name": n.name,
            "expected_resistance_ohms": n.expected_resistance_ohms,
            "expected_open": n.expected_open,
            "expected_voltage_v": n.expected_voltage_v,
        }
        for n in board.nets
        if (
            n.expected_resistance_ohms is not None
            or n.expected_open
            or n.expected_voltage_v is not None
        )
    ]

    # Manufacturer-tagged inspection rectangles (XZZ type_03 blocks).
    # Empty on most boards; only diagnostic-tagged exports ship them.
    markers_payload = [
        {
            "centre_x": _mm(m.centre.x),
            "centre_y": _mm(m.centre.y),
            "x_min": _mm(m.bbox[0].x),
            "y_min": _mm(m.bbox[0].y),
            "x_max": _mm(m.bbox[1].x),
            "y_max": _mm(m.bbox[1].y),
            "marker_id": m.marker_id,
        }
        for m in board.markers
    ]
    if dual_outline is not None:
        for marker in markers_payload:
            marker["_side"] = _side_for(
                marker["centre_x"], marker["centre_y"], dual_outline
            )

    mech_holes_payload = [
        {
            "x": _mm(h.pos.x),
            "y": _mm(h.pos.y),
            "diameter": _mm(h.diameter),
            "is_fiducial": h.is_fiducial,
        }
        for h in board.mech_holes
    ]

    payload: dict[str, Any] = {
        "board_id": board.board_id,
        "file_hash": board.file_hash,
        "format_type": board.source_format,
        "board_width": width_mm,
        "board_height": height_mm,
        "board_offset_x": min_x,
        "board_offset_y": min_y,
        "outline": outline_payload,
        "components": parts_payload,
        "pins": pins_payload,
        "vias": vias_payload,
        "test_pads": test_pads_payload,
        "traces": traces_payload,
        "markers": markers_payload,
        "mech_holes": mech_holes_payload,
        "net_diagnostics": net_diagnostics,
        "components_count": len(parts_payload),
        "pins_count": len(pins_payload),
        "nets_count": len(board.nets),
    }
    if dual_outline is not None:
        payload["dual_outline"] = dual_outline
    return payload
