# SPDX-License-Identifier: Apache-2.0
"""Verify the Scout user-prompt assembly when technician documents are
supplied. The legacy path (no documents) MUST be byte-for-byte identical
to today; the enriched path MUST surface the MPN map, rails, boot phases,
boardview parts, and local datasheet filenames in stable order."""

from __future__ import annotations

from pathlib import Path

from api.board.model import Board, Layer, Part, Pin, Point
from api.pipeline.prompts import SCOUT_RETRY_SUFFIX, SCOUT_USER_TEMPLATE
from api.pipeline.schematic.schemas import (
    ComponentNode,
    ComponentValue,
    ElectricalGraph,
    PowerRail,
    SchematicQualityReport,
)
from api.pipeline.scout import _build_user_prompt


def _legacy_prompt(device_label: str, attempt: int = 0) -> str:
    base = SCOUT_USER_TEMPLATE.format(device_label=device_label)
    return base + SCOUT_RETRY_SUFFIX if attempt > 0 else base


def test_legacy_path_is_byte_for_byte_identical() -> None:
    """No graph / board / datasheets → output equals SCOUT_USER_TEMPLATE."""
    actual = _build_user_prompt(
        device_label="MNT Reform motherboard",
        attempt=0,
        graph=None,
        board=None,
        datasheet_paths=None,
    )
    assert actual == _legacy_prompt("MNT Reform motherboard", attempt=0)


def test_legacy_retry_path_byte_for_byte() -> None:
    actual = _build_user_prompt(
        device_label="iPhone X",
        attempt=1,
        graph=None,
        board=None,
        datasheet_paths=[],  # empty list also counts as "no datasheets"
    )
    assert actual == _legacy_prompt("iPhone X", attempt=1)


def _tiny_graph() -> ElectricalGraph:
    return ElectricalGraph(
        device_slug="demo",
        components={
            "U7": ComponentNode(
                refdes="U7",
                type="ic",
                kind="ic",
                role="buck_regulator",
                value=ComponentValue(raw="LM2677SX-5", mpn="LM2677SX-5"),
            ),
            "C16": ComponentNode(
                refdes="C16",
                type="capacitor",
                kind="passive_c",
                role="decoupling",
                value=ComponentValue(raw="100nF", mpn=None),
            ),
        },
        power_rails={
            "+5V": PowerRail(
                label="+5V",
                voltage_nominal=5.0,
                source_refdes="U7",
                consumers=["U14"],
                decoupling=["C16"],
            )
        },
        boot_sequence=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )


def test_enriched_prompt_includes_graph_section_with_mpn_and_rails() -> None:
    out = _build_user_prompt(
        device_label="demo",
        attempt=0,
        graph=_tiny_graph(),
        board=None,
        datasheet_paths=None,
    )
    # Legacy core still present.
    assert SCOUT_USER_TEMPLATE.format(device_label="demo") in out
    # Targeting block headers.
    assert "# Provided ElectricalGraph" in out
    assert "## MPN map" in out
    assert "## Power rails" in out
    # Specific entries.
    assert "U7: mpn=LM2677SX-5 kind=ic role=buck_regulator" in out
    assert "C16: mpn=— kind=passive_c role=decoupling" in out
    assert "+5V: voltage=5.00V source=U7 consumers=U14" in out


def test_enriched_prompt_includes_boardview_parts() -> None:
    board = Board(
        board_id="demo",
        file_hash="0",
        source_format="brd",
        outline=[Point(x=0, y=0)],
        parts=[
            Part(
                refdes="U7",
                layer=Layer.TOP,
                is_smd=True,
                bbox=(Point(x=0, y=0), Point(x=10, y=10)),
                pin_refs=[],
                value="LM2677",
                footprint="SOIC-8",
            ),
        ],
        pins=[],
        nets=[],
        nails=[],
    )
    out = _build_user_prompt(
        device_label="demo",
        attempt=0,
        graph=None,
        board=board,
        datasheet_paths=None,
    )
    assert "# Provided boardview" in out
    assert "U7: value=LM2677 footprint=SOIC-8" in out


def test_enriched_prompt_lists_datasheets_as_local_urls() -> None:
    out = _build_user_prompt(
        device_label="demo",
        attempt=0,
        graph=None,
        board=None,
        datasheet_paths=[Path("/abs/path/lm2677.pdf"), Path("rel/atsaml21.pdf")],
    )
    assert "# Provided local datasheets" in out
    assert "- local://datasheets/lm2677.pdf" in out
    assert "- local://datasheets/atsaml21.pdf" in out


def test_enriched_blocks_appear_before_retry_suffix() -> None:
    """Retry suffix must remain at the very end so the model reads it last."""
    out = _build_user_prompt(
        device_label="demo",
        attempt=1,
        graph=_tiny_graph(),
        board=None,
        datasheet_paths=None,
    )
    assert out.endswith(SCOUT_RETRY_SUFFIX)
    assert "# Provided ElectricalGraph" in out
    assert out.index("# Provided ElectricalGraph") < out.index(SCOUT_RETRY_SUFFIX)
