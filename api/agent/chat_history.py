# SPDX-License-Identifier: Apache-2.0
"""Per-repair chat history persistence for diagnostic sessions.

Each repair_id owns a JSONL file at
`memory/{device_slug}/repairs/{repair_id}/messages.jsonl`. Every line is one
`{ts, event}` record where `event` is the Anthropic Messages API shape that
gets passed verbatim back to `client.messages.create` on resume.

Backend is feature-flagged (`chat_history_backend` in settings):

- **jsonl (default)** — append-only local file. Works today without any
  Anthropic feature gate, survives restarts, is grep-able / git-diffable
  for debugging.
- **managed_agents (future)** — when the MA sessions Research Preview lands,
  each repair_id will map to a persistent MA session_id; replay will be
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api.config import get_settings

logger = logging.getLogger("microsolder.agent.chat_history")


def _repair_dir(memory_root: Path, device_slug: str, repair_id: str) -> Path:
    return memory_root / device_slug / "repairs" / repair_id


def _history_file(memory_root: Path, device_slug: str, repair_id: str) -> Path:
    return _repair_dir(memory_root, device_slug, repair_id) / "messages.jsonl"


def _metadata_file(memory_root: Path, device_slug: str, repair_id: str) -> Path:
    # The pre-existing metadata lives one level up: memory/{slug}/repairs/{id}.json
    return memory_root / device_slug / "repairs" / f"{repair_id}.json"


def append_event(
    *,
    device_slug: str,
    repair_id: str | None,
    event: dict[str, Any],
    memory_root: Path | None = None,
) -> None:
    """Append one Anthropic-format message event to the session's JSONL.

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
    path = _history_file(memory_root, device_slug, repair_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        logger.warning(
            "[ChatHistory] append_event failed for repair=%s: %s", repair_id, exc
        )


def load_events(
    *,
    device_slug: str,
    repair_id: str | None,
    memory_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Return the list of Anthropic-format events, in write order."""
    if not repair_id:
        return []
    settings = get_settings()
    if settings.chat_history_backend != "jsonl":
        return []
    memory_root = memory_root or Path(settings.memory_root)
    path = _history_file(memory_root, device_slug, repair_id)
    if not path.exists():
        return []

    events: list[dict[str, Any]] = []
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
                if isinstance(event, dict):
                    events.append(event)
    except OSError as exc:
        logger.warning("[ChatHistory] load_events failed for repair=%s: %s", repair_id, exc)
    return events


def save_ma_session_id(
    *,
    device_slug: str,
    repair_id: str | None,
    session_id: str,
    memory_root: Path | None = None,
) -> None:
    """Persist the MA session_id on the repair's metadata so we can resume it
    on the next WS open — otherwise every reopen creates a fresh MA session
    and the conversation context is lost. Silent no-op on any error.
    """
    if not repair_id or not session_id:
        return
    settings = get_settings()
    memory_root = memory_root or Path(settings.memory_root)
    path = _metadata_file(memory_root, device_slug, repair_id)
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("ma_session_id") == session_id:
            return
        payload["ma_session_id"] = session_id
        payload["ma_session_linked_at"] = datetime.now(UTC).isoformat()
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "[ChatHistory] save_ma_session_id failed for repair=%s: %s",
            repair_id,
            exc,
        )


def load_ma_session_id(
    *,
    device_slug: str,
    repair_id: str | None,
    memory_root: Path | None = None,
) -> str | None:
    """Return the persisted MA session_id for a repair, or None."""
    meta = load_repair_metadata(
        device_slug=device_slug, repair_id=repair_id, memory_root=memory_root
    )
    if not meta:
        return None
    sid = meta.get("ma_session_id")
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
    agent can immediately query `mb_list_findings` / `mb_get_rules_for_symptoms`
    without asking "which device are you on?". Returns None when there's
    nothing to tell (no repair_id given).
    """
    if not repair_id:
        return None
    meta = load_repair_metadata(
        device_slug=device_slug, repair_id=repair_id, memory_root=memory_root
    )
    if not meta:
        # Still worth surfacing the device slug even if the repair file is gone.
        return f"[Nouvelle session · device_slug: {device_slug}]"
    label = meta.get("device_label") or device_slug
    symptom = (meta.get("symptom") or "").strip()
    lines = [
        "[Nouvelle session de diagnostic]",
        f"Device: {label} (slug: {device_slug})",
    ]
    if symptom:
        lines.append(f"Symptôme signalé par le technicien: {symptom}")
    lines.append(
        "Commence par mb_list_findings pour voir les réparations passées, "
        "puis mb_get_rules_for_symptoms pour les règles applicables."
    )
    return "\n".join(lines)


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
