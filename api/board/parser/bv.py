# SPDX-License-Identifier: Apache-2.0
"""ATE BoardView .bv parser — written from scratch.

ATE ships a drag-and-drop viewer whose output follows the Test_Link
ASCII grammar: an optional single-line `BoardView <version>` banner,
then the classic `var_data: n1 n2 n3 n4` counts, then `Format:`,
`Parts:`, `Pins:`, `Nails:` blocks. The banner is informational only
and is safely ignored by the helper since it does not match any
block marker.

Reference for the shape: public format catalog
https://gist.github.com/vyach-vasiliev/35d610e14c40b4060f5d929ac70746a3
and OBV-community format notes. No code copied from any external
codebase.
"""

from __future__ import annotations

from api.board.model import Board
from api.board.parser._ascii_boardview import DialectMarkers, parse_test_link_shape
from api.board.parser.base import BoardParser, register


@register
class BVParser(BoardParser):
    extensions = (".bv",)

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        text = raw.decode("utf-8", errors="replace")
        return parse_test_link_shape(
            text,
            markers=DialectMarkers(),
            source_format="bv",
            board_id=board_id,
            file_hash=file_hash,
        )
