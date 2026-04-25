# SPDX-License-Identifier: Apache-2.0
"""Unisoft ProntoPLACE .f2b parser — written from scratch.

ProntoPLACE (Place5 converter) saves a complete board database as a
`.f2b` file. Redistributed `.f2b` files in the repair community carry
a Test_Link-shape ASCII layout with the canonical `Parts:` / `Pins:`
/ `Nails:` markers plus optional `Annotations:` and `Outline:`
sections. The annotation block carries UI overlays that the Unisoft
tool renders on top of the board — we skip them since the unified
`Board` model does not currently expose an annotations field; the
agent's own `bv_annotate` tool covers that path at runtime instead.

Reference: public Unisoft ProntoPLACE product page and community
observations of `.f2b` redistributions. No code copied from any
external codebase.
"""

from __future__ import annotations

from api.board.model import Board
from api.board.parser._ascii_boardview import DialectMarkers, parse_test_link_shape
from api.board.parser.base import BoardParser, register

_F2B_MARKERS = DialectMarkers(
    header_count_marker="var_data:",
    outline_markers=("Format:", "Outline:"),
    parts_markers=("Parts:", "Components:"),
    pins_markers=("Pins:",),
    nails_markers=("Nails:", "TestPoints:"),
    all_block_markers=(
        "Format:",
        "Outline:",
        "Parts:",
        "Components:",
        "Pins:",
        "Nails:",
        "TestPoints:",
        "Annotations:",  # skipped on purpose — not in the unified Board model
    ),
)


@register
class F2BParser(BoardParser):
    extensions = (".f2b",)

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        text = raw.decode("utf-8", errors="replace")
        return parse_test_link_shape(
            text,
            markers=_F2B_MARKERS,
            source_format="f2b",
            board_id=board_id,
            file_hash=file_hash,
        )
