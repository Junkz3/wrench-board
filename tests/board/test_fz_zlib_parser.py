"""Tests for the FZ-zlib variant — the most common real-world `.fz` layout
(Quanta, ASRock, ASUS Prime, Gigabyte boards). Synthetic fixtures only;
real-world files are exercised by `test_real_files_runner.py`."""

from __future__ import annotations

import math
import struct
import zlib

import pytest

from api.board.model import Layer
from api.board.parser._fz_zlib import looks_like_fz_zlib, parse_fz_zlib
from api.board.parser.base import InvalidBoardFile, MalformedHeaderError
from api.board.parser.fz import FZParser


def _wrap_fz_zlib(text: str) -> bytes:
    """Wrap plaintext as a real FZ-zlib container: 4-byte LE size + zlib body."""
    body = text.encode("utf-8")
    compressed = zlib.compress(body)
    header = struct.pack("<I", len(body))
    return header + compressed


def _wrap_fz_two_streams(content_text: str, bom_text: str) -> bytes:
    """Build a real-shape `.fz` container with both content + BOM streams.

    Layout matches what the AMD/ASUS dumps ship:
      [0..4)              LE32 size of content (decompressed)
      [4..S1_END)         zlib stream 1 (pipe-delimited content)
      [S1_END..S1_END+8)  8-byte stream-2 header (we fill with anything)
      [S1_END+8..)        zlib stream 2 (BOM)
      last 4 bytes        LE32 trailer (parser ignores it; OBV uses it)
    """
    s1 = zlib.compress(content_text.encode("utf-8"))
    s2 = zlib.compress(bom_text.encode("utf-8"))
    body = (
        struct.pack("<I", len(content_text)) + s1
        + b"\x00" * 4 + struct.pack("<I", len(bom_text)) + s2
    )
    return body + struct.pack("<I", len(body))


_PLAINTEXT = """A!REFDES!COMP_INSERTION_CODE!SYM_NAME!SYM_MIRROR!SYM_ROTATE!
S!R1!1!RES_0402!NO!0!
S!C1!1!CAP_0402!YES!90!
S!U1!1!QFN32!NO!180!
A!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!PIN_X!PIN_Y!TEST_POINT!RADIUS!
S!+3V3!R1!0!1!100!200!!10!
S!GND!R1!0!2!200!200!!10!
S!+3V3!C1!0!1!300.5!400.5!!10!
S!GND!C1!0!2!400!400!!10!
S!CLK!U1!0!1!500!500!!10!
S!DATA!U1!0!2!600!500!!10!
A!TESTVIA!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!VIA_X!VIA_Y!TEST_POINT!RADIUS!
S!1!+3V3!R1!0!1!100!200!!10!
"""


def test_looks_like_fz_zlib_detects_zlib_magic_at_offset_4():
    raw = _wrap_fz_zlib("hello")
    assert looks_like_fz_zlib(raw) is True
    # Reject random garbage
    assert looks_like_fz_zlib(b"random text without zlib") is False


def test_parses_synthetic_fz_zlib():
    raw = _wrap_fz_zlib(_PLAINTEXT)
    board = parse_fz_zlib(raw, file_hash="sha256:x", board_id="demo")
    assert board.source_format == "fz"
    assert [p.refdes for p in board.parts] == ["R1", "C1", "U1"]
    assert len(board.pins) == 6

    # Layer mapping: SYM_MIRROR YES → BOTTOM, NO → TOP
    assert board.part_by_refdes("R1").layer == Layer.TOP
    assert board.part_by_refdes("C1").layer == Layer.BOTTOM
    assert board.part_by_refdes("U1").layer == Layer.TOP

    # Footprint enrichment from SYM_NAME
    assert board.part_by_refdes("R1").footprint == "RES_0402"
    assert board.part_by_refdes("U1").rotation_deg == 180.0

    # Float pin positions are rounded to int
    c1_pin1 = next(pin for pin in board.pins if pin.part_refdes == "C1" and pin.index == 1)
    assert c1_pin1.pos.x in (300, 301)  # 300.5 rounds to nearest even/up
    assert c1_pin1.pos.y in (400, 401)

    # Power/ground classification
    assert board.net_by_name("+3V3").is_power is True
    assert board.net_by_name("GND").is_ground is True

    # 1 nail from TESTVIA
    assert len(board.nails) == 1


def test_fz_dispatcher_routes_zlib_variant_without_key():
    """FZParser() without a key still parses zlib-flavoured `.fz`."""
    raw = _wrap_fz_zlib(_PLAINTEXT)
    board = FZParser().parse(raw, file_hash="sha256:x", board_id="x")
    assert board.source_format == "fz"
    assert len(board.parts) == 3


def test_invalid_zlib_body_raises_clean_error():
    """A 4-byte header but garbage zlib stream must surface a clear error,
    not a Python zlib traceback."""
    bad = struct.pack("<I", 100) + b"this is not zlib"
    # Should fall through dispatcher to xor path → MissingFZKeyError, but
    # parse_fz_zlib called directly raises InvalidBoardFile.
    with pytest.raises(InvalidBoardFile):
        parse_fz_zlib(bad, file_hash="sha256:x", board_id="x")


def test_missing_required_section_raises_malformed():
    """A zlib body without REFDES or NET_NAME schemas must raise."""
    raw = _wrap_fz_zlib("A!OTHER!a!b!\nS!x!y!z!\n")
    with pytest.raises(MalformedHeaderError):
        parse_fz_zlib(raw, file_hash="sha256:x", board_id="x")


def test_orphan_pin_referencing_unknown_refdes_is_dropped():
    """A pin row whose REFDES isn't in the parts section must be silently
    dropped — we never fabricate a part to satisfy a pin (anti-hallucination)."""
    text = (
        "A!REFDES!COMP_INSERTION_CODE!SYM_NAME!SYM_MIRROR!SYM_ROTATE!\n"
        "S!R1!1!FOO!NO!0!\n"
        "A!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!PIN_X!PIN_Y!TEST_POINT!RADIUS!\n"
        "S!+3V3!R1!0!1!10!10!!1!\n"
        "S!+5V!UNKNOWN_PART!0!1!50!50!!1!\n"
    )
    board = parse_fz_zlib(_wrap_fz_zlib(text), file_hash="sha256:x", board_id="x")
    assert len(board.parts) == 1
    assert len(board.pins) == 1


def test_pin_index_falls_back_to_sequential_when_pin_name_alphanumeric():
    """PIN_NAME like 'A1' → fallback to 1-based sequential per part."""
    text = (
        "A!REFDES!COMP_INSERTION_CODE!SYM_NAME!SYM_MIRROR!SYM_ROTATE!\n"
        "S!U1!1!BGA!NO!0!\n"
        "A!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!PIN_X!PIN_Y!TEST_POINT!RADIUS!\n"
        "S!N1!U1!0!A1!0!0!!1!\n"
        "S!N2!U1!0!A2!10!0!!1!\n"
        "S!N3!U1!0!B1!0!10!!1!\n"
    )
    board = parse_fz_zlib(_wrap_fz_zlib(text), file_hash="sha256:x", board_id="x")
    indices = [p.index for p in board.pins]
    assert indices == [1, 2, 3]


def test_pin_radius_propagates_to_pad_shape_and_pad_size():
    """The 8th column of NET_NAME (RADIUS, in mils) is the per-pin pad
    radius. With an unrecognised footprint the inference returns None
    and the radius-derived default takes over: `pad_shape="circle"`
    with `pad_size=(2r, 2r)`."""
    text = (
        "A!REFDES!COMP_INSERTION_CODE!SYM_NAME!SYM_MIRROR!SYM_ROTATE!\n"
        "S!U1!1!CUSTOM_NOMATCH!NO!0!\n"
        "A!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!PIN_X!PIN_Y!TEST_POINT!RADIUS!\n"
        "S!+3V3!U1!0!1!0!0!!12.5!\n"
        "S!GND!U1!0!2!10!0!!8.0!\n"
    )
    board = parse_fz_zlib(_wrap_fz_zlib(text), file_hash="sha256:x", board_id="x")
    pin1 = next(p for p in board.pins if p.index == 1)
    assert pin1.pad_shape == "circle"
    assert pin1.pad_size == (25.0, 25.0)
    pin2 = next(p for p in board.pins if p.index == 2)
    assert pin2.pad_shape == "circle"
    assert pin2.pad_size == (16.0, 16.0)


def test_unit_millimeters_directive_converts_pin_coords_to_mils():
    """Real-world `.fz` files shipped from millimeter-native CAD tools
    carry a top-level `UNIT:millimeters` directive. Without conversion
    the pins land 25× too close together (1 mm = 39.37 mils), the
    viewer auto-fits the board to a postage-stamp window, and pad
    diameters round to 0. The parser must scale every coordinate +
    pad radius by 39.3700787 when this directive is present."""
    text = (
        "UNIT:millimeters\n"
        "A!REFDES!COMP_INSERTION_CODE!SYM_NAME!SYM_MIRROR!SYM_ROTATE!\n"
        "S!R1!1!RES_0402!NO!0!\n"
        "A!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!PIN_X!PIN_Y!TEST_POINT!RADIUS!\n"
        "S!+3V3!R1!0!1!10!20!!0.25!\n"
    )
    board = parse_fz_zlib(_wrap_fz_zlib(text), file_hash="sha256:x", board_id="x")
    pin = board.pins[0]
    # 10 mm → 393 mils (rounded), 20 mm → 787 mils
    assert pin.pos.x == 394
    assert pin.pos.y == 787
    # pad radius 0.25 mm → 9.84 mils → diameter 19.68 mils
    assert pin.pad_size is not None
    assert math.isclose(pin.pad_size[0], 0.25 * 2 * 39.3700787, abs_tol=0.01)


def test_unit_directive_absent_keeps_mil_default():
    """No `UNIT:` line → treat coords as already in mils (matches the
    GTX 1080 .fz observed in the wild). No scaling applied."""
    text = (
        "A!REFDES!COMP_INSERTION_CODE!SYM_NAME!SYM_MIRROR!SYM_ROTATE!\n"
        "S!R1!1!RES_0402!NO!0!\n"
        "A!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!PIN_X!PIN_Y!TEST_POINT!RADIUS!\n"
        "S!+3V3!R1!0!1!500!1000!!10!\n"
    )
    board = parse_fz_zlib(_wrap_fz_zlib(text), file_hash="sha256:x", board_id="x")
    pin = board.pins[0]
    assert pin.pos.x == 500
    assert pin.pos.y == 1000
    assert pin.pad_size == (20.0, 20.0)


def test_unit_millimeters_scales_testvia_coordinates():
    """TESTVIA_X / TESTVIA_Y must use the same mm→mil scale as pin coords —
    otherwise the nail floats off the board on mm-native files."""
    text = (
        "UNIT:millimeters\n"
        "A!REFDES!COMP_INSERTION_CODE!SYM_NAME!SYM_MIRROR!SYM_ROTATE!\n"
        "S!R1!1!RES_0402!NO!0!\n"
        "A!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!PIN_X!PIN_Y!TEST_POINT!RADIUS!\n"
        "S!+3V3!R1!0!1!1!1!!0.1!\n"
        "A!TESTVIA!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!VIA_X!VIA_Y!TEST_POINT!RADIUS!\n"
        "S!1!+3V3!R1!0!1!5!10!!0.1!\n"
    )
    board = parse_fz_zlib(_wrap_fz_zlib(text), file_hash="sha256:x", board_id="x")
    assert len(board.nails) == 1
    nail = board.nails[0]
    # 5 mm → 197 mils, 10 mm → 394 mils
    assert nail.pos.x == 197
    assert nail.pos.y == 394


def test_bom_stream_descriptions_attach_to_part_value():
    """The second zlib stream (BOM) carries `PARTNUMBER \\t DESCRIPTION \\t
    QTY \\t LOCATION \\t PARTNUMBER2` rows. Each space-separated refdes
    listed under LOCATION must inherit the DESCRIPTION on its
    `Part.value`. Without this, the parser surfaces no human-readable
    value for any component on `.fz` boards."""
    content = (
        "A!REFDES!COMP_INSERTION_CODE!SYM_NAME!SYM_MIRROR!SYM_ROTATE!\n"
        "S!R1!1!RES_0402!NO!0!\n"
        "S!C1!1!CAP_0402!YES!90!\n"
        "S!U1!1!QFN!NO!0!\n"
        "A!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!PIN_X!PIN_Y!TEST_POINT!RADIUS!\n"
        "S!+3V3!R1!0!1!100!200!!10!\n"
        "S!GND!C1!0!1!300!400!!10!\n"
        "S!CLK!U1!0!1!500!500!!10!\n"
    )
    bom = (
        "GTX1080-8GD5X_2.0||||60PD01W0-VG0B04\n"
        "PARTNUMBER\tDESCRIPTION\tQTY\tLOCATION\tPARTNUMBER2\n"
        "10G212100114030\tRES 100K OHM 1/16W (0402) 1%\t2\tR1 R2\t\n"
        "11G233110001234\tMLCC 0.1UF/16V(0402)X7R\t1\tC1\t\n"
        "07005-A0330100\tN-MOSFET NTMFS4C06NBT1G\t1\tU1\t\n"
    )
    raw = _wrap_fz_two_streams(content, bom)
    board = parse_fz_zlib(raw, file_hash="sha256:x", board_id="x")
    assert board.part_by_refdes("R1").value == "RES 100K OHM 1/16W (0402) 1%"
    assert board.part_by_refdes("C1").value == "MLCC 0.1UF/16V(0402)X7R"
    assert board.part_by_refdes("U1").value == "N-MOSFET NTMFS4C06NBT1G"


def test_bom_stream_absent_leaves_part_value_none():
    """No BOM stream → no fabrication. Part.value stays None."""
    raw = _wrap_fz_zlib(_PLAINTEXT)
    board = parse_fz_zlib(raw, file_hash="sha256:x", board_id="x")
    for p in board.parts:
        assert p.value is None


def test_outline_synthesized_from_pin_bbox():
    """The `.fz` format never ships a board outline. The parser must
    synthesize a closed bbox polygon from pin positions plus a small
    margin so the Three.js viewer has board chrome to draw — otherwise
    the user sees floating components on a void."""
    raw = _wrap_fz_zlib(_PLAINTEXT)
    board = parse_fz_zlib(raw, file_hash="sha256:x", board_id="x")
    # _PLAINTEXT pins land in x ∈ [100, 600], y ∈ [200, 500]
    assert len(board.outline) >= 4
    xs = [p.x for p in board.outline]
    ys = [p.y for p in board.outline]
    assert min(xs) < 100
    assert max(xs) > 600
    assert min(ys) < 200
    assert max(ys) > 500
    # Closed polygon: first and last point match
    assert (board.outline[0].x, board.outline[0].y) == (
        board.outline[-1].x,
        board.outline[-1].y,
    )


def test_outline_empty_when_no_pins():
    """Zero pins → zero outline points (don't fabricate a 0×0 box)."""
    text = (
        "A!REFDES!COMP_INSERTION_CODE!SYM_NAME!SYM_MIRROR!SYM_ROTATE!\n"
        "S!R1!1!FOO!NO!0!\n"
        "A!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!PIN_X!PIN_Y!TEST_POINT!RADIUS!\n"
    )
    raw = _wrap_fz_zlib(text)
    board = parse_fz_zlib(raw, file_hash="sha256:x", board_id="x")
    assert board.outline == []


def test_dnp_alternates_detected_for_overlapping_unplaced_part():
    """`.fz` files encode DFM alternates: two parts sharing the same
    physical pads, only one populated per board variant. The placed
    one is listed in the BOM; the alternate has no BOM value. The
    parser must flag the unplaced ghost with `is_dnp=True` and attach
    its refdes to the placed part's `dnp_alternates`."""
    content = (
        "A!REFDES!COMP_INSERTION_CODE!SYM_NAME!SYM_MIRROR!SYM_ROTATE!\n"
        "S!C30!1!C30!NO!0!\n"
        "S!R54!1!R54!NO!0!\n"
        "S!U1!1!U1!NO!0!\n"  # standalone — no overlap
        "A!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!PIN_X!PIN_Y!TEST_POINT!RADIUS!\n"
        "S!N1!C30!0!1!100!100!!5!\n"
        "S!N2!C30!0!2!200!100!!5!\n"
        "S!N3!R54!0!1!100!100!!5!\n"
        "S!N4!R54!0!2!200!100!!5!\n"
        "S!N5!U1!0!1!500!500!!5!\n"
    )
    bom = (
        "BOARD_HEADER\n"
        "PARTNUMBER\tDESCRIPTION\tQTY\tLOCATION\tPARTNUMBER2\n"
        "PN1\tRES 0 OHM JUMP\t1\tR54\t\n"           # only R54 placed
        "PN2\tCUSTOM_PART\t1\tU1\t\n"               # U1 placed (no overlap)
    )
    raw = _wrap_fz_two_streams(content, bom)
    board = parse_fz_zlib(raw, file_hash="x", board_id="x")
    r54 = board.part_by_refdes("R54")
    c30 = board.part_by_refdes("C30")
    u1 = board.part_by_refdes("U1")
    assert r54.is_dnp is False
    assert c30.is_dnp is True
    assert "C30" in r54.dnp_alternates
    assert c30.dnp_alternates == []
    # Standalone placed part has no alternates.
    assert u1.is_dnp is False
    assert u1.dnp_alternates == []


def test_dnp_alternates_unplaced_with_no_overlap_stays_unflagged():
    """A part without a BOM value that does NOT overlap any placed part
    is just a regular non-stuffed component (no BOM listing) and stays
    `is_dnp=False`. We only flag overlap-style alternates."""
    content = (
        "A!REFDES!COMP_INSERTION_CODE!SYM_NAME!SYM_MIRROR!SYM_ROTATE!\n"
        "S!R1!1!R1!NO!0!\n"
        "S!R2!1!R2!NO!0!\n"
        "A!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!PIN_X!PIN_Y!TEST_POINT!RADIUS!\n"
        "S!N1!R1!0!1!100!100!!5!\n"
        "S!N2!R2!0!1!1000!1000!!5!\n"  # far away
    )
    bom = (
        "BOARD\nPARTNUMBER\tDESCRIPTION\tQTY\tLOCATION\tPARTNUMBER2\n"
        "PN1\tRES 10K (0402)\t1\tR1\t\n"  # R1 placed; R2 has no value
    )
    raw = _wrap_fz_two_streams(content, bom)
    board = parse_fz_zlib(raw, file_hash="x", board_id="x")
    r1 = board.part_by_refdes("R1")
    r2 = board.part_by_refdes("R2")
    assert r1.is_dnp is False
    assert r1.dnp_alternates == []
    assert r2.is_dnp is False  # missing-from-BOM ≠ DNP alternate
    assert r2.dnp_alternates == []


def test_pin_pad_shape_falls_back_to_bom_description():
    """Asus DUAL 1060 dumps duplicate the refdes in `SYM_NAME` so the
    footprint inference returns None. The BOM stream carries the real
    package keyword (`MLCC 0.1UF/16V(0402)X7R`, `N-MOSFET BSS138 SOT-23`).
    `_build_pins` must fall back to inferring against `Part.value`
    when the footprint inference is empty."""
    content = (
        "A!REFDES!COMP_INSERTION_CODE!SYM_NAME!SYM_MIRROR!SYM_ROTATE!\n"
        "S!C1!1!C1!NO!0!\n"        # SYM_NAME = REFDES (1060 dump style)
        "S!Q1!1!Q1!NO!0!\n"
        "S!XYZ!1!XYZ!NO!0!\n"
        "A!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!PIN_X!PIN_Y!TEST_POINT!RADIUS!\n"
        "S!N1!C1!0!1!10!10!!5!\n"
        "S!N2!Q1!0!1!20!20!!5!\n"
        "S!N3!XYZ!0!1!30!30!!5!\n"
    )
    bom = (
        "BOARD_HEADER\n"
        "PARTNUMBER\tDESCRIPTION\tQTY\tLOCATION\tPARTNUMBER2\n"
        "PN1\tMLCC 0.1UF/16V(0402)X7R 10%\t1\tC1\t\n"
        "PN2\tN-MOSFET BSS138 SOT-23\t1\tQ1\t\n"
        "PN3\tBLACK_BOX_NO_PACKAGE_KEYWORDS\t1\tXYZ\t\n"
    )
    raw = _wrap_fz_two_streams(content, bom)
    board = parse_fz_zlib(raw, file_hash="sha256:x", board_id="x")
    by_refdes = {p.part_refdes: p for p in board.pins}
    assert by_refdes["C1"].pad_shape == "rect"   # via BOM "MLCC ... (0402)"
    assert by_refdes["Q1"].pad_shape == "rect"   # via BOM "SOT-23"
    assert by_refdes["XYZ"].pad_shape == "circle"  # no keyword either side → default


def test_pin_pad_shape_inferred_from_part_footprint():
    """Pins on a chip-passive part (`SYM_NAME` like `R0402_H16`,
    `CAP1005_0_55H`, `RES1005`) must surface `pad_shape="rect"` even
    though the FZ format itself only ships a radius. The inference
    runs against the parsed footprint name.

    Pins on a BGA / mounting / test-point part keep `pad_shape="circle"`.
    Pins on an unrecognised footprint fall back to the radius-derived
    circle (the parser's existing default)."""
    text = (
        "A!REFDES!COMP_INSERTION_CODE!SYM_NAME!SYM_MIRROR!SYM_ROTATE!\n"
        "S!R1!1!R0402_H16!NO!0!\n"
        "S!C1!1!CAP1005_0_55H!NO!0!\n"
        "S!U1!1!QFN32!NO!0!\n"
        "S!U2!1!BGA080_12_5X15!NO!0!\n"
        "S!MH1!1!MTG3_175_8VIAS!NO!0!\n"
        "S!XYZ!1!CUSTOM_NOMATCH!NO!0!\n"
        "A!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!PIN_X!PIN_Y!TEST_POINT!RADIUS!\n"
        "S!N1!R1!0!1!10!10!!5!\n"
        "S!N2!C1!0!1!20!20!!5!\n"
        "S!N3!U1!0!1!30!30!!5!\n"
        "S!N4!U2!0!1!40!40!!5!\n"
        "S!N5!MH1!0!1!50!50!!5!\n"
        "S!N6!XYZ!0!1!60!60!!5!\n"
    )
    board = parse_fz_zlib(_wrap_fz_zlib(text), file_hash="sha256:x", board_id="x")
    by_refdes = {p.part_refdes: p for p in board.pins}
    assert by_refdes["R1"].pad_shape == "rect"     # R0402_H16 → chip
    assert by_refdes["C1"].pad_shape == "rect"     # CAP1005_0_55H → chip
    assert by_refdes["U1"].pad_shape == "rect"     # QFN32 → leaded
    assert by_refdes["U2"].pad_shape == "circle"   # BGA → grid
    assert by_refdes["MH1"].pad_shape == "circle"  # MTG → mounting
    assert by_refdes["XYZ"].pad_shape == "circle"  # unknown → default circle


def test_pin_with_missing_or_zero_radius_has_no_pad_shape():
    """Conservative — only emit pad_shape when the file actually carries a
    positive numeric RADIUS. Empty / 0 / non-numeric stays `None` so the
    renderer keeps using its global pin size fallback."""
    text = (
        "A!REFDES!COMP_INSERTION_CODE!SYM_NAME!SYM_MIRROR!SYM_ROTATE!\n"
        "S!U1!1!QFN!NO!0!\n"
        "A!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!PIN_X!PIN_Y!TEST_POINT!RADIUS!\n"
        "S!+3V3!U1!0!1!0!0!!!\n"
        "S!GND!U1!0!2!10!0!!0!\n"
    )
    board = parse_fz_zlib(_wrap_fz_zlib(text), file_hash="sha256:x", board_id="x")
    for pin in board.pins:
        assert pin.pad_shape is None
        assert pin.pad_size is None
