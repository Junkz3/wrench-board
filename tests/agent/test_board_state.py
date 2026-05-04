"""Round-trip + replay tests for the boardview overlay persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from api.agent.board_state import (
    load_board_state,
    replay_board_state_to_ws,
    save_board_state,
)
from api.session.state import SessionState

SLUG = "test-device"
REPAIR = "r-board-state"


def test_serialize_roundtrip_preserves_overlay() -> None:
    s = SessionState()
    s.layer = "bottom"
    s.highlights = {"U7", "J1"}
    s.annotations = {"a1": {"refdes": "U7", "label": "check this"}}
    s.arrows = {"arr1": {"from": [10, 20], "to": [30, 40]}}
    s.dim_unrelated = True
    s.filter_prefix = "U"
    s.layer_visibility = {"top": True, "bottom": False}

    snap = s.serialize_view()
    s2 = SessionState()
    s2.restore_view(snap)

    assert s2.layer == "bottom"
    assert s2.highlights == {"U7", "J1"}
    assert s2.annotations == {"a1": {"refdes": "U7", "label": "check this"}}
    assert s2.arrows == {"arr1": {"from": [10, 20], "to": [30, 40]}}
    assert s2.dim_unrelated is True
    assert s2.filter_prefix == "U"
    assert s2.layer_visibility == {"top": True, "bottom": False}


def test_save_skips_empty_overlay(tmp_path: Path) -> None:
    """A pristine session shouldn't write a no-op board_state.json."""
    s = SessionState()
    save_board_state(
        memory_root=tmp_path, device_slug=SLUG, repair_id=REPAIR, session=s
    )
    assert (
        tmp_path / SLUG / "repairs" / REPAIR / "board_state.json"
    ).exists() is False


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    s = SessionState()
    s.highlights = {"U18", "U7"}
    s.dim_unrelated = True
    save_board_state(
        memory_root=tmp_path, device_slug=SLUG, repair_id=REPAIR, session=s
    )
    loaded = load_board_state(
        memory_root=tmp_path, device_slug=SLUG, repair_id=REPAIR
    )
    assert loaded is not None
    assert sorted(loaded["highlights"]) == ["U18", "U7"]
    assert loaded["dim_unrelated"] is True


def test_load_returns_none_when_absent(tmp_path: Path) -> None:
    assert load_board_state(
        memory_root=tmp_path, device_slug=SLUG, repair_id=REPAIR
    ) is None


def test_save_noop_without_repair_id(tmp_path: Path) -> None:
    """Anonymous sessions skip persistence entirely."""
    s = SessionState()
    s.highlights = {"U1"}
    save_board_state(
        memory_root=tmp_path, device_slug=SLUG, repair_id=None, session=s
    )
    assert not any(tmp_path.rglob("board_state.json"))


class _FakeWS:
    """Captures ws.send_json calls for replay assertions."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.events.append(payload)


@pytest.mark.asyncio
async def test_replay_emits_events_in_correct_order() -> None:
    snapshot = {
        "layer": "bottom",
        "highlights": ["U7", "J1"],
        "highlight_color": "warn",
        "last_focused": None,
        "last_focused_bbox": None,
        "net_highlight": None,
        "annotations": {"a1": {"refdes": "U7", "label": "check"}},
        "arrows": {},
        "dim_unrelated": True,
        "filter_prefix": "U",
        "layer_visibility": {"top": True, "bottom": True},
    }
    ws = _FakeWS()
    sent = await replay_board_state_to_ws(ws, snapshot)
    types = [e["type"] for e in ws.events]
    assert sent == 5
    # Order matters: layout/filter/highlights → annotations → dim last.
    assert types[0] == "boardview.flip"
    assert types[1] == "boardview.filter"
    assert types[2] == "boardview.highlight"
    assert types[3] == "boardview.annotate"
    assert types[-1] == "boardview.dim_unrelated"
    # Highlight payload preserves the saved color, not a hard-coded accent.
    h = next(e for e in ws.events if e["type"] == "boardview.highlight")
    assert sorted(h["refdes"]) == ["J1", "U7"]
    assert h["color"] == "warn"


@pytest.mark.asyncio
async def test_replay_emits_focus_event_when_set() -> None:
    snapshot = {
        "layer": "top",
        "highlights": ["U7"],
        "highlight_color": "accent",
        "last_focused": "U7",
        "last_focused_bbox": [[10, 20], [30, 40]],
        "last_focused_zoom": 2.5,
        "net_highlight": None,
        "annotations": {},
        "arrows": {},
        "dim_unrelated": False,
        "filter_prefix": None,
        "layer_visibility": {"top": True, "bottom": True},
    }
    ws = _FakeWS()
    await replay_board_state_to_ws(ws, snapshot)
    focus = next((e for e in ws.events if e["type"] == "boardview.focus"), None)
    assert focus is not None
    assert focus["refdes"] == "U7"
    assert focus["bbox"] == [[10, 20], [30, 40]]
    assert focus["zoom"] == 2.5
    # Focus must come AFTER the broad highlight so it isn't clobbered.
    types = [e["type"] for e in ws.events]
    assert types.index("boardview.focus") > types.index("boardview.highlight")


@pytest.mark.asyncio
async def test_replay_empty_snapshot_emits_nothing() -> None:
    ws = _FakeWS()
    sent = await replay_board_state_to_ws(ws, {
        "layer": "top",
        "highlights": [],
        "net_highlight": None,
        "annotations": {},
        "arrows": {},
        "dim_unrelated": False,
        "filter_prefix": None,
        "layer_visibility": {"top": True, "bottom": True},
    })
    assert sent == 0
    assert ws.events == []
