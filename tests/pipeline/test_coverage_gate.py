"""Lot 3 — run_coverage_gate: post-build graph↔boardview audit, wired so a build
with a boardview present gets a PASS/WARN/FAIL verdict written to disk."""

import json
import types
from pathlib import Path

import pytest

from api.pipeline.qa import graph_coverage


def _fake_board(refdes, nets):
    return types.SimpleNamespace(
        parts=[types.SimpleNamespace(refdes=r) for r in refdes],
        nets=[types.SimpleNamespace(name=n) for n in nets],
    )


def _patch_parser(monkeypatch, board):
    import api.board.parser as bp
    monkeypatch.setattr(bp, "parser_for", lambda path: types.SimpleNamespace(
        parse=lambda data, file_hash, board_id: board
    ))


def _write_graph(pack_dir, components, nets):
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "electrical_graph.json").write_text(json.dumps({
        "components": {c: {} for c in components},
        "nets": {n: {} for n in nets},
    }))


def test_gate_pass_writes_report(tmp_path, monkeypatch):
    pack = tmp_path / "dev"
    _write_graph(pack, ["U1", "C1", "R1"], ["PP1V8", "PP3V3"])
    bv = pack / "board.brd"
    bv.write_text("x")
    _patch_parser(monkeypatch, _fake_board(["U1", "C1", "R1"], ["PP1V8", "PP3V3"]))

    verdict = graph_coverage.run_coverage_gate(pack, bv)
    assert verdict == "PASS"
    report = json.loads((pack / "coverage_report.json").read_text())
    assert report["verdict"] == "PASS"


def test_gate_fail_on_poor_coverage(tmp_path, monkeypatch):
    pack = tmp_path / "dev"
    # Graph knows almost nothing; board has many critical parts + nets → FAIL.
    _write_graph(pack, ["U1"], ["PP1V8"])
    bv = pack / "board.brd"
    bv.write_text("x")
    board = _fake_board(
        ["U1"] + [f"U{i}" for i in range(100, 140)],
        ["PP1V8"] + [f"NET{i}" for i in range(60)],
    )
    _patch_parser(monkeypatch, board)

    verdict = graph_coverage.run_coverage_gate(pack, bv)
    assert verdict == "FAIL"


def test_gate_returns_none_without_graph(tmp_path, monkeypatch):
    pack = tmp_path / "dev"
    pack.mkdir()
    bv = pack / "board.brd"
    bv.write_text("x")
    _patch_parser(monkeypatch, _fake_board(["U1"], ["N1"]))
    assert graph_coverage.run_coverage_gate(pack, bv) is None


def test_gate_returns_none_without_boardview(tmp_path):
    pack = tmp_path / "dev"
    _write_graph(pack, ["U1"], ["N1"])
    assert graph_coverage.run_coverage_gate(pack, None) is None


def test_gate_never_raises_on_parser_error(tmp_path, monkeypatch):
    pack = tmp_path / "dev"
    _write_graph(pack, ["U1"], ["N1"])
    bv = pack / "board.brd"
    bv.write_text("x")
    import api.board.parser as bp

    def boom(_path):
        raise ValueError("bad board")
    monkeypatch.setattr(bp, "parser_for", boom)
    assert graph_coverage.run_coverage_gate(pack, bv) is None
