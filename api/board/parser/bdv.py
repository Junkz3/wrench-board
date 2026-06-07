"""Encoded `.bdv` boardview parser — two arithmetic schedules, one family.

`.bdv` files wrap an ASCII boardview grammar in an arithmetic encoding:
`clear = (key - encoded) & 0xFF` with the key walking a fixed schedule. Two
schedules exist in the wild, both decoded here:

1. **Per-byte schedule** (the historical synthetic shape). The key starts at
   160 and increments by 1 for every *non-newline byte*, wrapping 286→159.
   Line-break bytes (`\r`=13, `\n`=10) pass through unchanged and do not
   advance the counter. Payload is the canonical `var_data:` Test_Link shape
   → `parse_test_link_shape`.

2. **Per-line schedule** (the field shape). Every real `.bdv` in the field
   (header bytes `dd:`, the encoded form of the line-0 key) uses the SAME
   arithmetic family but a LINE-INDEXED key: constant for every byte within a
   line, advancing by 1 once per `\n`, with the same 285→159 wrap. (The
   per-line schedule was derived from the byte statistics of sample files —
   printable-ratio + decoded-marker scoring.) The decoded payload is a
   test-fixture ("TestLink") dialect — an outline contour, a `Part <ref> (T/B)`
   pin-list, and a `$n` nails list — which is NOT the `var_data:` shape, so it
   gets its own section parser (`_parse_fixture_dialect`).

`BDVParser.parse` sniffs the variant by header and routes accordingly. The
per-byte path and its tests are untouched (no regression).
"""

from __future__ import annotations

import re

from api.board.model import (
    Board,
    Layer,
    Nail,
    Net,
    Part,
    Pin,
    Point,
)
from api.board.parser._ascii_boardview import (
    GROUND_RE,
    POWER_RE,
    DialectMarkers,
    parse_test_link_shape,
)
from api.board.parser.base import (
    BoardParser,
    InvalidBoardFile,
    ObfuscatedFileError,
    register,
)

# --- per-byte schedule (historical synthetic shape) -----------------------
_KEY_START = 160
_KEY_RESET = 159
_KEY_MAX = 285  # after increment past this, wrap to _KEY_RESET


def _deobfuscate(raw: bytes) -> bytes:
    """Invert the per-byte arithmetic encoding.

    The encoding is symmetric — `encode == decode` with the same key
    schedule. We keep this as the public decode path; the inverse is
    exposed via `_obfuscate` for test fixtures only.
    """
    out = bytearray()
    key = _KEY_START
    for b in raw:
        if b in (10, 13):
            out.append(b)
            continue
        out.append((key - b) & 0xFF)
        key = _KEY_RESET if key >= _KEY_MAX else key + 1
    return bytes(out)


def _obfuscate(text: str) -> bytes:
    """Encode plaintext ASCII into the per-byte `.bdv` arithmetic encoding.

    Used only by tests that need to synthesize a fixture — runtime
    never calls this. Kept next to the decoder so both halves of the
    round-trip live in one place and stay in sync.
    """
    data = text.encode("utf-8")
    out = bytearray()
    key = _KEY_START
    for c in data:
        if c in (10, 13):
            out.append(c)
            continue
        out.append((key - c) & 0xFF)
        key = _KEY_RESET if key >= _KEY_MAX else key + 1
    return bytes(out)


# --- per-LINE schedule (the field shape) ----------------------------------
#
# The schedule, frozen as constants. ``step`` advances once per LF; the key is
# otherwise constant across a whole line. Same arithmetic (sub) and the same
# 285→159 wrap as the per-byte path — only the index changes (line number, not
# byte position). See the module docstring.
_SCHEDULE = {
    "mode": "sub",  # clear = (key - encoded) & 0xFF
    "start": 160,
    "step": 1,  # +1 per newline (\n)
    "wrap_hi": 285,
    "wrap_lo": 159,
    "per_line": True,
}

# Raw header magic: every real `.bdv` begins with the encoded form of
# its first line `<<format.asc>>`. At the line-0 key (160), `<` (60)
# encodes to `(160-60)&0xFF` = 100 = `d`, so `<<` → `dd` and the
# stream opens `dd:`. We detect on the still-encoded bytes.
_REAL_HEADER = b"dd:"


def _real_codec(data: bytes) -> bytes:
    """Apply the line-indexed real `.bdv` schedule to ``data``.

    Symmetric: with sub-mode and a fixed per-line key, applying this
    twice is the identity, so the SAME function both encodes and
    decodes. `\r` / `\n` always pass through unchanged; only `\n`
    advances the line key (with the 285→159 wrap).
    """
    out = bytearray()
    key = _SCHEDULE["start"]
    hi = _SCHEDULE["wrap_hi"]
    lo = _SCHEDULE["wrap_lo"]
    for b in data:
        if b in (10, 13):
            out.append(b)
            if b == 10:  # the LINE index advances once per LF
                key = key + _SCHEDULE["step"]
                if key > hi:
                    key = lo
            continue
        out.append((key - b) & 0xFF)
    return bytes(out)


def _deobfuscate_real(raw: bytes) -> bytes:
    """Decode real-variant `.bdv` bytes (line-indexed schedule)."""
    return _real_codec(raw)


def _obfuscate_real(text: str) -> bytes:
    """Encode plaintext into the real-variant schedule (symmetric).

    Used to synthesize the real-variant fixture in tests; the symmetry
    means decode(encode(x)) == x is guaranteed by construction.
    """
    return _real_codec(text.encode("utf-8"))


def _is_real_variant(raw: bytes) -> bool:
    """True if ``raw`` is the field (per-LINE) variant, by its header.

    Keyed on the raw `dd:` magic (encoded `<<format.asc>>`). The
    synthetic per-byte fixture starts with the encoded `var_data:`/`hello`
    text, never `dd:`, so it returns False and stays on the per-byte
    path.
    """
    return raw[:3] == _REAL_HEADER


# --- test-fixture ("TestLink") dialect parser -----------------------------
#
# The decoded real payload is NOT the var_data:/Parts:/Pins: Test_Link
# shape, so parse_test_link_shape can't read it. Its grammar:
#
#   <<format.asc>>
#    Board Outline Contour ...
#        X        Y        Radius
#       -4.183   5.752    0.000        <- outline vertices (x y radius)
#   <<nails.asc>>
#    Test Fixture Nails ...
#   Nail   X   Y   Type Grid T/B  Net   Net Name ...
#   $1    4.77  1.55  1  D2  (T)  #3    GND  ...   <- nail rows
#    Part Pins List ...
#   Part U2  (T)                       <- part header (refdes, side)
#    1   1   -3.207  3.722  1  +3VS  383   <- pin: idx name x y layer net nail
#
# We walk it section by section. Outline → Points; each `Part …` block →
# a Part plus its Pins (net + probe from the trailing nail column); the
# `$n` nail rows → Nails (and a power/ground net catalogue). Nets are
# derived from pins + nails via the shared heuristics.

_FLOAT = r"[-+]?\d+(?:\.\d+)?"
# Part header: "Part U2  (T)" / "Part JCRT1  (B)".
_PART_RE = re.compile(r"^Part\s+(\S+)\s+\(([TB])\)\s*$")
# Pin row: idx name x y layer net [nail]. name can be alphanumeric
# ("A1"), net can contain (), #, +, _, digits; nail is an optional
# trailing integer probe.
# Inherent ambiguity: a row ending in a single numeric token is parsed
# as net (the optional nail group stays empty), not as nail. That's the
# correct default for the real format — nets are named rails (+3V3, GND)
# and only ever appear in the (mandatory) net column, while nails are an
# optional extra; without two trailing tokens the lone one is the net.
_PIN_RE = re.compile(
    rf"^\s*(\d+)\s+(\S+)\s+({_FLOAT})\s+({_FLOAT})\s+(\d+)\s+(\S+)(?:\s+(\d+))?\s*$"
)
# Nail row: "$1  4.7725  1.5550  1  D2  (T)  #3  GND ...". We need the
# x, y, side (T/B), net number and net name; trailing columns vary.
_NAIL_RE = re.compile(
    rf"^\$(\d+)\s+({_FLOAT})\s+({_FLOAT})\s+\d+\s+\S+\s+\(([TB])\)\s+#(\d+)\s+(\S+)"
)
# Outline vertex: "  -4.183   5.752   0.000" (x y radius). Three floats.
_OUTLINE_RE = re.compile(rf"^\s*({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})\s*$")


def _net_or_none(token: str) -> str | None:
    """Normalise a fixture net token. `(NC)` / empty → no net."""
    if not token or token == "(NC)":
        return None
    return token


def _parse_fixture_dialect(text: str, *, board_id: str, file_hash: str) -> Board:
    """Parse the decoded test-fixture dialect into a `Board`.

    Outline vertices come from the `<<format.asc>>` Board-Outline-Contour
    block (x y radius — we keep x/y). Parts + pins come from the `Part …`
    pin-list; nails (and their net names) from the `$n` rows. Nets are
    derived from every pin/nail net mention via the shared power/ground
    heuristics so power rails (+3V3, +5VS) and grounds (GND) are flagged.
    """
    lines = text.splitlines()

    outline: list[Point] = []
    parts: list[Part] = []
    pins: list[Pin] = []
    nails: list[Nail] = []

    in_outline = False  # within the Board-Outline-Contour vertex table
    cur_part: str | None = None
    cur_layer = Layer.TOP
    cur_pin_refs: list[int] = []
    cur_pin_idx = 0

    def _flush_part() -> None:
        """Close the in-progress part, computing its bbox from its pins."""
        nonlocal cur_part, cur_pin_refs
        if cur_part is None:
            return
        if cur_pin_refs:
            xs = [pins[i].pos.x for i in cur_pin_refs]
            ys = [pins[i].pos.y for i in cur_pin_refs]
            bbox = (Point(x=min(xs), y=min(ys)), Point(x=max(xs), y=max(ys)))
        else:
            bbox = (Point(x=0, y=0), Point(x=0, y=0))
        parts.append(
            Part(
                refdes=cur_part,
                layer=cur_layer,
                is_smd=True,  # fixture dumps don't distinguish; SMD dominates
                bbox=bbox,
                pin_refs=list(cur_pin_refs),
            )
        )
        cur_part = None
        cur_pin_refs = []

    for raw_ln in lines:
        ln = raw_ln.rstrip()
        stripped = ln.strip()

        # Section transitions. The outline vertex table sits between the
        # "Board Outline Contour" / "X Y Radius" header and the next
        # "<<...>>" section.
        if stripped.startswith("<<"):
            _flush_part()
            in_outline = stripped == "<<format.asc>>"
            continue
        if "Board Outline Contour" in stripped:
            in_outline = True
            continue

        # Part header — opens a new part, closing the previous one.
        m = _PART_RE.match(stripped)
        if m:
            _flush_part()
            in_outline = False
            cur_part = m.group(1)
            cur_layer = Layer.TOP if m.group(2) == "T" else Layer.BOTTOM
            cur_pin_idx = 0
            continue

        # Nail row. In the real fixture format nails precede the part
        # list (<<format.asc>> → <<nails.asc>> → <<pins.asc>>) and never
        # interleave, so no part flush is needed here — flushing happens on
        # the <<...>> section transition and on each Part header.
        m = _NAIL_RE.match(stripped)
        if m:
            in_outline = False
            _, x, y, side, _netnum, netname = m.groups()
            nails.append(
                Nail(
                    probe=int(m.group(1)),
                    pos=Point(x=float(x), y=float(y)),
                    layer=Layer.TOP if side == "T" else Layer.BOTTOM,
                    net=netname,
                )
            )
            continue

        # Pin row — only meaningful inside a Part block.
        if cur_part is not None:
            m = _PIN_RE.match(ln)
            if m:
                _, name, x, y, layer_int, net, nail = m.groups()
                cur_pin_idx += 1
                probe = int(nail) if nail else None
                pins.append(
                    Pin(
                        part_refdes=cur_part,
                        index=cur_pin_idx,
                        pos=Point(x=float(x), y=float(y)),
                        net=_net_or_none(net),
                        probe=probe,
                        layer=cur_layer,
                        name=name,
                    )
                )
                cur_pin_refs.append(len(pins) - 1)
                continue

        # Outline vertex.
        if in_outline:
            m = _OUTLINE_RE.match(ln)
            if m:
                x, y, _radius = m.groups()
                outline.append(Point(x=float(x), y=float(y)))
                continue

    _flush_part()

    # Derive nets from every pin net mention, then fold in nail nets so
    # power/ground rails that only appear on test points still surface.
    nets = _derive_nets_fixture(pins, nails)

    if not parts and not pins and not nails:
        raise InvalidBoardFile("bdv: real-variant decode produced no board data")

    return Board(
        board_id=board_id,
        file_hash=file_hash,
        source_format="bdv",
        outline=outline,
        parts=parts,
        pins=pins,
        nets=nets,
        nails=nails,
    )


def _derive_nets_fixture(pins: list[Pin], nails: list[Nail]) -> list[Net]:
    """Group pins by net + attach nail probes; flag power/ground.

    Mirrors `_ascii_boardview.derive_nets` but also lists nail probes
    under their net so a power rail reachable only via a test point is
    still catalogued. Deterministic (sorted by net name)."""
    pins_by_net: dict[str, list[int]] = {}
    for i, pin in enumerate(pins):
        if pin.net is None:
            continue
        pins_by_net.setdefault(pin.net, []).append(i)
    # Nails contribute net NAMES even when no pin references them.
    for nail in nails:
        pins_by_net.setdefault(nail.net, [])
    return [
        Net(
            name=name,
            pin_refs=refs,
            is_power=bool(POWER_RE.match(name)),
            is_ground=bool(GROUND_RE.match(name)),
        )
        for name, refs in sorted(pins_by_net.items())
    ]


@register
class BDVParser(BoardParser):
    extensions = (".bdv",)

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        # Sniff the variant on the still-encoded bytes, then decode with
        # the matching schedule. The real (field) variant decodes to the
        # test-fixture dialect; the historical per-byte variant to the
        # var_data: Test_Link shape.
        if _is_real_variant(raw):
            try:
                plain = _deobfuscate_real(raw).decode("utf-8", errors="replace")
            except Exception as exc:  # pragma: no cover — defensive
                raise ObfuscatedFileError(
                    f"bdv: real-variant decoding failed ({exc})"
                ) from exc
            return _parse_fixture_dialect(plain, board_id=board_id, file_hash=file_hash)

        try:
            plain = _deobfuscate(raw).decode("utf-8", errors="replace")
        except Exception as exc:  # pragma: no cover — defensive
            raise ObfuscatedFileError(f"bdv: decoding failed ({exc})") from exc
        return parse_test_link_shape(
            plain,
            markers=DialectMarkers(),
            source_format="bdv",
            board_id=board_id,
            file_hash=file_hash,
        )
