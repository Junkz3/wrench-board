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
    from api.board.parser.test_link import BRDParser  # noqa: F401

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


def test_parser_for_dispatches_to_brd2_on_content(tmp_path: Path):
    """A .brd file with BRDOUT: must route to BRD2Parser, not BRDParser."""
    from api.board.parser.brd2 import BRD2Parser

    f = tmp_path / "mnt.brd"
    f.write_text("0\nBRDOUT: 0 0 0\n\nNETS: 0\n\nPARTS: 0\n\nPINS: 0\n\nNAILS: 0\n")
    p = parser_for(f)
    assert isinstance(p, BRD2Parser)


def test_parser_for_dispatches_to_test_link_on_content(tmp_path: Path):
    """A .brd file with str_length: still routes to the Test_Link parser."""
    from api.board.parser.test_link import BRDParser

    f = tmp_path / "legacy.brd"
    f.write_text("str_length: 0\nvar_data: 0 0 0 0\n")
    p = parser_for(f)
    assert isinstance(p, BRDParser)


def test_parser_for_mnt_reform_fixture_routes_to_brd2():
    """End-to-end : the committed MNT Reform fixture routes to BRD2Parser."""
    from api.board.parser.brd2 import BRD2Parser

    repo_root = Path(__file__).resolve().parents[2]
    fixture = repo_root / "board_assets" / "mnt-reform-motherboard.brd"
    p = parser_for(fixture)
    assert isinstance(p, BRD2Parser)
