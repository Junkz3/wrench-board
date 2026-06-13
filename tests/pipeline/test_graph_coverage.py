"""Unit tests for api.pipeline.qa.graph_coverage — the post-build QA gate.

Compares the vision-built electrical graph against the physical boardview
(the independent ground truth: every part actually placed on the PCB) and
produces coverage metrics + a PASS/WARN/FAIL verdict. Calibrated on the three
real pilots (2026-06-12): A2338 nets 97.9%, iPhone 8 98.9%, iPhone 11 93.8%
(source-PDF gap) — thresholds chosen so those land PASS/PASS/WARN.
"""

from __future__ import annotations

import pytest

from api.pipeline.qa.graph_coverage import (
    CoverageReport,
    compare_graph_to_board,
)


def _graph(components: list[str], nets: list[str]) -> dict:
    return {
        "components": {c: {} for c in components},
        "nets": {n: {} for n in nets},
    }


def test_full_coverage_passes():
    report = compare_graph_to_board(
        graph=_graph(["U1000", "C100", "R200"], ["PP1V8", "PP3V3"]),
        board_refdes=["U1000", "C100", "R200"],
        board_nets=["PP1V8", "PP3V3"],
    )
    assert report.component_coverage == 1.0
    assert report.net_coverage == 1.0
    assert report.verdict == "PASS"
    assert report.missing_critical == []


def test_testpads_and_strap_families_are_excluded_from_components():
    """TPU/TP/PP/XW/FID/MP are physical test/strap artefacts, not schematic
    components — they must not drag component coverage down (A2338 carries
    310 TPU pads the schematic legitimately never draws as parts)."""
    report = compare_graph_to_board(
        graph=_graph(["U1000"], ["PP1V8"]),
        board_refdes=["U1000", "TPU001", "TP12", "PP0500", "XW1", "FID3", "MP9"],
        board_nets=["PP1V8"],
    )
    assert report.component_coverage == 1.0
    assert report.excluded_families["TPU"] == 1
    assert report.verdict == "PASS"


def test_suffixed_graph_refdes_match_base():
    """The vision suffixes refdes on region-marked pages (C300_K, L7700_W) —
    base-name matching must credit them against the boardview."""
    report = compare_graph_to_board(
        graph=_graph(["C300_K", "L7700_W"], ["NET1"]),
        board_refdes=["C300", "L7700"],
        board_nets=["NET1"],
    )
    assert report.component_coverage == 1.0


def test_missing_critical_components_are_listed_and_fail():
    """Missing U/Q/J/F (IC, mosfet, connector, fuse) beyond the tolerance
    means the diagnostic backbone has holes → FAIL."""
    board = [f"U{i}" for i in range(30)] + ["C1"]
    report = compare_graph_to_board(
        graph=_graph(["C1"], ["NET1"]),
        board_refdes=board,
        board_nets=["NET1"],
    )
    assert len(report.missing_critical) == 30
    assert report.verdict == "FAIL"


def test_low_net_coverage_warns():
    """Nets are the diagnostic backbone — below the WARN floor the pack needs
    review (e.g. an incomplete source PDF, like the iPhone 11 pilot)."""
    nets_board = [f"NET{i}" for i in range(100)]
    nets_graph = nets_board[:85]  # 85% < PASS floor (90), ≥ FAIL floor (75)
    report = compare_graph_to_board(
        graph=_graph(["U1"], nets_graph),
        board_refdes=["U1"],
        board_nets=nets_board,
    )
    assert report.verdict == "WARN"
    assert report.net_coverage == pytest.approx(0.85)


def test_very_low_net_coverage_fails():
    nets_board = [f"NET{i}" for i in range(100)]
    report = compare_graph_to_board(
        graph=_graph(["U1"], nets_board[:50]),
        board_refdes=["U1"],
        board_nets=nets_board,
    )
    assert report.verdict == "FAIL"


def test_ghosts_are_reported_not_fatal():
    """Graph entries absent from the board (DNP parts, rev skew) are listed
    for review but don't fail the gate on their own."""
    report = compare_graph_to_board(
        graph=_graph(["U1", "C9999"], ["NET1"]),
        board_refdes=["U1"],
        board_nets=["NET1"],
    )
    assert "C9999" in report.ghosts
    assert report.verdict == "PASS"


def test_report_serialises_to_json_dict():
    report = compare_graph_to_board(
        graph=_graph(["U1"], ["NET1"]),
        board_refdes=["U1", "C2"],
        board_nets=["NET1", "NET2"],
    )
    d = report.to_dict()
    assert d["verdict"] in ("PASS", "WARN", "FAIL")
    assert "component_coverage" in d and "net_coverage" in d
    assert isinstance(d["missing_components"], list)


def test_real_pilot_profile_a2338_passes():
    """Synthetic profile shaped like the real A2338 result: 97.9% nets,
    functional components covered, only RF inductors missing → PASS."""
    board_parts = (
        [f"U{i:04d}" for i in range(60)]
        + [f"C{i:04d}" for i in range(2000)]
        + [f"TPU{i:03d}" for i in range(310)]
        + ["L77A0", "L77C0", "L77D0", "L84E0"]  # the 4 real missing inductors
    )
    graph_parts = [f"U{i:04d}" for i in range(60)] + [f"C{i:04d}" for i in range(2000)]
    board_nets = [f"NET{i}" for i in range(1000)]
    graph_nets = board_nets[:979]
    report = compare_graph_to_board(
        graph=_graph(graph_parts, graph_nets),
        board_refdes=board_parts,
        board_nets=board_nets,
    )
    assert report.verdict == "PASS"
    assert set(report.missing_critical) == {"L77A0", "L77C0", "L77D0", "L84E0"}
