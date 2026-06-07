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


def test_parses_bilayer_fixture_with_top_and_bottom_parts():
    """Synthetic bilayer BRD2 fixture must split parts and pins correctly across
    TOP (side=1) and BOTTOM (side=2). Regression guard added after the MNT Reform
    fixture turned out to be 100 % single-sided (all side=1) and we needed an
    independent bilayer input to confirm the parser splits sides correctly."""
    path = FIXTURE_DIR / "bilayer_minimal.brd"
    board = BRD2Parser().parse_file(path)

    assert len(board.parts) == 4
    top_parts = [p for p in board.parts if p.layer == Layer.TOP]
    bot_parts = [p for p in board.parts if p.layer == Layer.BOTTOM]
    assert len(top_parts) == 2
    assert len(bot_parts) == 2
    assert {p.refdes for p in top_parts} == {"R1_TOP", "C1_TOP"}
    assert {p.refdes for p in bot_parts} == {"R2_BOT", "C2_BOT"}

    assert len(board.pins) == 8
    top_pins = [p for p in board.pins if p.layer == Layer.TOP]
    bot_pins = [p for p in board.pins if p.layer == Layer.BOTTOM]
    assert len(top_pins) == 4
    assert len(bot_pins) == 4


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


def test_net_line_with_empty_name_is_accepted(tmp_path: Path):
    """A NET line carrying only an id and no name is an unnamed net, not a parse error.

    Real-world exporters (e.g. several vendors' `.brd` exports like
    LA-E921P, BK3, X81K) declare trailing unnamed nets as `<id> ` — the id
    followed by whitespace and nothing else. These are unconnected/auto-named
    nets; the slot must be kept (net ids in PINS/NAILS are 1-based positional)
    with an empty net name rather than raising MalformedHeaderError(NETS).
    """
    f = tmp_path / "empty_net.brd"
    f.write_text(
        "0\n"
        "BRDOUT: 0 100 100\n"
        "\n"
        "NETS: 3\n"
        "1 GND\n"
        "2 \n"  # unnamed net — id only, trailing space
        "3 +3V3\n"
        "\n"
        "PARTS: 1\n"
        "R1 0 0 10 10 0 1\n"
        "\n"
        "PINS: 2\n"
        "5 5 1 1\n"  # references net 1 -> GND
        "6 6 2 1\n"  # references net 2 -> unnamed
        "\n"
        "NAILS: 0\n"
    )
    board = BRD2Parser().parse_file(f)
    # The unnamed net keeps its positional slot, so net 3 still resolves to +3V3.
    assert board.net_by_name("GND") is not None
    assert board.net_by_name("+3V3") is not None
    # Pin on net 2 resolves to the empty-string net name (slot preserved).
    p2 = next(p for p in board.pins if p.part_refdes == "R1" and p.index == 2)
    assert p2.net == ""


def test_missing_nails_block_is_zero_nails(tmp_path: Path):
    """A BRD2 file that omits the NAILS block entirely must parse with zero nails.

    Real-world exporters (e.g. some motherboard `.brd` exports like the
    some boards) emit no NAILS block at all when the board declares no
    test points — they simply stop after the PINS block. The absent marker is
    semantically identical to `NAILS: 0` and must not raise MalformedHeaderError.
    """
    f = tmp_path / "no_nails.brd"
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
        "5 5 1 1\n"
        # NO NAILS block at all — stops here.
    )
    board = BRD2Parser().parse_file(f)
    assert board.nails == []
    assert len(board.parts) == 1
    assert len(board.pins) == 1


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
