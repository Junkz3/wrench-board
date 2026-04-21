import importlib
from pathlib import Path

import pytest

from api.board.parser.base import (
    BoardParser,
    UnsupportedFormatError,
    parser_for,
)


def test_parser_for_unknown_extension_raises(tmp_path: Path):
    p = tmp_path / "nope.xyz"
    p.write_bytes(b"irrelevant")
    with pytest.raises(UnsupportedFormatError):
        parser_for(p)


def test_parser_for_brd_returns_brd_parser(tmp_path: Path):
    try:
        from api.board.parser.brd import BRDParser  # noqa: F401
    except ImportError:
        pytest.skip("BRDParser not yet implemented (Task 5)")
    p = tmp_path / "mini.brd"
    p.write_text("str_length: 0\nvar_data: 0 0 0 0\n")
    parser = parser_for(p)
    assert isinstance(parser, BoardParser)
    assert ".brd" in parser.extensions


def test_concrete_subclass_without_extensions_raises():
    """Concrete subclasses must declare a non-empty extensions tuple."""
    with pytest.raises(TypeError, match="extensions"):

        class _Bad(BoardParser):
            extensions = ()

            def parse(self, raw, *, file_hash, board_id):
                raise NotImplementedError


def test_importing_parser_package_populates_registry_when_brd_exists(tmp_path: Path):
    """Smoke test for the bootstrap : after `import api.board.parser`,
    `parser_for` should work without the caller importing concrete submodules."""
    try:
        import api.board.parser.brd  # noqa: F401
    except ImportError:
        pytest.skip("BRDParser not yet implemented (Task 5)")

    # Fresh import of the top-level package — simulates a caller that just did
    # `from api.board.parser import parser_for` without touching submodules.
    import api.board.parser as pkg

    importlib.reload(pkg)

    p = tmp_path / "mini.brd"
    p.write_text("str_length: 0\nvar_data: 0 0 0 0\n")
    parser = pkg.parser_for(p)
    assert ".brd" in parser.extensions


def test_parser_for_file_without_extension_raises_clearly(tmp_path: Path):
    p = tmp_path / "mystery_file_no_extension"
    p.write_bytes(b"whatever")
    with pytest.raises(UnsupportedFormatError, match="no extension"):
        parser_for(p)
