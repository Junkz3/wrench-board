# SPDX-License-Identifier: Apache-2.0
"""Per-repair chat history persistence for diagnostic sessions.

Each repair_id owns a set of *conversations* under
`memory/{device_slug}/repairs/{repair_id}/conversations/{conv_id}/`. Every
conversation holds its own `messages.jsonl` (one `{ts, event}` record per
line, Anthropic Messages API shape) and, for the managed-agent runtime, its
own `ma_session_{tier}.json` session pointer. A sibling `index.json` lists
the conversations chronologically with lightweight metadata (tier, title,
turns, cost) for the frontend switcher.

Legacy repairs predate conversations and stored the flat file at
`repairs/{repair_id}/messages.jsonl`. The first call to
`ensure_conversation(conv_id=None, …)` for such a repair migrates the file
into a new conversation directory and writes a fresh `index.json`.

Backend is feature-flagged (`chat_history_backend` in settings):

- **jsonl (default)** — append-only local files. Works today without any
  Anthropic feature gate, survives restarts, is grep-able / git-diffable
  for debugging.
- **managed_agents (future)** — when the MA sessions Research Preview lands,
  each conversation will map to a persistent MA session_id; replay will be
  handled natively by the MA runtime. This module becomes a no-op in that
  mode — the backend will query MA for history instead.

Same design pattern as the field_reports module: JSON-first, MA as a mirror
when access lands. Zero migration when flipping.

Two signals are persisted for UI consumption:

- **messages.jsonl** carries the Anthropic-shaped trail (user.content,
  assistant.content, tool_use, tool_result blocks).
- **status.json** tracks the repair's lifecycle — `open` at creation,
  `in_progress` at first exchange, `closed` when the technician signals
  completion (button or agent confirmation). Updated by `touch_status` below.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api.config import get_settings

logger = logging.getLogger("wrench_board.agent.chat_history")


def _repair_dir(memory_root: Path, device_slug: str, repair_id: str) -> Path:
    return memory_root / device_slug / "repairs" / repair_id


def _conv_root(memory_root: Path, device_slug: str, repair_id: str) -> Path:
    return _repair_dir(memory_root, device_slug, repair_id) / "conversations"


def _conv_index_file(memory_root: Path, device_slug: str, repair_id: str) -> Path:
    return _conv_root(memory_root, device_slug, repair_id) / "index.json"


def _conv_dir(
    memory_root: Path, device_slug: str, repair_id: str, conv_id: str
) -> Path:
    return _conv_root(memory_root, device_slug, repair_id) / conv_id


def _history_file(
    memory_root: Path, device_slug: str, repair_id: str, conv_id: str
) -> Path:
    return _conv_dir(memory_root, device_slug, repair_id, conv_id) / "messages.jsonl"


def _legacy_history_file(
    memory_root: Path, device_slug: str, repair_id: str
) -> Path:
    """Pre-conversations flat file path — used only for migration."""
    return _repair_dir(memory_root, device_slug, repair_id) / "messages.jsonl"


def _ma_session_file(
    memory_root: Path,
    device_slug: str,
    repair_id: str,
    conv_id: str,
    tier: str,
) -> Path:
    return (
        _conv_dir(memory_root, device_slug, repair_id, conv_id)
        / f"ma_session_{tier}.json"
    )


def _metadata_file(memory_root: Path, device_slug: str, repair_id: str) -> Path:
    # The pre-existing metadata lives one level up: memory/{slug}/repairs/{id}.json
    return memory_root / device_slug / "repairs" / f"{repair_id}.json"


def _read_index(
    memory_root: Path, device_slug: str, repair_id: str
) -> list[dict[str, Any]]:
    path = _conv_index_file(memory_root, device_slug, repair_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        logger.warning(
            "corrupt conversations/index.json at %s; treating as empty", path
        )
        return []


def _write_index(
    memory_root: Path,
    device_slug: str,
    repair_id: str,
    index: list[dict[str, Any]],
) -> None:
    path = _conv_index_file(memory_root, device_slug, repair_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, indent=2), encoding="utf-8")


def append_event(
    *,
    device_slug: str,
    repair_id: str | None,
    conv_id: str,
    event: dict[str, Any],
    cost: dict[str, Any] | None = None,
    memory_root: Path | None = None,
) -> None:
    """Append one Anthropic-format message event to a conversation's JSONL.

    Optional `cost` attaches the per-turn token cost alongside an assistant
    event so the conversation's lifetime spend survives WS close/reopen. The
    record shape is `{ts, event, cost?}` — `cost` is only surfaced on the
    record (not inside `event`) so the Anthropic-facing `messages` list stays
    clean when load_events reads it back.

    No-ops silently when `repair_id` is missing (anonymous session), when
    `event` is falsy, or when the feature flag is set to a non-jsonl backend.
    Errors here must NEVER take down the diagnostic session — persistence is
    best-effort.
    """
    if not repair_id or not event:
        return
    settings = get_settings()
    if settings.chat_history_backend != "jsonl":
        return
    memory_root = memory_root or Path(settings.memory_root)
    path = _history_file(memory_root, device_slug, repair_id, conv_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
        }
        if cost is not None:
            record["cost"] = cost
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        logger.warning(
            "[ChatHistory] append_event failed for repair=%s conv=%s: %s",
            repair_id,
            conv_id,
            exc,
        )


def load_events(
    *,
    device_slug: str,
    repair_id: str | None,
    conv_id: str,
    memory_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Return the list of Anthropic-format events, in write order."""
    return [event for event, _cost in load_events_with_costs(
        device_slug=device_slug, repair_id=repair_id, conv_id=conv_id,
        memory_root=memory_root,
    )]


def load_events_with_costs(
    *,
    device_slug: str,
    repair_id: str | None,
    conv_id: str,
    memory_root: Path | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Like load_events but also returns each record's attached cost.

    Used by the replay path so the turn_cost chip + running-total accumulator
    can rebuild visually on reopen, matching what the tech saw live.
    """
    if not repair_id:
        return []
    settings = get_settings()
    if settings.chat_history_backend != "jsonl":
        return []
    memory_root = memory_root or Path(settings.memory_root)
    path = _history_file(memory_root, device_slug, repair_id, conv_id)
    if not path.exists():
        return []

    records: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "[ChatHistory] skipping malformed line in %s", path
                    )
                    continue
                event = rec.get("event")
                if not isinstance(event, dict):
                    continue
                cost = rec.get("cost") if isinstance(rec.get("cost"), dict) else None
                records.append((event, cost))
    except OSError as exc:
        logger.warning(
            "[ChatHistory] load_events failed for repair=%s conv=%s: %s",
            repair_id,
            conv_id,
            exc,
        )
    return records


def save_ma_session_id(
    *,
    device_slug: str,
    repair_id: str | None,
    conv_id: str,
    session_id: str,
    tier: str,
    memory_root: Path | None = None,
) -> None:
    """Persist the MA session_id for this conversation AND tier combo.

    Each tier (fast / normal / deep) has its own MA agent, therefore its
    own session_id. Storing a single ma_session_id at the conv level
    would confuse tier switches (resuming a fast session on the normal
    agent, etc.). The per-(conv, tier) file keeps them isolated.

    Silent no-op on any error.
    """
    if not repair_id or not session_id or not tier or not conv_id:
        return
    settings = get_settings()
    memory_root = memory_root or Path(settings.memory_root)
    path = _ma_session_file(memory_root, device_slug, repair_id, conv_id, tier)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": session_id,
            "tier": tier,
            "linked_at": datetime.now(UTC).isoformat(),
        }
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning(
            "[ChatHistory] save_ma_session_id failed for repair=%s conv=%s tier=%s: %s",
            repair_id,
            conv_id,
            tier,
            exc,
        )


def load_ma_session_id(
    *,
    device_slug: str,
    repair_id: str | None,
    conv_id: str,
    tier: str,
    memory_root: Path | None = None,
) -> str | None:
    """Return the persisted MA session_id for a (conv, tier) pair, or None."""
    if not tier or not repair_id or not conv_id:
        return None
    settings = get_settings()
    memory_root = memory_root or Path(settings.memory_root)
    path = _ma_session_file(memory_root, device_slug, repair_id, conv_id, tier)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "[ChatHistory] load_ma_session_id failed for repair=%s conv=%s tier=%s: %s",
            repair_id,
            conv_id,
            tier,
            exc,
        )
        return None
    sid = payload.get("session_id") if isinstance(payload, dict) else None
    return sid if isinstance(sid, str) and sid else None


def load_repair_metadata(
    *,
    device_slug: str,
    repair_id: str | None,
    memory_root: Path | None = None,
) -> dict[str, Any] | None:
    """Return the JSON payload of memory/{slug}/repairs/{repair_id}.json, or None."""
    if not repair_id:
        return None
    settings = get_settings()
    memory_root = memory_root or Path(settings.memory_root)
    path = _metadata_file(memory_root, device_slug, repair_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "[ChatHistory] load_repair_metadata failed for repair=%s: %s",
            repair_id,
            exc,
        )
        return None


def build_session_intro(
    *,
    device_slug: str,
    repair_id: str | None,
    memory_root: Path | None = None,
) -> str | None:
    """Compose the hidden bootstrap message the agent sees on session open.

    Carries the device identity and the client's reported symptom so the
    agent can immediately consult its mounts (grep field_reports/, lookup
    matching playbooks) and query mb_get_rules_for_symptoms without asking
    "which device are you on?". Returns None when there's nothing to tell
    (no repair_id given).
    """
    if not repair_id:
        return None
    meta = load_repair_metadata(
        device_slug=device_slug, repair_id=repair_id, memory_root=memory_root
    )
    if not meta:
        # Still worth surfacing the device slug even if the repair file is gone.
        return f"[New diagnostic session · device_slug: {device_slug}]"
    label = meta.get("device_label") or device_slug
    symptom = (meta.get("symptom") or "").strip()
    lines = [
        "[New diagnostic session]",
        f"Device: {label} (slug: {device_slug})",
    ]
    if symptom:
        lines.append(f"Symptom reported by the technician: {symptom}")
    lines.append(
        f"Start by grep'ing /mnt/memory/wrench-board-{device_slug}/field_reports/ "
        "to see past repairs, then mb_get_rules_for_symptoms for the "
        "applicable rules."
    )
    return "\n".join(lines)


CTX_TAG_PREFIX = "[ctx ·"


def build_ctx_tag(
    *,
    device_slug: str,
    repair_id: str | None,
    memory_root: Path | None = None,
) -> str | None:
    """Compose a single-line context tag prepended to every user turn.

    Smaller models (Haiku especially) don't reliably scan the full session
    history when greeted with a fresh terse message — they treat "salut" as
    a context-free hello and forget the device + symptom carried by the
    bootstrap intro on turn 1. Restating both as a stable prefix on every
    user message keeps that context in the foreground for ~25 tokens/turn,
    which is also a stable cache hit after the first turn.

    The wording is deliberately **passive** — `initial_complaint`, not
    `symptom`, with the value quoted so the tag is visibly
    self-delimiting. The system prompt instructs the agent to treat
    this tag as intake-sheet metadata, never as a fresh symptom
    declaration that should re-trigger `mb_get_rules_for_symptoms` /
    `mb_expand_knowledge` on a resumed session.

    Returns None when no repair_id is given (anonymous sessions don't
    have a known initial_complaint to restate).
    """
    if not repair_id:
        return None
    meta = load_repair_metadata(
        device_slug=device_slug, repair_id=repair_id, memory_root=memory_root
    )
    label = (meta or {}).get("device_label") or device_slug
    symptom = ((meta or {}).get("symptom") or "").strip()
    if symptom:
        return f'{CTX_TAG_PREFIX} device={label} ({device_slug}) · initial_complaint="{symptom}"]'
    return f"{CTX_TAG_PREFIX} device={label} ({device_slug})]"


def strip_ctx_tag(text: str) -> str:
    """Peel a leading `[ctx · …]` line from `text`, if present.

    Keeps the chat panel replay clean — without this, the per-turn ctx
    prefix would show up in front of every replayed user message. Safe
    no-op when no tag is present.
    """
    if not text.startswith(CTX_TAG_PREFIX):
        return text
    nl = text.find("\n\n")
    if nl < 0:
        # Tag-only message with no content — surface as empty.
        return ""
    return text[nl + 2 :]


def touch_status(
    *,
    device_slug: str,
    repair_id: str | None,
    status: str,
    memory_root: Path | None = None,
) -> None:
    """Update the repair's `status` field in memory/{slug}/repairs/{id}.json.

    Swallows all errors — metadata drift is acceptable, session crash is not.
    """
    if not repair_id or not status:
        return
    settings = get_settings()
    memory_root = memory_root or Path(settings.memory_root)
    path = _metadata_file(memory_root, device_slug, repair_id)
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("status") == status:
            return
        payload["status"] = status
        payload["status_updated_at"] = datetime.now(UTC).isoformat()
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "[ChatHistory] touch_status failed for repair=%s: %s", repair_id, exc
        )


# ------------ Conversations (multi-thread per repair) ------------
# A repair holds N conversations under `conversations/{conv_id}/`, each with
# its own messages.jsonl and optional MA session pointer. An ordered index
# at `conversations/index.json` lists them chronologically with metadata
# for the UI popover.


def list_conversations(
    *,
    device_slug: str,
    repair_id: str,
    memory_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Return the ordered list of conversations for a repair (oldest first)."""
    root = memory_root or Path(get_settings().memory_root)
    return _read_index(root, device_slug, repair_id)


def _create_index_entry(
    *,
    root: Path,
    device_slug: str,
    repair_id: str,
    conv_id: str,
    tier: str,
) -> bool:
    """Append a fresh entry for `conv_id` to index.json, closing the previous
    open one. Idempotent — returns False (and writes nothing) when an entry
    with `conv_id` is already in the index. Returns True when a new entry
    was actually created.

    Shared by `create_conversation` (which auto-generates the id) and
    `materialize_conversation` (which persists a pre-allocated pending id
    that was returned by `ensure_conversation(materialize=False)`).
    """
    index = _read_index(root, device_slug, repair_id)
    if any(entry.get("id") == conv_id for entry in index):
        return False
    for entry in index:
        if not entry.get("closed"):
            entry["closed"] = True
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
    _conv_dir(root, device_slug, repair_id, conv_id).mkdir(
        parents=True, exist_ok=True
    )
    _write_index(root, device_slug, repair_id, index)
    # One-shot migration of the legacy per-repair `ma_sessions` dict into
    # the first conversation's per-tier files. Pre-T1, MA session ids were
    # stored under `memory/{slug}/repairs/{id}.json::ma_sessions[{tier}]`;
    # after the refactor they live at `conversations/{conv}/ma_session_{tier}.json`.
    # Without this hop, the first conv created on a legacy repair loses the
    # real agent memory and the tech sees the model start from scratch.
    if len(index) == 1:
        _seed_legacy_ma_sessions(
            root=root, device_slug=device_slug, repair_id=repair_id,
            conv_id=conv_id,
        )
    return True


def create_conversation(
    *,
    device_slug: str,
    repair_id: str,
    tier: str,
    memory_root: Path | None = None,
) -> str:
    """Create a fresh conversation, close the previous active one, return its id."""
    root = memory_root or Path(get_settings().memory_root)
    conv_id = secrets.token_hex(4)  # 8 hex chars
    _create_index_entry(
        root=root, device_slug=device_slug, repair_id=repair_id,
        conv_id=conv_id, tier=tier,
    )
    return conv_id


def delete_conversation(
    *,
    device_slug: str,
    repair_id: str,
    conv_id: str,
    memory_root: Path | None = None,
) -> bool:
    """Remove a single conversation from disk: drop its index entry and wipe
    its `conversations/{conv_id}/` directory (messages.jsonl + per-tier MA
    session pointers + any artefacts the runtime persisted there).

    Returns True when something was actually removed (entry or directory),
    False when the conv was already absent. The repair itself, its metadata
    file, and other conversations are untouched.

    The per-tier MA session ids stored under the conv directory are dropped
    along with it; the upstream Anthropic sessions are left to expire on
    their own (the repair-scoped memory store is shared across convs and
    must outlive any single deletion).
    """
    import shutil

    root = memory_root or Path(get_settings().memory_root)
    index = _read_index(root, device_slug, repair_id)
    new_index = [entry for entry in index if entry.get("id") != conv_id]
    index_changed = len(new_index) != len(index)
    if index_changed:
        _write_index(root, device_slug, repair_id, new_index)

    conv_dir = _conv_dir(root, device_slug, repair_id, conv_id)
    dir_removed = False
    if conv_dir.exists() and conv_dir.is_dir():
        try:
            shutil.rmtree(conv_dir)
            dir_removed = True
        except OSError as exc:
            logger.error(
                "[ChatHistory] delete_conversation: rmtree failed for %s: %s",
                conv_dir, exc,
            )
            raise

    return index_changed or dir_removed


def get_conversation_tier(
    *,
    device_slug: str,
    repair_id: str,
    conv_id: str,
    memory_root: Path | None = None,
) -> str | None:
    """Return the tier the conv was originally opened with, per the index
    entry. Used by the runtime to auto-align the WS tier with the conv on
    default landings (no explicit `?tier=` from the tech) so a Sonnet
    thread isn't silently resumed as Haiku because of the URL default.
    Returns None when the conv isn't in the index (pending or unknown).
    """
    root = memory_root or Path(get_settings().memory_root)
    index = _read_index(root, device_slug, repair_id)
    for entry in index:
        if entry.get("id") == conv_id:
            tier = entry.get("tier")
            return tier if isinstance(tier, str) else None
    return None


def materialize_conversation(
    *,
    device_slug: str,
    repair_id: str,
    conv_id: str,
    tier: str,
    memory_root: Path | None = None,
) -> bool:
    """Persist a previously-pending `conv_id` to disk (index entry + dir).

    The companion to `ensure_conversation(materialize=False)`: at WS open we
    pre-allocate an id but skip the disk write so the index doesn't pile up
    with 0-turn conversations from sessions the tech opens and never sends
    a message in. This call materializes the slot when real content is
    about to land. Idempotent — returns False if `conv_id` is already in
    the index, True when a new entry was actually appended.
    """
    root = memory_root or Path(get_settings().memory_root)
    return _create_index_entry(
        root=root, device_slug=device_slug, repair_id=repair_id,
        conv_id=conv_id, tier=tier,
    )


def _seed_legacy_ma_sessions(
    *, root: Path, device_slug: str, repair_id: str, conv_id: str
) -> None:
    """Copy `ma_sessions` dict from repair metadata into per-tier files.

    Idempotent: if a `ma_session_{tier}.json` already exists for this conv,
    it wins (never overwrite a deliberately-saved id). Swallows all errors —
    this is best-effort backfill, not a hard precondition.
    """
    meta_path = _metadata_file(root, device_slug, repair_id)
    if not meta_path.exists():
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    legacy = meta.get("ma_sessions") or {}
    if not isinstance(legacy, dict):
        return
    linked_at = meta.get("ma_session_linked_at") or datetime.now(UTC).isoformat()
    for tier, session_id in legacy.items():
        if not (isinstance(tier, str) and isinstance(session_id, str) and session_id):
            continue
        path = _ma_session_file(root, device_slug, repair_id, conv_id, tier)
        if path.exists():
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"session_id": session_id, "tier": tier, "linked_at": linked_at},
                    indent=2, ensure_ascii=False,
                ) + "\n",
                encoding="utf-8",
            )
            logger.info(
                "[ChatHistory] seeded legacy MA session for repair=%s conv=%s tier=%s",
                repair_id, conv_id, tier,
            )
        except OSError as exc:
            logger.warning(
                "[ChatHistory] _seed_legacy_ma_sessions failed for repair=%s tier=%s: %s",
                repair_id, tier, exc,
            )


def ensure_conversation(
    *,
    device_slug: str,
    repair_id: str,
    conv_id: str | None,
    tier: str,
    memory_root: Path | None = None,
    materialize: bool = True,
) -> tuple[str, bool]:
    """Resolve a conv_id to the right target, creating / migrating when needed.

    Semantics:
      - `conv_id is None` → active (most recent). If none exist, migrate
        from legacy messages.jsonl if present, else create a fresh one.
      - `conv_id == "new"` → always create a fresh conversation.
      - `conv_id` matches an existing entry → pass through untouched.
      - Unknown `conv_id` → raise KeyError.

    Returns `(resolved_id, created)` — `created` is True when this call
    created (or pre-allocated, see below) a conversation, including the
    legacy migration.

    `materialize` (default True): when False, the create path returns a
    freshly-generated id WITHOUT writing the index entry or making the conv
    directory. The caller is then responsible for calling
    `materialize_conversation` once real content is about to land — typically
    on the first user message. This avoids spawning 0-turn entries when a
    technician opens the diagnostic panel without sending anything. Has no
    effect on the resolve-existing path (no write happens there anyway).
    Migration of legacy `messages.jsonl` always materializes — moving a
    file off disk is the whole point of that path.
    """
    root = memory_root or Path(get_settings().memory_root)
    if conv_id == "new":
        if materialize:
            return (
                create_conversation(
                    device_slug=device_slug,
                    repair_id=repair_id,
                    tier=tier,
                    memory_root=root,
                ),
                True,
            )
        return secrets.token_hex(4), True

    index = _read_index(root, device_slug, repair_id)

    if conv_id is None:
        if index:
            # Active = the conv the technician most recently *touched*. The
            # naive "last in index" pick was wrong: index order is
            # `started_at` ascending, so a conv started 5 minutes ago that
            # got 0 turns trumps a conv started 10 minutes ago that's still
            # accumulating turns right now. Tier switches and reopens
            # particularly hit this — they create a fresh conv even if the
            # tech then keeps working in the previous one. Sort by
            # `last_turn_at` (with `started_at` as tie-breaker for never-
            # touched entries) so the default landing always lands on the
            # thread the tech actually has live activity in.
            def _recency_key(entry: dict[str, Any]) -> str:
                return entry.get("last_turn_at") or entry.get("started_at") or ""
            return max(index, key=_recency_key)["id"], False
        # No index yet — migrate legacy if present, else create fresh.
        legacy = _legacy_history_file(root, device_slug, repair_id)
        if legacy.exists():
            return (
                _migrate_legacy(
                    root=root,
                    device_slug=device_slug,
                    repair_id=repair_id,
                    tier=tier,
                ),
                True,
            )
        if materialize:
            return (
                create_conversation(
                    device_slug=device_slug,
                    repair_id=repair_id,
                    tier=tier,
                    memory_root=root,
                ),
                True,
            )
        return secrets.token_hex(4), True

    # Explicit id — must exist.
    if not any(entry["id"] == conv_id for entry in index):
        raise KeyError(
            f"unknown conversation {conv_id!r} for repair {repair_id!r}"
        )
    return conv_id, False


def _migrate_legacy(
    *, root: Path, device_slug: str, repair_id: str, tier: str
) -> str:
    """Move repair-root messages.jsonl into a new conversation."""
    legacy = _legacy_history_file(root, device_slug, repair_id)
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
    index: list[dict[str, Any]] = [
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
    _seed_legacy_ma_sessions(
        root=root, device_slug=device_slug, repair_id=repair_id, conv_id=conv_id
    )
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
    root = memory_root or Path(get_settings().memory_root)
    index = _read_index(root, device_slug, repair_id)
    updated = False
    for entry in index:
        if entry["id"] != conv_id:
            continue
        if first_message and not entry.get("title"):
            entry["title"] = (
                first_message[:80].replace("\n", " ").strip() or None
            )
        if cost_usd is not None:
            entry["cost_usd"] = round(
                (entry.get("cost_usd") or 0.0) + cost_usd, 6
            )
            entry["turns"] = (entry.get("turns") or 0) + 1
            entry["last_turn_at"] = (
                datetime.now(UTC).isoformat().replace("+00:00", "Z")
            )
        if model and not entry.get("model"):
            entry["model"] = model
        updated = True
        break
    if updated:
        _write_index(root, device_slug, repair_id, index)


