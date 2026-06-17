"""Le module Rust optionnel `wb_fz_cipher` doit produire une sortie
BYTE-IDENTIQUE au `decrypt_fz_xor` Python de référence (le moat de cache T9
dépend du déterminisme : même cipher+clé ⇒ mêmes octets, quel que soit le moteur).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# Module Rust/PyO3 OPTIONNEL : si le build n'a pas été fait (self-host sans
# toolchain Rust), tout ce fichier est skip — le moteur fonctionne sans lui.
wb_fz_cipher = pytest.importorskip("wb_fz_cipher")

# Imports après l'importorskip : volontaire, le module testé n'a de sens
# que si l'extension Rust est présente.
from api.board.parser._fz_engine.cipher import _decrypt_core_py  # noqa: E402
from api.board.parser._fz_engine.cipher import decrypt_fz_xor as py_decrypt  # noqa: E402

# Clé arbitraire de 44 mots uint32 (indépendante du corpus / d'un .env).
KEY = tuple((i * 2654435761) & 0xFFFFFFFF for i in range(44))

_CORPUS = [os.path.expanduser("~/Documents/Boardview XZZ"),
           os.path.expanduser("~/Documents/XZZ Laptop")]


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x00",
        bytes(range(16)),  # exactement une fenêtre
        bytes((i * 37) & 0xFF for i in range(1000)),  # > fenêtre, motif varié
    ],
)
def test_rust_matches_python(data):
    assert wb_fz_cipher.decrypt_fz_xor(data, list(KEY)) == py_decrypt(data, KEY)


def test_rust_rejects_wrong_key_length():
    with pytest.raises((ValueError, Exception)):
        wb_fz_cipher.decrypt_fz_xor(b"abc", [1, 2, 3])


def test_public_decrypt_identical_rust_vs_python_fallback(monkeypatch):
    """La fonction publique `decrypt_fz_xor` doit donner un résultat byte-identique
    qu'elle délègue au Rust (chemin par défaut quand le module est installé) ou
    qu'elle retombe sur le cœur Python (self-hoster sans toolchain Rust)."""
    import api.board.parser._fz_engine.cipher as mod

    cipher = bytes((i * 37) & 0xFF for i in range(500))
    rust_out = mod.decrypt_fz_xor(cipher, KEY)            # chemin Rust (installé)
    monkeypatch.setattr(mod, "_rust_decrypt", None)        # force le fallback Python
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
    """Golden de bout en bout : sur un VRAI fichier `.fz` chiffré du corpus, le
    cœur Rust doit produire exactement les mêmes octets que le cœur Python."""
    key = _real_key()
    if key is None:
        pytest.skip("clé WRENCH_BOARD_FZ_KEY absente de .env")
    path = _find_real_xor_fz()
    if path is None:
        pytest.skip("aucun .fz XOR dans le corpus local")
    raw = path.read_bytes()
    assert wb_fz_cipher.decrypt_fz_xor(raw, list(key)) == _decrypt_core_py(raw, key)
