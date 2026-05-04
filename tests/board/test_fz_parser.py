"""High-level `.fz` parser tests — dispatch, env-var key, error surface.

Cipher-internal round-trips and the cipher-level edge cases live in the
sibling `test_fz_xor_cipher.py` / `test_fz_xor_parser.py` modules.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api.board.parser.base import InvalidBoardFile, parser_for
from api.board.parser.fz import FZParser
from tests.board.test_fz_xor_cipher import TEST_KEY, _encrypt
from tests.board.test_fz_xor_parser import _MIN_BOARD, _make_zlib_payload


def test_dispatches_fz_extension(tmp_path: Path):
    f = tmp_path / "demo.fz"
    f.write_bytes(b"anything")
    assert isinstance(parser_for(f), FZParser)


def test_xor_payload_with_garbage_raises_invalid(tmp_path: Path, monkeypatch):
    """A non-zlib payload that doesn't decrypt to a zlib container must
    raise `InvalidBoardFile`, not return a partial Board."""
    monkeypatch.setenv("WRENCH_BOARD_FZ_KEY", " ".join(str(w) for w in TEST_KEY))
    f = tmp_path / "bad.fz"
    f.write_bytes(b"any payload that's not encrypted properly")
    with pytest.raises(InvalidBoardFile, match="zlib container"):
        FZParser(key=TEST_KEY).parse_file(f)


def test_env_var_key_is_loaded(tmp_path: Path, monkeypatch):
    """When `WRENCH_BOARD_FZ_KEY` holds 44 space-separated ints, the
    cipher module picks it up at import time."""
    custom_key = tuple(range(1, 45))
    key_str = " ".join(str(w) for w in custom_key)
    monkeypatch.setenv("WRENCH_BOARD_FZ_KEY", key_str)

    # Reload the loader to re-read env.
    from api.board.parser._fz_engine.cipher import _load_key_words
    loaded = _load_key_words()
    assert loaded == custom_key


def test_malformed_env_var_yields_no_key(monkeypatch):
    """A bad env var (wrong count or non-numeric) must not half-configure
    the loader — `_load_key_words` returns None so callers raise a clean
    error rather than running with a silently-truncated key."""
    monkeypatch.setenv("WRENCH_BOARD_FZ_KEY", "1 2 3")  # only 3 words
    from api.board.parser._fz_engine.cipher import _load_key_words
    assert _load_key_words() is None

    monkeypatch.setenv("WRENCH_BOARD_FZ_KEY", "not numbers at all")
    assert _load_key_words() is None


def test_zlib_flavour_does_not_invoke_cipher(tmp_path: Path):
    """An FZ-zlib file (zlib magic at offset 4) must parse even when the
    parser has no key configured at all — the dispatcher must skip the
    XOR path entirely."""
    plain = _make_zlib_payload(_MIN_BOARD)
    f = tmp_path / "zlib.fz"
    f.write_bytes(plain)

    parser = FZParser(key=(0,) * 44)  # deliberately useless key
    board = parser.parse_file(f)
    assert {p.refdes for p in board.parts} == {"R1", "R2"}
