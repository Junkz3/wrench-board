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
try:
    from api.board.parser import test_link  # noqa: F401
except ImportError:
    # BRDParser not yet implemented. Safe during bootstrap.
    pass

try:
    from api.board.parser import kicad  # noqa: F401
except ImportError:
    pass

# Stub parsers — file exists, registry is wired, parse() raises
# NotImplementedError until the format is actually supported.
# See docs/superpowers/specs/2026-04-22-boardview-formats-roadmap.md
from api.board.parser import asc, bdv, bv, cad, cst, f2b, fz, gr, tvw  # noqa: F401, E402

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
