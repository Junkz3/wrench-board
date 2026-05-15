"""Shared utilities for the diagnostic-runtime sub-modules.

Pure helpers + tiny module-level state. Kept dependency-free against the
sibling runtime modules so any of them can import from here without
risking a cycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Literal

from anthropic import AsyncAnthropic

from api.agent.chat_history import (
    append_event,
    materialize_conversation,
    save_ma_session_id,
)

TierLiteral = Literal["fast", "normal", "deep"]
DEFAULT_TIER: TierLiteral = "fast"

logger = logging.getLogger("wrench_board.agent.managed")


# Hard cap on macro upload size (post-base64-decode). 5 MB is plenty for a
# JPEG of a board macro at sane resolutions ; bigger payloads waste WS
# bandwidth and Anthropic Files API quota.
_MAX_MACRO_BYTES = 5 * 1024 * 1024


def _safe_tool_result_text(result: dict[str, Any]) -> str:
    """Serialize a tool result for the MA `user.custom_tool_result` event.

    Plain `json.dumps(result, default=str)` silently coerces any object
    via `str()` — a Path becomes a string OK, but a custom object's
    `__str__` may render as `<Foo at 0x7f…>` which the agent then sees
    as opaque garbage in its tool-result history. Worse, an object
    holding a file descriptor or socket can crash inside `str()`.

    Try `json.dumps` without the default first (catches non-JSON-clean
    results immediately), fall back to `default=str` only when the
    type system genuinely tolerates lossy stringification (Path,
    datetime, UUID — common Pydantic round-trip targets). On total
    failure return a structured error so the agent at least sees
    "the tool result couldn't be serialized" instead of nothing.
    """
    try:
        return json.dumps(result)
    except TypeError:
        pass
    try:
        return json.dumps(result, default=str)
    except Exception as exc:  # noqa: BLE001 — last-resort safety net
        # An object that raises out of `__str__` (or a json encoder bug)
        # must NOT take the runtime down. Best we can do is tell the
        # agent the result couldn't be serialized so it picks a
        # different code path instead of waiting on a tool result that
        # never arrives.
        logger.error(
            "[Diag-MA] tool result serialization failed: %s — type=%s",
            exc,
            type(result).__name__,
        )
        return json.dumps({
            "ok": False,
            "reason": "serialization_failed",
            "error": str(exc)[:200] if exc.args else type(exc).__name__,
        })


def _build_log_id(
    repair_id: str | None,
    conv_id: str | None,
    tier: str,
) -> str:
    """Compact correlation id for logs: `repair:conv:tier`.

    Trace a single session across thousands of log lines without grepping
    on the bare `session_id` (which is the same across resumes — and
    multiple conv_ids can resume on the same session). The order is
    repair → conv → tier so partial matches narrow naturally:
    `grep "rep_001:" logs/`  → all activity on a repair across convs.
    """
    return f"{repair_id or 'anon'}:{conv_id or 'new'}:{tier}"


# Process-local guard: at most one diagnostic WS per
# (device_slug, repair_id, conv_id) triplet at a time. The audit-revealed
# bug — `responded_tool_ids` lives inside `_forward_session_to_ws` and is
# NOT shared across sibling forwarders — would otherwise let two browser
# tabs on the same conv each dispatch the same `agent.custom_tool_use`,
# both POST `user.custom_tool_result`, and the second POST returns HTTP
# 400 ("waiting on responses to events …") which tears down the stream.
# Server-side rejection at WS-open is the simplest fix that doesn't
# require shared mutable state across forwarders. Anonymous WS (no
# repair_id, no conv_id) skip the guard since they can't collide.
# asyncio is single-threaded so set membership + add happens atomically
# between awaits — no lock needed.
_active_diagnostic_keys: set[tuple[str, str, str]] = set()

# How long a contending WS waits for a sibling's teardown to release its
# guard claim before bouncing the user with `session_already_open`. The
# common contention path is the frontend's close+immediate-reconnect
# (tier auto-align on session_ready, page reload mid-teardown), which
# resolves in <100 ms; 5 s comfortably absorbs that without making a
# truly concurrent double-open (two tabs) hang an unreasonable amount.
# Exposed as a module-level constant so tests can monkeypatch it to a
# small value and avoid sleeping the real budget.
_GUARD_ACQUIRE_TIMEOUT_SECONDS: float = 5.0


async def _sessions_create_with_retry(
    client: AsyncAnthropic,
    *,
    max_attempts: int = 4,
    base_delay: float = 1.0,
    **session_kwargs,
):
    """Create an MA session with exponential backoff on 429 / 5xx.

    MA's create endpoints are quota-limited at 300 req/min per org. A burst
    of fresh sessions (multiple techs opening WS) can trip it; the SDK does
    not auto-retry for us on `client.beta.sessions.create`.
    """
    from anthropic import APIStatusError, RateLimitError

    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await client.beta.sessions.create(**session_kwargs)
        except RateLimitError as exc:
            last_exc = exc
            retry_after = 0.0
            try:
                hdr = exc.response.headers.get("retry-after")
                if hdr:
                    retry_after = float(hdr)
            except Exception:  # noqa: BLE001
                retry_after = 0.0
            delay = max(retry_after, base_delay * (2**attempt))
        except APIStatusError as exc:
            if getattr(exc, "status_code", None) and exc.status_code >= 500:
                last_exc = exc
                delay = base_delay * (2**attempt)
            else:
                raise
        if attempt + 1 < max_attempts:
            logger.warning(
                "[Diag-MA] sessions.create attempt=%d failed (%s) — retrying in %.1fs",
                attempt + 1,
                last_exc,
                delay,
            )
            await asyncio.sleep(delay)
    assert last_exc is not None  # for type checker
    raise last_exc


# NOTE: ``_SessionMirrors`` (the fire-and-forget task tracker used by
# every mb_validate_finding mirror, the cam_capture round-trip and the
# auto-seed re-upload) lives in :mod:`api.agent._session_mirrors` so the
# tool-dispatch table can reference it without importing this runtime
# module (avoiding a cycle). The alias at the top of this file
# (``from api.agent._session_mirrors import SessionMirrors as
# _SessionMirrors``) preserves the legacy ``rm._SessionMirrors`` import
# path used by the test suite — see the wiring tests in
# ``tests/agent/test_runtime_managed_async_wiring.py``.


def _mirror_jsonl(
    *,
    device_slug: str | None,
    repair_id: str | None,
    conv_id: str | None,
    memory_root: Path | None,
    event: dict[str, Any],
) -> None:
    """Best-effort mirror of one Anthropic-shaped event to the conv's
    `messages.jsonl`. The managed runtime historically relied on MA's
    server-side event store as its only source of truth for transcripts,
    but MA can archive sessions out from under us (beta TTL is undocumented
    and shorter than the ~30 d the docs imply — observed real loss of a
    31-turn diagnostic conv on 2026-04-26 where `events.list` returned
    empty).
    Mirroring every user message + agent text + tool_use to disk gives
    `_replay_ma_history_to_ws` (UI re-rendering on reconnect) something
    to fall back on. Anonymous (no repair_id) and pending convs skip
    silently — no destination yet.
    """
    if not repair_id or not conv_id or not device_slug:
        return
    try:
        append_event(
            device_slug=device_slug,
            repair_id=repair_id,
            conv_id=conv_id,
            event=event,
            memory_root=memory_root,
        )
    except Exception as exc:  # noqa: BLE001 — never block the WS on a mirror write
        logger.warning(
            "[Diag-MA] _mirror_jsonl failed for repair=%s conv=%s: %s",
            repair_id, conv_id, exc,
        )


class _PendingConv:
    """Lazy-materialization handle for a conversation that doesn't exist on
    disk yet. Created at WS-open via `ensure_conversation(materialize=False)`
    so the index doesn't accumulate 0-turn entries from sessions the tech
    opens and never sends a message in. The first `materialize_now()` call
    writes the index entry, the conv directory, and (if applicable) saves
    the MA session id linking this conv to the freshly-created MA session.
    Idempotent.
    """

    def __init__(
        self,
        *,
        device_slug: str,
        repair_id: str | None,
        conv_id: str | None,
        tier: str,
        memory_root: Path,
        session_id: str | None,
        pending: bool,
    ) -> None:
        self.device_slug = device_slug
        self.repair_id = repair_id
        self.conv_id = conv_id
        self.tier = tier
        self.memory_root = memory_root
        self.session_id = session_id
        self._pending = pending

    @property
    def is_pending(self) -> bool:
        return self._pending

    def materialize_now(self) -> None:
        if not self._pending or not self.conv_id or not self.repair_id:
            return
        materialize_conversation(
            device_slug=self.device_slug,
            repair_id=self.repair_id,
            conv_id=self.conv_id,
            tier=self.tier,
            memory_root=self.memory_root,
        )
        if self.session_id:
            save_ma_session_id(
                device_slug=self.device_slug,
                repair_id=self.repair_id,
                conv_id=self.conv_id,
                session_id=self.session_id,
                tier=self.tier,
                memory_root=self.memory_root,
            )
        self._pending = False


# NOTE: the legacy `_summarize_prior_history_for_resume` function was removed
# when the layered MA memory architecture landed (2026-04-26). With the
# per-repair RW scribe mount (memory/repair-{repair_id}), the agent
# self-orients on resume by reading state.md / decisions/*.md / etc. The
# pre-session Haiku call that pre-cuisined a recovery summary is no longer
# needed — it cost a round-trip + tokens for context the agent now fetches
# on-demand from the mount.
#
# `_replay_ma_history_to_ws` stays (still needed for the FRONTEND to
# re-render past chat bubbles when the WS reconnects).
