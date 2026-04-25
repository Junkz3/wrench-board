# SPDX-License-Identifier: Apache-2.0
"""Reusable ASCII-boardview parser for Test_Link-shape dialects.

Several vendor boardview formats share the Test_Link layout invented by
Landrex in the 1980s: a four-integer count header, followed by an outline
block, a parts block, a pins block and a nails block. Each dialect swaps
the block marker spellings (`Parts:` vs `Components:` vs `[Parts]`) but
keeps the same line grammar inside.

`parse_test_link_shape()` takes a dialect description (`TestLinkMarkers`)
and returns a `Board`. The canonical Test_Link parser in `test_link.py`
stays as-is — it predates this helper and its tests are stable. New
dialect parsers (.bv / .gr / .cad / .cst / .f2b and the post-decode
payload of .bdv / .tvw / .fz / .asc) compose this helper.

Written from scratch against the public format descriptions collected
under `docs/superpowers/specs/2026-04-25-boardview-formats-v1.md`. No
code from OpenBoardView or FlexBV was copied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from api.board.model import Layer, Nail, Net, Part, Pin, Point
from api.board.parser.base import (
    InvalidBoardFile,
    MalformedHeaderError,
    PinPartMismatchError,
)

# Power / ground net heuristics — single source of truth for every dialect.
# Matches the families documented in test_link.py (kept in sync). Lifted
# here so brd2.py and the new parsers stop crossing the test_link import.
POWER_RE = re.compile(
    r"^(\+?\d+V\d*(_[A-Z0-9_]+)?|VCC[A-Z0-9_]*|VDD[A-Z0-9_]*|V_[A-Z0-9_]+)$",
    re.IGNORECASE,
)
GROUND_RE = re.compile(r"^(GND|VSS|AGND|DGND|PGND)$", re.IGNORECASE)


@dataclass(frozen=True)
class DialectMarkers:
    """Dialect description for a Test_Link-shape boardview format.

    All marker collections are case-sensitive and matched by `startswith`
    on the stripped line. Multiple candidates are allowed — the first one
    present in the file wins. This covers OBV-era variants that spell
    the same block as `Parts:` or `Pins1:`.

    `header_count_marker` is the `startswith` prefix of the line carrying
    the four space-separated counts (outline, parts, pins, nails). For
    canonical Test_Link it is `"var_data:"`. Dialects that omit the
    header line must set it to the empty string; in that case counts are
    inferred from the block bodies (read until the next marker).
    """

    header_count_marker: str = "var_data:"
    outline_markers: tuple[str, ...] = ("Format:",)
    parts_markers: tuple[str, ...] = ("Parts:", "Pins1:")
    pins_markers: tuple[str, ...] = ("Pins:", "Pins2:")
    nails_markers: tuple[str, ...] = ("Nails:",)
    # Extra markers that, if seen, mean the current block has ended.
    # Used when `header_count_marker` is empty and we walk the file
    # section by section.
    all_block_markers: tuple[str, ...] = field(default=())


def _stripped_nonempty_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _line_matches_any(line: str, markers: tuple[str, ...]) -> bool:
    """True if `line` starts with any of the given markers.

    Matches are exact-prefix: `Parts:` matches `Parts:` or `Parts: 4`,
    but not `Parts_of_interest:`. `[Parts]` matches its own literal.
    """
    return any(line.startswith(m) for m in markers)


def _find_first_marker(lines: list[str], markers: tuple[str, ...]) -> int:
    """Return the 0-based index of the first line whose prefix matches any marker.

    Returns -1 if none is present.
    """
    for i, ln in enumerate(lines):
        if _line_matches_any(ln, markers):
            return i
    return -1


def _read_block(
    lines: list[str],
    start_idx: int,
    count: int | None,
    stop_markers: tuple[str, ...],
) -> list[str]:
    """Return up to `count` content lines starting just after `lines[start_idx]`.

    Stops early on any line whose prefix matches `stop_markers`.
    If `count is None`, reads until a stop marker or end-of-file.
    """
    out: list[str] = []
    for ln in lines[start_idx + 1 :]:
        if _line_matches_any(ln, stop_markers):
            break
        out.append(ln)
        if count is not None and len(out) >= count:
            break
    return out


def _parse_header_counts(
    lines: list[str], marker: str
) -> tuple[int, int, int, int] | None:
    """Parse a `<marker> n1 n2 n3 n4` line. Returns None if marker absent / empty.

    Raises `MalformedHeaderError` if the marker is present but its tail
    doesn't hold exactly four ints.
    """
    if not marker:
        return None
    for ln in lines:
        if ln.startswith(marker):
            rest = ln[len(marker) :].split()
            if len(rest) < 4:
                raise MalformedHeaderError(marker.rstrip(":"))
            try:
                return (int(rest[0]), int(rest[1]), int(rest[2]), int(rest[3]))
            except ValueError as exc:
                raise MalformedHeaderError(marker.rstrip(":")) from exc
    return None


def _layer_from_bits(type_layer: int) -> Layer:
    return Layer.BOTTOM if (type_layer & 0x2) else Layer.TOP


def _is_smd_from_bits(type_layer: int) -> bool:
    return bool(type_layer & 0x4)


def _parse_outline(
    lines: list[str],
    markers: tuple[str, ...],
    count: int | None,
    stop: tuple[str, ...],
) -> list[Point]:
    idx = _find_first_marker(lines, markers)
    if idx == -1:
        if count in (0, None):
            return []
        raise MalformedHeaderError(markers[0].rstrip(":"))
    body = _read_block(lines, idx, count, stop)
    out: list[Point] = []
    for ln in body:
        toks = ln.split()
        if len(toks) < 2:
            raise MalformedHeaderError(markers[0].rstrip(":"))
        try:
            out.append(Point(x=int(toks[0]), y=int(toks[1])))
        except ValueError as exc:
            raise MalformedHeaderError(markers[0].rstrip(":")) from exc
    return out


def _parse_parts(
    lines: list[str],
    markers: tuple[str, ...],
    count: int | None,
    stop: tuple[str, ...],
) -> list[tuple[str, int, int]]:
    """Parse the parts block — Test_Link variant only.

    Each line is `refdes type_layer end_of_pins [extra...]` where
    `type_layer` encodes layer via bit 0x2 and SMD via bit 0x4, and
    `end_of_pins` is the 1-based exclusive upper bound of pins owned
    by this part in the following pins block.
    """
    if count == 0:
        return []
    idx = _find_first_marker(lines, markers)
    if idx == -1:
        if count is None:
            return []
        raise MalformedHeaderError(markers[0].rstrip(":"))
    body = _read_block(lines, idx, count, stop)
    out: list[tuple[str, int, int]] = []
    for ln in body:
        toks = ln.split()
        if len(toks) < 3:
            raise MalformedHeaderError(markers[0].rstrip(":"))
        try:
            out.append((toks[0], int(toks[1]), int(toks[2])))
        except ValueError as exc:
            raise MalformedHeaderError(markers[0].rstrip(":")) from exc
    return out


def _parse_pins_and_link(
    lines: list[str],
    markers: tuple[str, ...],
    count: int | None,
    stop: tuple[str, ...],
    parts_raw: list[tuple[str, int, int]],
) -> tuple[list[Pin], list[Part]]:
    """Parse the pins block and build linked Parts.

    Pin line: `x y probe part_idx [net_name]`, `part_idx` is 1-based.
    Part ownership comes from the `end_of_pins` column of each part —
    part k owns pins in [prev_end, end_of_pins_k).
    """
    parts: list[Part] = [
        Part(
            refdes=r,
            layer=_layer_from_bits(t),
            is_smd=_is_smd_from_bits(t),
            bbox=(Point(x=0, y=0), Point(x=0, y=0)),
            pin_refs=[],
        )
        for r, t, _ in parts_raw
    ]
    if count == 0 or not parts_raw:
        return [], parts

    idx = _find_first_marker(lines, markers)
    if idx == -1:
        if count is None:
            return [], parts
        raise MalformedHeaderError(markers[0].rstrip(":"))
    body = _read_block(lines, idx, count, stop)

    # Ownership ranges from end_of_pins monotonic.
    pin_refs_by_part: list[list[int]] = []
    prev_end = 0
    for _, _, end in parts_raw:
        pin_refs_by_part.append(list(range(prev_end, end)))
        prev_end = end

    pins: list[Pin] = []
    counters = [0] * len(parts_raw)
    for i, ln in enumerate(body):
        toks = ln.split()
        if len(toks) < 4:
            raise MalformedHeaderError(markers[0].rstrip(":"))
        try:
            x = int(toks[0])
            y = int(toks[1])
            probe = int(toks[2])
            part_idx = int(toks[3])
        except ValueError as exc:
            raise MalformedHeaderError(markers[0].rstrip(":")) from exc
        net = toks[4] if len(toks) >= 5 else ""

        if part_idx < 1 or part_idx > len(parts_raw):
            raise PinPartMismatchError(i)
        owner_k = part_idx - 1
        counters[owner_k] += 1
        pins.append(
            Pin(
                part_refdes=parts[owner_k].refdes,
                index=counters[owner_k],
                pos=Point(x=x, y=y),
                net=(net or None),
                probe=(probe if probe != -99 else None),
                layer=parts[owner_k].layer,
            )
        )

    # Cross-validate the two ownership sources.
    for k, refs in enumerate(pin_refs_by_part):
        expected = parts[k].refdes
        for i in refs:
            if i >= len(pins) or pins[i].part_refdes != expected:
                raise PinPartMismatchError(i)

    # Compute bboxes + attach pin_refs.
    patched: list[Part] = []
    for k, part in enumerate(parts):
        refs = pin_refs_by_part[k]
        if not refs:
            bbox = part.bbox
        else:
            xs = [pins[j].pos.x for j in refs]
            ys = [pins[j].pos.y for j in refs]
            bbox = (Point(x=min(xs), y=min(ys)), Point(x=max(xs), y=max(ys)))
        patched.append(part.model_copy(update={"pin_refs": refs, "bbox": bbox}))
    return pins, patched


def _parse_nails(
    lines: list[str],
    markers: tuple[str, ...],
    count: int | None,
    stop: tuple[str, ...],
) -> list[Nail]:
    """Parse the nails block: `probe x y side net`."""
    if count == 0:
        return []
    idx = _find_first_marker(lines, markers)
    if idx == -1:
        if count is None:
            return []
        raise MalformedHeaderError(markers[0].rstrip(":"))
    body = _read_block(lines, idx, count, stop)
    out: list[Nail] = []
    for ln in body:
        toks = ln.split()
        if len(toks) < 5:
            raise MalformedHeaderError(markers[0].rstrip(":"))
        try:
            probe = int(toks[0])
            x = int(toks[1])
            y = int(toks[2])
            side = int(toks[3])
        except ValueError as exc:
            raise MalformedHeaderError(markers[0].rstrip(":")) from exc
        net = toks[4]
        layer = Layer.TOP if side == 1 else Layer.BOTTOM
        out.append(Nail(probe=probe, pos=Point(x=x, y=y), layer=layer, net=net))
    return out


def _backfill_nets_from_nails(pins: list[Pin], nails: list[Nail]) -> list[Pin]:
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


def derive_nets(pins: list[Pin]) -> list[Net]:
    """Group pins by net name, flag power/ground via shared regex heuristics.

    Pins with `pin.net is None` are skipped. Output is sorted alphabetically
    by net name for deterministic serialization.
    """
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


def normalize_bbox(x1: int, y1: int, x2: int, y2: int) -> tuple[Point, Point]:
    return (
        Point(x=min(x1, x2), y=min(y1, y2)),
        Point(x=max(x1, x2), y=max(y1, y2)),
    )


def parse_test_link_shape(
    text: str,
    *,
    markers: DialectMarkers,
    source_format: str,
    board_id: str,
    file_hash: str,
):
    """Parse an ASCII boardview in Test_Link shape.

    Returns a `Board`. Raises `InvalidBoardFile` if the file looks empty
    or completely unrelated (no recognizable markers), `MalformedHeaderError`
    if a block is present but malformed, `PinPartMismatchError` if a pin
    references a non-existent part.
    """
    from api.board.model import Board

    lines = _stripped_nonempty_lines(text)
    if not lines:
        raise InvalidBoardFile(f"{source_format}: empty payload")

    # Stop-marker universe: any known marker ends the current block.
    stop = (
        markers.outline_markers
        + markers.parts_markers
        + markers.pins_markers
        + markers.nails_markers
        + markers.all_block_markers
    )

    counts = _parse_header_counts(lines, markers.header_count_marker)
    n_format = counts[0] if counts else None
    n_parts = counts[1] if counts else None
    n_pins = counts[2] if counts else None
    n_nails = counts[3] if counts else None

    # Require at least one real marker — otherwise the payload is noise.
    recognized = any(
        _find_first_marker(lines, ms) != -1
        for ms in (
            markers.outline_markers,
            markers.parts_markers,
            markers.pins_markers,
            markers.nails_markers,
        )
    )
    if not recognized and counts is None:
        raise InvalidBoardFile(f"{source_format}: no recognizable block markers")

    outline = _parse_outline(lines, markers.outline_markers, n_format, stop)
    parts_raw = _parse_parts(lines, markers.parts_markers, n_parts, stop)
    pins, parts = _parse_pins_and_link(
        lines, markers.pins_markers, n_pins, stop, parts_raw
    )
    nails = _parse_nails(lines, markers.nails_markers, n_nails, stop)
    pins = _backfill_nets_from_nails(pins, nails)
    nets = derive_nets(pins)

    return Board(
        board_id=board_id,
        file_hash=file_hash,
        source_format=source_format,
        outline=outline,
        parts=parts,
        pins=pins,
        nets=nets,
        nails=nails,
    )
