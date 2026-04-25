"""Tebo .tvw parser — rotation cipher round-trip + happy-path parse."""

from __future__ import annotations

from pathlib import Path

import pytest

from api.board.parser.base import InvalidBoardFile, parser_for
from api.board.parser.tvw import TVWParser, _deobfuscate, _obfuscate

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_dispatches_tvw_extension(tmp_path: Path):
    f = tmp_path / "demo.tvw"
    f.write_bytes(b"anything")
    assert isinstance(parser_for(f), TVWParser)


@pytest.mark.parametrize(
    "text",
    [
        "abcdefghijklmnopqrstuvwxyz\n",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ\n",
        "0123456789\n",
        "Tebo-ictview files.\n",
        "var_data: 1 2 3 4\nParts: R1 5 1 C1 10 1\n",
        "Refdes: R1 -> net +3V3 / side 1 (pin #4)\n",
    ],
)
def test_round_trip_is_identity(text: str):
    assert _deobfuscate(_obfuscate(text)).decode("utf-8") == text


def test_separators_and_symbols_pass_through_unchanged():
    """`-`, `.`, space, `+`, `/`, `:`, newline must NOT be transformed —
    they anchor block markers and ownership counts in the plaintext."""
    seps = "-.+:/ \n\t"
    assert _obfuscate(seps) == seps.encode()
    assert _deobfuscate(seps.encode()).decode() == seps


def test_parses_minimal_tvw_fixture():
    board = TVWParser().parse_file(FIXTURE_DIR / "minimal.tvw")
    assert board.source_format == "tvw"
    assert [p.refdes for p in board.parts] == ["R1", "C1"]
    assert len(board.pins) == 4
    assert len(board.nails) == 1
    assert board.net_by_name("+3V3").is_power is True


def test_fixture_is_genuinely_encoded():
    """Guard: the committed fixture must differ from the plaintext we'd
    get by writing the same text uncovered. Catches regressions that
    silently forget to call the encoder."""
    raw = (FIXTURE_DIR / "minimal.tvw").read_bytes()
    assert b"Parts:" not in raw, "fixture is un-encoded — encoder not exercised"
    assert b"var_data:" not in raw


def test_rejects_payload_that_doesnt_decode_to_boardview(tmp_path: Path):
    """A file that deciphers to prose must be rejected, not silently
    produce a blank Board."""
    f = tmp_path / "bad.tvw"
    f.write_bytes(_obfuscate("just some prose, no boardview markers at all\n"))
    with pytest.raises(InvalidBoardFile):
        TVWParser().parse_file(f)


def test_rejects_production_binary_tvw_with_clear_hint(tmp_path: Path):
    """A binary-layout production TVW file (Tebo IctView 3.0/4.0 native
    output — little-endian ints + Pascal strings + layer sections) must
    get a clear error pointing the user at the format-scope note. The
    rotation-cipher parser cannot decode the binary container."""
    from api.board.parser.base import ObfuscatedFileError

    # Synthetic binary-TVW-looking payload: Pascal string "Teboview" then
    # a pile of little-endian int32s and colour bytes. ~60% non-printable.
    binary_blob = bytes(
        [8] + list(b"Teboview")  # Pascal string header
        + [0x00, 0x00, 0x00, 0x01]  # uint32 unknown
        + [4] + list(b"Main")
        + [0x33]  # section marker
        + list(range(256)) * 2  # 512 bytes of entropy
    )
    f = tmp_path / "prod.tvw"
    f.write_bytes(binary_blob)
    with pytest.raises(ObfuscatedFileError) as exc:
        TVWParser().parse_file(f)
    msg = str(exc.value)
    assert "binary-layout" in msg or "binary" in msg
    assert "rotation-cipher" in msg or "ASCII variant" in msg
