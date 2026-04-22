from pathlib import Path

import pytest

from api.board.parser.base import parser_for
from api.board.parser.kicad import KicadPcbParser

# Primary: in-repo committed fixture (stable, reproducible).
# Fallback: tmp path kept for local dev workflows where the repo copy may
# not yet be present (e.g. fresh checkout without LFS or mid-session).
_INREPO = Path(__file__).parent.parent.parent / "board_assets" / "mnt-reform-motherboard.kicad_pcb"
_TMP = Path(
    "/tmp/mnt-reform-work/mnt-reform/reform2-motherboard25-pcb/reform2-motherboard25.kicad_pcb"
)
KICAD_FIXTURE = _INREPO if _INREPO.exists() else _TMP


def _skip_if_fixture_missing():
    if not KICAD_FIXTURE.exists():
        pytest.skip("MNT Reform .kicad_pcb fixture not available (in-repo or /tmp)")


def test_parser_registered_for_kicad_pcb_extension(tmp_path):
    path = tmp_path / "empty.kicad_pcb"
    path.write_text("(kicad_pcb (version 20221018))")
    # parser_for only dispatches by extension/content-sniff; should return KicadPcbParser
    p = parser_for(path)
    assert isinstance(p, KicadPcbParser)


def test_parses_mnt_reform_motherboard_kicad_pcb():
    _skip_if_fixture_missing()
    board = KicadPcbParser().parse_file(KICAD_FIXTURE)
    assert board.source_format == "kicad_pcb"
    assert len(board.parts) > 400  # motherboard25 has 505
    # Bbox is silkscreen + pads + courtyard (no text) — matches what a tech
    # sees on the real PCB including EMI cages and module outlines. For U1
    # SoM connector the full module silkscreen covers ~28x72 mm (vs the
    # pads-only 19x70 mm that excludes the physical module body).
    u1 = board.part_by_refdes("U1")
    assert u1 is not None, "U1 should exist on MNT Reform"
    w = u1.bbox[1].x - u1.bbox[0].x
    h = u1.bbox[1].y - u1.bbox[0].y
    # 28 mm = 1102 mils, 72 mm = 2835 mils — allow generous range
    assert 1000 < w < 1200, f"U1 width {w} outside expected 1000-1200 mils"
    assert 2700 < h < 2950, f"U1 height {h} outside expected 2700-2950 mils"


def test_kicad_parser_fills_rich_fields():
    _skip_if_fixture_missing()
    board = KicadPcbParser().parse_file(KICAD_FIXTURE)
    # At least some parts should have value set (the MNT Reform KiCad source
    # has value strings on capacitors/resistors)
    parts_with_value = [p for p in board.parts if p.value]
    assert len(parts_with_value) > 100, "expected many parts to have a KiCad value string"
    # footprint field
    parts_with_footprint = [p for p in board.parts if p.footprint]
    assert len(parts_with_footprint) == len(board.parts), "every part should have a footprint ref"
    # At least some rotations non-zero (board has rotated components)
    rotations = {p.rotation_deg for p in board.parts}
    assert len(rotations) > 1, "expected multiple rotation angles"


def test_kicad_parser_pin_pads_have_size_and_shape():
    _skip_if_fixture_missing()
    board = KicadPcbParser().parse_file(KICAD_FIXTURE)
    pins_with_size = [p for p in board.pins if p.pad_size]
    assert len(pins_with_size) == len(board.pins), "every pin should have pad_size from KiCad"
    shapes = {p.pad_shape for p in board.pins}
    # At least rect should appear; probably also circle (for thru-hole / via-style pads)
    assert "rect" in shapes or "roundrect" in shapes, f"expected rect pads; got shapes={shapes}"


def test_kicad_parser_rejects_invalid_path(tmp_path):
    from api.board.parser.kicad import KicadSubprocessError

    bad = tmp_path / "not_a_kicad_pcb.kicad_pcb"
    bad.write_text("this is not a kicad file")
    with pytest.raises(KicadSubprocessError):
        KicadPcbParser().parse_file(bad)
