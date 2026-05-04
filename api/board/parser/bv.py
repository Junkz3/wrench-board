"""ATE BoardView `.bv` parser.

**Scope honesty.** ATE BoardView 1.5.0 has no published format
specification and is documented in the wild only as "drag-and-drop"
to the viewer. Some redistributions in repair forums carry a
Test_Link-shape ASCII payload (an optional `BoardView <version>`
banner followed by `var_data:` / `Format:` / `Parts:` / `Pins:` /
`Nails:` blocks); native ATE output is more likely a binary
container. Until a binary fixture lands in `board_assets/`, this
parser handles the ASCII variant only. Files whose first 2 KB look
binary trip a clear `ObfuscatedFileError` rather than silently
producing an empty Board.
"""

from __future__ import annotations

from api.board.model import Board
from api.board.parser._ascii_boardview import (
    DialectMarkers,
    looks_like_binary,
    parse_test_link_shape,
)
from api.board.parser.base import BoardParser, ObfuscatedFileError, register


@register
class BVParser(BoardParser):
    extensions = (".bv",)

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        if looks_like_binary(raw):
            raise ObfuscatedFileError(
                "bv: this file looks like a binary ATE BoardView container "
                "(non-printable byte ratio > 30%). Current parser supports "
                "the Test_Link-shape ASCII variant only. See "
                "docs/superpowers/specs/2026-04-25-boardview-formats-v1.md."
            )
        text = raw.decode("utf-8", errors="replace")
        return parse_test_link_shape(
            text,
            markers=DialectMarkers(),
            source_format="bv",
            board_id=board_id,
            file_hash=file_hash,
        )
