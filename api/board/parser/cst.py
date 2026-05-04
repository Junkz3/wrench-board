"""IBM Lenovo Card Analysis Support Tool `.cst` parser.

**Scope honesty.** Castw v3.32 (IBM's internal ICT viewer for
ThinkPad service dumps) is a 1990s-era tool with no published
file-format spec. This parser assumes a Test_Link-shape ASCII
variant with INI-style bracketed section headers (`[Format]`,
`[Components]`, `[Pins]`, `[Nails]`). Real Castw files in the field
are more likely a binary container. Until a binary fixture lands in
`board_assets/`, this stays best-effort: the parser detects clearly
binary payloads and rejects them with a clear hint instead of
silently emitting an empty Board.
"""

from __future__ import annotations

from api.board.model import Board
from api.board.parser._ascii_boardview import (
    DialectMarkers,
    looks_like_binary,
    parse_test_link_shape,
)
from api.board.parser.base import BoardParser, ObfuscatedFileError, register

_CST_MARKERS = DialectMarkers(
    header_count_marker="",  # no var_data — counts are inferred per block
    outline_markers=("[Format]", "[Outline]"),
    parts_markers=("[Components]", "[Parts]"),
    pins_markers=("[Pins]",),
    nails_markers=("[Nails]", "[TestPoints]"),
    all_block_markers=(
        "[Format]",
        "[Outline]",
        "[Components]",
        "[Parts]",
        "[Pins]",
        "[Nails]",
        "[TestPoints]",
    ),
)


@register
class CSTParser(BoardParser):
    extensions = (".cst",)

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        if looks_like_binary(raw):
            raise ObfuscatedFileError(
                "cst: this file looks like a binary IBM Castw container "
                "(non-printable byte ratio > 30%). Current parser supports "
                "the Test_Link-shape ASCII variant only."
            )
        text = raw.decode("utf-8", errors="replace")
        return parse_test_link_shape(
            text,
            markers=_CST_MARKERS,
            source_format="cst",
            board_id=board_id,
            file_hash=file_hash,
        )
