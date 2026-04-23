# Multi-Conversation Per Repair — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a technician maintain several independent conversations inside a single `repair_id`, cleanly cleave when switching tier or clicking "+ Nouvelle conversation", and browse past conversations through a popover in the chat panel's status strip.

**Architecture:** Backend-and-frontend coordinated feature. Storage moves from a single `messages.jsonl` per repair to `conversations/{conv_id}/messages.jsonl` + `conversations/index.json`. Each conversation owns its own MA session (managed) or replay scope (direct). The WS endpoint gains a `conv` query param. Legacy repairs with a flat `messages.jsonl` are migrated lazily on first access.

**Tech Stack:** Python 3.11+, FastAPI, pytest; vanilla JS/HTML/CSS.

**Spec:** `docs/superpowers/specs/2026-04-23-multi-conversation-per-repair.md` (commit `9127a48`).

---

## Task 1: Storage layer — conversations module + updated chat_history

Extend `api/agent/chat_history.py` to thread `conv_id` through every file helper, plus a new set of conversation-lifecycle functions. Cover with focused unit tests.

**Files:**
- Modify: `api/agent/chat_history.py`
- Create: `tests/agent/test_conversations.py`

- [ ] **Step 1: Write failing tests for the conversation lifecycle helpers**

Create `tests/agent/test_conversations.py` with these tests (full code):

```python
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
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `.venv/bin/pytest tests/agent/test_conversations.py -v`
Expected: ImportError or fail on most assertions (functions don't exist yet).

- [ ] **Step 3: Add the conversation helpers to `api/agent/chat_history.py`**

Open `api/agent/chat_history.py`. Near the top, add:

```python
import secrets
from pathlib import Path
```

(If already imported, merge.) Then add these helpers at the end of the file, after the existing `touch_status` function:

```python
# ------------ Conversations (multi-thread per repair) ------------
# A repair holds N conversations under `conversations/{conv_id}/`, each with
# its own messages.jsonl and optional MA session pointer. An ordered index
# at `conversations/index.json` lists them chronologically with metadata
# for the UI popover.

def _conv_root(memory_root: Path, device_slug: str, repair_id: str) -> Path:
    return _repair_dir(memory_root, device_slug, repair_id) / "conversations"


def _conv_index_file(memory_root: Path, device_slug: str, repair_id: str) -> Path:
    return _conv_root(memory_root, device_slug, repair_id) / "index.json"


def _conv_dir(memory_root: Path, device_slug: str, repair_id: str, conv_id: str) -> Path:
    return _conv_root(memory_root, device_slug, repair_id) / conv_id


def _read_index(memory_root: Path, device_slug: str, repair_id: str) -> list[dict]:
    path = _conv_index_file(memory_root, device_slug, repair_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        logger.warning("corrupt conversations/index.json at %s; treating as empty", path)
        return []


def _write_index(
    memory_root: Path, device_slug: str, repair_id: str, index: list[dict]
) -> None:
    path = _conv_index_file(memory_root, device_slug, repair_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, indent=2), encoding="utf-8")


def list_conversations(
    *,
    device_slug: str,
    repair_id: str,
    memory_root: Path | None = None,
) -> list[dict]:
    """Return the ordered list of conversations for a repair (oldest first)."""
    root = memory_root or get_settings().memory_root
    return _read_index(root, device_slug, repair_id)


def create_conversation(
    *,
    device_slug: str,
    repair_id: str,
    tier: str,
    memory_root: Path | None = None,
) -> str:
    """Create a fresh conversation, close the previous active one, return its id."""
    root = memory_root or get_settings().memory_root
    index = _read_index(root, device_slug, repair_id)
    # Close any existing open entries (typically the last one).
    for entry in index:
        if not entry.get("closed"):
            entry["closed"] = True
    conv_id = secrets.token_hex(4)  # 8 hex chars
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    index.append(
        {
            "id": conv_id,
            "started_at": now,
            "tier": tier,
            "model": None,
            "last_turn_at": None,
            "cost_usd": 0.0,
            "turns": 0,
            "title": None,
            "closed": False,
        }
    )
    _conv_dir(root, device_slug, repair_id, conv_id).mkdir(parents=True, exist_ok=True)
    _write_index(root, device_slug, repair_id, index)
    return conv_id


def ensure_conversation(
    *,
    device_slug: str,
    repair_id: str,
    conv_id: str | None,
    tier: str,
    memory_root: Path | None = None,
) -> tuple[str, bool]:
    """Resolve a conv_id to the right target, creating / migrating when needed.

    Semantics:
      - `conv_id is None` → active (most recent). If none exist, migrate
        from legacy messages.jsonl if present, else create a fresh one.
      - `conv_id == "new"` → always create a fresh conversation.
      - `conv_id` matches an existing entry → pass through untouched.
      - Unknown `conv_id` → raise KeyError.

    Returns `(resolved_id, created)` — `created` is True when this call
    created or migrated a conversation.
    """
    root = memory_root or get_settings().memory_root
    if conv_id == "new":
        return create_conversation(
            device_slug=device_slug, repair_id=repair_id, tier=tier,
            memory_root=root,
        ), True

    index = _read_index(root, device_slug, repair_id)

    if conv_id is None:
        if index:
            # Active = most recent (last in index) — even if marked closed,
            # we open it read-only. Callers can decide to create a new one.
            return index[-1]["id"], False
        # No index yet — migrate legacy if present, else create fresh.
        legacy = _history_file(root, device_slug, repair_id)
        if legacy.exists():
            return _migrate_legacy(
                root=root, device_slug=device_slug, repair_id=repair_id,
                tier=tier,
            ), True
        return create_conversation(
            device_slug=device_slug, repair_id=repair_id, tier=tier,
            memory_root=root,
        ), True

    # Explicit id — must exist.
    if not any(entry["id"] == conv_id for entry in index):
        raise KeyError(f"unknown conversation {conv_id!r} for repair {repair_id!r}")
    return conv_id, False


def _migrate_legacy(
    *, root: Path, device_slug: str, repair_id: str, tier: str
) -> str:
    """Move repair-root messages.jsonl into a new conversation."""
    legacy = _history_file(root, device_slug, repair_id)
    conv_id = secrets.token_hex(4)
    conv_dir = _conv_dir(root, device_slug, repair_id, conv_id)
    conv_dir.mkdir(parents=True, exist_ok=True)
    # Move atomically (rename inside same fs).
    target = conv_dir / "messages.jsonl"
    legacy.rename(target)
    # Derive title from first user message if readable.
    title: str | None = None
    turns = 0
    try:
        for line in target.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = rec.get("event") or {}
            if event.get("role") == "user" and not title:
                content = event.get("content")
                if isinstance(content, str):
                    title = content
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            title = block.get("text") or None
                            break
            if event.get("role") == "assistant":
                turns += 1
    except OSError:
        pass
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    index: list[dict] = [
        {
            "id": conv_id,
            "started_at": now,
            "tier": tier,
            "model": None,
            "last_turn_at": now,
            "cost_usd": 0.0,
            "turns": turns,
            "title": (title or "")[:80].replace("\n", " ").strip() or None,
            "closed": False,
        }
    ]
    _write_index(root, device_slug, repair_id, index)
    return conv_id


def touch_conversation(
    *,
    device_slug: str,
    repair_id: str,
    conv_id: str,
    cost_usd: float | None = None,
    first_message: str | None = None,
    model: str | None = None,
    memory_root: Path | None = None,
) -> None:
    """Update the conversation's metadata in index.json — title, cost, turns, last_turn_at."""
    root = memory_root or get_settings().memory_root
    index = _read_index(root, device_slug, repair_id)
    updated = False
    for entry in index:
        if entry["id"] != conv_id:
            continue
        if first_message and not entry.get("title"):
            entry["title"] = first_message[:80].replace("\n", " ").strip() or None
        if cost_usd is not None:
            entry["cost_usd"] = round((entry.get("cost_usd") or 0.0) + cost_usd, 6)
            entry["turns"] = (entry.get("turns") or 0) + 1
            entry["last_turn_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        if model and not entry.get("model"):
            entry["model"] = model
        updated = True
        break
    if updated:
        _write_index(root, device_slug, repair_id, index)


def close_conversation(
    *, device_slug: str, repair_id: str, conv_id: str,
    memory_root: Path | None = None,
) -> None:
    """Mark a conversation as closed in the index (informational only)."""
    root = memory_root or get_settings().memory_root
    index = _read_index(root, device_slug, repair_id)
    for entry in index:
        if entry["id"] == conv_id and not entry.get("closed"):
            entry["closed"] = True
            _write_index(root, device_slug, repair_id, index)
            return
```

- [ ] **Step 4: Thread `conv_id` through the existing file helpers**

Update these functions in `api/agent/chat_history.py` — each gains a `conv_id: str` parameter AFTER `repair_id`. File path shifts from `repairs/{repair}/messages.jsonl` to `repairs/{repair}/conversations/{conv}/messages.jsonl`:

- `_history_file` — new signature: `(memory_root, device_slug, repair_id, conv_id)` returns conversation-scoped path.

Replace the function body with:

```python
def _history_file(
    memory_root: Path, device_slug: str, repair_id: str, conv_id: str
) -> Path:
    return _conv_dir(memory_root, device_slug, repair_id, conv_id) / "messages.jsonl"
```

Do the same for `append_event`, `load_events`, `load_events_with_costs`, `save_ma_session_id`, `load_ma_session_id`:

- Add `conv_id: str` parameter.
- For `save_ma_session_id` / `load_ma_session_id`: key the MA session file at `conversations/{conv_id}/ma_session.json` (per-conv isolation). Replace their internal path builders accordingly.

Example for `append_event`:

```python
def append_event(
    *,
    device_slug: str,
    repair_id: str | None,
    conv_id: str,
    event: dict[str, Any],
    cost: dict[str, Any] | None = None,
    memory_root: Path | None = None,
) -> None:
    if not repair_id or not event:
        return
    settings = get_settings()
    if settings.chat_history_backend != "jsonl":
        return
    root = memory_root or settings.memory_root
    path = _history_file(root, device_slug, repair_id, conv_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "event": event,
    }
    if cost is not None:
        record["cost"] = cost
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as err:
        logger.warning("append_event failed: %s", err)
```

Apply the same `conv_id` insertion to `load_events`, `load_events_with_costs`, `save_ma_session_id`, `load_ma_session_id`. The MA session file also moves: `_ma_session_file(root, slug, repair_id, conv_id, tier)` returns `conversations/{conv_id}/ma_session_{tier}.json` (keep tier in filename to support per-tier fallback within the same conv if ever needed).

**Re-export** `list_conversations`, `create_conversation`, `ensure_conversation`, `touch_conversation`, `close_conversation` implicitly by public functions at module top (they're already defined in the new code block).

- [ ] **Step 5: Re-run tests until green**

Run: `.venv/bin/pytest tests/agent/test_conversations.py -v`
Expected: all 11 tests pass.

- [ ] **Step 6: Run the whole suite — callsites will break**

Run: `.venv/bin/pytest -x`
Expected: failures in `runtime_direct` / `runtime_managed` callers — they don't pass `conv_id` yet. This is fine; Task 2 fixes them.

- [ ] **Step 7: Commit**

```bash
git add api/agent/chat_history.py tests/agent/test_conversations.py
git commit -m "$(cat <<'EOF'
feat(agent/chat_history): conversation-scoped storage + lifecycle helpers

Adds list/create/ensure/touch/close_conversation on top of a new
conversations/{conv_id}/messages.jsonl layout with an index.json
metadata file per repair. Threads conv_id through append_event /
load_events / save_ma_session_id / load_ma_session_id.

Legacy repair dirs with a flat messages.jsonl migrate lazily on first
ensure_conversation(None, ...) call — the file moves under a new
conv_id and an initial index.json is written.

Runtime callsites are not yet updated; that lands in the next commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/agent/chat_history.py tests/agent/test_conversations.py
```

---

## Task 2: Runtimes + WS + HTTP surface

Thread `conv_id` through both runtimes, extend the WS query param surface, add the listing HTTP endpoint. Break-and-fix: Task 1's storage rewrite has broken the runtimes; this task restores them and extends them with conversation awareness.

**Files:**
- Modify: `api/main.py` — WS `/ws/diagnostic/{slug}` reads `conv` param.
- Modify: `api/agent/runtime_direct.py`
- Modify: `api/agent/runtime_managed.py`
- Modify: `api/pipeline/__init__.py` — new GET /repairs/{id}/conversations.

- [ ] **Step 1: WS endpoint — read `conv` param**

In `api/main.py`, find the `/ws/diagnostic/{device_slug}` handler. After `repair_id = websocket.query_params.get("repair") or None`, add:

```python
    conv_id = websocket.query_params.get("conv") or None
```

Pass `conv_id` to both runtime dispatchers (the two `run_session` calls in the mode branch). Keep the signatures aligned.

- [ ] **Step 2: runtime_direct — call ensure_conversation, use resolved id everywhere**

Open `api/agent/runtime_direct.py`. At the top of the entry function `run_session` (look for the signature accepting `ws, device_slug, tier, repair_id`), add `conv_id` to the signature and, **as the first substantive action**, resolve it:

```python
async def run_session(
    websocket: WebSocket,
    device_slug: str,
    *,
    tier: str,
    repair_id: str | None,
    conv_id: str | None = None,
) -> None:
    …
    # Resolve the conversation once; every write/read below targets this id.
    resolved_conv_id: str | None = None
    if repair_id:
        from api.agent.chat_history import ensure_conversation
        resolved_conv_id, _created = ensure_conversation(
            device_slug=device_slug, repair_id=repair_id,
            conv_id=conv_id, tier=tier,
        )
```

Then:
- Every `append_event(...)` call gains `conv_id=resolved_conv_id` after `repair_id=repair_id`.
- Every `load_events_with_costs(...)` call gains `conv_id=resolved_conv_id`.
- The `session_ready` WS emission — find where it's sent and add `"conv_id": resolved_conv_id`, `"tier": tier`, and a `"conversation_count": len(list_conversations(device_slug=device_slug, repair_id=repair_id))` so the frontend can paint the chip immediately.
- In the per-turn cost handler, after computing `cost_usd`, call:
  ```python
  from api.agent.chat_history import touch_conversation
  touch_conversation(
      device_slug=device_slug, repair_id=repair_id,
      conv_id=resolved_conv_id, cost_usd=cost_usd,
      model=<the model string>,
  )
  ```
- On the FIRST `user.message` event of the session (right before `append_event` for it), call:
  ```python
  touch_conversation(
      device_slug=device_slug, repair_id=repair_id,
      conv_id=resolved_conv_id, first_message=<the text>,
  )
  ```
  Use a local `first_user_seen = False` flag and flip it after the first.

- [ ] **Step 3: runtime_managed — same treatment**

Mirror the changes in `api/agent/runtime_managed.py`. In addition, the MA session-id storage (`save_ma_session_id` / `load_ma_session_id`) now needs `conv_id=resolved_conv_id` — each conversation has its own MA session id. Keep the tier in the key so we can still save tier-specific sessions per conv (for the rare case a conv's tier evolves — though today tier is fixed per conv).

- [ ] **Step 4: New HTTP route — list conversations**

In `api/pipeline/__init__.py`, add a new route near the existing repair routes:

```python
@router.get("/repairs/{repair_id}/conversations")
def list_repair_conversations(repair_id: str) -> dict:
    """Return the conversation index for a repair.

    The repair's device_slug is inferred from the metadata file one level
    up in memory/{slug}/repairs/{repair_id}.json — clients don't pass it.
    """
    from api.agent.chat_history import list_conversations, load_repair_metadata
    settings = get_settings()
    # Scan repairs/ tree to find which slug owns this id.
    memory = Path(settings.memory_root)
    found_slug: str | None = None
    for metadata_file in memory.glob("*/repairs/" + repair_id + ".json"):
        found_slug = metadata_file.parent.parent.name
        break
    if not found_slug:
        raise HTTPException(status_code=404, detail=f"unknown repair_id {repair_id}")
    convs = list_conversations(device_slug=found_slug, repair_id=repair_id)
    return {"device_slug": found_slug, "repair_id": repair_id, "conversations": convs}
```

(Imports: add `HTTPException` from `fastapi` if not already present, and `Path` from `pathlib`, and `get_settings` from `api.config`.)

- [ ] **Step 5: Run the whole test suite**

Run: `.venv/bin/pytest -x`
Expected: all tests pass. If `tests/agent/test_manifest_dynamic.py` or similar still fails, update their callsites to pass `conv_id=<some-fixture-id>`.

- [ ] **Step 6: Manual smoke**

```bash
make run &
sleep 2
# Open a repair in the browser; send a message; confirm no 500 in logs.
# Then curl the new endpoint:
curl -s http://localhost:8000/pipeline/repairs/<repair_id>/conversations | jq .
kill %1
```

Expected: JSON with one conversation after the message lands.

- [ ] **Step 7: Commit**

```bash
git add api/main.py api/agent/runtime_direct.py api/agent/runtime_managed.py api/pipeline/__init__.py
git commit -m "$(cat <<'EOF'
feat(agent,pipeline): conv_id routing through WS + runtimes + list endpoint

WS /ws/diagnostic/{slug} now reads ?conv= and passes it through to both
runtimes. Each runtime calls ensure_conversation() at startup, scopes
every append_event / load_events / touch_conversation call to the
resolved id, and emits conv_id + tier + conversation_count in
session_ready.

New HTTP route GET /pipeline/repairs/{id}/conversations returns the
index.json + device_slug so the UI can populate its conversation
switcher without extra lookups.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/main.py api/agent/runtime_direct.py api/agent/runtime_managed.py api/pipeline/__init__.py
```

---

## Task 3: Frontend — conversation chip, popover, and switcher

Wire the UI: chip in the status strip, popover with the list + "+ Nouvelle conversation" button, reconnection logic, tier-switch now implies `conv=new`.

**Files:**
- Modify: `web/llm_panel.html`
- Modify: `web/js/llm.js`
- Modify: `web/styles/llm.css`

- [ ] **Step 1: Add the chip + popover markup to `web/llm_panel.html`**

Find the `<div class="llm-status" id="llmStatus">` block. Replace it with:

```html
<div class="llm-status" id="llmStatus">
  <span class="dot"></span>
  <span id="llmStatusText">inactif</span>
  <span class="device-tag" id="llmDevice" style="display:none"></span>

  <div class="conv-wrap">
    <button class="conv-chip" id="llmConvChip" aria-haspopup="menu" aria-expanded="false" title="Conversations">
      <span class="conv-label" id="llmConvLabel">CONV 1/1</span>
      <svg class="chevron-down" width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
    </button>
    <div class="conv-popover" id="llmConvPopover" role="menu" hidden>
      <div class="conv-list" id="llmConvList"></div>
      <div class="conv-popover-sep"></div>
      <button class="conv-new" id="llmConvNew" type="button">
        <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>
        Nouvelle conversation
      </button>
    </div>
  </div>

  <span class="cost-total" id="llmCostTotal" style="display:none">$0.00</span>
</div>
```

- [ ] **Step 2: CSS for the chip + popover — append to `web/styles/llm.css`**

```css
/* ============ Conversation chip + popover ============ */
.conv-wrap { position: relative; display: inline-block; }

.conv-chip {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 3px 7px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 4px;
  cursor: pointer;
  transition: all .15s;
  font-family: var(--mono); font-size: 10px; letter-spacing: .4px;
  color: var(--text-2);
  text-transform: uppercase;
}
.conv-chip:hover { color: var(--text); border-color: #2e4468; }
.conv-chip .chevron-down { transition: transform .15s; opacity: .7; }
.conv-chip[aria-expanded="true"] .chevron-down { transform: rotate(180deg); }

.conv-popover {
  position: absolute; top: calc(100% + 6px); left: 0;
  min-width: 280px; max-width: 340px; z-index: 40;
  background: rgba(20,32,48,.96);
  backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
  border: 1px solid var(--border); border-radius: 7px;
  box-shadow: 0 8px 24px rgba(0,0,0,.35);
  padding: 6px;
  display: flex; flex-direction: column; gap: 2px;
}
.conv-popover[hidden] { display: none; }
.conv-popover-sep {
  height: 1px; background: var(--border-soft);
  margin: 6px -2px 4px;
}

.conv-list { display: flex; flex-direction: column; gap: 2px; max-height: 300px; overflow-y: auto; }
.conv-list::-webkit-scrollbar { width: 6px; }
.conv-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

.conv-item {
  display: flex; flex-direction: column; gap: 3px;
  padding: 8px 10px;
  background: transparent; border: 1px solid transparent;
  border-left: 2px solid transparent;
  border-radius: 4px;
  cursor: pointer;
  text-align: left;
  color: var(--text-2);
  font-family: inherit;
  transition: background .15s, border-color .15s;
}
.conv-item:hover { background: var(--panel-2); color: var(--text); }
.conv-item.active {
  background: rgba(192,132,252,.08);
  border-left-color: var(--violet);
  color: var(--text);
}
.conv-item-head {
  display: flex; align-items: center; gap: 6px; min-width: 0;
}
.conv-item-tier {
  font-family: var(--mono); font-size: 9px; letter-spacing: .4px;
  text-transform: uppercase;
  padding: 1px 5px; border-radius: 3px;
  flex-shrink: 0;
}
.conv-item-tier.t-fast   { color: var(--emerald); background: rgba(52,211,153,.08); border: 1px solid rgba(52,211,153,.3); }
.conv-item-tier.t-normal { color: var(--cyan);    background: rgba(56,189,248,.08); border: 1px solid rgba(56,189,248,.3); }
.conv-item-tier.t-deep   { color: var(--violet);  background: rgba(192,132,252,.08); border: 1px solid rgba(192,132,252,.3); }

.conv-item-title {
  font-size: 12px; flex: 1;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.conv-item-meta {
  font-family: var(--mono); font-size: 9.5px;
  color: var(--text-3); letter-spacing: .3px;
  display: flex; gap: 6px;
}
.conv-item-meta .conv-item-sep { opacity: .5; }

.conv-new {
  display: inline-flex; align-items: center; gap: 6px;
  background: transparent; border: 1px dashed rgba(192,132,252,.35);
  padding: 7px 10px; border-radius: 5px;
  cursor: pointer;
  color: var(--violet); font-family: inherit; font-size: 12px;
  transition: background .15s, border-color .15s;
}
.conv-new:hover {
  background: rgba(192,132,252,.08);
  border-color: rgba(192,132,252,.6);
  border-style: solid;
}
```

- [ ] **Step 3: JS — load, render, wire switch in `web/js/llm.js`**

Add near the other module-level state:

```js
let currentConvId = null;
let conversationsCache = [];
```

Add these helpers above `initLLMPanel`:

```js
async function loadConversations() {
  const rid = currentRepairId();
  if (!rid) { conversationsCache = []; renderConvItems(); return; }
  try {
    const res = await fetch(`/pipeline/repairs/${encodeURIComponent(rid)}/conversations`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    conversationsCache = Array.isArray(data.conversations) ? data.conversations : [];
    renderConvItems();
  } catch (err) {
    console.warn("[llm] loadConversations failed", err);
  }
}

function renderConvItems() {
  const list = el("llmConvList");
  const label = el("llmConvLabel");
  if (!list || !label) return;
  list.innerHTML = "";
  if (conversationsCache.length === 0) {
    label.textContent = "CONV 0/0";
    return;
  }
  const activeIdx = Math.max(0, conversationsCache.findIndex(c => c.id === currentConvId));
  label.textContent = `CONV ${activeIdx + 1}/${conversationsCache.length}`;
  conversationsCache.forEach((c, idx) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "conv-item" + (c.id === currentConvId ? " active" : "");
    btn.dataset.convId = c.id;
    const tier = (c.tier || "fast").toLowerCase();
    const title = escapeHTML((c.title || `Conversation ${idx + 1}`).slice(0, 80));
    const cost = Number(c.cost_usd || 0);
    const ago = c.last_turn_at ? humanAgo(c.last_turn_at) : "—";
    btn.innerHTML =
      `<span class="conv-item-head">` +
        `<span class="conv-item-tier t-${tier}">${tier.toUpperCase()}</span>` +
        `<span class="conv-item-title">${title}</span>` +
      `</span>` +
      `<span class="conv-item-meta">` +
        `<span>${c.turns || 0} turn${(c.turns || 0) === 1 ? "" : "s"}</span>` +
        `<span class="conv-item-sep">·</span>` +
        `<span>${fmtUsd(cost)}</span>` +
        `<span class="conv-item-sep">·</span>` +
        `<span>${ago}</span>` +
      `</span>`;
    btn.addEventListener("click", () => {
      if (c.id === currentConvId) { closeConvPopover(); return; }
      switchConv(c.id);
      closeConvPopover();
    });
    list.appendChild(btn);
  });
}

function humanAgo(iso) {
  try {
    const then = new Date(iso).getTime();
    const diff = Math.max(0, Date.now() - then) / 1000;
    if (diff < 60) return `il y a ${Math.floor(diff)} s`;
    if (diff < 3600) return `il y a ${Math.floor(diff / 60)} min`;
    if (diff < 86400) return `il y a ${Math.floor(diff / 3600)} h`;
    return `il y a ${Math.floor(diff / 86400)} j`;
  } catch { return "—"; }
}

function switchConv(convIdOrNew) {
  if (convIdOrNew === currentConvId) return;
  logSys(`→ changement de conversation : ${convIdOrNew}`);
  if (ws && ws.readyState <= 1) {
    try { ws.close(); } catch (_) {}
  }
  ws = null;
  // Route connect() to target the requested conv on reopen.
  pendingConvParam = convIdOrNew;
  connect();
}

let pendingConvParam = null;  // null = use active, string = ?conv=…

function openConvPopover() {
  const chip = el("llmConvChip");
  const pop = el("llmConvPopover");
  if (!chip || !pop) return;
  loadConversations(); // refresh on open
  pop.hidden = false;
  chip.setAttribute("aria-expanded", "true");
}
function closeConvPopover() {
  const chip = el("llmConvChip");
  const pop = el("llmConvPopover");
  if (!chip || !pop) return;
  pop.hidden = true;
  chip.setAttribute("aria-expanded", "false");
}
function toggleConvPopover() {
  const pop = el("llmConvPopover");
  if (!pop) return;
  if (pop.hidden) openConvPopover(); else closeConvPopover();
}
```

- [ ] **Step 4: Wire the chip + "Nouvelle conversation" + session_ready + turn_cost refreshes**

In `initLLMPanel` (after the tier popover wiring), add:

```js
  // Conversation chip + popover.
  const convChip = el("llmConvChip");
  const convPopover = el("llmConvPopover");
  const convNew = el("llmConvNew");
  convChip?.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleConvPopover();
  });
  convNew?.addEventListener("click", () => {
    switchConv("new");
    closeConvPopover();
  });
  document.addEventListener("click", (e) => {
    if (convPopover && !convPopover.hidden &&
        !convPopover.contains(e.target) && e.target !== convChip &&
        !convChip?.contains(e.target)) {
      closeConvPopover();
    }
  }, true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && convPopover && !convPopover.hidden) {
      closeConvPopover();
    }
  });
```

In the WS `session_ready` handler, after setting the subline, add:

```js
        currentConvId = payload.conv_id || null;
        loadConversations();
```

In the `turn_cost` handler (after the existing cost updates), debounce a refresh:

```js
        clearTimeout(window._llmConvRefreshT);
        window._llmConvRefreshT = setTimeout(() => loadConversations(), 500);
```

- [ ] **Step 5: Update `wsURL` and `connect` to forward `conv`**

Find `wsURL(slug, tier, repairId)`. Change its signature to accept `convParam`:

```js
function wsURL(slug, tier, repairId, convParam) {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const params = new URLSearchParams();
  if (tier) params.set("tier", tier);
  if (repairId) params.set("repair", repairId);
  if (convParam) params.set("conv", convParam);
  const q = params.toString() ? `?${params.toString()}` : "";
  return `${scheme}://${window.location.host}/ws/diagnostic/${encodeURIComponent(slug)}${q}`;
}
```

In `connect()`, replace the `wsURL(...)` call:

```js
  const url = wsURL(slug, currentTier, repairId, pendingConvParam);
  pendingConvParam = null;  // consume after this connect
```

- [ ] **Step 6: Update `switchTier` to imply `conv=new`**

Replace the WS close + reconnect block inside `switchTier`:

```js
  logSys(`→ changement de tier : ${newTier}. Nouvelle conversation.`);
  if (ws && ws.readyState <= 1) {
    try { ws.close(); } catch (_) { /* ignore */ }
  }
  ws = null;
  pendingConvParam = "new";  // new tier = new conversation
  connect();
```

- [ ] **Step 7: Reset state on every connect**

At the start of `connect()` (alongside the other counter resets), add:

```js
  currentConvId = null;
```

- [ ] **Step 8: Manual smoke**

```bash
make run
```

In the browser:
1. Open a repair → panel auto-opens → chip should show `CONV 1/1` after the session_ready (load happens on that event).
2. Send a message, wait for turn_cost → the conv updates with title + turns + cost.
3. Click the chip → popover opens, shows the item as active.
4. Click "+ Nouvelle conversation" → WS reconnects with `?conv=new`, a new item appears in the popover, chip becomes `CONV 2/2`.
5. Click the first item in the popover → WS reconnects to that conv, replay fires, messages reappear.
6. Change tier via the tier chip → a new conv is created automatically (chip shows `CONV 3/3`).

- [ ] **Step 9: Commit**

```bash
git add web/llm_panel.html web/js/llm.js web/styles/llm.css
git commit -m "$(cat <<'EOF'
feat(web/llm): multi-conversation switcher in the status strip

Adds a CONV N/T chip that opens a popover listing every conversation
of the current repair — each item shows its tier pill, title (derived
from the first user message), turn count, cost, and "how long ago".
Clicking an item reconnects the WS with ?conv=<id>; "+ Nouvelle
conversation" sends ?conv=new. Switching tier via the existing tier
chip now also spawns a fresh conversation on the new tier.

Conversation state is kept in currentConvId + conversationsCache; the
list is refreshed on session_ready and debounced 500ms after each
turn_cost.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- web/llm_panel.html web/js/llm.js web/styles/llm.css
```

---

## Post-implementation checks

- [ ] **Run the full suite + lint**

```bash
make test
make lint
```

Expected: all tests pass. Lint may have unrelated warnings from parallel work — ignore those; verify nothing NEW from this feature landed red.

- [ ] **Manual end-to-end smoke**

Walk through a full user flow with an existing repair:

1. Open a repair that has a legacy `messages.jsonl` at the repair root (any pre-existing repair). Confirm the first WS open migrates it — panel shows the chip `CONV 1/1`, history replays, no error.
2. Send a message, watch conv metadata update.
3. Click "+ Nouvelle conversation" — new conv, chip becomes `CONV 2/2`, popover lists both.
4. Click back to conv 1 — replay happens, messages reappear.
5. Close tab, reopen the repair URL — lands on the most recent (conv 2) by default (backend active resolution).

---

## Self-review checklist

- Every spec section has a task (§2 model, §3 index, §4 WS, §5 HTTP, §6 backend, §7 frontend, §8 migration, §9 tests).
- Types consistent: `conv_id: str` in all storage helpers; `conv_id: str | None` at WS entry; `resolved_conv_id: str` after `ensure_conversation`.
- No dangling TBDs. Every step has code or an explicit command.
- `pendingConvParam` state is threaded through `switchConv` / `switchTier` / `connect` consistently — set before `connect()`, consumed inside.
- The new route path matches the frontend call: `GET /pipeline/repairs/{id}/conversations`.
- No backwards-incompat breakage for callers that don't know about convs: `ensure_conversation(None, …)` either migrates legacy or creates a fresh one.
