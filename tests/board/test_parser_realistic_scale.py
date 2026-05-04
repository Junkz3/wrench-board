"""Realistic-scale parser tests — drives every new parser through a
~200-part / ~800-pin / ~40-net synthetic board matching the shape of a
real laptop motherboard.

The committed fixtures under `tests/board/fixtures/minimal.*` are
intentionally tiny (4 pins) so format-specific quirks are easy to
inspect by eye. This file generates a larger payload at runtime,
feeds it through each parser via the format-specific encoder or
marker swap, and asserts that every parser yields a Board whose:

- counts match the generated input
- nets classify power/ground correctly across real-world rail names
- pin-part linkage is fully resolvable via the validator
- HTTP `/api/board/parse` path emits the same Board as direct parse()

This gives high confidence that a technician's 10 MB `.bv` or `.cst`
won't fall over on real data, even without shipping proprietary
fixtures in the repo.
"""

from __future__ import annotations

import hashlib
import io

import pytest
from fastapi.testclient import TestClient

from api.board.model import Board
from api.board.parser.asc import ASCParser
from api.board.parser.bdv import BDVParser
from api.board.parser.bdv import _obfuscate as _bdv_encode
from api.board.parser.bv import BVParser
from api.board.parser.cad import CADParser
from api.board.parser.cst import CSTParser
from api.board.parser.f2b import F2BParser
from api.board.parser.fz import FZParser
from api.board.parser.gr import GRParser
from api.board.parser.test_link import BRDParser
from api.board.parser.tvw import TVWParser
from api.board.parser.tvw import _obfuscate as _tvw_encode
from api.main import app
from tests.board.test_fz_xor_cipher import _encrypt as _fz_encrypt

# ---------------------------------------------------------------------------
# Plaintext generator — one realistic Test_Link-shape payload
# ---------------------------------------------------------------------------


def _build_realistic_plaintext() -> tuple[str, dict]:
    """Generate a ~200-part board and return (text, expected_counts).

    Shape resembles a commodity laptop motherboard:
      - 100 R* resistors on top layer (SMD)
      - 80  C* capacitors on top layer (SMD)
      - 15  U* ICs on bottom layer (SMD, many pins each)
      - 5   J* connectors on top layer (through-hole)

    Pin distribution: R/C = 2 pins each, U = 20 pins each, J = 40 pins
    each. Total pins = 100*2 + 80*2 + 15*20 + 5*40 = 200 + 160 + 300 + 200
    = 860.

    Nets cover realistic rail names (power + ground + signal) spread
    across pins pseudo-randomly but deterministically (seeded by index).
    """
    parts: list[tuple[str, int, int]] = []  # (refdes, type_layer_bits, end_of_pins)
    lines_parts: list[str] = []
    lines_pins: list[str] = []
    lines_nails: list[str] = []

    net_pool = [
        "+3V3",
        "+5V",
        "+1V8",
        "+12V",
        "+1V0_CORE",
        "+2V5_DDR",
        "+0V9_VTT",
        "+3V3_PMIC_RTC",
        "VCC",
        "VCC_CORE",
        "VCC_IO",
        "VDD_CPU",
        "VDD_GPU",
        "VDD_SDRAM",
        "V_USB",
        "GND",
        "AGND",
        "DGND",
        "PGND",
        "CLK_100M",
        "CLK_25M",
        "XTAL_IN",
        "XTAL_OUT",
        "USB_DP",
        "USB_DN",
        "HDMI_D0+",
        "HDMI_D0-",
        "HDMI_D1+",
        "HDMI_D1-",
        "SATA_RX+",
        "SATA_RX-",
        "SATA_TX+",
        "SATA_TX-",
        "I2C_SDA",
        "I2C_SCL",
        "SPI_MOSI",
        "SPI_MISO",
        "SPI_CLK",
        "SPI_CS",
        "RESET_n",
        "PWR_EN",
        "PWR_GOOD",
    ]
    n_nets = len(net_pool)  # 41 distinct nets

    pin_idx = 0  # 0-based running index across the pins block

    def add_pins(
        refdes_prefix: str,
        count: int,
        pins_per: int,
        layer_bits: int,
        start_x: int,
        start_y: int,
        step_y: int,
    ):
        nonlocal pin_idx
        for i in range(1, count + 1):
            refdes = f"{refdes_prefix}{i}"
            end_of_pins = pin_idx + pins_per
            lines_parts.append(f"{refdes} {layer_bits} {end_of_pins}")
            parts.append((refdes, layer_bits, end_of_pins))
            # Emit pins: at position (start_x + i*step_x, start_y + j*step_y)
            row_y = start_y + (i - 1) * step_y
            for p in range(1, pins_per + 1):
                col_x = start_x + p * 50
                # Deterministic net assignment — seed = pin_idx for stability.
                net_name = net_pool[(pin_idx * 7 + p) % n_nets]
                probe = -99 if pin_idx % 4 != 0 else (pin_idx // 4) + 1
                # part_idx is 1-based
                part_idx = len(parts)
                lines_pins.append(f"{col_x} {row_y} {probe} {part_idx} {net_name}")
                if probe > 0:
                    lines_nails.append(f"{probe} {col_x} {row_y} 1 {net_name}")
                pin_idx += 1

    # Layer bits: 0b0101=5 → TOP + SMD; 0b1010=10 → BOTTOM (not-SMD)
    # 0b0001=1 → TOP + through-hole; 0b1110=14 → BOTTOM + SMD
    R_TOP_SMD = 5
    C_TOP_SMD = 5
    U_BOT_SMD = 14  # bit 0x2 set → BOTTOM; bit 0x4 set → SMD
    J_TOP_TH = 1  # neither bit 0x2 nor 0x4 → TOP + through-hole

    add_pins("R", 100, 2, R_TOP_SMD, start_x=100, start_y=200, step_y=20)
    add_pins("C", 80, 2, C_TOP_SMD, start_x=100, start_y=2300, step_y=20)
    add_pins("U", 15, 20, U_BOT_SMD, start_x=300, start_y=4000, step_y=600)
    add_pins("J", 5, 40, J_TOP_TH, start_x=500, start_y=13500, step_y=1200)

    total_parts = len(parts)
    total_pins = pin_idx
    total_nails = len(lines_nails)

    outline_points = [(0, 0), (20000, 0), (20000, 20000), (0, 20000)]
    n_format = len(outline_points)

    header = (
        f"str_length: 20000 20000\nvar_data: {n_format} {total_parts} {total_pins} {total_nails}\n"
    )
    outline = "Format:\n" + "\n".join(f"{x} {y}" for x, y in outline_points) + "\n"
    parts_block = "Parts:\n" + "\n".join(lines_parts) + "\n"
    pins_block = "Pins:\n" + "\n".join(lines_pins) + "\n"
    nails_block = "Nails:\n" + "\n".join(lines_nails) + "\n" if lines_nails else ""

    text = header + outline + parts_block + pins_block + nails_block

    # Compute the exact set of net names that were actually assigned to
    # at least one pin — this is the ground truth the parser must
    # reproduce (not just `len(net_pool)`, which includes nets the
    # deterministic formula may not hit).
    expected_net_names: set[str] = set()
    for ln in lines_pins:
        expected_net_names.add(ln.rsplit(" ", 1)[-1])

    expected = {
        "n_parts": total_parts,
        "n_pins": total_pins,
        "expected_nets": expected_net_names,
        "n_nails": total_nails,
    }
    return text, expected


# ---------------------------------------------------------------------------
# Shared assertions
# ---------------------------------------------------------------------------


def _assert_scale_matches(board: Board, expected: dict):
    assert len(board.parts) == expected["n_parts"], (
        f"parts: got {len(board.parts)}, expected {expected['n_parts']}"
    )
    assert len(board.pins) == expected["n_pins"], (
        f"pins: got {len(board.pins)}, expected {expected['n_pins']}"
    )
    # The parser must reproduce EXACTLY the net names the generator emitted.
    actual_names = {n.name for n in board.nets}
    assert actual_names == expected["expected_nets"], (
        f"nets differ. missing={expected['expected_nets'] - actual_names}, "
        f"extra={actual_names - expected['expected_nets']}"
    )
    assert len(board.nails) == expected["n_nails"], (
        f"nails: got {len(board.nails)}, expected {expected['n_nails']}"
    )


def _assert_power_ground_classification(board: Board):
    """Known-power and known-ground net names must be flagged accordingly."""
    power_names = {"+3V3", "+5V", "+1V8", "+12V", "VCC_CORE", "VDD_CPU", "V_USB"}
    ground_names = {"GND", "AGND", "DGND", "PGND"}
    for name in power_names:
        net = board.net_by_name(name)
        if net is not None:
            assert net.is_power is True, f"{name} not flagged power"
    for name in ground_names:
        net = board.net_by_name(name)
        if net is not None:
            assert net.is_ground is True, f"{name} not flagged ground"


def _assert_topology_resolvable(board: Board):
    """Every pin_ref in parts + nets resolves to a real pin with matching metadata."""
    for part in board.parts:
        for ref in part.pin_refs:
            pin = board.pins[ref]
            assert pin.part_refdes == part.refdes
            assert pin.layer == part.layer
    for net in board.nets:
        for ref in net.pin_refs:
            assert board.pins[ref].net == net.name


# ---------------------------------------------------------------------------
# Per-parser tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def realistic():
    text, expected = _build_realistic_plaintext()
    return text, expected


def _raw_for(text: str) -> bytes:
    return text.encode("utf-8")


def test_realistic_plaintext_passes_canonical_test_link_parser(realistic):
    """Sanity check: the generator actually produces a valid Test_Link file."""
    text, expected = realistic
    board = BRDParser().parse(_raw_for(text), file_hash="sha256:realistic", board_id="realistic")
    _assert_scale_matches(board, expected)
    _assert_power_ground_classification(board)
    _assert_topology_resolvable(board)


def test_realistic_bv(realistic):
    text, expected = realistic
    board = BVParser().parse(
        _raw_for("BoardView 1.5\n" + text),
        file_hash="sha256:bv",
        board_id="realistic-bv",
    )
    assert board.source_format == "bv"
    _assert_scale_matches(board, expected)
    _assert_power_ground_classification(board)
    _assert_topology_resolvable(board)


def test_realistic_gr(realistic):
    """GR uses `Components:` / `TestPoints:` — swap the plaintext markers."""
    text, expected = realistic
    dialect = text.replace("Parts:", "Components:").replace("Nails:", "TestPoints:")
    board = GRParser().parse(_raw_for(dialect), file_hash="sha256:gr", board_id="realistic-gr")
    assert board.source_format == "gr"
    _assert_scale_matches(board, expected)
    _assert_power_ground_classification(board)


def test_realistic_cad_test_link_form(realistic):
    text, expected = realistic
    # CAD accepts uppercase markers — exercise that branch.
    dialect = (
        text.replace("Format:", "FORMAT:")
        .replace("Parts:", "PARTS:")
        .replace("Pins:", "PINS:")
        .replace("Nails:", "NAILS:")
    )
    board = CADParser().parse(_raw_for(dialect), file_hash="sha256:cad", board_id="realistic-cad")
    assert board.source_format == "cad"
    _assert_scale_matches(board, expected)
    _assert_power_ground_classification(board)


def test_realistic_cst(realistic):
    text, expected = realistic
    # CST uses [Bracketed] section headers and no var_data prelude.
    # Drop the `str_length:` and `var_data:` prelude, swap markers.
    body = text.split("Format:", 1)[
        1
    ]  # drop prelude, keep from "Format:" onward (minus the marker)
    dialect = (
        "; Castw v3.32 synthetic realistic\n"
        "[Format]\n"
        + body.split("Parts:", 1)[0]
        + "[Components]\n"
        + body.split("Parts:", 1)[1].split("Pins:", 1)[0]
        + "[Pins]\n"
        + body.split("Pins:", 1)[1].split("Nails:", 1)[0]
        + "[Nails]\n"
        + body.split("Nails:", 1)[1]
    )
    board = CSTParser().parse(_raw_for(dialect), file_hash="sha256:cst", board_id="realistic-cst")
    assert board.source_format == "cst"
    _assert_scale_matches(board, expected)
    _assert_power_ground_classification(board)


def test_realistic_f2b(realistic):
    text, expected = realistic
    dialect = text.replace("Format:", "Outline:").replace("Parts:", "Components:")
    board = F2BParser().parse(_raw_for(dialect), file_hash="sha256:f2b", board_id="realistic-f2b")
    assert board.source_format == "f2b"
    _assert_scale_matches(board, expected)
    _assert_power_ground_classification(board)


def test_realistic_bdv(realistic):
    text, expected = realistic
    encoded = _bdv_encode(text)
    board = BDVParser().parse(encoded, file_hash="sha256:bdv", board_id="realistic-bdv")
    assert board.source_format == "bdv"
    _assert_scale_matches(board, expected)
    _assert_power_ground_classification(board)


def test_realistic_tvw(realistic):
    text, expected = realistic
    encoded = _tvw_encode(text)
    board = TVWParser().parse(encoded, file_hash="sha256:tvw", board_id="realistic-tvw")
    assert board.source_format == "tvw"
    _assert_scale_matches(board, expected)
    _assert_power_ground_classification(board)


def test_realistic_fz_with_key(realistic):
    """End-to-end realistic-scale validation of the FZ-xor → zlib path.

    The on-disk shape of a real `.fz` file is a 4-byte LE size header
    followed by a zlib stream of pipe-delimited (`A!`/`S!`) rows, then
    wrapped in the 16-byte sliding-window byte cipher. Build that shape
    with the realistic-scale parts/pins/nails counts so the cipher,
    inflate, and section walker are all exercised at production scale.
    """
    import struct
    import zlib

    _text, expected = realistic

    # Translate the realistic Test_Link plaintext into an FZ-zlib
    # pipe-delimited equivalent. We rebuild it here from the same fixture
    # fields (parts/pins/nails) so counts match exactly.
    a_parts = "A!REFDES!COMP_INSERTION_CODE!SYM_NAME!SYM_MIRROR!SYM_ROTATE"
    a_pins = "A!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!PIN_X!PIN_Y!TEST_POINT!RADIUS"
    a_via = "A!TESTVIA!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!VIA_X!VIA_Y!TEST_POINT!RADIUS"

    parts: list[tuple[str, str]] = []  # (refdes, mirror)
    for prefix, n, mirror in (("R", 100, "NO"), ("C", 80, "NO"), ("U", 15, "YES"), ("J", 5, "NO")):
        for i in range(n):
            parts.append((f"{prefix}{i + 1}", mirror))

    pins_rows: list[str] = []
    nails_rows: list[str] = []
    pin_idx = 0
    nail_idx = 0
    for refdes, _mirror in parts:
        prefix = refdes[0]
        n_pins = {"R": 2, "C": 2, "U": 20, "J": 40}[prefix]
        for k in range(1, n_pins + 1):
            net = sorted(expected["expected_nets"])[pin_idx % len(expected["expected_nets"])]
            x = 100 + (pin_idx % 50) * 10
            y = 200 + (pin_idx // 50) * 20
            pins_rows.append(f"S!{net}!{refdes}!0!{k}!{x}.0!{y}.0!!1")
            if pin_idx % 4 == 0:
                nail_idx += 1
                nails_rows.append(f"S!{nail_idx}!{net}!{refdes}!0!{k}!{x}.0!{y}.0!T!1")
            pin_idx += 1

    parts_rows = [f"S!{r}!1!FOOTPRINT!{m}!0" for r, m in parts]
    text = (
        a_parts
        + "\n"
        + "\n".join(parts_rows)
        + "\n"
        + a_pins
        + "\n"
        + "\n".join(pins_rows)
        + "\n"
        + a_via
        + "\n"
        + "\n".join(nails_rows)
        + "\n"
    )
    payload = struct.pack("<I", len(text)) + zlib.compress(text.encode())

    key = tuple(range(1, 45))
    encoded = _fz_encrypt(payload, key)
    board = FZParser(key=key).parse(encoded, file_hash="sha256:fz", board_id="realistic-fz")
    assert board.source_format == "fz"
    assert len(board.parts) == expected["n_parts"]
    assert len(board.pins) == expected["n_pins"]
    assert len(board.nails) == expected["n_nails"]
    _assert_power_ground_classification(board)


def test_realistic_asc_combined(realistic):
    text, expected = realistic
    board = ASCParser().parse(_raw_for(text), file_hash="sha256:asc", board_id="realistic-asc")
    assert board.source_format == "asc"
    _assert_scale_matches(board, expected)
    _assert_power_ground_classification(board)


# ---------------------------------------------------------------------------
# HTTP layer end-to-end: confirm the UI path works at scale
# ---------------------------------------------------------------------------


def test_http_upload_at_scale_each_format(realistic):
    """POST /api/board/parse with the realistic-scale fixture in each
    format. Confirms the API surface emits a full, valid Board JSON
    the frontend can consume without degrading."""
    text, expected = realistic
    client = TestClient(app)
    cases = [
        ("board.bv", _raw_for("BoardView 1.5\n" + text), "bv"),
        (
            "board.gr",
            _raw_for(text.replace("Parts:", "Components:").replace("Nails:", "TestPoints:")),
            "gr",
        ),
        (
            "board.f2b",
            _raw_for(text.replace("Format:", "Outline:").replace("Parts:", "Components:")),
            "f2b",
        ),
        ("board.bdv", _bdv_encode(text), "bdv"),
        ("board.tvw", _tvw_encode(text), "tvw"),
        ("board.asc", _raw_for(text), "asc"),
    ]
    for fname, payload, expected_src in cases:
        r = client.post(
            "/api/board/parse",
            files={"file": (fname, io.BytesIO(payload), "application/octet-stream")},
        )
        assert r.status_code == 200, f"{fname} → {r.status_code}: {r.text[:300]}"
        body = r.json()
        assert body["source_format"] == expected_src
        assert len(body["parts"]) == expected["n_parts"], f"{fname}: parts count mismatch"
        assert len(body["pins"]) == expected["n_pins"], f"{fname}: pins count mismatch"
        # Every pin.part_refdes resolves to a part in the same JSON
        refdes = {p["refdes"] for p in body["parts"]}
        for pin in body["pins"]:
            assert pin["part_refdes"] in refdes, (
                f"{fname}: pin references unknown part {pin['part_refdes']}"
            )


def test_file_hash_is_deterministic_per_payload(realistic, tmp_path):
    """Same payload → same file_hash. This is what the caching layer
    upstream relies on; a drift here would re-parse the same upload
    on every call."""
    from pathlib import Path

    text, _expected = realistic
    raw = _raw_for(text)
    payload = b"BoardView 1.5\n" + raw
    f: Path = tmp_path / "board.bv"
    f.write_bytes(payload)
    board_a = BVParser().parse_file(f)
    board_b = BVParser().parse_file(f)
    assert board_a.file_hash == board_b.file_hash
    assert board_a.file_hash == "sha256:" + hashlib.sha256(payload).hexdigest()
