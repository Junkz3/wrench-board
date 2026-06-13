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
    # Pads-only bbox, not inflated — U1 should be ~19x70 mm (not 28x72 mm)
    u1 = board.part_by_refdes("U1")
    assert u1 is not None, "U1 should exist on MNT Reform"
    w = u1.bbox[1].x - u1.bbox[0].x
    h = u1.bbox[1].y - u1.bbox[0].y
    # 19 mm = 748 mils, 70 mm = 2756 mils — allow generous range
    assert 600 < w < 850, f"U1 width {w} outside expected 600-850 mils"
    assert 2500 < h < 2900, f"U1 height {h} outside expected 2500-2900 mils"


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


# --- Pre-extracted sidecar (deploys without pcbnew) ---------------------------
# pcbnew ships with KiCad (not pip-installable), so a slim Docker runtime can't
# run the extractor subprocess. A `<file>.extract.json` sidecar generated where
# KiCad IS available lets such deploys parse the committed fixture. The sidecar
# is only trusted when its embedded source hash matches the .kicad_pcb bytes.

def _fake_extract() -> dict:
    # Mirrors _kicad_extract.py's output shape (what _json_to_board consumes).
    return {
        "outline": [{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 50}, {"x": 0, "y": 50}],
        "nets": [{"code": 1, "name": "GND"}, {"code": 2, "name": "+5V"}],
        "parts": [
            {
                "refdes": "R1", "first_pin": 0, "side": 1, "value": "10k",
                "footprint": "R_0402", "rotation_deg": 0.0, "is_smd": True,
                "bbox": [{"x": 10, "y": 10}, {"x": 20, "y": 14}],
            }
        ],
        "pins": [
            {"x": 11, "y": 12, "side": 1, "net_code": 1, "pad_shape": "rect", "pad_size": [2, 2]},
            {"x": 19, "y": 12, "side": 1, "net_code": 2, "pad_shape": "rect", "pad_size": [2, 2]},
        ],
    }


def test_kicad_parser_uses_matching_sidecar_without_subprocess(tmp_path, monkeypatch):
    import hashlib
    import json as _json
    import subprocess as _subprocess

    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20221018))")
    digest = "sha256:" + hashlib.sha256(pcb.read_bytes()).hexdigest()
    sidecar = tmp_path / "board.kicad_pcb.extract.json"
    sidecar.write_text(_json.dumps({"source_sha256": digest, "extract": _fake_extract()}))

    # Any subprocess attempt = the sidecar was NOT used → fail loudly.
    def _boom(*a, **k):
        raise AssertionError("subprocess must not run when a matching sidecar exists")

    monkeypatch.setattr(_subprocess, "run", _boom)
    board = KicadPcbParser().parse_file(pcb)
    assert [p.refdes for p in board.parts] == ["R1"]
    assert len(board.pins) == 2


def test_kicad_parser_ignores_stale_sidecar(tmp_path, monkeypatch):
    import json as _json
    import subprocess as _subprocess

    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20221018))")
    sidecar = tmp_path / "board.kicad_pcb.extract.json"
    sidecar.write_text(_json.dumps({"source_sha256": "sha256:deadbeef", "extract": _fake_extract()}))

    ran = {}

    def _spy(*a, **k):
        ran["subprocess"] = True
        raise FileNotFoundError("no pcbnew here")

    monkeypatch.setattr(_subprocess, "run", _spy)
    with pytest.raises(FileNotFoundError):
        KicadPcbParser().parse_file(pcb)
    assert ran.get("subprocess"), "a hash-mismatched sidecar must fall through to the extractor"


# --- Broader fixture coverage ------------------------------------------------
# The MNT Reform motherboard is a real, open-hardware board (CERN-OHL-S-2.0,
# cf. board_assets/ATTRIBUTIONS.md). These tests lock in the rest of the
# parser's contract beyond bbox/metadata/pad shape.


def test_kicad_parser_extracts_board_outline():
    _skip_if_fixture_missing()
    board = KicadPcbParser().parse_file(KICAD_FIXTURE)
    assert board.outline, "board outline should be populated"
    assert len(board.outline) >= 4, (
        f"outline polygon needs at least 4 points; got {len(board.outline)}"
    )


def test_kicad_parser_extracts_nets_including_power_and_ground():
    _skip_if_fixture_missing()
    board = KicadPcbParser().parse_file(KICAD_FIXTURE)
    assert len(board.nets) > 100, f"expected many nets on a motherboard; got {len(board.nets)}"
    # At least one ground and one power rail should be flagged — the parser
    # classifies by name, so this also guards against a regex regression.
    grounds = [n for n in board.nets if n.is_ground]
    powers = [n for n in board.nets if n.is_power]
    assert grounds, "expected at least one ground net flagged"
    assert powers, "expected at least one power/rail net flagged"


def test_kicad_parser_part_and_net_indexes_are_queryable():
    _skip_if_fixture_missing()
    board = KicadPcbParser().parse_file(KICAD_FIXTURE)
    # Part lookup via the O(1) index built in model_post_init.
    u1 = board.part_by_refdes("U1")
    assert u1 is not None
    assert u1.refdes == "U1"
    assert board.part_by_refdes("DOES_NOT_EXIST") is None
    # Net lookup by name — pick a net known to exist on this board by reading
    # it from the parse output itself (avoids hardcoding net names that could
    # drift as the KiCad source evolves upstream).
    some_net_name = board.nets[0].name
    assert board.net_by_name(some_net_name) is not None
    assert board.net_by_name("__absent_net__") is None


def test_kicad_parser_assigns_layer_to_every_part():
    """Every part must carry a Layer value — no missing / null layer. The MNT
    Reform motherboard happens to be single-sided (all Layer.TOP), so we only
    assert TOP is present; we still verify there's no garbage layer bit set."""
    _skip_if_fixture_missing()
    board = KicadPcbParser().parse_file(KICAD_FIXTURE)
    from api.board.model import Layer
    layers = {p.layer for p in board.parts}
    assert Layer.TOP in layers, "expected at least one top-side part"
    # Any emitted layer must be one of the enum values — no stray bits.
    for layer in layers:
        assert layer in (Layer.TOP, Layer.BOTTOM, Layer.BOTH), f"unexpected layer {layer!r}"


def test_kicad_parser_pins_reference_existing_parts():
    """Referential integrity: every pin.part_refdes must resolve to a Part.
    Catches a parser regression that would emit orphan pins."""
    _skip_if_fixture_missing()
    board = KicadPcbParser().parse_file(KICAD_FIXTURE)
    known_refdes = {p.refdes for p in board.parts}
    orphans = [p for p in board.pins if p.part_refdes not in known_refdes]
    assert not orphans, (
        f"{len(orphans)} pins reference unknown parts "
        f"(first 3: {[(p.part_refdes, p.index) for p in orphans[:3]]})"
    )


def test_kicad_parser_emits_source_format_and_file_hash():
    _skip_if_fixture_missing()
    board = KicadPcbParser().parse_file(KICAD_FIXTURE)
    assert board.source_format == "kicad_pcb"
    assert board.file_hash.startswith("sha256:")
    assert len(board.file_hash) == len("sha256:") + 64
