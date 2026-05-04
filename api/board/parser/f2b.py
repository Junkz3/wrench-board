"""Unisoft ProntoPLACE `.f2b` parser.

**Scope honesty.** Unisoft describes `.f2b` as "complete board save"
— a proprietary database format used by ProntoPLACE and the Place5
converter. No public format spec exists. Native `.f2b` is almost
certainly a binary container. Some `.f2b` redistributions in the
repair community appear to carry a Test_Link-shape ASCII payload
with `Outline:` / `Components:` markers (plus an `Annotations:`
block we skip — the unified `Board` model doesn't carry overlay
annotations; the runtime `bv_annotate` tool covers that path
instead). Until a binary fixture lands in `board_assets/`, this
parser handles the ASCII variant only and rejects clearly-binary
payloads with a clear hint.
"""

from __future__ import annotations

from api.board.model import Board
from api.board.parser._ascii_boardview import (
    DialectMarkers,
    looks_like_binary,
    parse_test_link_shape,
)
from api.board.parser.base import BoardParser, ObfuscatedFileError, register

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
        if looks_like_binary(raw):
            raise ObfuscatedFileError(
                "f2b: this file looks like a binary Unisoft ProntoPLACE "
                "container (non-printable byte ratio > 30%). Current parser "
                "supports the Test_Link-shape ASCII variant only."
            )
        text = raw.decode("utf-8", errors="replace")
        return parse_test_link_shape(
            text,
            markers=_F2B_MARKERS,
            source_format="f2b",
            board_id=board_id,
            file_hash=file_hash,
        )
