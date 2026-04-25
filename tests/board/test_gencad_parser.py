"""Tests for the GenCAD 1.4 parser — the format real `.cad` files actually
ship in (verified against ASUS Prime A520M and GRANGER 6050A2977701)."""

from __future__ import annotations

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
