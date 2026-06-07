"""BoardView R5.0 `.gr` parser.

**Scope honesty.** BoardView R5.0 has no published file-format spec.
This parser assumes a Test_Link-shape ASCII variant with
`Components:` / `Pins:` / `TestPoints:` markers (and accepts the
canonical `Parts:` / `Nails:` spellings as fallback). If a real `.gr`
file lands binary instead — likely, given the era — the parser
trips a clear `ObfuscatedFileError` rather than producing nonsense.
Until a binary fixture lands in `board_assets/`, this stays
best-effort.
"""

from __future__ import annotations

from api.board.model import Board
from api.board.parser._ascii_boardview import (
    DialectMarkers,
    looks_like_binary,
    parse_test_link_shape,
)
from api.board.parser.base import BoardParser, ObfuscatedFileError, register

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
        if looks_like_binary(raw):
            raise ObfuscatedFileError(
                "gr: this file looks like a binary BoardView R5.0 container "
                "(non-printable byte ratio > 30%). Current parser supports "
                "the Test_Link-shape ASCII variant only."
            )
        text = raw.decode("utf-8", errors="replace")
        # Some `.gr` files in the wild are actually BRD2-format payloads (the
        # UPPERCASE `BRDOUT:` block grammar), sometimes preceded by a one-line
        # vendor title. Real example: `vendor-board boardview.gr`.
        # The Test_Link-shape grammar below can't read those, so delegate to
        # the BRD2 parser when the BRD2 outline marker is present — but keep
        # `source_format="gr"` so the on-disk extension stays the source of truth.
        if "BRDOUT:" in text:
            from api.board.parser.brd2 import BRD2Parser

            board = BRD2Parser().parse(raw, file_hash=file_hash, board_id=board_id)
            return board.model_copy(update={"source_format": "gr"})
        return parse_test_link_shape(
            text,
            markers=_GR_MARKERS,
            source_format="gr",
            board_id=board_id,
            file_hash=file_hash,
        )
