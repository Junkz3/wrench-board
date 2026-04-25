# SPDX-License-Identifier: Apache-2.0
"""IBM Lenovo Card Analysis Support Tool .cst parser — written from scratch.

Castw v3.32 (IBM's internal ICT viewer, used on ThinkPad service
dumps) emits ASCII with INI-style bracketed section headers:
`[Format]` for the outline, `[Components]` for parts, `[Pins]` for
pins, `[Nails]` for test points. Counts are not declared up-front —
each block runs until the next section header or end-of-file. The
Test_Link line grammar still holds inside every section.

Reference for the shape: Castw redistributions observed in the
community ThinkPad repair forums. No code copied from any external
codebase.
"""

from __future__ import annotations

from api.board.model import Board
from api.board.parser._ascii_boardview import DialectMarkers, parse_test_link_shape
from api.board.parser.base import BoardParser, register

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
        text = raw.decode("utf-8", errors="replace")
        return parse_test_link_shape(
            text,
            markers=_CST_MARKERS,
            source_format="cst",
            board_id=board_id,
            file_hash=file_hash,
        )
