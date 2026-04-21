"""OpenBoardView .brd (Test_Link) parser — written from scratch.

Reference for the format : the OpenBoardView project documents the .brd
Test_Link layout. The code below is a clean-room reimplementation from
that format specification — no code from the OBV codebase was copied
(per hackathon hard rule #1, Apache 2.0).

See docs/superpowers/specs/2026-04-21-boardview-design.md §7 for the
field layout summary used here.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.board.model import Board, Layer, Nail, Net, Part, Pin, Point
from api.board.parser.base import (
    BoardParser,
    InvalidBoardFile,
    MalformedHeaderError,
    ObfuscatedFileError,
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

        # Placeholders populated by later tasks :
        #   pins    — Task 7 (Pins block + part linkage + bbox)
        #   nails   — Task 8 (Nails block + dangling-net backfill)
        #   nets    — Task 9 (derived from pins)
        # Note : bbox will be patched in Task 7 once pin coordinates are known.
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
        pins: list[Pin] = []
        nails: list[Nail] = []
        nets: list[Net] = []

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
