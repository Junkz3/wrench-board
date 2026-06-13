"""The optional Rust module `wb_fz_cipher` must produce output that is
BYTE-IDENTICAL to the reference Python `decrypt_fz_xor` (the shared decode
cache relies on determinism: same input + key produces the same bytes,
whichever engine runs).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# OPTIONAL Rust/PyO3 module: if it was not built (self-host without a Rust
# toolchain) the whole file is skipped — the engine works without it.
wb_fz_cipher = pytest.importorskip("wb_fz_cipher")

# Imports after the importorskip: intentional, the module under test only
# makes sense when the Rust extension is present.
from api.board.parser._fz_engine.cipher import _decrypt_core_py  # noqa: E402
from api.board.parser._fz_engine.cipher import decrypt_fz_xor as py_decrypt  # noqa: E402

# Arbitrary 44-word uint32 key (independent of any corpus or .env).
KEY = tuple((i * 2654435761) & 0xFFFFFFFF for i in range(44))

_CORPUS = [os.path.expanduser("~/Documents/Boardview XZZ"),
           os.path.expanduser("~/Documents/XZZ Laptop")]


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x00",
        bytes(range(16)),  # exactly one window
        bytes((i * 37) & 0xFF for i in range(1000)),  # > window, varied pattern
    ],
)
def test_rust_matches_python(data):
    assert wb_fz_cipher.decrypt_fz_xor(data, list(KEY)) == py_decrypt(data, KEY)


def test_rust_rejects_wrong_key_length():
    with pytest.raises((ValueError, Exception)):
        wb_fz_cipher.decrypt_fz_xor(b"abc", [1, 2, 3])


def test_public_decrypt_identical_rust_vs_python_fallback(monkeypatch):
    """The public `decrypt_fz_xor` must give a byte-identical result whether it
    delegates to Rust (the default path when the module is installed) or falls
    back to the Python core (self-host without a Rust toolchain)."""
    import api.board.parser._fz_engine.cipher as mod

    cipher = bytes((i * 37) & 0xFF for i in range(500))
    rust_out = mod.decrypt_fz_xor(cipher, KEY)            # Rust path (installed)
    monkeypatch.setattr(mod, "_rust_decrypt", None)        # force the Python fallback
    py_out = mod.decrypt_fz_xor(cipher, KEY)
    assert rust_out == py_out


def _real_key():
    import struct
    repo = Path(__file__).resolve().parents[2]
    env = repo / ".env"
    if not env.is_file():
        return None
    for line in env.read_text().splitlines():
        if line.startswith("WRENCH_BOARD_FZ_KEY="):
            raw = line.split("=", 1)[1].strip()
            try:
                return struct.unpack("<44I", bytes.fromhex(raw))
            except (ValueError, struct.error):
                return None
    return None


def _find_real_xor_fz():
    from api.board.parser._fz_engine.cipher import looks_like_fz_xor
    for root in _CORPUS:
        for dp, _, fs in os.walk(root):
            for f in fs:
                if f.lower().endswith(".fz"):
                    p = Path(dp) / f
                    try:
                        if looks_like_fz_xor(p.read_bytes()):
                            return p
                    except OSError:
                        continue
    return None


def test_rust_matches_python_on_real_fz_file():
    """End-to-end golden: on a REAL XOR-flavoured `.fz` file from the corpus, the
    Rust core must produce exactly the same bytes as the Python core."""
    key = _real_key()
    if key is None:
        pytest.skip("WRENCH_BOARD_FZ_KEY not present in .env")
    path = _find_real_xor_fz()
    if path is None:
        pytest.skip("no XOR-flavoured .fz in the local corpus")
    raw = path.read_bytes()
    assert wb_fz_cipher.decrypt_fz_xor(raw, list(key)) == _decrypt_core_py(raw, key)
