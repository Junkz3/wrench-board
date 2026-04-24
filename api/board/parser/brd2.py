# SPDX-License-Identifier: Apache-2.0
"""OpenBoardView BRD2 parser — written from scratch (Apache 2.0).

Parses BRD2-format files produced by converters like `whitequark/kicad-boardview`.
Distinct from Test_Link : block markers are UPPERCASE (`BRDOUT:`, `NETS:`,
`PARTS:`, `PINS:`, `NAILS:`) and PARTS lines carry the bbox inline. PART lines
also expose a cumulative `first_pin_0_based` index that drives pin ownership —
BRD2 has no per-part `end_of_pins` field ; ownership is derived from the
cumulative `first_pin` indices of successive parts.

See docs/superpowers/specs/2026-04-21-boardview-design.md §7 for layout overview.
"""

from __future__ import annotations

from api.board.model import Board, Layer, Nail, Net, Part, Pin, Point
from api.board.parser.base import (
    BoardParser,
    InvalidBoardFile,
    MalformedHeaderError,
    register,
)
from api.board.parser.test_link import _GROUND_RE, _POWER_RE


@register
class BRD2Parser(BoardParser):
    # Synthetic extension tag : real BRD2 files end in `.brd`, same as
    # Test_Link. Content-based dispatch (see `base.parser_for`) routes real
    # `.brd` files to this parser when a `BRDOUT:` marker is present. The
    # `.brd2` tag here only keeps the registry non-colliding.
    extensions = (".brd2",)

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        text = raw.decode("utf-8", errors="replace")
        if "BRDOUT:" not in text:
            raise InvalidBoardFile("not a BRD2 file : no BRDOUT: header")
        lines = text.splitlines()

        outline = _parse_brdout(lines)
        net_names = _parse_nets(lines)
        parts_raw = _parse_parts(lines)
        pins_raw = _parse_pins(lines)
        nails_raw = _parse_nails(lines)

        # Validate pin net_id range before linking.
        for _, _, net_id, _ in pins_raw:
            if net_id < 0 or net_id > len(net_names):
                raise MalformedHeaderError("PINS")

        parts, pins = _link_pins_to_parts(parts_raw, pins_raw, net_names)
        nets = _derive_nets_from_names(net_names, pins)
        nails = _resolve_nails(nails_raw, net_names)

        return Board(
            board_id=board_id,
            file_hash=file_hash,
            source_format="brd2",
            outline=outline,
            parts=parts,
            pins=pins,
            nets=nets,
            nails=nails,
        )


# ---------------------------------------------------------------------------
# Block locator
# ---------------------------------------------------------------------------


def _find_block(lines: list[str], marker: str) -> tuple[int, int]:
    """Locate a block whose header line starts with `marker` (e.g. `"BRDOUT:"`).

    Returns `(header_idx, count)` where `count` is the integer that immediately
    follows the marker on the same line. For `BRDOUT:` the same line carries
    three ints (count, width, height) ; callers that need width/height parse
    the header line themselves and ignore this helper.

    Raises `MalformedHeaderError(marker.rstrip(':'))` if the marker is absent
    or the count is not an integer.
    """
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if stripped.startswith(marker):
            rest = stripped[len(marker) :].split()
            if not rest:
                raise MalformedHeaderError(marker.rstrip(":"))
            try:
                return i, int(rest[0])
            except ValueError as exc:
                raise MalformedHeaderError(marker.rstrip(":")) from exc
    raise MalformedHeaderError(marker.rstrip(":"))


def _iter_block_lines(lines: list[str], header_idx: int, count: int) -> list[str]:
    """Return the `count` non-empty lines that follow `lines[header_idx]`.

    Skips blank lines and stops when `count` non-empty lines have been
    collected or the next block marker / EOF is reached. Raises
    `MalformedHeaderError` if fewer than `count` non-empty lines are available.
    """
    out: list[str] = []
    for raw in lines[header_idx + 1 :]:
        stripped = raw.strip()
        if not stripped:
            if len(out) >= count:
                break
            continue
        # Stop on the next block header — the current block is shorter than declared.
        if _looks_like_block_header(stripped) and len(out) < count:
            break
        out.append(stripped)
        if len(out) >= count:
            break
    if len(out) < count:
        raise MalformedHeaderError(lines[header_idx].split(":", 1)[0])
    return out


_BLOCK_MARKERS = ("BRDOUT:", "NETS:", "PARTS:", "PINS:", "NAILS:")


def _looks_like_block_header(line: str) -> bool:
    return any(line.startswith(m) for m in _BLOCK_MARKERS)


# ---------------------------------------------------------------------------
# Block parsers
# ---------------------------------------------------------------------------


def _parse_brdout(lines: list[str]) -> list[Point]:
    """Parse the `BRDOUT: <n> <width> <height>` block.

    `n == 0` is legal — BRD2 can omit outline points entirely.
    """
    header_idx, n = _find_block(lines, "BRDOUT:")
    # Validate the rest of the header line : it must be `<n> <w> <h>`, all ints.
    header_rest = lines[header_idx].strip()[len("BRDOUT:") :].split()
    if len(header_rest) != 3:
        raise MalformedHeaderError("BRDOUT")
    try:
        int(header_rest[1])
        int(header_rest[2])
    except ValueError as exc:
        raise MalformedHeaderError("BRDOUT") from exc

    if n == 0:
        return []

    body = _iter_block_lines(lines, header_idx, n)
    pts: list[Point] = []
    for raw in body:
        toks = raw.split()
        if len(toks) != 2:
            raise MalformedHeaderError("BRDOUT")
        try:
            pts.append(Point(x=int(toks[0]), y=int(toks[1])))
        except ValueError as exc:
            raise MalformedHeaderError("BRDOUT") from exc
    return pts


def _parse_nets(lines: list[str]) -> list[str]:
    """Parse the `NETS: <n>` block into a list of net names, indexed 0-based.

    Net IDs in PINS/NAILS are 1-based ; caller must subtract 1 before lookup.
    A `net_id == 0` means "no net" and must not reach this list.
    """
    header_idx, n = _find_block(lines, "NETS:")
    if n == 0:
        return []
    body = _iter_block_lines(lines, header_idx, n)
    names: list[str] = []
    for raw in body:
        parts = raw.split(maxsplit=1)
        if len(parts) < 2:
            raise MalformedHeaderError("NETS")
        try:
            int(parts[0])
        except ValueError as exc:
            raise MalformedHeaderError("NETS") from exc
        names.append(parts[1].strip())
    return names


# Raw part tuple : (refdes, x1, y1, x2, y2, first_pin_0_based, side)
_PartRaw = tuple[str, int, int, int, int, int, int]


def _parse_parts(lines: list[str]) -> list[_PartRaw]:
    """Parse the `PARTS: <n>` block. Each line is `refdes x1 y1 x2 y2 first_pin side`."""
    header_idx, n = _find_block(lines, "PARTS:")
    if n == 0:
        return []
    body = _iter_block_lines(lines, header_idx, n)
    out: list[_PartRaw] = []
    for raw in body:
        toks = raw.split()
        if len(toks) != 7:
            raise MalformedHeaderError("PARTS")
        try:
            refdes = toks[0]
            x1 = int(toks[1])
            y1 = int(toks[2])
            x2 = int(toks[3])
            y2 = int(toks[4])
            first_pin = int(toks[5])
            side = int(toks[6])
        except ValueError as exc:
            raise MalformedHeaderError("PARTS") from exc
        out.append((refdes, x1, y1, x2, y2, first_pin, side))
    return out


# Raw pin tuple : (x, y, net_id_1_based, side)
_PinRaw = tuple[int, int, int, int]


def _parse_pins(lines: list[str]) -> list[_PinRaw]:
    """Parse the `PINS: <n>` block. Each line is `x y net_id side`."""
    header_idx, n = _find_block(lines, "PINS:")
    if n == 0:
        return []
    body = _iter_block_lines(lines, header_idx, n)
    out: list[_PinRaw] = []
    for raw in body:
        toks = raw.split()
        if len(toks) != 4:
            raise MalformedHeaderError("PINS")
        try:
            x = int(toks[0])
            y = int(toks[1])
            net_id = int(toks[2])
            side = int(toks[3])
        except ValueError as exc:
            raise MalformedHeaderError("PINS") from exc
        out.append((x, y, net_id, side))
    return out


# Raw nail tuple : (probe, x, y, net_id_1_based, side)
_NailRaw = tuple[int, int, int, int, int]


def _parse_nails(lines: list[str]) -> list[_NailRaw]:
    """Parse the `NAILS: <n>` block. Each line is `probe x y net_id side`.

    `NAILS: 0` is legal (no test points declared).
    """
    header_idx, n = _find_block(lines, "NAILS:")
    if n == 0:
        return []
    body = _iter_block_lines(lines, header_idx, n)
    out: list[_NailRaw] = []
    for raw in body:
        toks = raw.split()
        if len(toks) != 5:
            raise MalformedHeaderError("NAILS")
        try:
            probe = int(toks[0])
            x = int(toks[1])
            y = int(toks[2])
            net_id = int(toks[3])
            side = int(toks[4])
        except ValueError as exc:
            raise MalformedHeaderError("NAILS") from exc
        out.append((probe, x, y, net_id, side))
    return out


# ---------------------------------------------------------------------------
# Linking and derivation
# ---------------------------------------------------------------------------


def _layer_from_side(side: int) -> Layer:
    """BRD2 convention : 1 = TOP, 2 = BOTTOM. Any other value falls back to TOP.

    We don't raise on unknown sides — some exporters emit 0 or >2 for parts
    placed on internal layers. Conservative fallback to TOP matches OBV behavior.
    """
    return Layer.TOP if side != 2 else Layer.BOTTOM


def _link_pins_to_parts(
    parts_raw: list[_PartRaw],
    pins_raw: list[_PinRaw],
    net_names: list[str],
) -> tuple[list[Part], list[Pin]]:
    """Resolve each pin to its owning part via the cumulative `first_pin` indices.

    Part `k` owns pins in range [first_pin_k, first_pin_{k+1}). The last part
    owns pins in [first_pin_last, len(pins_raw)). Pin.index is 1-based within
    the owning part ; Pin.net is the resolved net name or None for net_id == 0.
    """
    n_pins = len(pins_raw)
    parts: list[Part] = []
    pins: list[Pin] = []

    if not parts_raw:
        return parts, pins

    # Build ownership ranges.
    first_pins = [p[5] for p in parts_raw]
    # Validate monotonic non-decreasing within bounds.
    for k, fp in enumerate(first_pins):
        if fp < 0 or fp > n_pins:
            raise MalformedHeaderError("PARTS")
        if k > 0 and fp < first_pins[k - 1]:
            raise MalformedHeaderError("PARTS")

    ranges: list[tuple[int, int]] = []
    for k in range(len(parts_raw)):
        start = first_pins[k]
        end = first_pins[k + 1] if k + 1 < len(parts_raw) else n_pins
        ranges.append((start, end))

    for k, (refdes, x1, y1, x2, y2, _first_pin, side) in enumerate(parts_raw):
        layer = _layer_from_side(side)
        start, end = ranges[k]
        pin_refs = list(range(start, end))

        # Emit pins for this part, assigning 1-based indices.
        for local_idx, i in enumerate(pin_refs, start=1):
            px, py, net_id, pin_side = pins_raw[i]
            if net_id == 0:
                net_name: str | None = None
            else:
                # 1-based -> 0-based lookup. Range already validated above.
                net_name = net_names[net_id - 1]
            pins.append(
                Pin(
                    part_refdes=refdes,
                    index=local_idx,
                    pos=Point(x=px, y=py),
                    net=net_name,
                    probe=None,  # BRD2 PINS block carries no probe — only NAILS does.
                    layer=_layer_from_side(pin_side),
                )
            )

        # Normalize bbox to (min, max) — whitequark's pcbnew2boardview emits
        # y1 > y2 after its global Y-flip without renormalizing the corners,
        # and `Part.bbox` is documented as `(min, max)` in api/board/model.py.
        bbox_lo = Point(x=min(x1, x2), y=min(y1, y2))
        bbox_hi = Point(x=max(x1, x2), y=max(y1, y2))
        parts.append(
            Part(
                refdes=refdes,
                layer=layer,
                # BRD2 carries no SMD flag. Default to True — most BRD2 exports
                # come from KiCad SMD-first designs. Callers needing hard
                # through-hole classification must use schematic / footprint data.
                is_smd=True,
                bbox=(bbox_lo, bbox_hi),
                pin_refs=pin_refs,
            )
        )

    return parts, pins


def _derive_nets_from_names(net_names: list[str], pins: list[Pin]) -> list[Net]:
    """Build `Net` objects from the declared NETS list, grouping pins by name.

    Returned list is sorted alphabetically by net name for deterministic output,
    matching the Test_Link parser's behavior. Nets with no pins are still
    emitted — they exist in the file, and callers may want to surface them
    (e.g. unconnected pads).
    """
    refs_by_name: dict[str, list[int]] = {name: [] for name in net_names}
    for i, pin in enumerate(pins):
        if pin.net is None:
            continue
        # pin.net was already resolved from net_names, so it must be present.
        refs_by_name.setdefault(pin.net, []).append(i)

    out: list[Net] = []
    for name in sorted(refs_by_name):
        out.append(
            Net(
                name=name,
                pin_refs=refs_by_name[name],
                is_power=bool(_POWER_RE.match(name)),
                is_ground=bool(_GROUND_RE.match(name)),
            )
        )
    return out


def _resolve_nails(nails_raw: list[_NailRaw], net_names: list[str]) -> list[Nail]:
    """Resolve each nail's `net_id` to the corresponding net name string."""
    out: list[Nail] = []
    for probe, x, y, net_id, side in nails_raw:
        if net_id < 0 or net_id > len(net_names):
            raise MalformedHeaderError("NAILS")
        if net_id == 0:
            # A nail with no net is pathological but not impossible. Emit an
            # empty-string net rather than raising — the downstream model
            # requires a `str`, not Optional.
            net_name = ""
        else:
            net_name = net_names[net_id - 1]
        out.append(
            Nail(
                probe=probe,
                pos=Point(x=x, y=y),
                layer=_layer_from_side(side),
                net=net_name,
            )
        )
    return out
