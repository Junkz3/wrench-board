"""Le module Rust optionnel `wb_tvw_walker` doit reproduire EXACTEMENT
`_try_walk_pins_at` (le hot-loop binaire `.tvw` : ~0,3 Mo/s, dominé par
`_read_pin_record` + des millions de `_u8`/`_u32`).

Stratégie de test : on rejoue les VRAIS appels `_try_walk_pins_at` capturés
pendant un parse Python d'un vrai `.tvw`, et on exige des résultats identiques
côté Rust. Plus une équivalence end-to-end : Board identique Rust-on vs Rust-off.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

wb_tvw_walker = pytest.importorskip("wb_tvw_walker")

# Imports après l'importorskip : volontaire, le module testé n'a de sens
# que si l'extension Rust est présente.
import api.board.parser._tvw_engine.walker as W  # noqa: E402
from api.board.parser import parser_for  # noqa: E402

_CORPUS = [os.path.expanduser("~/Documents/Boardview XZZ"),
           os.path.expanduser("~/Documents/XZZ Laptop")]


def _find_real_tvw():
    for root in _CORPUS:
        for dp, _, fs in os.walk(root):
            for f in fs:
                if f.lower().endswith(".tvw"):
                    return Path(dp) / f
    return None


def _pin_tuple(rec):
    return (rec.part_index, rec.pin_local_index, rec.x, rec.y, rec.flag1,
            rec.flag3, rec.raw_size, rec.pad_dx1, rec.pad_dy1, rec.pad_dx2,
            rec.pad_dy2, rec.has_pad_bbox)


def test_rust_try_walk_matches_python_on_real_calls(monkeypatch):
    path = _find_real_tvw()
    if path is None:
        pytest.skip("aucun .tvw dans le corpus local")

    # Capture les appels Python (buf, off, region_end) qui trouvent des pins.
    calls = []
    orig = W._try_walk_pins_at_py if hasattr(W, "_try_walk_pins_at_py") else None
    if orig is None:
        pytest.skip("câblage _try_walk_pins_at_py absent")

    def spy(buf, off, region_end, max_pin_count=200_000, min_partial_ratio=0.5):
        res = orig(buf, off, region_end, max_pin_count, min_partial_ratio)
        if res is not None and res[0]:
            calls.append((bytes(buf), off, region_end, max_pin_count, min_partial_ratio, res))
        return res

    monkeypatch.setattr(W, "_try_walk_pins_at", spy)
    parser_for(path).parse_file(path)
    monkeypatch.undo()

    assert calls, "aucun appel pin-walk capturé sur ce .tvw"
    for buf, off, region_end, mpc, mpr, py_res in calls:
        rust_res = wb_tvw_walker.try_walk_pins_at(buf, off, region_end, mpc, mpr)
        assert rust_res is not None
        r_pins, r_end, r_decl = rust_res
        py_pins, py_end, py_decl = py_res
        assert r_end == py_end and r_decl == py_decl
        assert [tuple(t) for t in r_pins] == [_pin_tuple(p) for p in py_pins]


def _norm_scan(r):
    if r is None:
        return None
    best_off, pins, end, declared = r
    norm = [tuple(p) if not hasattr(p, "part_index") else _pin_tuple(p) for p in pins]
    return (best_off, norm, end, declared)


def test_rust_scan_matches_python_on_real_calls(monkeypatch):
    """Le scan brute-force complet (triage + try_walk + meilleur candidat) porté
    en Rust doit donner le MÊME meilleur (offset, pins, end, declared) que le
    cœur Python, sur les vrais appels capturés d'un parse `.tvw`."""
    path = _find_real_tvw()
    if path is None:
        pytest.skip("aucun .tvw dans le corpus local")
    if not hasattr(W, "_scan_best_pin_section_py"):
        pytest.skip("câblage scan absent")

    calls = []
    py_ref = W._scan_best_pin_section_py

    def spy(buf, ss, se, re_, step, mpc=200_000, mpr=0.5):
        r = py_ref(buf, ss, se, re_, step, mpc, mpr)
        calls.append((bytes(buf), ss, se, re_, step, mpc, mpr, r))
        return r

    monkeypatch.setattr(W, "_scan_best_pin_section", spy)
    parser_for(path).parse_file(path)
    monkeypatch.undo()

    assert calls, "aucun appel de scan capturé"
    for buf, ss, se, re_, step, mpc, mpr, py_r in calls:
        rust_r = wb_tvw_walker.scan_best_pin_section(buf, ss, se, re_, step, mpc, mpr)
        assert _norm_scan(rust_r) == _norm_scan(py_r)


def test_rust_netnames_matches_python_on_real_calls(monkeypatch):
    """Le scan des net-names (`_try_read_network_names`, ~58% du parse d'un gros
    .tvw) porté en Rust doit retourner exactement la même liste de noms."""
    path = _find_real_tvw()
    if path is None:
        pytest.skip("aucun .tvw dans le corpus local")
    if not hasattr(W, "_try_read_network_names_py") or not hasattr(wb_tvw_walker, "try_read_network_names"):
        pytest.skip("câblage net-names absent")

    calls = []
    py_ref = W._try_read_network_names_py

    def spy(buf, after_layers):
        r = py_ref(buf, after_layers)
        calls.append((bytes(buf), after_layers, r))
        return r

    monkeypatch.setattr(W, "_try_read_network_names", spy)
    parser_for(path).parse_file(path)
    monkeypatch.undo()

    assert calls, "aucun appel net-names capturé"
    for buf, after_layers, py_r in calls:
        assert wb_tvw_walker.try_read_network_names(buf, after_layers) == py_r


def test_board_identical_rust_vs_python_fallback(monkeypatch):
    """Bout en bout : le Board parsé est identique que le walker passe par le
    Rust (défaut) ou retombe entièrement sur le cœur Python (self-host sans Rust)."""
    path = _find_real_tvw()
    if path is None:
        pytest.skip("aucun .tvw dans le corpus local")

    board_rust = parser_for(path).parse_file(path)
    monkeypatch.setattr(W, "_rust_walk", None)  # force le fallback Python (pin-walk)
    if hasattr(W, "_rust_scan"):
        monkeypatch.setattr(W, "_rust_scan", None)  # ... et le scan
    if hasattr(W, "_rust_netnames"):
        monkeypatch.setattr(W, "_rust_netnames", None)  # ... et les net-names
    board_py = parser_for(path).parse_file(path)

    assert len(board_rust.parts) == len(board_py.parts)
    assert len(board_rust.pins) == len(board_py.pins)
    assert [(p.pos.x, p.pos.y) for p in board_rust.pins] == [(p.pos.x, p.pos.y) for p in board_py.pins]
