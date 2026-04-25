"""Tests for the shared ASCII-boardview helper used by the Test_Link-shape
dialects. The canonical Test_Link parser lives in `test_link.py` and has
its own tests; this file exercises the helper's dialect knobs."""

from __future__ import annotations

import pytest

from api.board.model import Layer
from api.board.parser._ascii_boardview import (
    GROUND_RE,
    POWER_RE,
    DialectMarkers,
    derive_nets,
    normalize_bbox,
    parse_test_link_shape,
)
from api.board.parser.base import (
    InvalidBoardFile,
    MalformedHeaderError,
    PinPartMismatchError,
)


def test_power_and_ground_regex_cover_common_names():
    for name in ("+3V3", "5V", "1V8_AUDIO", "VCC", "VCC_IO", "VDD_CORE", "V_USB"):
        assert POWER_RE.match(name), f"POWER missed {name}"
    for name in ("GND", "VSS", "AGND", "DGND", "PGND"):
        assert GROUND_RE.match(name), f"GROUND missed {name}"
    for name in ("CLK", "DATA", "RESETn", "MOSI"):
        assert not POWER_RE.match(name)
        assert not GROUND_RE.match(name)


def test_normalize_bbox_flips_inverted_corners():
    a, b = normalize_bbox(50, 200, 30, 80)
    assert (a.x, a.y) == (30, 80)
    assert (b.x, b.y) == (50, 200)


def test_parse_canonical_test_link_text():
    text = (
        "str_length: 1024 512\n"
        "var_data: 4 2 4 1\n"
        "Format:\n"
        "0 0\n"
        "1000 0\n"
        "1000 500\n"
        "0 500\n"
        "Parts:\n"
        "R1 5 2\n"
        "C1 10 4\n"
        "Pins:\n"
        "100 100 -99 1 +3V3\n"
        "100 200 -99 1 GND\n"
        "400 100 1 2 +3V3\n"
        "400 200 -99 2 GND\n"
        "Nails:\n"
        "1 400 100 1 +3V3\n"
    )
    board = parse_test_link_shape(
        text,
        markers=DialectMarkers(),
        source_format="test-helper",
        board_id="helper-demo",
        file_hash="sha256:fixture",
    )
    assert board.source_format == "test-helper"
    assert len(board.outline) == 4
    assert len(board.parts) == 2
    assert len(board.pins) == 4
    assert len(board.nails) == 1
    assert board.part_by_refdes("R1").layer == Layer.TOP
    assert board.part_by_refdes("R1").is_smd is True  # bit 0x4 set in type=5
    assert board.part_by_refdes("C1").layer == Layer.BOTTOM  # bit 0x2 set in type=10
    gnd = board.net_by_name("GND")
    assert gnd is not None and gnd.is_ground is True


def test_custom_markers_handle_components_and_testpoints():
    """Dialect with `Components:`, `Nets:`, `TestPoints:` markers (BoardView R5-like)."""
    markers = DialectMarkers(
        header_count_marker="var_data:",
        outline_markers=("Format:",),
        parts_markers=("Components:",),
        pins_markers=("Pins:",),
        nails_markers=("TestPoints:",),
    )
    text = (
        "var_data: 0 1 2 0\n"
        "Components:\n"
        "U1 5 2\n"
        "Pins:\n"
        "10 10 -99 1 +5V\n"
        "20 10 -99 1 GND\n"
    )
    board = parse_test_link_shape(
        text,
        markers=markers,
        source_format="r5",
        board_id="demo",
        file_hash="sha256:x",
    )
    assert [p.refdes for p in board.parts] == ["U1"]
    assert len(board.pins) == 2
    assert board.net_by_name("+5V").is_power is True


def test_empty_payload_raises_invalid():
    with pytest.raises(InvalidBoardFile):
        parse_test_link_shape(
            "",
            markers=DialectMarkers(),
            source_format="test",
            board_id="x",
            file_hash="sha256:x",
        )


def test_unrelated_text_raises_invalid():
    with pytest.raises(InvalidBoardFile):
        parse_test_link_shape(
            "Lorem ipsum dolor sit amet.\nNothing useful here.\n",
            markers=DialectMarkers(),
            source_format="test",
            board_id="x",
            file_hash="sha256:x",
        )


def test_malformed_var_data_raises():
    with pytest.raises(MalformedHeaderError):
        parse_test_link_shape(
            "var_data: 4 not-an-int 4 1\nFormat:\n0 0\n",
            markers=DialectMarkers(),
            source_format="test",
            board_id="x",
            file_hash="sha256:x",
        )


def test_pin_referring_unknown_part_raises():
    text = (
        "var_data: 0 1 1 0\n"
        "Parts:\n"
        "R1 5 1\n"
        "Pins:\n"
        "10 10 -99 7\n"  # part_idx=7, only 1 part exists
    )
    with pytest.raises(PinPartMismatchError):
        parse_test_link_shape(
            text,
            markers=DialectMarkers(),
            source_format="test",
            board_id="x",
            file_hash="sha256:x",
        )


def test_nails_backfill_net_when_pin_net_empty():
    text = (
        "var_data: 0 1 1 1\n"
        "Parts:\n"
        "R1 5 1\n"
        "Pins:\n"
        "100 100 1 1\n"  # no explicit net, probe=1
        "Nails:\n"
        "1 100 100 1 +3V3\n"
    )
    board = parse_test_link_shape(
        text,
        markers=DialectMarkers(),
        source_format="test",
        board_id="x",
        file_hash="sha256:x",
    )
    assert board.pins[0].net == "+3V3"
    assert board.net_by_name("+3V3") is not None


def test_derive_nets_sorts_alphabetically_and_flags_power_ground():
    from api.board.model import Pin, Point

    pins = [
        Pin(part_refdes="R1", index=1, pos=Point(x=0, y=0), net="GND", layer=Layer.TOP),
        Pin(part_refdes="R1", index=2, pos=Point(x=1, y=0), net="+3V3", layer=Layer.TOP),
        Pin(part_refdes="R2", index=1, pos=Point(x=2, y=0), net="CLK", layer=Layer.TOP),
    ]
    nets = derive_nets(pins)
    assert [n.name for n in nets] == ["+3V3", "CLK", "GND"]
    assert nets[0].is_power is True
    assert nets[2].is_ground is True
    assert nets[1].is_power is False and nets[1].is_ground is False
