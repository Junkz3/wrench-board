# SPDX-License-Identifier: Apache-2.0
"""Board parsers — one implementation per file format.

Importing this package guarantees that every available concrete parser has
registered itself with the dispatch registry, so callers can use
`parser_for(path)` without worrying about import order.
"""

from api.board.parser.base import (
    BoardParser,
    BoardParserError,
    InvalidBoardFile,
    MalformedHeaderError,
    ObfuscatedFileError,
    PinPartMismatchError,
    UnsupportedFormatError,
    parser_for,
    register,
)

# Concrete parsers — importing them populates the dispatch registry.
# Add new formats here as they ship.
# See docs/superpowers/specs/2026-04-22-boardview-formats-roadmap.md.
from api.board.parser import (  # noqa: F401
    asc,
    bdv,
    brd2,
    bv,
    cad,
    cst,
    f2b,
    fz,
    gr,
    kicad,
    test_link,
    tvw,
)

__all__ = [
    "BoardParser",
    "BoardParserError",
    "InvalidBoardFile",
    "MalformedHeaderError",
    "ObfuscatedFileError",
    "PinPartMismatchError",
    "UnsupportedFormatError",
    "parser_for",
    "register",
]
