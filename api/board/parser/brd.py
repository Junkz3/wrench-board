"""OpenBoardView .brd (Test_Link) parser — written from scratch.

The .brd format is a public line-oriented ASCII format ; the code below
reimplements the parser from the design spec (no code from OBV was copied).
See docs/superpowers/specs/2026-04-21-boardview-design.md §7 for the field
layout reference.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.board.model import Board, Nail, Net, Part, Pin, Point
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

        # Parts / Pins / Nails — implemented in later tasks ; start empty.
        parts: list[Part] = []
        pins: list[Pin] = []
        nets: list[Net] = []
        nails: list[Nail] = []

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
    """Return stripped non-empty lines."""
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _parse_header(lines: list[str]) -> _Header:
    for ln in lines:
        if ln.startswith("var_data:"):
            toks = ln.split()[1:]  # skip "var_data:"
            if len(toks) != 4:
                raise MalformedHeaderError("var_data")
            try:
                return _Header(*(int(t) for t in toks))
            except ValueError as exc:
                raise MalformedHeaderError("var_data") from exc
    raise MalformedHeaderError("var_data")


def _parse_outline(lines: list[str], n: int) -> list[Point]:
    try:
        idx = lines.index("Format:")
    except ValueError:
        if n == 0:
            return []
        raise MalformedHeaderError("Format") from None
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
