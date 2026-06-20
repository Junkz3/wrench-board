"""Fallback diagnostic runtime using `messages.stream` (no Managed Agents).

Keeps the WebSocket protocol identical to `runtime_managed`, so the frontend
doesn't care which mode is active. Activated with env var
`DIAGNOSTIC_MODE=direct`; used when the Managed Agents beta is unavailable
or when a lighter-weight path is wanted for local development.

Uses the streaming Messages API so each completed text block is emitted to
the WebSocket as soon as it finishes, rather than waiting for the full
response.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
from pathlib import Path
from typing import Any

from anthropic import APIError, AsyncAnthropic
from fastapi import WebSocket, WebSocketDisconnect

from api.agent import cloud_metering
from api.agent.chat_history import (
    append_event,
    build_board_refresh_note,
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
from api.agent.cousin_hint import build_cousin_line
from api.agent.dispatch_bv import dispatch_bv
from api.agent.macros import persist_macro
from api.agent.manifest import (
    _has_electrical_graph,
    build_tools_manifest,
    render_system_prompt,
)
from api.agent.owner_ref import current_owner_ref, set_owner_ref
from api.agent.session_caps import set_can_expand
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


def _strip_output_only(block: Any) -> Any:
    """Remove OUTPUT-only fields the API rejects when a block is re-sent as input.

    SDK ≥0.97 streaming returns ParsedTextBlock with `parsed_output` (the
    structured-outputs sink). Echoing it back in the next turn's messages is a
    400 ("messages.N.content.M.text.parsed_output: Extra inputs are not
    permitted"), which broke every turn after the first tool call. Strip it
    (and keep this the one place to drop any future output-only field)."""
    if isinstance(block, dict) and "parsed_output" in block:
        return {k: v for k, v in block.items() if k != "parsed_output"}
    return block


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
                    normalized_content.append(_strip_output_only(block))
                elif hasattr(block, "model_dump"):
                    normalized_content.append(_strip_output_only(block.model_dump(mode="json")))
                else:
                    normalized_content.append(block)
            return {**msg, "content": normalized_content}
        return msg
    if hasattr(msg, "model_dump"):
        return msg.model_dump(mode="json")
    return msg  # type: ignore[return-value]


# Inactivity-watchdog retry budget: how many times a stalled stream (no event
# within ma_stream_event_timeout_seconds) is re-streamed before the turn is
# declared lost. The retry re-sends the SAME request — safe because no partial
# state is committed until get_final_message() succeeds. Direct mode has no
# server-side replay, so this is the only resume mechanism it gets.
_STREAM_STALL_MAX_RETRIES = 1


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

    # Opus 4.7+ reasoning bump: adaptive thinking + xhigh effort give the
    # deep tier the agentic-coding profile recommended in the API skill
    # (~20 % better tool-use reasoning vs. defaults). Opus 4.7/4.8 only accept
    # `thinking.type=adaptive` (enabled returns 400); adaptive is also
    # incompatible with forced tool_choice — the runtime never sets one
    # here (default `auto`), so this is safe. Sonnet 4.6 / Haiku 4.5 keep
    # their defaults: effort=xhigh is Opus-tier only and 400s elsewhere.
    # Opus 4.7/4.8 default for `thinking.display` is "omitted" — without
    # `display: "summarized"` the frontend never sees the reasoning text.
    extra_kwargs: dict[str, Any] = {}
    if model.startswith(("claude-opus-4-7", "claude-opus-4-8")):
        extra_kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
        extra_kwargs["output_config"] = {"effort": "xhigh"}

    stall_retries = 0
    while True:
        # Emit each text block the moment it finishes (content_block_stop),
        # then dispatch any tool_use blocks once stop_reason is known. For a
        # typical answer with one narrative + N tool calls this means the tech
        # sees the narrative *before* the model has even finished emitting the
        # tool-use inputs.
        # Per-event inactivity watchdog (parity with runtime_managed): a hung
        # TCP connection or an unresponsive model can stall the stream's
        # __anext__() forever, leaving the tech on an infinite spinner. Each
        # event is wrapped in a timeout. Direct mode's messages.stream() has no
        # server-side replay, so on a stall we re-stream the SAME request (no
        # partial state is committed until get_final_message succeeds) up to
        # _STREAM_STALL_MAX_RETRIES times; only then is the turn declared lost
        # (terminal stream_error, WS kept alive for the next message).
        stream_timeout = get_settings().ma_stream_event_timeout_seconds
        stalled = False
        try:
            # PRODUCTION FREEZE FIX: the stream OPEN must be watchdog-bounded too,
            # not just per-event reads.
            #
            # The Anthropic SDK fires the actual HTTP request inside
            # `AsyncMessageStreamManager.__aenter__` (`await self.__api_request`).
            # Previously we entered it via `async with` — OUTSIDE the per-event
            # `asyncio.wait_for` below — so that connect/request await had NO
            # timeout. In the field, a concurrent CPU-bound pack `expand_pack`
            # starved the event loop for minutes; the diagnostic's next
            # stream-open (right after a bv_scene dispatch) got no service and
            # the server dropped the idle socket, so `__aenter__` hung forever.
            # The watchdog never engaged (it only wrapped `__anext__`), so the
            # turn froze until the client's own WS timeout ~5 min later and never
            # recovered even once the loop was free again. We now drive the
            # context manager manually so the OPEN is wrapped by the SAME
            # `wait_for`, and a hung open folds into the existing stall→retry
            # path. `try/finally` guarantees `__aexit__` runs (closing the
            # half-open connection) even when the open times out.
            stream_mgr = client.messages.stream(
                model=model,
                max_tokens=8000,
                system=cached_system,
                messages=messages,
                tools=cached_tools,
                **extra_kwargs,
            )
            stream = None
            try:
                try:
                    stream = await asyncio.wait_for(
                        stream_mgr.__aenter__(), timeout=stream_timeout
                    )
                except TimeoutError:
                    # The open never completed (starved/dead connection). Nothing
                    # is committed — treat exactly like a mid-stream stall so the
                    # retry/give-up logic below kicks in. `stream` stays None, so
                    # the finally's __aexit__ is skipped (never entered).
                    stalled = True
                if stream is not None:
                    stream_iter = stream.__aiter__()
                    while True:
                        try:
                            event = await asyncio.wait_for(
                                stream_iter.__anext__(), timeout=stream_timeout
                            )
                        except StopAsyncIteration:
                            break
                        except TimeoutError:
                            # Nothing is committed for this attempt — bail out of
                            # the read loop and let the post-stream logic retry or
                            # give up.
                            stalled = True
                            break
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
                    if not stalled:
                        response = await stream.get_final_message()
            finally:
                # Only close the manager if the open actually succeeded — calling
                # __aexit__ on a manager whose __aenter__ timed out would touch
                # uninitialised SDK state. A successful open is always closed,
                # even if the read loop stalled (releases the HTTP connection).
                if stream is not None:
                    await stream_mgr.__aexit__(None, None, None)
        except APIError as exc:
            # The SDK already retried 429/529/5xx up to anthropic_max_retries
            # before surfacing this. Reaching here = retries exhausted, a 4xx,
            # or a mid-stream disconnect. Unlike runtime_managed (server-side
            # replay + reconnect), direct mode can't resume — so end the turn
            # cleanly: signal the tech instead of letting the exception kill the
            # WS handler silently (quota already spent, no UI hint).
            logger.warning(
                "[Diag-Direct] Anthropic API error — repair=%s: %s", repair_id, exc
            )
            try:
                await ws.send_json({
                    "type": "stream_error",
                    "error": "api_error",
                    "message": (
                        "Le service du modèle a renvoyé une erreur — "
                        "réessayez dans un instant."
                    ),
                })
            except Exception:  # noqa: BLE001 — best-effort UI hint
                pass
            return

        if stalled:
            if stall_retries < _STREAM_STALL_MAX_RETRIES:
                stall_retries += 1
                logger.warning(
                    "[Diag-Direct] stream inactive for %.0fs — repair=%s; "
                    "retry %d/%d (re-streaming the same request)",
                    stream_timeout, repair_id, stall_retries,
                    _STREAM_STALL_MAX_RETRIES,
                )
                try:
                    await ws.send_json({
                        "type": "stream_retry",
                        "attempt": stall_retries,
                        "message": (
                            "Le flux du modèle s'est interrompu — "
                            "nouvelle tentative…"
                        ),
                    })
                except Exception:  # noqa: BLE001 — best-effort UI hint
                    pass
                continue  # re-stream this turn; messages list is untouched
            logger.warning(
                "[Diag-Direct] stream inactive for %.0fs after %d retries — "
                "repair=%s; ending turn (no server-side replay to reconnect to)",
                stream_timeout, stall_retries, repair_id,
            )
            try:
                await ws.send_json({
                    "type": "stream_error",
                    "error": "stream_timeout",
                    "message": (
                        f"Le flux du modèle est resté inactif "
                        f"{stream_timeout:.0f}s — la session a peut-être "
                        "été perdue."
                    ),
                })
            except Exception:  # noqa: BLE001 — best-effort UI hint
                pass
            return

        # A clean stream completed — reset the consecutive-stall budget so a
        # later sub-turn (model→tool→model) gets its own full retry allowance.
        stall_retries = 0

        # Token cost estimate for THIS API call — sent AFTER the text so the
        # frontend can attach a "$" chip to the just-rendered assistant bubble
        # and bump the running total in the panel footer.
        cost = cost_from_response(model, response.usage)
        await ws.send_json({"type": "turn_cost", **cost})

        # T13/T16 — report this LLM call's raw token usage to the cloud (the
        # tenant-private billing unit), keeping the direct runtime at parity
        # with runtime_managed's span.model_request_end hook. Without it,
        # `DIAGNOSTIC_MODE=direct` would spend API credit while the cloud's
        # ledger stays empty (free diagnostics). Best-effort + no-op when
        # cloud metering is unconfigured (self-host never phones home). The
        # owner_ref is the session's tenant (ContextVar); the Anthropic message
        # id is the cloud's idempotency key, so a retried report collapses to a
        # single ledger row. Skip a response with no id (would all dedup to one
        # row → undercount) — the customer-favourable choice.
        _msg_id = getattr(response, "id", None)
        if _msg_id is not None:
            cloud_metering.fire_and_forget_report(
                owner_ref=current_owner_ref(),
                model=model,
                input_tokens=getattr(response.usage, "input_tokens", 0) or 0,
                output_tokens=getattr(response.usage, "output_tokens", 0) or 0,
                # Cache tokens price at their own tiers cloud-side (read 0.1x,
                # creation 1.25x input) — without them a hot turn is overcharged.
                cache_read_input_tokens=getattr(
                    response.usage, "cache_read_input_tokens", 0
                ) or 0,
                cache_creation_input_tokens=getattr(
                    response.usage, "cache_creation_input_tokens", 0
                ) or 0,
                engine_repair_id=repair_id,
                event_id=f"{repair_id or device_slug}:{_msg_id}",
            )

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
            # Parity with runtime_managed (forwarders.py): the agent finished its
            # tech-turn and is waiting for the next user.message. Emit an explicit
            # turn_complete so WS clients (bench scripts, automated tests, and the
            # reused engine UI) know it's safe to send the next input — without it
            # the spinner hangs forever at end of turn. UI chat clients ignore it.
            await ws.send_json({"type": "turn_complete", "stop_reason": response.stop_reason})
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
            if block.name == "bv_propose_protocol":
                # Pattern-4: park for the tech's accept/reject before the
                # protocol touches disk (parity with runtime_managed).
                result = await _propose_protocol_with_confirmation(
                    ws=ws,
                    block=block,
                    device_slug=device_slug,
                    memory_root=memory_root,
                    session=session,
                    repair_id=repair_id,
                    conv_id=conv_id,
                )
            elif block.name == "cam_capture":
                # Flow B: request a camera frame, await it, and feed the image
                # back as the tool_result (parity with runtime_managed).
                result = await _dispatch_cam_capture_direct(
                    ws=ws,
                    client=client,
                    memory_root=memory_root,
                    device_slug=device_slug,
                    repair_id=repair_id,
                    block=block,
                )
            elif block.name in (
                "bv_update_protocol",
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
                # Same pre-dispatch refresh as the managed runtime's
                # dispatch_tool: a boardview switched mid-turn (re-upload
                # while the agent is in a tool loop) reloads before the
                # refdes validation runs. Cheap no-op when unchanged.
                session.refresh_board_if_changed()
                result = dispatch_bv(session, block.name, block.input or {})
            elif block.name.startswith("profile_"):
                result = _dispatch_profile_tool(block.name, block.input or {}, session=session)
            elif block.name.startswith("stock_"):
                # Donor inventory / salvage tools. Parity with the managed
                # runtime (tool_dispatch._dispatch_stock) — without this branch
                # they fell into _dispatch_mb_tool's "unknown mb_* tool" else,
                # so the agent could never mark/search/consume donors in direct
                # mode (the prod default). owner_ref scoping is already bound
                # via set_owner_ref() at session open; the stock tools read the
                # ContextVar internally.
                result = _dispatch_stock_tool(block.name, block.input or {})
            else:
                result = await _dispatch_mb_tool(
                    block.name,
                    block.input or {},
                    device_slug,
                    memory_root,
                    client,
                    session,
                    session_id=repair_id,
                    repair_id=repair_id,
                    conv_id=conv_id,
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
            # cam_capture (and any future vision tool) returns a ready-made
            # content list (image + text blocks) so the model sees the picture;
            # everything else feeds back a JSON-stringified result.
            cam_content = (
                result.get("tool_result_content") if isinstance(result, dict) else None
            )
            if cam_content is not None:
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": cam_content,
                    }
                )
            else:
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


logger = logging.getLogger("wrench_board.agent.direct")


async def _await_protocol_confirmation(
    ws: WebSocket, tool_use_id: str, timeout_s: float
) -> dict | None:
    """Park on the WS until the tech accepts/rejects THIS proposal, or the
    inactivity timeout fires.

    Direct mode is a single sequential loop (unlike runtime_managed's two
    concurrent forwarders), so the dispatch reads the socket itself while the
    confirmation modal is up. The tech is expected to only click accept/reject,
    so any unrelated frame that arrives mid-wait is drained. Returns the
    decision frame, or None on timeout.
    """
    while True:
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=timeout_s)
        except TimeoutError:
            return None
        try:
            frame = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if (
            isinstance(frame, dict)
            and frame.get("type") == "client.protocol_confirmation"
            and frame.get("tool_use_id") == tool_use_id
        ):
            return frame
        # ignore any other frame while waiting for this confirmation


async def _propose_protocol_with_confirmation(
    *,
    ws: WebSocket,
    block: Any,
    device_slug: str,
    memory_root: Path,
    session: SessionState,
    repair_id: str | None,
    conv_id: str | None,
) -> dict:
    """bv_propose_protocol with the Pattern-4 tech-confirmation handshake.

    Emit ``protocol_pending_confirmation``, park on the WS for the tech's
    accept/reject (or time out), and only run the real dispatch on accept.
    Mirrors runtime_managed's ``_dispatch_protocol_with_confirmation`` minus the
    Managed-Agents event plumbing — direct mode returns the result inline to the
    turn loop, which feeds it back to the agent as the tool_result.
    """
    tool_use_id = getattr(block, "id", None) or ""
    tool_input = block.input or {}
    timeout_s = get_settings().ma_protocol_confirmation_timeout_seconds

    # Lightweight projection for the modal — title + rationale + step previews
    # (12 max). The full payload would bloat the frame; the modal needs the gist.
    steps = list(tool_input.get("steps") or [])
    step_previews = [
        {
            "type": s.get("type"),
            "target": s.get("target"),
            "test_point": s.get("test_point"),
            "instruction": s.get("instruction"),
        }
        for s in steps[:12]
    ]
    await ws.send_json({
        "type": "protocol_pending_confirmation",
        "tool_use_id": tool_use_id,
        "title": tool_input.get("title") or "",
        "rationale": tool_input.get("rationale") or "",
        "step_count": len(steps),
        "steps": step_previews,
        "rule_inspirations": list(tool_input.get("rule_inspirations") or []),
        "timeout_seconds": timeout_s,
    })

    decision = await _await_protocol_confirmation(ws, tool_use_id, timeout_s)

    if decision is None:
        # Tech never answered — drop the modal and tell the agent so it doesn't
        # assume the protocol is live. No protocol is materialised on disk.
        try:
            await ws.send_json({
                "type": "protocol_confirmation_timeout",
                "tool_use_id": tool_use_id,
                "timeout_seconds": timeout_s,
            })
        except Exception:  # noqa: BLE001 — best-effort UI hint
            pass
        return {
            "ok": False,
            "reason": "confirmation_timeout",
            "error": (
                f"Protocol confirmation timed out after {timeout_s:.0f}s — the "
                "technician did not respond. Try again with a tighter, more "
                "obvious protocol or ask in chat first."
            ),
        }

    verdict = str(decision.get("decision") or "").lower().strip()
    if verdict == "accept":
        return await _dispatch_protocol_tool(
            "bv_propose_protocol",
            tool_input,
            device_slug,
            memory_root,
            session,
            repair_id=repair_id,
            conv_id=conv_id,
        )

    # reject (or any non-accept value) — never materialise; hand the tech's
    # reason back to the agent so it pivots instead of re-emitting.
    reason = str(decision.get("reason") or "").strip()
    return {
        "ok": False,
        "reason": "rejected",
        "error": (
            "Technician rejected the proposed protocol. Reason: "
            f"{reason or '(none given)'}. Do not re-emit the same protocol; "
            "either ask a clarifying question, propose a different approach, or "
            "wait for further instruction."
        ),
    }


async def _await_capture_response(
    ws: WebSocket, request_id: str, timeout_s: float
) -> dict | None:
    """Park on the WS until the matching client.capture_response arrives, or
    the timeout fires. Same sequential-read rationale as
    :func:`_await_protocol_confirmation`. Returns the response frame or None."""
    while True:
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=timeout_s)
        except TimeoutError:
            return None
        try:
            frame = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if (
            isinstance(frame, dict)
            and frame.get("type") == "client.capture_response"
            and frame.get("request_id") == request_id
        ):
            return frame


async def _dispatch_cam_capture_direct(
    *,
    ws: WebSocket,
    client: AsyncAnthropic,
    memory_root: Path,
    device_slug: str,
    repair_id: str | None,
    block: Any,
) -> dict:
    """cam_capture (Flow B) for direct mode: push a server.capture_request,
    await the frame, persist + upload it to the Files API, and return an
    image-shaped tool_result content list. Mirrors runtime/camera.py minus the
    Managed-Agents event envelope — direct returns the content inline to the
    turn loop (which wraps it as the tool_result). Always returns a dict with
    ``tool_result_content`` (a list of content blocks)."""
    tool_use_id = getattr(block, "id", None) or ""
    tool_input = block.input or {}
    timeout_s = get_settings().ma_camera_capture_timeout_seconds
    request_id = secrets.token_urlsafe(8)

    await ws.send_json({
        "type": "server.capture_request",
        "request_id": request_id,
        "tool_use_id": tool_use_id,
        "reason": tool_input.get("reason") or "",
    })

    response = await _await_capture_response(ws, request_id, timeout_s)
    if response is None:
        return {
            "ok": False,
            "is_error": True,
            "tool_result_content": [{
                "type": "text",
                "text": (
                    f"Capture timeout after {timeout_s:.0f}s — the frontend did "
                    "not respond. Check that a camera is selected in the metabar."
                ),
            }],
        }

    try:
        bytes_ = base64.b64decode(response.get("base64") or "", validate=True)
        if not bytes_:
            raise ValueError("empty payload")
        mime = (response.get("mime") or "image/jpeg").lower()
        device_label = response.get("device_label") or "camera"

        persist_macro(
            memory_root=memory_root, slug=device_slug, repair_id=repair_id or "",
            source="capture", bytes_=bytes_, mime=mime,
        )
        uploaded = await client.beta.files.upload(
            file=(f"capture_{request_id}.jpg", bytes_, mime),
        )
        return {
            "ok": True,
            "tool_result_content": [
                {"type": "image",
                 "source": {"type": "file", "file_id": uploaded.id}},
                {"type": "text",
                 "text": f"Capture acquise depuis {device_label}."},
            ],
        }
    except Exception as exc:  # noqa: BLE001 — never crash the turn on a bad frame
        logger.exception("[Diag-Direct] cam_capture processing failed")
        return {
            "ok": False,
            "is_error": True,
            "tool_result_content": [{
                "type": "text", "text": f"Capture processing failed: {exc}",
            }],
        }


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
    repair_id: str | None = None,
    conv_id: str | None = None,
) -> dict:
    """Run one of the mb_* memory-bank tools. Passes `session` so mb_get_component can aggregate."""
    # Direct-mode memory recall (parity with the managed FUSE stores). Read-only
    # wrappers over api.agent.recall — see manifest RECALL_TOOLS.
    if name == "mb_recall_field_reports":
        from api.agent.recall import recall_field_reports

        reports = recall_field_reports(
            device_slug=device_slug,
            memory_root=memory_root,
            query=payload.get("query"),
            refdes=payload.get("refdes"),
            limit=payload.get("limit", 8),
        )
        return {"ok": True, "reports": reports, "count": len(reports)}
    if name == "mb_search_patterns":
        from api.agent.recall import search_patterns

        patterns = search_patterns(payload.get("query", ""))
        return {"ok": True, "patterns": patterns, "count": len(patterns)}
    if name == "mb_search_playbooks":
        from api.agent.recall import search_playbooks

        playbooks = search_playbooks(payload.get("symptom", ""))
        return {"ok": True, "playbooks": playbooks, "count": len(playbooks)}
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
    if name == "mb_record_session_log":
        from api.agent.tools import mb_record_session_log as _mb_session_log

        return await _mb_session_log(
            client=client,
            device_slug=device_slug,
            repair_id=repair_id or session_id or "",
            conv_id=conv_id or "",
            symptom=payload.get("symptom", ""),
            outcome=payload.get("outcome", "unresolved"),
            memory_root=memory_root,
            tested=payload.get("tested"),
            hypotheses=payload.get("hypotheses"),
            findings=payload.get("findings"),
            next_steps=payload.get("next_steps"),
            lesson=payload.get("lesson"),
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


def _dispatch_stock_tool(name: str, payload: dict) -> dict:
    """Run one of the stock_* donor-inventory tools.

    Mirrors ``tool_dispatch._dispatch_stock`` (the managed path). Lazy-imports
    ``api.stock.tools`` to keep the optional package decoupled from the runtime,
    same as the profile dispatcher. The tools scope to the session tenant via
    ``current_owner_ref()`` internally — the ContextVar is set at session open.
    """
    from api.stock.tools import (
        stock_consume,
        stock_list_donors,
        stock_mark_donor,
        stock_search,
        stock_unmark_donor,
    )

    if name == "stock_search":
        return stock_search(payload)
    if name == "stock_consume":
        return stock_consume(payload)
    if name == "stock_mark_donor":
        return stock_mark_donor(payload)
    if name == "stock_unmark_donor":
        return stock_unmark_donor(payload)
    if name == "stock_list_donors":
        return stock_list_donors(payload)
    logger.warning("unknown stock_* tool: %s", name)
    return {"ok": False, "reason": "unknown-tool"}


async def run_diagnostic_session_direct(
    ws: WebSocket,
    device_slug: str,
    tier: str = "fast",
    repair_id: str | None = None,
    conv_id: str | None = None,
    owner_ref: str | None = None,
    can_expand: bool = True,
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

    `owner_ref` (the tenant id from the cloud's X-Owner-Ref header) binds the
    session to its tenant so owner-sensitive tools (stock) stay isolated.

    `can_expand` (the cloud's X-Wb-Can-Expand verdict) gates the paid pack
    enrichment tool: False (free plan) drops `mb_expand_knowledge` from the
    manifest so the agent never proposes it. True / self-host = unrestricted.
    """
    set_owner_ref(owner_ref)
    set_can_expand(can_expand)
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
        "deep": "claude-opus-4-8",
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
    # The user-turn loop below re-checks the active boardview each turn
    # (`refresh_board_if_changed`) and recomputes both when it changed.
    # T9a Phase B: when this board has no schematic of its own, offer the agent a
    # same-family sibling pack as an indicative fallback reference.
    cousin_line = None
    if not _has_electrical_graph(device_slug):
        cousin_line = await build_cousin_line(device_slug)
    system_prompt = render_system_prompt(
        session, device_slug=device_slug, cousin_line=cousin_line
    )
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
        and messages[-1]["content"].startswith("[New diagnostic session")
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
        and not (isinstance(m.get("content"), str) and m["content"].startswith("[New diagnostic session"))
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

            # Camera capability handshake. The frontend sends this AFTER
            # session_ready (i.e. after the initial manifest snapshot at line
            # ~1045), so flip the flag and REBUILD the manifest in place — that
            # makes cam_capture available on the next turn (parity with
            # runtime_managed). No agent turn is triggered.
            if isinstance(incoming, dict) and incoming.get("type") == "client.capabilities":
                session.has_camera = bool(incoming.get("camera_available"))
                tools = build_tools_manifest(session)
                continue

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

            # Tech pressed Abandon on the running quest panel — mark the
            # protocol as abandoned in the on-disk store, broadcast a
            # protocol_updated WS event so the UI cleans its state, and
            # inject a synthetic user message so the agent stops acting on
            # the dead protocol.
            if isinstance(incoming, dict) and incoming.get("type") == "protocol_abandon":
                from api.tools.protocol import (
                    load_active_protocol,
                )
                from api.tools.protocol import (
                    update_protocol as _update_protocol,
                )
                reason = (incoming.get("reason") or "tech_dismiss").strip() or "tech_dismiss"
                res = _update_protocol(
                    memory_root=memory_root,
                    device_slug=device_slug,
                    repair_id=repair_id or "",
                    action="abandon_protocol",
                    reason=reason,
                    conv_id=resolved_conv_id,
                )
                if res.get("ok"):
                    proto = load_active_protocol(memory_root, device_slug, repair_id or "", conv_id=resolved_conv_id)
                    history_tail = proto.history[-3:] if proto is not None else []
                    await ws.send_json({
                        "type": "protocol_updated",
                        "protocol_id": res.get("protocol_id"),
                        "action": "abandoned",
                        "current_step_id": None,
                        "steps": [s.model_dump(mode="json") for s in (proto.steps if proto else [])],
                        "history_tail": [h.model_dump(mode="json") for h in history_tail],
                        "status": "abandoned",
                        "reason": reason,
                    })
                    synthetic = (
                        f"[protocol_abandoned] The technician abandoned the "
                        f"running protocol. Reason: {reason}. Stop acting on "
                        f"this protocol; propose a fresh approach if relevant."
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
                    await ws.send_json({
                        "type": "error",
                        "code": "protocol_abandon_rejected",
                        "text": res.get("reason", "unknown"),
                    })
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
                    "I just finished this repair. Can you summarise in one "
                    "sentence which component(s) I fixed or replaced based on "
                    "the history of our chat and the measurements taken, then "
                    "record the result with the `mb_validate_finding` tool? "
                    "If you have any doubt about a refdes or a mode, ask me "
                    "before calling the tool."
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

            # Board snapshot refresh — a boardview imported mid-session is
            # invisible otherwise: the manifest (gating bv_*) and the system
            # prompt's "boardview ❌" line were computed at WS open. When the
            # active board changed, recompute both and tell the agent inline
            # via a ctx-style note that `strip_ctx_tag` drops from replays.
            board_note: str | None = None
            if session.refresh_board_if_changed():
                system_prompt = render_system_prompt(
                    session, device_slug=device_slug, cousin_line=cousin_line
                )
                tools = build_tools_manifest(session)
                board_note = build_board_refresh_note(
                    session.board, session.board_source
                )
                logger.info(
                    "[Diag-Direct] board (re)loaded mid-session from %s",
                    session.board_source,
                )

            prefix = "\n".join(line for line in (ctx_tag, board_note) if line)
            tagged_text = f"{prefix}\n\n{user_text}" if prefix else user_text
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
