# SPDX-License-Identifier: Apache-2.0
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
from api.board.parser.base import InvalidBoardFile, MalformedHeaderError

_ZLIB_MAGIC_FAST = b"\x78\x9c"  # default compression
_ZLIB_MAGIC_BEST = b"\x78\xda"  # best compression
_ZLIB_MAGIC_NONE = b"\x78\x01"  # store
_ZLIB_MAGICS = (_ZLIB_MAGIC_FAST, _ZLIB_MAGIC_BEST, _ZLIB_MAGIC_NONE)


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
        text = zlib.decompress(raw[4:]).decode("utf-8", errors="replace")
    except zlib.error as exc:
        raise InvalidBoardFile(f"fz-zlib: decompression failed ({exc})") from exc

    sections = _split_sections(text)
    parts_section = _pick_section(sections, "REFDES")
    pins_section = _pick_section(sections, "NET_NAME")
    vias_section = _pick_section(sections, "TESTVIA")

    if parts_section is None or pins_section is None:
        raise MalformedHeaderError("fz-zlib: missing REFDES or NET_NAME section")

    parts, pin_lookup = _build_parts(parts_section)
    pins, parts = _build_pins(pins_section, parts, pin_lookup)
    nails = _build_nails(vias_section) if vias_section else []
    nets = _derive_nets(pins)

    return Board(
        board_id=board_id,
        file_hash=file_hash,
        source_format=source_format,
        outline=[],  # FZ-zlib carries no explicit outline; UI infers from pins.
        parts=parts,
        pins=pins,
        nets=nets,
        nails=nails,
    )


# ---------------------------------------------------------------------------
# Section walker
# ---------------------------------------------------------------------------


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
        x = _safe_float(row[4])
        y = _safe_float(row[5])
        # row[6] == TEST_POINT (often empty), row[7] == RADIUS (pad radius mils)

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
        pins.append(
            Pin(
                part_refdes=refdes,
                index=pin_idx,
                pos=Point(x=ix, y=iy),
                net=(net_name or None),
                probe=None,
                layer=owner.layer,
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


def _build_nails(rows: list[list[str]]) -> list[Nail]:
    """TESTVIA schema: `TESTVIA NET_NAME REFDES PIN_NUMBER PIN_NAME VIA_X VIA_Y TEST_POINT RADIUS`."""
    out: list[Nail] = []
    for i, row in enumerate(rows, start=1):
        if len(row) < 7:
            continue
        net_name = row[1].strip()
        x = _safe_float(row[5])
        y = _safe_float(row[6])
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
