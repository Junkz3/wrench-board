"""Chat-history replay helpers.

When a WS reconnects to an existing repair conversation, the chat panel
needs the past turns to re-render. We try MA's server-side event store
first (rich, includes per-turn cost) and fall back to the local JSONL
mirror when MA's events.list returns empty — common after a session
checkpoint expiry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic
from fastapi import WebSocket

from api.agent.chat_history import (
    load_events,
    strip_ctx_tag,
)
from api.agent.pricing import compute_turn_cost
from api.agent.runtime._aux import logger
from api.agent.sanitize import sanitize_agent_text
from api.session.state import SessionState


async def _replay_jsonl_history_to_ws(
    ws: WebSocket,
    *,
    device_slug: str,
    repair_id: str | None,
    conv_id: str | None,
    memory_root: Path | None,
    session_state: SessionState,
) -> bool:
    """Replay the conv's local `messages.jsonl` to the WS chat panel.

    Used as a fallback when `_replay_ma_history_to_ws` finds the MA session
    archived (events.list empty) but we mirrored the transcript locally.
    Returns True if anything was emitted, False when JSONL was empty too.
    """
    if not repair_id or not conv_id:
        return False
    events = load_events(
        device_slug=device_slug,
        repair_id=repair_id,
        conv_id=conv_id,
        memory_root=memory_root,
    )
    if not events:
        return False
    await ws.send_json({"type": "history_replay_start", "count": len(events)})
    for ev in events:
        role = ev.get("role")
        content = ev.get("content")
        if role == "user":
            text: str | None = None
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text") or ""
                        break
            if not text:
                continue
            text = strip_ctx_tag(text)
            if text.startswith(
                (
                    "[New diagnostic session]",
                    "[TECHNICIAN CONTEXT]",
                    "[CONVERSATION RESUMED",
                    # Keep the legacy French markers so JSONL files written
                    # before the system-prompt translation still get stripped
                    # cleanly on replay.
                    "[Nouvelle session de diagnostic]",
                    "[CONTEXTE TECHNICIEN]",
                    "[REPRISE DE CONVERSATION",
                )
            ):
                marker = "\n\n---\n\n"
                idx = text.rfind(marker)
                if idx >= 0:
                    text = text[idx + len(marker):].strip()
                else:
                    continue
            if not text:
                continue
            await ws.send_json(
                {"type": "message", "role": "user", "text": text, "replay": True}
            )
        elif role == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text") or ""
                    if not text:
                        continue
                    clean, _ = sanitize_agent_text(text, session_state.board)
                    await ws.send_json(
                        {
                            "type": "message",
                            "role": "assistant",
                            "text": clean,
                            "replay": True,
                        }
                    )
                elif btype == "tool_use":
                    await ws.send_json(
                        {
                            "type": "tool_use",
                            "name": block.get("name"),
                            "input": block.get("input") or {},
                            "replay": True,
                        }
                    )
    await ws.send_json({"type": "history_replay_end"})
    return True


async def _replay_ma_history_to_ws(
    ws: WebSocket,
    client: AsyncAnthropic,
    session_id: str,
    session_state: SessionState,
    agent_model: str,
    *,
    device_slug: str | None = None,
    repair_id: str | None = None,
    conv_id: str | None = None,
    memory_root: Path | None = None,
) -> bool:
    """Replay a MA session's past events to the browser chat panel.

    The SDK exposes events via `client.beta.sessions.events.list(session_id)`.
    We iterate chronologically and surface only the subset the chat UI
    renders: user text, agent text, agent custom_tool_use. The session
    intro prefix (the hidden "[New diagnostic session] …" glued to
    the first real user message) is stripped so the tech sees only what
    they themselves typed.

    Returns True when something was emitted (either from MA or from the
    JSONL fallback). Returns False when both sources were empty — the
    caller can then warn the tech that the agent's internal context was
    likely lost too. Swallows any error.
    """
    async def _try_jsonl_fallback(reason: str) -> bool:
        if device_slug is None:
            return False
        used = await _replay_jsonl_history_to_ws(
            ws,
            device_slug=device_slug,
            repair_id=repair_id,
            conv_id=conv_id,
            memory_root=memory_root,
            session_state=session_state,
        )
        if used:
            logger.info(
                "[Diag-MA] %s — replayed from local JSONL instead "
                "(repair=%s conv=%s)",
                reason, repair_id, conv_id,
            )
        return used

    try:
        events_iter = client.beta.sessions.events.list(session_id)
    except AttributeError:
        logger.warning("[Diag-MA] SDK has no beta.sessions.events.list — skipping replay")
        return await _try_jsonl_fallback(f"events.list unavailable for {session_id}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Diag-MA] events.list failed for %s: %s", session_id, exc)
        return await _try_jsonl_fallback(f"events.list failed for {session_id}")

    collected: list[Any] = []
    try:
        # The SDK returns either an async iterator or a paginator; accept both.
        if hasattr(events_iter, "__aiter__"):
            async for ev in events_iter:
                collected.append(ev)
        else:
            # Awaitable returning a list-like page.
            page = await events_iter  # type: ignore[misc]
            data = getattr(page, "data", None) or list(page)
            collected.extend(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Diag-MA] events.list iterate failed: %s", exc)
        return await _try_jsonl_fallback(f"events.list iterate failed for {session_id}")

    if not collected:
        # MA archived/expired the session — happens silently in the beta.
        # Without this fallback the chat panel was empty even though we
        # have the full transcript on disk (post-mirror). With it, the tech
        # sees their conversation again the next time they open it.
        return await _try_jsonl_fallback(f"events.list empty for {session_id}")

    # Pre-count events that have a chance to render visibly. MA can return
    # turn-skeleton events (agent.thinking, span.model_request_end,
    # session.status_idle) without any user/agent.message survival — the
    # banner used to lie ("replay · 3 events" then nothing). Counting
    # candidates first matches the banner to what the chat will actually
    # show. Pure-intro user messages still get filtered later in the for
    # loop (their content depends on the marker layout); any drop there
    # is caught by `emitted_visible` below so the caller can flag a
    # context-loss when the banner promised content the chat couldn't render.
    renderable_types = {"user.message", "agent.message", "agent.custom_tool_use"}
    renderable_count = sum(
        1 for e in collected if getattr(e, "type", None) in renderable_types
    )
    if renderable_count == 0:
        # Only metadata events survived (cost, thinking, idle markers).
        # Try JSONL — if it has the real transcript we'll replay from there.
        return await _try_jsonl_fallback(
            f"events.list yielded only metadata for {session_id}"
        )

    await ws.send_json({"type": "history_replay_start", "count": renderable_count})

    emitted_visible = 0
    for event in collected:
        etype = getattr(event, "type", None)
        if etype == "user.message":
            content = getattr(event, "content", None) or []
            for block in content:
                if getattr(block, "type", None) != "text":
                    continue
                text = getattr(block, "text", "") or ""
                # Drop the per-turn ctx tag (prepended to every user message
                # so Haiku never loses device + symptom) and the bootstrap
                # intro prefix (only on the very first real user message,
                # carries the device context + technician profile blocks
                # separated by "---" markers).
                text = strip_ctx_tag(text)
                if text.startswith(
                    (
                        "[New diagnostic session]",
                        "[TECHNICIAN CONTEXT]",
                        "[CONVERSATION RESUMED",
                        # Legacy French markers — kept so MA event streams
                        # produced before the system-prompt translation
                        # still get stripped cleanly on replay.
                        "[Nouvelle session de diagnostic]",
                        "[CONTEXTE TECHNICIEN]",
                        "[REPRISE DE CONVERSATION",
                    )
                ):
                    marker = "\n\n---\n\n"
                    idx = text.rfind(marker)
                    if idx >= 0:
                        text = text[idx + len(marker) :].strip()
                    else:
                        continue  # pure intro with no follow-up — hide
                if not text:
                    continue
                await ws.send_json(
                    {"type": "message", "role": "user", "text": text, "replay": True}
                )
                emitted_visible += 1

        elif etype == "agent.message":
            content = getattr(event, "content", None) or []
            for block in content:
                if getattr(block, "type", None) == "text":
                    text = getattr(block, "text", "") or ""
                    if not text:
                        continue
                    clean, _ = sanitize_agent_text(text, session_state.board)
                    await ws.send_json(
                        {
                            "type": "message",
                            "role": "assistant",
                            "text": clean,
                            "replay": True,
                        }
                    )
                    emitted_visible += 1

        elif etype == "agent.custom_tool_use":
            await ws.send_json(
                {
                    "type": "tool_use",
                    "name": getattr(event, "name", None),
                    "input": getattr(event, "input", {}) or {},
                    "replay": True,
                }
            )
            emitted_visible += 1

        elif etype == "span.model_request_end":
            # Reprice the turn from MA's persisted usage so the lifetime
            # cost chip reflects real spend rather than starting from $0.
            usage = getattr(event, "model_usage", None)
            if usage is not None:
                model_label = (
                    getattr(usage, "model", None) or getattr(event, "model", None) or agent_model
                )
                cost = compute_turn_cost(
                    model_label,
                    input_tokens=getattr(usage, "input_tokens", 0) or 0,
                    output_tokens=getattr(usage, "output_tokens", 0) or 0,
                    cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                    cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0)
                    or 0,
                )
                await ws.send_json({"type": "turn_cost", **cost, "replay": True})

    await ws.send_json({"type": "history_replay_end"})
    if emitted_visible == 0:
        # Banner promised renderable events but every one of them turned
        # out to be the auto-injected device intro (no real exchange ever
        # happened on this MA session). Treat as no real replay so the
        # caller can flag context_lost — the chat panel showing only the
        # banner row would otherwise look like the agent silently lost
        # the conversation while pretending nothing happened.
        logger.info(
            "[Diag-MA] replay rendered 0 visible events out of %d renderable "
            "(all intro-only) for session=%s — flagging as empty",
            renderable_count, session_id,
        )
        return False
    return True
