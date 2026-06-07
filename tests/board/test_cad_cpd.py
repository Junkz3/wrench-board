"""CPD "neutral file" `.cad` dialect parser.

A second, unrelated dialect ships under `.cad`: the CPD3
`mfg/neutral_file` ASCII export (`#`-commented, `###`-sectioned,
`COMP`/`C_PIN`/`NET` records). These tests use a synthetic, hand-built
fixture (own data, no copyrighted dump) plus a corpus-gated smoke test
over the real wild files when they are present locally.
"""

from __future__ import annotations

import glob
import os

import pytest

from api.board.model import Layer
from api.board.parser._cpd_neutral import (
    _parse_geometries,
    _placed_body_lines,
    looks_like_cpd_neutral,
)
from api.board.parser.cad import CADParser

# --- Synthetic CPD neutral file (own data) ------------------------------
# Two components on opposite sides; an inch-unit board. R1 (top) bridges a
# power rail and a signal; C1 (bottom) bridges that signal and ground. The
# Nets section declares the power/ground types explicitly so we exercise
# the N_PROP path, and U1 only appears in the Nets section (no COMP/C_PIN)
# to prove parts in the component block win but nets still classify.
SYNTHETIC_CPD = """\
# file : D:/mentor/CPD3/Demo/demo/pcb/mfg/neutral_file
# date : Monday January 1, 2024; 00:00:00
#
#############################################
###Board Information
#############################################
BOARD DEMO_MAIN OFFSET x:0.0 y:0.0 ORIENTATION    0
B_UNITS Inches
#############################################
###Nets Information
#############################################
NET /VCC3V3
N_PROP  (NET_TYPE,"POWER")
N_PIN R1-1 1.0 1.0 r016x020n   1
N_PIN U1-1 2.0 2.0 r016x020n   1
N_VIA 1.5 1.5 via10   1   8
NET /SIG_A
N_PROP  (NET_TYPE,"DEFAULT_NET_TYPE")
N_PIN R1-2 1.05 1.0 r016x020n   1
N_PIN C1-1 1.10 0.5 r016x020n   2
NET /GND
N_PROP  (NET_TYPE,"GROUND")
N_PIN C1-2 1.15 0.5 r016x020n   2
#############################################
###Component Information
#############################################
COMP R1 1234-001 res_geom r0402  1.0 1.0 1   0
C_PROP (DESC,"RES 10k")
C_PIN R1-1 1.0 1.0  1  1 0 r016x020n /VCC3V3
C_PIN R1-2 1.05 1.0  1  1 0 r016x020n /SIG_A
COMP C1 5678-002 cap_geom c0402  1.10 0.5 2  90
C_PIN C1-1 1.10 0.5  8  2 90 r016x020n /SIG_A
C_PIN C1-2 1.15 0.5  8  2 90 r016x020n /GND
#############################################
###Hole Information
#############################################
HOLE PTH   3.0 3.0 0.024
HOLE NPTH   0.1 0.1 0.150
"""

# A Mm-unit variant to exercise unit conversion.
SYNTHETIC_CPD_MM = """\
# file : D:/mentor/CPD3/DemoMm/pcb/mfg/neutral_file
###Board Information
BOARD DEMO_MM OFFSET x:0.0 y:0.0 ORIENTATION 0
B_UNITS Mm.
###Component Information
COMP R1 1 g f  25.4 25.4 1 0
C_PIN R1-1 25.4 25.4 1 1 0 pad /N1
C_PIN R1-2 26.4 25.4 1 1 0 pad /N2
"""


def _parse(text: str):
    return CADParser().parse(
        text.encode("utf-8"), file_hash="deadbeef", board_id="b1"
    )


def test_signature_detects_cpd_neutral():
    assert looks_like_cpd_neutral(SYNTHETIC_CPD) is True
    assert looks_like_cpd_neutral(SYNTHETIC_CPD_MM) is True
    assert looks_like_cpd_neutral("BRDOUT: 1 0 0 0\n") is False
    assert looks_like_cpd_neutral("just prose, nothing structured\n") is False


def test_parses_components_pins_and_nets():
    board = _parse(SYNTHETIC_CPD)
    assert board.source_format == "cad"

    # Two real components from the COMP block (U1 only appears in nets).
    assert len(board.parts) == 2
    assert {p.refdes for p in board.parts} == {"R1", "C1"}

    r1 = board.part_by_refdes("R1")
    c1 = board.part_by_refdes("C1")
    assert r1.layer == Layer.TOP  # side 1
    assert c1.layer == Layer.BOTTOM  # side 2
    assert r1.rotation_deg == 0
    assert c1.rotation_deg == 90

    # 4 pins total (2 per part).
    assert len(board.pins) == 4
    r1_pins = [p for p in board.pins if p.part_refdes == "R1"]
    assert len(r1_pins) == 2
    assert {p.net for p in r1_pins} == {"/VCC3V3", "/SIG_A"}
    assert r1_pins[0].name == "1"

    # Pin coordinates are scaled to mils (inches × 1000).
    assert r1_pins[0].pos.x == pytest.approx(1000.0)
    assert r1_pins[0].pos.y == pytest.approx(1000.0)


def test_net_power_ground_classification():
    board = _parse(SYNTHETIC_CPD)
    vcc = board.net_by_name("/VCC3V3")
    gnd = board.net_by_name("/GND")
    sig = board.net_by_name("/SIG_A")
    assert vcc is not None and vcc.is_power is True and vcc.is_ground is False
    assert gnd is not None and gnd.is_ground is True and gnd.is_power is False
    assert sig is not None and sig.is_power is False and sig.is_ground is False


def test_mechanical_holes_scaled_and_fiducial_flagged():
    board = _parse(SYNTHETIC_CPD)
    assert len(board.mech_holes) == 2
    # 0.024 in → 24 mils (≤100 → fiducial); 0.150 in → 150 mils (mount hole).
    diams = sorted(h.diameter for h in board.mech_holes)
    assert diams[0] == pytest.approx(24.0)
    assert diams[1] == pytest.approx(150.0)
    fiducials = [h for h in board.mech_holes if h.is_fiducial]
    assert len(fiducials) == 1


def test_mm_units_converted_to_mils():
    board = _parse(SYNTHETIC_CPD_MM)
    r1 = board.part_by_refdes("R1")
    assert r1 is not None
    p = next(p for p in board.pins if p.name == "1")
    # 25.4 mm / 0.0254 = 1000 mils.
    assert p.pos.x == pytest.approx(1000.0, rel=1e-6)


# --- Corpus-gated smoke test over real wild CPD .cad files -----------

_CORPUS_GLOBS = [
    "/tmp/wb-cad-corpus/**/*.cad",
]


def _find_real_cpd_cad(limit: int = 5) -> list[str]:
    found: list[str] = []
    for pattern in _CORPUS_GLOBS:
        for path in glob.glob(pattern, recursive=True):
            try:
                with open(path, "rb") as fh:
                    head = fh.read(400)
            except OSError:
                continue
            if b"mentor" in head.lower() or b"###" in head:
                found.append(path)
            if len(found) >= limit:
                return found
    return found


@pytest.mark.skipif(
    not _find_real_cpd_cad(1),
    reason="no real CPD .cad corpus present locally",
)
def test_smoke_real_cpd_corpus():
    paths = _find_real_cpd_cad(5)
    assert paths, "guard above should have skipped"
    parsed_ok = 0
    for path in paths:
        raw = open(path, "rb").read()
        text = raw.decode("utf-8", errors="replace")
        if not looks_like_cpd_neutral(text):
            continue
        board = CADParser().parse(raw, file_hash="x", board_id=os.path.basename(path))
        # A real CPD board must surface non-trivial parts/pins/nets.
        assert len(board.parts) > 0, f"{path}: no parts"
        assert len(board.pins) > 0, f"{path}: no pins"
        assert len(board.nets) > 0, f"{path}: no nets"
        parsed_ok += 1
    assert parsed_ok > 0, "found CPD .cad files but none parsed"


def test_parse_geometries_reads_placement_outline_with_continuation():
    # The outline coords wrap onto a continuation line; G_PIN / other G_ATTR
    # lines must not pollute it. A unit square (closed) -> 4 vertices.
    lines = [
        "GEOM sq1 ",
        "G_PIN 1 0.0 0.0 pad Surf",
        "G_ATTR 'COMPONENT_PLACEMENT_OUTLINE' ''  1 1 -1 1 -1 -1",
        "1 -1",  # continuation
        "G_ATTR 'COMPONENT_HEIGHT' ''  0.1 0.0",
        "GEOM other ",
    ]
    geoms = _parse_geometries(lines)
    assert geoms["sq1"] == [(1.0, 1.0), (-1.0, 1.0), (-1.0, -1.0), (1.0, -1.0)]
    assert "other" not in geoms  # no outline -> not recorded


def test_placed_body_lines_translates_scales_and_closes():
    # Unit square placed at (10, 20), no rotation, top side, scale x1000 (inches
    # -> mils). 4 closed segments, centred on the placement point.
    sq = [(1.0, 1.0), (-1.0, 1.0), (-1.0, -1.0), (1.0, -1.0)]
    segs = _placed_body_lines(sq, 10.0, 20.0, 0.0, "1", 1000.0)
    assert len(segs) == 4
    assert segs[0].a.x == 11000.0 and segs[0].a.y == 21000.0
    assert segs[-1].b == segs[0].a  # polygon closes back to the first vertex


def test_placed_body_lines_mirrors_bottom_side():
    # Bottom side (2) flips X about the placement centre.
    sq = [(1.0, 0.0)]  # too short -> no segments
    assert _placed_body_lines(sq, 0.0, 0.0, 0.0, "2", 1.0) == []
    tri = [(2.0, 0.0), (0.0, 1.0), (0.0, -1.0)]
    top = _placed_body_lines(tri, 0.0, 0.0, 0.0, "1", 1.0)
    bot = _placed_body_lines(tri, 0.0, 0.0, 0.0, "2", 1.0)
    assert top[0].a.x == 2.0 and bot[0].a.x == -2.0
