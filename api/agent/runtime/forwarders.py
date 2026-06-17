"""WS event loops in / out.

* ``_forward_ws_to_session`` — read tech-side frames off the WS, dispatch
  client-side handlers, forward user.message to the MA session.
* ``_forward_session_to_ws`` — stream MA events back, sanitise + relay
  agent text and tool_use events, dispatch custom tool calls.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic
from fastapi import WebSocket, WebSocketDisconnect

from api.agent import cloud_metering
from api.agent import runtime_managed as _rm
from api.agent._session_mirrors import SessionMirrors as _SessionMirrors
from api.agent.chat_history import (
    append_event,
    touch_conversation,
)
from api.agent.owner_ref import current_owner_ref
from api.agent.session_caps import current_can_expand
from api.agent.pricing import compute_turn_cost
from api.agent.runtime._aux import (
    TierLiteral,
    _mirror_jsonl,
    _PendingConv,
    _safe_tool_result_text,
    logger,
)
from api.agent.runtime.camera import _dispatch_cam_capture
from api.agent.runtime.handlers import (
    _handle_client_capabilities,
    _handle_client_capture_response,
    _handle_client_protocol_confirmation,
    _handle_client_upload_macro,
)
from api.agent.runtime.protocol import _dispatch_protocol_with_confirmation
from api.agent.runtime.subagents import (
    _run_knowledge_curator,
    _run_subagent_consultation,
)
from api.agent.sanitize import sanitize_agent_text
from api.session.state import SessionState


async def _forward_ws_to_session(
    ws: WebSocket,
    client: AsyncAnthropic,
    session_id: str,
    *,
    pending_intro: str | None = None,
    ctx_tag: str | None = None,
    repair_id: str | None = None,
    device_slug: str | None = None,
    conv_id: str | None = None,
    memory_root: Path | None = None,
    pending_conv: _PendingConv | None = None,
    session_state: SessionState | None = None,
) -> None:
    """Read user text from the WS, post it as `user.message` to the session.

    When `pending_intro` is set, it is PREFIXED to the tech's very first
    message so the agent sees (device context + reported symptom) and the
    tech's actual question in a single turn — avoids the empty-ack turn
    that happens when context is sent in isolation.

    When `ctx_tag` is set, it is prepended to EVERY user message as a
    stable, cacheable single-line prefix that restates the device +
    symptom — keeps Haiku from losing context on later turns.
    """
    intro_pending = pending_intro
    first_user_seen = False
    while True:
        raw = await ws.receive_text()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"text": raw}

        ptype = payload.get("type")

        # Files+Vision frames — handled before MA forwarding.
        if ptype == "client.capabilities":
            if session_state is not None:
                _handle_client_capabilities(session_state, payload)
            continue

        if ptype == "client.upload_macro":
            if session_state is None or not repair_id or not device_slug or not memory_root:
                logger.warning("[Diag-MA] upload_macro received but session context incomplete")
                continue
            try:
                await _handle_client_upload_macro(
                    client=client,
                    session=session_state,
                    memory_root=memory_root,
                    slug=device_slug,
                    repair_id=repair_id,
                    ma_session_id=session_id,
                    frame=payload,
                )
            except ValueError as exc:
                logger.warning("[Diag-MA] upload_macro rejected: %s", exc)
                await ws.send_json({
                    "type": "server.upload_macro_error",
                    "reason": str(exc),
                })
            continue

        if ptype == "client.capture_response":
            if session_state is not None:
                await _handle_client_capture_response(session=session_state, frame=payload)
            continue

        # Pattern 4 (tool_confirmation round-trip) for `bv_propose_protocol`.
        # The runtime parked the tool call on a Future in
        # `session_state.pending_protocol_confirmations[tool_use_id]`; the UI
        # modal resolves it by sending us this frame.
        if ptype == "client.protocol_confirmation":
            if session_state is not None:
                await _handle_client_protocol_confirmation(
                    session=session_state, frame=payload,
                )
            continue

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

        # Client submits a step result from the protocol UI panel.
        # Record it, emit a protocol_updated WS event, then forward a
        # synthetic user.message to the agent summarising the outcome so
        # it can react (adjust next steps, give a reading, etc.).
        if payload.get("type") == "protocol_step_result":
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
                step_id=payload.get("step_id", ""),
                value=payload.get("value"),
                unit=payload.get("unit"),
                observation=payload.get("observation"),
                skip_reason=payload.get("skip_reason"),
                submitted_by="tech",
                conv_id=conv_id,
            )
            if res.get("ok"):
                proto = load_active_protocol(memory_root, device_slug, repair_id or "", conv_id=conv_id)
                history_tail = proto.history[-3:] if proto is not None else []
                await ws.send_json({
                    "type": "protocol_updated",
                    "protocol_id": res.get("protocol_id"),
                    "action": "step_completed",
                    "current_step_id": res.get("current_step_id"),
                    "steps": [s.model_dump(mode="json") for s in (proto.steps if proto else [])],
                    "history_tail": [h.model_dump(mode="json") for h in history_tail],
                })
                step_id = payload.get("step_id", "")
                target = ""
                value = payload.get("value")
                unit = payload.get("unit") or ""
                outcome = res.get("outcome", "neutral")
                current = res.get("current_step_id") or "completed"
                step_count = len(proto.steps) if proto else 0
                if proto is not None:
                    src_step = next((s for s in proto.steps if s.id == step_id), None)
                    if src_step is not None:
                        target = src_step.target or src_step.test_point or ""
                synthetic = (
                    f"[step_result] step={step_id} target={target} "
                    f"value={value}{unit} outcome={outcome} · "
                    f"plan: {step_count} steps, current={current}"
                )
                await client.beta.sessions.events.send(
                    session_id,
                    events=[{"type": "user.message",
                             "content": [{"type": "text", "text": synthetic}]}],
                )
            else:
                await ws.send_json({"type": "error", "code": "protocol_result_rejected",
                                     "text": res.get("reason", "unknown")})
            continue

        # Tech pressed Abandon on the running quest panel — mark the protocol
        # as abandoned in the on-disk store, broadcast a protocol_updated WS
        # event so the UI cleans its state, and forward a synthetic
        # user.message so the agent stops acting on the dead protocol. The
        # session.events.send call is wrapped in try/except: if the MA
        # state machine rejects the synthetic (rare, was previously masked
        # by the now-fixed oversized seed bug), the protocol is still
        # abandoned cleanly on disk and the UI panel cleans up — only the
        # agent stays oblivious until its next protocol-aware tool call
        # gets a "no_active_protocol" return.
        if payload.get("type") == "protocol_abandon":
            from api.tools.protocol import (
                load_active_protocol,
            )
            from api.tools.protocol import (
                update_protocol as _update_protocol,
            )
            reason = (payload.get("reason") or "tech_dismiss").strip() or "tech_dismiss"
            res = _update_protocol(
                memory_root=memory_root,
                device_slug=device_slug,
                repair_id=repair_id or "",
                action="abandon_protocol",
                reason=reason,
                conv_id=conv_id,
            )
            if res.get("ok"):
                proto = load_active_protocol(memory_root, device_slug, repair_id or "", conv_id=conv_id)
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
                    f"this protocol; do not re-emit it; if relevant, "
                    f"propose a fresh approach or ask a clarifying question."
                )
                try:
                    await client.beta.sessions.events.send(
                        session_id,
                        events=[{"type": "user.message",
                                 "content": [{"type": "text", "text": synthetic}]}],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[Diag-MA] protocol_abandoned synthetic forward failed "
                        "session=%s exc=%s — UI cleaned up, agent will learn on "
                        "next tool call (no_active_protocol)",
                        session_id, type(exc).__name__,
                    )
            else:
                await ws.send_json({
                    "type": "error",
                    "code": "protocol_abandon_rejected",
                    "text": res.get("reason", "unknown"),
                })
            continue

        # Intercept validation trigger events before they reach the agent as
        # ordinary messages. Synthesise a user-role prompt that asks the agent
        # to summarise fixes and call mb_validate_finding.
        if payload.get("type") == "validation.start":
            text = (
                "I just finished this repair. Can you summarise in one "
                "sentence which component(s) I fixed or replaced based on "
                "the history of our chat and the measurements taken, then "
                "record the result with the `mb_validate_finding` tool? "
                "If you have any doubt about a refdes or a mode, ask me "
                "before calling the tool."
            )
            if repair_id and conv_id and device_slug and memory_root:
                if pending_conv is not None:
                    pending_conv.materialize_now()
                append_event(
                    device_slug=device_slug,
                    repair_id=repair_id,
                    conv_id=conv_id,
                    memory_root=memory_root,
                    event={
                        "role": "user",
                        "content": text,
                        "source": "trigger",
                        "trigger_kind": "validation.start",
                    },
                )
        else:
            text = (payload.get("text") or "").strip()

        if not text:
            continue

        # Stamp the conv title from the first real user message (before the
        # intro prefix is glued on so the popover shows what the tech typed,
        # not the device-context boilerplate). Materialize the conv on disk
        # at the same moment if it was opened lazily — this is the point at
        # which the slot stops being a no-op WS open and starts holding
        # actual content worth indexing.
        if not first_user_seen and repair_id and conv_id and device_slug:
            if pending_conv is not None:
                pending_conv.materialize_now()
            touch_conversation(
                device_slug=device_slug,
                repair_id=repair_id,
                conv_id=conv_id,
                first_message=text,
                memory_root=memory_root,
            )
            first_user_seen = True

        if intro_pending:
            text = intro_pending + "\n\n---\n\n" + text
            intro_pending = None
            if repair_id and device_slug:
                from api.agent.chat_history import touch_status

                touch_status(device_slug=device_slug, repair_id=repair_id, status="in_progress")
        # The board is a snapshot taken at WS open. The pre-dispatch refresh
        # in `dispatch_tool` makes a mid-session import visible to bv_* calls,
        # but an agent told "no board" at session start never CALLS bv_* — so
        # re-resolve on every user turn and, when the active board actually
        # changed, tell the agent inline. The note stacks under the ctx tag
        # as one leading block that `strip_ctx_tag` removes from replays.
        board_note: str | None = None
        if session_state is not None and session_state.refresh_board_if_changed():
            from api.agent.chat_history import build_board_refresh_note

            board_note = build_board_refresh_note(
                session_state.board, session_state.board_source
            )
            logger.info(
                "[Diag-MA] board (re)loaded mid-session from %s",
                session_state.board_source,
            )
        prefix = "\n".join(line for line in (ctx_tag, board_note) if line)
        if prefix:
            text = prefix + "\n\n" + text
        await client.beta.sessions.events.send(
            session_id,
            events=[
                {
                    "type": "user.message",
                    "content": [{"type": "text", "text": text}],
                }
            ],
        )
        # Mirror the user turn to local JSONL so we still have the transcript
        # if MA later archives the session. Symmetric with what MA stores —
        # ctx_tag + intro prefix included; the replay path strips them.
        _mirror_jsonl(
            device_slug=device_slug,
            repair_id=repair_id,
            conv_id=conv_id,
            memory_root=memory_root,
            event={
                "role": "user",
                "content": [{"type": "text", "text": text}],
            },
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
    *,
    tier: TierLiteral,
    environment_id: str,
    repair_id: str | None = None,
    conv_id: str | None = None,
    session_mirrors: _SessionMirrors | None = None,
    pending_conv: _PendingConv | None = None,
) -> None:
    """Stream session events to the WS and dispatch custom tool calls.

    `agent_model` is the tier's configured model (claude-haiku-4-5 etc.),
    used as a fallback when MA's span.model_request_end doesn't carry a
    model name on its model_usage payload.
    """
    # Deduplicate tool-use responses. MA can re-emit `session.status_idle`
    # with `stop_reason=requires_action` carrying the SAME event_ids after
    # we've already sent their `user.custom_tool_result` — a naive re-dispatch
    # then posts a duplicate response, which MA rejects with 400
    # ("Invalid user.custom_tool_result event [...] waiting on responses to
    # events [...]") and tears down the stream. Track ids we've answered.
    #
    # responded_tool_ids + events_by_id + pending_tool_results live OUTSIDE
    # the reconnect loop below so they survive a stream drop+resume: the
    # tool-dispatch dedup contract must hold across the gap, otherwise a
    # reconnect would re-dispatch a tool the agent already got an answer to.
    responded_tool_ids: set[str] = set()
    # Lossless-reconnect dedup. The MA SSE stream has NO replay: if the
    # connection drops (watchdog timeout, transport reset, or the stream
    # ending without a terminal `session.status_terminated`), every event
    # emitted during the gap is gone. Worse, a `requires_action` that fired
    # in the gap leaves the session waiting forever on a
    # `user.custom_tool_result` we never sent — a deadlock. The official MA
    # consolidation pattern (event-stream.md "Reconnecting / replaying")
    # closes this: on every (re)connect we first `events.list(session_id)`
    # to pull the full server-side history and re-process anything whose id
    # we haven't seen, THEN tail the live stream deduping on the same set.
    # The set gates only applicative RE-processing (re-render, re-mirror);
    # the terminal/control branches (terminated, requires_action dispatch,
    # tool-result telemetry) always run so a terminal event present only in
    # the catch-up history is never skipped.
    seen_event_ids: set[str] = set()
    # Tool-result processing telemetry. Every event MA streams back carries
    # `processed_at` (ISO 8601 — null while queued, populated once the agent
    # picks it up). For our `user.custom_tool_result` events the round-trip
    # tells us how long the agent took to consume our response: a healthy
    # session shows sub-second deltas; multi-second values usually mean the
    # agent is rate-limited or blocked on an upstream call. We don't react
    # programmatically — just log so post-mortems on a slow turn can pinpoint
    # the stall without re-running the trace. Keys are the eid of the
    # original `agent.custom_tool_use`; value is the local `time.monotonic()`
    # at send time. Cleared on echo; entries that linger past the watchdog
    # are dropped silently with the rest of the loop state.
    pending_tool_results: dict[str, float] = {}
    # Stream watchdog: each .__anext__() is wrapped in asyncio.wait_for so an
    # SSE stall (Anthropic outage, dropped TCP without RST, slow keepalive)
    # surfaces as a clean close + WS notification instead of hanging the
    # session indefinitely. Window is per-event (settings.ma_stream_event_
    # _timeout_seconds, default 600 s) — generous enough that an Opus turn
    # with adaptive thinking can spend a minute before its first chunk.
    settings_for_watchdog = _rm.get_settings()
    stream_timeout = settings_for_watchdog.ma_stream_event_timeout_seconds
    # Bound consecutive recovery reconnects so a genuinely-dead session can't
    # spin forever. A clean run (no drop) never touches this budget. Each
    # successful event delivery resets it — only *consecutive* drops count.
    max_reconnects = getattr(
        settings_for_watchdog, "ma_stream_max_reconnects", 4
    )

    async def _resilient_events():
        """Yield MA events with lossless reconnect across recoverable drops.

        On the first iteration and on every reconnect, pull the server-side
        history via `events.list` and yield it (the caller dedupes via
        `seen_event_ids`) so the gap created by the drop is filled, THEN
        tail the live stream. On a recoverable drop (watchdog timeout,
        transport error, or the stream ending without a terminal event) we
        reconnect up to `max_reconnects` consecutive times. A clean
        `session.status_terminated` (signalled by the caller setting
        `terminal_seen`) or budget exhaustion ends the generator.

        WebSocketDisconnect is re-raised (client gone — not an MA fault).
        """
        reconnects = 0
        first_connect = True
        while True:
            # --- Catch-up pass: replay server-side history to fill any gap.
            # SKIPPED on the very first connect: session.py already replayed
            # the resumed session's history to the WS (via
            # `_replay_ma_history_to_ws`) before this forwarder started, and
            # the live stream only carries events emitted after it attaches —
            # so on first connect there's nothing to catch up, and re-listing
            # would re-render the just-replayed bubbles. On a RECONNECT it is
            # the ONLY way to recover events emitted during the gap, including
            # a pending requires_action that would otherwise deadlock the
            # session. `seen_event_ids` dedups any overlap with the live tail.
            if not first_connect:
                try:
                    hist = client.beta.sessions.events.list(session_id)
                    if hasattr(hist, "__aiter__"):
                        async for hev in hist:
                            yield hev
                    else:
                        page = await hist  # type: ignore[misc]
                        for hev in (getattr(page, "data", None) or list(page)):
                            yield hev
                except WebSocketDisconnect:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # events.list is best-effort catch-up; a failure here just
                    # means we lean on the live stream. Log and continue.
                    logger.warning(
                        "[Diag-MA] events.list catch-up failed session=%s: %s",
                        session_id, exc,
                    )
            first_connect = False

            # --- Live tail.
            dropped = False
            try:
                stream_ctx = await client.beta.sessions.events.stream(session_id)
                async with stream_ctx as stream:
                    stream_iter = stream.__aiter__()
                    while True:
                        try:
                            ev = await asyncio.wait_for(
                                stream_iter.__anext__(), timeout=stream_timeout,
                            )
                        except StopAsyncIteration:
                            # Stream ended. If the caller already saw a
                            # terminal event, this is a clean end — stop.
                            # Otherwise treat it as a recoverable drop and
                            # reconnect (the session may still be live with
                            # a pending action).
                            dropped = not _terminal_seen["v"]
                            break
                        except TimeoutError:
                            logger.warning(
                                "[Diag-MA] stream inactive for %.0fs — "
                                "session=%s; attempting lossless reconnect",
                                stream_timeout, session_id,
                            )
                            dropped = True
                            break
                        reconnects = 0  # a delivered event resets the budget
                        yield ev
            except WebSocketDisconnect:
                # Client window closed mid-stream — bubble up so the caller's
                # asyncio.wait observes completion and the symmetric forwarder
                # shuts down too. Not an MA-side error.
                raise
            except Exception as exc:  # noqa: BLE001 — SSE transport collapse
                # Transport-level failure (TLS reset, ConnectionError,
                # APIStatusError mid-stream). Recoverable: reconnect.
                logger.warning(
                    "[Diag-MA] stream transport failed session=%s exc=%s — "
                    "attempting lossless reconnect",
                    session_id, type(exc).__name__,
                )
                dropped = True

            if _terminal_seen["v"]:
                return
            if not dropped:
                return
            reconnects += 1
            if reconnects > max_reconnects:
                logger.warning(
                    "[Diag-MA] exhausted %d reconnect attempts session=%s — "
                    "giving up; session may be dead",
                    max_reconnects, session_id,
                )
                try:
                    await ws.send_json({
                        "type": "stream_error",
                        "session_id": session_id,
                        "error": "reconnect_exhausted",
                        "message": (
                            f"Stream dropped and {max_reconnects} reconnect "
                            "attempts failed — session may be lost."
                        ),
                    })
                except Exception:  # noqa: BLE001
                    pass
                return
            logger.info(
                "[Diag-MA] reconnecting stream session=%s (attempt %d/%d)",
                session_id, reconnects, max_reconnects,
            )

    # Caller-visible terminal flag, mutated from inside the dispatch chain so
    # `_resilient_events` knows a `session.status_terminated` arrived and a
    # subsequent StopAsyncIteration is a clean end, not a recoverable drop.
    # A 1-element dict so the closure can mutate it without `nonlocal`.
    _terminal_seen = {"v": False}

    async for event in _resilient_events():
            etype = getattr(event, "type", None)

            # Lossless-reconnect dedup gate. `_already_seen` is True when this
            # exact event id was already processed (live or in an earlier
            # catch-up pass). Applicative RENDER branches below skip on a
            # repeat; terminal/control branches ignore this flag and always
            # run (so a terminal or pending-action event present only in the
            # catch-up history is never dropped). Events without an id (rare
            # span markers) always process — they carry no dedup key.
            _eid_for_dedup = getattr(event, "id", None)
            if _eid_for_dedup is not None and _eid_for_dedup in seen_event_ids:
                _already_seen = True
            else:
                _already_seen = False
                if _eid_for_dedup is not None:
                    seen_event_ids.add(_eid_for_dedup)

            if etype == "agent.message":
                if _already_seen:
                    # Catch-up replay of an already-rendered turn — don't
                    # double-post the bubble or re-mirror it to JSONL.
                    continue
                for block in getattr(event, "content", None) or []:
                    if getattr(block, "type", None) == "text":
                        clean, unknown = sanitize_agent_text(block.text, session_state.board)
                        if unknown:
                            logger.warning("sanitizer wrapped unknown refdes: %s", unknown)
                        await ws.send_json({"type": "message", "role": "assistant", "text": clean})
                        _mirror_jsonl(
                            device_slug=device_slug,
                            repair_id=repair_id,
                            conv_id=conv_id,
                            memory_root=memory_root,
                            event={
                                "role": "assistant",
                                "content": [{"type": "text", "text": clean}],
                            },
                        )

            elif etype == "agent.thinking":
                if _already_seen:
                    continue
                # MA surfaces summarized thinking text on this event when the
                # configured model supports adaptive thinking (Opus 4.6/4.7/4.8,
                # Sonnet 4.6 — all enabled by default server-side; the agent
                # config doesn't expose a `thinking` knob, see bootstrap docs).
                # Empty `text` means MA emitted the marker but the model chose
                # `display: omitted` for that block — skip.
                text = getattr(event, "text", "") or ""
                if text:
                    await ws.send_json({"type": "thinking", "text": text})

            elif etype == "span.model_request_end":
                if _already_seen:
                    # Don't re-emit turn_cost on catch-up — it would
                    # double-count the lifetime cost chip and re-touch the
                    # conv's accumulated cost on disk.
                    continue
                # MA attaches token usage to the span terminator. The model
                # name may or may not be carried on model_usage across SDK
                # versions — fall back to the tier-configured agent model
                # (claude-haiku-4-5 / sonnet-4-6 / opus-4-8) so pricing still
                # resolves.
                usage = getattr(event, "model_usage", None)
                if usage is not None:
                    model_label = (
                        getattr(usage, "model", None)
                        or getattr(event, "model", None)
                        or agent_model
                    )
                    in_tok = getattr(usage, "input_tokens", 0) or 0
                    out_tok = getattr(usage, "output_tokens", 0) or 0
                    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
                    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
                    cost = compute_turn_cost(
                        model_label,
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        cache_read_input_tokens=cache_read,
                        cache_creation_input_tokens=cache_write,
                    )
                    # Per-turn cache hit rate (read / total prompt-tokens). Useful
                    # to confirm the warm-up + 4-store layered prompt actually
                    # pays off across resumed sessions.
                    total_prompt = in_tok + cache_read + cache_write
                    hit_rate: float | None = None
                    if total_prompt > 0:
                        hit_rate = (cache_read / total_prompt) * 100.0
                        logger.info(
                            "[CacheRate] session=%s tier=%s rate=%.1f%% (read=%d total=%d)",
                            session_id,
                            tier,
                            hit_rate,
                            cache_read,
                            total_prompt,
                        )
                    # Surface the per-turn cache hit rate so the front can render
                    # a cache-hit chip. `cost` already carries the raw
                    # cache_read/creation token counts (from compute_turn_cost);
                    # this adds the derived ratio (percent, 0-100, null when no
                    # prompt tokens). Additive only — no existing key is touched.
                    cost_with_cache = {
                        **cost,
                        "cache_hit_rate": (
                            round(hit_rate, 1) if hit_rate is not None else None
                        ),
                    }
                    await ws.send_json({"type": "turn_cost", **cost_with_cache})
                    if repair_id and conv_id:
                        # Defensive: in normal flow `_forward_ws_to_session`
                        # has already materialized on the user message that
                        # triggered this turn, but call it again so a cost
                        # event never lands against an unindexed conv slot.
                        if pending_conv is not None:
                            pending_conv.materialize_now()
                        touch_conversation(
                            device_slug=device_slug,
                            repair_id=repair_id,
                            conv_id=conv_id,
                            cost_usd=cost.get("cost_usd") if isinstance(cost, dict) else None,
                            model=model_label,
                            memory_root=memory_root,
                        )
                    # T13 — report this LLM call's raw token usage to the cloud
                    # (the tenant-private billing unit). Best-effort + no-op when
                    # unconfigured (self-host never phones home). The catch-up
                    # replay path already `continue`d above on `_already_seen`, so
                    # a re-seen span never double-reports; the cloud also dedups on
                    # the {session_id}:{event.id} event_id as a second guard.
                    # Only report a span that carries an id — it's the cloud's
                    # idempotency key. Id-less spans (rare markers) would all
                    # collapse to "{session}:None" and silently dedup to a single
                    # ledger row → undercount; skipping them is the
                    # customer-favourable choice (and they rarely carry usage).
                    _event_id = getattr(event, "id", None)
                    if _event_id is not None:
                        cloud_metering.fire_and_forget_report(
                            owner_ref=current_owner_ref(),
                            model=model_label,
                            input_tokens=in_tok,
                            output_tokens=out_tok,
                            # Already computed above for the turn cost + hit-rate
                            # log; forward them so the cloud prices cache reads
                            # (0.1x) and writes (1.25x) at their own tiers instead
                            # of billing the whole turn as full input.
                            cache_read_input_tokens=cache_read,
                            cache_creation_input_tokens=cache_write,
                            engine_repair_id=repair_id,
                            event_id=f"{session_id}:{_event_id}",
                        )

            elif etype == "agent.custom_tool_use":
                # ALWAYS cache by id (even on catch-up) — `requires_action`
                # below looks the tool up here, and a tool emitted during a
                # stream gap must be dispatchable on reconnect, or the
                # session deadlocks waiting on a result we can't form.
                events_by_id[event.id] = event
                tool_name = getattr(event, "name", None)
                tool_input = getattr(event, "input", {}) or {}
                if _already_seen:
                    # Already rendered live — skip the WS echo + JSONL mirror,
                    # but the cache update above still ran.
                    continue
                await ws.send_json(
                    {
                        "type": "tool_use",
                        "name": tool_name,
                        "input": tool_input,
                    }
                )
                _mirror_jsonl(
                    device_slug=device_slug,
                    repair_id=repair_id,
                    conv_id=conv_id,
                    memory_root=memory_root,
                    event={
                        "role": "assistant",
                        "content": [{
                            "type": "tool_use",
                            "id": getattr(event, "id", None),
                            "name": tool_name,
                            "input": tool_input,
                        }],
                    },
                )

            elif etype == "agent.tool_use":
                if _already_seen:
                    continue
                # MA-native memory_* tools (memory_search / memory_list /
                # memory_read / memory_write) are dispatched server-side by
                # Anthropic, not by our runtime. Surface them on the WS so
                # benchmarks can attribute cost — inference tokens don't
                # include the per-op memory charges Anthropic bills on top.
                await ws.send_json(
                    {
                        "type": "memory_tool_use",
                        "name": getattr(event, "name", None),
                        "input": getattr(event, "input", {}) or {},
                    }
                )

            elif etype == "session.status_idle":
                stop = getattr(event, "stop_reason", None)
                stop_type = getattr(stop, "type", None) if stop is not None else None
                if stop_type != "requires_action":
                    # Agent finished its tech-turn and is waiting for the
                    # next user.message. Expose this as an explicit signal
                    # for WS clients that need to know when it's safe to
                    # send the next user input (bench scripts, automated
                    # tests). UI chat clients can ignore it.
                    await ws.send_json(
                        {
                            "type": "turn_complete",
                            "stop_reason": stop_type,
                        }
                    )
                    continue
                event_ids = getattr(stop, "event_ids", None) or []
                for eid in event_ids:
                    if eid in responded_tool_ids:
                        # MA re-emitted a requires_action whose event_ids
                        # include ones we already responded to. Skip —
                        # responding twice yields HTTP 400.
                        continue
                    tool_event = events_by_id.get(eid)
                    if tool_event is None:
                        logger.warning("[Diag-MA] requires_action for unknown event id %s", eid)
                        continue
                    name = getattr(tool_event, "name", "")
                    payload = getattr(tool_event, "input", {}) or {}

                    # mb_expand_knowledge: route through the MA
                    # KnowledgeCurator sub-agent instead of the inline
                    # Scout `messages.create`. The curator does the focused
                    # research; the existing Registry + Clinicien validate
                    # and merge the chunk into rules.json.
                    if name == "mb_expand_knowledge":
                        # Plan gate (defence in depth): the managed manifest is
                        # baked at agent bootstrap, so a free tenant's agent may
                        # still emit this call. Refuse here — no Curator session,
                        # no Scout spend — and feed a typed result back so the
                        # agent relays the limitation instead of stalling.
                        if not current_can_expand():
                            await client.beta.sessions.events.send(
                                session_id,
                                events=[{
                                    "type": "user.custom_tool_result",
                                    "custom_tool_use_id": eid,
                                    "content": [{
                                        "type": "text",
                                        "text": _safe_tool_result_text({
                                            "ok": False,
                                            "expanded": False,
                                            "reason": "plan_gated",
                                            "error": "Pack enrichment requires a paid plan.",
                                        }),
                                    }],
                                }],
                            )
                            responded_tool_ids.add(eid)
                            continue
                        from api.pipeline.expansion import expand_pack

                        focus_symptoms = list(payload.get("focus_symptoms") or [])
                        focus_refdes = list(payload.get("focus_refdes") or [])

                        async def _curator_provider(
                            *,
                            device_label: str,
                            focus_symptoms: list[str],
                            focus_refdes: list[str],
                        ) -> str:
                            return await _run_knowledge_curator(
                                client=client,
                                device_label=device_label,
                                focus_symptoms=focus_symptoms,
                                focus_refdes=focus_refdes,
                                environment_id=environment_id,
                                parent_session_id=session_id,
                                ws=ws,
                            )

                        try:
                            # T8 : propage l'owner_ref de la session (curator
                            # path) → added_by_tenant dans la provenance des
                            # facts promus. Ferme le résidu de fuite T6.
                            # (current_owner_ref is imported at module top.)
                            expand_result = await expand_pack(
                                device_slug=device_slug,
                                focus_symptoms=focus_symptoms,
                                focus_refdes=focus_refdes,
                                client=client,
                                memory_root=memory_root,
                                chunk_provider=_curator_provider,
                                owner_ref=current_owner_ref(),
                            )
                            expand_result["ok"] = True
                            if session_state is not None:
                                session_state.invalidate_pack_cache(device_slug)
                            # Sync the MA memory store mount with the freshly
                            # expanded pack so the agent's mount-based reads
                            # (grep on /mnt/memory/wrench-board-{slug}/) see the
                            # new rules + registry mid-session, not just on
                            # the next session-create. Custom mb_* tools see
                            # the changes immediately via the cache invalidate
                            # above; this closes the gap on the mount path.
                            try:
                                from api.agent.memory_seed import (
                                    seed_memory_store_from_pack,
                                )
                                sync_status = await seed_memory_store_from_pack(
                                    client=client,
                                    device_slug=device_slug,
                                    pack_dir=memory_root / device_slug,
                                    only_files=["rules.json", "registry.json"],
                                )
                                seeded = [
                                    p for p, s in sync_status.items()
                                    if s == "seeded"
                                ]
                                logger.info(
                                    "[Curator] mount sync slug=%s seeded=%s",
                                    device_slug,
                                    seeded,
                                )
                            except Exception as sync_exc:  # noqa: BLE001
                                logger.warning(
                                    "[Curator] memory store sync failed "
                                    "(non-critical): %s",
                                    sync_exc,
                                )
                        except Exception as exc:  # noqa: BLE001
                            logger.exception(
                                "[Curator] expand_pack failed device=%s",
                                device_slug,
                            )
                            expand_result = {
                                "ok": False,
                                "expanded": False,
                                "reason": type(exc).__name__,
                                "error": str(exc)[:300],
                            }

                        await ws.send_json({
                            "type": "knowledge_expanded",
                            "ok": bool(expand_result.get("ok")),
                            "stats": {
                                k: v for k, v in expand_result.items()
                                if k in (
                                    "new_rules_count",
                                    "new_components_count",
                                    "new_signals_count",
                                    "total_rules_after",
                                    "dump_bytes_added",
                                )
                            },
                        })
                        await client.beta.sessions.events.send(
                            session_id,
                            events=[{
                                "type": "user.custom_tool_result",
                                "custom_tool_use_id": eid,
                                "content": [{
                                    "type": "text",
                                    "text": _safe_tool_result_text(expand_result),
                                }],
                            }],
                        )
                        responded_tool_ids.add(eid)
                        continue

                    # consult_specialist is async (spawns a fresh MA session
                    # on another tier and streams its events). Intercept
                    # before _dispatch_tool because the helper needs the
                    # parent session's environment + tier in closure.
                    if name == "consult_specialist":
                        requested_tier = str(payload.get("tier", "")).strip()
                        if not requested_tier:
                            sub_result = {
                                "ok": False,
                                "reason": "missing-tier",
                                "error": "tier is required",
                            }
                        elif requested_tier == tier:
                            sub_result = {
                                "ok": False,
                                "reason": "self-consultation",
                                "error": (
                                    f"refusing to consult tier={requested_tier} "
                                    "from itself — pick a different tier"
                                ),
                            }
                        else:
                            sub_result = await _run_subagent_consultation(
                                client=client,
                                tier=requested_tier,  # type: ignore[arg-type]
                                query=str(payload.get("query", "")),
                                context=payload.get("context"),
                                environment_id=environment_id,
                                parent_session_id=session_id,
                            )
                        await ws.send_json({
                            "type": "subagent_result",
                            "tier": requested_tier,
                            "ok": bool(sub_result.get("ok")),
                        })
                        await client.beta.sessions.events.send(
                            session_id,
                            events=[{
                                "type": "user.custom_tool_result",
                                "custom_tool_use_id": eid,
                                "content": [{
                                    "type": "text",
                                    "text": _safe_tool_result_text(sub_result),
                                }],
                            }],
                        )
                        responded_tool_ids.add(eid)
                        continue

                    # cam_capture is async (round-trips to the frontend) and
                    # produces its own user.custom_tool_result. Intercept
                    # before the generic _dispatch_tool which wouldn't know
                    # how to handle the WS round-trip.
                    #
                    # Track via session_mirrors (not bare create_task) so a
                    # WS close before the round-trip completes drains the
                    # task instead of orphaning it. The eid goes into the
                    # dedup set IMMEDIATELY to block MA from re-dispatching
                    # while the capture is in flight; on crash we DISCARD
                    # the eid in the done callback so MA's next
                    # `requires_action` re-emit gets a real retry instead of
                    # being silently swallowed. Without the rollback, a
                    # camera dispatch failure would permablock the tool_use:
                    # responded_tool_ids would say "answered" but no
                    # user.custom_tool_result ever reached MA, leaving the
                    # session waiting forever.
                    if name == "cam_capture":
                        cam_eid = eid

                        def _release_eid_on_failure(
                            task: asyncio.Task,
                            *,
                            eid: str = cam_eid,
                        ) -> None:
                            if task.cancelled():
                                responded_tool_ids.discard(eid)
                                logger.warning(
                                    "[Diag-MA] cam_capture cancelled for "
                                    "eid=%s — released for retry",
                                    eid,
                                )
                                return
                            exc = task.exception()
                            if exc is not None:
                                responded_tool_ids.discard(eid)
                                logger.warning(
                                    "[Diag-MA] cam_capture crashed for "
                                    "eid=%s — released for retry: %s",
                                    eid,
                                    exc,
                                )

                        responded_tool_ids.add(cam_eid)
                        cam_task = session_mirrors.spawn(_dispatch_cam_capture(
                            client=client,
                            session=session_state,
                            ws=ws,
                            memory_root=memory_root,
                            slug=device_slug,
                            repair_id=repair_id or "default",
                            ma_session_id=session_id,
                            tool_use_id=cam_eid,
                            tool_input=payload,
                        ))
                        cam_task.add_done_callback(_release_eid_on_failure)
                        continue

                    # bv_propose_protocol — Pattern 4 round-trip with tech.
                    # The runtime emits `protocol_pending_confirmation`, the
                    # UI modal accepts/rejects, and only an accept dispatches
                    # the actual tool. Same crash-rollback discipline as
                    # cam_capture: eid goes into the dedup set IMMEDIATELY,
                    # but the done callback releases it on cancellation /
                    # crash so MA's next requires_action re-emit gets a real
                    # retry instead of a permablock.
                    if name == "bv_propose_protocol":
                        proto_eid = eid

                        def _release_proto_on_failure(
                            task: asyncio.Task,
                            *,
                            eid: str = proto_eid,
                        ) -> None:
                            if task.cancelled():
                                responded_tool_ids.discard(eid)
                                logger.warning(
                                    "[Diag-MA] propose_protocol cancelled "
                                    "for eid=%s — released for retry",
                                    eid,
                                )
                                return
                            exc = task.exception()
                            if exc is not None:
                                responded_tool_ids.discard(eid)
                                logger.warning(
                                    "[Diag-MA] propose_protocol crashed for "
                                    "eid=%s — released for retry: %s",
                                    eid,
                                    exc,
                                )

                        responded_tool_ids.add(proto_eid)
                        proto_task = session_mirrors.spawn(
                            _dispatch_protocol_with_confirmation(
                                client=client,
                                session=session_state,
                                ws=ws,
                                memory_root=memory_root,
                                device_slug=device_slug,
                                repair_id=repair_id,
                                conv_id=conv_id,
                                ma_session_id=session_id,
                                tool_use_id=proto_eid,
                                tool_input=payload,
                                session_mirrors=session_mirrors,
                            )
                        )
                        proto_task.add_done_callback(_release_proto_on_failure)
                        continue

                    result = await _rm._dispatch_tool(
                        name,
                        payload,
                        device_slug,
                        memory_root,
                        client,
                        session_state,
                        session_id,
                        repair_id=repair_id,
                        session_mirrors=session_mirrors,
                        conv_id=conv_id,
                    )
                    # Emit the WS event(s) if the dispatch succeeded. Atomic
                    # tools return `event` (single), composites like bv_scene
                    # return `events` (list); fan both out as individual WS
                    # frames so the frontend stays oblivious.
                    single_event = result.get("event")
                    multi_events = (
                        result.get("events")
                        if isinstance(result.get("events"), list)
                        else None
                    )
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
                                ev if isinstance(ev, dict)
                                else ev.model_dump(by_alias=True)
                            )
                            emitted_any = True
                    if emitted_any and name.startswith("bv_"):
                        # Snapshot board overlay after every successful bv_*
                        # mutation so a WS reconnect can replay highlights /
                        # annotations / focus instead of showing a bare board
                        # while the chat references "I highlighted U7 for you".
                        from api.agent.board_state import save_board_state
                        save_board_state(
                            memory_root=memory_root,
                            device_slug=device_slug,
                            repair_id=repair_id,
                            session=session_state,
                            conv_id=conv_id,
                        )
                    result_for_agent = {k: v for k, v in result.items() if k not in ("event", "events")}
                    pending_tool_results[eid] = time.monotonic()
                    await client.beta.sessions.events.send(
                        session_id,
                        events=[
                            {
                                "type": "user.custom_tool_result",
                                "custom_tool_use_id": eid,
                                "content": [
                                    {
                                        "type": "text",
                                        "text": _safe_tool_result_text(result_for_agent),
                                    }
                                ],
                            }
                        ],
                    )
                    responded_tool_ids.add(eid)

            elif etype == "user.custom_tool_result":
                # MA echoes user-sent events back on the stream — first with
                # `processed_at: null` (queued), then with a timestamp once
                # the agent picked up our response. Both arrive after our own
                # `events.send`, so the second copy gives us the agent's
                # consumption latency. Useful for diagnosing slow turns: a
                # healthy session shows sub-second deltas; multi-second
                # values usually mean the agent is rate-limited or blocked
                # on an upstream call. Strictly observational — no retry,
                # no failover, just a log line.
                processed_at = getattr(event, "processed_at", None)
                if processed_at is None:
                    continue
                eid = getattr(event, "custom_tool_use_id", None)
                sent_at = pending_tool_results.pop(eid, None) if eid else None
                if sent_at is None:
                    continue
                delay = time.monotonic() - sent_at
                if delay >= 5.0:
                    logger.warning(
                        "[Diag-MA] tool_result consumed slowly session=%s "
                        "eid=%s delay=%.2fs",
                        session_id,
                        eid,
                        delay,
                    )
                else:
                    logger.info(
                        "[Diag-MA] tool_result consumed session=%s eid=%s "
                        "delay=%.2fs",
                        session_id,
                        eid,
                        delay,
                    )

            elif etype == "session.status_terminated":
                # Mark terminal so the resilient generator treats the stream
                # ending as a clean close (no reconnect). Always runs, even on
                # a catch-up replay — a terminated event present only in the
                # history must still end the loop.
                _terminal_seen["v"] = True
                await ws.send_json({"type": "session_terminated"})
                return

            elif etype == "session.error":
                err = getattr(event, "error", None)
                msg = getattr(err, "message", None) if err is not None else None
                # Dump the full event so the next "An internal service error
                # occurred." surfaces with enough context to act on (MA error
                # type, request_id if present, the raw event payload). Without
                # this, the frontend shows the user a wall and we have no log
                # to bisect transient-MA-hiccup vs. our-own-bug.
                err_type = getattr(err, "type", None) if err is not None else None
                request_id = getattr(event, "request_id", None) or (
                    getattr(err, "request_id", None) if err is not None else None
                )
                try:
                    raw = event.model_dump() if hasattr(event, "model_dump") else repr(event)
                except Exception:  # noqa: BLE001
                    raw = repr(event)
                logger.error(
                    "[Diag-MA] session.error session=%s err_type=%s msg=%s "
                    "request_id=%s raw=%s",
                    session_id,
                    err_type,
                    msg,
                    request_id,
                    raw,
                )
                await ws.send_json({"type": "error", "text": msg or "session error"})
