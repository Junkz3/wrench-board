"""Real-variant `.bdv` decode + test-fixture dialect parse.

The real-world `.bdv` corpus (header bytes ``dd:``) is the same arithmetic
family with a LINE-INDEXED key schedule (constant within a line, +1 per LF,
wrap 285→159) rather than the per-byte schedule the shipped decoder also
handles. The decoded payload is a test-fixture ("TestLink") ASCII dialect
(``<<format.asc>>`` outline + ``Part <ref> (T/B)`` blocks + a ``$n`` nails
list) — distinct from the ``var_data:`` Test_Link shape
`parse_test_link_shape` handles.

These tests pin the per-line schedule (round-trip + header detection) and
were written BEFORE the implementation (TDD).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from api.board.model import Layer
from api.board.parser.bdv import (
    BDVParser,
    _deobfuscate,
    _deobfuscate_real,
    _is_real_variant,
    _obfuscate_real,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "text",
    [
        "dd:1.3 header line\n",
        # Multi-line: the per-LINE key advances once per newline, so the
        # round-trip must survive several line boundaries.
        "<<format.asc>>\nPart U2  (T)\n 1    1   GND\n",
        # Long lines that exercise the wrap (285→159) once the line key
        # walks past 285 (>125 lines from start=160).
        "x\n" * 200,
        # CRLF endings — \r passes through, only \n advances the key.
        "abc\r\ndef\r\nghi\r\n",
    ],
)
def test_real_decode_of_encode_is_identity(text: str):
    """The real schedule round-trips: decode(encode(text)) == text.

    `sub` is an involution under a fixed key, so encode == decode with
    the same line-indexed schedule."""
    assert _deobfuscate_real(_obfuscate_real(text)).decode("utf-8") == text


def test_real_schedule_is_symmetric():
    """sub-mode round-trip both directions: applying the schedule twice
    is the identity (the encoding is its own inverse)."""
    text = "Part C1  (B)\n 1    1   +3V3\n"
    once = _obfuscate_real(text)
    twice = _deobfuscate_real(once)
    assert twice.decode("utf-8") == text


def test_is_real_variant_detects_dd_header():
    """The real variant is fingerprinted by its raw `dd:` header bytes.

    `dd:` is what the payload's leading `<<f` (the `<<format.asc>>` section
    marker) encodes to at the line-0 key (start=160): `<` (60) →
    `(160-60)&0xFF` = 100 = `d`. So a raw stream beginning `dd:` is the
    encoded form of `<<…` — a reliable signature. Detection runs on the
    RAW (still-encoded) bytes."""
    real = _obfuscate_real("<<format.asc>>\n LA6221PR10\n")
    assert real[:3] == b"dd:"  # the encoded header magic
    assert _is_real_variant(real) is True


def test_is_real_variant_rejects_per_byte_fixture():
    """The shipped per-byte fixture must NOT be taken for the real
    variant — otherwise we'd decode it with the wrong schedule."""
    from pathlib import Path

    per_byte = (Path(__file__).parent / "fixtures" / "minimal.bdv").read_bytes()
    assert _is_real_variant(per_byte) is False


def test_real_and_per_byte_schedules_differ():
    """A non-trivial payload encodes differently under the two schedules
    — proving the real variant is a distinct key schedule, not a rename."""
    text = "Part U1  (T)\n 1    1   GND\n 2    2   +5V\n"
    assert _obfuscate_real(text) != _obfuscate_via_per_byte(text)


def _obfuscate_via_per_byte(text: str) -> bytes:
    """Encode with the shipped per-byte schedule (test helper)."""
    from api.board.parser.bdv import _obfuscate

    return _obfuscate(text)


def test_per_byte_decode_of_real_encode_is_garbage():
    """Decoding real-variant bytes with the per-byte schedule must NOT
    round-trip — the whole point of a distinct line-indexed schedule."""
    text = "Part U1  (T)\n 1    1   GND\n 2    2   +5V\n"
    real_bytes = _obfuscate_real(text)
    assert _deobfuscate(real_bytes).decode("utf-8", "replace") != text


# --- Step C: synthetic fixture + real-file smoke test ---------------------


def test_parses_real_variant_fixture():
    """The committed synthetic real-variant fixture parses end-to-end.

    Built by `_obfuscate_real` from a known plaintext: 4 outline vertices,
    parts R1 (top) / C1 (bottom), 4 pins, 1 nail, nets +3V3 (power) / GND
    (ground)."""
    board = BDVParser().parse_file(FIXTURE_DIR / "bdv_real_min.bdv")
    assert board.source_format == "bdv"
    assert len(board.parts) == 2
    assert len(board.pins) == 4
    assert len(board.nails) == 1
    assert board.part_by_refdes("R1").layer == Layer.TOP
    assert board.part_by_refdes("C1").layer == Layer.BOTTOM
    assert board.net_by_name("+3V3").is_power is True
    assert board.net_by_name("GND").is_ground is True


def test_real_variant_fixture_is_genuinely_binary_not_ascii():
    """Guard: the committed fixture must be encoded, not plaintext —
    a plain-ASCII regression would make the decoder look exercised
    without actually running the decode."""
    raw = (FIXTURE_DIR / "bdv_real_min.bdv").read_bytes()
    assert raw[:3] == b"dd:"  # the real-variant header magic
    printable = sum(1 for b in raw if 32 <= b <= 126)
    assert printable < len(raw) / 2, "fixture reads as plaintext; decoder not exercised"


def _smallest_real_bdv() -> Path | None:
    """Locate the smallest real corpus `.bdv` under ~/Documents, if any.

    The corpus is a local developer asset, not committed, so the smoke
    test skips cleanly when it's absent (CI / fresh checkout)."""
    docs = Path.home() / "Documents"
    if not docs.is_dir():
        return None
    found = subprocess.run(
        [
            "find",
            str(docs),
            "-iname",
            "*.bdv",
            "!",
            "-iname",
            "minimal.bdv",
            "!",
            "-iname",
            "bdv_real_min.bdv",
            "-printf",
            "%s\t%p\n",
        ],
        capture_output=True,
        text=True,
    )
    rows = [ln.split("\t", 1) for ln in found.stdout.splitlines() if "\t" in ln]
    if not rows:
        return None
    rows.sort(key=lambda r: int(r[0]))
    return Path(rows[0][1])


def test_smoke_parses_smallest_real_corpus_bdv():
    """Smoke test: the smallest real-world `.bdv` parses to a non-empty
    board. Skips if the (uncommitted) corpus isn't present locally."""
    target = _smallest_real_bdv()
    if target is None:
        pytest.skip("no real .bdv corpus under ~/Documents")
    board = BDVParser().parse_file(target)
    assert board.source_format == "bdv"
    # The fixture dialect carries all four block kinds; a non-zero count
    # on each proves the decode + dialect parser both fired.
    assert len(board.parts) > 0
    assert len(board.pins) > 0
    assert len(board.nets) > 0
    assert len(board.nails) > 0
