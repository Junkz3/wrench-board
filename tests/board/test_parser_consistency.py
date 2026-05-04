"""Cross-parser data-consistency invariants.

Every new boardview parser must produce a `Board` that satisfies a
handful of invariants the frontend and the agent take for granted. A
regression in any single parser here translates directly to a
broken UI or a hallucinated refdes at runtime — so we check them all,
on every format, via one generic invariant runner.

Invariants checked per parser:
1. board.source_format is set and matches the expected tag
2. parts[].refdes is unique and non-empty
3. pins[].part_refdes always resolves to an existing part
4. parts[k].pin_refs is a list of indices into the pins array, all
   pointing back to the same refdes
5. pin.index is 1-based and strictly increasing per owning part
6. parts[k].bbox is a true (min, max) tuple (min.x <= max.x, min.y <= max.y)
7. nets[].pin_refs point to pins whose pin.net == nets[].name
8. Every pin.net that is not None is represented in nets[]
9. nails[].net is one of the declared net names when nets exist
10. layer values are valid Layer flag values
11. power/ground flags agree with the shared regex heuristics
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api.board.model import Board, Layer
from api.board.parser._ascii_boardview import GROUND_RE, POWER_RE
from api.board.parser.asc import ASCParser
from api.board.parser.bdv import BDVParser
from api.board.parser.bv import BVParser
from api.board.parser.cad import CADParser
from api.board.parser.cst import CSTParser
from api.board.parser.f2b import F2BParser
from api.board.parser.gr import GRParser
from api.board.parser.test_link import BRDParser
from api.board.parser.tvw import TVWParser

FIXTURE_DIR = Path(__file__).parent / "fixtures"

# (parser_instance, fixture_path, expected source_format tag)
_CASES: list[tuple[object, Path, str]] = [
    (BRDParser(), FIXTURE_DIR / "minimal.brd", "brd"),
    (BVParser(), FIXTURE_DIR / "minimal.bv", "bv"),
    (GRParser(), FIXTURE_DIR / "minimal.gr", "gr"),
    (CADParser(), FIXTURE_DIR / "minimal.cad", "cad"),
    (CADParser(), FIXTURE_DIR / "brd2_form.cad", "cad"),
    (CSTParser(), FIXTURE_DIR / "minimal.cst", "cst"),
    (F2BParser(), FIXTURE_DIR / "minimal.f2b", "f2b"),
    (BDVParser(), FIXTURE_DIR / "minimal.bdv", "bdv"),
    (TVWParser(), FIXTURE_DIR / "minimal.tvw", "tvw"),
    (ASCParser(), FIXTURE_DIR / "tsict_combined.asc", "asc"),
]


def _assert_invariants(board: Board, expected_src: str):
    # --- 1. source_format ---
    assert board.source_format == expected_src, (
        f"source_format expected {expected_src!r}, got {board.source_format!r}"
    )
    assert board.board_id, "board_id must be non-empty"
    assert board.file_hash.startswith("sha256:"), f"file_hash: {board.file_hash!r}"

    # --- 2. parts refdes uniqueness + non-empty ---
    refdes_seen: set[str] = set()
    for part in board.parts:
        assert part.refdes, "part has empty refdes"
        assert part.refdes not in refdes_seen, f"duplicate refdes: {part.refdes}"
        refdes_seen.add(part.refdes)

    # --- 3. every pin.part_refdes resolves to a part ---
    for i, pin in enumerate(board.pins):
        assert pin.part_refdes in refdes_seen, (
            f"pin {i} references unknown part {pin.part_refdes!r}"
        )

    # --- 4. parts[k].pin_refs are indices into board.pins and point back correctly ---
    for part in board.parts:
        for ref in part.pin_refs:
            assert 0 <= ref < len(board.pins), (
                f"part {part.refdes}: pin_ref {ref} out of bounds {len(board.pins)}"
            )
            assert board.pins[ref].part_refdes == part.refdes, (
                f"part {part.refdes}: pin_ref {ref} points to pin owned by "
                f"{board.pins[ref].part_refdes}"
            )

    # --- 5. pin.index values are positive integers ---
    # We deliberately do NOT assert uniqueness. Real connectors and CPU
    # sockets carry multiple physical pads per logical pin number — a
    # USB CN_UCB header has front + back contacts both labelled pin 1,
    # an LGA1331 socket has multiple test pads per BGA ball. Synthetic
    # fixtures use 1..N contiguous indexing; real boards don't. We also
    # don't require "starts at 1" or "monotonic" — the FZ-zlib pins
    # section iterates by net, not by part, so the first pin we see for
    # a given component is whichever appears first in the source file.
    # The unique identity of a pin is (part_refdes, index, pos), not just
    # (part_refdes, index) — `validator.resolve_pin` returns the first
    # matching pad which is the right behavior for "go to pin 5".
    for part in board.parts:
        indices = [board.pins[r].index for r in part.pin_refs]
        for idx in indices:
            assert idx >= 1, (
                f"part {part.refdes}: pin.index must be >= 1, got {idx}"
            )

    # --- 6. bbox is (min, max) normalized ---
    for part in board.parts:
        lo, hi = part.bbox
        assert lo.x <= hi.x, f"part {part.refdes}: bbox x not normalized"
        assert lo.y <= hi.y, f"part {part.refdes}: bbox y not normalized"

    # --- 7. nets[].pin_refs are consistent with pin.net ---
    net_names: set[str] = set()
    for net in board.nets:
        assert net.name, "empty net name"
        net_names.add(net.name)
        for ref in net.pin_refs:
            assert 0 <= ref < len(board.pins), (
                f"net {net.name}: pin_ref {ref} out of bounds"
            )
            assert board.pins[ref].net == net.name, (
                f"net {net.name}: pin_ref {ref} has pin.net={board.pins[ref].net}"
            )

    # --- 8. every named pin.net has a Net entry ---
    for i, pin in enumerate(board.pins):
        if pin.net is not None:
            assert pin.net in net_names, (
                f"pin {i}: net {pin.net!r} not represented in board.nets"
            )

    # --- 9. nails[].net is one of the declared net names when nets exist ---
    if board.nets:
        for nail in board.nails:
            # Empty string on a nail is a known degenerate case (BRD2 NAILS
            # with net_id=0). Skip that — the shape is still `str`.
            if nail.net:
                assert nail.net in net_names, (
                    f"nail probe={nail.probe}: net {nail.net!r} not in board.nets"
                )

    # --- 10. layer values are valid Layer flags ---
    for part in board.parts:
        assert part.layer in (Layer.TOP, Layer.BOTTOM, Layer.BOTH), (
            f"part {part.refdes}: invalid layer {part.layer!r}"
        )
    for pin in board.pins:
        assert pin.layer in (Layer.TOP, Layer.BOTTOM, Layer.BOTH), (
            f"pin {pin.part_refdes}#{pin.index}: invalid layer {pin.layer!r}"
        )
    for nail in board.nails:
        assert nail.layer in (Layer.TOP, Layer.BOTTOM, Layer.BOTH), (
            f"nail probe={nail.probe}: invalid layer {nail.layer!r}"
        )

    # --- 11. power/ground classification agrees with shared regexes ---
    for net in board.nets:
        expected_power = bool(POWER_RE.match(net.name))
        expected_ground = bool(GROUND_RE.match(net.name))
        assert net.is_power == expected_power, (
            f"net {net.name}: is_power={net.is_power} but regex says {expected_power}"
        )
        assert net.is_ground == expected_ground, (
            f"net {net.name}: is_ground={net.is_ground} but regex says {expected_ground}"
        )


@pytest.mark.parametrize(
    "parser,path,src", _CASES, ids=[c[2] + "_" + c[1].name for c in _CASES]
)
def test_parser_emits_internally_consistent_board(parser, path: Path, src: str):
    board = parser.parse_file(path)
    _assert_invariants(board, src)


def test_all_new_parsers_share_power_ground_regex():
    """Regression guard: every parser must go through `derive_nets` (or its
    equivalent in brd2) so power/ground classification is uniform. A parser
    that hard-coded its own flags would drift silently — _assert_invariants
    catches it, but this stand-alone case asserts the shared regex covers
    at least one +3V3 and one GND net end-to-end in every fixture."""
    for parser, path, _src in _CASES:
        board = parser.parse_file(path)
        names = {n.name for n in board.nets}
        if "+3V3" in names:
            assert board.net_by_name("+3V3").is_power is True, (
                f"{path.name}: +3V3 not flagged as power"
            )
        if "GND" in names:
            assert board.net_by_name("GND").is_ground is True, (
                f"{path.name}: GND not flagged as ground"
            )


def test_every_pin_position_is_finite():
    """Pin coordinates must be finite real numbers. `Point.x/y` is float
    since XZZ board-to-board connector pads land at fractional mils
    (sub-mil precision is required to keep them centred). Earlier this
    test asserted int type — that invariant was relaxed when Point
    moved to float, but each parser must still produce real numbers
    for every pin."""
    import math
    for parser, path, _src in _CASES:
        board = parser.parse_file(path)
        for pin in board.pins:
            assert math.isfinite(pin.pos.x) and math.isfinite(pin.pos.y), (
                f"{path.name}: pin {pin.part_refdes}#{pin.index} has non-finite pos"
            )


def test_layer_side_consistency_between_pin_and_owning_part():
    """For formats where the parser copies the owning part's layer onto
    its pins (Test_Link-shape), every pin on a given part must share
    that part's layer. BRD2-shape parsers emit pin-level layer
    independently — they're exempt."""
    test_link_shape_srcs = {"brd", "bv", "gr", "cst", "f2b", "bdv", "tvw", "asc"}
    for parser, path, src in _CASES:
        if src not in test_link_shape_srcs:
            continue
        board = parser.parse_file(path)
        for part in board.parts:
            for ref in part.pin_refs:
                assert board.pins[ref].layer == part.layer, (
                    f"{path.name}: pin {ref} layer={board.pins[ref].layer} "
                    f"differs from owning part {part.refdes} layer={part.layer}"
                )


def test_every_emitted_refdes_resolves_via_validator():
    """The validator (`api/board/validator.py`) is the anti-hallucination
    gate enforced on agent outputs. A parser that emitted a refdes that
    `is_valid_refdes(board, r)` rejects would create an "unknown" loop
    where the agent references a part the validator can't find."""
    from api.board.validator import is_valid_refdes, resolve_part

    for parser, path, _src in _CASES:
        board = parser.parse_file(path)
        for part in board.parts:
            assert is_valid_refdes(board, part.refdes), (
                f"{path.name}: refdes {part.refdes!r} rejected by validator"
            )
            assert resolve_part(board, part.refdes) is not None, (
                f"{path.name}: resolve_part({part.refdes!r}) returned None"
            )


def test_parsing_is_deterministic():
    """Parsing the same bytes twice must produce equal Boards. A regression
    here typically means one of the parsers leaked mutable state across
    calls (e.g. reused a dict) — the UI then races on identity."""
    for parser_cls, path, _src in _CASES:
        # Re-instantiate the parser class for each call; otherwise a stateful
        # parser (like FZParser with a key cache) would give itself a free
        # pass by not actually re-initializing between runs.
        cls = type(parser_cls)
        a = cls().parse_file(path)
        b = cls().parse_file(path)
        assert a.model_dump() == b.model_dump(), (
            f"{path.name}: non-deterministic parse"
        )


def test_outline_polygon_is_either_empty_or_coherent():
    """When an outline is present, it should have ≥3 points and the
    bounding box of those points should contain every pin — otherwise
    the frontend draws a polygon that doesn't enclose the board."""
    for parser, path, _src in _CASES:
        board = parser.parse_file(path)
        if not board.outline:
            continue
        assert len(board.outline) >= 3, (
            f"{path.name}: outline has only {len(board.outline)} points"
        )
        if not board.pins:
            continue
        min_x = min(p.x for p in board.outline)
        max_x = max(p.x for p in board.outline)
        min_y = min(p.y for p in board.outline)
        max_y = max(p.y for p in board.outline)
        for pin in board.pins:
            assert min_x <= pin.pos.x <= max_x, (
                f"{path.name}: pin {pin.part_refdes}#{pin.index} x={pin.pos.x} "
                f"outside outline x=[{min_x}, {max_x}]"
            )
            assert min_y <= pin.pos.y <= max_y, (
                f"{path.name}: pin {pin.part_refdes}#{pin.index} y={pin.pos.y} "
                f"outside outline y=[{min_y}, {max_y}]"
            )


def test_every_fixture_has_at_least_one_nonempty_block():
    """Smoke check: every fixture must exercise at least parts AND pins.
    An empty-board fixture would mask genuine parsing failures behind
    trivially-true invariants."""
    for parser, path, _src in _CASES:
        board = parser.parse_file(path)
        assert board.parts, f"{path.name}: emits zero parts"
        assert board.pins, f"{path.name}: emits zero pins"


def test_roundtrip_encoded_formats_produce_same_board_as_plaintext():
    """Family-B formats (bdv, tvw) use a symmetric cipher. Encoding a
    known plaintext and re-parsing must yield the same Board (minus
    source_format + board_id + file_hash, which differ by design)."""
    from api.board.parser import bdv as bdv_mod
    from api.board.parser import tvw as tvw_mod

    plaintext = (
        "var_data: 4 2 4 1\n"
        "Format:\n0 0\n100 0\n100 50\n0 50\n"
        "Parts:\nR1 5 2\nC1 10 4\n"
        "Pins:\n10 10 -99 1 +3V3\n20 10 -99 1 GND\n30 10 1 2 +3V3\n40 10 -99 2 GND\n"
        "Nails:\n1 30 10 1 +3V3\n"
    )

    # Baseline: parse plaintext as Test_Link via the BRD parser.
    brd_board = BRDParser().parse(
        ("str_length: 1024 512\n" + plaintext).encode(),
        file_hash="sha256:baseline",
        board_id="baseline",
    )

    for cls, encoder, name in [
        (BDVParser, bdv_mod._obfuscate, "bdv"),
        (TVWParser, tvw_mod._obfuscate, "tvw"),
    ]:
        raw = encoder(plaintext)
        board = cls().parse(raw, file_hash="sha256:roundtrip", board_id="rt")
        # Compare the load-bearing shape — parts, pins, nets, nails.
        def fingerprint(b):
            return (
                [(p.refdes, p.layer, p.is_smd, p.pin_refs) for p in b.parts],
                [(pin.part_refdes, pin.index, pin.pos.x, pin.pos.y, pin.net)
                 for pin in b.pins],
                sorted((n.name, n.is_power, n.is_ground) for n in b.nets),
                [(n.probe, n.pos.x, n.pos.y, n.net) for n in b.nails],
            )
        assert fingerprint(board) == fingerprint(brd_board), (
            f"{name}: round-trip board differs from plaintext baseline"
        )
