# SPDX-License-Identifier: Apache-2.0
"""Abstract base and format dispatch for board file parsers."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from pathlib import Path

from api.board.model import Board


class BoardParserError(Exception):
    """Base class for parser errors."""


class UnsupportedFormatError(BoardParserError):
    """Raised when no parser is registered for a file's extension."""


class InvalidBoardFile(BoardParserError):
    """Raised when a file is recognized but malformed or refused."""


class ObfuscatedFileError(InvalidBoardFile):
    """Raised on OBV-signature obfuscated files — we refuse to decode."""


class MissingFZKeyError(InvalidBoardFile):
    """Raised when a .fz file is uploaded without a decryption key configured.

    ASUS .fz files are XOR-scrambled with a per-vendor 44×32-bit key
    that ships separately from the file. Set `WRENCH_BOARD_FZ_KEY` in
    the environment (space-separated decimal or hex integers), or
    pass the key to `FZParser(key=...)` directly.
    """


class MalformedHeaderError(InvalidBoardFile):
    """Raised when a known block (e.g. `Parts:`, `Pins:`) is present but malformed."""

    def __init__(self, field: str):
        super().__init__(f"malformed header block: {field}")
        self.field = field


class PinPartMismatchError(InvalidBoardFile):
    """Raised when a pin references a part index that doesn't exist."""

    def __init__(self, pin_index: int):
        super().__init__(f"pin {pin_index} references an unknown part")
        self.pin_index = pin_index


class BoardParser(ABC):
    """Abstract parser. One subclass per file format."""

    extensions: tuple[str, ...] = ()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # Only enforce on concrete subclasses — intermediate ABCs are allowed to be empty.
        if not getattr(cls, "__abstractmethods__", None) and not cls.extensions:
            raise TypeError(f"{cls.__name__} must declare a non-empty 'extensions' tuple")

    def parse_file(self, path: Path) -> Board:
        raw = path.read_bytes()
        file_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
        return self.parse(raw, file_hash=file_hash, board_id=path.stem)

    @abstractmethod
    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board: ...


_REGISTRY: dict[str, type[BoardParser]] = {}


def register(parser_cls: type[BoardParser]) -> type[BoardParser]:
    """Decorator : register a parser by its extensions."""
    for ext in parser_cls.extensions:
        _REGISTRY[ext.lower()] = parser_cls
    return parser_cls


_BRD2_SNIFF = b"BRDOUT:"
_TEST_LINK_SNIFF = b"str_length:"


def _sniff_brd_variant(path: Path) -> BoardParser | None:
    """Peek the first 256 bytes of a .brd file and return the right parser instance.

    The `.brd` extension hosts two incompatible formats — Test_Link (OBV's
    original layout, declared by `str_length:` / `var_data:` header tokens)
    and BRD2 (converter output from tools like `whitequark/kicad-boardview`,
    declared by `BRDOUT:` / `NETS:` / `PARTS:` UPPERCASE block markers). A
    byte-level sniff is enough to tell them apart — both markers live in the
    file's first few lines.

    Returns `None` if neither marker is present, letting the caller fall back
    to extension dispatch or raise `UnsupportedFormatError`.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(256)
    except OSError:
        return None
    if _BRD2_SNIFF in head:
        from api.board.parser.brd2 import BRD2Parser

        return BRD2Parser()
    if _TEST_LINK_SNIFF in head:
        from api.board.parser.test_link import BRDParser

        return BRDParser()
    return None


def parser_for(path: Path) -> BoardParser:
    ext = path.suffix.lower()
    if not ext:
        raise UnsupportedFormatError(f"file has no extension: {path.name!r}")

    # `.brd` hosts two different formats (Test_Link and BRD2) — sniff the
    # content to decide which parser to hand back. Other extensions route
    # straight through the registry.
    if ext == ".brd":
        sniffed = _sniff_brd_variant(path)
        if sniffed is not None:
            return sniffed

    cls = _REGISTRY.get(ext)
    if cls is None:
        raise UnsupportedFormatError(f"no parser registered for extension {ext!r}")
    return cls()
