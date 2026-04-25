# SPDX-License-Identifier: Apache-2.0
"""Generic BoardViewer 2.1.0.8 .cad parser — written from scratch.

The `.cad` extension is an umbrella used by the generic BoardViewer
2.1.0.8 distribution. Files in the wild carry either the Test_Link
ASCII grammar (lowercase `Parts:` / `Pins:` / `Nails:` or uppercase
`PARTS:` / `PINS:` / `NAILS:`) or the BRD2-shape with a `BRDOUT:`
header. This parser sniffs for the BRD2 marker and delegates to the
existing `BRD2Parser`; otherwise it falls back to the shared
Test_Link-shape helper with both-case markers. Source-format tag is
always `"cad"` in the emitted Board so the frontend and downstream
pipeline know which upload produced the artefact. No code copied
from any external codebase.
"""

from __future__ import annotations

from api.board.model import Board
from api.board.parser._ascii_boardview import DialectMarkers, parse_test_link_shape
from api.board.parser.base import BoardParser, register
from api.board.parser.brd2 import BRD2Parser

_CAD_MARKERS = DialectMarkers(
    header_count_marker="var_data:",
    outline_markers=("Format:", "FORMAT:"),
    parts_markers=("Parts:", "PARTS:", "Pins1:"),
    pins_markers=("Pins:", "PINS:", "Pins2:"),
    nails_markers=("Nails:", "NAILS:"),
)


@register
class CADParser(BoardParser):
    extensions = (".cad",)

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        text = raw.decode("utf-8", errors="replace")
        if "BRDOUT:" in text[:1024]:
            board = BRD2Parser().parse(raw, file_hash=file_hash, board_id=board_id)
            return board.model_copy(update={"source_format": "cad"})
        return parse_test_link_shape(
            text,
            markers=_CAD_MARKERS,
            source_format="cad",
            board_id=board_id,
            file_hash=file_hash,
        )
