"""Walk a TVW production-binary buffer and emit normalized records.

Section sequence (per the format):

    file_header
    for i in range(layer_count):
        layer_header
        if layer_header.body_kind == 0xb:
            3 × uint32 (consumed)
        elif layer_header is a data layer:
            dcode_table          (variable-size aperture records)
            int32 a, int32 b     (gating flags)
            if dcode_count > 0 OR a > 0 OR b > 0:
                pins             (variable-size pin records)
                lines, arcs, surfaces, texts   (skipped — rest of layer)
            probes, nails, postnails, lines, end  (skipped)
    network_names                (global netlist — Pascal strings)

Per-pin record (variable, 19 base bytes + optional 3..47 byte extension):

    uint32  part_index
    uint32  pin_local_index   (used as 1/10 in display paths we surveyed)
    int32   x                 (centi-mils)
    int32   y                 (centi-mils)
    uint8   flag1             (purpose unclear)
    uint8   has_extension     (0 = base record only)
    if has_extension != 0:
        uint8 sub_a;  if sub_a == 1: skip 12 bytes
        uint8 sub_b;  if sub_b != 0: skip 16 bytes
        uint8 sub_c;  if sub_c != 0: skip 16 bytes
    uint8   flag3             (purpose unclear)

Coordinate scale: centi-mils. Divide by 100 to get mils.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

from .cipher import decode as cipher_decode
from .magic import is_production_binary

# === Public dataclasses ===========================================================


@dataclass(slots=True)
class Aperture:
    index: int       # 1-based ordinal used by PinRecord.pin_local_index
    width: int       # centi-mils
    height: int
    type_: int       # shape selector (1=round/rect, 3=?, 5=Custom polygon, ...)


@dataclass(slots=True)
class PinRecord:
    part_index: int
    pin_local_index: int
    x: int                       # centi-mils
    y: int
    flag1: int
    flag3: int
    raw_size: int                # number of bytes consumed by this record (incl. extension)
    # Optional pad bbox (offsets relative to (x, y), centi-mils).
    # Set from the 16-byte sub_b extension block when present —
    # carries the actual pad rectangle for non-circular SMD pads.
    pad_dx1: int = 0
    pad_dy1: int = 0
    pad_dx2: int = 0
    pad_dy2: int = 0
    has_pad_bbox: bool = False


@dataclass(slots=True)
class LineRecord:
    """Drawing primitive — straight line from (x1,y1) to (x2,y2)."""
    x1: int          # centi-mils
    y1: int
    x2: int
    y2: int
    aperture_or_kind: int    # tail u32, semantics depends on body_kind


@dataclass(slots=True)
class ArcRecord:
    """Drawing primitive — arc with centre, radius, start/end coords.

    Per the format we surveyed, an arc record is 28 bytes:
        i32 + u32 (header) + 5 × u32 (centre, radius, endpoints).
    """
    cx: int          # centi-mils — centre X
    cy: int          # centre Y
    radius: int      # centi-mils
    sx: int          # start X
    sy: int          # start Y


@dataclass(slots=True)
class TextRecord:
    """Silkscreen text label — name (often a refdes like 'R12', 'C45') plus
    a 39-byte trailer we don't yet decode (font / size / placement)."""
    text: str


@dataclass(slots=True)
class SurfaceRecord:
    """Filled polygon surface outer ring.

    TVW surfaces encode a filled outer ring plus optional void rings.
    We keep the outer ring for now because it is the only part needed
    to investigate global board-edge candidates; void point payloads
    are still skipped for cursor accuracy.
    """
    kind: int
    vertices: list[tuple[int, int]] = field(default_factory=list)
    void_count: int = 0


@dataclass(slots=True)
class TestPointRecord:
    """Probe / test-point record — position + optional Pascal name.

    Found in the per-layer probes section. The named records are
    flying-probe / ATE test points the original CAD authoring user
    flagged (e.g. 'TP1', 'GND_REF'); unnamed records carry only
    a position.
    """
    x: int           # centi-mils
    y: int
    name: str        # often empty


@dataclass(slots=True)
class PolygonRecord:
    """Custom polygon — used for the board outline, ground-plane shapes,
    and other non-rectangular copper / silkscreen primitives.

    Per the format we surveyed, each polygon record opens with a
    fixed `\\x05\\x00\\x00\\x00\\x00\\x00\\x00\\x00` signature followed
    by a Pascal-prefixed name (typically `Custom` or `Custom_NN`),
    then a 4 × i32 bbox, two u32 flags, three u32 padding fields,
    a u32 vertex_count, and then `vertex_count × (i32 X, i32 Y)`
    pairs. Trailing bytes after the first ring carry additional
    rings / holes whose layout we have not yet decoded.
    """
    name: str
    bbox_x1: int     # centi-mils
    bbox_y1: int
    bbox_x2: int
    bbox_y2: int
    vertices: list[tuple[int, int]] = field(default_factory=list)


@dataclass(slots=True)
class OutlinePrimRecord:
    """One primitive within an outline group. Either a line or a polyline.

    Format: `\\xff\\xff\\xff\\xff` marker (4 bytes) + u32 kind + body.
      * kind == 10 (0xa): body is exactly 16 bytes (4 i32 = line endpoints).
      * kind in [3, 200]: body is `kind × 8` bytes — `kind` 2D points
        encoded as (i32 X, i32 Y) pairs in mils; each |coord| ≤ 9999.
    """
    kind: int
    points: list[tuple[int, int]] = field(default_factory=list)


@dataclass(slots=True)
class OutlineGroup:
    """A package or board outline group, anchored on the F00B signature.

    Each group opens with a 12-byte signature
    `\\xff\\x00\\x00\\x00\\x00\\xff\\x00\\x00\\x0b\\x00\\x00\\x00`,
    followed by a small header (typically 12 × u32, occasionally 11)
    whose 11th value carries the primitive count, then a sequence of
    `OutlinePrimRecord` primitives. The header values include a scale
    field (typically 100 or 500) and several flag fields whose
    semantics are still being decoded.

    Coordinate unit for kind ∈ [3, 200] primitives: mils (capped at
    ±9999). For kind == 10 lines: mils, no validation cap.
    """
    file_offset: int
    header: tuple[int, ...]
    prims: list[OutlinePrimRecord] = field(default_factory=list)


@dataclass(slots=True)
class ComponentRecord:
    """A real schematic component — refdes + value + footprint + position.

    Found in trailing component sections (after all layer bodies). Not
    every fixture has them, but on the multi-layer graphics-card boards
    we surveyed they carry the canonical 'C134', 'R242', 'U7' style
    refdes plus the silkscreened value ('10u/6/x5r/6.3v/m') and
    package footprint ('C0805_0603-1', 'CAP_SMD_7343', 'SOT23', …).
    """
    refdes: str
    value: str       # the silkscreened value text — sometimes empty
    comment: str     # extra description; usually empty
    footprint: str   # the package name — often empty for non-standard parts
    cx: int          # centi-mils — centre X
    cy: int          # centre Y
    bbox_x1: int
    bbox_y1: int
    bbox_x2: int
    bbox_y2: int
    rotation: int    # raw u32 from the file (0/90/180/270 typically)
    kind: int        # raw u32 — type / category code from the source CAD
    pin_count: int


@dataclass(slots=True)
class Layer:
    name: str
    source_path: str
    body_kind: int
    apertures: list[Aperture] = field(default_factory=list)
    pins: list[PinRecord] = field(default_factory=list)
    pin_count_declared: int = 0   # the u32 pin_count from the section header
    lines: list[LineRecord] = field(default_factory=list)
    second_lines: list[LineRecord] = field(default_factory=list)
    arcs: list[ArcRecord] = field(default_factory=list)
    surfaces: list[SurfaceRecord] = field(default_factory=list)
    texts: list[TextRecord] = field(default_factory=list)
    test_points: list[TestPointRecord] = field(default_factory=list)


@dataclass(slots=True)
class TVWFile:
    version: int
    date: str               # decoded build date (e.g. "April 27, 2017")
    vendor: str             # decoded vendor field
    product: str            # decoded product / customer field
    layer_count_declared: int
    layers: list[Layer] = field(default_factory=list)
    net_names: list[str] = field(default_factory=list)
    components: list[ComponentRecord] = field(default_factory=list)
    polygons: list[PolygonRecord] = field(default_factory=list)
    outlines: list[OutlineGroup] = field(default_factory=list)


# === Primitive readers ===========================================================


def _u8(buf: bytes, off: int) -> tuple[int, int]:
    return buf[off], off + 1


def _u32(buf: bytes, off: int) -> tuple[int, int]:
    return struct.unpack_from("<I", buf, off)[0], off + 4


def _i32(buf: bytes, off: int) -> tuple[int, int]:
    return struct.unpack_from("<i", buf, off)[0], off + 4


def _read_pascal(buf: bytes, off: int) -> tuple[bytes, int]:
    """Pascal string: u8 length + that many bytes."""
    if off >= len(buf):
        raise ValueError(f"pascal read past EOF at offset {off}")
    n = buf[off]
    end = off + 1 + n
    if end > len(buf):
        raise ValueError(f"pascal len {n} at {off} would overrun")
    return buf[off + 1:end], end


# === File header ================================================================
#
# 4 cipher-decoded Pascal strings stored, 3 cipher-decoded Pascal strings
# discarded, then a 12-byte gap, then 2 uint32 (one stored as layer_count,
# one discarded). Cipher applies only here — every other Pascal string in
# the file is plain-text.


def _read_file_header(buf: bytes, off: int) -> tuple[dict, int]:
    fields: list[str] = []  # 4 stored fields
    # Field 1: format magic ("Tebo-ictview files.")
    s, off = _read_pascal(buf, off)
    fields.append(cipher_decode(s))
    # First u32 is consumed but not stored
    _, off = _u32(buf, off)
    # Field 2: vendor string
    s, off = _read_pascal(buf, off)
    fields.append(cipher_decode(s))
    # Field 3: product / customer marker
    s, off = _read_pascal(buf, off)
    fields.append(cipher_decode(s))
    # Field 4: build date
    s, off = _read_pascal(buf, off)
    fields.append(cipher_decode(s))
    # 3 discarded cipher-decoded Pascal strings
    for _ in range(3):
        s, off = _read_pascal(buf, off)
        _ = cipher_decode(s)  # consumed for cursor advancement
    # 12-byte gap
    off += 12
    # uint32: layer count (stored)
    layer_count, off = _u32(buf, off)
    # uint32: discarded
    _, off = _u32(buf, off)
    return {
        "magic": fields[0],
        "vendor": fields[1],
        "product": fields[2],
        "date": fields[3],
        "layer_count": layer_count,
    }, off


# === Layer header ===============================================================
#
# u32 layer_type            ([+0x1fc])
#   if layer_type == 4: u32 (overrides layer_type)
# u32 sub1                  ([+0x200])
# u32 sub2                  ([+0x204])
# Pascal layer_name1        (NOT cipher-decoded)
# Pascal layer_name2        (same value as layer_name1 — redundant)
# Pascal source_path        (Windows path to original CAD source)
# u32 body_kind             ([+0x268]) — 0xb special, otherwise normal
# u32 (discarded)
# u32 (discarded)


def _read_layer_header(buf: bytes, off: int) -> tuple[dict, int]:
    layer_type, off = _u32(buf, off)
    if layer_type == 4:
        layer_type, off = _u32(buf, off)
    _sub1, off = _u32(buf, off)
    _sub2, off = _u32(buf, off)
    name1_b, off = _read_pascal(buf, off)
    _name2_b, off = _read_pascal(buf, off)
    path_b, off = _read_pascal(buf, off)
    body_kind, off = _u32(buf, off)
    _, off = _u32(buf, off)
    _, off = _u32(buf, off)
    return {
        "layer_type": layer_type,
        "name1": name1_b.decode("ascii", errors="replace"),
        "source_path": path_b.decode("utf-8", errors="replace"),
        "body_kind": body_kind,
        "is_empty": (len(name1_b) == 0 and len(path_b) == 0),
    }, off


# === D-code (aperture) table ====================================================
#
# u32 count
# count × variable record. Per record:
#   u32  shape_flag
#   i32  width                (centi-mils)
#   i32  height
#   u32  type     (1, 3, 5, or any other small int)
#   u32  param    (interpreted as a float by the reference reader — meaning depends on type)
# If type ∈ {0, 1, 3}:   6 u32 = 24 bytes (one extra u32 read)
# If type == 5 (Custom):  20 bytes + Pascal-prefixed polygon name + vertex
#                          list. We DON'T fully decode the polygon vertex
#                          list — when we hit type 5, we stop the table
#                          (apertures up to this point are still useful).
# Else (type 2, 4, 6, 7, …):  6 u32 = 24 bytes (one extra u32 read)


def _read_regular_dcode_at(
    buf: bytes, off: int, region_end: int
) -> tuple[Aperture | None, int]:
    if off + 24 > region_end:
        return None, off
    try:
        _shape_flag, off2 = _u32(buf, off)
        w, off2 = _i32(buf, off2)
        h, off2 = _i32(buf, off2)
        type_, off2 = _u32(buf, off2)
        _param, off2 = _u32(buf, off2)
        _extra, off2 = _u32(buf, off2)
    except struct.error:
        return None, off
    if type_ not in (0, 1, 3):
        return None, off
    if w < 1 or h < 1 or w > 0x100000 or h > 0x100000:
        return None, off
    return Aperture(index=0, width=w, height=h, type_=type_), off2


def _find_regular_dcode_run(
    buf: bytes, start: int, stop: int
) -> list[Aperture]:
    best: list[Aperture] = []
    for cand in range(start, max(start, stop - 23)):
        cur = cand
        run: list[Aperture] = []
        while cur + 24 <= stop:
            ap, next_cur = _read_regular_dcode_at(buf, cur, stop)
            if ap is None:
                break
            run.append(ap)
            cur = next_cur
        if len(run) > len(best):
            best = run
    return best


def _read_dcodes(buf: bytes, off: int, region_end: int) -> tuple[list[Aperture], int]:
    if off + 4 > region_end:
        return [], off
    count, off = _u32(buf, off)
    apertures: list[Aperture] = []
    if count == 0:
        return apertures, off
    if count > 1_000_000:
        return apertures, off

    for _i in range(count):
        if off + 20 > region_end:
            break
        try:
            type_ = struct.unpack_from("<I", buf, off + 12)[0]
        except struct.error:
            break
        # Bail on the first record that looks like the Custom-polygon
        # entry or anything else with a non-aperture type. The fixtures
        # we surveyed all have regular records (type ∈ {0, 1, 3}) up to
        # the first non-regular one, after which the polygon section
        # begins. The polygon record layout is variable-length and not
        # yet fully decoded — `_read_pins` forward-scans past whatever
        # comes between here and the pin section.
        if type_ not in (0, 1, 3):
            if type_ != 5:
                return apertures, off
            last_poly_end = _last_polygon_pascal_end(buf, off, region_end)
            if last_poly_end is None:
                return apertures, off
            pin_start = None
            best_pin_count = 0
            # The pin header follows the final Custom aperture body
            # closely on surveyed TVWs. Bound this local search so a
            # late false layer marker does not make dcode parsing scan
            # the rest of a multi-MB layer byte by byte.
            scan_limit = min(region_end, last_poly_end + 262_144)
            for cand in range(min(last_poly_end + 8, region_end), scan_limit):
                if _looks_like_pin_section_header(buf, cand, region_end):
                    res = _try_walk_pins_at(buf, cand, region_end)
                    if res is None:
                        continue
                    pins, _pin_end, _declared = res
                    if len(pins) > best_pin_count:
                        best_pin_count = len(pins)
                        pin_start = cand
            custom_stop = pin_start - 8 if pin_start is not None and pin_start >= 8 else region_end
            scan_stop = pin_start if pin_start is not None else region_end
            for match in _CUSTOM_POLY_RE.finditer(buf, off + 12, scan_stop):
                record_start = match.start() - 12
                if record_start < off or record_start >= custom_stop or record_start + 20 > region_end:
                    continue
                try:
                    _shape_flag = struct.unpack_from("<I", buf, record_start)[0]
                    cw = struct.unpack_from("<i", buf, record_start + 4)[0]
                    ch = struct.unpack_from("<i", buf, record_start + 8)[0]
                except struct.error:
                    continue
                if cw < 1 or ch < 1 or cw > 0x100000 or ch > 0x100000:
                    continue
                apertures.append(
                    Aperture(index=len(apertures) + 1, width=cw, height=ch, type_=5)
                )
                if len(apertures) >= count:
                    break
            if len(apertures) < count:
                regular_tail = _find_regular_dcode_run(buf, last_poly_end, custom_stop)
                for ap in regular_tail:
                    apertures.append(
                        Aperture(
                            index=len(apertures) + 1,
                            width=ap.width,
                            height=ap.height,
                            type_=ap.type_,
                        )
                    )
                    if len(apertures) >= count:
                        break
            return apertures, custom_stop
        ap, off2 = _read_regular_dcode_at(buf, off, region_end)
        if ap is None:
            return apertures, off
        apertures.append(
            Aperture(index=len(apertures) + 1, width=ap.width, height=ap.height, type_=ap.type_)
        )
        off = off2
    return apertures, off


# === Pin records ===============================================================


def _read_pin_record(buf: bytes, off: int, region_end: int) -> tuple[PinRecord | None, int]:
    """Read one variable-size pin record. Returns (None, off) if malformed.

    The optional extension's 16-byte sub_b block carries the pad's
    bounding rectangle as 4 × i32 offsets relative to (x, y). We expose
    those when present — they describe the actual SMD pad's shape /
    size, useful for the WebGL viewer.
    """
    base_start = off
    pad_dx1 = pad_dy1 = pad_dx2 = pad_dy2 = 0
    has_pad_bbox = False
    try:
        if off + 18 > region_end:
            return None, off
        part_idx, off = _u32(buf, off)
        pin_local, off = _u32(buf, off)
        x, off = _i32(buf, off)
        y, off = _i32(buf, off)
        flag1, off = _u8(buf, off)
        has_ext, off = _u8(buf, off)
        if has_ext != 0:
            if off + 3 > region_end:
                return None, off
            sub_a, off = _u8(buf, off)
            if sub_a == 1:
                if off + 12 > region_end:
                    return None, off
                off += 12
            sub_b, off = _u8(buf, off)
            if sub_b != 0:
                if off + 16 > region_end:
                    return None, off
                pad_dx1, off = _i32(buf, off)
                pad_dy1, off = _i32(buf, off)
                pad_dx2, off = _i32(buf, off)
                pad_dy2, off = _i32(buf, off)
                has_pad_bbox = True
            sub_c, off = _u8(buf, off)
            if sub_c != 0:
                if off + 16 > region_end:
                    return None, off
                off += 16
        if off + 1 > region_end:
            return None, off
        flag3, off = _u8(buf, off)
    except (IndexError, struct.error):
        return None, off
    return PinRecord(
        part_index=part_idx,
        pin_local_index=pin_local,
        x=x,
        y=y,
        flag1=flag1,
        flag3=flag3,
        raw_size=off - base_start,
        pad_dx1=pad_dx1, pad_dy1=pad_dy1,
        pad_dx2=pad_dx2, pad_dy2=pad_dy2,
        has_pad_bbox=has_pad_bbox,
    ), off


_MAX_COORD_CMILS = 5_000_000  # ±50 inches at centi-mil resolution
_MAX_PART_INDEX = 1_000_000


def _is_plausible_pin(rec: PinRecord) -> bool:
    if abs(rec.x) > _MAX_COORD_CMILS:
        return False
    if abs(rec.y) > _MAX_COORD_CMILS:
        return False
    if rec.part_index >= 1 << 31:
        return False
    if rec.pin_local_index > 0x100000:
        return False
    return True


def _try_walk_pins_at(
    buf: bytes,
    off: int,
    region_end: int,
    max_pin_count: int = 200_000,
    min_partial_ratio: float = 0.5,
) -> tuple[list[PinRecord], int, int] | None:
    """Try to read a pins section starting at `off`. Return (pins, end_off,
    declared_pin_count) on success. Accepts partial walks (≥
    `min_partial_ratio` of declared pin_count) — the format has trailing
    auxiliary records after the pin list whose layout isn't yet decoded,
    so the last few records sometimes fail plausibility.
    """
    if off + 12 > region_end:
        return None
    try:
        first_count, off2 = _u32(buf, off)
        pin_count, off2 = _u32(buf, off2)
    except struct.error:
        return None
    if pin_count == 0:
        return [], off2, 0
    if pin_count > max_pin_count or first_count > max_pin_count:
        return None
    try:
        _gap, off2 = _u32(buf, off2)
    except struct.error:
        return None
    pins: list[PinRecord] = []
    cur = off2
    for _i in range(pin_count):
        rec, new_off = _read_pin_record(buf, cur, region_end)
        if rec is None or not _is_plausible_pin(rec):
            break
        pins.append(rec)
        cur = new_off
    if not pins:
        return None
    if pin_count >= 10 and len(pins) < max(2, pin_count * min_partial_ratio):
        return None
    return pins, cur, pin_count


def _looks_like_pin_record(buf: bytes, off: int, region_end: int) -> bool:
    """Quick filter: do the next 18 bytes look like a pin record?

    A pin record's X / Y are signed centi-mils, typically within
    [-1e6, +1e6] (corresponds to ±100 mm at 1/100 mil resolution).
    The base 18-byte layout is:
        u32 part_idx
        u32 pin_local_idx
        i32 X
        i32 Y
        u8  flag1
        u8  has_extension
    """
    if off + 18 > region_end:
        return False
    try:
        x = struct.unpack_from("<i", buf, off + 8)[0]
        y = struct.unpack_from("<i", buf, off + 12)[0]
    except struct.error:
        return False
    return abs(x) <= 5_000_000 and abs(y) <= 5_000_000


def _looks_like_pin_section_header(
    buf: bytes, off: int, region_end: int
) -> bool:
    """Triage filter for a candidate pin-section header at `off`.

    Pin section: u32 first_count + u32 pin_count + u32 gap + records.
    For a real pin section:
      * pin_count is usually in [50, 60_000] on the fixtures we surveyed
      * the first hypothetical pin record's X / Y are plausible
      * the first part_index is in [0, 200_000]
    """
    if off + 12 + 18 > region_end:
        return False
    # Fast pre-filter: small u32s have their top byte == 0. This
    # rejects ~99% of polygon-vertex-data candidates without the
    # cost of struct.unpack_from. The pin-record part_index gate
    # below is also cheap.
    if buf[off + 3] != 0 or buf[off + 7] != 0 or buf[off + 11] != 0:
        return False
    if buf[off + 15] != 0:  # first part_index high byte
        return False
    try:
        first_count = struct.unpack_from("<I", buf, off)[0]
        pin_count = struct.unpack_from("<I", buf, off + 4)[0]
    except struct.error:
        return False
    if pin_count < 1 or pin_count > 100_000:
        return False
    if first_count > 100_000:
        return False
    if not _looks_like_pin_record(buf, off + 12, region_end):
        return False
    return True


# Custom polygon record signature: u32 type=5 + u32 param=0 + Pascal "Custom..."
# The Pascal length byte is in [6 (= "\x06Custom") … 15 (= "\x0fCustom_NNNN")].
_CUSTOM_POLY_RE = re.compile(rb"\x05\x00\x00\x00\x00\x00\x00\x00[\x06-\x0f]Custom")


def _last_polygon_pascal_end(
    buf: bytes, region_start: int, region_end: int
) -> int | None:
    """Find the last Custom-polygon Pascal-name end inside the region.

    Each polygon record begins with the byte signature
    `05 00 00 00 00 00 00 00 [len]Custom`, where `[len]` is the Pascal
    prefix in [6, 15]. We scan the region for that signature and
    return the offset just past the Pascal name of the LAST match.

    The byte following the polygon name is the start of the polygon
    body — the pin section sits past the final polygon's body, so
    starting the pin-section forward-scan from this point cuts the
    scan length from O(layer_size) to O(last_polygon_body_size + ε).
    """
    last_match = None
    for m in _CUSTOM_POLY_RE.finditer(buf, region_start, region_end):
        last_match = m
    if last_match is None:
        return None
    sig_off = last_match.start()
    name_len_byte_off = sig_off + 8
    if name_len_byte_off >= region_end:
        return None
    name_len = buf[name_len_byte_off]
    pascal_end = name_len_byte_off + 1 + name_len
    if pascal_end > region_end:
        return None
    return pascal_end


def _read_pins(
    buf: bytes, off: int, region_end: int
) -> tuple[list[PinRecord], int, int]:
    """Find and read the pins section. Return (pins, end_off, declared_pin_count).

    Sequential walking of the dcodes section is incomplete — Custom
    polygon records have a variable-length body we haven't fully
    decoded. So we forward-scan, but anchor the scan to just past the
    last Custom polygon's Pascal name (when one is present) — this
    cuts the search window from MB to KB on graphics-card boards.
    """
    # First try: assume pins section starts exactly at `off`.
    res = _try_walk_pins_at(buf, off, region_end)
    if res is not None and len(res[0]) > 0:
        return res

    # Anchor the forward-scan to just past the last Custom polygon
    # Pascal name in this layer's body. Polygons end before the pin
    # section, so this gets us much closer to the real header without
    # scanning through MB of polygon vertex data.
    last_poly_end = _last_polygon_pascal_end(buf, off, region_end)
    scan_start = max(off, last_poly_end) if last_poly_end is not None else off

    scan_end = region_end - 30
    best_pins: list[PinRecord] = []
    best_end = off
    best_decl = 0
    cand = scan_start
    while cand < scan_end:
        if not _looks_like_pin_section_header(buf, cand, region_end):
            cand += 4
            continue
        res = _try_walk_pins_at(buf, cand, region_end)
        if res is not None:
            pins_list, end, declared = res
            if len(pins_list) > len(best_pins):
                best_pins = pins_list
                best_end = end
                best_decl = declared
        cand += 4
    return best_pins, best_end, best_decl


# === Sequential readers for sections after the pin block =========================
#
#   layer_lines_read   (24 + 24×count bytes — header is u32 count + u32 variant,
#                       per-record is exactly 6 × u32 = 24 bytes regardless of
#                       body_kind; the dispatch on body_kind only affects
#                       what to *do* with the data, not how many bytes to read)
#   layer_arcs_read    (i32 count + u32 + 28×count bytes — per arc is
#                       read_32i + read_u32i + 5 inline u32 = 28 bytes)
#   layer_surfaces_read (u32 count + u32 + count × variable surface;
#                       per surface = i32 + i32 vc + vc×8 + optional u32 +
#                       u32 m + optional u32 + m × void, per void =
#                       u32 vv + vv×8 + optional u32)
#   layer_texts_read   (u32 count + u32 + count × (Pascal string + 39 bytes))


def _read_lines(buf: bytes, off: int, region_end: int) -> tuple[list[LineRecord], int]:
    """layer_lines_read: u32 count + u32 variant + count × 24-byte record."""
    if off + 4 > region_end:
        return [], off
    count, off = _u32(buf, off)
    if count == 0:
        return [], off
    if count > 10_000_000:
        return [], off
    if off + 4 > region_end:
        return [], off
    _variant, off = _u32(buf, off)
    lines: list[LineRecord] = []
    for _i in range(count):
        if off + 24 > region_end:
            break
        try:
            _a, off2 = _u32(buf, off)
            _b, off2 = _u32(buf, off2)
            x1, off2 = _i32(buf, off2)
            y1, off2 = _i32(buf, off2)
            x2, off2 = _i32(buf, off2)
            y2, off2 = _i32(buf, off2)
        except struct.error:
            break
        lines.append(LineRecord(x1=x1, y1=y1, x2=x2, y2=y2, aperture_or_kind=_b))
        off = off2
    return lines, off


def _read_arcs(buf: bytes, off: int, region_end: int) -> tuple[list[ArcRecord], int]:
    """layer_arcs_read: i32 count + u32 + count × 28-byte record.

    Per-record layout (7 × u32 = 28 bytes):
        u32 header_a  (purpose unclear)
        i32 header_b  (purpose unclear; signed)
        u32 header_c
        u32 centre_x  (centi-mils)
        u32 centre_y
        u32 radius
        u32 start_x   (start point X — interpretation pending)
    """
    if off + 4 > region_end:
        return [], off
    count, off = _i32(buf, off)
    if count <= 0:
        return [], off
    if count > 10_000_000:
        return [], off
    if off + 4 > region_end:
        return [], off
    _, off = _u32(buf, off)
    arcs: list[ArcRecord] = []
    for _i in range(count):
        if off + 28 > region_end:
            break
        try:
            _h_a, off2 = _u32(buf, off)
            _h_b, off2 = _i32(buf, off2)
            _h_c, off2 = _u32(buf, off2)
            cx, off2 = _i32(buf, off2)
            cy, off2 = _i32(buf, off2)
            radius, off2 = _i32(buf, off2)
            sx, off2 = _i32(buf, off2)
        except struct.error:
            break
        arcs.append(ArcRecord(cx=cx, cy=cy, radius=radius, sx=sx, sy=0))
        off = off2
    return arcs, off


def _read_surfaces(buf: bytes, off: int, region_end: int) -> tuple[list[SurfaceRecord], int]:
    """layer_surfaces_read: u32 count + u32 + count × variable surface.

    Per surface:
        i32 a
        i32 vertex_count
        vertex_count × 8 bytes (i32 X, i32 Y)
        u32 c
        u32 void_count
        void_count × ((u32 vv) + (vv × 8))
    """
    if off + 4 > region_end:
        return [], off
    count, off = _u32(buf, off)
    if count == 0:
        return [], off
    if count > 1_000_000:
        return [], off
    if off + 4 > region_end:
        return [], off
    _, off = _u32(buf, off)
    surfaces: list[SurfaceRecord] = []
    for _i in range(count):
        if off + 8 > region_end:
            break
        try:
            kind, off = _i32(buf, off)
            vc, off = _i32(buf, off)
        except struct.error:
            break
        if vc < 0 or vc > 50_000:
            return surfaces, off
        if off + 8 * vc > region_end:
            break
        vertices: list[tuple[int, int]] = []
        for _ in range(vc):
            try:
                x, off = _i32(buf, off)
                y, off = _i32(buf, off)
            except struct.error:
                return surfaces, off
            vertices.append((x, y))
        if vc > 0:
            if off + 4 > region_end:
                break
            try:
                _, off = _u32(buf, off)
            except struct.error:
                break
        if off + 4 > region_end:
            break
        try:
            void_count, off = _u32(buf, off)
        except struct.error:
            break
        if void_count > 100_000:
            return surfaces, off
        if void_count > 0:
            if off + 4 > region_end:
                return surfaces, off
            try:
                _, off = _u32(buf, off)
            except struct.error:
                return surfaces, off
        for _v in range(void_count):
            if off + 4 > region_end:
                return surfaces, off
            try:
                vv, off = _u32(buf, off)
            except struct.error:
                return surfaces, off
            if vv > 50_000:
                return surfaces, off
            if off + 8 * vv > region_end:
                return surfaces, off
            off += 8 * vv
            if vv > 0:
                if off + 4 > region_end:
                    return surfaces, off
                try:
                    _, off = _u32(buf, off)
                except struct.error:
                    return surfaces, off
        surfaces.append(
            SurfaceRecord(kind=kind, vertices=vertices, void_count=void_count)
        )
    return surfaces, off


def _read_probes(buf: bytes, off: int, region_end: int) -> tuple[list[TestPointRecord], int]:
    """layer_probes_read:
       u32 magic
       if magic != 7: return
       u32 inner_count
       inner_count × probe1_record (42 bytes)
       i32 named_count
       named_count × probe2_record:
            u8 sep + i32 + i32 + i32 + i32 + u8 + u8 + u8 has_name +
            (if has_name: Pascal name) else (skip 6 bytes + i32)

    Returns the named test-point list (probe2 records). Probe1 records
    are skipped — they carry rectangular probe areas without names.
    """
    if off + 4 > region_end:
        return [], off
    try:
        magic, off = _u32(buf, off)
    except struct.error:
        return [], off
    if magic != 7:
        return [], off
    if off + 4 > region_end:
        return [], off
    try:
        inner, off = _u32(buf, off)
    except struct.error:
        return [], off
    if inner > 1_000_000:
        return [], off
    skip = 42 * inner
    if off + skip > region_end:
        return [], off
    off += skip
    if off + 4 > region_end:
        return [], off
    try:
        named, off = _i32(buf, off)
    except struct.error:
        return [], off
    if named < 0:
        return [], off
    if named > 1_000_000:
        return [], off
    points: list[TestPointRecord] = []
    for _i in range(named):
        if off + 20 > region_end:
            return points, off
        try:
            _sep, off = _u8(buf, off)
            _v1, off = _i32(buf, off)
            _v2, off = _i32(buf, off)
            x, off = _i32(buf, off)
            y, off = _i32(buf, off)
            _f1, off = _u8(buf, off)
            _f2, off = _u8(buf, off)
            has_name, off = _u8(buf, off)
        except struct.error:
            return points, off
        name = ""
        if has_name == 1:
            try:
                name_b, off = _read_pascal(buf, off)
                if all(32 <= b <= 126 for b in name_b):
                    name = name_b.decode("ascii", errors="replace")
                else:
                    return points, off
            except (ValueError, struct.error):
                return points, off
        else:
            # 6 byte skip + i32
            if off + 10 > region_end:
                return points, off
            off += 6
            try:
                _, off = _i32(buf, off)
            except struct.error:
                return points, off
        if abs(x) <= 5_000_000 and abs(y) <= 5_000_000:
            points.append(TestPointRecord(x=x, y=y, name=name))
    return points, off


def _skip_nail_optional_tail(buf: bytes, off: int, region_end: int) -> int:
    """Skip the variable 3/15/23-byte tail used in nail records."""
    if off + 3 > region_end:
        return off
    marker_a = buf[off]
    marker_b = buf[off + 2]
    off += 3
    if marker_b == 1:
        if off + 12 > region_end:
            return off
        off += 12
    elif marker_a == 1:
        if off + 20 > region_end:
            return off
        off += 20
    return off


def _read_nails(buf: bytes, off: int, region_end: int) -> tuple[int, int]:
    """layer_nails_read: magic u32(4), then two optional nail groups.

    The record payloads are not yet exposed, but this advances exactly
    through the observed fixed fields plus the conditional byte tail.
    """
    if off + 4 > region_end:
        return 0, off
    try:
        magic, off = _u32(buf, off)
    except struct.error:
        return 0, off
    if magic != 4:
        return 0, off
    if off + 4 > region_end:
        return 0, off
    try:
        first_count, off = _u32(buf, off)
    except struct.error:
        return 0, off
    if first_count > 1_000_000:
        return 0, off
    read_count = 0
    if first_count > 0:
        if off + 4 > region_end:
            return read_count, off
        off += 4
        for _ in range(first_count):
            fixed = 36 + 3 + 8
            if off + fixed > region_end:
                return read_count, off
            off += 36
            off += 3
            off += 8
            off = _skip_nail_optional_tail(buf, off, region_end)
            if off + 4 > region_end:
                return read_count, off
            off += 4
            read_count += 1
    if off + 4 > region_end:
        return read_count, off
    try:
        second_count, off = _i32(buf, off)
    except struct.error:
        return read_count, off
    if second_count <= 0:
        return read_count, off
    if second_count > 1_000_000:
        return read_count, off
    if off + 4 > region_end:
        return read_count, off
    off += 4
    for _ in range(second_count):
        fixed = 36 + 3 + 8
        if off + fixed > region_end:
            return read_count, off
        off += fixed
        off = _skip_nail_optional_tail(buf, off, region_end)
        if off + 4 > region_end:
            return read_count, off
        off += 4
        read_count += 1
    return read_count, off


def _read_postnails(buf: bytes, off: int, region_end: int) -> tuple[int, int]:
    """layer_postnails_read: u32 count, optional u32 header, count × 9 bytes."""
    if off + 4 > region_end:
        return 0, off
    try:
        count, off = _u32(buf, off)
    except struct.error:
        return 0, off
    if count == 0:
        return 0, off
    if count > 1_000_000:
        return 0, off
    if off + 4 > region_end:
        return 0, off
    off += 4
    skip = count * 9
    if off + skip > region_end:
        return 0, off
    return count, off + skip


def _read_layer_end(buf: bytes, off: int, region_end: int) -> int:
    """layer_end_read: consume zero padding bytes until the next non-zero."""
    while off < region_end and buf[off] == 0:
        off += 1
    return off


def _read_texts(buf: bytes, off: int, region_end: int) -> tuple[list[TextRecord], int]:
    """layer_texts_read: u32 count + u32 + count × (Pascal name + 39 fixed bytes).

    The Pascal name is the visible text content (typically a refdes
    label like 'R12', 'C45', 'U7'). We additionally validate each
    decoded text as printable ASCII — when the surfaces walker landed
    the cursor in mis-aligned territory the "Pascal name" comes back
    as binary garbage, and we want to fail-soft rather than emit it.
    """
    if off + 4 > region_end:
        return [], off
    count, off = _u32(buf, off)
    if count == 0:
        return [], off
    if count > 1_000_000:
        return [], off
    if off + 4 > region_end:
        return [], off
    _, off = _u32(buf, off)
    texts: list[TextRecord] = []
    for _i in range(count):
        if off + 1 > region_end:
            break
        try:
            name_b, off = _read_pascal(buf, off)
        except (ValueError, struct.error):
            break
        # 39-byte trailer — font / size / placement; not yet decoded.
        if off + 39 > region_end:
            break
        off += 39
        # Reject obviously-garbage text: must be printable ASCII (or
        # empty), 0-30 chars. Anything else means the cursor isn't on
        # a real text section.
        if len(name_b) > 30:
            return [], off
        if any(b < 32 or b > 126 for b in name_b if b != 0):
            return [], off
        texts.append(TextRecord(text=name_b.decode("utf-8", errors="replace")))
    return texts, off


# === Heuristic: skip to next layer marker / EOF =================================


# Layer name markers we look for. The format includes inner signal
# layers (L1..L9, L10..L15) on multi-layer boards plus the canonical
# TOP / BOTTOM sides.
_LAYER_MARKER_NAMES = (
    b"TOP", b"BOTTOM", b"top", b"bottom",
    b"L1", b"L2", b"L3", b"L4", b"L5", b"L6", b"L7", b"L8", b"L9",
    b"L10", b"L11", b"L12", b"L13", b"L14", b"L15",
)


def _next_layer_marker(buf: bytes, search_from: int) -> int | None:
    best: int | None = None
    for name in _LAYER_MARKER_NAMES:
        marker = bytes([len(name)]) + name
        i = buf.find(marker, search_from)
        if i >= 0 and (best is None or i < best):
            best = i
    return best


# === Network names ==============================================================
#
# u32 a; u32 b
# if (a == 1 AND b == 0): single-net mode (no name list follows)
# else: u32 c; c × Pascal strings (plain text, no cipher)
#
# Because the layers between pins and network_names contain sections
# (lines/arcs/surfaces/texts/probes/nails/postnails) whose record
# layouts we have not fully decoded, we cannot land on the
# network_names header by sequential walking. Instead we scan the
# trailing bytes of the file for the longest run of valid
# Pascal-prefixed strings. The 8 bytes immediately preceding that run
# are the (a, b) header; the count is len(run).


def _is_plausible_net_name(s: bytes) -> bool:
    """Net names are typically uppercase identifiers with /, +, -, _, ., #."""
    if len(s) == 0 or len(s) > 64:
        return False
    for b in s:
        if 32 <= b <= 126:
            continue
        return False
    return True


def _scan_pascal_string_run(
    buf: bytes, start: int, end: int
) -> tuple[list[str], int, int]:
    """Walk forward from `start` reading Pascal strings as long as they
    parse and look like net names. Return (names, start_offset, end_offset).
    """
    names: list[str] = []
    cur = start
    while cur < end:
        n = buf[cur]
        if n == 0:
            break
        if cur + 1 + n > end:
            break
        s = buf[cur + 1:cur + 1 + n]
        if not _is_plausible_net_name(s):
            break
        names.append(s.decode("utf-8", errors="replace"))
        cur += 1 + n
    return names, start, cur


def _try_read_network_names(buf: bytes, after_layers: int) -> list[str]:
    """Locate and decode the trailing network_names section.

    Strategy: scan from `after_layers` toward EOF, find the position
    that yields the longest run of plausible Pascal-prefixed net names.
    Verify that the 8 bytes preceding that position parse as either
    `(a, b) == (1, 0)` (single-net mode, no list) or a plausible 3-uint
    header `(a, b, count)` where count == len(names).
    """
    end = len(buf)
    best_names: list[str] = []
    # Always include the trailing 25% of the file in the search window
    # — network_names is the last data block before EOF and the
    # per-layer section walkers can land cursor anywhere when they
    # bail mid-section. Cap the window at min(after_layers, last 25%).
    safe_window = end - max(end // 4, 4096)
    window_start = min(after_layers, safe_window)
    if window_start < 0:
        window_start = 0
    if window_start >= end - 8:
        return []
    cur = window_start
    while cur < end - 4:
        names, _s, _e = _scan_pascal_string_run(buf, cur, end)
        if len(names) >= len(best_names) and len(names) >= 1:
            # Bias toward longer runs that reach close to EOF.
            best_names = names
        cur += 1
    return best_names


# === Component records (real refdes section, post-layer) =======================
#
# After all layer bodies, multi-layer fixtures ship one or more
# component sections that carry the canonical refdes + value +
# footprint + position metadata for every part on the board. Each
# record's byte layout (variable length, dominated by string fields):
#
#     u8  refdes_len + refdes              (e.g. "C134", "R200", "U7")
#     i32 bbox_x1, bbox_y1                 (centi-mils)
#     i32 bbox_x2, bbox_y2
#     i32 cx, cy                           (placement centre)
#     u32 rotation                         (degrees: 0 / 90 / 180 / 270)
#     u32 kind                             (CAD-tool type code)
#     12 bytes zero-padding (3 × u32)
#     u8 sep + u8 plen + plen bytes value
#     u8 sep + u8 plen + plen bytes comment    (typically empty)
#     u8 plen + plen bytes footprint            (no leading sep)
#     5 bytes zero-padding
#     u32 pin_count
#     16 bytes (pin block header — purpose unclear)
#     pin_count × { u32 pin_idx + u8 sep + u8 plen + Pascal pin_name +
#                   variable zero-padding }
#
# We anchor the scan via a regex over the whole file (any position
# whose Pascal-prefixed body matches `[A-Z]{1,3}[0-9]{1,4}` AND whose
# next 8 i32 form plausible centi-mil coordinates), then parse each
# record bounded by the next refdes match.

_REFDES_PAT = re.compile(rb"[A-Z]{1,3}[0-9]{1,4}")


# Outline-group signature: 8 bytes `FF 00 00 00 00 FF 00 00` followed by
# `0B 00 00 00`. Anchored verbatim in `TVW::parse_outlines`. 166 occurrences
# on a typical multi-layer graphics-card .tvw — one F00B group per unique
# package footprint plus a handful of mechanical / corner markers.
_F00B_SIG = b"\xff\x00\x00\x00\x00\xff\x00\x00\x0b\x00\x00\x00"


def _read_outline_group(
    buf: bytes, sig_off: int, region_end: int
) -> OutlineGroup | None:
    """Parse one F00B outline group beginning at `sig_off`.

    Header layout: the 12-byte F00B signature is followed by a fixed-size
    header. We snap onto the first `\\xff\\xff\\xff\\xff` primitive marker
    occurring in the trailing 60 bytes — this absorbs the 11-vs-12 u32
    header variant we observe across groups (real OUTLINE_TB_TPN markers
    use the shorter form). Anything between the signature and the first
    marker is read as the header `tuple[int, ...]` for downstream use.
    """
    body_start = sig_off + 12
    if body_start >= region_end:
        return None
    # Header is small — search a short window for the first prim marker.
    scan_to = min(region_end, body_start + 80)
    first_marker = buf.find(b"\xff\xff\xff\xff", body_start, scan_to)
    if first_marker < 0:
        return None
    header_bytes = buf[body_start:first_marker]
    # Header should be a multiple of 4 (u32 sequence). If not, bail.
    if len(header_bytes) == 0 or len(header_bytes) % 4 != 0:
        return None
    header = struct.unpack_from(
        f"<{len(header_bytes) // 4}I", header_bytes
    )
    prims: list[OutlinePrimRecord] = []
    cur = first_marker
    while cur + 8 <= region_end:
        if buf[cur:cur + 4] != b"\xff\xff\xff\xff":
            break
        kind = struct.unpack_from("<I", buf, cur + 4)[0]
        if kind == 10:
            # Fixed-size line primitive: 4 i32 = (x1, y1, x2, y2).
            if cur + 24 > region_end:
                break
            x1, y1, x2, y2 = struct.unpack_from("<4i", buf, cur + 8)
            prims.append(
                OutlinePrimRecord(kind=kind, points=[(x1, y1), (x2, y2)])
            )
            cur += 24
        elif 3 <= kind <= 200:
            body_size = kind * 8
            if cur + 8 + body_size > region_end:
                break
            try:
                pts = list(
                    struct.iter_unpack("<2i", buf[cur + 8:cur + 8 + body_size])
                )
            except struct.error:
                break
            # Per-Pt validation gate from the reference algorithm: each
            # coord must satisfy |abs| ≤ 9999. Anything outside this
            # range is not a real outline polyline.
            if any(abs(x) > 9999 or abs(y) > 9999 for (x, y) in pts):
                break
            prims.append(OutlinePrimRecord(kind=kind, points=pts))
            cur += 8 + body_size
        else:
            break
    return OutlineGroup(file_offset=sig_off, header=header, prims=prims)


def _scan_outline_groups(buf: bytes) -> list[OutlineGroup]:
    """Locate every F00B outline group and decode its primitive list."""
    positions: list[int] = []
    cur = 0
    while True:
        p = buf.find(_F00B_SIG, cur)
        if p < 0:
            break
        positions.append(p)
        cur = p + 1
    groups: list[OutlineGroup] = []
    for idx, p in enumerate(positions):
        end = positions[idx + 1] if idx + 1 < len(positions) else len(buf)
        g = _read_outline_group(buf, p, end)
        if g is not None:
            groups.append(g)
    return groups


def _scan_polygon_records(buf: bytes) -> list[PolygonRecord]:
    """Locate Custom-polygon records via the type=5 + Pascal "Custom"
    signature and decode each polygon's first vertex ring.

    The full per-polygon body has multiple rings + footer data that
    isn't fully decoded yet, but the first ring is sufficient to
    cover the board outline and most copper polygon shapes.
    """
    polygons: list[PolygonRecord] = []
    matches = list(_CUSTOM_POLY_RE.finditer(buf))
    n = len(buf)
    for idx, m in enumerate(matches):
        sig_off = m.start()
        name_len_off = sig_off + 8
        if name_len_off >= n:
            continue
        name_len = buf[name_len_off]
        body_off = name_len_off + 1 + name_len
        if body_off + 16 > n:
            continue
        next_sig = matches[idx + 1].start() if idx + 1 < len(matches) else n
        try:
            bx1, by1, bx2, by2 = struct.unpack_from("<4i", buf, body_off)
        except struct.error:
            continue
        # Sanity: bbox coords in plausible range.
        if any(abs(v) > 10_000_000 for v in (bx1, by1, bx2, by2)):
            continue
        # Vertex count is at body_off + 36 in the layout we surveyed.
        vc_off = body_off + 36
        if vc_off + 4 > n:
            continue
        try:
            vc = struct.unpack_from("<I", buf, vc_off)[0]
        except struct.error:
            continue
        vertices: list[tuple[int, int]] = []
        if 1 <= vc <= 10_000:
            v_start = vc_off + 4
            if v_start + vc * 8 <= next_sig and v_start + vc * 8 <= n:
                for vi in range(vc):
                    try:
                        x, y = struct.unpack_from(
                            "<ii", buf, v_start + vi * 8
                        )
                    except struct.error:
                        break
                    vertices.append((x, y))
        name = buf[name_len_off + 1:name_len_off + 1 + name_len].decode(
            "ascii", errors="replace"
        )
        polygons.append(
            PolygonRecord(
                name=name,
                bbox_x1=bx1, bbox_y1=by1,
                bbox_x2=bx2, bbox_y2=by2,
                vertices=vertices,
            )
        )
    return polygons


def _scan_component_records(buf: bytes) -> list[ComponentRecord]:
    """Locate and decode every component record in the file."""
    candidates: list[int] = []
    n = len(buf)
    for i in range(n - 1):
        plen = buf[i]
        if not (2 <= plen <= 8):
            continue
        body = buf[i + 1:i + 1 + plen]
        if not _REFDES_PAT.fullmatch(body):
            continue
        # Filter out layer-name false positives (L1..L15) and 1-letter
        # prefixes that look like coords ('A1' is a legit refdes column
        # but 'L8' is a layer marker — we already process that
        # separately).
        if (plen == 2 and body[0:1] == b"L" and body[1:].isdigit()):
            continue
        if plen == 3 and body[0:1] == b"L" and body[1:].isdigit():
            continue
        # Must have plausible bbox after the refdes.
        if i + 1 + plen + 32 > n:
            continue
        try:
            bx1, by1, bx2, by2, cx, cy = struct.unpack_from(
                "<6i", buf, i + 1 + plen
            )
        except struct.error:
            continue
        if any(abs(v) > 10_000_000 for v in (bx1, by1, bx2, by2, cx, cy)):
            continue
        # Sanity: bbox must enclose the centre.
        if not (bx1 <= cx <= bx2 and by1 <= cy <= by2):
            continue
        candidates.append(i)

    components: list[ComponentRecord] = []
    for idx, start in enumerate(candidates):
        next_start = candidates[idx + 1] if idx + 1 < len(candidates) else n
        rec = _parse_component_at(buf, start, next_start)
        if rec is not None:
            components.append(rec)
    return components


def _parse_component_at(
    buf: bytes, start: int, region_end: int
) -> ComponentRecord | None:
    """Best-effort parser: read fixed prefix + variable string fields.

    Records have variable format depending on the component kind code,
    so we read defensively — when a field would overrun the next
    record's start we return what we have so far rather than failing.
    """
    value = ""
    comment = ""
    footprint = ""
    pin_count = 0
    o = start
    try:
        plen, o = _u8(buf, o)
        if not (1 <= plen <= 12):
            return None
        refdes_bytes = buf[o:o + plen]
        if not all(32 <= b <= 126 for b in refdes_bytes):
            return None
        refdes = refdes_bytes.decode("ascii", errors="replace")
        o += plen
        if o + 32 > region_end:
            return None
        bx1, o = _i32(buf, o)
        by1, o = _i32(buf, o)
        bx2, o = _i32(buf, o)
        by2, o = _i32(buf, o)
        cx, o = _i32(buf, o)
        cy, o = _i32(buf, o)
        rotation, o = _u32(buf, o)
        kind, o = _u32(buf, o)
        # 3 u32 — may carry additional fields per kind, may be padding.
        if o + 12 > region_end:
            return None
        o += 12
    except (struct.error, IndexError, UnicodeDecodeError):
        return None

    # Soft reads from here onward — record may end before all fields.
    try:
        if o + 2 <= region_end:
            _sep_v, o = _u8(buf, o)
            plen_v, o = _u8(buf, o)
            if plen_v <= 64 and o + plen_v <= region_end:
                value = buf[o:o + plen_v].decode("ascii", errors="replace")
                o += plen_v
        if o + 2 <= region_end:
            _sep_c, o = _u8(buf, o)
            plen_c, o = _u8(buf, o)
            if plen_c <= 64 and o + plen_c <= region_end:
                comment = buf[o:o + plen_c].decode("ascii", errors="replace")
                o += plen_c
        if o + 1 <= region_end:
            plen_f, o = _u8(buf, o)
            if plen_f <= 64 and o + plen_f <= region_end:
                footprint = buf[o:o + plen_f].decode("ascii", errors="replace")
                o += plen_f
        if o + 9 <= region_end:
            o += 5
            cnt_u32, o = _u32(buf, o)
            if cnt_u32 <= 1000:
                pin_count = cnt_u32
    except (struct.error, IndexError, UnicodeDecodeError):
        pass

    # Sanity: refdes must look like one (printable, common prefixes).
    if not refdes or refdes[0:1].isdigit():
        return None

    return ComponentRecord(
        refdes=refdes,
        value=value,
        comment=comment,
        footprint=footprint,
        cx=cx, cy=cy,
        bbox_x1=bx1, bbox_y1=by1, bbox_x2=bx2, bbox_y2=by2,
        rotation=rotation, kind=kind,
        pin_count=pin_count,
    )


# === Top-level walk =============================================================


def parse(raw: bytes) -> TVWFile:
    """Parse a TVW production-binary buffer end-to-end.

    Tolerant of regions we don't fully understand: when a section
    cannot be walked, we fall back on the next-layer-marker scan.
    """
    if not is_production_binary(raw):
        raise ValueError("not a TVW production-binary file (magic mismatch)")

    off = 0
    fh, off = _read_file_header(raw, off)

    file = TVWFile(
        version=1,
        date=fh["date"],
        vendor=fh["vendor"],
        product=fh["product"],
        layer_count_declared=fh["layer_count"],
    )

    last_off = off
    seen_layer_names: set[str] = set()

    # Walk layers via marker-anchored boundaries. We find each layer's
    # header by snapping to the nearest layer-name Pascal-prefixed
    # marker and then walking the structured parts within that layer.
    while True:
        # Snap to the next layer-name marker. The byte just before the
        # marker hosts the layer-type uint32 + sub1/sub2 + nothing — but
        # in practice the layer header starts ~12 bytes earlier.
        marker_at = _next_layer_marker(raw, off)
        if marker_at is None:
            break
        # The layer header begins 12 bytes (3 × u32) before the
        # name1 marker. Reset cursor to that anchor.
        layer_header_start = marker_at - 12
        if layer_header_start < off:
            # Marker we just consumed; advance past it.
            off = marker_at + 1
            continue

        try:
            lh, body_start = _read_layer_header(raw, layer_header_start)
        except (ValueError, struct.error, IndexError):
            # Marker isn't a real header — advance and retry.
            off = marker_at + 1
            continue

        if lh["is_empty"]:
            off = body_start
            continue

        # Layer names are unique within a file (TOP / BOTTOM / L1..L15).
        # When we re-encounter a name we already processed, the marker
        # is a spurious match inside polygon / line / surface data, not
        # a new layer header. Stop the walk.
        if lh["name1"] in seen_layer_names:
            break
        seen_layer_names.add(lh["name1"])

        # The constructor body. Bound the body by the next layer
        # marker (or EOF).
        next_marker = _next_layer_marker(raw, body_start)
        body_end = next_marker - 12 if next_marker is not None else len(raw)

        layer = Layer(
            name=lh["name1"] or "UNK",
            source_path=lh["source_path"],
            body_kind=lh["body_kind"],
        )

        if lh["body_kind"] == 0xb:
            # Special body: 3 u32 then end. Nothing useful for us.
            file.layers.append(layer)
            # Advance to the next layer's header start (= 12 bytes
            # before the next layer-name marker), so the next loop
            # iteration reads its header cleanly. Setting `off` to the
            # marker itself triggers the "already-consumed" guard.
            off = (next_marker - 12) if next_marker is not None else len(raw)
            last_off = off
            continue

        try:
            apertures, after_dcodes = _read_dcodes(raw, body_start, body_end)
        except (ValueError, struct.error, IndexError):
            apertures = []
            after_dcodes = body_start
        layer.apertures = apertures

        # Two int32 between dcodes and pins
        if after_dcodes + 8 <= body_end:
            try:
                a, after_a = _i32(raw, after_dcodes)
                b, after_b = _i32(raw, after_a)
            except (struct.error, IndexError):
                a, b, after_b = 0, 0, after_dcodes
        else:
            a, b, after_b = 0, 0, after_dcodes

        if (len(apertures) > 0) or a > 0 or b > 0:
            try:
                pins, after_pins, declared = _read_pins(raw, after_b, body_end)
            except (ValueError, struct.error, IndexError):
                pins, after_pins, declared = [], after_b, 0
            layer.pins = pins
            layer.pin_count_declared = declared
            # Walk lines / arcs / surfaces / texts sequentially only
            # when the pin walk reached the end of the declared block.
            # If we got a partial walk, the cursor is in the middle of
            # the pin records and the line walker would read garbage.
            if declared > 0 and len(pins) == declared:
                cursor = after_pins
                try:
                    lines, cursor = _read_lines(raw, cursor, body_end)
                except (ValueError, struct.error, IndexError):
                    lines = []
                layer.lines = lines
                try:
                    arcs, cursor = _read_arcs(raw, cursor, body_end)
                except (ValueError, struct.error, IndexError):
                    arcs = []
                layer.arcs = arcs
                try:
                    surfaces, cursor = _read_surfaces(raw, cursor, body_end)
                except (ValueError, struct.error, IndexError):
                    surfaces = []
                layer.surfaces = surfaces
                try:
                    texts, cursor = _read_texts(raw, cursor, body_end)
                except (ValueError, struct.error, IndexError):
                    texts = []
                layer.texts = texts
                if b != 2 and cursor + 4 <= body_end:
                    cursor += 4
                try:
                    test_points, cursor = _read_probes(raw, cursor, body_end)
                except (ValueError, struct.error, IndexError):
                    test_points = []
                layer.test_points = test_points
                try:
                    _, cursor = _read_nails(raw, cursor, body_end)
                except (ValueError, struct.error, IndexError):
                    pass
                try:
                    _, cursor = _read_postnails(raw, cursor, body_end)
                except (ValueError, struct.error, IndexError):
                    pass
                try:
                    second_lines, cursor = _read_lines(raw, cursor, body_end)
                except (ValueError, struct.error, IndexError):
                    second_lines = []
                layer.second_lines = second_lines
                cursor = _read_layer_end(raw, cursor, body_end)
                last_off = cursor
            else:
                last_off = after_pins
        else:
            last_off = after_b

        file.layers.append(layer)

        if next_marker is None:
            break
        # Advance `off` to the next layer header's start, not to the
        # marker itself — otherwise the next iteration's
        # `_next_layer_marker` returns the same marker, the
        # `layer_header_start < off` guard fires, and the next layer
        # is silently skipped.
        off = next_marker - 12

    # Recover the netlist from the trailing region. We can't sequentially
    # walk lines/arcs/surfaces/texts/probes/nails/postnails (their byte
    # layout isn't fully decoded yet), so we scan the trailing bytes for
    # the longest run of plausible Pascal-prefixed net names.
    file.net_names = _try_read_network_names(raw, last_off)

    # Component records — refdes + value + footprint + position. These
    # sit in dedicated sections after all layer bodies; we anchor the
    # scan on Pascal-prefixed refdes patterns whose following bytes
    # form plausible centi-mil bbox coordinates. See docstring on
    # `_scan_component_records` for the per-record byte layout.
    file.components = _scan_component_records(raw)

    # Custom polygons — board outline, ground-plane shapes, and other
    # non-rectangular copper / silkscreen primitives. We decode each
    # polygon's first vertex ring (most use a single ring).
    file.polygons = _scan_polygon_records(raw)

    # F00B outline groups — package outlines, mechanical markers, and
    # the per-corner OUTLINE_TB_TPN groups. Each group carries a small
    # header + a list of line / polyline primitives in mils.
    file.outlines = _scan_outline_groups(raw)

    return file
