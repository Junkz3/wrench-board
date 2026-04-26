# SPDX-License-Identifier: Apache-2.0
"""OpenBoardView .brd (Test_Link) parser — written from scratch.

Reference for the format : the OpenBoardView project documents the .brd
Test_Link layout. The code below is a clean-room reimplementation from
that format specification — no code from the OBV codebase was copied
(per hard rule #1 in CLAUDE.md, Apache 2.0).

See docs/superpowers/specs/2026-04-22-boardview-formats-roadmap.md for the
parser registry overview.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from api.board.model import Board, Layer, Nail, Net, Part, Pin, Point
from api.board.parser.base import (
    BoardParser,
    InvalidBoardFile,
    MalformedHeaderError,
    ObfuscatedFileError,
    PinPartMismatchError,
    register,
)

_OBF_SIGNATURE = b"\x23\xe2\x63\x28"


@dataclass
class _Header:
    num_format: int
    num_parts: int
    num_pins: int
    num_nails: int


@register
class BRDParser(BoardParser):
    extensions = (".brd",)

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        if raw.startswith(_OBF_SIGNATURE):
            raise ObfuscatedFileError("file uses OBV XOR obfuscation — refused")

        text = raw.decode("utf-8", errors="replace")
        if "str_length:" not in text or "var_data:" not in text:
            raise InvalidBoardFile("unknown encoding or not a .brd Test_Link file")

        lines = _lines(text)
        header = _parse_header(lines)
        outline = _parse_outline(lines, header.num_format)

        parts_raw = _parse_parts(lines, header.num_parts)
        parts = [
            Part(
                refdes=r,
                layer=_layer_from_bits(t),
                is_smd=_is_smd_from_bits(t),
                bbox=(Point(x=0, y=0), Point(x=0, y=0)),
                pin_refs=[],
            )
            for r, t, _ in parts_raw
        ]
        pins, parts = _parse_pins_and_patch_parts(lines, header.num_pins, parts_raw, parts)
        nails = _parse_nails(lines, header.num_nails)
        pins = _backfill_empty_nets(pins, nails)
        nets = _derive_nets(pins)

        return Board(
            board_id=board_id,
            file_hash=file_hash,
            source_format="brd",
            outline=outline,
            parts=parts,
            pins=pins,
            nets=nets,
            nails=nails,
        )


def _lines(text: str) -> list[str]:
    """Return stripped non-empty lines.

    Blank lines are dropped globally : .brd blocks are line-oriented and
    should not contain internal blank lines. If a real-world file ever
    breaks that assumption, block-aware parsing (scanning by block headers
    rather than globally-cleaned lines) will be needed.
    """
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _parse_header(lines: list[str]) -> _Header:
    for ln in lines:
        if ln.startswith("var_data:"):
            rest = ln[len("var_data:") :].split()
            if len(rest) != 4:
                raise MalformedHeaderError("var_data")
            try:
                return _Header(*(int(t) for t in rest))
            except ValueError as exc:
                raise MalformedHeaderError("var_data") from exc
    raise MalformedHeaderError("var_data")


def _parse_outline(lines: list[str], n: int) -> list[Point]:
    try:
        idx = lines.index("Format:")
    except ValueError as exc:
        # n == 0 is valid: var_data declared no outline points, so the
        # Format: block may be legitimately absent. Any other case is
        # a structural error in the file.
        if n == 0:
            return []
        raise MalformedHeaderError("Format") from exc
    pts: list[Point] = []
    for raw in lines[idx + 1 : idx + 1 + n]:
        toks = raw.split()
        if len(toks) != 2:
            raise MalformedHeaderError("Format")
        try:
            x, y = int(toks[0]), int(toks[1])
        except ValueError as exc:
            raise MalformedHeaderError("Format") from exc
        pts.append(Point(x=x, y=y))
    if len(pts) != n:
        raise MalformedHeaderError("Format")
    return pts


def _parse_parts(lines: list[str], n: int) -> list[tuple[str, int, int]]:
    """Parse the Parts: / Pins1: block.

    Returns a list of `(refdes, type_layer, end_of_pins)` tuples.

    `end_of_pins` is the 1-based exclusive upper bound of pin indices owned by
    this part (used in Task 7 for pin-to-part linkage). Part k owns pins in
    [prev_end, end_of_pins_k), with prev_end starting at 0.

    Real-world `.brd` files from some exporters append extra whitespace-separated
    tokens (footprint, pad-count) after the three required fields. We accept
    any line with >= 3 tokens and silently ignore the rest, which matches
    observed behavior of the OpenBoardView reference tooling.
    """
    if n == 0:
        return []
    try:
        idx = next(i for i, ln in enumerate(lines) if ln in ("Parts:", "Pins1:"))
    except StopIteration as exc:
        raise MalformedHeaderError("Parts") from exc

    out: list[tuple[str, int, int]] = []
    for raw in lines[idx + 1 : idx + 1 + n]:
        toks = raw.split()
        if len(toks) < 3:
            raise MalformedHeaderError("Parts")
        try:
            name = toks[0]
            type_layer = int(toks[1])
            end_of_pins = int(toks[2])
        except ValueError as exc:
            raise MalformedHeaderError("Parts") from exc
        out.append((name, type_layer, end_of_pins))
    if len(out) != n:
        raise MalformedHeaderError("Parts")
    return out


def _layer_from_bits(type_layer: int) -> Layer:
    """Single-bit scheme : bit 0x2 set → bottom layer, else top.

    Validated against the fixture : 5 (0b0101) → top, 10 (0b1010) → bottom.

    Only bit 0x2 is meaningful ; other bits (0x1 / 0x8 / higher) are reserved and ignored here.
    """
    return Layer.BOTTOM if (type_layer & 0x2) else Layer.TOP


def _is_smd_from_bits(type_layer: int) -> bool:
    """Bit 0x4 set → SMD, else through-hole.

    Validated : 5 → SMD, 10 → through-hole.

    Only bit 0x4 is meaningful ; other bits (0x1 / 0x8 / higher) are reserved and ignored here.
    """
    return bool(type_layer & 0x4)


def _parse_pins_and_patch_parts(
    lines: list[str],
    num_pins: int,
    parts_raw: list[tuple[str, int, int]],
    parts: list[Part],
) -> tuple[list[Pin], list[Part]]:
    """Parse the Pins: / Pins2: block ; link each pin to its owner part.

    Each pin line is `x y probe part_idx [net_name]` where `part_idx` is
    1-based (1..len(parts_raw)). Ownership ranges come from the
    `end_of_pins` exclusive upper bounds in `parts_raw` : part k owns
    pin indices [prev_end, end_of_pins_k). `Pin.index` is 1-based within
    the owning part.

    Returns (pins, patched_parts) where each patched Part has its
    `pin_refs` populated and `bbox` computed from pin positions.
    """
    if num_pins == 0:
        # No pins — leave parts unchanged (bbox / pin_refs stay as the
        # zero placeholder from Task 6). Empty boards are degenerate but valid.
        return [], parts

    try:
        idx = next(i for i, ln in enumerate(lines) if ln in ("Pins:", "Pins2:"))
    except StopIteration as exc:
        raise MalformedHeaderError("Pins") from exc

    pin_lines = lines[idx + 1 : idx + 1 + num_pins]
    if len(pin_lines) != num_pins:
        raise MalformedHeaderError("Pins")

    # Ownership ranges : part k owns pins [prev_end, end_of_pins_k).
    pin_refs_by_part: list[list[int]] = [[] for _ in parts_raw]
    prev_end = 0
    for k, (_, _, end) in enumerate(parts_raw):
        pin_refs_by_part[k] = list(range(prev_end, end))
        prev_end = end

    pins: list[Pin] = []
    counters = [0] * len(parts_raw)  # 1-based index within each part
    for i, raw in enumerate(pin_lines):
        toks = raw.split()
        if len(toks) < 4:
            raise MalformedHeaderError("Pins")
        try:
            x = int(toks[0])
            y = int(toks[1])
            probe = int(toks[2])
            part_idx = int(toks[3])
        except ValueError as exc:
            raise MalformedHeaderError("Pins") from exc
        net = toks[4] if len(toks) >= 5 else ""

        if part_idx < 1 or part_idx > len(parts_raw):
            raise PinPartMismatchError(i)

        owner_k = part_idx - 1
        owner = parts[owner_k]
        counters[owner_k] += 1

        pins.append(
            Pin(
                part_refdes=owner.refdes,
                index=counters[owner_k],
                pos=Point(x=x, y=y),
                net=(net or None),
                probe=(probe if probe != -99 else None),
                layer=owner.layer,
            )
        )

    # Cross-validate the two ownership sources (end_of_pins boundaries vs the
    # part_idx on each pin line). If they disagree, the file is inconsistent —
    # raise rather than silently producing a board where pin.part_refdes
    # contradicts parts[k].pin_refs (anti-hallucination hard rule #5).
    for k, refs in enumerate(pin_refs_by_part):
        expected = parts[k].refdes
        for i in refs:
            if pins[i].part_refdes != expected:
                raise PinPartMismatchError(i)

    # Patch parts : pin_refs + bbox.
    patched: list[Part] = []
    for k, part in enumerate(parts):
        refs = pin_refs_by_part[k]
        if not refs:
            bbox = part.bbox  # leave the zero-placeholder if the part has no pins
        else:
            xs = [pins[j].pos.x for j in refs]
            ys = [pins[j].pos.y for j in refs]
            bbox = (Point(x=min(xs), y=min(ys)), Point(x=max(xs), y=max(ys)))
        patched.append(part.model_copy(update={"pin_refs": refs, "bbox": bbox}))

    return pins, patched


def _parse_nails(lines: list[str], n: int) -> list[Nail]:
    """Parse the Nails: block.

    Each line is `probe x y side net_name` where `side == 1` → `Layer.TOP`,
    else `Layer.BOTTOM`. An absent `Nails:` block with `n == 0` is valid.
    """
    if n == 0:
        return []
    try:
        idx = lines.index("Nails:")
    except ValueError as exc:
        raise MalformedHeaderError("Nails") from exc

    out: list[Nail] = []
    for raw in lines[idx + 1 : idx + 1 + n]:
        toks = raw.split()
        if len(toks) < 5:
            raise MalformedHeaderError("Nails")
        try:
            probe = int(toks[0])
            x = int(toks[1])
            y = int(toks[2])
            side = int(toks[3])
        except ValueError as exc:
            raise MalformedHeaderError("Nails") from exc
        net = toks[4]
        # Valid `side` values are 1 (top probe) and 2 (bottom probe).
        # Any other integer is treated as bottom — a conservative fallback
        # that matches observed exporter behavior and avoids hard-failing
        # on slightly-malformed files. Not raising here is intentional.
        layer = Layer.TOP if side == 1 else Layer.BOTTOM
        out.append(Nail(probe=probe, pos=Point(x=x, y=y), layer=layer, net=net))
    if len(out) != n:
        raise MalformedHeaderError("Nails")
    return out


def _backfill_empty_nets(pins: list[Pin], nails: list[Nail]) -> list[Pin]:
    """Backfill pin.net from the nail probe map (Lenovo variant).

    Only pins with `pin.net is None` AND `pin.probe in nail_map` are
    rewritten. An explicitly declared net always wins — a blank net token
    on a pin line means "look up via probe", never "overwrite".

    If two nails share the same probe number (malformed file), the last
    entry in declaration order wins. This is the natural `dict` comprehension
    behavior and callers should not rely on it.
    """
    if not nails:
        return pins
    nail_by_probe: dict[int, str] = {n.probe: n.net for n in nails}
    patched: list[Pin] = []
    for pin in pins:
        if pin.net is None and pin.probe is not None and pin.probe in nail_by_probe:
            patched.append(pin.model_copy(update={"net": nail_by_probe[pin.probe]}))
        else:
            patched.append(pin)
    return patched


# Power net heuristic — case-insensitive.
# Covered families :
#   1. Voltage magnitudes : "+3V3", "5V", "1V8", "+12V", with optional
#      qualifier suffix : "3V3_RUN", "1V8_AUDIO", "5V_EXT".
#   2. VCC family : "VCC", "VCCIO", "VCC_IO", "VCC3", "VCCIO_HDMI".
#   3. VDD family : "VDD", "VDDIO", "VDD_CORE", "VDDIO_HDMI", "VDD_SDRAM_P".
#   4. V_ prefix : "V_CORE", "V_USB", "V_CPU_CORE".
# Intentional misses : "VBAT", "VICTOR", "VOUT" (signal-like names).
# Extend on evidence, not speculation — false positives colour signal
# traces like power and are harder to diagnose than false negatives.
_POWER_RE = re.compile(
    r"^(\+?\d+V\d*(_[A-Z0-9_]+)?|VCC[A-Z0-9_]*|VDD[A-Z0-9_]*|V_[A-Z0-9_]+)$",
    re.IGNORECASE,
)

# Ground net heuristic : the five classic names in EE schematics.
# Case-insensitive. Extend only when a real board shows up with a new spelling.
_GROUND_RE = re.compile(r"^(GND|VSS|AGND|DGND|PGND)$", re.IGNORECASE)


def _derive_nets(pins: list[Pin]) -> list[Net]:
    """Group pins by net name ; flag power / ground by regex heuristic.

    Pins with `pin.net is None` are skipped. The returned list is sorted
    alphabetically by net name for deterministic test output and for
    stable serialization order on the wire.

    is_power / is_ground flags are heuristic — the `.brd` format does not
    carry net-type metadata. Callers that need stricter classification
    (e.g. PMIC output rail detection) should layer their own logic on top.
    """
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
                is_power=bool(_POWER_RE.match(name)),
                is_ground=bool(_GROUND_RE.match(name)),
            )
        )
    return out
