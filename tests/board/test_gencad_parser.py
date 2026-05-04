"""Tests for the GenCAD 1.4 parser — the format real `.cad` files actually
ship in (verified against ASUS Prime A520M and GRANGER 6050A2977701)."""

from __future__ import annotations

import math

import pytest

from api.board.model import Layer
from api.board.parser._gencad import looks_like_gencad, parse_gencad
from api.board.parser.base import InvalidBoardFile, MalformedHeaderError
from api.board.parser.cad import CADParser

_GENCAD_DEMO = """$HEADER
GENCAD 1.4
USER "synthetic test"
DRAWING demo
REVISION "test"
UNITS USER 1000
ORIGIN 0 0
INTERTRACK 0
$ENDHEADER

$BOARD
$ENDBOARD

$SHAPES
SHAPE RES_0402
PIN 1  PADSTACK_1 -10 0 TOP 0 0
PIN 2  PADSTACK_1 10 0 TOP 0 0
INSERT SMD
SHAPE QFN16
PIN 1  PADSTACK_2 -20 -20 TOP 0 0
PIN 2  PADSTACK_2 -20 0 TOP 0 0
PIN 3  PADSTACK_2 -20 20 TOP 0 0
PIN 4  PADSTACK_2 0 20 TOP 0 0
INSERT SMD
$ENDSHAPES

$DEVICES
DEVICE D_R1
PART 10K_RES
VALUE 10K
DEVICE D_U1
PART STM32_QFN16
VALUE STM32F0
$ENDDEVICES

$COMPONENTS
COMPONENT R1
PLACE 100 200
LAYER TOP
ROTATION 0
SHAPE RES_0402
DEVICE D_R1
COMPONENT R2
PLACE 100 300
LAYER BOTTOM
ROTATION 90
SHAPE RES_0402 0 0
DEVICE D_R1
COMPONENT U1
PLACE 500 500
LAYER TOP
ROTATION 0
SHAPE QFN16
DEVICE D_U1
$ENDCOMPONENTS

$SIGNALS
SIGNAL +3V3
NODE R1 1
NODE R2 1
NODE U1 1
SIGNAL GND
NODE R1 2
NODE U1 2
SIGNAL CLK
NODE U1 3
SIGNAL DATA
NODE U1 4
$ENDSIGNALS

$TESTPINS
TESTPIN +3V3 R1 1
TESTPIN GND U1 2
$ENDTESTPINS
"""


def test_sniff_detects_gencad_header():
    assert looks_like_gencad(_GENCAD_DEMO) is True
    assert looks_like_gencad("not a gencad file at all") is False


def test_parses_synthetic_gencad_layout():
    board = parse_gencad(_GENCAD_DEMO, file_hash="sha256:x", board_id="demo")
    assert board.source_format == "cad"
    assert [p.refdes for p in board.parts] == ["R1", "R2", "U1"]

    # Layer comes from COMPONENT.LAYER (TOP/BOTTOM)
    assert board.part_by_refdes("R1").layer == Layer.TOP
    assert board.part_by_refdes("R2").layer == Layer.BOTTOM
    assert board.part_by_refdes("U1").layer == Layer.TOP

    # Footprint = shape name; value = device VALUE
    r1 = board.part_by_refdes("R1")
    assert r1.footprint == "RES_0402"
    assert r1.value == "10K"
    u1 = board.part_by_refdes("U1")
    assert u1.value == "STM32F0"

    # Pin positions: world = place + shape_pin (rotated/mirrored).
    # R1 at (100, 200), rotation 0, layer TOP — pin 1 at rel (-10, 0) → world (90, 200).
    r1_pin1 = next(p for p in board.pins if p.part_refdes == "R1" and p.index == 1)
    assert (r1_pin1.pos.x, r1_pin1.pos.y) == (90, 200)
    r1_pin2 = next(p for p in board.pins if p.part_refdes == "R1" and p.index == 2)
    assert (r1_pin2.pos.x, r1_pin2.pos.y) == (110, 200)

    # R2 on BOTTOM with rotation 90 — pin 1 at rel (-10, 0) flips Y (mirror)
    # then rotates 90°: rotation matrix * (-10, -0) = (0, -10) → world = (100, 290).
    r2_pin1 = next(p for p in board.pins if p.part_refdes == "R2" and p.index == 1)
    # 90° rotation of (-10, 0) (mirror y not visible since y=0): (0, -10) → (100, 290)
    assert (r2_pin1.pos.x, r2_pin1.pos.y) == (100, 290)
    assert r2_pin1.layer == Layer.BOTTOM


def test_signals_resolve_to_pin_nets():
    board = parse_gencad(_GENCAD_DEMO, file_hash="sha256:x", board_id="x")
    r1_pin1 = next(p for p in board.pins if p.part_refdes == "R1" and p.index == 1)
    assert r1_pin1.net == "+3V3"
    r1_pin2 = next(p for p in board.pins if p.part_refdes == "R1" and p.index == 2)
    assert r1_pin2.net == "GND"

    v33 = board.net_by_name("+3V3")
    assert v33 is not None and v33.is_power is True
    gnd = board.net_by_name("GND")
    assert gnd is not None and gnd.is_ground is True


def test_testpins_become_nails():
    board = parse_gencad(_GENCAD_DEMO, file_hash="sha256:x", board_id="x")
    assert len(board.nails) == 2
    nets = {nl.net for nl in board.nails}
    assert nets == {"+3V3", "GND"}


def test_shape_with_trailing_numeric_args_is_handled():
    """Real ASUS files write `SHAPE name 0 0` — extra numeric tokens after
    the shape name must not be treated as part of the name."""
    board = parse_gencad(_GENCAD_DEMO, file_hash="sha256:x", board_id="x")
    r2 = board.part_by_refdes("R2")
    assert r2 is not None
    # R2 uses `SHAPE RES_0402 0 0` — the parser must still resolve the shape
    # and emit pins for it.
    assert len(r2.pin_refs) == 2


def test_component_referencing_unknown_shape_emits_pinless_part():
    """We never fabricate pin data — a component pointing at an undefined
    shape gets an empty pin list rather than guessed positions."""
    text = (
        "$HEADER\nGENCAD 1.4\n$ENDHEADER\n"
        "$SHAPES\nSHAPE A\nPIN 1 P 0 0 TOP 0 0\nINSERT SMD\n$ENDSHAPES\n"
        "$COMPONENTS\nCOMPONENT R1\nPLACE 0 0\nLAYER TOP\nROTATION 0\n"
        "SHAPE NONEXISTENT\nDEVICE D\n$ENDCOMPONENTS\n"
    )
    board = parse_gencad(text, file_hash="sha256:x", board_id="x")
    assert len(board.parts) == 1
    assert board.parts[0].pin_refs == []
    assert len(board.pins) == 0


def test_missing_required_section_raises():
    text = "$HEADER\nGENCAD 1.4\n$ENDHEADER\n"
    with pytest.raises(MalformedHeaderError):
        parse_gencad(text, file_hash="sha256:x", board_id="x")


def test_non_gencad_payload_raises():
    with pytest.raises(InvalidBoardFile):
        parse_gencad("Lorem ipsum, no GENCAD marker.", file_hash="sha256:x", board_id="x")


def test_cad_dispatcher_routes_gencad_payload(tmp_path):
    """`.cad` parser must sniff GenCAD header and route through the GenCAD
    parser, not fall back to Test_Link-shape."""
    f = tmp_path / "demo.cad"
    f.write_text(_GENCAD_DEMO)
    board = CADParser().parse_file(f)
    assert board.source_format == "cad"
    assert len(board.parts) == 3
    assert len(board.pins) == 8  # R1×2 + R2×2 + U1×4 = 8


# ---------------------------------------------------------------------------
# Rich-section fixture — exercises $BOARD outline, $PADS shapes,
# $PADSTACKS layer mapping, and $ROUTES vias. Mirrors the real V386
# MSI RTX 2070 .cad layout (verified 2026-05-03).
# ---------------------------------------------------------------------------

_GENCAD_RICH = """$HEADER
GENCAD 1.4
USER "rich test"
DRAWING rich
UNITS USER 1000
ORIGIN 0 0
$ENDHEADER

$BOARD
ARTWORK board_outline TOP
LINE 0 0 1000 0
LINE 1000 0 1000 500
LINE 1000 500 0 500
LINE 0 500 0 0
ARTWORK silk1 TOP
LINE 100 100 200 100
ARC 200 100 100 100 150 100
$ENDBOARD

$PADS
PAD PS_RECT_0 RECTANGULAR -1
RECTANGLE -10 -10 20 20
PAD PS_CIRC_0 ROUND -1
CIRCLE 0 0 12
$ENDPADS

$PADSTACKS
PADSTACK PADSTACK_RECT 0
PAD PS_RECT_0 TOP 0 0
PAD PS_RECT_0 BOTTOM 0 0
PADSTACK PADSTACK_CIRC 0
PAD PS_CIRC_0 TOP 0 0
PAD PS_CIRC_0 BOTTOM 0 0
PADSTACK PADSTACK_VIA 8
PAD PS_CIRC_0 ALL 0 0
$ENDPADSTACKS

$SHAPES
SHAPE RES_0402
PIN 1  PADSTACK_RECT -10 0 TOP 0 0
PIN 2  PADSTACK_RECT 10 0 TOP 0 0
INSERT SMD
SHAPE QFN4
PIN 1  PADSTACK_CIRC -20 -20 TOP 0 0
PIN 2  PADSTACK_CIRC -20 20 TOP 0 0
PIN 3  PADSTACK_CIRC 20 20 TOP 0 0
PIN 4  PADSTACK_CIRC 20 -20 TOP 0 0
INSERT SMD
$ENDSHAPES

$COMPONENTS
COMPONENT R1
PLACE 100 200
LAYER TOP
ROTATION 0
SHAPE RES_0402
COMPONENT U1
PLACE 500 500
LAYER TOP
ROTATION 0
SHAPE QFN4
$ENDCOMPONENTS

$SIGNALS
SIGNAL +3V3
NODE R1 1
NODE U1 1
SIGNAL GND
NODE R1 2
$ENDSIGNALS

$TRACKS
TRACK 0 1.000
TRACK 1 5.000
$ENDTRACKS

$ROUTES
ROUTE GND
VIA PADSTACK_VIA 250.5 100.0 ALL -2 via1
VIA PADSTACK_VIA 750.5 400.0 ALL -2 via2
TRACK 1
LAYER TOP
LINE 250 100 750 100
LINE 750 100 750 400
LAYER BOTTOM
LINE 0 0 100 0
ROUTE +3V3
VIA PADSTACK_VIA 300.0 350.0 ALL -2 via3
TRACK 0
LAYER TOP
ARC 200 100 100 100 150 100
$ENDROUTES
"""


# ---------------------------------------------------------------------------
# A.1 + A.2 — pad shape extracted from $PADS and propagated via $PADSTACKS
# ---------------------------------------------------------------------------


def test_rectangular_pad_shape_propagated_to_pin():
    """`PADSTACK_RECT` references `PS_RECT_0` (RECTANGULAR 20×20). Every pin
    of a shape using PADSTACK_RECT must surface `pad_shape="rect"` and
    `pad_size=(20, 20)`."""
    board = parse_gencad(_GENCAD_RICH, file_hash="sha256:x", board_id="rich")
    r1_pin1 = next(p for p in board.pins if p.part_refdes == "R1" and p.index == 1)
    assert r1_pin1.pad_shape == "rect"
    assert r1_pin1.pad_size == (20.0, 20.0)


def test_round_pad_shape_propagated_to_pin():
    """`PADSTACK_CIRC` references `PS_CIRC_0` (ROUND r=12 → diameter 24).
    Every pin of QFN4 must surface `pad_shape="circle"` and
    `pad_size=(24, 24)`."""
    board = parse_gencad(_GENCAD_RICH, file_hash="sha256:x", board_id="rich")
    u1_pin1 = next(p for p in board.pins if p.part_refdes == "U1" and p.index == 1)
    assert u1_pin1.pad_shape == "circle"
    assert u1_pin1.pad_size == (24.0, 24.0)


# ---------------------------------------------------------------------------
# A.4 — $BOARD section emits silkscreen traces + arcs at layer 28
# ---------------------------------------------------------------------------


def test_board_section_emits_traces_for_each_line():
    """Every `LINE x1 y1 x2 y2` under `$BOARD` becomes a `Trace` at layer 28
    (the silkscreen layer the Three.js renderer uses for outline
    reconstruction). The four sides of the rectangle plus the silk1 segment
    = 5 traces."""
    board = parse_gencad(_GENCAD_RICH, file_hash="sha256:x", board_id="rich")
    silk_traces = [t for t in board.traces if t.layer == 28]
    assert len(silk_traces) == 5
    pts = {(t.a.x, t.a.y, t.b.x, t.b.y) for t in silk_traces}
    assert (0, 0, 1000, 0) in pts
    assert (1000, 0, 1000, 500) in pts
    assert (1000, 500, 0, 500) in pts
    assert (0, 500, 0, 0) in pts


def test_board_section_emits_arc_with_center_and_radius():
    """`ARC x1 y1 x2 y2 cx cy` becomes an `Arc(center, radius, angle_start,
    angle_end, layer=28)`. The arc in the fixture goes from (200,100) to
    (100,100) around (150,100) — radius 50, angles 0° → 180°."""
    board = parse_gencad(_GENCAD_RICH, file_hash="sha256:x", board_id="rich")
    silk_arcs = [a for a in board.arcs if a.layer == 28]
    assert len(silk_arcs) == 1
    arc = silk_arcs[0]
    assert (arc.center.x, arc.center.y) == (150, 100)
    assert math.isclose(arc.radius, 50.0, abs_tol=0.01)
    assert math.isclose(arc.angle_start, 0.0, abs_tol=0.01)
    assert math.isclose(abs(arc.angle_end), 180.0, abs_tol=0.01)


# ---------------------------------------------------------------------------
# A.5 — $ROUTES section emits vias on Board.vias
# ---------------------------------------------------------------------------


def test_routes_section_emits_vias_with_net_and_radius():
    """Each `VIA <padstack> x y <layer> <flags> <id>` line produces one
    `Via(pos, radius, net)`. Radius comes from the padstack drill diameter
    (or its referenced PAD CIRCLE radius)."""
    board = parse_gencad(_GENCAD_RICH, file_hash="sha256:x", board_id="rich")
    assert len(board.vias) == 3
    nets = sorted(v.net for v in board.vias if v.net)
    assert nets == ["+3V3", "GND", "GND"]
    # PADSTACK_VIA references PS_CIRC_0 (radius 12) → via radius 12.
    assert all(math.isclose(v.radius, 12.0, abs_tol=0.01) for v in board.vias)
    via1 = next(v for v in board.vias if math.isclose(v.pos.x, 250.5))
    assert via1.net == "GND"


# ---------------------------------------------------------------------------
# A.5+ — $ROUTES copper segments (LINE/ARC inside ROUTE blocks)
# ---------------------------------------------------------------------------


def test_routes_section_emits_copper_line_traces_per_layer_and_net():
    """`LINE x1 y1 x2 y2` inside a `ROUTE <net>` block, under a `LAYER TOP`
    or `LAYER BOTTOM` directive, becomes a `Trace` with layer=1 (TOP) or
    layer=16 (BOTTOM) — distinct from the 28 silkscreen layer. The trace
    inherits the current net and the current TRACK width."""
    board = parse_gencad(_GENCAD_RICH, file_hash="sha256:x", board_id="rich")
    copper_traces = [t for t in board.traces if t.layer in (1, 16)]
    assert len(copper_traces) == 3
    by_layer = {t.layer for t in copper_traces}
    assert by_layer == {1, 16}
    gnd_traces = [t for t in copper_traces if t.net == "GND"]
    assert len(gnd_traces) == 3  # 2 TOP + 1 BOTTOM
    # TRACK 1 = width 5.0 → all GND traces carry width=5.0
    assert all(math.isclose(t.width, 5.0) for t in gnd_traces)


def test_routes_section_emits_copper_arc_for_route_arc():
    """`ARC` inside a `ROUTE` block is also routed copper, not silkscreen.
    The arc emitted under `ROUTE +3V3 / LAYER TOP` carries layer=1."""
    board = parse_gencad(_GENCAD_RICH, file_hash="sha256:x", board_id="rich")
    copper_arcs = [a for a in board.arcs if a.layer in (1, 16)]
    assert len(copper_arcs) == 1
    assert copper_arcs[0].layer == 1
    assert math.isclose(copper_arcs[0].radius, 50.0, abs_tol=0.01)


# ---------------------------------------------------------------------------
# Regression — BOTTOM SMD with single-face padstack (the MSI v300 bug)
#
# Real-world `.cad` files (MSI V300, every modern board file from the XZZ
# corpus) declare SMD padstacks with ONLY a `LAYER TOP` entry — that's the
# GenCAD convention for surface-mount pads. When a component using such a
# padstack is placed on `LAYER BOTTOM` with `SHAPE foo MIRRORY FLIP`, every
# pin must end up on Layer.BOTTOM. A previous over-eager pad-face override
# remapped them to Layer.TOP because the padstack happened to ship a TOP
# pad — making the entire bottom side appear as empty rectangles in the
# viewer.
# ---------------------------------------------------------------------------

_GENCAD_BOTTOM_SMD = """$HEADER
GENCAD 1.4
USER "bottom-smd regression"
DRAWING demo
UNITS USER 1000
ORIGIN 0 0
$ENDHEADER

$BOARD
$ENDBOARD

$PADS
PAD PS_RECT_SMD RECTANGULAR -1
RECTANGLE -10 -10 20 20
$ENDPADS

$PADSTACKS
PADSTACK PAD_SMD_TOP_ONLY 0
PAD PS_RECT_SMD TOP 0 0
PAD PS_RECT_SMD SOLDERMASK_TOP 0 0
PAD PS_RECT_SMD SOLDERPASTE_TOP 0 0
$ENDPADSTACKS

$SHAPES
SHAPE RES_0603
PIN 1  PAD_SMD_TOP_ONLY -30 0 TOP 0 0
PIN 2  PAD_SMD_TOP_ONLY 30 0 TOP 0 0
INSERT SMD
$ENDSHAPES

$COMPONENTS
COMPONENT R_TOP
PLACE 100 100
LAYER TOP
ROTATION 0
SHAPE RES_0603 0 0
COMPONENT R_BOT
PLACE 500 500
LAYER BOTTOM
ROTATION 0
SHAPE RES_0603 MIRRORY FLIP
$ENDCOMPONENTS

$SIGNALS
$ENDSIGNALS
"""


def test_bottom_smd_pin_keeps_bottom_layer_with_top_only_padstack():
    """Component on LAYER BOTTOM whose padstack only declares `PAD ... TOP`
    must produce pins on Layer.BOTTOM. A regression here makes every BOTTOM
    SMD render as an empty silkscreen rectangle (no pads visible) on the
    Bottom view of any modern XZZ-style `.cad` file."""
    board = parse_gencad(_GENCAD_BOTTOM_SMD, file_hash="sha256:x", board_id="rb")

    top_pins = [p for p in board.pins if p.part_refdes == "R_TOP"]
    bot_pins = [p for p in board.pins if p.part_refdes == "R_BOT"]
    assert len(top_pins) == 2
    assert len(bot_pins) == 2

    # TOP-mounted component: pins on TOP, with the padstack's TOP pad.
    for p in top_pins:
        assert p.layer == Layer.TOP
        assert p.pad_shape == "rect"
        assert p.pad_size == (20.0, 20.0)

    # BOTTOM-mounted component: pins must stay on BOTTOM even though the
    # padstack only declares a TOP face. The pad geometry is reused from
    # TOP (it's the same physical SMD pad, just on the other side).
    for p in bot_pins:
        assert p.layer == Layer.BOTTOM, (
            f"R_BOT pin {p.index} ended up on {p.layer} — the parser "
            "remapped a BOTTOM pin onto TOP because its padstack only "
            "declared TOP. This breaks the Bottom view for every modern "
            ".cad file (regression of the MSI v300 fix)."
        )
        assert p.pad_shape == "rect"
        assert p.pad_size == (20.0, 20.0)


# ---------------------------------------------------------------------------
# Regression — preserve the original PCI Express edge-connector behavior:
# a padstack that ships BOTH TOP and BOTTOM pads is genuinely dual-face,
# and a SHAPES `PIN ... BOTTOM` directive on a TOP-mounted component must
# still place the pin on Layer.BOTTOM. This is what the override existed
# for in the first place.
# ---------------------------------------------------------------------------

_GENCAD_DUAL_FACE = """$HEADER
GENCAD 1.4
UNITS USER 1000
$ENDHEADER

$BOARD
$ENDBOARD

$PADS
PAD PS_RECT_DF RECTANGULAR -1
RECTANGLE -5 -5 10 10
$ENDPADS

$PADSTACKS
PADSTACK PAD_DUAL 0
PAD PS_RECT_DF TOP 0 0
PAD PS_RECT_DF BOTTOM 0 0
$ENDPADSTACKS

$SHAPES
SHAPE PEX_EDGE
PIN A1  PAD_DUAL -10 0 TOP 0 0
PIN B1  PAD_DUAL 10 0 BOTTOM 0 0
INSERT SMD
$ENDSHAPES

$COMPONENTS
COMPONENT J1
PLACE 0 0
LAYER TOP
ROTATION 0
SHAPE PEX_EDGE 0 0
$ENDCOMPONENTS

$SIGNALS
$ENDSIGNALS
"""


def test_dual_face_padstack_preserves_per_pin_layer():
    """When a padstack genuinely declares both TOP and BOTTOM pads (PCI
    Express edge connector pattern), the SHAPES `PIN ... BOTTOM` directive
    must still drop that specific pin on Layer.BOTTOM — even when the
    component frame is on TOP."""
    board = parse_gencad(_GENCAD_DUAL_FACE, file_hash="sha256:x", board_id="df")
    pins_by_name = {p.index: p for p in board.pins if p.part_refdes == "J1"}
    # Pin A1 declared TOP in the shape → on TOP.
    assert any(p.layer == Layer.TOP for p in board.pins if p.part_refdes == "J1")
    # Pin B1 declared BOTTOM in the shape → on BOTTOM despite frame on TOP.
    assert any(p.layer == Layer.BOTTOM for p in board.pins if p.part_refdes == "J1")
    assert pins_by_name  # keep the dict in scope for clarity


# ---------------------------------------------------------------------------
# Regression — synthesized board outline (the V300 "carte immense" bug)
#
# GenCAD `$BOARD` ARTWORK blocks are a grab-bag of silkscreen / fiducial /
# logo segments — NOT a closed PCB contour. The renderer's edge-chaining
# heuristic latches onto whichever fragment closes first and returns a
# tiny pseudo-outline (46×46 mm for a real 272×113 mm GPU PCB), which
# then drives the camera frustum so the whole board appears "immense"
# because the camera fits to a logo, not the actual extent. The parser
# must synthesize a rectangular bbox outline from every spatial entity
# (pins + vias + traces + a margin) and ship it as `Board.outline`.
# ---------------------------------------------------------------------------


def test_outline_synthesized_from_pins_when_board_section_is_silkscreen():
    """A GenCAD file with only silkscreen LINEs in `$BOARD` (no closed
    contour) must still produce a rectangular outline computed from the
    pin/via positions, with a small margin around them."""
    board = parse_gencad(_GENCAD_DEMO, file_hash="sha256:x", board_id="demo")
    assert board.outline, "synthesized outline must be non-empty"
    # 5-point closed rectangle (first == last).
    assert len(board.outline) == 5
    assert (board.outline[0].x, board.outline[0].y) == (board.outline[-1].x, board.outline[-1].y)

    # The outline must contain every pin, via, and trace endpoint.
    xs = [p.x for p in board.outline]
    ys = [p.y for p in board.outline]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    for pin in board.pins:
        assert minx <= pin.pos.x <= maxx
        assert miny <= pin.pos.y <= maxy

    # And it must be larger than the entity bbox by the parser's margin
    # (no degenerate zero-width outline that hugs a single fragment).
    pin_xs = [p.pos.x for p in board.pins]
    pin_ys = [p.pos.y for p in board.pins]
    assert minx < min(pin_xs)
    assert maxx > max(pin_xs)
    assert miny < min(pin_ys)
    assert maxy > max(pin_ys)


_GENCAD_MECH = """$HEADER
GENCAD 1.4
UNITS USER 1000
$ENDHEADER

$BOARD
$ENDBOARD

$PADS
PAD PS_R RECTANGULAR -1
RECTANGLE -5 -5 10 10
$ENDPADS

$PADSTACKS
PADSTACK PADSTACK_R 0
PAD PS_R TOP 0 0
PADSTACK PADSTACK_VIA 8
PAD PS_R ALL 0 0
$ENDPADSTACKS

$SHAPES
SHAPE RES_0402
PIN 1  PADSTACK_R -10 0 TOP 0 0
PIN 2  PADSTACK_R 10 0 TOP 0 0
INSERT SMD
SHAPE THM_PIN
PIN 1  PADSTACK_R 0 0 TOP 0 0
INSERT TH
$ENDSHAPES

$COMPONENTS
COMPONENT R1
PLACE 100 100
LAYER TOP
ROTATION 0
SHAPE RES_0402 0 0
COMPONENT J1
PLACE 5000 100
LAYER TOP
ROTATION 0
SHAPE THM_PIN 0 0
$ENDCOMPONENTS

$SIGNALS
SIGNAL GND
NODE R1 1
SIGNAL N1
NODE R1 2
$ENDSIGNALS

$TRACKS
TRACK 0 1
$ENDTRACKS

$ROUTES
ROUTE GND
VIA PADSTACK_VIA 100 100 ALL -2 v1
VIA PADSTACK_VIA 5000 100 TOP -2 v2
$ENDROUTES

$MECH
HOLE 8000 4000 160
HOLE 0 4000 160
HOLE 8000 0 160
HOLE 0 0 160
HOLE 4000 2000 50
$ENDMECH
"""


def test_mech_holes_extracted_with_fiducial_classification():
    """`$MECH HOLE x y diameter` lines surface as `Board.mech_holes`.
    Holes ≤ 100 mils ø are classified as fiducials, larger ones as
    fixation holes. Order preserved."""
    board = parse_gencad(_GENCAD_MECH, file_hash="sha256:x", board_id="mech")
    assert len(board.mech_holes) == 5
    fixations = [h for h in board.mech_holes if not h.is_fiducial]
    fiducials = [h for h in board.mech_holes if h.is_fiducial]
    assert len(fixations) == 4
    assert all(h.diameter == 160 for h in fixations)
    assert len(fiducials) == 1
    assert fiducials[0].diameter == 50
    assert (fiducials[0].pos.x, fiducials[0].pos.y) == (4000, 2000)


def test_outline_uses_mech_holes_when_at_least_three_fixation_holes():
    """When `$MECH` declares 3+ non-fiducial holes, the synthesized
    outline is the bbox of those holes (the screws sit at the actual
    PCB corners). The fiducial is excluded so a centre dot doesn't
    skew the bounds. The outline must include all entities — including
    the centre fiducial — so we know the bbox really covers the board."""
    board = parse_gencad(_GENCAD_MECH, file_hash="sha256:x", board_id="mech")
    assert board.outline
    xs = [p.x for p in board.outline]
    ys = [p.y for p in board.outline]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    # Bbox of fixation holes is [0, 8000] × [0, 4000], padded by half a
    # hole diameter (80 mils).
    assert minx == -80.0
    assert maxx == 8080.0
    assert miny == -80.0
    assert maxy == 4080.0


def test_via_carries_padstack_and_layer_span():
    """`VIA <padstack> <x> <y> <layer_span> ...` from `$ROUTES` must
    populate `Via.padstack` and `Via.layer_span` so the renderer can
    distinguish the 14+ via types a single GenCAD board ships."""
    board = parse_gencad(_GENCAD_MECH, file_hash="sha256:x", board_id="mech")
    assert len(board.vias) == 2
    v1, v2 = board.vias
    assert v1.padstack == "PADSTACK_VIA"
    assert v1.layer_span == "ALL"
    assert v2.padstack == "PADSTACK_VIA"
    assert v2.layer_span == "TOP"


def test_is_smd_propagated_from_shape_insert_directive():
    """A shape with `INSERT SMD` must produce `Part.is_smd=True`,
    `INSERT TH` (or PTH/THM) must produce `Part.is_smd=False`. Required
    for through-hole connectors / sockets to render distinctly from SMD
    components."""
    board = parse_gencad(_GENCAD_MECH, file_hash="sha256:x", board_id="mech")
    r1 = board.part_by_refdes("R1")
    j1 = board.part_by_refdes("J1")
    assert r1 is not None and r1.is_smd is True
    assert j1 is not None and j1.is_smd is False


def test_outline_empty_when_board_has_no_geometry():
    """A degenerate GenCAD file with `$COMPONENTS` referencing only an
    unknown shape (no pins emitted, no vias, no traces) gracefully
    produces an empty outline rather than a crash."""
    empty_geom = """$HEADER
GENCAD 1.4
UNITS USER 1000
$ENDHEADER

$BOARD
$ENDBOARD

$SHAPES
$ENDSHAPES

$COMPONENTS
COMPONENT GHOST
PLACE 0 0
LAYER TOP
ROTATION 0
SHAPE NOT_DEFINED
$ENDCOMPONENTS

$SIGNALS
$ENDSIGNALS
"""
    board = parse_gencad(empty_geom, file_hash="sha256:x", board_id="empty")
    assert board.outline == []
