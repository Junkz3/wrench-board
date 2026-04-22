"""Parser for OpenBoardView BRD2 format."""

from pathlib import Path

import pytest

from api.board.model import Layer
from api.board.parser.base import (
    InvalidBoardFile,
    MalformedHeaderError,
)
from api.board.parser.brd2 import BRD2Parser

FIXTURE_DIR = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_parses_mnt_reform_motherboard():
    """The committed MNT Reform BRD2 fixture must parse cleanly and match header counts."""
    path = REPO_ROOT / "board_assets" / "mnt-reform-motherboard.brd"
    board = BRD2Parser().parse_file(path)

    assert board.source_format == "brd2"
    assert board.board_id == "mnt-reform-motherboard"
    assert len(board.parts) == 493
    assert len(board.pins) == 2104
    assert len(board.nets) == 647
    assert len(board.nails) == 5
    assert len(board.outline) == 9

    # Spot-check a known component : C2 should exist on the top layer.
    c2 = board.part_by_refdes("C2")
    assert c2 is not None
    assert c2.layer == Layer.TOP

    # Known net should classify as ground.
    gnd = board.net_by_name("GND")
    assert gnd is not None
    assert gnd.is_ground is True

    # HDMI differential-pair nets exist under their real names.
    hdmi = board.net_by_name("HDMI_D2+")
    assert hdmi is not None


def test_part_bbox_is_normalized_to_min_max(tmp_path: Path):
    """PART lines with y1 > y2 (common in whitequark converter output after Y-flip) must
    be normalized so that bbox[0] is (min_x, min_y) and bbox[1] is (max_x, max_y),
    per the `Part.bbox: tuple[Point, Point]  # (min, max)` invariant in the model."""
    f = tmp_path / "inverted.brd"
    f.write_text(
        "0\n"
        "BRDOUT: 0 100 100\n"
        "\n"
        "NETS: 0\n"
        "\n"
        # x1=50 x2=30 (x reversed); y1=200 y2=80 (y reversed — typical BRD2 output)
        "PARTS: 1\n"
        "R1 50 200 30 80 0 1\n"
        "\n"
        "PINS: 0\n"
        "\n"
        "NAILS: 0\n"
    )
    board = BRD2Parser().parse_file(f)
    (a, b) = board.parts[0].bbox
    assert a.x <= b.x and a.y <= b.y, f"bbox not normalized: {a} > {b}"
    assert (a.x, a.y) == (30, 80)
    assert (b.x, b.y) == (50, 200)


def test_mnt_reform_all_part_bboxes_are_normalized():
    """Regression guard : every part in the committed MNT Reform fixture must have a
    normalized bbox. The whitequark converter emits y1 > y2 for all 493 parts, so this
    test catches any regression where the normalization is removed or bypassed."""
    path = REPO_ROOT / "board_assets" / "mnt-reform-motherboard.brd"
    board = BRD2Parser().parse_file(path)
    for part in board.parts:
        a, b = part.bbox
        assert a.x <= b.x, f"{part.refdes}: x not normalized ({a.x} > {b.x})"
        assert a.y <= b.y, f"{part.refdes}: y not normalized ({a.y} > {b.y})"


def test_rejects_plain_test_link_by_mistake(tmp_path: Path):
    """A Test_Link file handed to BRD2Parser must refuse, not silently produce garbage."""
    f = tmp_path / "wrong_format.brd"
    f.write_text("str_length: 0\nvar_data: 0 0 0 0\n")
    with pytest.raises(InvalidBoardFile):
        BRD2Parser().parse_file(f)


def test_malformed_brdout_header(tmp_path: Path):
    f = tmp_path / "bad.brd"
    f.write_text("0\nBRDOUT: not-a-number 0 0\n")
    with pytest.raises(MalformedHeaderError):
        BRD2Parser().parse_file(f)


def test_pin_without_valid_net_id(tmp_path: Path):
    """net_id referencing a NET that doesn't exist (past end of NETS block) must fail."""
    f = tmp_path / "bad_net.brd"
    f.write_text(
        "0\n"
        "BRDOUT: 4 100 100\n"
        "0 0\n100 0\n100 100\n0 100\n"
        "\n"
        "NETS: 1\n"
        "1 +3V3\n"
        "\n"
        "PARTS: 1\n"
        "R1 0 0 10 10 0 1\n"
        "\n"
        "PINS: 1\n"
        "5 5 99 1\n"  # net_id=99 references nothing
        "\n"
        "NAILS: 0\n"
    )
    with pytest.raises(MalformedHeaderError):
        BRD2Parser().parse_file(f)
