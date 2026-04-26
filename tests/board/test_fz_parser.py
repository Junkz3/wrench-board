"""ASUS .fz parser — missing-key path + round-trip on a synthetic key.

We cannot ship real ASUS `.fz` samples (proprietary). The tests cover:
1. A clean `MissingFZKeyError` when no key is configured.
2. A symmetric round-trip with a user-supplied dummy key proving the
   descrambling structure is sound.
3. Dispatcher wiring.
4. Env-var key loading + rejection of malformed keys.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api.board.parser.base import MissingFZKeyError, parser_for
from api.board.parser.fz import FZParser, _decrypt, _encrypt

DUMMY_KEY: tuple[int, ...] = tuple(range(1, 45))  # 44 words


def test_dispatches_fz_extension(tmp_path: Path):
    f = tmp_path / "demo.fz"
    f.write_bytes(b"anything")
    assert isinstance(parser_for(f), FZParser)


def test_missing_key_raises_with_helpful_message(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("WRENCH_BOARD_FZ_KEY", raising=False)
    f = tmp_path / "bad.fz"
    f.write_bytes(b"any payload")
    with pytest.raises(MissingFZKeyError) as exc:
        FZParser().parse_file(f)
    msg = str(exc.value)
    assert "WRENCH_BOARD_FZ_KEY" in msg
    assert "44" in msg


@pytest.mark.parametrize(
    "text",
    [
        "var_data: 0 1 1 0\nParts:\nR1 5 1\nPins:\n0 0 -99 1 +3V3\n",
        "hello world\n",
        "A" * 300,  # longer than the 16-byte window
    ],
)
def test_encrypt_decrypt_roundtrip(text: str):
    cipher = _encrypt(text.encode(), DUMMY_KEY)
    plain = _decrypt(cipher, DUMMY_KEY)
    assert plain.decode("utf-8") == text


def test_parses_roundtripped_test_link_payload(tmp_path: Path):
    """With a key set, a plaintext-encrypted Test_Link payload must
    decrypt + parse end-to-end."""
    plaintext = (
        "var_data: 0 1 1 0\n"
        "Parts:\n"
        "R1 5 1\n"
        "Pins:\n"
        "100 100 -99 1 +3V3\n"
    )
    cipher = _encrypt(plaintext.encode(), DUMMY_KEY)
    f = tmp_path / "demo.fz"
    f.write_bytes(cipher)
    board = FZParser(key=DUMMY_KEY).parse_file(f)
    assert board.source_format == "fz"
    assert [p.refdes for p in board.parts] == ["R1"]
    assert len(board.pins) == 1


def test_env_var_key_is_loaded(tmp_path: Path, monkeypatch):
    """When WRENCH_BOARD_FZ_KEY holds 44 space-separated ints, the default
    FZParser() picks it up."""
    key_str = " ".join(str(w) for w in DUMMY_KEY)
    monkeypatch.setenv("WRENCH_BOARD_FZ_KEY", key_str)

    plaintext = "var_data: 0 1 1 0\nParts:\nR1 5 1\nPins:\n0 0 -99 1 +3V3\n"
    f = tmp_path / "demo.fz"
    f.write_bytes(_encrypt(plaintext.encode(), DUMMY_KEY))
    # No explicit key arg → env var wins.
    board = FZParser().parse_file(f)
    assert [p.refdes for p in board.parts] == ["R1"]


def test_malformed_env_var_is_ignored_parser_still_raises_missing(monkeypatch, tmp_path):
    """A bad env var (wrong count, non-numeric) should not half-configure
    the parser — it must behave exactly like no key was set."""
    monkeypatch.setenv("WRENCH_BOARD_FZ_KEY", "1 2 3")  # only 3 words
    f = tmp_path / "x.fz"
    f.write_bytes(b"payload")
    with pytest.raises(MissingFZKeyError):
        FZParser().parse_file(f)

    monkeypatch.setenv("WRENCH_BOARD_FZ_KEY", "not numbers at all")
    with pytest.raises(MissingFZKeyError):
        FZParser().parse_file(f)
