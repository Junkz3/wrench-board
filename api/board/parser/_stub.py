# SPDX-License-Identifier: Apache-2.0
"""Factory for stub parsers awaiting implementation.

Each concrete boardview format lives in its own file under `api/board/parser/`
(one-file-one-format keeps the registry easy to scan and avoids merge conflicts
when different contributors add different formats). For formats we don't yet
support, the file still exists so the registry dispatches cleanly — uploading
a `.fz` file yields a clear `501 Not Implemented`, not a confusing
`415 Unsupported Format`.

See docs/superpowers/specs/2026-04-22-boardview-formats-roadmap.md.
"""

from __future__ import annotations

from api.board.model import Board
from api.board.parser.base import BoardParser


def make_stub_parser(extension: str, format_name: str) -> type[BoardParser]:
    """Build a `BoardParser` subclass that declares `extension` and raises on parse."""

    class StubParser(BoardParser):
        extensions = (extension,)

        def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
            raise NotImplementedError(
                f"Parser for {format_name} ({extension}) is not yet implemented — "
                "see docs/superpowers/specs/2026-04-22-boardview-formats-roadmap.md"
            )

    StubParser.__name__ = f"Stub_{extension.lstrip('.').upper()}Parser"
    StubParser.__qualname__ = StubParser.__name__
    return StubParser
