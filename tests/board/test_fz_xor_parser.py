"""Tests for the .fz parser dispatch (zlib vs xor flavours)."""

from __future__ import annotations

import struct
import zlib

import pytest

from api.board.parser.base import InvalidBoardFile
from api.board.parser.fz import FZParser
from tests.board.test_fz_xor_cipher import TEST_KEY, _encrypt


def _make_zlib_payload(text: str) -> bytes:
    body = zlib.compress(text.encode())
    return struct.pack("<I", len(text)) + body


_MIN_BOARD = (
    "A!REFDES!COMP_INSERTION_CODE!SYM_NAME!SYM_MIRROR!SYM_ROTATE\n"
    "S!R1!1!R0402!NO!0\n"
    "S!R2!1!R0402!YES!90\n"
    "A!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!PIN_X!PIN_Y!TEST_POINT!RADIUS\n"
    "S!VCC!R1!0!1!1.5!2.0!!1\n"
    "S!GND!R1!0!2!1.5!3.0!!1\n"
    "S!VCC!R2!0!1!4.0!2.0!!1\n"
    "S!GND!R2!0!2!4.0!3.0!!1\n"
)


def test_fz_parser_dispatches_zlib_flavour():
    raw = _make_zlib_payload(_MIN_BOARD)
    board = FZParser().parse(raw, file_hash="sha256:test", board_id="zlib")
    assert len(board.parts) == 2
    assert {p.refdes for p in board.parts} == {"R1", "R2"}
    assert board.source_format == "fz"


def test_fz_parser_dispatches_xor_flavour():
    plain = _make_zlib_payload(_MIN_BOARD)
    cipher = _encrypt(plain, TEST_KEY)
    board = FZParser(key=TEST_KEY).parse(cipher, file_hash="sha256:test", board_id="xor")
    assert len(board.parts) == 2
    assert {p.refdes for p in board.parts} == {"R1", "R2"}


def test_fz_parser_accepts_explicit_key():
    custom = tuple(range(44))
    parser = FZParser(key=custom)
    assert parser.key == custom


def test_fz_parser_falls_back_to_default_when_key_wrong_length(monkeypatch):
    """Explicit key with wrong length is ignored — parser uses the env-loaded
    `KEY_WORDS` or `None` when neither is available."""
    monkeypatch.delenv("WRENCH_BOARD_FZ_KEY", raising=False)
    import api.board.parser._fz_engine.cipher as cipher_mod
    monkeypatch.setattr(cipher_mod, "KEY_WORDS", None)
    parser = FZParser(key=(1, 2, 3))  # too short
    assert parser.key is None


def test_fz_parser_rejects_xor_payload_with_wrong_key():
    plain = _make_zlib_payload(_MIN_BOARD)
    cipher = _encrypt(plain, key=TEST_KEY)
    bogus_key = tuple(reversed(TEST_KEY))
    with pytest.raises(InvalidBoardFile, match="zlib container"):
        FZParser(key=bogus_key).parse(cipher, file_hash="sha256:test", board_id="bad")


def test_fz_parser_xor_with_no_key_raises_clean_error(monkeypatch):
    """An FZ-xor file encountered without any configured key surfaces a
    clean error message that points users at the env var."""
    monkeypatch.delenv("WRENCH_BOARD_FZ_KEY", raising=False)
    import api.board.parser._fz_engine.cipher as cipher_mod
    monkeypatch.setattr(cipher_mod, "KEY_WORDS", None)
    plain = _make_zlib_payload(_MIN_BOARD)
    cipher = _encrypt(plain, TEST_KEY)
    with pytest.raises(InvalidBoardFile, match="WRENCH_BOARD_FZ_KEY"):
        FZParser().parse(cipher, file_hash="sha256:test", board_id="bad")
