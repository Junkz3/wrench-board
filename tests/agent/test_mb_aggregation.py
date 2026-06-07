"""Tests for the 4 presence cases of restructured mb_get_component."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.agent.tools import mb_get_component
from api.board.model import Board, Layer, Part, Pin, Point
from api.session.state import SessionState


@pytest.fixture
def seeded_memory(tmp_path: Path) -> Path:
    """Memory root with U7 (pmic) and C29 (cap) in the registry and dictionary."""
    slug_dir = tmp_path / "testdev"
    slug_dir.mkdir()
    (slug_dir / "registry.json").write_text(json.dumps({
        "components": [
            {"canonical_name": "U7", "aliases": ["pmic"], "kind": "pmic",
             "description": "Power management IC"},
            {"canonical_name": "C29", "aliases": [], "kind": "capacitor",
             "description": "Bulk cap"},
        ]
    }))
    (slug_dir / "dictionary.json").write_text(json.dumps({
        "entries": [
            {"canonical_name": "U7", "role": "PMIC", "package": "QFN-24",
             "typical_failure_modes": ["short"]},
            {"canonical_name": "C29", "role": "decoupling", "package": "0402",
             "typical_failure_modes": []},
        ]
    }))
    (slug_dir / "rules.json").write_text(json.dumps({"rules": []}))
    return tmp_path


def _session_with_parts(refdeses: list[str]) -> SessionState:
    parts = [
        Part(refdes=r, layer=Layer.TOP, is_smd=True,
             bbox=(Point(x=0, y=0), Point(x=10, y=10)),
             pin_refs=[i * 2 for i in range(4)])
        for i, r in enumerate(refdeses)
    ]
    pins = []
    for i, r in enumerate(refdeses):
        for pin_idx in range(4):
            pins.append(Pin(
                part_refdes=r, index=pin_idx + 1,
                pos=Point(x=i * 20, y=pin_idx * 5),
                net="VDD" if pin_idx == 0 else None,
                layer=Layer.TOP,
            ))
    board = Board(
        board_id="b", file_hash="sha256:x", source_format="test",
        outline=[], parts=parts, pins=pins, nets=[], nails=[],
    )
    session = SessionState()
    session.set_board(board)
    return session


def test_case1_memory_and_board_both_present(seeded_memory: Path) -> None:
    session = _session_with_parts(["U7", "C29"])
    result = mb_get_component(
        device_slug="testdev", refdes="U7",
        memory_root=seeded_memory, session=session,
    )
    assert result["found"] is True
    assert result["canonical_name"] == "U7"
    assert result["memory_bank"] is not None
    assert result["memory_bank"]["role"] == "PMIC"
    assert result["memory_bank"]["package"] == "QFN-24"
    assert result["board"] is not None
    assert result["board"]["side"] == "top"
    assert result["board"]["pin_count"] == 4


def test_case2_memory_only_no_session(seeded_memory: Path) -> None:
    """Session=None → memory_bank populated, board is None."""
    result = mb_get_component(
        device_slug="testdev", refdes="U7",
        memory_root=seeded_memory, session=None,
    )
    assert result["found"] is True
    assert result["memory_bank"] is not None
    assert result["memory_bank"]["role"] == "PMIC"
    assert result["board"] is None


def test_case2_memory_only_refdes_absent_from_board(seeded_memory: Path) -> None:
    session = _session_with_parts(["C29"])  # no U7 on the board
    result = mb_get_component(
        device_slug="testdev", refdes="U7",
        memory_root=seeded_memory, session=session,
    )
    assert result["found"] is True
    assert result["memory_bank"] is not None
    assert result["board"] is None


def test_case3_board_only_no_memory_entry(seeded_memory: Path) -> None:
    """R1 is on the board but has no registry/dictionary entry."""
    session = _session_with_parts(["U7", "R1"])
    result = mb_get_component(
        device_slug="testdev", refdes="R1",
        memory_root=seeded_memory, session=session,
    )
    assert result["found"] is True
    assert result["canonical_name"] == "R1"
    assert result["memory_bank"] is None
    assert result["board"] is not None


def test_case4_neither_source_has_refdes(seeded_memory: Path) -> None:
    session = _session_with_parts(["U7"])
    result = mb_get_component(
        device_slug="testdev", refdes="U999",
        memory_root=seeded_memory, session=session,
    )
    assert result["found"] is False
    assert "closest_matches" in result
    assert "memory_bank" not in result
    assert "board" not in result


def test_closest_matches_merges_memory_and_board(seeded_memory: Path) -> None:
    """closest_matches is the union of memory bank and board candidates."""
    session = _session_with_parts(["U7", "U12"])
    result = mb_get_component(
        device_slug="testdev", refdes="U99",
        memory_root=seeded_memory, session=session,
    )
    assert result["found"] is False
    matches = set(result["closest_matches"])
    assert "U7" in matches


def test_no_schematic_key_ever(seeded_memory: Path) -> None:
    """schematic key is never present (api/vision/ stub)."""
    session = _session_with_parts(["U7"])
    result = mb_get_component(
        device_slug="testdev", refdes="U7",
        memory_root=seeded_memory, session=session,
    )
    assert "schematic" not in result
