# SPDX-License-Identifier: Apache-2.0
"""Fallback diagnostic runtime using `messages.stream` (no Managed Agents).

Keeps the WebSocket protocol identical to `runtime_managed`, so the frontend
doesn't care which mode is active. Activated with env var
`DIAGNOSTIC_MODE=direct`; used when the Managed Agents beta is unavailable
or when a lighter-weight path is wanted for local development.

Uses the streaming Messages API so each completed text block is emitted to
the WebSocket as soon as it finishes, rather than waiting for the full
response — CLAUDE.md §"Streaming over polling".
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
    build_ctx_tag,
    build_session_intro,
    ensure_conversation,
    get_conversation_tier,
    list_conversations,
    load_events_with_costs,
    materialize_conversation,
    touch_conversation,
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
    mb_record_finding,
)
from api.config import get_settings
from api.session.state import SessionState
from api.tools.schematic import mb_schematic_graph


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
    conv_id: str | None,
    memory_root: Path,
) -> None:
    """Drive the model-call / tool-dispatch inner loop until the agent stops.

    Extracted so it can be called from two places: (a) automatically right
    after we inject the session intro (fresh session on a known repair), and
    (b) after each user input in the main WS loop. Both paths mutate the
    caller's `messages` list in place.
    """
    # Mark the end of the stable prefix (system + tools) with cache_control
    # so Anthropic caches the ~2-3k token prefix across turns. First call
    # pays 1.25x input for cache creation; every subsequent call of this
    # session pays 0.10x for the same prefix — the 50-90% input reduction
    # Anthropic advertises. Our tools list is large (16 custom tools) so
    # this is the big win.
    cached_tools = list(tools)
    if cached_tools:
        last = cached_tools[-1]
        if "cache_control" not in last:
            cached_tools[-1] = {**last, "cache_control": {"type": "ephemeral"}}
    cached_system = [
        {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
    ]

    # Opus 4.7 reasoning bump: adaptive thinking + xhigh effort give the
    # deep tier the agentic-coding profile recommended in the API skill
    # (~20 % better tool-use reasoning vs. defaults). Opus 4.7 only accepts
    # `thinking.type=adaptive` (enabled returns 400); adaptive is also
    # incompatible with forced tool_choice — the runtime never sets one
    # here (default `auto`), so this is safe. Sonnet 4.6 / Haiku 4.5 keep
    # their defaults: effort=xhigh is Opus-tier only and 400s elsewhere.
    extra_kwargs: dict[str, Any] = {}
    if model.startswith("claude-opus-4-7"):
        extra_kwargs["thinking"] = {"type": "adaptive"}
        extra_kwargs["output_config"] = {"effort": "xhigh"}

    while True:
        # Emit each text block the moment it finishes (content_block_stop),
        # then dispatch any tool_use blocks once stop_reason is known. For a
        # typical answer with one narrative + N tool calls this means the tech
        # sees the narrative *before* the model has even finished emitting the
        # tool-use inputs.
        async with client.messages.stream(
            model=model,
            max_tokens=8000,
            system=cached_system,
            messages=messages,
            tools=cached_tools,
            **extra_kwargs,
        ) as stream:
            async for event in stream:
                if getattr(event, "type", None) != "content_block_stop":
                    continue
                idx = getattr(event, "index", None)
                if idx is None:
                    continue
                snapshot_blocks = getattr(stream.current_message_snapshot, "content", [])
                if idx >= len(snapshot_blocks):
                    continue
                block = snapshot_blocks[idx]
                if getattr(block, "type", None) != "text":
                    continue
                clean, unknown = sanitize_agent_text(
                    getattr(block, "text", "") or "", session.board
                )
                if unknown:
                    logger.warning("sanitizer wrapped unknown refdes: %s", unknown)
                await ws.send_json({"type": "message", "role": "assistant", "text": clean})
            response = await stream.get_final_message()

        # Token cost estimate for THIS API call — sent AFTER the text so the
        # frontend can attach a "$" chip to the just-rendered assistant bubble
        # and bump the running total in the panel footer.
        cost = cost_from_response(model, response.usage)
        await ws.send_json({"type": "turn_cost", **cost})

        # Roll the turn's cost into the conversation index so the popover's
        # "turns · $spend · recency" trio stays fresh even if the tech never
        # refetches.
        if repair_id and conv_id:
            touch_conversation(
                device_slug=device_slug,
                repair_id=repair_id,
                conv_id=conv_id,
                cost_usd=cost.get("cost_usd") if isinstance(cost, dict) else None,
                model=model,
            )

        assistant_msg = _normalize_message({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            messages.append(assistant_msg)
            if conv_id:
                append_event(
                    device_slug=device_slug,
                    repair_id=repair_id,
                    conv_id=conv_id,
                    event=assistant_msg,
                    cost=cost,
                )
            return

        messages.append(assistant_msg)
        if conv_id:
            append_event(
                device_slug=device_slug,
                repair_id=repair_id,
                conv_id=conv_id,
                event=assistant_msg,
                cost=cost,
            )
        tool_results: list[dict] = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            await ws.send_json({"type": "tool_use", "name": block.name, "input": block.input})
            if block.name in (
                "bv_propose_protocol", "bv_update_protocol",
                "bv_record_step_result", "bv_get_protocol",
            ):
                result = await _dispatch_protocol_tool(
                    block.name,
                    block.input or {},
                    device_slug,
                    memory_root,
                    session,
                    repair_id=repair_id,
                    conv_id=conv_id,
                )
            elif block.name.startswith("bv_"):
                result = dispatch_bv(session, block.name, block.input or {})
            elif block.name.startswith("profile_"):
                result = _dispatch_profile_tool(block.name, block.input or {}, session=session)
            else:
                result = await _dispatch_mb_tool(
                    block.name,
                    block.input or {},
                    device_slug,
                    memory_root,
                    client,
                    session,
                    session_id=repair_id,
                )
            # Tools may return either `event` (single) for atomic ops or
            # `events` (list) for composites like bv_scene. Both paths fan
            # out individual WS frames so the frontend stays oblivious.
            single_event = result.get("event")
            multi_events = result.get("events") if isinstance(result.get("events"), list) else None
            emitted_any = False
            if result.get("ok") and single_event is not None:
                await ws.send_json(
                    single_event if isinstance(single_event, dict)
                    else single_event.model_dump(by_alias=True)
                )
                emitted_any = True
            if multi_events:
                for ev in multi_events:
                    await ws.send_json(
                        ev if isinstance(ev, dict) else ev.model_dump(by_alias=True)
                    )
                    emitted_any = True
            if emitted_any and block.name.startswith("bv_"):
                # Snapshot board overlay after every successful bv_* mutation
                # — same rationale as runtime_managed: a WS reconnect should
                # show the same highlights/annotations, not a bare board.
                from api.agent.board_state import save_board_state
                save_board_state(
                    memory_root=memory_root,
                    device_slug=device_slug,
                    repair_id=repair_id,
                    session=session,
                    conv_id=conv_id,
                )
            result_for_agent = {k: v for k, v in result.items() if k not in ("event", "events")}
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result_for_agent, default=str),
                }
            )
        tool_results_msg = {"role": "user", "content": tool_results}
        messages.append(tool_results_msg)
        if conv_id:
            append_event(
                device_slug=device_slug,
                repair_id=repair_id,
                conv_id=conv_id,
                event=tool_results_msg,
            )


async def _replay_history_to_ws(
    ws: WebSocket,
    records: list[tuple[dict[str, Any], dict[str, Any] | None]],
) -> None:
    """Stream past events back to the client so its chat panel can reconstruct
    the conversation on a reopen. Only surface user text + assistant text +
    tool_use — tool_results are implementation noise for the UI. When an
    assistant turn has a persisted cost, re-emit a turn_cost event with
    replay=true right after the text block so the session running total
    reflects the true lifetime spend.
    """
    from api.agent.chat_history import strip_ctx_tag

    if not records:
        return
    await ws.send_json({"type": "history_replay_start", "count": len(records)})
    for msg, cost in records:
        role = msg.get("role")
        content = msg.get("content")
        if role == "user" and isinstance(content, str):
            await ws.send_json(
                {"type": "message", "role": "user", "text": strip_ctx_tag(content)}
            )
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
            if cost is not None:
                await ws.send_json({"type": "turn_cost", **cost, "replay": True})
    await ws.send_json({"type": "history_replay_end"})


logger = logging.getLogger("microsolder.agent.direct")


async def _dispatch_protocol_tool(
    name: str,
    payload: dict,
    device_slug: str,
    memory_root: Path,
    session: SessionState,
    repair_id: str | None = None,
    conv_id: str | None = None,
) -> dict:
    """Dispatch the 4 stepwise-protocol tools (bv_propose_protocol, bv_update_protocol,
    bv_record_step_result, bv_get_protocol). Mirrors the branches in runtime_managed._dispatch_tool.
    """
    if name == "bv_propose_protocol":
        from api.tools.protocol import (
            StepInput as _SI,
        )
        from api.tools.protocol import (
            propose_protocol as _propose,
        )

        valid_refdes = (
            {p.refdes for p in session.board.parts}
            if session.board is not None
            else None
        )
        # Tolerate "comp:U7" / "rail:+5V" prefixes the agent learns from
        # mb_set_observation; strip "comp:" so the refdes validates, drop
        # rail/test-point prefixes into test_point so the step still anchors
        # somewhere meaningful even without a board part.
        for s in payload.get("steps", []) or []:
            t = s.get("target")
            if isinstance(t, str) and ":" in t:
                kind, _, rest = t.partition(":")
                if kind == "comp":
                    s["target"] = rest
                else:
                    s["target"] = None
                    s.setdefault("test_point", t)
        try:
            step_inputs = [_SI.model_validate(s) for s in payload.get("steps", [])]
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": "invalid_step_input", "detail": str(exc)}
        result = _propose(
            memory_root=memory_root,
            device_slug=device_slug,
            repair_id=repair_id or "",
            title=payload.get("title", ""),
            rationale=payload.get("rationale", ""),
            steps=step_inputs,
            rule_inspirations=payload.get("rule_inspirations") or None,
            valid_refdes=valid_refdes,
            conv_id=conv_id,
        )
        if result.get("ok"):
            from api.tools.protocol import load_active_protocol
            proto = load_active_protocol(memory_root, device_slug, repair_id or "", conv_id=conv_id)
            if proto is not None:
                result["event"] = {
                    "type": "protocol_proposed",
                    "protocol_id": proto.protocol_id,
                    "title": proto.title,
                    "rationale": proto.rationale,
                    "steps": [s.model_dump() for s in proto.steps],
                    "current_step_id": proto.current_step_id,
                }
        return result

    if name == "bv_update_protocol":
        from api.tools.protocol import StepInput as _SI
        from api.tools.protocol import update_protocol as _update

        new_step_payload = payload.get("new_step")
        new_step = None
        if new_step_payload is not None:
            try:
                new_step = _SI.model_validate(new_step_payload)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "reason": "invalid_new_step", "detail": str(exc)}
        result = _update(
            memory_root=memory_root,
            device_slug=device_slug,
            repair_id=repair_id or "",
            action=payload.get("action", ""),
            reason=payload.get("reason", ""),
            step_id=payload.get("step_id"),
            after=payload.get("after"),
            new_step=new_step,
            new_order=payload.get("new_order"),
            verdict=payload.get("verdict"),
            conv_id=conv_id,
        )
        if result.get("ok"):
            from api.tools.protocol import load_active_protocol
            proto = load_active_protocol(memory_root, device_slug, repair_id or "", conv_id=conv_id)
            history_tail = proto.history[-3:] if proto is not None else []
            result["event"] = {
                "type": "protocol_updated",
                "protocol_id": result.get("protocol_id"),
                "action": payload.get("action"),
                "current_step_id": result.get("current_step_id"),
                "steps": [s.model_dump() for s in (proto.steps if proto else [])],
                "history_tail": [h.model_dump() for h in history_tail],
            }
        return result

    if name == "bv_record_step_result":
        from api.tools.protocol import record_step_result as _record
        result = _record(
            memory_root=memory_root,
            device_slug=device_slug,
            repair_id=repair_id or "",
            step_id=payload.get("step_id", ""),
            value=payload.get("value"),
            unit=payload.get("unit"),
            observation=payload.get("observation"),
            skip_reason=payload.get("skip_reason"),
            submitted_by="agent",
            conv_id=conv_id,
        )
        if result.get("ok"):
            from api.tools.protocol import load_active_protocol
            proto = load_active_protocol(memory_root, device_slug, repair_id or "", conv_id=conv_id)
            history_tail = proto.history[-3:] if proto is not None else []
            result["event"] = {
                "type": "protocol_updated",
                "protocol_id": result.get("protocol_id"),
                "action": "step_completed",
                "current_step_id": result.get("current_step_id"),
                "steps": [s.model_dump() for s in (proto.steps if proto else [])],
                "history_tail": [h.model_dump() for h in history_tail],
            }
        return result

    if name == "bv_get_protocol":
        from api.tools.protocol import load_active_protocol
        proto = load_active_protocol(memory_root, device_slug, repair_id or "", conv_id=conv_id)
        if proto is None:
            return {"ok": True, "active": False}
        return {
            "ok": True, "active": True,
            "protocol_id": proto.protocol_id,
            "title": proto.title,
            "rationale": proto.rationale,
            "current_step_id": proto.current_step_id,
            "status": proto.status,
            "steps": [s.model_dump() for s in proto.steps],
            "history": [h.model_dump() for h in proto.history],
        }

    logger.warning("unknown protocol tool: %s", name)
    return {"ok": False, "reason": "unknown-tool", "error": f"unknown protocol tool: {name}"}


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
            session=session,
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
    if name == "mb_schematic_graph":
        return mb_schematic_graph(
            device_slug=device_slug,
            memory_root=memory_root,
            query=payload.get("query", ""),
            label=payload.get("label"),
            refdes=payload.get("refdes"),
            index=payload.get("index"),
            domain=payload.get("domain"),
            killed_refdes=payload.get("killed_refdes"),
            failures=payload.get("failures"),
            rail_overrides=payload.get("rail_overrides"),
            session=session,
        )
    if name == "mb_hypothesize":
        from api.tools.hypothesize import mb_hypothesize as _mb_hypothesize

        return _mb_hypothesize(
            device_slug=device_slug,
            memory_root=memory_root,
            state_comps=payload.get("state_comps"),
            state_rails=payload.get("state_rails"),
            metrics_comps=payload.get("metrics_comps"),
            metrics_rails=payload.get("metrics_rails"),
            max_results=payload.get("max_results", 5),
            # Always pass the session's repair_id — it scopes the
            # diagnosis_log.jsonl hook (for field-corpus calibration) and
            # is the fallback for journal-based synthesis when the agent
            # didn't supply explicit state/metrics.
            repair_id=payload.get("repair_id") or session_id,
        )
    if name == "mb_record_measurement":
        from api.tools.measurements import mb_record_measurement as _mb_rec

        return _mb_rec(
            device_slug=device_slug,
            repair_id=session_id or "",
            memory_root=memory_root,
            target=payload.get("target", ""),
            value=payload.get("value", 0.0),
            unit=payload.get("unit", "V"),
            nominal=payload.get("nominal"),
            note=payload.get("note"),
            source="agent",
        )
    if name == "mb_list_measurements":
        from api.tools.measurements import mb_list_measurements as _mb_list

        return _mb_list(
            device_slug=device_slug,
            repair_id=session_id or "",
            memory_root=memory_root,
            target=payload.get("target"),
            since=payload.get("since"),
        )
    if name == "mb_compare_measurements":
        from api.tools.measurements import mb_compare_measurements as _mb_cmp

        return _mb_cmp(
            device_slug=device_slug,
            repair_id=session_id or "",
            memory_root=memory_root,
            target=payload.get("target", ""),
            before_ts=payload.get("before_ts"),
            after_ts=payload.get("after_ts"),
        )
    if name == "mb_observations_from_measurements":
        from api.tools.measurements import mb_observations_from_measurements as _mb_syn

        return _mb_syn(
            device_slug=device_slug,
            repair_id=session_id or "",
            memory_root=memory_root,
        )
    if name == "mb_set_observation":
        from api.tools.measurements import mb_set_observation as _mb_set

        return _mb_set(
            device_slug=device_slug,
            repair_id=session_id or "",
            memory_root=memory_root,
            target=payload.get("target", ""),
            mode=payload.get("mode", "unknown"),
        )
    if name == "mb_clear_observations":
        from api.tools.measurements import mb_clear_observations as _mb_clr

        return _mb_clr(
            device_slug=device_slug,
            repair_id=session_id or "",
            memory_root=memory_root,
        )
    if name == "mb_validate_finding":
        from api.tools.validation import mb_validate_finding as _mb_val

        return _mb_val(
            device_slug=device_slug,
            repair_id=session_id or "",
            memory_root=memory_root,
            fixes=payload.get("fixes", []),
            tech_note=payload.get("tech_note"),
            agent_confidence=payload.get("agent_confidence", "high"),
        )
    if name == "mb_expand_knowledge":
        return await mb_expand_knowledge(
            client=client,
            device_slug=device_slug,
            focus_symptoms=payload.get("focus_symptoms", []),
            focus_refdes=payload.get("focus_refdes", []),
            memory_root=memory_root,
            session=session,
        )
    logger.warning("unknown mb_* tool: %s", name)
    return {"ok": False, "reason": "unknown-tool"}


def _dispatch_profile_tool(name: str, payload: dict, session=None) -> dict:
    """Run one of the profile_* technician-profile tools."""
    from api.profile.tools import (
        profile_check_skills,
        profile_get,
        profile_track_skill,
    )

    if name == "profile_get":
        return profile_get(session=session)
    if name == "profile_check_skills":
        return profile_check_skills(payload.get("candidate_skills", []))
    if name == "profile_track_skill":
        return profile_track_skill(
            payload.get("skill_id", ""),
            payload.get("evidence", {}),
        )
    logger.warning("unknown profile_* tool: %s", name)
    return {"ok": False, "reason": "unknown-tool"}


async def run_diagnostic_session_direct(
    ws: WebSocket,
    device_slug: str,
    tier: str = "fast",
    repair_id: str | None = None,
    conv_id: str | None = None,
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
        await ws.send_json({
            "type": "error",
            "code": "missing_api_key",
            "text": "ANTHROPIC_API_KEY absente — configure-la dans .env puis relance le serveur.",
        })
        await ws.close()
        return

    client = AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=settings.anthropic_max_retries)  # noqa: E501
    memory_root = Path(settings.memory_root)
    session = SessionState.from_device(device_slug)
    tier_to_model = {
        "fast": "claude-haiku-4-5",
        "normal": "claude-sonnet-4-6",
        "deep": "claude-opus-4-7",
    }
    model = tier_to_model.get(tier, settings.anthropic_model_main)

    # Resolve the conversation once; every write/read below targets this id.
    # Anonymous sessions (no repair_id) skip conversation tracking entirely —
    # they already don't persist anything. Lazy materialization
    # (`materialize=False`): when the resolution would create a fresh conv,
    # we get back a pre-allocated id but nothing is written to disk yet —
    # the slot only persists if the tech actually sends a message. The first
    # `_materialize_pending()` call writes the index entry.
    resolved_conv_id: str | None = None
    pending_materialize = False
    conversation_count = 0
    if repair_id:
        resolved_conv_id, pending_materialize = ensure_conversation(
            device_slug=device_slug,
            repair_id=repair_id,
            conv_id=conv_id,
            tier=tier,
            memory_root=memory_root,
            materialize=False,
        )
        conversation_count = len(
            list_conversations(
                device_slug=device_slug,
                repair_id=repair_id,
                memory_root=memory_root,
            )
        )

    pending_state = {"pending": pending_materialize}

    def _materialize_pending() -> None:
        """Idempotent: write the conv index entry + dir on the first call,
        no-op afterwards. Used to defer the disk write to first user input
        so opens-without-message don't pollute the index."""
        if not pending_state["pending"] or not resolved_conv_id or not repair_id:
            return
        materialize_conversation(
            device_slug=device_slug,
            repair_id=repair_id,
            conv_id=resolved_conv_id,
            tier=tier,
            memory_root=memory_root,
        )
        pending_state["pending"] = False

    # Surface the conv's preferred tier so the frontend can auto-align when
    # the WS opened with a default tier that doesn't match — same rationale
    # as the managed runtime (avoids silently dropping the tech onto an
    # almost-empty per-tier thread of an existing multi-tier conv).
    conv_tier_pref: str | None = None
    if resolved_conv_id and repair_id and not pending_state["pending"]:
        conv_tier_pref = get_conversation_tier(
            device_slug=device_slug,
            repair_id=repair_id,
            conv_id=resolved_conv_id,
            memory_root=memory_root,
        )

    await ws.accept()
    await ws.send_json(
        {
            "type": "session_ready",
            "mode": "direct",
            "device_slug": device_slug,
            "tier": tier,
            "conv_tier": conv_tier_pref,
            "model": model,
            "board_loaded": session.board is not None,
            "repair_id": repair_id,
            "conv_id": resolved_conv_id,
            "conversation_count": conversation_count,
        }
    )

    # Hydrate any active protocol so the UI panel rebuilds on reconnect.
    # Same protocol_cleared fallback as runtime_managed — silence at WS
    # open would otherwise leave the previous conv's wizard pinned.
    if repair_id:
        from api.tools.protocol import load_active_protocol as _lap
        active = _lap(memory_root, device_slug, repair_id or "", conv_id=resolved_conv_id)
        if active is not None:
            await ws.send_json({
                "type": "protocol_proposed",
                "protocol_id": active.protocol_id,
                "title": active.title,
                "rationale": active.rationale,
                "steps": [s.model_dump(mode="json") for s in active.steps],
                "current_step_id": active.current_step_id,
                "replay": True,
            })
        else:
            await ws.send_json({"type": "protocol_cleared"})

    # Hydrate the boardview overlay snapshot — same rationale as the
    # managed runtime: the overlay state is the on-disk truth, not
    # something to reconstruct from chat events. Apply to live SessionState
    # AND emit the boardview.* events so brd_viewer reconstructs visually.
    if repair_id:
        from api.agent.board_state import load_board_state, replay_board_state_to_ws
        # Wipe the renderer's overlay first — same rationale as
        # runtime_managed: silence ≠ clear, the previous conv's overlay
        # would otherwise leak onto a fresh conv's empty canvas.
        await ws.send_json({"type": "boardview.reset_view"})
        snapshot = load_board_state(
            memory_root=memory_root,
            device_slug=device_slug,
            repair_id=repair_id,
            conv_id=resolved_conv_id,
        )
        if snapshot:
            session.restore_view(snapshot)
            sent = await replay_board_state_to_ws(ws, snapshot)
            if sent:
                logger.info(
                    "[Diag-Direct] replayed boardview state for repair=%s conv=%s (%d events)",
                    repair_id, resolved_conv_id, sent,
                )

    # NOTE: prompt + manifest are a snapshot of the session at open time.
    # If a future task supports loading a board mid-session, both must be
    # recomputed after `session.set_board(...)`.
    system_prompt = render_system_prompt(session, device_slug=device_slug)
    tools = build_tools_manifest(session)

    # Load prior history (+ per-turn costs) when reopening a persisted repair —
    # the agent continues the same conversation and the chat panel rebuilds
    # with the right lifetime cost total.
    records: list[tuple[dict, dict | None]] = []
    if resolved_conv_id:
        records = load_events_with_costs(
            device_slug=device_slug,
            repair_id=repair_id,
            conv_id=resolved_conv_id,
            memory_root=memory_root,
        )
    messages: list[dict] = [event for event, _cost in records]
    if records:
        logger.info(
            "[Diag-Direct] Resuming repair=%s conv=%s with %d prior events",
            repair_id,
            resolved_conv_id,
            len(records),
        )
        await _replay_history_to_ws(ws, records)
    elif repair_id and resolved_conv_id:
        # Fresh session on a known repair — stash the device identity + the
        # reported symptom as a hidden first user message so the agent has
        # context the moment the tech DOES type. We do NOT call the agent
        # here: compute only runs on explicit user action.
        # The intro is kept in the in-memory `messages` list but we DO NOT
        # `append_event` it on disk yet when the conv is pending — otherwise
        # an open-without-message would persist a single intro line into a
        # never-indexed conv directory. The deferred append happens on the
        # first real user input below, right after _materialize_pending().
        intro = build_session_intro(device_slug=device_slug, repair_id=repair_id)
        if intro:
            intro_msg = {"role": "user", "content": intro}
            messages.append(intro_msg)
            if not pending_state["pending"]:
                append_event(
                    device_slug=device_slug,
                    repair_id=repair_id,
                    conv_id=resolved_conv_id,
                    event=intro_msg,
                )
            await ws.send_json(
                {
                    "type": "context_loaded",
                    "device_slug": device_slug,
                    "repair_id": repair_id,
                }
            )
            logger.info(
                "[Diag-Direct] Stashed session intro for repair=%s conv=%s (awaiting tech input, pending=%s)",
                repair_id,
                resolved_conv_id,
                pending_state["pending"],
            )

    # When the conv is pending, the intro_msg above isn't yet on disk. Track
    # it so we can flush it as the first appended event right after
    # materialization (preserves the chronological JSONL even though the
    # write is deferred).
    pending_intro_msg = (
        messages[-1]
        if pending_state["pending"]
        and messages
        and isinstance(messages[-1], dict)
        and messages[-1].get("role") == "user"
        and isinstance(messages[-1].get("content"), str)
        and messages[-1]["content"].startswith("[Nouvelle session")
        else None
    )

    def _materialize_and_flush_intro() -> None:
        """Materialize the conv on disk, then write the deferred intro so
        the JSONL still starts with the device-context line — same on-disk
        shape that resume / replay paths expect."""
        was_pending = pending_state["pending"]
        _materialize_pending()
        if was_pending and pending_intro_msg is not None and resolved_conv_id:
            append_event(
                device_slug=device_slug,
                repair_id=repair_id,
                conv_id=resolved_conv_id,
                event=pending_intro_msg,
            )

    first_user_seen = any(
        isinstance(m, dict)
        and m.get("role") == "user"
        and not (isinstance(m.get("content"), str) and m["content"].startswith("[Nouvelle session"))
        for m in messages
    )

    # Per-turn context tag — restated on EVERY user message so smaller models
    # don't lose device + symptom on terse follow-ups. ~25 tokens, stable
    # cacheable prefix.
    ctx_tag = build_ctx_tag(
        device_slug=device_slug, repair_id=repair_id, memory_root=memory_root
    )

    import asyncio as _asyncio

    from api.tools.measurements import set_ws_emitter
    from api.tools.validation import set_ws_emitter as set_validation_emitter

    def _emit(event: dict) -> None:
        _asyncio.create_task(ws.send_json(event))

    set_ws_emitter(_emit)
    set_validation_emitter(_emit)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                incoming = json.loads(raw)
            except json.JSONDecodeError:
                incoming = {"text": raw}

            # Client submits a step result from the protocol UI panel.
            # Record it, emit a protocol_updated WS event, then inject a
            # synthetic user message so the agent can react to the outcome.
            if isinstance(incoming, dict) and incoming.get("type") == "protocol_step_result":
                from api.tools.protocol import (
                    load_active_protocol,
                )
                from api.tools.protocol import (
                    record_step_result as _record,
                )
                res = _record(
                    memory_root=memory_root,
                    device_slug=device_slug,
                    repair_id=repair_id or "",
                    step_id=incoming.get("step_id", ""),
                    value=incoming.get("value"),
                    unit=incoming.get("unit"),
                    observation=incoming.get("observation"),
                    skip_reason=incoming.get("skip_reason"),
                    submitted_by="tech",
                    conv_id=resolved_conv_id,
                )
                if res.get("ok"):
                    proto = load_active_protocol(memory_root, device_slug, repair_id or "", conv_id=resolved_conv_id)
                    history_tail = proto.history[-3:] if proto is not None else []
                    await ws.send_json({
                        "type": "protocol_updated",
                        "protocol_id": res.get("protocol_id"),
                        "action": "step_completed",
                        "current_step_id": res.get("current_step_id"),
                        "steps": [s.model_dump(mode="json") for s in (proto.steps if proto else [])],
                        "history_tail": [h.model_dump(mode="json") for h in history_tail],
                    })
                    target = ""
                    if proto is not None:
                        src_step = next(
                            (s for s in proto.steps if s.id == incoming.get("step_id")),
                            None,
                        )
                        if src_step is not None:
                            target = src_step.target or src_step.test_point or ""
                    synthetic = (
                        f"[step_result] step={incoming.get('step_id', '')} target={target} "
                        f"value={incoming.get('value')}{incoming.get('unit') or ''} "
                        f"outcome={res.get('outcome')} · "
                        f"plan: {len(proto.steps) if proto else 0} steps, "
                        f"current={res.get('current_step_id') or 'completed'}"
                    )
                    user_msg = {"role": "user", "content": synthetic}
                    messages.append(user_msg)
                    if resolved_conv_id:
                        _materialize_and_flush_intro()
                        append_event(
                            device_slug=device_slug,
                            repair_id=repair_id,
                            conv_id=resolved_conv_id,
                            event=user_msg,
                            memory_root=memory_root,
                        )
                    await _run_agent_turn(
                        ws=ws,
                        client=client,
                        model=model,
                        system_prompt=system_prompt,
                        tools=tools,
                        messages=messages,
                        session=session,
                        device_slug=device_slug,
                        repair_id=repair_id,
                        conv_id=resolved_conv_id,
                        memory_root=memory_root,
                    )
                else:
                    await ws.send_json({"type": "error", "code": "protocol_result_rejected",
                                         "text": res.get("reason", "unknown")})
                continue

            # Intercept validation trigger events before they reach the agent
            # as ordinary messages. Synthesise a user-role prompt that asks
            # the agent to summarise fixes and call mb_validate_finding. The
            # trigger's JSONL record is the ONLY append for this turn — the
            # normal user-append below is skipped when `is_trigger` is set.
            is_trigger = False
            if isinstance(incoming, dict) and incoming.get("type") == "validation.start":
                is_trigger = True
                user_text = (
                    "J'ai fini cette réparation. Peux-tu résumer en une phrase "
                    "quel(s) composant(s) j'ai réparé ou remplacé à partir de "
                    "l'historique de notre discussion et des mesures prises, "
                    "puis enregistrer le résultat avec l'outil "
                    "`mb_validate_finding` ? Si tu as un doute sur un refdes ou "
                    "un mode, demande-moi avant d'appeler l'outil."
                )
                if resolved_conv_id:
                    _materialize_and_flush_intro()
                    append_event(
                        device_slug=device_slug,
                        repair_id=repair_id,
                        conv_id=resolved_conv_id,
                        memory_root=memory_root,
                        event={
                            "role": "user",
                            "content": user_text,
                            "source": "trigger",
                            "trigger_kind": "validation.start",
                        },
                    )
            else:
                user_text = (incoming.get("text") or "").strip()

            if not user_text:
                continue

            # Before the first live exchange, flip the repair's status so the
            # library badge shows it's actively being worked on.
            if not messages:
                touch_status(device_slug=device_slug, repair_id=repair_id, status="in_progress")

            # Stamp the conversation title from the first real user message —
            # the tech-visible summary in the switcher popover. Materialize
            # the conv on disk at the same moment if it was opened lazily.
            if not first_user_seen and repair_id and resolved_conv_id:
                _materialize_and_flush_intro()
                touch_conversation(
                    device_slug=device_slug,
                    repair_id=repair_id,
                    conv_id=resolved_conv_id,
                    first_message=user_text,
                    memory_root=memory_root,
                )
                first_user_seen = True

            tagged_text = f"{ctx_tag}\n\n{user_text}" if ctx_tag else user_text
            user_msg = {"role": "user", "content": tagged_text}
            messages.append(user_msg)
            if resolved_conv_id and not is_trigger:
                _materialize_and_flush_intro()
                append_event(
                    device_slug=device_slug,
                    repair_id=repair_id,
                    conv_id=resolved_conv_id,
                    event=user_msg,
                )

            await _run_agent_turn(
                ws=ws,
                client=client,
                model=model,
                system_prompt=system_prompt,
                tools=tools,
                messages=messages,
                session=session,
                device_slug=device_slug,
                repair_id=repair_id,
                conv_id=resolved_conv_id,
                memory_root=memory_root,
            )
    except WebSocketDisconnect:
        logger.info(
            "[Diag-Direct] WS closed for device=%s repair=%s conv=%s",
            device_slug,
            repair_id,
            resolved_conv_id,
        )
    finally:
        set_ws_emitter(None)
        set_validation_emitter(None)
