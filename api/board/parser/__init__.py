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
    from api.board.parser import brd  # noqa: F401
except ImportError:
    # BRDParser not yet implemented (pending Task 5). Safe during bootstrap.
    pass


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
