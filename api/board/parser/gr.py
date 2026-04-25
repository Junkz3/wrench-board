# SPDX-License-Identifier: Apache-2.0
"""BoardView R5.0 .gr parser — written from scratch.

BoardView R5 emits a Test_Link-shape ASCII file but spells the blocks
`Components:` / `Pins:` / `TestPoints:` instead of `Parts:` /
`Pins:` / `Nails:`. Many redistributed R5 files also carry the
canonical Test_Link spellings, so the parser accepts both in the same
dialect — the first marker present in the file wins. No code copied
from any external codebase.
"""

from __future__ import annotations

from api.board.model import Board
from api.board.parser._ascii_boardview import DialectMarkers, parse_test_link_shape
from api.board.parser.base import BoardParser, register

_GR_MARKERS = DialectMarkers(
    header_count_marker="var_data:",
    outline_markers=("Format:",),
    parts_markers=("Components:", "Parts:"),
    pins_markers=("Pins:", "Pins2:"),
    nails_markers=("TestPoints:", "Nails:"),
)


@register
class GRParser(BoardParser):
    extensions = (".gr",)

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        text = raw.decode("utf-8", errors="replace")
        return parse_test_link_shape(
            text,
            markers=_GR_MARKERS,
            source_format="gr",
            board_id=board_id,
            file_hash=file_hash,
        )
