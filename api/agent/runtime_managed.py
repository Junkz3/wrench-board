# SPDX-License-Identifier: Apache-2.0
"""Diagnostic runtime using Anthropic Managed Agents.

Wire flow:
    browser ⇄ /ws/diagnostic/{slug} ⇄ backend ⇄ MA session event stream

Key SDK contract (see `docs/en/managed-agents/events-and-streaming`):
  - Open the stream **before** sending the first `user.message`, else we
    race against events the server has already emitted.
  - Custom tool handling is two-step. The agent first emits an
    `agent.custom_tool_use` event with full `{id, name, input}`; then the
    session pauses with `session.status_idle` + `stop_reason =
    requires_action`, whose `event_ids` point at the pending tool uses.
    We cache the tool_use events as they arrive so we can look them up
    when `requires_action` fires and send back `user.custom_tool_result`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Literal

from anthropic import AsyncAnthropic
from fastapi import WebSocket, WebSocketDisconnect

from api.agent.chat_history import (
    build_session_intro,
    load_ma_session_id,
    save_ma_session_id,
)
from api.agent.dispatch_bv import dispatch_bv
from api.agent.managed_ids import get_agent, load_managed_ids
from api.agent.memory_stores import ensure_memory_store
from api.agent.pricing import compute_turn_cost
from api.agent.sanitize import sanitize_agent_text
from api.agent.tools import (
    mb_expand_knowledge,
    mb_get_component,
    mb_get_rules_for_symptoms,
    mb_list_findings,
    mb_record_finding,
)
from api.config import get_settings
from api.session.state import SessionState
from api.tools.schematic import mb_schematic_graph

TierLiteral = Literal["fast", "normal", "deep"]
DEFAULT_TIER: TierLiteral = "fast"

logger = logging.getLogger("microsolder.agent.managed")


async def _dispatch_tool(
    name: str,
    payload: dict,
    device_slug: str,
    memory_root: Path,
    client: AsyncAnthropic,
    session: SessionState,
    session_id: str | None = None,
) -> dict:
    """Run a custom tool locally and return the raw result.

    Routes bv_* → dispatch_bv (synchronous), mb_* → their Python handlers.
    The returned dict may contain a Pydantic `event` field — the caller is
    responsible for emitting it on the WS and stripping it from the agent
    tool_result.
    """
    if name.startswith("bv_"):
        return dispatch_bv(session, name, payload)
    if name == "mb_get_component":
        return mb_get_component(
            device_slug=device_slug, refdes=payload.get("refdes", ""),
            memory_root=memory_root, session=session,
        )
    if name == "mb_get_rules_for_symptoms":
        return mb_get_rules_for_symptoms(
            device_slug=device_slug, symptoms=payload.get("symptoms", []),
            memory_root=memory_root, max_results=payload.get("max_results", 5),
        )
    if name == "mb_list_findings":
        return mb_list_findings(
            device_slug=device_slug, memory_root=memory_root,
            limit=payload.get("limit", 20),
            filter_refdes=payload.get("filter_refdes"),
        )
    if name == "mb_record_finding":
        return await mb_record_finding(
            client=client, device_slug=device_slug,
            refdes=payload.get("refdes", ""), symptom=payload.get("symptom", ""),
            confirmed_cause=payload.get("confirmed_cause", ""),
            memory_root=memory_root, mechanism=payload.get("mechanism"),
            notes=payload.get("notes"), session_id=session_id,
        )
    if name == "mb_schematic_graph":
        return mb_schematic_graph(
            device_slug=device_slug,
            memory_root=memory_root,
            query=payload.get("query", ""),
            label=payload.get("label"),
            refdes=payload.get("refdes"),
            index=payload.get("index"),
        )
    if name == "mb_expand_knowledge":
        return await mb_expand_knowledge(
            client=client, device_slug=device_slug,
            focus_symptoms=payload.get("focus_symptoms", []),
            focus_refdes=payload.get("focus_refdes", []),
            memory_root=memory_root,
        )
    logger.warning("unknown mb_* tool: %s", name)
    return {"ok": False, "reason": "unknown-tool", "error": f"unknown tool: {name}"}


async def run_diagnostic_session_managed(
    ws: WebSocket,
    device_slug: str,
    tier: TierLiteral = DEFAULT_TIER,
    repair_id: str | None = None,
) -> None:
    """Open a Managed Agents session on the tier-scoped agent and relay it to `ws`.

    `tier` picks which agent (fast=Haiku, normal=Sonnet, deep=Opus) handles the
    conversation. A new WS connection with a different tier = a fresh MA session
    on that tier's agent. No in-session swap: by design, tier choice is explicit
    and the user starts a new conversation when changing it.

    `repair_id` is accepted for signature parity with runtime_direct but MA
    sessions already persist conversation state natively server-side; when the
    chat_history_backend flag flips to 'managed_agents', we'll map repair_id
    to an MA session_id here and skip the JSONL layer entirely.
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        await ws.accept()
        await ws.send_json({"type": "error", "text": "ANTHROPIC_API_KEY not set"})
        await ws.close()
        return

    try:
        ids = load_managed_ids()
        agent_info = get_agent(ids, tier)
    except RuntimeError as exc:
        await ws.accept()
        await ws.send_json({"type": "error", "text": str(exc)})
        await ws.close()
        return

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    memory_root = Path(settings.memory_root)
    memory_store_id = await ensure_memory_store(client, device_slug)
    session_state = SessionState.from_device(device_slug)

    # Build session params. `resources` is the current (2026-04-01) surface
    # for attaching memory stores. If the beta isn't enabled, ensure_memory_store
    # returned None and we just skip the resources field.
    session_kwargs: dict[str, Any] = {
        "agent": {
            "type": "agent",
            "id": agent_info["id"],
            "version": agent_info["version"],
        },
        "environment_id": ids["environment_id"],
        "title": f"diag-{device_slug}-{tier}",
    }
    if memory_store_id:
        session_kwargs["resources"] = [
            {
                "type": "memory_store",
                "memory_store_id": memory_store_id,
                "access": "read_write",
                "prompt": (
                    "Repair history for this specific device. Check before "
                    "starting diagnosis, write durable learnings at the end."
                ),
            }
        ]

    # Reuse the repair's previously-persisted MA session when possible —
    # that's how conversation context survives a WS close/reopen. Sessions
    # are keyed BY TIER because each tier uses a different MA agent.
    reused_session_id = load_ma_session_id(
        device_slug=device_slug, repair_id=repair_id, tier=tier
    )
    session = None
    resumed = False
    if reused_session_id:
        try:
            session = await client.beta.sessions.retrieve(reused_session_id)
            resumed = True
            logger.info(
                "[Diag-MA] Resuming existing session=%s for repair=%s",
                reused_session_id, repair_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Diag-MA] could not resume session=%s (%s) — creating fresh",
                reused_session_id, exc,
            )
            session = None

    if session is None:
        try:
            session = await client.beta.sessions.create(**session_kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "[Diag-MA] session create failed for device=%s", device_slug
            )
            await ws.accept()
            await ws.send_json({"type": "error", "text": f"session create failed: {exc}"})
            await ws.close()
            return
        save_ma_session_id(
            device_slug=device_slug, repair_id=repair_id,
            session_id=session.id, tier=tier,
        )

    logger.info(
        "[Diag-MA] session=%s device=%s tier=%s model=%s memory=%s resumed=%s",
        session.id, device_slug, tier, agent_info["model"], memory_store_id, resumed,
    )

    await ws.accept()
    await ws.send_json(
        {
            "type": "session_ready",
            "mode": "managed",
            "session_id": session.id,
            "memory_store_id": memory_store_id,
            "device_slug": device_slug,
            "tier": tier,
            "model": agent_info["model"],
            "board_loaded": session_state.board is not None,
            "repair_id": repair_id,
        }
    )

    # The intro (device context + reported symptom) only needs injection on
    # a FRESH session. When we resume, the MA session already carries the
    # full conversation history including the original intro.
    intro = None if resumed else build_session_intro(
        device_slug=device_slug, repair_id=repair_id
    )
    if intro:
        await ws.send_json({
            "type": "context_loaded",
            "device_slug": device_slug,
            "repair_id": repair_id,
        })
        logger.info(
            "[Diag-MA] Stashed session intro for repair=%s (awaiting tech input)",
            repair_id,
        )
    if resumed:
        await ws.send_json({
            "type": "session_resumed",
            "session_id": session.id,
            "repair_id": repair_id,
        })
        # Replay the MA session's past events so the UI chat panel rebuilds
        # the conversation visually. Also replays per-turn costs from the
        # span.model_request_end events MA stores alongside so the lifetime
        # cost chip survives the reopen.
        await _replay_ma_history_to_ws(
            ws, client, session.id, session_state, agent_info["model"],
        )

    # Cache: agent.custom_tool_use events by event.id, so we can look up
    # name+input when `requires_action` arrives and only hands us event_ids.
    events_by_id: dict[str, Any] = {}

    try:
        recv_task = asyncio.create_task(
            _forward_ws_to_session(
                ws, client, session.id, pending_intro=intro, repair_id=repair_id,
                device_slug=device_slug,
            ),
            name="ws->session",
        )
        emit_task = asyncio.create_task(
            _forward_session_to_ws(
                ws, client, session.id, device_slug, memory_root, events_by_id,
                session_state, agent_info["model"],
            ),
            name="session->ws",
        )
        done, pending = await asyncio.wait(
            {recv_task, emit_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        # Surface exceptions from the completed task to the logger.
        for task in done:
            if task.exception() is not None:
                logger.exception(
                    "[Diag-MA] task %s raised", task.get_name(), exc_info=task.exception()
                )
    except WebSocketDisconnect:
        logger.info("[Diag-MA] WS disconnected for device=%s", device_slug)
    finally:
        # DO NOT archive: we want this session reusable on the next reopen
        # so the tech picks up the conversation where they left off. MA
        # keeps idle sessions alive (checkpoint TTL ~30 days per the beta
        # docs). We only interrupt in case the stream was mid-turn, so the
        # next connection doesn't inherit a stuck session_status_running.
        try:
            await client.beta.sessions.events.send(
                session.id,
                events=[{"type": "user.interrupt"}],
            )
        except Exception:  # noqa: BLE001 — best-effort
            pass


async def _replay_ma_history_to_ws(
    ws: WebSocket,
    client: AsyncAnthropic,
    session_id: str,
    session_state: SessionState,
    agent_model: str,
) -> None:
    """Replay a MA session's past events to the browser chat panel.

    The SDK exposes events via `client.beta.sessions.events.list(session_id)`.
    We iterate chronologically and surface only the subset the chat UI
    renders: user text, agent text, agent custom_tool_use. The session
    intro prefix (the hidden "[Nouvelle session de diagnostic] …" glued to
    the first real user message) is stripped so the tech sees only what
    they themselves typed.

    Swallows any error — the stream will still work even if replay fails.
    """
    try:
        events_iter = client.beta.sessions.events.list(session_id)
    except AttributeError:
        logger.warning(
            "[Diag-MA] SDK has no beta.sessions.events.list — skipping replay"
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Diag-MA] events.list failed for %s: %s", session_id, exc)
        return

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
        return

    if not collected:
        return

    await ws.send_json({"type": "history_replay_start", "count": len(collected)})

    for event in collected:
        etype = getattr(event, "type", None)
        if etype == "user.message":
            content = getattr(event, "content", None) or []
            for block in content:
                if getattr(block, "type", None) != "text":
                    continue
                text = getattr(block, "text", "") or ""
                # Drop the bootstrap intro prefix (we prepend it to the first
                # real user message in _forward_ws_to_session).
                if text.startswith("[Nouvelle session de diagnostic]"):
                    marker = "\n\n---\n\n"
                    idx = text.find(marker)
                    if idx >= 0:
                        text = text[idx + len(marker):].strip()
                    else:
                        continue  # pure intro with no follow-up — hide
                if not text:
                    continue
                await ws.send_json(
                    {"type": "message", "role": "user", "text": text, "replay": True}
                )

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

        elif etype == "agent.custom_tool_use":
            await ws.send_json(
                {
                    "type": "tool_use",
                    "name": getattr(event, "name", None),
                    "input": getattr(event, "input", {}) or {},
                    "replay": True,
                }
            )

        elif etype == "span.model_request_end":
            # Reprice the turn from MA's persisted usage so the lifetime
            # cost chip reflects real spend rather than starting from $0.
            usage = getattr(event, "model_usage", None)
            if usage is not None:
                model_label = (
                    getattr(usage, "model", None)
                    or getattr(event, "model", None)
                    or agent_model
                )
                cost = compute_turn_cost(
                    model_label,
                    input_tokens=getattr(usage, "input_tokens", 0) or 0,
                    output_tokens=getattr(usage, "output_tokens", 0) or 0,
                    cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                    cache_creation_input_tokens=getattr(
                        usage, "cache_creation_input_tokens", 0
                    ) or 0,
                )
                await ws.send_json({"type": "turn_cost", **cost, "replay": True})

    await ws.send_json({"type": "history_replay_end"})


async def _forward_ws_to_session(
    ws: WebSocket,
    client: AsyncAnthropic,
    session_id: str,
    *,
    pending_intro: str | None = None,
    repair_id: str | None = None,
    device_slug: str | None = None,
) -> None:
    """Read user text from the WS, post it as `user.message` to the session.

    When `pending_intro` is set, it is PREFIXED to the tech's very first
    message so the agent sees (device context + reported symptom) and the
    tech's actual question in a single turn — avoids the empty-ack turn
    that happens when context is sent in isolation.
    """
    intro_pending = pending_intro
    while True:
        raw = await ws.receive_text()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"text": raw}

        # Tech pressed Stop — forward as a user.interrupt MA event so the
        # agent halts any in-flight turn. Session stays alive; the tech can
        # keep typing afterwards.
        if payload.get("type") == "interrupt":
            try:
                await client.beta.sessions.events.send(
                    session_id,
                    events=[{"type": "user.interrupt"}],
                )
                logger.info("[Diag-MA] Forwarded user.interrupt for session=%s", session_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Diag-MA] interrupt failed: %s", exc)
            continue

        text = (payload.get("text") or "").strip()
        if not text:
            continue
        if intro_pending:
            text = intro_pending + "\n\n---\n\n" + text
            intro_pending = None
            if repair_id and device_slug:
                from api.agent.chat_history import touch_status

                touch_status(
                    device_slug=device_slug, repair_id=repair_id, status="in_progress"
                )
        await client.beta.sessions.events.send(
            session_id,
            events=[
                {
                    "type": "user.message",
                    "content": [{"type": "text", "text": text}],
                }
            ],
        )


async def _forward_session_to_ws(
    ws: WebSocket,
    client: AsyncAnthropic,
    session_id: str,
    device_slug: str,
    memory_root: Path,
    events_by_id: dict[str, Any],
    session_state: SessionState,
    agent_model: str,
) -> None:
    """Stream session events to the WS and dispatch custom tool calls.

    `agent_model` is the tier's configured model (claude-haiku-4-5 etc.),
    used as a fallback when MA's span.model_request_end doesn't carry a
    model name on its model_usage payload.
    """
    # AsyncAnthropic: `.stream(...)` returns a coroutine resolving to an
    # `AsyncStream[...]`. We must await first, then use it as an async
    # context manager — otherwise we get `TypeError: 'coroutine' object
    # does not support the asynchronous context manager protocol`.
    stream_ctx = await client.beta.sessions.events.stream(session_id)
    async with stream_ctx as stream:
        async for event in stream:
            etype = getattr(event, "type", None)

            if etype == "agent.message":
                for block in getattr(event, "content", None) or []:
                    if getattr(block, "type", None) == "text":
                        clean, unknown = sanitize_agent_text(
                            block.text, session_state.board
                        )
                        if unknown:
                            logger.warning(
                                "sanitizer wrapped unknown refdes: %s", unknown
                            )
                        await ws.send_json(
                            {"type": "message", "role": "assistant", "text": clean}
                        )

            elif etype == "agent.thinking":
                text = getattr(event, "text", "") or ""
                if text:
                    await ws.send_json({"type": "thinking", "text": text})

            elif etype == "span.model_request_end":
                # MA attaches token usage to the span terminator. The model
                # name may or may not be carried on model_usage across SDK
                # versions — fall back to the tier-configured agent model
                # (claude-haiku-4-5 / sonnet-4-6 / opus-4-7) so pricing still
                # resolves.
                usage = getattr(event, "model_usage", None)
                if usage is not None:
                    model_label = (
                        getattr(usage, "model", None)
                        or getattr(event, "model", None)
                        or agent_model
                    )
                    cost = compute_turn_cost(
                        model_label,
                        input_tokens=getattr(usage, "input_tokens", 0) or 0,
                        output_tokens=getattr(usage, "output_tokens", 0) or 0,
                        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                        cache_creation_input_tokens=getattr(
                            usage, "cache_creation_input_tokens", 0
                        ) or 0,
                    )
                    await ws.send_json({"type": "turn_cost", **cost})

            elif etype == "agent.custom_tool_use":
                events_by_id[event.id] = event
                await ws.send_json(
                    {
                        "type": "tool_use",
                        "name": getattr(event, "name", None),
                        "input": getattr(event, "input", {}) or {},
                    }
                )

            elif etype == "session.status_idle":
                stop = getattr(event, "stop_reason", None)
                stop_type = getattr(stop, "type", None) if stop is not None else None
                if stop_type != "requires_action":
                    continue
                event_ids = getattr(stop, "event_ids", None) or []
                for eid in event_ids:
                    tool_event = events_by_id.get(eid)
                    if tool_event is None:
                        logger.warning(
                            "[Diag-MA] requires_action for unknown event id %s", eid
                        )
                        continue
                    name = getattr(tool_event, "name", "")
                    payload = getattr(tool_event, "input", {}) or {}
                    result = await _dispatch_tool(
                        name, payload, device_slug, memory_root, client,
                        session_state, session_id,
                    )
                    # Emit the WS event if the dispatch succeeded.
                    bv_event = result.get("event")
                    if result.get("ok") and bv_event is not None:
                        await ws.send_json(bv_event.model_dump(by_alias=True))
                    result_for_agent = {k: v for k, v in result.items() if k != "event"}
                    await client.beta.sessions.events.send(
                        session_id,
                        events=[
                            {
                                "type": "user.custom_tool_result",
                                "custom_tool_use_id": eid,
                                "content": [
                                    {"type": "text", "text": json.dumps(result_for_agent, default=str)}
                                ],
                            }
                        ],
                    )

            elif etype == "session.status_terminated":
                await ws.send_json({"type": "session_terminated"})
                return

            elif etype == "session.error":
                err = getattr(event, "error", None)
                msg = getattr(err, "message", None) if err is not None else None
                await ws.send_json({"type": "error", "text": msg or "session error"})
