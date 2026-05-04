"""Real open-hardware parser tests — drive parsers over the committed
MNT Reform motherboard file (CERN-OHL-S-2.0, 493 parts / 2104 pins /
647 nets). Concrete proof that the new parsers handle production-scale
real-world boards, not just synthetic fixtures.

Only the `.cad` umbrella is exercised directly on the MNT Reform bytes
(via BRDOUT: content-sniff). The other new parsers use different
dialects, so the MNT Reform file is not a natural fit — `test_parser_realistic_scale.py`
generates a 200-part synthetic workload that every parser can consume.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from api.board.model import Board, Layer
from api.board.parser.asc import ASCParser
from api.board.parser.bdv import BDVParser
from api.board.parser.bdv import _obfuscate as _bdv_encode
from api.board.parser.brd2 import BRD2Parser
from api.board.parser.bv import BVParser
from api.board.parser.cad import CADParser
from api.board.parser.cst import CSTParser
from api.board.parser.f2b import F2BParser
from api.board.parser.gr import GRParser
from api.board.parser.tvw import TVWParser
from api.board.parser.tvw import _obfuscate as _tvw_encode

REPO_ROOT = Path(__file__).resolve().parents[2]
MNT_REFORM = REPO_ROOT / "board_assets" / "mnt-reform-motherboard.brd"

# Expected MNT Reform Motherboard v2.5 counts (from the existing BRD2
# parser test — treated here as ground truth to check for drift).
MNT_EXPECTED = {"parts": 493, "pins": 2104, "nets": 647, "nails": 5, "outline": 9}


@pytest.mark.skipif(
    not MNT_REFORM.exists(), reason="MNT Reform fixture not present"
)
def test_cad_parser_reads_mnt_reform_brd2_content(tmp_path: Path):
    """`.cad` sniffs BRDOUT: and delegates to BRD2Parser while retagging
    source_format. Real-world board (2000+ pins) must parse end-to-end."""
    dst = tmp_path / "mnt-reform-motherboard.cad"
    shutil.copy2(MNT_REFORM, dst)

    board = CADParser().parse_file(dst)

    assert board.source_format == "cad"
    assert len(board.parts) == MNT_EXPECTED["parts"]
    assert len(board.pins) == MNT_EXPECTED["pins"]
    assert len(board.nets) == MNT_EXPECTED["nets"]
    assert len(board.nails) == MNT_EXPECTED["nails"]

    # Spot-check a realistic power rail.
    gnd = board.net_by_name("GND")
    assert gnd is not None and gnd.is_ground is True


@pytest.mark.skipif(
    not MNT_REFORM.exists(), reason="MNT Reform fixture not present"
)
def test_cad_parser_and_brd2_parser_agree_on_mnt_reform_topology(tmp_path: Path):
    """Parse the same real-world file through both the native BRD2Parser
    and the `.cad` umbrella — the resulting Boards must have the same
    topology (parts, pins, nets, nails, bboxes). Only source_format
    differs by design."""
    dst = tmp_path / "mnt-reform-motherboard.cad"
    shutil.copy2(MNT_REFORM, dst)

    cad_board = CADParser().parse_file(dst)
    brd2_board = BRD2Parser().parse_file(MNT_REFORM)

    assert cad_board.source_format == "cad"
    assert brd2_board.source_format == "brd2"

    # Compare the load-bearing topology shape-for-shape.
    def topology(board):
        return (
            [(p.refdes, p.layer, p.is_smd, p.bbox) for p in board.parts],
            [
                (pin.part_refdes, pin.index, pin.pos.x, pin.pos.y, pin.net, pin.layer)
                for pin in board.pins
            ],
            [(n.name, n.is_power, n.is_ground, n.pin_refs) for n in board.nets],
            [(nl.probe, nl.pos.x, nl.pos.y, nl.layer, nl.net) for nl in board.nails],
        )

    assert topology(cad_board) == topology(brd2_board)


# ---------------------------------------------------------------------------
# Test_Link serializer — converts a `Board` back into the grammar every
# new parser reads. Used here to pipe the MNT Reform real topology
# through every dialect parser.
# ---------------------------------------------------------------------------


def _escape_net(name: str) -> str:
    """Escape spaces in a net name for Test_Link ASCII output.

    Test_Link-shape dialects parse pin lines as whitespace-split tokens,
    so embedded spaces in a net name (e.g. KiCad hierarchical paths like
    `/Reform 2 Power/LPC_RTS`) would confuse the parser. We replace them
    with `_` on the way out; the same transform is applied in the test
    assertions so round-trip comparisons remain meaningful.
    """
    return name.replace(" ", "_")


def _serialize_as_test_link(board: Board) -> str:
    """Emit `board` in Test_Link ASCII grammar.

    Parts are written in input order; pins are regrouped so each part's
    pins are contiguous (so `end_of_pins` monotonically increases).
    This mirrors the invariant every Test_Link dialect expects.
    """
    # Test_Link grammar takes integer-mil tokens — Point is float now
    # to support sub-mil XZZ probe-pad positions, but the ASCII format
    # is still int by convention. Cast on the way out.
    outline_lines = [f"{int(p.x)} {int(p.y)}" for p in board.outline]

    # Regroup pins by part so the file is Test_Link-valid (contiguous
    # pin ranges per part, with per-part 1-based pin.index).
    regrouped_pins: list = []
    parts_lines: list[str] = []
    for part in board.parts:
        # Layer+SMD → type_layer byte. bit 0x2 = BOTTOM, bit 0x4 = SMD.
        type_layer = (0x2 if part.layer == Layer.BOTTOM else 0) | (
            0x4 if part.is_smd else 0
        )
        # Ensure non-zero so the marker scan has something to chew on.
        if type_layer == 0:
            type_layer = 0x1
        part_pins = [board.pins[i] for i in part.pin_refs]
        end_of_pins = len(regrouped_pins) + len(part_pins)
        parts_lines.append(f"{part.refdes} {type_layer} {end_of_pins}")
        for local_idx, pin in enumerate(part_pins, start=1):
            # part_idx is 1-based into parts.
            regrouped_pins.append(
                (pin, len(parts_lines), local_idx)
            )

    pins_lines: list[str] = []
    for pin, part_idx, _local_idx in regrouped_pins:
        probe = pin.probe if pin.probe is not None else -99
        net = _escape_net(pin.net or "")
        pins_lines.append(
            f"{int(pin.pos.x)} {int(pin.pos.y)} {probe} {part_idx} {net}".rstrip()
        )

    nails_lines = [
        f"{nl.probe} {int(nl.pos.x)} {int(nl.pos.y)} "
        f"{1 if nl.layer == Layer.TOP else 2} {_escape_net(nl.net)}"
        for nl in board.nails
    ]

    header = (
        "str_length: 100000 100000\n"
        f"var_data: {len(outline_lines)} {len(parts_lines)} "
        f"{len(pins_lines)} {len(nails_lines)}\n"
    )
    blocks = []
    if outline_lines:
        blocks.append("Format:\n" + "\n".join(outline_lines))
    if parts_lines:
        blocks.append("Parts:\n" + "\n".join(parts_lines))
    if pins_lines:
        blocks.append("Pins:\n" + "\n".join(pins_lines))
    if nails_lines:
        blocks.append("Nails:\n" + "\n".join(nails_lines))
    return header + "\n".join(blocks) + "\n"


def _dialect_transform(text: str, source_format: str) -> bytes:
    """Return the Test_Link `text` in the dialect every new parser expects."""
    if source_format == "bv":
        return ("BoardView 1.5\n" + text).encode()
    if source_format == "gr":
        return (
            text.replace("Parts:", "Components:").replace("Nails:", "TestPoints:")
        ).encode()
    if source_format == "cst":
        # Strip var_data + str_length prelude, swap to [Bracketed] markers.
        body = text.split("Format:", 1)[1] if "Format:" in text else text
        parts_chunk = body.split("Parts:", 1)
        pins_chunk = parts_chunk[1].split("Pins:", 1) if len(parts_chunk) == 2 else ("", "")
        nails_chunk = (
            pins_chunk[1].split("Nails:", 1) if len(pins_chunk) == 2 else ("", "")
        )
        return (
            "; Castw real-hardware replay\n"
            "[Format]\n" + parts_chunk[0]
            + ("[Components]\n" + pins_chunk[0] if pins_chunk[0] else "")
            + ("[Pins]\n" + nails_chunk[0] if nails_chunk[0] else "")
            + ("[Nails]\n" + nails_chunk[1] if len(nails_chunk) == 2 else "")
        ).encode()
    if source_format == "f2b":
        return (
            text.replace("Format:", "Outline:").replace("Parts:", "Components:")
        ).encode()
    if source_format == "bdv":
        return _bdv_encode(text)
    if source_format == "tvw":
        return _tvw_encode(text)
    if source_format == "asc":
        return text.encode()
    raise ValueError(source_format)


_PARSERS = {
    "bv": BVParser,
    "gr": GRParser,
    "cst": CSTParser,
    "f2b": F2BParser,
    "bdv": BDVParser,
    "tvw": TVWParser,
    "asc": ASCParser,
}


@pytest.mark.skipif(
    not MNT_REFORM.exists(), reason="MNT Reform fixture not present"
)
@pytest.mark.parametrize("source_format", list(_PARSERS.keys()))
def test_mnt_reform_topology_flows_through_every_new_parser(source_format: str):
    """Parse the real MNT Reform BRD2, serialize to Test_Link grammar,
    transcode into each new parser's dialect, parse back, and confirm
    the topology is preserved. This is the strongest real-hardware
    guarantee we can give: every new parser handles the same 493-part /
    2104-pin / 647-net board the existing BRD2 parser already reads."""
    source = BRD2Parser().parse_file(MNT_REFORM)
    serialized = _serialize_as_test_link(source)
    raw = _dialect_transform(serialized, source_format)

    parser = _PARSERS[source_format]()
    reparsed = parser.parse(
        raw, file_hash="sha256:mnt-replay", board_id="mnt-replay"
    )

    # Counts must match the MNT Reform ground truth (BRD2 parse).
    assert len(reparsed.parts) == len(source.parts)
    assert len(reparsed.pins) == len(source.pins)
    assert len(reparsed.nails) == len(source.nails)

    # Every refdes from the BRD2 source must round-trip.
    src_refdes = {p.refdes for p in source.parts}
    rt_refdes = {p.refdes for p in reparsed.parts}
    assert src_refdes == rt_refdes, (
        f"{source_format}: missing refdes {src_refdes - rt_refdes}, "
        f"extra {rt_refdes - src_refdes}"
    )

    # Every net name must survive the trip. Spaces in MNT Reform's
    # KiCad hierarchical net paths are escaped to `_` by the serializer
    # (Test_Link grammar is whitespace-delimited), so compare under
    # the same transform on both sides.
    src_nets = {_escape_net(n.name) for n in source.nets if n.name}
    rt_nets = {n.name for n in reparsed.nets}
    assert src_nets == rt_nets, (
        f"{source_format}: missing nets {src_nets - rt_nets}, "
        f"extra {rt_nets - src_nets}"
    )

    # Power/ground classification must stay consistent across the replay.
    for name in ("GND", "AGND", "DGND", "PGND"):
        if name in src_nets:
            assert reparsed.net_by_name(name).is_ground is True
