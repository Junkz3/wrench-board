"""`.pcb` XZZ-encrypted boardview parser.

Thin shim around the in-tree XZZ engine
(`api/board/parser/_xzz_engine/`). The engine does the heavy lifting —
XOR descramble, DES-ECB on PART blocks, full block-graph parsing
including arcs, traces, vias, silkscreen and the on-board translation
to origin. This shim just calls `XZZFile.load()` then maps its raw
`.parts` / `.pins` / `.lines` / `.arcs` / `.vias` collections onto the
`Board` Pydantic model so the rest of the app (agent tools, render
endpoint, frontend) treats the result the same as any other format.

Coordinates from the engine are floats in millimetres; positions and
pad dims are converted to integer mils on the way out (the Pydantic
Point model is `int` in mils).
"""

from __future__ import annotations

import re
import struct

from api.board.model import (
    Arc,
    Board,
    Layer,
    Marker,
    Net,
    Part,
    Pin,
    Point,
    Segment,
    TestPad,
    Trace,
    Via,
)
from api.board.parser._ascii_boardview import GROUND_RE, POWER_RE
from api.board.parser._xzz_engine.xzz_file import XZZFile
from api.board.parser.base import BoardParser, InvalidBoardFile, register

MM_TO_MIL = 39.37007874  # 1 mm = 39.37 mil


def _mm_to_mils(x: float) -> float:
    """Convert mm → mils. Returned as float so sub-mil resolution is
    preserved on absolute coords (XZZ probe-pad pin positions land at
    fractional mils — int rounding made them drift off-centre by up to
    0.013 mm vs the silkscreen body)."""
    return x * MM_TO_MIL


_VALID_REFDES = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,31}$")


def _decode(name: bytes | str) -> str:
    if isinstance(name, str):
        return name.strip("\x00").strip()
    return name.decode("utf-8", errors="replace").strip("\x00").strip()


def _layer_for_side(side: str) -> Layer:
    s = (side or "").upper()
    if s == "BOTTOM":
        return Layer.BOTTOM
    if s == "BOTH":
        return Layer.BOTH
    return Layer.TOP


def _classify_net(name: str) -> tuple[bool, bool]:
    return bool(POWER_RE.match(name)), bool(GROUND_RE.match(name))


def _xzz_to_board(
    xzz: XZZFile, *, file_hash: str, board_id: str,
    diagnostics: dict | None = None,
    markers_abs_mm: list[dict] | None = None,
    type09_test_pads: list[dict] | None = None,
) -> Board:
    """Map a loaded `XZZFile` into the `Board` Pydantic model.

    `diagnostics` is the dict produced by `_xzz_engine_extras.
    extract_post_v6_diagnostics` — when non-empty its `resistance` /
    `voltage` / `signal_map` sub-maps are merged into the resulting
    `Net` objects. Pure mapping pass; the parser shim does the
    extraction.
    """
    parts: list[Part] = []
    pins: list[Pin] = []
    pin_index_by_xzz_part: dict[int, list[int]] = {}

    # XZZ v6+ stores placeholder net names ("Net1", "Net2", …) in the
    # main net block and the real signal labels ("GND", "PP3V3_G3H", …)
    # in a post-v6 signal section. Without this map, pins on
    # manufacturer-tagged exports come out with 1 unique placeholder net
    # per pin → no power / ground / signal classification, no net
    # highlight on click.
    signal_map: dict[str, str] = {}
    post_v6 = getattr(xzz, "post_v6_data", None) or {}
    if isinstance(post_v6, dict):
        raw_map = post_v6.get("signal_map") or {}
        if isinstance(raw_map, dict):
            for k, v in raw_map.items():
                key = _decode(k) if isinstance(k, (bytes, bytearray)) else str(k)
                val = _decode(v) if isinstance(v, (bytes, bytearray)) else str(v)
                if key and val:
                    signal_map[key] = val

    # Stripped-refdes detection. Some XZZ exports ship every non-test
    # part as `U1` and every test pad as `TEST_PAD_U1` — a placeholder,
    # not a real refdes. Without this, the model gets a part_by_refdes
    # index where every query collides. The convention used by other
    # XZZ viewers in the wild is to display `group_name` (`J-D13`,
    # `PAD-2`, …) as the part label on these boards, so we follow the
    # same: take group_name when present and disambiguate duplicates
    # with a `_<n>` suffix; fall back to category-numbered (`U0001`)
    # only when both name and group_name fail to disambiguate.
    raw_names = [_decode(p.name) for p in xzz.parts]
    unique_names = len(set(raw_names))
    placeholder_mode = unique_names <= max(2, len(raw_names) // 50)
    gn_counters: dict[str, int] = {}
    cat_counters: dict[str, int] = {}

    def _sanitize_refdes(s: str) -> str:
        """Trim to characters that pass `_VALID_REFDES` and keep
        within 32 chars. Drops disallowed punctuation (replaces with
        `_`); keeps `-` because the validator accepts it."""
        out = []
        for c in s.strip():
            if c.isalnum() or c in ("_", "-"):
                out.append(c)
            else:
                out.append("_")
        cleaned = "".join(out)[:32]
        if not cleaned or not cleaned[0].isalpha():
            cleaned = "X" + cleaned
        return cleaned

    for k, xzz_part in enumerate(xzz.parts):
        original_name = _decode(xzz_part.name)
        # Engine prepends "TEST_PAD_" to single-pin parts in
        # parser_helpers.py L891 (`f"TEST_PAD_{old_name}"`). The TP
        # designation already flows through `category="TP"` and the
        # render layer's TEST_POINT classification — the prefix is
        # redundant noise in the inspector ("TEST_PAD_PAD63" vs
        # "PAD63"). Strip it on the way in so the refdes stays
        # readable.
        if original_name.startswith("TEST_PAD_"):
            original_name = original_name[len("TEST_PAD_"):]
        if placeholder_mode:
            gn = (xzz_part.group_name or "").strip()
            if gn:
                # Disambiguate duplicates with `_<n>` suffix. First
                # occurrence keeps the bare group_name; subsequent
                # ones append `_2`, `_3`, …
                gn_counters[gn] = gn_counters.get(gn, 0) + 1
                base = _sanitize_refdes(gn)
                if gn_counters[gn] > 1:
                    refdes = f"{base}_{gn_counters[gn]}"
                else:
                    refdes = base
            else:
                # No group_name to anchor — fall back to category +
                # sequential.
                cat = (getattr(xzz_part, "category", "") or "").upper().strip()
                if not cat:
                    first_alpha = next(
                        (c.upper() for c in original_name if c.isalpha()), "X"
                    )
                    cat = first_alpha
                cat_counters[cat] = cat_counters.get(cat, 0) + 1
                refdes = f"{cat}{cat_counters[cat]:04d}"
            if not _VALID_REFDES.match(refdes):
                # Last-resort fallback — should be unreachable after
                # _sanitize_refdes but keeps the index trustworthy.
                continue
        else:
            refdes = original_name
            if not _VALID_REFDES.match(refdes):
                # Skip parts whose refdes can't be recovered cleanly — they'd
                # poison the part_by_refdes index downstream.
                continue
        part_rotation = getattr(xzz_part, "rotation", 0.0) or 0.0
        per_part_pins = getattr(xzz_part, "pins", []) or []

        # Detect test-point family parts. The XZZ engine sets
        # `xzz_part.category == "TP"` for genuine test/probe pads
        # (single-pin parts the manufacturer tagged in the source).
        # We additionally accept the historical refdes-prefix heuristic
        # so 2-pin XW board-to-board connectors (which the engine does
        # NOT tag as TP) also skip the spacing cap — they fit the same
        # "isolated, no-neighbour-overlap" footprint family even when
        # not flagged at the format level.
        upper_refdes = refdes.upper()
        engine_category = (getattr(xzz_part, "category", "") or "").upper()
        is_test_point = (
            engine_category == "TP"
            or upper_refdes.startswith(('TEST_PAD', 'TEST', 'XW', 'TP'))
        )

        # XW board-to-board connector pads — 2-pin parts with an
        # elongated silkscreen body (XW8230 0.0762×0.0254, two pads
        # at the body extremes). The 32-byte shape block reports pads
        # that are W/H-swapped vs the body and overshoot in height
        # (the engine treats them like standard SMD passives, which
        # they aren't), so /2 leaves them ultra-thin and overflowing
        # the silkscreen. Derive each pad directly from the body
        # bbox: ~45% of the long axis (leaves a centre gap between
        # the two pads) × ~90% of the short axis (fills the body
        # height, no overflow). 1-pin XW (isolated probe / TEST_PAD)
        # keeps the /2 calibration — those parts have no anchor body
        # to derive from.
        xw_pad_dims_mm: tuple[float, float] | None = None
        if upper_refdes.startswith("XW") and len(per_part_pins) == 2:
            part_lines = list(getattr(xzz_part, "lines", []) or [])
            if part_lines:
                # Engine line endpoints are mm float — full precision,
                # no round-trip loss now that Point is float too, so we
                # can read the body bbox directly.
                xs = [v for ln in part_lines for v in (ln.x1, ln.x2)]
                ys = [v for ln in part_lines for v in (ln.y1, ln.y2)]
                body_w = max(xs) - min(xs)
                body_h = max(ys) - min(ys)
                if body_w > 0 and body_h > 0:
                    body_long = max(body_w, body_h)
                    body_short = min(body_w, body_h)
                    if body_long / body_short > 1.4:
                        pad_long = body_long * 0.35
                        pad_short = body_short * 0.85
                        if body_w >= body_h:
                            xw_pad_dims_mm = (pad_long, pad_short)
                        else:
                            xw_pad_dims_mm = (pad_short, pad_long)

        pad_dim_cap_mm: float | None = None
        if len(per_part_pins) >= 2 and not is_test_point:
            min_dist_mm = float("inf")
            for i in range(len(per_part_pins)):
                for k2 in range(i + 1, len(per_part_pins)):
                    dx = per_part_pins[i].pos.x - per_part_pins[k2].pos.x
                    dy = per_part_pins[i].pos.y - per_part_pins[k2].pos.y
                    d = (dx * dx + dy * dy) ** 0.5
                    if 0 < d < min_dist_mm:
                        min_dist_mm = d
            if min_dist_mm not in (0, float("inf")):
                pad_dim_cap_mm = min_dist_mm * 0.70

        part_pin_indices: list[int] = []
        for j, xzz_pin in enumerate(per_part_pins):
            net_name = _decode(getattr(xzz_pin, "net", "")) or None
            if net_name in ("UNCONNECTED", "NC", ""):
                net_name = None
            elif net_name in signal_map:
                # Resolve the v6+ placeholder ("Net973") to the real
                # signal name ("PP3V3_G3H").
                net_name = signal_map[net_name]

            x_mils = _mm_to_mils(xzz_pin.pos.x)
            y_mils = _mm_to_mils(xzz_pin.pos.y)

            # Pad dimension + rotation strategy.
            # The engine's flip_flag swap heuristic in
            # _xzz_engine/parser_helpers.py L738 swaps width/height for
            # rotations in [<45, 135-225, >315] (i.e. near 0°/180°/360°)
            # and leaves them as-is for [45-135, 225-315] (near
            # 90°/270°). The intent: the swap encodes the visual pad
            # orientation in the QUADRANT-aligned frame, treating the
            # rotation as if it were the nearest of {0,90,180,270}.
            #
            # Therefore the visual rule is:
            #   - dims = post-swap (xzz_pin.width / xzz_pin.height)
            #   - pad_rotation = delta between the actual part rotation
            #     and the quadrant the engine snapped it to
            #
            # For standard 0/90/180/270 packages delta == 0, no extra
            # rotation. For diagonal packages (XW0451 at 120°, C0456
            # at 151.4°, …) delta is the small remainder that tilts the
            # already-correctly-oriented pad to its final angle.
            normalized = part_rotation % 360.0
            quadrants = (0.0, 90.0, 180.0, 270.0, 360.0)
            nearest = min(quadrants, key=lambda a: min(
                abs(a - normalized), 360.0 - abs(a - normalized)
            ))
            delta = (normalized - nearest + 180.0) % 360.0 - 180.0  # signed in (-180, 180]

            # Shape kind from the 32-byte XZZ pin block (signed int32 LE).
            # Two binary signals carry the pad shape:
            #   int[0] vs int[1]  : symmetry — self-ref means w == h
            #   (int[6] >> 16)    : kind family (1 = round-ish, 2 = sharp)
            #
            #   v0 == v1 AND v6_kind == 1   -> circle  (BGA balls,
            #                                  round through-hole,
            #                                  round TP probe pads)
            #   v0 == v1 AND v6_kind == 2   -> square  (board-to-board
            #                                  connector lands like
            #                                  J5150 with 4171 sharp
            #                                  0.302 mm pads)
            #   v0 != v1 AND v6_kind == 2   -> rect    (standard SMD
            #                                  pads, ICs, RES/CAP/L/D)
            #   v0 != v1 AND v6_kind == 1   -> oval    (TEST_PAD_Z*
            #                                  oblong probe landings,
            #                                  pill / capsule shape)
            shape = "circle"
            try:
                raw_hex = getattr(xzz_pin, "raw_shape_data", "") or ""
                if raw_hex:
                    raw_bytes = bytes.fromhex(raw_hex)
                    if len(raw_bytes) == 32:
                        ints = struct.unpack("<8i", raw_bytes)
                        v0, v1 = ints[0], ints[1]
                        v6_kind = (ints[6] >> 16) & 0xFFFF
                        if v0 == v1:
                            shape = "circle" if v6_kind == 1 else "square"
                        else:
                            shape = "oval" if v6_kind == 1 else "rect"
            except (ValueError, struct.error):
                pass

            # Pad dimensions:
            #   - All pads start with a /2 calibration (engine over-reports).
            #   - The 70% inter-pin spacing cap applies ONLY to circle
            #     and square pads (where the engine's reported dim is
            #     a 'bounding diameter' that often spills beyond what
            #     visually fits between neighbours — observed on
            #     CE111 / XW8002 etc.). Rect / oval pads carry real
            #     oriented dimensions that legitimately extend beyond
            #     the body (SOT/SON 'small legs' on UE430 / UE420
            #     where pad width 1.018 mm laterally exceeds the
            #     0.197 mm pin pitch by design — capping that erases
            #     the legs).
            raw_w = xzz_pin.width or 0
            raw_h = xzz_pin.height or 0
            # /2 calibrates against engine over-reporting. Inter-pin
            # spacing cap below handles passive overlap; no extra
            # global shrink — that erased ultra-small probe pads
            # (XW8230 dropped to ~0.018 mm sub-pixel).
            # Calibration factor against the engine's over-reported
            # raw pad dim. /2.0 was the original setting, but several
            # XZZ exports showed every R/C/IC pad overflowing its
            # silkscreen body by ~25%. /2.5 brings them inside the
            # body without erasing standard SOT/SON 'small legs' — the
            # 70% inter-pin spacing cap below still catches the rare
            # tight-pitch passive that the calibration alone misses.
            w_mm = raw_w / 2.5 if raw_w else 0
            h_mm = raw_h / 2.5 if raw_h else 0
            if xw_pad_dims_mm is not None:
                w_mm, h_mm = xw_pad_dims_mm
            if pad_dim_cap_mm is not None and shape in ("circle", "square"):
                w_mm = min(w_mm, pad_dim_cap_mm) if w_mm else w_mm
                h_mm = min(h_mm, pad_dim_cap_mm) if h_mm else h_mm
            # Keep mils as float — sub-mil XW pads (~0.9 mil) need the
            # fractional precision; rounding to int collapsed them to
            # 1×1 mil squares.
            w_mils = max(w_mm * MM_TO_MIL, 0.1) if w_mm else 0.0
            h_mils = max(h_mm * MM_TO_MIL, 0.1) if h_mm else 0.0

            pad_size: tuple[float, float] | None = None
            if w_mils > 0 and h_mils > 0:
                if shape == "circle":
                    diameter = min(w_mils, h_mils)
                    pad_size = (diameter, diameter)
                else:
                    pad_size = (w_mils, h_mils)

            # Pad rotation: see the dim block above. The engine's
            # quadrant-aligned swap already encodes the visual frame
            # for the nearest of {0,90,180,270}, so we only need the
            # leftover delta. The ~40° special-case engine path sets
            # pin.rotation explicitly via a derived formula; if we see
            # that signal, honour it instead of the delta.
            engine_pin_rot = getattr(xzz_pin, "rotation", 0.0) or 0.0
            pin_rot = engine_pin_rot if engine_pin_rot != 0.0 else delta

            pins.append(
                Pin(
                    part_refdes=refdes,
                    index=j + 1,  # 1-based per part, matches Test_Link convention
                    pos=Point(x=x_mils, y=y_mils),
                    net=net_name,
                    layer=_layer_for_side(getattr(xzz_pin, "side", "TOP")),
                    pad_shape=shape,
                    pad_size=pad_size,
                    pad_rotation_deg=pin_rot,
                )
            )
            part_pin_indices.append(len(pins) - 1)

        pin_index_by_xzz_part[k] = part_pin_indices

        # Component body silkscreen — XZZPart.lines if available; else
        # we leave body_lines empty and the renderer falls back to shape.
        body_lines: list[Segment] = []
        for ln in getattr(xzz_part, "lines", []) or []:
            body_lines.append(
                Segment(
                    a=Point(x=_mm_to_mils(ln.x1), y=_mm_to_mils(ln.y1)),
                    b=Point(x=_mm_to_mils(ln.x2), y=_mm_to_mils(ln.y2)),
                )
            )

        # Bbox priority: silkscreen > pin span > zero-fallback.
        if body_lines:
            xs = [s.a.x for s in body_lines] + [s.b.x for s in body_lines]
            ys = [s.a.y for s in body_lines] + [s.b.y for s in body_lines]
            bbox = (Point(x=min(xs), y=min(ys)), Point(x=max(xs), y=max(ys)))
        elif part_pin_indices:
            xs = [pins[i].pos.x for i in part_pin_indices]
            ys = [pins[i].pos.y for i in part_pin_indices]
            bbox = (Point(x=min(xs), y=min(ys)), Point(x=max(xs), y=max(ys)))
        else:
            cx = _mm_to_mils(xzz_part.x)
            cy = _mm_to_mils(xzz_part.y)
            bbox = (Point(x=cx, y=cy), Point(x=cx, y=cy))

        layer = (
            Layer.BOTTOM
            if getattr(xzz_part, "mounting_side", "TOP") == "BOTTOM"
            else Layer.TOP
        )
        parts.append(
            Part(
                refdes=refdes,
                layer=layer,
                is_smd=getattr(xzz_part, "part_type", "SMD") == "SMD",
                bbox=bbox,
                pin_refs=part_pin_indices,
                body_lines=body_lines,
                footprint=_decode(getattr(xzz_part, "group_name", "")) or None,
                category=_decode(getattr(xzz_part, "category", "")) or None,
                # Propagate part rotation so the viewer rotates the
                # generic body shape (rounded rect, IC notch, etc) to
                # match the package orientation. body_lines stay in
                # absolute coords and are rendered outside the rotated
                # group by the viewer to avoid double-rotation.
                rotation_deg=part_rotation or None,
            )
        )

    # Top-level traces (board copper / outline lines).
    traces: list[Trace] = []
    for ln in getattr(xzz, "lines", []) or []:
        traces.append(
            Trace(
                a=Point(x=_mm_to_mils(ln.x1), y=_mm_to_mils(ln.y1)),
                b=Point(x=_mm_to_mils(ln.x2), y=_mm_to_mils(ln.y2)),
                layer=ln.layer,
                net=None,
            )
        )

    vias: list[Via] = []
    for v in getattr(xzz, "vias", []) or []:
        radius = max(v.layer_a_radius, v.layer_b_radius)
        vias.append(
            Via(
                pos=Point(x=_mm_to_mils(v.x), y=_mm_to_mils(v.y)),
                radius=max(_mm_to_mils(radius), 1),
                net=None,
            )
        )

    arcs: list[Arc] = []
    for a in getattr(xzz, "arcs", []) or []:
        arcs.append(
            Arc(
                center=Point(x=_mm_to_mils(a.x1), y=_mm_to_mils(a.y1)),
                radius=max(_mm_to_mils(a.radius), 1),
                angle_start=float(getattr(a, "angle_start", 0.0) or 0.0),
                angle_end=float(getattr(a, "angle_end", 0.0) or 0.0),
                layer=int(getattr(a, "layer", 28) or 28),
            )
        )

    nets = _derive_nets(pins, diagnostics or {}, signal_map)

    # Type-03 markers — manufacturer-tagged inspection rectangles
    # (only present on diagnostic-tagged XZZ exports). Coords come
    # from the extras module in absolute mm; we apply the
    # engine's xy_translation (same translation it applied to pins
    # and silkscreen) so the markers land in the same frame as
    # everything else, then convert mm → mils to match the model.
    markers: list[Marker] = []
    if markers_abs_mm:
        tx = getattr(xzz.xy_translation, "x", 0.0) or 0.0
        ty = getattr(xzz.xy_translation, "y", 0.0) or 0.0
        for m in markers_abs_mm:
            cx = _mm_to_mils(m["centre_x"] - tx)
            cy = _mm_to_mils(m["centre_y"] - ty)
            x_min = _mm_to_mils(m["x_min"] - tx)
            y_min = _mm_to_mils(m["y_min"] - ty)
            x_max = _mm_to_mils(m["x_max"] - tx)
            y_max = _mm_to_mils(m["y_max"] - ty)
            markers.append(
                Marker(
                    centre=Point(x=cx, y=cy),
                    bbox=(Point(x=x_min, y=y_min), Point(x=x_max, y=y_max)),
                    marker_id=int(m.get("marker", 0)),
                )
            )

    # Type-09 entries — the spec calls them "Test pad / drill hole":
    # `inner_diameter > 0` means it's a drilled hole (mounting hole or
    # through-hole), `inner_diameter == 0` means a solid pad (real probe
    # test point). The engine never dispatches block_type=0x09 at all
    # (parse_test_pad_block is imported but never wired), so both kinds
    # were silently dropped on the affected fixtures.
    # We split the two types here:
    #   - drilled (inner_d > 0) → board.vias  (annular ring render)
    #   - solid (inner_d == 0)  → board.test_pads (filled disc render)
    # This way the mounting holes render with the correct visual
    # semantics — outer ring around an inner hole — instead of as
    # fake test pads.
    test_pads_out: list[TestPad] = []
    extra_vias: list[Via] = []
    if type09_test_pads:
        tx = getattr(xzz.xy_translation, "x", 0.0) or 0.0
        ty = getattr(xzz.xy_translation, "y", 0.0) or 0.0
        net_by_idx: dict[int, str] = {}
        for n in (getattr(xzz, "nets", []) or []):
            if n is not None:
                net_by_idx[getattr(n, "index", 0)] = getattr(n, "name", "") or ""
        for tp in type09_test_pads:
            x_mils = _mm_to_mils(tp["x_mm"] - tx)
            y_mils = _mm_to_mils(tp["y_mm"] - ty)
            outer_r_mils = _mm_to_mils(
                max(tp["outer_width_mm"], tp["outer_height_mm"]) / 2.0
            )
            net_name = net_by_idx.get(tp["net_index"], "") or None
            if tp["inner_diameter_mm"] > 0:
                # Drill / mounting hole. Use the OUTER radius as the
                # via radius (the annular ring renderer derives the
                # inner hole from a fixed proportion).
                extra_vias.append(
                    Via(
                        pos=Point(x=x_mils, y=y_mils),
                        radius=max(outer_r_mils, 1.0),
                        net=net_name,
                    )
                )
            else:
                # Solid pad — real test/probe point.
                test_pads_out.append(
                    TestPad(
                        pos=Point(x=x_mils, y=y_mils),
                        radius=max(outer_r_mils, 1.0),
                        layer=Layer.TOP,
                        net=net_name,
                    )
                )
    if extra_vias:
        vias = list(vias) + extra_vias

    return Board(
        board_id=board_id,
        file_hash=file_hash,
        source_format="xzz",
        outline=[],  # we surface board contour via traces (layer 28 lines)
        parts=parts,
        pins=pins,
        nets=nets,
        nails=[],
        vias=vias,
        test_pads=test_pads_out,
        traces=traces,
        arcs=arcs,
        markers=markers,
    )


def _derive_nets(
    pins: list[Pin],
    diagnostics: dict,
    signal_map: dict[str, str],
) -> list[Net]:
    """Build the per-net rollup from pin list, attaching diagnostic
    expectations (resistance / voltage) keyed by either the placeholder
    net name (`Net204`) or its mapped real name (`PP_VBAT`) — the
    source format keys diagnostics on placeholders, but pin nets are
    already resolved to real names by the time we get here.
    """
    by_name: dict[str, list[int]] = {}
    for i, pin in enumerate(pins):
        if pin.net is None:
            continue
        by_name.setdefault(pin.net, []).append(i)

    # Diagnostic look-up: try the resolved name first, then walk
    # signal_map backwards to find the placeholder it resolved from
    # and look that up. (signal_map is `placeholder → real`; we want
    # `real → placeholder` here.)
    resistance_map: dict[str, dict] = diagnostics.get("resistance") or {}
    voltage_map: dict[str, float] = diagnostics.get("voltage") or {}
    real_to_placeholder = {real: ph for ph, real in signal_map.items()}

    def lookup(real_name: str, src: dict) -> object | None:
        if real_name in src:
            return src[real_name]
        ph = real_to_placeholder.get(real_name)
        if ph and ph in src:
            return src[ph]
        return None

    out: list[Net] = []
    for name in sorted(by_name):
        is_pwr, is_gnd = _classify_net(name)
        r = lookup(name, resistance_map)
        v = lookup(name, voltage_map)
        out.append(
            Net(
                name=name,
                pin_refs=by_name[name],
                is_power=is_pwr,
                is_ground=is_gnd,
                expected_resistance_ohms=r["expected_resistance_ohms"] if isinstance(r, dict) else None,
                expected_open=r["expected_open"] if isinstance(r, dict) else False,
                expected_voltage_v=float(v) if isinstance(v, (int, float)) else None,
            )
        )
    return out


@register
class XZZParser(BoardParser):
    extensions = (".pcb",)

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        if len(raw) < 64:
            raise InvalidBoardFile("xzz: file shorter than 64 bytes")
        xzz = XZZFile()
        if not xzz.load(raw):
            raise InvalidBoardFile(f"xzz: {xzz.error_msg or 'load failed'}")
        # Side-channel: pull manufacturer-tagged diagnostic expectations
        # (resistance / voltage / signal_map) out of the post-v6 block.
        # The engine recognises the markers but its
        # `_parse_resistance_section` expects a different shape than what
        # diagnostic-tagged XZZ exports actually carry, so we re-extract
        # from scratch in `_xzz_engine_extras`. Empty dict on boards
        # that don't ship the section (the majority of XZZ files).
        from api.board.parser._xzz_engine_extras import (
            extract_post_v6_diagnostics,
            extract_type_03_markers,
            extract_type_09_test_pads,
        )
        diagnostics = extract_post_v6_diagnostics(raw)
        markers_abs_mm = extract_type_03_markers(raw)
        type09 = extract_type_09_test_pads(raw)
        return _xzz_to_board(
            xzz, file_hash=file_hash, board_id=board_id,
            diagnostics=diagnostics, markers_abs_mm=markers_abs_mm,
            type09_test_pads=type09,
        )
