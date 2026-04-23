# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.agent.chat_history import (
    append_event,
    create_conversation,
    ensure_conversation,
    list_conversations,
    load_events,
    touch_conversation,
)


SLUG = "test-device"
REPAIR = "r-123"


def _repair_root(tmp_path: Path) -> Path:
    return tmp_path / SLUG / "repairs" / REPAIR


def test_list_empty_when_no_index(tmp_path: Path) -> None:
    assert list_conversations(
        device_slug=SLUG, repair_id=REPAIR, memory_root=tmp_path
    ) == []


def test_create_conversation_writes_index(tmp_path: Path) -> None:
    conv_id = create_conversation(
        device_slug=SLUG, repair_id=REPAIR, tier="fast",
        memory_root=tmp_path,
    )
    assert conv_id and len(conv_id) >= 5
    index_path = _repair_root(tmp_path) / "conversations" / "index.json"
    assert index_path.exists()
    data = json.loads(index_path.read_text())
    assert isinstance(data, list) and len(data) == 1
    assert data[0]["id"] == conv_id
    assert data[0]["tier"] == "fast"
    assert data[0]["closed"] is False
    assert data[0]["turns"] == 0


def test_create_second_conversation_closes_previous(tmp_path: Path) -> None:
    first = create_conversation(
        device_slug=SLUG, repair_id=REPAIR, tier="fast",
        memory_root=tmp_path,
    )
    second = create_conversation(
        device_slug=SLUG, repair_id=REPAIR, tier="deep",
        memory_root=tmp_path,
    )
    convs = list_conversations(
        device_slug=SLUG, repair_id=REPAIR, memory_root=tmp_path
    )
    ids = [c["id"] for c in convs]
    closed = {c["id"]: c["closed"] for c in convs}
    assert ids == [first, second]
    assert closed[first] is True
    assert closed[second] is False


def test_ensure_none_uses_active(tmp_path: Path) -> None:
    first = create_conversation(
        device_slug=SLUG, repair_id=REPAIR, tier="fast",
        memory_root=tmp_path,
    )
    resolved, created = ensure_conversation(
        device_slug=SLUG, repair_id=REPAIR, conv_id=None, tier="fast",
        memory_root=tmp_path,
    )
    assert resolved == first
    assert created is False


def test_ensure_none_creates_when_empty(tmp_path: Path) -> None:
    resolved, created = ensure_conversation(
        device_slug=SLUG, repair_id=REPAIR, conv_id=None, tier="fast",
        memory_root=tmp_path,
    )
    assert resolved and created is True
    convs = list_conversations(
        device_slug=SLUG, repair_id=REPAIR, memory_root=tmp_path
    )
    assert len(convs) == 1 and convs[0]["id"] == resolved


def test_ensure_new_always_creates(tmp_path: Path) -> None:
    first = create_conversation(
        device_slug=SLUG, repair_id=REPAIR, tier="fast",
        memory_root=tmp_path,
    )
    resolved, created = ensure_conversation(
        device_slug=SLUG, repair_id=REPAIR, conv_id="new", tier="normal",
        memory_root=tmp_path,
    )
    assert resolved != first and created is True
    convs = list_conversations(
        device_slug=SLUG, repair_id=REPAIR, memory_root=tmp_path
    )
    assert len(convs) == 2 and convs[1]["id"] == resolved
    assert convs[0]["closed"] is True
    assert convs[1]["closed"] is False
    assert convs[1]["tier"] == "normal"


def test_ensure_existing_id_passes_through(tmp_path: Path) -> None:
    first = create_conversation(
        device_slug=SLUG, repair_id=REPAIR, tier="fast",
        memory_root=tmp_path,
    )
    _ = create_conversation(
        device_slug=SLUG, repair_id=REPAIR, tier="deep",
        memory_root=tmp_path,
    )
    resolved, created = ensure_conversation(
        device_slug=SLUG, repair_id=REPAIR, conv_id=first, tier="fast",
        memory_root=tmp_path,
    )
    assert resolved == first and created is False


def test_ensure_unknown_id_raises(tmp_path: Path) -> None:
    create_conversation(
        device_slug=SLUG, repair_id=REPAIR, tier="fast",
        memory_root=tmp_path,
    )
    with pytest.raises(KeyError):
        ensure_conversation(
            device_slug=SLUG, repair_id=REPAIR, conv_id="doesnotexist",
            tier="fast", memory_root=tmp_path,
        )


def test_touch_sets_title_once(tmp_path: Path) -> None:
    conv_id = create_conversation(
        device_slug=SLUG, repair_id=REPAIR, tier="fast",
        memory_root=tmp_path,
    )
    touch_conversation(
        device_slug=SLUG, repair_id=REPAIR, conv_id=conv_id,
        first_message="Le board ne boot plus depuis la chute.",
        memory_root=tmp_path,
    )
    touch_conversation(
        device_slug=SLUG, repair_id=REPAIR, conv_id=conv_id,
        first_message="SECOND should NOT overwrite.",
        memory_root=tmp_path,
    )
    convs = list_conversations(
        device_slug=SLUG, repair_id=REPAIR, memory_root=tmp_path
    )
    assert convs[0]["title"].startswith("Le board ne boot plus")
    assert "SECOND" not in convs[0]["title"]


def test_touch_accumulates_cost_and_turns(tmp_path: Path) -> None:
    conv_id = create_conversation(
        device_slug=SLUG, repair_id=REPAIR, tier="fast",
        memory_root=tmp_path,
    )
    touch_conversation(
        device_slug=SLUG, repair_id=REPAIR, conv_id=conv_id,
        cost_usd=0.003, memory_root=tmp_path,
    )
    touch_conversation(
        device_slug=SLUG, repair_id=REPAIR, conv_id=conv_id,
        cost_usd=0.005, memory_root=tmp_path,
    )
    convs = list_conversations(
        device_slug=SLUG, repair_id=REPAIR, memory_root=tmp_path
    )
    assert convs[0]["turns"] == 2
    assert convs[0]["cost_usd"] == pytest.approx(0.008, abs=1e-6)


def test_events_scoped_to_conversation(tmp_path: Path) -> None:
    a = create_conversation(
        device_slug=SLUG, repair_id=REPAIR, tier="fast",
        memory_root=tmp_path,
    )
    b = create_conversation(
        device_slug=SLUG, repair_id=REPAIR, tier="fast",
        memory_root=tmp_path,
    )
    append_event(
        device_slug=SLUG, repair_id=REPAIR, conv_id=a,
        event={"role": "user", "content": "msg-in-A"},
        memory_root=tmp_path,
    )
    append_event(
        device_slug=SLUG, repair_id=REPAIR, conv_id=b,
        event={"role": "user", "content": "msg-in-B"},
        memory_root=tmp_path,
    )
    events_a = load_events(
        device_slug=SLUG, repair_id=REPAIR, conv_id=a,
        memory_root=tmp_path,
    )
    events_b = load_events(
        device_slug=SLUG, repair_id=REPAIR, conv_id=b,
        memory_root=tmp_path,
    )
    assert [e["content"] for e in events_a] == ["msg-in-A"]
    assert [e["content"] for e in events_b] == ["msg-in-B"]


def test_migration_legacy_messages_jsonl(tmp_path: Path) -> None:
    # Set up a legacy repair: messages.jsonl sitting at the repair root,
    # no conversations/ subtree yet.
    repair_dir = _repair_root(tmp_path)
    repair_dir.mkdir(parents=True, exist_ok=True)
    legacy = repair_dir / "messages.jsonl"
    legacy.write_text(
        '{"ts":"2026-04-22T10:00:00Z","event":{"role":"user","content":"legacy hello"}}\n'
        '{"ts":"2026-04-22T10:00:05Z","event":{"role":"assistant","content":"legacy reply"}}\n'
    )
    # ensure_conversation(None, ...) should migrate and return the migrated id.
    resolved, created = ensure_conversation(
        device_slug=SLUG, repair_id=REPAIR, conv_id=None, tier="fast",
        memory_root=tmp_path,
    )
    assert resolved
    # Not a freshly created empty conv — it's the migrated one.
    assert created is True
    events = load_events(
        device_slug=SLUG, repair_id=REPAIR, conv_id=resolved,
        memory_root=tmp_path,
    )
    assert [e["content"] for e in events] == ["legacy hello", "legacy reply"]
    # Legacy file removed or preserved? Spec says keep for safety; check not crashed.
    # We don't assert on legacy existence — the migration may leave it or move it.
