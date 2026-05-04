"""FZ-zlib parser — Mentor/Allegro-style pipe-delimited boardview format.

Real-world `.fz` files in the repair community come in two distinct
flavours:

1. **FZ-zlib** (this module). 4-byte LE int32 header carrying the
   decompressed size, followed by a zlib stream. Decompression yields
   pipe-delimited (`!`) text with section schemas (`A!col1!col2!…`)
   and data rows (`S!val1!val2!…`). This is the format Quanta /
   ASRock / ASUS Prime / Gigabyte boards ship in. Confirmed against
   a real Quanta BKL boardview (2701 parts / 11438 pins / 1986 nets).

2. **FZ-xor** (sibling module `fz.py` keeps that path). 16-byte
   sliding-window XOR cipher seeded by an ASUS-shipped 44×32-bit
   key. Used by the original ASUS PCBRepairTool. Without the key,
   the file cannot be decrypted.

Layer convention (verified by inspecting parts on the Quanta board):
  SYM_MIRROR == "YES" → bottom-layer (mirrored to back)
  SYM_MIRROR == "NO"  → top-layer
  Other       → top-layer (defensive default)

Pin coordinates are floats (typical 2-decimal precision in mils).
We round to nearest int because `Point.x/y` are int mils per the
OBV convention.

Written from scratch by inspecting the decompressed text of a real
file. No code copied from any external codebase.
"""

from __future__ import annotations

import zlib

from api.board.model import Board, Layer, Nail, Net, Part, Pin, Point
from api.board.parser._ascii_boardview import GROUND_RE, POWER_RE
from api.board.parser._pad_shape_inference import infer_pad_shape
from api.board.parser.base import InvalidBoardFile, MalformedHeaderError

_ZLIB_MAGIC_FAST = b"\x78\x9c"  # default compression
_ZLIB_MAGIC_BEST = b"\x78\xda"  # best compression
_ZLIB_MAGIC_NONE = b"\x78\x01"  # store
_ZLIB_MAGICS = (_ZLIB_MAGIC_FAST, _ZLIB_MAGIC_BEST, _ZLIB_MAGIC_NONE)

# `Board` coordinates are mils per the OBV convention. Some `.fz` files
# (typically the AMD/ASUS graphics-card dumps) ship in millimeters and
# announce the choice with a top-level `UNIT:millimeters` directive.
# Without scaling, every coordinate is 25× too small and the viewer
# fits the board to a postage stamp.
_MILS_PER_MM = 39.3700787
_MILS_PER_INCH = 1000.0


def looks_like_fz_zlib(raw: bytes) -> bool:
    """True iff `raw` looks like the zlib-flavoured .fz container.

    The first 4 bytes carry the decompressed size as LE int32, then
    the zlib stream begins with one of the three common magic words.
    Any 78 0x?? byte at offset 4 with a valid second byte (low 4 bits
    of byte 0 must be 8 = "deflate" method, etc.) is a strong signal.
    """
    if len(raw) < 8:
        return False
    return raw[4:6] in _ZLIB_MAGICS


def parse_fz_zlib(
    raw: bytes, *, file_hash: str, board_id: str, source_format: str = "fz"
) -> Board:
    """Decode + parse one zlib-flavoured `.fz` payload."""
    if not looks_like_fz_zlib(raw):
        raise InvalidBoardFile("fz-zlib: missing zlib magic at offset 4")
    try:
        text, bom_text = _decompress_streams(raw)
    except zlib.error as exc:
        raise InvalidBoardFile(f"fz-zlib: decompression failed ({exc})") from exc

    sections = _split_sections(text)
    parts_section = _pick_section(sections, "REFDES")
    pins_section = _pick_section(sections, "NET_NAME")
    vias_section = _pick_section(sections, "TESTVIA")
    scale = _detect_unit_scale(text)

    if parts_section is None or pins_section is None:
        raise MalformedHeaderError("fz-zlib: missing REFDES or NET_NAME section")

    parts, pin_lookup = _build_parts(parts_section)
    # Patch parts with BOM descriptions before building pins so the
    # pad-shape inference can fall back to `Part.value` when the
    # SYM_NAME column duplicates the refdes (Asus DUAL 1060 style).
    if bom_text is not None:
        parts = _apply_bom_descriptions(parts, _parse_bom(bom_text))
    pins, parts = _build_pins(pins_section, parts, pin_lookup, scale=scale)
    nails = _build_nails(vias_section, scale=scale) if vias_section else []
    nets = _derive_nets(pins)
    parts = _detect_dnp_alternates(parts, pins)

    outline = _synthesize_outline(pins)

    return Board(
        board_id=board_id,
        file_hash=file_hash,
        source_format=source_format,
        # Synthesized from pin bbox — the format itself ships no outline,
        # and OpenBoardView's reference implementation does the same.
        outline=outline,
        parts=parts,
        pins=pins,
        nets=nets,
        nails=nails,
    )


# ---------------------------------------------------------------------------
# Two-stream decompression (content + BOM)
# ---------------------------------------------------------------------------


def _decompress_streams(raw: bytes) -> tuple[str, str | None]:
    """Inflate the content stream and, if present, the trailing BOM stream.

    Real `.fz` files carry two zlib streams concatenated: stream 1 is the
    pipe-delimited content (REFDES / NET_NAME / TESTVIA), stream 2 is a
    tab-separated BOM (PARTNUMBER / DESCRIPTION / QTY / LOCATION / …).
    The two streams are separated by an 8-byte header (decompressed-size
    metadata) that zlib-inflate ignores via `unused_data`.
    """
    d1 = zlib.decompressobj()
    s1 = d1.decompress(raw[4:])
    text = s1.decode("utf-8", errors="replace")
    leftover = d1.unused_data
    bom_text: str | None = None
    if leftover:
        for off in range(min(16, max(0, len(leftover) - 1))):
            if leftover[off : off + 2] in (_ZLIB_MAGIC_FAST, _ZLIB_MAGIC_BEST, _ZLIB_MAGIC_NONE):
                try:
                    bom_text = zlib.decompress(leftover[off:]).decode(
                        "utf-8", errors="replace"
                    )
                except zlib.error:
                    bom_text = None
                break
    return text, bom_text


# ---------------------------------------------------------------------------
# BOM (descr) — partno, description, qty, refdes locations
# ---------------------------------------------------------------------------


def _parse_bom(text: str) -> dict[str, str]:
    """Map every refdes listed in the BOM to its DESCRIPTION column.

    Layout: line 1 is a board-model banner, line 2 is the column header,
    and every following row is `PARTNUMBER \\t DESCRIPTION \\t QTY \\t
    REFDES_LIST \\t PARTNUMBER2`. REFDES_LIST is space-separated. Rows
    starting with a literal `s` are flagged as secondary by the producer
    and carry no useful data — skip them, matching OpenBoardView.
    """
    out: dict[str, str] = {}
    lines = text.splitlines()
    if len(lines) < 3:
        return out
    for line in lines[2:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("s"):
            continue
        cols = stripped.split("\t")
        if len(cols) < 4:
            continue
        description = cols[1].strip()
        if not description:
            continue
        for refdes in cols[3].split():
            if refdes:
                out[refdes] = description
    return out


def _apply_bom_descriptions(parts: list[Part], bom: dict[str, str]) -> list[Part]:
    """Patch each `Part.value` with the BOM description when available."""
    if not bom:
        return parts
    return [
        part.model_copy(update={"value": bom[part.refdes]})
        if part.refdes in bom
        else part
        for part in parts
    ]


# ---------------------------------------------------------------------------
# Synthetic outline (the format ships none — OBV does the same)
# ---------------------------------------------------------------------------

_OUTLINE_MARGIN_MILS = 100.0

# Spatial-overlap thresholds for DFM-alternate (DNP) detection. Two parts
# are considered alternates of each other if their first pin centres land
# within `_DNP_OVERLAP_MILS` on both axes. 100 mils ≈ 2.5 mm — wide
# enough to absorb the small pin-grid offsets ASUS uses between its
# `PGCEx` 8x9.7 footprint and the smaller `PGCEx_alt` 7x8 footprint at
# the same physical seat.
_DNP_OVERLAP_MILS = 100.0
_DNP_BUCKET_MILS = 200.0


def _detect_dnp_alternates(parts: list[Part], pins: list[Pin]) -> list[Part]:
    """Tag parts that share their physical seat with a placed sibling.

    `.fz` ASUS dumps encode DFM alternates: two refdes at the same
    pin-cluster, only one of which appears in the BOM (the populated
    one). The unpopulated one becomes a "ghost" — the parser flags it
    `is_dnp=True` and attaches its refdes to the placed sibling's
    `dnp_alternates` so the viewer can surface the relationship in
    its info panel.
    """
    placed: dict[tuple[int, int], list[int]] = {}
    for i, part in enumerate(parts):
        if part.value is not None and part.pin_refs:
            first = pins[part.pin_refs[0]]
            key = (
                int(first.pos.x // _DNP_BUCKET_MILS),
                int(first.pos.y // _DNP_BUCKET_MILS),
            )
            placed.setdefault(key, []).append(i)

    ghost_to_real: dict[int, int] = {}
    for gi, part in enumerate(parts):
        if part.value is not None or not part.pin_refs:
            continue
        first = pins[part.pin_refs[0]]
        gx = first.pos.x // _DNP_BUCKET_MILS
        gy = first.pos.y // _DNP_BUCKET_MILS
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for ri in placed.get((int(gx + dx), int(gy + dy)), []):
                    if ri == gi:
                        continue
                    rfirst = pins[parts[ri].pin_refs[0]]
                    if (
                        abs(rfirst.pos.x - first.pos.x) < _DNP_OVERLAP_MILS
                        and abs(rfirst.pos.y - first.pos.y) < _DNP_OVERLAP_MILS
                    ):
                        ghost_to_real[gi] = ri
                        break
                if gi in ghost_to_real:
                    break
            if gi in ghost_to_real:
                break

    real_alternates: dict[int, list[str]] = {}
    for gi, ri in ghost_to_real.items():
        real_alternates.setdefault(ri, []).append(parts[gi].refdes)

    out: list[Part] = []
    for i, part in enumerate(parts):
        if i in ghost_to_real:
            out.append(part.model_copy(update={"is_dnp": True}))
        elif i in real_alternates:
            out.append(part.model_copy(update={"dnp_alternates": real_alternates[i]}))
        else:
            out.append(part)
    return out


def _synthesize_outline(pins: list[Pin]) -> list[Point]:
    """Return a closed-polygon bbox around all pins with a margin so the
    Three.js viewer has a board chrome to draw. Empty when no pins exist."""
    if not pins:
        return []
    xs = [p.pos.x for p in pins]
    ys = [p.pos.y for p in pins]
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


# ---------------------------------------------------------------------------
# Section walker
# ---------------------------------------------------------------------------


def _detect_unit_scale(text: str) -> float:
    """Return the multiplier needed to bring source coordinates into mils.

    Looks for a top-level `UNIT:<name>` directive. Recognised names:
    `millimeters` / `mm` (×39.37), `inches` (×1000), `mils` / unset
    (×1, default). Any unrecognised value falls back to mils with no
    scaling so we don't silently corrupt the board geometry.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line.upper().startswith("UNIT:"):
            continue
        unit = line.split(":", 1)[1].strip().lower()
        if unit in ("millimeters", "millimetres", "mm"):
            return _MILS_PER_MM
        if unit == "inches":
            return _MILS_PER_INCH
        return 1.0
    return 1.0


def _split_sections(text: str) -> dict[str, dict]:
    """Group rows by the most recent `A!` schema row.

    Each section is keyed by the first column of its schema (e.g.
    `REFDES`, `NET_NAME`, `TESTVIA`). Returns a dict
    `{name: {"schema": [col, ...], "rows": [[val, ...], ...]}}`.
    """
    sections: dict[str, dict] = {}
    current: dict | None = None
    for raw in text.splitlines():
        if not raw:
            continue
        if raw.startswith("A!"):
            cols = raw[2:].rstrip("!").split("!")
            if not cols:
                continue
            name = cols[0]
            current = {"schema": cols, "rows": []}
            sections[name] = current
        elif raw.startswith("S!") and current is not None:
            vals = raw[2:].rstrip("!").split("!")
            current["rows"].append(vals)
    return sections


def _pick_section(sections: dict[str, dict], name: str) -> list[list[str]] | None:
    sec = sections.get(name)
    return sec["rows"] if sec else None


# ---------------------------------------------------------------------------
# Parts (REFDES section)
# ---------------------------------------------------------------------------


def _layer_from_mirror(mirror: str) -> Layer:
    """`SYM_MIRROR == "YES"` → BOTTOM; otherwise TOP."""
    return Layer.BOTTOM if mirror.strip().upper() == "YES" else Layer.TOP


def _safe_float(s: str) -> float:
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _safe_int(s: str, default: int = 0) -> int:
    try:
        return int(s)
    except (ValueError, TypeError):
        try:
            return int(float(s))
        except (ValueError, TypeError):
            return default


def _build_parts(
    rows: list[list[str]],
) -> tuple[list[Part], dict[str, int]]:
    """Build `Part` objects with placeholder bbox / pin_refs.

    Returns (parts, lookup) where lookup maps refdes → 0-based index
    into the parts list, used by the pins-section walker.
    """
    parts: list[Part] = []
    lookup: dict[str, int] = {}
    for row in rows:
        if len(row) < 5:
            # Tolerate short rows — fill missing fields conservatively.
            row = row + [""] * (5 - len(row))
        refdes, _ins_code, sym_name, mirror, rotate = row[:5]
        if not refdes or refdes in lookup:
            continue
        rotation_deg = _safe_float(rotate) % 360.0
        parts.append(
            Part(
                refdes=refdes,
                layer=_layer_from_mirror(mirror),
                # FZ-zlib doesn't expose a TH/SMD flag; default to SMD which
                # is the dominant case on modern boards. Through-hole parts
                # render the same way in the boardview canvas.
                is_smd=True,
                bbox=(Point(x=0, y=0), Point(x=0, y=0)),
                pin_refs=[],
                footprint=sym_name or None,
                rotation_deg=rotation_deg,
            )
        )
        lookup[refdes] = len(parts) - 1
    return parts, lookup


# ---------------------------------------------------------------------------
# Pins (NET_NAME section)
# ---------------------------------------------------------------------------


def _build_pins(
    rows: list[list[str]],
    parts: list[Part],
    lookup: dict[str, int],
    *,
    scale: float = 1.0,
) -> tuple[list[Pin], list[Part]]:
    """Resolve each pin row to its owning part, build `Pin` objects, and
    patch each `Part` with `pin_refs` + bbox computed from pin positions.

    `PIN_NUMBER` is normally 0 in the wild — the meaningful pin ID is
    `PIN_NAME` (string, can be alphanumeric). We store an integer
    `pin.index` per Pydantic; when `PIN_NAME` is non-numeric we fall
    back to a sequential 1-based counter within the owning part.
    """
    pins: list[Pin] = []
    refs_by_part: list[list[int]] = [[] for _ in parts]
    counters: list[int] = [0] * len(parts)
    extents: list[tuple[float, float, float, float]] = [
        (float("inf"), float("inf"), float("-inf"), float("-inf")) for _ in parts
    ]

    for row in rows:
        if len(row) < 6:
            continue
        net_name = row[0].strip()
        refdes = row[1].strip()
        # row[2] == PIN_NUMBER (often 0 — kept for compatibility with the
        # source format but not used downstream)
        pin_name = row[3].strip()
        x = _safe_float(row[4]) * scale
        y = _safe_float(row[5]) * scale
        # row[6] == TEST_POINT (often empty), row[7] == RADIUS (pad radius mils)
        radius = (_safe_float(row[7]) if len(row) >= 8 else 0.0) * scale

        if refdes not in lookup:
            # Orphan pin — skip. We refuse to fabricate parts.
            continue
        k = lookup[refdes]
        owner = parts[k]
        counters[k] += 1
        # Try numeric PIN_NAME first, fall back to monotonic counter.
        pin_idx = _safe_int(pin_name, default=counters[k])
        if pin_idx <= 0:
            pin_idx = counters[k]

        ix = int(round(x))
        iy = int(round(y))
        pin_kwargs: dict = {}
        if radius > 0.0:
            # The FZ format ships a per-pin pad RADIUS (mils) but no
            # shape token — every pad would default to a circle of
            # diameter 2r. We override the shape via package-aware
            # inference on the parent part's footprint when it
            # confidently maps to a rectangular pad (chip passive,
            # leaded SMD, SMD inductor); BGA / mounting / test-point
            # footprints keep "circle". Unknown footprints stay circle
            # (the radius-derived default).
            diameter = 2.0 * radius
            # Try the SYM_NAME footprint first; if it's empty or just
            # echoes the refdes (no package keyword), fall back to the
            # BOM description (`Part.value`). Some `.fz` dialects ship
            # all parts with `SYM_NAME == REFDES` and the real package
            # info only lives in the BOM stream.
            inferred = infer_pad_shape(owner.footprint) or infer_pad_shape(owner.value)
            pin_kwargs["pad_shape"] = inferred or "circle"
            pin_kwargs["pad_size"] = (diameter, diameter)
        pins.append(
            Pin(
                part_refdes=refdes,
                index=pin_idx,
                pos=Point(x=ix, y=iy),
                net=(net_name or None),
                probe=None,
                layer=owner.layer,
                **pin_kwargs,
            )
        )
        refs_by_part[k].append(len(pins) - 1)
        x0, y0, x1, y1 = extents[k]
        extents[k] = (min(x0, ix), min(y0, iy), max(x1, ix), max(y1, iy))

    # Patch parts with pin_refs + computed bbox.
    patched: list[Part] = []
    for k, part in enumerate(parts):
        refs = refs_by_part[k]
        if refs:
            x0, y0, x1, y1 = extents[k]
            bbox = (Point(x=int(x0), y=int(y0)), Point(x=int(x1), y=int(y1)))
        else:
            bbox = part.bbox
        patched.append(part.model_copy(update={"pin_refs": refs, "bbox": bbox}))
    return pins, patched


# ---------------------------------------------------------------------------
# Nails (TESTVIA section)
# ---------------------------------------------------------------------------


def _build_nails(rows: list[list[str]], *, scale: float = 1.0) -> list[Nail]:
    """TESTVIA schema: `TESTVIA NET_NAME REFDES PIN_NUMBER PIN_NAME VIA_X VIA_Y TEST_POINT RADIUS`."""
    out: list[Nail] = []
    for i, row in enumerate(rows, start=1):
        if len(row) < 7:
            continue
        net_name = row[1].strip()
        x = _safe_float(row[5]) * scale
        y = _safe_float(row[6]) * scale
        out.append(
            Nail(
                probe=i,
                pos=Point(x=int(round(x)), y=int(round(y))),
                # FZ-zlib doesn't tag via side; default to TOP. The board
                # consumer can refine later if needed.
                layer=Layer.TOP,
                net=net_name,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Net derivation
# ---------------------------------------------------------------------------


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
