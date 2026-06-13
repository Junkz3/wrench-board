"""The optional Rust module `wb_tvw_walker` must reproduce `_try_walk_pins_at`
EXACTLY (the binary `.tvw` hot loop: ~0.3 MB/s, dominated by `_read_pin_record`
plus millions of `_u8`/`_u32`).

Test strategy: replay the REAL `_try_walk_pins_at` calls captured during a Python
parse of a real `.tvw`, and require identical results from the Rust side. Plus an
end-to-end equivalence: identical Board with Rust on vs off.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

wb_tvw_walker = pytest.importorskip("wb_tvw_walker")

# Imports after the importorskip: intentional, the module under test only
# makes sense when the Rust extension is present.
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
        pytest.skip("no .tvw in the local corpus")

    # Capture the Python calls (buf, off, region_end) that find pins.
    calls = []
    orig = W._try_walk_pins_at_py if hasattr(W, "_try_walk_pins_at_py") else None
    if orig is None:
        pytest.skip("_try_walk_pins_at_py wiring absent")

    def spy(buf, off, region_end, max_pin_count=200_000, min_partial_ratio=0.5):
        res = orig(buf, off, region_end, max_pin_count, min_partial_ratio)
        if res is not None and res[0]:
            calls.append((bytes(buf), off, region_end, max_pin_count, min_partial_ratio, res))
        return res

    monkeypatch.setattr(W, "_try_walk_pins_at", spy)
    parser_for(path).parse_file(path)
    monkeypatch.undo()

    assert calls, "no pin-walk call captured on this .tvw"
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
    """The full brute-force scan (triage + try_walk + best candidate) ported to
    Rust must give the SAME best (offset, pins, end, declared) as the Python
    core, on the real calls captured from a `.tvw` parse."""
    path = _find_real_tvw()
    if path is None:
        pytest.skip("no .tvw in the local corpus")
    if not hasattr(W, "_scan_best_pin_section_py"):
        pytest.skip("scan wiring absent")

    calls = []
    py_ref = W._scan_best_pin_section_py

    def spy(buf, ss, se, re_, step, mpc=200_000, mpr=0.5):
        r = py_ref(buf, ss, se, re_, step, mpc, mpr)
        calls.append((bytes(buf), ss, se, re_, step, mpc, mpr, r))
        return r

    monkeypatch.setattr(W, "_scan_best_pin_section", spy)
    parser_for(path).parse_file(path)
    monkeypatch.undo()

    assert calls, "no scan call captured"
    for buf, ss, se, re_, step, mpc, mpr, py_r in calls:
        rust_r = wb_tvw_walker.scan_best_pin_section(buf, ss, se, re_, step, mpc, mpr)
        assert _norm_scan(rust_r) == _norm_scan(py_r)


def test_rust_netnames_matches_python_on_real_calls(monkeypatch):
    """The net-names scan (`_try_read_network_names`, ~58% of the parse of a large
    .tvw) ported to Rust must return exactly the same list of names."""
    path = _find_real_tvw()
    if path is None:
        pytest.skip("no .tvw in the local corpus")
    if not hasattr(W, "_try_read_network_names_py") or not hasattr(wb_tvw_walker, "try_read_network_names"):
        pytest.skip("net-names wiring absent")

    calls = []
    py_ref = W._try_read_network_names_py

    def spy(buf, after_layers):
        r = py_ref(buf, after_layers)
        calls.append((bytes(buf), after_layers, r))
        return r

    monkeypatch.setattr(W, "_try_read_network_names", spy)
    parser_for(path).parse_file(path)
    monkeypatch.undo()

    assert calls, "no net-names call captured"
    for buf, after_layers, py_r in calls:
        assert wb_tvw_walker.try_read_network_names(buf, after_layers) == py_r


def test_board_identical_rust_vs_python_fallback(monkeypatch):
    """End-to-end: the parsed Board is identical whether the walker goes through
    Rust (default) or falls back entirely to the Python core (self-host without Rust)."""
    path = _find_real_tvw()
    if path is None:
        pytest.skip("no .tvw in the local corpus")

    board_rust = parser_for(path).parse_file(path)
    monkeypatch.setattr(W, "_rust_walk", None)  # force the Python fallback (pin-walk)
    if hasattr(W, "_rust_scan"):
        monkeypatch.setattr(W, "_rust_scan", None)  # ... and the scan
    if hasattr(W, "_rust_netnames"):
        monkeypatch.setattr(W, "_rust_netnames", None)  # ... and the net-names
    board_py = parser_for(path).parse_file(path)

    assert len(board_rust.parts) == len(board_py.parts)
    assert len(board_rust.pins) == len(board_py.pins)
    assert [(p.pos.x, p.pos.y) for p in board_rust.pins] == [(p.pos.x, p.pos.y) for p in board_py.pins]
