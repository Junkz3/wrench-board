"""Decoder for a substitution-encoded ASCII boardview ``.brd`` container.

Some ``.brd`` files store a line-based ASCII boardview grammar under a fixed
byte-for-byte substitution table — the SAME table for every file. Line-break
bytes (``\r`` = 13, ``\n`` = 10) pass through unencoded; every other byte is
substituted. This module reads the bytes of a user-supplied file for
interoperability: it applies the recovered table (frozen in ``SUBST``) to get
the plaintext boardview grammar, then parses the structured sections.

The table was derived from the byte statistics of sample files (a small-alphabet
monoalphabetic mapping — not compression): the most frequent byte maps to SPACE,
the most common letter-token to the ground net, digits via the dense index
column, the rest by a bigram language fit against boardview token vocabulary. A
few rare bytes stay best-effort and only affect the readability of some names;
the mapping is deterministic and collision-free across samples, so the net graph
is unaffected.

Decoded grammar (line-based, fixed-width, keyword-delimited per-block):
  <device-name>#
  <part-name>#               <- a part block: name, then its pin/outline rows
  X1 Y1 X2 Y2 NET1 NET2 ...
  ...
  <PARTS-keyword>#
  REFDES  layercode  pinoffset   <- the parts table (one row per component)
  ...
  <NAILS-keyword>#
  X  Y  probe  side  NET         <- the nail / pin-net table (connectivity)
  ...

We extract the two reliably-structured, diagnostically valuable sections:
  * the PARTS table  ->  `Part`s (refdes + layer)
  * the NAILS table  ->  `Pin`s (position + net + probe) and `Net`s with the
    shared power/ground heuristics.
"""

from __future__ import annotations

import re

from api.board.model import Board, Layer, Net, Part, Pin, Point
from api.board.parser._ascii_boardview import GROUND_RE, POWER_RE
from api.board.parser.base import (
    BoardParser,
    InvalidBoardFile,
    register,
)

_MAGIC = b"\x23\xe2\x63\x28"

# The recovered fixed substitution table (encoded byte -> plaintext char).
# Newline bytes 0x0a / 0x0d are NOT in the table — they pass through. Frozen
# here; see the module docstring for how it was derived.
SUBST: dict[int, str] = {
    0x13: "?", 0x23: "$", 0x24: "/", 0x26: "&", 0x28: "_", 0x2A: "M",
    0x2B: "A", 0x2C: "O", 0x2D: "E", 0x2E: "G", 0x2F: "C", 0x32: "5",
    0x33: "3", 0x62: ",", 0x63: "Z", 0x64: ")", 0x6A: "V", 0x6B: "R",
    0x6C: "N", 0x6D: "Q", 0x6E: "+", 0x6F: "T", 0x70: "<", 0x71: "#",
    0x72: "8", 0x73: "2", 0x74: ".", 0x7E: "=", 0xA4: "*", 0xA5: "Y",
    0xA6: ";", 0xA7: "X", 0xA9: "%", 0xAA: "U", 0xAB: "H", 0xAC: "B",
    0xAD: "!", 0xAE: "S", 0xAF: "I", 0xB1: "6", 0xB2: "9", 0xB3: "1",
    0xB4: "F", 0xE2: "@", 0xE4: "J", 0xE5: "(", 0xE6: "W", 0xE9: ":",
    0xEA: "K", 0xEB: "P", 0xEC: "L", 0xED: "-", 0xEE: "D", 0xF0: ">",
    0xF1: "7", 0xF2: "4", 0xF3: "0", 0xF7: " ",
}

# Inverse, for synthesizing genuinely-encoded fixtures in tests (round-trip).
# Built once; SUBST is a bijection over its domain, so the inverse is unambiguous.
_INV: dict[str, int] = {v: k for k, v in SUBST.items()}

# A 256-entry lookup so decoding is one bytes.translate() pass. Bytes not in the
# table map to themselves (covers \r, \n, and any stray byte a future variant
# might carry — we don't want to silently corrupt them). SUBST values are always
# single-char str, so the mapped branch is a plain ord().
_DECODE_TABLE = bytes(ord(SUBST[b]) if b in SUBST else b for b in range(256))


def decode_subst(raw: bytes) -> bytes:
    """Apply the fixed substitution table. Newlines pass through untouched."""
    return raw.translate(_DECODE_TABLE)


def encode_subst(text: str) -> bytes:
    """Encode plaintext with the inverse table (for test fixtures).

    Inverse of `decode_subst`. Characters with no table byte (the rare
    best-effort symbols) and the line breaks pass through as their own byte —
    enough for round-tripping the boardview tokens used in fixtures (digits,
    upper-case, space, ``_ . + - : ( ) #`` ...).
    """
    out = bytearray()
    for ch in text:
        if ch in ("\n", "\r"):
            out.append(ord(ch))
        elif ch in _INV:
            out.append(_INV[ch])
        else:
            out.append(ord(ch) & 0xFF)
    return bytes(out)


# ---------------------------------------------------------------------------
# Grammar parsing (on the DECODED text)
# ---------------------------------------------------------------------------

# A parts-table row: "<REFDES> <layercode> <pinoffset>".  REFDES is a 1- or
# 2-letter class prefix + digits (e.g. C6620, R2901, U3340, TA0400). We accept
# any leading alpha run so 2-letter prefixes (FL, LP, TA ...) come through.
_PART_ROW = re.compile(r"^([A-Za-z][A-Za-z]?\d{1,6})\s+(\d+)\s+(\d+)\s*$")

# A nail / pin-net row: EXACTLY five whitespace-separated fields
# "<X> <Y> <probe> <side> <NET>". X/Y are signed ints; probe/side are ints; NET
# is a SINGLE token (net names never contain spaces — verified on samples).
# Requiring exactly 5 fields distinguishes these from the part-block pin
# records, which carry two trailing net tokens ("X1 Y1 X2 Y2 NET1 NET2").
_NAIL_ROW = re.compile(r"^(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(\d+)\s+(\S+)\s*$")


def _layer_from_code(code: int) -> Layer:
    """Map the parts-table layer code to a Layer.

    Observed codes: 9, 10, 1. The low bit distinguishes the two faces in the
    common boardview convention this format follows (odd = top, even = bottom);
    9 (0b1001, odd) -> TOP, 10 (0b1010, even) -> BOTTOM. Unknown codes fall back
    to TOP — a conservative default that never raises on a slightly-odd file.
    """
    return Layer.TOP if (code & 0x1) else Layer.BOTTOM


def _layer_from_side(side: int) -> Layer:
    """Nail-row side: 1 -> TOP, else BOTTOM (matches the BRD2 convention)."""
    return Layer.TOP if side == 1 else Layer.BOTTOM


def decode_subst_brd(raw: bytes, *, file_hash: str, board_id: str) -> Board:
    """Decode a substitution-encoded ``.brd`` and parse its boardview grammar."""
    if not raw.startswith(_MAGIC):
        raise InvalidBoardFile("not a substitution-encoded .brd (bad magic)")

    text = decode_subst(raw).decode("latin-1")
    lines = text.splitlines()

    parts: list[Part] = []
    pins: list[Pin] = []
    seen_refdes: set[str] = set()

    # Pass 1 — the parts table: every "<REFDES> <code> <offset>" row.
    for ln in lines:
        m = _PART_ROW.match(ln.strip())
        if not m:
            continue
        refdes, code_s, _offset = m.group(1), m.group(2), m.group(3)
        if refdes in seen_refdes:
            continue
        seen_refdes.add(refdes)
        parts.append(
            Part(
                refdes=refdes,
                layer=_layer_from_code(int(code_s)),
                is_smd=True,  # surface-mount dominates these dumps; format carries no THT flag
                bbox=(Point(x=0, y=0), Point(x=0, y=0)),
                pin_refs=[],
            )
        )

    # Pass 2 — the nail / pin-net rows: each carries a position, a probe and a
    # net. These are the connectivity the diagnostic agent needs. The net graph
    # is complete (every pin carries its net; every net groups its pins) — that
    # is what the agent traverses. Per-part pin grouping, however, is *largely
    # unpopulated on real boards*: the parts-table `pinoffset` column (the real
    # pin-ownership key) is not decoded, so we fall back to attaching a pin to
    # the part whose refdes prefixes the net name — which fires on almost no real
    # nets (names like `CPU+IN_KIC-1`). Most pins therefore stay part-less (free
    # probe points). This is a documented limitation, not a connectivity bug.
    # TODO: decode the `pinoffset` column to populate Part.pin_refs.
    refdes_by_name = {p.refdes: i for i, p in enumerate(parts)}
    pin_idx_by_part: dict[int, int] = {}
    for ln in lines:
        m = _NAIL_ROW.match(ln)
        if not m:
            continue
        x_s, y_s, probe_s, side_s, net = m.groups()
        net = net.strip()
        layer = _layer_from_side(int(side_s))
        # Best-effort owner: the net token sometimes leads with a refdes
        # (e.g. "U3340_..."); otherwise the pin is a free probe.
        owner = None
        head = re.match(r"^([A-Za-z]{1,2}\d{1,6})", net)
        if head and head.group(1) in refdes_by_name:
            owner = refdes_by_name[head.group(1)]
        owner_refdes = parts[owner].refdes if owner is not None else ""
        if owner is not None:
            pin_idx_by_part[owner] = pin_idx_by_part.get(owner, 0) + 1
            index = pin_idx_by_part[owner]
        else:
            index = len(pins) + 1
        try:
            probe = int(probe_s)
        except ValueError:
            probe = None
        pins.append(
            Pin(
                part_refdes=owner_refdes,
                index=index,
                pos=Point(x=int(x_s), y=int(y_s)),
                net=(net or None),
                probe=(probe if probe and probe > 0 else None),
                layer=layer,
            )
        )

    if not parts and not pins:
        raise InvalidBoardFile("decoded but no parts or pins found")

    # Link pin_refs back onto parts + compute bbox from owned pins.
    for owner, part in enumerate(parts):
        refs = [i for i, pin in enumerate(pins) if pin.part_refdes == part.refdes]
        if not refs:
            continue
        xs = [pins[i].pos.x for i in refs]
        ys = [pins[i].pos.y for i in refs]
        parts[owner] = part.model_copy(
            update={
                "pin_refs": refs,
                "bbox": (Point(x=min(xs), y=min(ys)), Point(x=max(xs), y=max(ys))),
            }
        )

    nets = _derive_nets(pins)
    outline = _extract_outline(lines)

    return Board(
        board_id=board_id,
        file_hash=file_hash,
        source_format="brd-subst",
        outline=outline,
        parts=parts,
        pins=pins,
        nets=nets,
        nails=[],
    )


_COORD_PAIR = re.compile(r"^\s*(-?\d+)\s+(-?\d+)\s*$")


def _extract_outline(lines: list[str]) -> list[Point]:
    """Recover the board-edge polyline.

    The grammar stores the board outline as a dedicated section whose body is
    bare ``X Y`` coordinate pairs — the ONLY block in the file shaped that way
    (parts rows carry 3 fields, pin/nail rows carry 5). The section header is
    itself encoded by the handful of unmapped table bytes, so we locate the
    outline structurally: the longest contiguous run of bare coordinate pairs.
    (The only other 2-int line is the lone count line near the top, a run of
    length 1, so the real outline always wins.) Consecutive duplicate vertices —
    the format repeats shared segment endpoints — are collapsed. Returns ``[]``
    when no plausible run exists.
    """
    best_start = best_len = 0
    run_start = run_len = 0
    for i, ln in enumerate(lines):
        if _COORD_PAIR.match(ln):
            if run_len == 0:
                run_start = i
            run_len += 1
            if run_len > best_len:
                best_len, best_start = run_len, run_start
        else:
            run_len = 0
    if best_len < 4:  # a stray count/metadata pair is not a board edge
        return []
    pts: list[Point] = []
    for ln in lines[best_start : best_start + best_len]:
        m = _COORD_PAIR.match(ln)
        x, y = int(m.group(1)), int(m.group(2))
        if pts and pts[-1].x == x and pts[-1].y == y:
            continue  # collapse repeated shared vertices
        pts.append(Point(x=x, y=y))
    return pts


def _derive_nets(pins: list[Pin]) -> list[Net]:
    """Group pins by net name; flag power / ground via the shared heuristics."""
    by_name: dict[str, list[int]] = {}
    for i, pin in enumerate(pins):
        if pin.net is None:
            continue
        by_name.setdefault(pin.net, []).append(i)
    out: list[Net] = []
    for name, refs in sorted(by_name.items()):
        out.append(
            Net(
                name=name,
                pin_refs=refs,
                is_power=bool(POWER_RE.match(name)) or name.startswith("PP") or name.startswith("+"),
                is_ground=bool(GROUND_RE.match(name)),
            )
        )
    return out


@register
class SubstEncodedBoardParser(BoardParser):
    # Real files use the `.brd` extension (same as Test_Link / BRD2); the
    # content-sniff in base.py routes the substitution-encoded magic here. The
    # synthetic `.brd-subst` tag only keeps the registry non-colliding with the
    # other `.brd` parsers.
    extensions = (".brd-subst",)

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        return decode_subst_brd(raw, file_hash=file_hash, board_id=board_id)
