# SPDX-License-Identifier: Apache-2.0
"""Fallback diagnostic runtime using `messages.create` (no Managed Agents).

Keeps the WebSocket protocol identical to `runtime_managed`, so the frontend
doesn't care which mode is active. Activated with env var
`DIAGNOSTIC_MODE=direct`; used when the Managed Agents beta is unavailable
or when we want a lighter-weight path for local demos.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic
from fastapi import WebSocket, WebSocketDisconnect

from api.agent.chat_history import (
    append_event,
    build_session_intro,
    load_events,
    touch_status,
)
from api.agent.dispatch_bv import dispatch_bv
from api.agent.manifest import build_tools_manifest, render_system_prompt
from api.agent.pricing import cost_from_response
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


def _normalize_message(msg: Any) -> dict[str, Any]:
    """Normalize a message to plain-dict form so it can be both persisted to
    JSONL and passed back to client.messages.create on the next turn.

    Anthropic's response.content is a list of typed Block objects (pydantic
    models). This coerces them to dicts — the SDK still accepts dicts for
    subsequent calls, and we can json.dump them safely.
    """
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, list):
            normalized_content = []
            for block in content:
                if isinstance(block, dict):
                    normalized_content.append(block)
                elif hasattr(block, "model_dump"):
                    normalized_content.append(block.model_dump(mode="json"))
                else:
                    normalized_content.append(block)
            return {**msg, "content": normalized_content}
        return msg
    if hasattr(msg, "model_dump"):
        return msg.model_dump(mode="json")
    return msg  # type: ignore[return-value]


async def _run_agent_turn(
    *,
    ws: WebSocket,
    client: AsyncAnthropic,
    model: str,
    system_prompt: str,
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    session: SessionState,
    device_slug: str,
    repair_id: str | None,
    memory_root: Path,
) -> None:
    """Drive the model-call / tool-dispatch inner loop until the agent stops.

    Extracted so it can be called from two places: (a) automatically right
    after we inject the session intro (fresh session on a known repair), and
    (b) after each user input in the main WS loop. Both paths mutate the
    caller's `messages` list in place.
    """
    while True:
        response = await client.messages.create(
            model=model,
            max_tokens=8000,
            system=system_prompt,
            messages=messages,
            tools=tools,
        )

        # Two passes over response.content are intentional: emit every
        # text block first so the user reads the narrative before the
        # canvas animates, THEN dispatch tool_use blocks (which fire
        # boardview.* events). Block-level ordering matches the model's
        # output order, just grouped by kind.
        for block in response.content:
            if block.type == "text":
                clean, unknown = sanitize_agent_text(block.text, session.board)
                if unknown:
                    logger.warning("sanitizer wrapped unknown refdes: %s", unknown)
                await ws.send_json(
                    {"type": "message", "role": "assistant", "text": clean}
                )

        # Token cost estimate for THIS API call — sent AFTER the text so the
        # frontend can attach a "$" chip to the just-rendered assistant bubble
        # and bump the running total in the panel footer.
        cost = cost_from_response(model, response.usage)
        await ws.send_json({"type": "turn_cost", **cost})

        assistant_msg = _normalize_message(
            {"role": "assistant", "content": response.content}
        )

        if response.stop_reason != "tool_use":
            messages.append(assistant_msg)
            append_event(
                device_slug=device_slug, repair_id=repair_id, event=assistant_msg
            )
            return

        messages.append(assistant_msg)
        append_event(
            device_slug=device_slug, repair_id=repair_id, event=assistant_msg
        )
        tool_results: list[dict] = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            await ws.send_json(
                {"type": "tool_use", "name": block.name, "input": block.input}
            )
            if block.name.startswith("bv_"):
                result = dispatch_bv(session, block.name, block.input or {})
            else:
                result = await _dispatch_mb_tool(
                    block.name, block.input or {}, device_slug,
                    memory_root, client, session,
                )
            event = result.get("event")
            if result.get("ok") and event is not None:
                await ws.send_json(event.model_dump(by_alias=True))
            result_for_agent = {k: v for k, v in result.items() if k != "event"}
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result_for_agent, default=str),
                }
            )
        tool_results_msg = {"role": "user", "content": tool_results}
        messages.append(tool_results_msg)
        append_event(
            device_slug=device_slug, repair_id=repair_id, event=tool_results_msg
        )


async def _replay_history_to_ws(
    ws: WebSocket, events: list[dict[str, Any]]
) -> None:
    """Stream past events back to the client so its chat panel can reconstruct
    the conversation on a reopen. Only surface user text + assistant text +
    tool_use — tool_results are implementation noise for the UI.
    """
    if not events:
        return
    await ws.send_json({"type": "history_replay_start", "count": len(events)})
    for msg in events:
        role = msg.get("role")
        content = msg.get("content")
        if role == "user" and isinstance(content, str):
            await ws.send_json({"type": "message", "role": "user", "text": content})
        elif role == "assistant" and isinstance(content, list):
            for block in content:
                btype = block.get("type") if isinstance(block, dict) else None
                if btype == "text":
                    await ws.send_json(
                        {
                            "type": "message",
                            "role": "assistant",
                            "text": block.get("text", ""),
                            "replay": True,
                        }
                    )
                elif btype == "tool_use":
                    await ws.send_json(
                        {
                            "type": "tool_use",
                            "name": block.get("name", ""),
                            "input": block.get("input", {}),
                            "replay": True,
                        }
                    )
    await ws.send_json({"type": "history_replay_end"})

logger = logging.getLogger("microsolder.agent.direct")


async def _dispatch_mb_tool(
    name: str,
    payload: dict,
    device_slug: str,
    memory_root: Path,
    client: AsyncAnthropic,
    session: SessionState,
    session_id: str | None = None,
) -> dict:
    """Run one of the mb_* memory-bank tools. Passes `session` so mb_get_component can aggregate."""
    if name == "mb_get_component":
        return mb_get_component(
            device_slug=device_slug,
            refdes=payload.get("refdes", ""),
            memory_root=memory_root,
            session=session,
        )
    if name == "mb_get_rules_for_symptoms":
        return mb_get_rules_for_symptoms(
            device_slug=device_slug,
            symptoms=payload.get("symptoms", []),
            memory_root=memory_root,
            max_results=payload.get("max_results", 5),
        )
    if name == "mb_list_findings":
        return mb_list_findings(
            device_slug=device_slug,
            memory_root=memory_root,
            limit=payload.get("limit", 20),
            filter_refdes=payload.get("filter_refdes"),
        )
    if name == "mb_record_finding":
        return await mb_record_finding(
            client=client,
            device_slug=device_slug,
            refdes=payload.get("refdes", ""),
            symptom=payload.get("symptom", ""),
            confirmed_cause=payload.get("confirmed_cause", ""),
            memory_root=memory_root,
            mechanism=payload.get("mechanism"),
            notes=payload.get("notes"),
            session_id=session_id,
        )
    if name == "mb_expand_knowledge":
        return await mb_expand_knowledge(
            client=client,
            device_slug=device_slug,
            focus_symptoms=payload.get("focus_symptoms", []),
            focus_refdes=payload.get("focus_refdes", []),
            memory_root=memory_root,
        )
    logger.warning("unknown mb_* tool: %s", name)
    return {"ok": False, "reason": "unknown-tool"}


async def run_diagnostic_session_direct(
    ws: WebSocket, device_slug: str, tier: str = "fast", repair_id: str | None = None
) -> None:
    """Run a direct-mode diagnostic session over `ws` for `device_slug`.

    Protocol on the wire (same as `runtime_managed`):
      - Client sends `{"type": "message", "text": "..."}`
      - Server emits `{"type": "message", "role": "assistant", "text": "..."}`,
        `{"type": "tool_use", "name": ..., "input": ...}`, and
        `{"type": "boardview.<verb>", ...}` events.

    When `repair_id` is provided, the session is scoped to that repair:
    past messages are loaded from disk and replayed to the client, and
    every new turn is appended to the same JSONL. Without it, the session
    runs unpersisted and exits when the WS closes.
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        await ws.accept()
        await ws.send_json({"type": "error", "text": "ANTHROPIC_API_KEY not set"})
        await ws.close()
        return

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    memory_root = Path(settings.memory_root)
    session = SessionState.from_device(device_slug)
    tier_to_model = {
        "fast": "claude-haiku-4-5",
        "normal": "claude-sonnet-4-6",
        "deep": "claude-opus-4-7",
    }
    model = tier_to_model.get(tier, settings.anthropic_model_main)
    await ws.accept()
    await ws.send_json({
        "type": "session_ready",
        "mode": "direct",
        "device_slug": device_slug,
        "tier": tier,
        "model": model,
        "board_loaded": session.board is not None,
        "repair_id": repair_id,
    })

    # NOTE: prompt + manifest are a snapshot of the session at open time.
    # If a future task supports loading a board mid-session, both must be
    # recomputed after `session.set_board(...)`.
    system_prompt = render_system_prompt(session, device_slug=device_slug)
    tools = build_tools_manifest(session)

    # Load prior history when reopening a persisted repair — the agent
    # continues the same conversation, and the client UI rebuilds the chat.
    messages: list[dict] = load_events(device_slug=device_slug, repair_id=repair_id)
    if messages:
        logger.info(
            "[Diag-Direct] Resuming repair=%s with %d prior events",
            repair_id,
            len(messages),
        )
        await _replay_history_to_ws(ws, messages)
    elif repair_id:
        # Fresh session on a known repair — stash the device identity + the
        # reported symptom as a hidden first user message so the agent has
        # context the moment the tech DOES type. We do NOT call the agent
        # here: compute only runs on explicit user action.
        intro = build_session_intro(device_slug=device_slug, repair_id=repair_id)
        if intro:
            intro_msg = {"role": "user", "content": intro}
            messages.append(intro_msg)
            append_event(
                device_slug=device_slug, repair_id=repair_id, event=intro_msg
            )
            await ws.send_json({
                "type": "context_loaded",
                "device_slug": device_slug,
                "repair_id": repair_id,
            })
            logger.info(
                "[Diag-Direct] Stashed session intro for repair=%s (awaiting tech input)",
                repair_id,
            )

    try:
        while True:
            raw = await ws.receive_text()
            try:
                user_text = (json.loads(raw).get("text") or "").strip()
            except json.JSONDecodeError:
                user_text = raw.strip()
            if not user_text:
                continue

            # Before the first live exchange, flip the repair's status so the
            # library badge shows it's actively being worked on.
            if not messages:
                touch_status(
                    device_slug=device_slug, repair_id=repair_id, status="in_progress"
                )

            user_msg = {"role": "user", "content": user_text}
            messages.append(user_msg)
            append_event(device_slug=device_slug, repair_id=repair_id, event=user_msg)

            await _run_agent_turn(
                ws=ws, client=client, model=model,
                system_prompt=system_prompt, tools=tools,
                messages=messages, session=session,
                device_slug=device_slug, repair_id=repair_id,
                memory_root=memory_root,
            )
    except WebSocketDisconnect:
        logger.info("[Diag-Direct] WS closed for device=%s repair=%s", device_slug, repair_id)
