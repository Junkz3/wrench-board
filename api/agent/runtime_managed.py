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
import base64 as _b64
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Literal

from anthropic import AsyncAnthropic
from fastapi import WebSocket, WebSocketDisconnect

from api.agent.chat_history import (
    append_event,
    build_ctx_tag,
    build_session_intro,
    ensure_conversation,
    get_conversation_tier,
    list_conversations,
    load_events,
    load_ma_session_id,
    materialize_conversation,
    save_ma_session_id,
    strip_ctx_tag,
    touch_conversation,
)
from api.agent.dispatch_bv import dispatch_bv
from api.agent.macros import persist_macro
from api.agent.managed_ids import get_agent, load_managed_ids
from api.agent.memory_stores import (
    ensure_global_store,
    ensure_memory_store,
    ensure_repair_store,
)
from api.agent.pricing import compute_turn_cost
from api.agent.sanitize import sanitize_agent_text
from api.agent.session_start_mode import (
    SessionStartMode,
    decide_session_start_mode,
)
from api.agent.tools import (
    mb_expand_knowledge,
    mb_get_component,
    mb_get_rules_for_symptoms,
    mb_record_finding,
)
from api.config import get_settings
from api.session.state import SessionState
from api.tools.schematic import mb_schematic_graph

TierLiteral = Literal["fast", "normal", "deep"]
DEFAULT_TIER: TierLiteral = "fast"

logger = logging.getLogger("wrench_board.agent.managed")

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


class _SessionMirrors:
    """Tracks fire-and-forget mirror tasks and awaits them on session close."""

    def __init__(self) -> None:
        self._pending: set[asyncio.Task] = set()

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return task

    async def wait_drain(self, timeout: float = 5.0) -> None:
        if not self._pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._pending, return_exceptions=True),
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning(
                "[Diag-MA] %d mirror tasks still pending after %.1fs — cancelling",
                len(self._pending),
                timeout,
            )
            for task in list(self._pending):
                task.cancel()


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
    and shorter than the ~30 d the docs imply — see real loss of a 31-turn
    iPhone repair conv on 2026-04-26 where `events.list` returned empty).
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


async def _run_subagent_consultation(
    *,
    client: AsyncAnthropic,
    tier: TierLiteral,
    query: str,
    context: str | None,
    environment_id: str,
    parent_session_id: str | None,
    timeout_s: float = 120.0,
) -> dict:
    """Spawn an MA sub-agent on `tier`, ask it `query`, return its text.

    The sub-agent runs in its own MA session with the tier-scoped agent
    config. Custom tool calls from the sub-agent are refused (returned as
    errors) so the consultation stays bounded — the prompt explicitly tells
    it to answer from its model knowledge using the provided `context`.

    Returns a dict shaped like every other custom-tool result:
        {"ok": True, "tier": ..., "answer": "..."} on success
        {"ok": False, "reason": ..., "error": ...} on failure
    """
    try:
        sub_agent_info = get_agent(load_managed_ids(), tier=tier)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": "unknown-tier",
            "error": f"could not resolve tier={tier}: {exc}",
        }

    parts = []
    if context:
        parts.append(f"=== Context from main agent ===\n{context.strip()}")
    parts.append(f"=== Question ===\n{query.strip()}")
    parts.append(
        "=== Important ===\n"
        "You are running as an isolated consultation sub-agent. You do NOT "
        "have access to the main agent's memory bank, board, or repair "
        "scribe — answer from the context above plus your model knowledge. "
        "Do NOT call any custom tool; respond directly with your analysis."
    )
    prompt = "\n\n".join(parts)

    sub_session = None
    try:
        sub_session = await _sessions_create_with_retry(
            client,
            agent={
                "type": "agent",
                "id": sub_agent_info["id"],
                "version": sub_agent_info["version"],
            },
            environment_id=environment_id,
            title=(
                f"subagent-{tier}-from-{parent_session_id}"
                if parent_session_id
                else f"subagent-{tier}"
            ),
        )
        sub_session_id = sub_session.id
        logger.info(
            "[Subagent] spawned tier=%s session=%s parent=%s",
            tier,
            sub_session_id,
            parent_session_id,
        )

        answer_parts: list[str] = []
        events_cache: dict[str, Any] = {}
        responded: set[str] = set()

        stream_ctx = await client.beta.sessions.events.stream(sub_session_id)
        async with stream_ctx as stream:
            await client.beta.sessions.events.send(
                sub_session_id,
                events=[{
                    "type": "user.message",
                    "content": [{"type": "text", "text": prompt}],
                }],
            )

            async def _consume() -> None:
                async for event in stream:
                    etype = getattr(event, "type", None)
                    if etype == "agent.message":
                        for block in getattr(event, "content", []) or []:
                            if getattr(block, "type", None) == "text":
                                answer_parts.append(block.text)
                    elif etype == "agent.custom_tool_use":
                        events_cache[event.id] = event
                    elif etype == "session.status_idle":
                        stop = getattr(event, "stop_reason", None)
                        sr = getattr(stop, "type", None) if stop else None
                        if sr == "requires_action":
                            event_ids = getattr(stop, "event_ids", []) or []
                            refusals = []
                            for eid in event_ids:
                                if eid in responded:
                                    continue
                                refusals.append({
                                    "type": "user.custom_tool_result",
                                    "custom_tool_use_id": eid,
                                    "content": [{
                                        "type": "text",
                                        "text": (
                                            "Tools are disabled in consultant "
                                            "mode. Answer directly from the "
                                            "context provided."
                                        ),
                                    }],
                                    "is_error": True,
                                })
                                responded.add(eid)
                            if refusals:
                                await client.beta.sessions.events.send(
                                    sub_session_id, events=refusals
                                )
                            continue
                        # end_turn / retries_exhausted / etc — terminal
                        return
                    elif etype == "session.status_terminated":
                        return

            try:
                await asyncio.wait_for(_consume(), timeout=timeout_s)
            except TimeoutError:
                logger.warning(
                    "[Subagent] tier=%s session=%s timed out after %.1fs",
                    tier,
                    sub_session_id,
                    timeout_s,
                )

        answer = "\n".join(p for p in answer_parts if p).strip()
        if not answer:
            return {
                "ok": False,
                "reason": "no-output",
                "error": "sub-agent returned no text",
                "tier": tier,
            }
        return {"ok": True, "tier": tier, "answer": answer}

    except Exception as exc:  # noqa: BLE001
        logger.exception("[Subagent] consultation failed tier=%s", tier)
        return {
            "ok": False,
            "reason": "subagent-failed",
            "error": str(exc),
            "tier": tier,
        }
    finally:
        if sub_session is not None:
            try:
                await client.beta.sessions.archive(sub_session.id)
            except Exception:  # noqa: BLE001
                pass


async def _run_knowledge_curator(
    *,
    client: AsyncAnthropic,
    device_label: str,
    focus_symptoms: list[str],
    focus_refdes: list[str],
    environment_id: str,
    parent_session_id: str | None,
    ws: WebSocket | None = None,
    timeout_s: float = 180.0,
) -> str:
    """Spawn the bootstrapped KnowledgeCurator MA agent for a research run.

    Returns the curator's Markdown chunk (same shape as the inline Scout in
    `api.pipeline.expansion._run_targeted_scout`). Surfaces `agent.tool_use`
    events on `ws` if provided so the tech sees the live web_search queries.
    """
    try:
        curator_info = get_agent(load_managed_ids(), tier="curator")
    except RuntimeError as exc:
        raise RuntimeError(
            "knowledge_curator agent not bootstrapped — re-run "
            "scripts/bootstrap_managed_agent.py"
        ) from exc

    focus_block = "\n".join(f"  - {s}" for s in focus_symptoms)
    refdes_section = ""
    if focus_refdes:
        refdes_lines = "\n".join(f"  - {r}" for r in focus_refdes)
        refdes_section = f"\n\nFocus refdes:\n{refdes_lines}"

    prompt = (
        f"Device: {device_label}\n\n"
        f"Focus symptoms (target THESE only):\n{focus_block}"
        f"{refdes_section}\n\n"
        "Run a focused web research pass and produce the Markdown dump in "
        "your system-prompt format. 4-8 searches max, each scoped to one "
        "symptom + the device. Stop when you have 3-6 symptom blocks with "
        "traceable sources. Avoid topics already common knowledge — surface "
        "new failure-mode information for the focus symptoms only."
    )

    sub_session = None
    try:
        sub_session = await _sessions_create_with_retry(
            client,
            agent={
                "type": "agent",
                "id": curator_info["id"],
                "version": curator_info["version"],
            },
            environment_id=environment_id,
            title=(
                f"curator-from-{parent_session_id}"
                if parent_session_id
                else "curator"
            ),
        )
        sub_session_id = sub_session.id
        logger.info(
            "[Curator] spawned session=%s for device=%r focus=%s",
            sub_session_id,
            device_label,
            focus_symptoms,
        )
        if ws is not None:
            await ws.send_json({
                "type": "subagent_spawned",
                "role": "curator",
                "session_id": sub_session_id,
            })

        chunks: list[str] = []
        stream_ctx = await client.beta.sessions.events.stream(sub_session_id)
        async with stream_ctx as stream:
            await client.beta.sessions.events.send(
                sub_session_id,
                events=[{
                    "type": "user.message",
                    "content": [{"type": "text", "text": prompt}],
                }],
            )

            async def _consume() -> None:
                async for event in stream:
                    etype = getattr(event, "type", None)
                    if etype == "agent.message":
                        for block in getattr(event, "content", []) or []:
                            if getattr(block, "type", None) == "text":
                                chunks.append(block.text)
                    elif etype == "agent.tool_use" and ws is not None:
                        # Server-side tools (web_search, web_fetch).
                        # Mirror to ws so the tech sees the live research.
                        await ws.send_json({
                            "type": "subagent_tool_use",
                            "role": "curator",
                            "name": getattr(event, "name", None),
                            "input": getattr(event, "input", {}) or {},
                        })
                    elif etype == "session.status_idle":
                        stop = getattr(event, "stop_reason", None)
                        sr = getattr(stop, "type", None) if stop else None
                        if sr != "requires_action":
                            return
                    elif etype == "session.status_terminated":
                        return

            try:
                await asyncio.wait_for(_consume(), timeout=timeout_s)
            except TimeoutError:
                logger.warning(
                    "[Curator] session=%s timed out after %.1fs",
                    sub_session_id,
                    timeout_s,
                )

        return "\n".join(c for c in chunks if c).strip()

    finally:
        if sub_session is not None:
            try:
                await client.beta.sessions.archive(sub_session.id)
            except Exception:  # noqa: BLE001
                pass


async def _dispatch_tool(
    name: str,
    payload: dict,
    device_slug: str,
    memory_root: Path,
    client: AsyncAnthropic,
    session: SessionState,
    session_id: str | None = None,
    repair_id: str | None = None,
    session_mirrors: _SessionMirrors | None = None,
    conv_id: str | None = None,
) -> dict:
    """Run a custom tool locally and return the raw result.

    Routes bv_* → dispatch_bv (synchronous), mb_* → their Python handlers.
    The returned dict may contain a Pydantic `event` field — the caller is
    responsible for emitting it on the WS and stripping it from the agent
    tool_result.
    """
    if name.startswith("profile_"):
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
        return {"ok": False, "reason": "unknown-tool", "error": f"unknown profile tool: {name}"}
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

    if name.startswith("bv_"):
        return dispatch_bv(session, name, payload)
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
            repair_id=repair_id or "",
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
            repair_id=payload.get("repair_id") or repair_id,
        )
    if name == "mb_record_measurement":
        from api.tools.measurements import mb_record_measurement as _mb_rec

        return _mb_rec(
            device_slug=device_slug,
            repair_id=repair_id or "",
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
            repair_id=repair_id or "",
            memory_root=memory_root,
            target=payload.get("target"),
            since=payload.get("since"),
        )
    if name == "mb_compare_measurements":
        from api.tools.measurements import mb_compare_measurements as _mb_cmp

        return _mb_cmp(
            device_slug=device_slug,
            repair_id=repair_id or "",
            memory_root=memory_root,
            target=payload.get("target", ""),
            before_ts=payload.get("before_ts"),
            after_ts=payload.get("after_ts"),
        )
    if name == "mb_observations_from_measurements":
        from api.tools.measurements import mb_observations_from_measurements as _mb_syn

        return _mb_syn(
            device_slug=device_slug,
            repair_id=repair_id or "",
            memory_root=memory_root,
        )
    if name == "mb_set_observation":
        from api.tools.measurements import mb_set_observation as _mb_set

        return _mb_set(
            device_slug=device_slug,
            repair_id=repair_id or "",
            memory_root=memory_root,
            target=payload.get("target", ""),
            mode=payload.get("mode", "unknown"),
        )
    if name == "mb_clear_observations":
        from api.tools.measurements import mb_clear_observations as _mb_clr

        return _mb_clr(
            device_slug=device_slug,
            repair_id=repair_id or "",
            memory_root=memory_root,
        )
    if name == "mb_validate_finding":
        from api.tools.validation import (
            mb_validate_finding as _mb_val,
        )
        from api.tools.validation import (
            mirror_outcome_to_memory,
        )

        result = _mb_val(
            device_slug=device_slug,
            repair_id=repair_id or "",
            memory_root=memory_root,
            fixes=payload.get("fixes", []),
            tech_note=payload.get("tech_note"),
            agent_confidence=payload.get("agent_confidence", "high"),
        )
        # Fire-and-forget: mirror the validated outcome into the device's
        # MA memory store so future repair sessions can `memory_search` it.
        # Kept off the critical path — the tool's response to the agent
        # doesn't wait for the HTTP upsert to complete.
        # session_mirrors ensures the task is awaited on WS close so a
        # fast disconnect doesn't cancel it mid-flight.
        if result.get("validated") and repair_id:
            if session_mirrors is None:
                raise RuntimeError(
                    "mb_validate_finding dispatch requires session_mirrors; "
                    "this path is only valid from run_diagnostic_session_managed"
                )
            session_mirrors.spawn(
                mirror_outcome_to_memory(
                    client=client,
                    device_slug=device_slug,
                    repair_id=repair_id,
                    memory_root=memory_root,
                )
            )
        return result
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
    return {"ok": False, "reason": "unknown-tool", "error": f"unknown tool: {name}"}


async def maybe_auto_seed(
    *,
    client: AsyncAnthropic,
    device_slug: str,
    memory_root: Path,
    session_mirrors: _SessionMirrors | None = None,
) -> asyncio.Task | None:
    """Launch a background re-seed of pack files that drifted since last seed.

    Returns the spawned task so callers can optionally await it (e.g. in tests).
    In the normal session path the task is fire-and-forget; its failure is
    logged and the next session open will retry.
    """
    from api.agent.memory_seed import (
        seed_memory_store_from_pack,
        stale_files_for_pack,
    )

    settings = get_settings()
    if not settings.ma_memory_store_enabled:
        return None
    pack_dir = memory_root / device_slug
    if not pack_dir.exists():
        return None
    stale = stale_files_for_pack(pack_dir)
    if not stale:
        return None

    async def _run():
        try:
            await seed_memory_store_from_pack(
                client=client,
                device_slug=device_slug,
                pack_dir=pack_dir,
                only_files=stale,
            )
            logger.info(
                "[Diag-MA] auto-seeded slug=%s files=%s",
                device_slug,
                stale,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Diag-MA] auto-seed failed slug=%s files=%s: %s",
                device_slug,
                stale,
                exc,
            )

    if session_mirrors is not None:
        return session_mirrors.spawn(_run())
    return asyncio.create_task(_run())


async def run_diagnostic_session_managed(
    ws: WebSocket,
    device_slug: str,
    tier: TierLiteral = DEFAULT_TIER,
    repair_id: str | None = None,
    conv_id: str | None = None,
) -> None:
    """Open a Managed Agents session on the tier-scoped agent and relay it to `ws`.

    `tier` picks which agent (fast=Haiku, normal=Sonnet, deep=Opus) handles the
    conversation. A new WS connection with a different tier = a fresh MA session
    on that tier's agent. No in-session swap: by design, tier choice is explicit
    and the user starts a new conversation when changing it.

    MA persists the full event stream server-side, so the happy-path replay
    on resume pulls from `client.beta.sessions.events.list(sid)` rather than
    from the local JSONL. The JSONL under `memory/{slug}/repairs/{repair_id}/
    conversations/{conv}/messages.jsonl` is still written live as an on-disk
    mirror — used for UI re-rendering on reconnect when MA's event stream is
    inaccessible (checkpoint expired after 30 d idle, Anthropic outage, etc.).
    That mirror is the reason JSONL keeps being written — the technician's
    repair history (chat bubbles in the UI) survives even if the managed
    session is gone. Semantic context for the agent now comes from the
    per-repair scribe mount instead of an LLM-summarized recap.
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

    try:
        ids = load_managed_ids()
        agent_info = get_agent(ids, tier)
    except RuntimeError as exc:
        await ws.accept()
        await ws.send_json({"type": "error", "text": str(exc)})
        await ws.close()
        return

    client = AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=settings.anthropic_max_retries)  # noqa: E501
    session_mirrors = _SessionMirrors()
    memory_root = Path(settings.memory_root)

    # Layered MA memory — provision up to 4 stores per session:
    #   1. global-patterns   (RO) — cross-device failure archetypes
    #   2. global-playbooks  (RO) — protocol templates
    #   3. device-{slug}     (RW) — knowledge pack + field reports
    #   4. repair-{repair_id} (RW) — agent's working notes (scribe layer)
    # Each surfaces as /mnt/memory/<store-name>/ inside the session container.
    # See docs/superpowers/plans/2026-04-26-ma-memory-layered-architecture.md

    PATTERNS_DESC_RUNTIME = (
        "Cross-device failure archetypes for board-level diagnostics: "
        "short-to-GND on power rails, thermal cascade failures, BGA "
        "solder ball lift, bench anti-patterns. Markdown documents "
        "under /patterns/<id>.md. Read this store first when the "
        "device-specific rules return 0 matches."
    )
    PLAYBOOKS_DESC_RUNTIME = (
        "Diagnostic protocol templates conformant to bv_propose_protocol's "
        "schema. JSON documents under /playbooks/<id>.json indexed by "
        "symptom (boot-no-power, usb-no-charge, pmic-rail-collapse). "
        "Reference these BEFORE synthesizing a protocol from scratch — "
        "they are field-tested."
    )

    # Collect any store-provisioning failures so the WS layer can tell
    # the technician they're operating with a degraded memory layer.
    # Without this signal the agent silently runs without its scribe
    # mount or its global-patterns / global-playbooks references — the
    # tech would see "session ready" and have no idea memory was off.
    # Each entry: {"store": "device|repair|patterns|playbooks", "error": "<msg>"}.
    memory_setup_failures: list[dict[str, str]] = []

    async def _safe_ensure(store_label: str, coro):
        """Run an ensure_* coroutine; on failure record the error and return None.

        Memory stores are non-critical for session start — the agent can
        still function (custom mb_* tools also serve the same data via
        disk reads). But the technician needs to know so they can stop
        relying on cross-session continuity.
        """
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Diag-MA] memory store %s provision failed: %s — "
                "session continues with that layer disabled",
                store_label,
                exc,
            )
            memory_setup_failures.append(
                {"store": store_label, "error": str(exc)[:300]}
            )
            return None

    patterns_store_id = await _safe_ensure(
        "patterns",
        ensure_global_store(
            client, kind="patterns", description=PATTERNS_DESC_RUNTIME,
        ),
    ) if settings.ma_memory_store_enabled else None
    playbooks_store_id = await _safe_ensure(
        "playbooks",
        ensure_global_store(
            client, kind="playbooks", description=PLAYBOOKS_DESC_RUNTIME,
        ),
    ) if settings.ma_memory_store_enabled else None
    memory_store_id = await _safe_ensure(
        "device", ensure_memory_store(client, device_slug),
    )
    repair_store_id = await _safe_ensure(
        "repair",
        ensure_repair_store(
            client, device_slug=device_slug, repair_id=repair_id,
        ),
    ) if (repair_id and settings.ma_memory_store_enabled) else None

    await maybe_auto_seed(
        client=client,
        device_slug=device_slug,
        memory_root=memory_root,
        session_mirrors=session_mirrors,
    )
    session_state = SessionState.from_device(device_slug)

    # Resolve which conversation within the repair this WS targets. Anonymous
    # sessions (no repair_id) skip conversation tracking — MA still persists
    # server-side, but we can't index it without an owning repair. Lazy
    # materialization (`materialize=False`): when the resolution would create
    # a fresh conv, we get back a pre-allocated id but nothing is written
    # to disk yet — the slot only persists if the tech actually sends a
    # message. Without this, every "+ Nouvelle conversation" click and every
    # tier switch leaves a 0-turn entry behind. Materialization happens on
    # the first user.message via `pending_conv.materialize_now()`.
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

    # Single-WS guard. The dedup of `responded_tool_ids` is per-forwarder,
    # so two WS that share an MA session (same triplet) would each respond
    # to the same `agent.custom_tool_use`; the second POST is rejected by
    # MA (HTTP 400, "waiting on responses to events …") and the stream
    # gets torn down. Reject the second WS at handshake time instead of
    # letting it crash later. The key is claimed BEFORE any further await
    # so a concurrent open can't slip through between the membership
    # check and the add (asyncio is single-threaded; both happen in one
    # uninterrupted scheduler step). Released in `finally` further down.
    diagnostic_key: tuple[str, str, str] | None = None
    if repair_id and resolved_conv_id:
        candidate_key = (device_slug, repair_id, resolved_conv_id)
        if candidate_key in _active_diagnostic_keys:
            await ws.accept()
            await ws.send_json({
                "type": "error",
                "code": "session_already_open",
                "text": (
                    "Une conversation est déjà ouverte ailleurs pour ce "
                    "repair. Ferme-la avant d'en ouvrir une nouvelle."
                ),
            })
            await ws.close(code=1008, reason="session already open")
            return
        _active_diagnostic_keys.add(candidate_key)
        diagnostic_key = candidate_key

    # Build session params. `resources` is the current (2026-04-01) surface
    # for attaching memory stores. We attach up to 4 layers (global patterns +
    # global playbooks + device + repair); any that returned None (beta off,
    # API failure, missing repair_id) is silently skipped.
    session_kwargs: dict[str, Any] = {
        "agent": {
            "type": "agent",
            "id": agent_info["id"],
            "version": agent_info["version"],
        },
        "environment_id": ids["environment_id"],
        "title": f"diag-{device_slug}-{tier}",
    }
    resources: list[dict] = []
    if patterns_store_id:
        resources.append({
            "type": "memory_store",
            "memory_store_id": patterns_store_id,
            "access": "read_only",
            "prompt": (
                "Global cross-device failure archetypes (short-to-GND, "
                "thermal cascades, BGA lift, bench anti-patterns). Grep "
                "here when the device-specific rules don't match the "
                "symptom — patterns often generalize across families."
            ),
        })
    if playbooks_store_id:
        resources.append({
            "type": "memory_store",
            "memory_store_id": playbooks_store_id,
            "access": "read_only",
            "prompt": (
                "Diagnostic protocol templates indexed by symptom. Before "
                "calling bv_propose_protocol, grep here for a matching "
                "playbook and prefer it over synthesizing one from scratch."
            ),
        })
    if memory_store_id:
        resources.append({
            "type": "memory_store",
            "memory_store_id": memory_store_id,
            "access": "read_only",
            "prompt": (
                "Knowledge pack + confirmed field reports for THIS device. "
                "/knowledge/* is pipeline-authored (registry, rules, etc.); "
                "/field_reports/* is mirrored from mb_record_finding — do "
                "NOT write directly here, use the tool for canonical "
                "findings (validation + format guarantees)."
            ),
        })
    if repair_store_id:
        resources.append({
            "type": "memory_store",
            "memory_store_id": repair_store_id,
            "access": "read_write",
            "prompt": (
                "Your scratch notebook for THIS repair, persisted across "
                "all sessions of the same repair_id. Read state.md at "
                "session start to orient yourself. Write decisions/{ts}.md "
                "when you validate or refute a hypothesis, append to "
                "measurements/{rail}.md when the tech reports a probed "
                "value, and edit open_questions.md for unresolved threads. "
                "Do NOT use this for chat narration or duplicates of "
                "field_reports/."
            ),
        })
    if resources:
        session_kwargs["resources"] = resources

    # Reuse the repair's previously-persisted MA session when possible —
    # that's how conversation context survives a WS close/reopen. Sessions
    # are keyed BY (CONV, TIER): each conversation owns its own MA session
    # id and each tier within a conversation has its own agent identity.
    # Pending convs (lazy-materialized) have no on-disk dir yet, so there's
    # no saved MA session id to look up — skip the read.
    reused_session_id = None
    if resolved_conv_id and not pending_materialize:
        reused_session_id = load_ma_session_id(
            device_slug=device_slug,
            repair_id=repair_id,
            conv_id=resolved_conv_id,
            tier=tier,
        )
    session = None
    # Classify the session-start path into one of five disjoint modes
    # (see api/agent/session_start_mode.py for the full table). The mode
    # drives the WS event contract — `context_lost` vs `session_resumed`
    # vs silent — and the recap-injection branch downstream. Centralizing
    # the decision here keeps the UI contract auditable instead of
    # reconstructing it from intermixed booleans 200 lines later.
    session = None
    retrieved_agent_id: str | None = None
    retrieve_failed = False
    if reused_session_id:
        try:
            session = await client.beta.sessions.retrieve(reused_session_id)
            session_agent = getattr(session, "agent", None)
            retrieved_agent_id = (
                getattr(session_agent, "id", None) if session_agent else None
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Diag-MA] could not retrieve session=%s (%s) — creating fresh",
                reused_session_id,
                exc,
            )
            retrieve_failed = True

    decision = decide_session_start_mode(
        reused_session_id=reused_session_id,
        retrieved_session_agent_id=retrieved_agent_id,
        current_agent_id=agent_info["id"],
        retrieve_failed=retrieve_failed,
    )
    start_mode = decision.mode
    if start_mode == SessionStartMode.RESUMED:
        logger.info(
            "[Diag-MA] Resuming existing session=%s for repair=%s conv=%s",
            reused_session_id,
            repair_id,
            resolved_conv_id,
        )
    elif start_mode == SessionStartMode.FRESH_RECOVERED_AGENT_BUMP:
        logger.info(
            "[Diag-MA] session=%s bound to stale agent=%s (current=%s) — "
            "forcing fresh session + silent recap",
            reused_session_id,
            retrieved_agent_id,
            agent_info["id"],
        )
        session = None  # discard the retrieved (stale-agent) session
    elif start_mode in (
        SessionStartMode.FRESH_RECOVERED_LOST,
        SessionStartMode.FRESH_NEW,
    ):
        # Either no prior id on disk, or retrieve failed — both lead to
        # the create branch below. session is already None.
        session = None

    # Back-compat aliases for the rest of the function — the boolean
    # forms are still threaded through replay decisions and intro
    # injection. Kept as derived views of `start_mode` so there's a
    # single source of truth.
    resumed = start_mode in (
        SessionStartMode.RESUMED,
        SessionStartMode.RESUMED_BUT_EMPTY,
    )
    stale_agent_recovery = (
        start_mode == SessionStartMode.FRESH_RECOVERED_AGENT_BUMP
    )

    if session is None:
        # The old MA session is gone (archived / expired) OR its bound agent
        # no longer matches the current one (overnight evolve bump). With the
        # per-repair scribe mount, the new agent self-orients by reading
        # state.md + decisions/ — no pre-session LLM summary call needed.
        try:
            session = await _sessions_create_with_retry(client, **session_kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Diag-MA] session create failed for device=%s", device_slug)
            await ws.accept()
            await ws.send_json({"type": "error", "text": f"session create failed: {exc}"})
            await ws.close()
            # Release the single-WS guard claimed up-stream so the next
            # tab open isn't permablocked by a transient session.create
            # failure (e.g. MA quota burst). Mirror release happens in
            # the function-final `finally`; this early return bypasses
            # it. discard() is a no-op if the key was never claimed
            # (anonymous WS path).
            if diagnostic_key is not None:
                _active_diagnostic_keys.discard(diagnostic_key)
            return
        # Save the link from this conv to the fresh MA session id NOW only
        # for already-materialized convs. Pending convs defer this until
        # `pending_conv.materialize_now()` runs on the first user message —
        # otherwise we'd write a `ma_session_<tier>.json` into a directory
        # whose parent index doesn't list this conv, leaving an orphan.
        if resolved_conv_id and not pending_materialize:
            save_ma_session_id(
                device_slug=device_slug,
                repair_id=repair_id,
                conv_id=resolved_conv_id,
                session_id=session.id,
                tier=tier,
            )

    pending_conv = _PendingConv(
        device_slug=device_slug,
        repair_id=repair_id,
        conv_id=resolved_conv_id,
        tier=tier,
        memory_root=memory_root,
        session_id=session.id,
        pending=pending_materialize,
    )

    logger.info(
        "[Diag-MA] session=%s device=%s tier=%s model=%s memory=%s resumed=%s",
        session.id,
        device_slug,
        tier,
        agent_info["model"],
        memory_store_id,
        resumed,
    )

    # Surface the conv's "preferred" tier (the one it was originally created
    # with) so the frontend can auto-align if the WS opened with a default
    # tier that doesn't match — e.g. tech reopens panel which defaults to
    # `fast`, lands on a Sonnet conv, and would otherwise silently see the
    # nearly-empty Haiku thread of that same conv instead of the real
    # Sonnet history.
    conv_tier_pref: str | None = None
    if resolved_conv_id and repair_id and not pending_materialize:
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
            "mode": "managed",
            "session_id": session.id,
            "memory_store_id": memory_store_id,
            "device_slug": device_slug,
            "tier": tier,
            "conv_tier": conv_tier_pref,
            "model": agent_info["model"],
            "board_loaded": session_state.board is not None,
            "repair_id": repair_id,
            "conv_id": resolved_conv_id,
            "conversation_count": conversation_count,
        }
    )
    if memory_setup_failures:
        # Tell the UI which memory layers came up degraded so the chat
        # banner can warn the tech that cross-session continuity is off
        # for this run. The session itself is healthy — we only emit
        # this when at least one ensure_* failed silently.
        await ws.send_json(
            {
                "type": "memory_store_setup_failed",
                "failures": memory_setup_failures,
            }
        )

    # Hydrate any active protocol so the UI panel rebuilds on reconnect.
    # When no protocol exists for this conv, push an explicit
    # `protocol_cleared` so the wizard sidebar drops any leftover state
    # from the previous conv (silence would have left the prior wizard
    # pinned on screen — same root cause as the boardview reset above).
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

    # Hydrate the boardview overlay (highlights, focus, annotations,
    # dim, layer flip) from the per-repair snapshot. This survives MA
    # archiving the conv: even if the agent's chat memory is gone, the
    # board still shows the same components highlighted / annotated as
    # before the reload — the visual state IS the on-disk truth, not
    # something that has to be reconstructed from MA events. Apply it
    # to the live SessionState too so the next bv_* dispatch sees the
    # restored overlay rather than silently overwriting it.
    if repair_id:
        from api.agent.board_state import load_board_state, replay_board_state_to_ws
        # Always wipe the renderer's overlay first so a switch from a
        # heavily-annotated conv to a fresh one shows a clean board. Without
        # this, brd_viewer keeps the previous conv's highlights / annotations
        # / focus on screen because the per-conv backend has nothing to
        # send for the new conv (and silence ≠ "clear what was there").
        await ws.send_json({"type": "boardview.reset_view"})
        snapshot = load_board_state(
            memory_root=memory_root,
            device_slug=device_slug,
            repair_id=repair_id,
            conv_id=resolved_conv_id,
        )
        if snapshot:
            session_state.restore_view(snapshot)
            sent = await replay_board_state_to_ws(ws, snapshot)
            if sent:
                logger.info(
                    "[Diag-MA] replayed boardview state for repair=%s conv=%s (%d events)",
                    repair_id, resolved_conv_id, sent,
                )

    # The intro (device context + reported symptom + technician profile) only
    # needs injection on a FRESH session. When we resume, the MA session
    # already carries the full conversation history including the original intro.
    # Fresh sessions get the device intro PLUS the technician profile block.
    # On a recovered fresh session (old MA session expired), the agent reads
    # state.md / decisions/ from the per-repair scribe mount on its first
    # turn — no pre-cuisined LLM summary is injected here.
    # MA stores the intro as one hidden user message prefixed to the first real
    # turn (see _forward_ws_to_session's pending_intro handling).
    intro: str | None
    state_summary: dict[str, Any] = {"measurements": 0, "protocol": None, "outcome": False}
    if resumed:
        intro = None
        # Even when MA resumes cleanly, compute the summary so we can ship
        # it in any later context_lost emitted from the replay path.
        from api.agent.recovery_state import build_repair_state_block as _brsb
        _, state_summary = _brsb(
            memory_root=memory_root, device_slug=device_slug, repair_id=repair_id,
            conv_id=resolved_conv_id,
        )
    else:
        from api.agent.recovery_state import build_repair_state_block
        from api.profile.prompt import render_technician_block
        from api.profile.store import load_profile

        device_intro = build_session_intro(device_slug=device_slug, repair_id=repair_id)
        tech_block = render_technician_block(load_profile())
        # Hard-fact snapshot from disk (measurements + protocol + outcome).
        # Surfaces what the tech actually has on record so a fresh MA agent
        # doesn't redo work or re-ask measurements that already exist on
        # disk. Critical when the prior MA session was lost (cf. context_lost
        # path) — without this the agent is back to "dis-moi quel est ton
        # symptôme" even though 8 measurements + a 5-step protocol survive.
        state_block, state_summary = build_repair_state_block(
            memory_root=memory_root, device_slug=device_slug, repair_id=repair_id,
            conv_id=resolved_conv_id,
        )
        parts: list[str] = []
        if device_intro:
            parts.append(device_intro)
        if state_block:
            parts.append(state_block)
        parts.append(f"[TECHNICIAN CONTEXT]\n{tech_block}")
        intro = "\n\n---\n\n".join(parts) if parts else None

    # Per-turn context tag — prepended to EVERY user message so smaller models
    # (Haiku in particular) keep the device + symptom in their foreground even
    # on terse follow-ups like "salut" / "ok" after a resume. ~25 tokens, stable
    # prefix so prompt caching covers it after the first turn.
    ctx_tag = build_ctx_tag(
        device_slug=device_slug, repair_id=repair_id, memory_root=memory_root
    )
    if reused_session_id and not resumed:
        # The old MA session is gone (archived / expired) or its agent was
        # bumped overnight. The new session has no native memory of the
        # prior turns, but the agent will read state.md / decisions/ from
        # the per-repair scribe mount on its first turn and self-orient.
        # Tell the tech we created a fresh session so they don't assume
        # the agent remembers the live in-conv chat (it doesn't — it
        # remembers what was scribed to the mount).
        if not stale_agent_recovery:
            await ws.send_json(
                {
                    "type": "context_lost",
                    "old_session_id": reused_session_id,
                    "new_session_id": session.id,
                    "preserved": state_summary,
                }
            )
        else:
            logger.info(
                "[Diag-MA] silent agent-bump (stale agent_id) — fresh "
                "session=%s for repair=%s conv=%s, agent will self-orient "
                "from scribe mount",
                session.id,
                repair_id,
                resolved_conv_id,
            )
        logger.warning(
            "[Diag-MA] context_lost emitted for repair=%s conv=%s — old "
            "session=%s archived and no JSONL backup; new agent starts blank",
            repair_id,
            resolved_conv_id,
            reused_session_id,
        )
    if intro:
        await ws.send_json(
            {
                "type": "context_loaded",
                "device_slug": device_slug,
                "repair_id": repair_id,
            }
        )
        logger.info(
            "[Diag-MA] Stashed session intro for repair=%s (awaiting tech input)",
            repair_id,
        )
    # Replay the chat history from local JSONL when we just created a fresh
    # MA session AND we have a transcript on disk. Without this, the silent
    # agent-bump path (bootstrap reload → stale agent_id → fresh session)
    # leaves the chat panel empty even though the conv has 37 lines on
    # disk. Symmetric with the `if resumed:` MA-events replay below — the
    # tech never has to guess whether their conversation history is
    # actually visible based on which recovery path the runtime took.
    if not resumed and repair_id and resolved_conv_id:
        replayed_local = await _replay_jsonl_history_to_ws(
            ws,
            device_slug=device_slug,
            repair_id=repair_id,
            conv_id=resolved_conv_id,
            memory_root=memory_root,
            session_state=session_state,
        )
        if replayed_local:
            logger.info(
                "[Diag-MA] replayed chat from local JSONL for fresh session "
                "(repair=%s conv=%s)",
                repair_id, resolved_conv_id,
            )
    if resumed:
        await ws.send_json(
            {
                "type": "session_resumed",
                "session_id": session.id,
                "repair_id": repair_id,
            }
        )
        # Replay the MA session's past events so the UI chat panel rebuilds
        # the conversation visually. Also replays per-turn costs from the
        # span.model_request_end events MA stores alongside so the lifetime
        # cost chip survives the reopen.
        replayed_anything = await _replay_ma_history_to_ws(
            ws,
            client,
            session.id,
            session_state,
            agent_info["model"],
            device_slug=device_slug,
            repair_id=repair_id,
            conv_id=resolved_conv_id,
            memory_root=memory_root,
        )
        # If MA's events.list returned empty AND there was no JSONL backup
        # to replay, the resumed session is alive in name only — its
        # internal context has likely been compacted/dropped. The chat
        # panel showing nothing is a lie unless we tell the tech the agent
        # is effectively starting fresh. Emit `context_lost` so the
        # frontend renders an explicit alert card.
        if not replayed_anything:
            # Promote the start mode to the post-replay observation:
            # session retrieved fine, agent_id matched, but the event
            # log was empty. This is a runtime fact, not a startup
            # decision — `decide_session_start_mode` cannot return this
            # value because it doesn't have replay information. Logging
            # the transition keeps the audit trail intact.
            start_mode = SessionStartMode.RESUMED_BUT_EMPTY
            logger.info(
                "[Diag-MA] start_mode promoted to RESUMED_BUT_EMPTY for "
                "session=%s repair=%s — agent has no conversational "
                "history, will be primed with state block",
                session.id,
                repair_id,
            )
            # The resumed MA session is alive but empty (events.list returned
            # only metadata, no JSONL backup). The agent has no
            # conversational history. Inject the on-disk state snapshot as
            # a synthetic user.message so it has the hard facts (mesures,
            # protocol progress, outcome) before the tech's next turn —
            # otherwise the agent would re-ask measurements that already
            # exist on disk. The intro path was skipped because we entered
            # via the resumed=True branch, so this is the only chance to
            # prime the agent.
            from api.agent.recovery_state import build_repair_state_block as _brsb

            state_block_now, _ = _brsb(
                memory_root=memory_root,
                device_slug=device_slug,
                repair_id=repair_id,
                conv_id=resolved_conv_id,
            )
            if state_block_now:
                try:
                    await client.beta.sessions.events.send(
                        session.id,
                        events=[{
                            "type": "user.message",
                            "content": [{"type": "text", "text": state_block_now}],
                        }],
                    )
                    _mirror_jsonl(
                        device_slug=device_slug,
                        repair_id=repair_id,
                        conv_id=resolved_conv_id,
                        memory_root=memory_root,
                        event={
                            "role": "user",
                            "content": [{"type": "text", "text": state_block_now}],
                        },
                    )
                    logger.info(
                        "[Diag-MA] context_lost recovery — pushed state block "
                        "(%d measurements, protocol=%s, outcome=%s) to fresh agent",
                        state_summary["measurements"],
                        bool(state_summary["protocol"]),
                        state_summary["outcome"],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[Diag-MA] failed to push state block on context_lost: %s",
                        exc,
                    )
            await ws.send_json(
                {
                    "type": "context_lost",
                    "old_session_id": session.id,
                    "new_session_id": session.id,
                    "reason": "ma_events_empty",
                    "preserved": state_summary,
                }
            )
            logger.warning(
                "[Diag-MA] context_lost (resumed but empty) for repair=%s "
                "conv=%s session=%s — events.list returned 0 and no JSONL "
                "backup; agent will respond as if starting fresh",
                repair_id,
                resolved_conv_id,
                session.id,
            )

    # Cache: agent.custom_tool_use events by event.id, so we can look up
    # name+input when `requires_action` arrives and only hands us event_ids.
    events_by_id: dict[str, Any] = {}

    from api.tools.measurements import set_ws_emitter
    from api.tools.validation import set_ws_emitter as set_validation_emitter

    def _emit(event: dict) -> None:
        # Route through session_mirrors instead of bare asyncio.create_task
        # so the send is awaited on session close. Bare create_task left the
        # task orphan: a fast WS close would tear down the session before the
        # frame hit the wire, and the technician would never see the
        # measurement / validation event the agent had just acknowledged.
        # Bonus: spawn() already wires a done callback that surfaces
        # exceptions instead of letting them die silently in the loop.
        session_mirrors.spawn(ws.send_json(event))

    set_ws_emitter(_emit)
    set_validation_emitter(_emit)

    try:
        recv_task = asyncio.create_task(
            _forward_ws_to_session(
                ws,
                client,
                session.id,
                pending_intro=intro,
                ctx_tag=ctx_tag,
                repair_id=repair_id,
                device_slug=device_slug,
                conv_id=resolved_conv_id,
                memory_root=memory_root,
                pending_conv=pending_conv,
                session_state=session_state,
            ),
            name="ws->session",
        )
        emit_task = asyncio.create_task(
            _forward_session_to_ws(
                ws,
                client,
                session.id,
                device_slug,
                memory_root,
                events_by_id,
                session_state,
                agent_info["model"],
                tier=tier,
                environment_id=ids["environment_id"],
                repair_id=repair_id,
                conv_id=resolved_conv_id,
                session_mirrors=session_mirrors,
                pending_conv=pending_conv,
            ),
            name="session->ws",
        )
        done, pending = await asyncio.wait(
            {recv_task, emit_task}, return_when=asyncio.FIRST_COMPLETED
        )
        # Wait for cancelled forwarder tasks to actually unwind before the
        # finally block tears down the global emitters. Without this await,
        # a recv_task interrupted mid-`ws.receive_text()` can race with the
        # `set_ws_emitter(None)` cleanup: the cancellation propagates while
        # _emit is still being invoked from a measurement tool, leading to
        # writes on a torn-down WS.
        #
        # Per-task cancel + bounded wait (instead of one global gather) so:
        #   - Each forwarder gets its own 2s unwind budget — a slow task
        #     can't starve a clean-finishing one out of the shared 5s
        #     window the previous gather provided.
        #   - A task that ignores its cancel is logged BY NAME, so the
        #     operator can map "did not unwind" to recv vs emit when
        #     reading a session teardown trace.
        # asyncio.wait() is preferred over wait_for() here: it never
        # raises CancelledError or TimeoutError on the awaited tasks
        # (it just returns them in the pending set), so a task that
        # observed its cancel and re-raised does not produce a noisy
        # exception path during teardown.
        for task in pending:
            task.cancel()
            _, unwind_pending = await asyncio.wait({task}, timeout=2.0)
            if unwind_pending:
                logger.warning(
                    "[Diag-MA] forwarder task %s did not unwind within "
                    "2s after cancel — session=%s; proceeding with "
                    "teardown",
                    task.get_name(),
                    session.id,
                )
                continue
            # Task unwound: surface any non-cancellation exception so
            # a forwarder that died with an unexpected error during
            # the cancel path is visible in the logs.
            try:
                exc = task.exception()
            except asyncio.CancelledError:
                # Expected unwind path — nothing to log.
                continue
            if exc is not None and not isinstance(
                exc, (asyncio.CancelledError, WebSocketDisconnect)
            ):
                logger.warning(
                    "[Diag-MA] forwarder task %s raised during unwind: "
                    "%s — session=%s; proceeding with teardown",
                    task.get_name(),
                    exc,
                    session.id,
                )
        # Surface exceptions from the completed task to the logger. A WS close
        # (code 1000 normal, 1012 service restart) raised inside a forwarder task
        # is expected — log it as INFO, not ERROR with a stacktrace.
        for task in done:
            exc = task.exception()
            if exc is None:
                continue
            if isinstance(exc, WebSocketDisconnect):
                logger.info(
                    "[Diag-MA] task %s finished on WS disconnect code=%s",
                    task.get_name(),
                    getattr(exc, "code", "?"),
                )
            else:
                logger.exception(
                    "[Diag-MA] task %s raised",
                    task.get_name(),
                    exc_info=exc,
                )
    except WebSocketDisconnect:
        logger.info("[Diag-MA] WS disconnected for device=%s", device_slug)
    finally:
        # Drain pending mirror tasks before tearing down the session so a
        # fast WS close doesn't cancel a mirror mid-flight (5 s max wait).
        await session_mirrors.wait_drain(timeout=5.0)
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
        set_ws_emitter(None)
        set_validation_emitter(None)
        # Release the single-WS guard claimed earlier (after
        # ensure_conversation). Anonymous sessions never claimed one.
        # discard() is no-op if already absent, so an unexpected
        # mid-setup failure path that already released elsewhere is safe.
        if diagnostic_key is not None:
            _active_diagnostic_keys.discard(diagnostic_key)


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


# --------------------------------------------------------------------------
# Files+Vision (Flow A + Flow B) helpers
# --------------------------------------------------------------------------

# Hard cap on macro upload size (post-base64-decode). 5 MB is plenty for a
# JPEG of a board macro at sane resolutions ; bigger payloads waste WS
# bandwidth and Anthropic Files API quota.
_MAX_MACRO_BYTES = 5 * 1024 * 1024
# How long to wait on the frontend to return a captured frame after we
# pushed server.capture_request. Mirrors the MA stream watchdog default.
_CAPTURE_TIMEOUT_S = 30.0


def _handle_client_capabilities(session: SessionState, frame: dict) -> None:
    """Update session capability flags from a client.capabilities frame.

    Idempotent ; can be re-sent during the WS session if the frontend's
    device list changes (camera plugged / unplugged, picker changed).
    """
    session.has_camera = bool(frame.get("camera_available"))


async def _handle_client_upload_macro(
    *,
    client: AsyncAnthropic,
    session: SessionState,
    memory_root: Path,
    slug: str,
    repair_id: str,
    ma_session_id: str,
    frame: dict,
) -> None:
    """Flow A: tech-uploaded photo → persist → Files API → user.message.

    Raises :class:`ValueError` on payload too large or invalid base64. The
    caller should catch and surface to the frontend, not crash the loop.
    """
    b64 = frame.get("base64") or ""
    mime = (frame.get("mime") or "").lower()
    filename = frame.get("filename") or "macro.png"

    try:
        bytes_ = _b64.b64decode(b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"invalid base64 payload: {exc}") from exc

    if len(bytes_) > _MAX_MACRO_BYTES:
        raise ValueError(
            f"macro upload too large: {len(bytes_)} bytes > {_MAX_MACRO_BYTES} cap"
        )

    persist_macro(
        memory_root=memory_root, slug=slug, repair_id=repair_id,
        source="manual", bytes_=bytes_, mime=mime,
    )

    # NOTE : SDK 0.97 doesn't expose `purpose=` on files.upload. Files
    # uploaded without it work for image content blocks. Revisit if a
    # later SDK adds `purpose` and we hit a "wrong purpose" rejection.
    uploaded = await client.beta.files.upload(
        file=(filename, bytes_, mime),
    )

    await client.beta.sessions.events.send(
        session_id=ma_session_id,
        events=[{
            "type": "user.message",
            "content": [
                {"type": "image", "source": {"type": "file", "file_id": uploaded.id}},
                {"type": "text", "text": "Macro photo uploaded by the technician."},
            ],
        }],
    )


async def _handle_client_capture_response(
    *,
    session: SessionState,
    frame: dict,
) -> None:
    """Resolve the pending Future for the matching request_id (Flow B)."""
    request_id = frame.get("request_id")
    if not request_id or request_id not in session.pending_captures:
        logger.warning(
            "[Diag-MA] capture_response with unknown request_id: %r", request_id,
        )
        return
    fut = session.pending_captures[request_id]
    if not fut.done():
        fut.set_result(frame)


async def _dispatch_cam_capture(
    *,
    client: AsyncAnthropic,
    session: SessionState,
    ws: WebSocket,
    memory_root: Path,
    slug: str,
    repair_id: str,
    ma_session_id: str,
    tool_use_id: str,
    tool_input: dict,
    timeout_s: float = _CAPTURE_TIMEOUT_S,
) -> None:
    """Flow B dispatcher: push capture_request, await response, send tool_result.

    Always sends back exactly one user.custom_tool_result for the given
    tool_use_id — either with the captured image (success) or is_error
    (timeout / decode failure / Files API failure / no-camera). Cleans up
    the pending Future on every exit path.
    """
    request_id = secrets.token_urlsafe(8)
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    session.pending_captures[request_id] = fut

    try:
        await ws.send_json({
            "type": "server.capture_request",
            "request_id": request_id,
            "tool_use_id": tool_use_id,
            "reason": tool_input.get("reason") or "",
        })

        try:
            response = await asyncio.wait_for(fut, timeout=timeout_s)
        except TimeoutError:
            await client.beta.sessions.events.send(
                session_id=ma_session_id,
                events=[{
                    "type": "user.custom_tool_result",
                    "custom_tool_use_id": tool_use_id,
                    "is_error": True,
                    "content": [{
                        "type": "text",
                        "text": (
                            f"Capture timeout after {timeout_s:.0f}s — the "
                            "frontend did not respond. Check that a camera "
                            "is selected in the metabar."
                        ),
                    }],
                }],
            )
            return

        try:
            bytes_ = _b64.b64decode(response.get("base64") or "", validate=True)
            if not bytes_:
                raise ValueError("empty payload")
            mime = (response.get("mime") or "image/jpeg").lower()
            device_label = response.get("device_label") or "camera"

            persist_macro(
                memory_root=memory_root, slug=slug, repair_id=repair_id,
                source="capture", bytes_=bytes_, mime=mime,
            )

            uploaded = await client.beta.files.upload(
                file=(f"capture_{request_id}.jpg", bytes_, mime),
            )

            await client.beta.sessions.events.send(
                session_id=ma_session_id,
                events=[{
                    "type": "user.custom_tool_result",
                    "custom_tool_use_id": tool_use_id,
                    "content": [
                        {"type": "image",
                         "source": {"type": "file", "file_id": uploaded.id}},
                        {"type": "text",
                         "text": f"Capture acquise depuis {device_label}."},
                    ],
                }],
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Diag-MA] cam_capture processing failed")
            await client.beta.sessions.events.send(
                session_id=ma_session_id,
                events=[{
                    "type": "user.custom_tool_result",
                    "custom_tool_use_id": tool_use_id,
                    "is_error": True,
                    "content": [{
                        "type": "text",
                        "text": f"Capture processing failed: {exc}",
                    }],
                }],
            )
    finally:
        session.pending_captures.pop(request_id, None)


# --------------------------------------------------------------------------
# WS event loops (in / out)
# --------------------------------------------------------------------------

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
        if ctx_tag:
            text = ctx_tag + "\n\n" + text
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
    # AsyncAnthropic: `.stream(...)` returns a coroutine resolving to an
    # `AsyncStream[...]`. We must await first, then use it as an async
    # context manager — otherwise we get `TypeError: 'coroutine' object
    # does not support the asynchronous context manager protocol`.
    stream_ctx = await client.beta.sessions.events.stream(session_id)
    # Deduplicate tool-use responses. MA can re-emit `session.status_idle`
    # with `stop_reason=requires_action` carrying the SAME event_ids after
    # we've already sent their `user.custom_tool_result` — a naive re-dispatch
    # then posts a duplicate response, which MA rejects with 400
    # ("Invalid user.custom_tool_result event [...] waiting on responses to
    # events [...]") and tears down the stream. Track ids we've answered.
    responded_tool_ids: set[str] = set()
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
    settings_for_watchdog = get_settings()
    stream_timeout = settings_for_watchdog.ma_stream_event_timeout_seconds
    async with stream_ctx as stream:
        stream_iter = stream.__aiter__()
        while True:
            try:
                event = await asyncio.wait_for(
                    stream_iter.__anext__(), timeout=stream_timeout,
                )
            except StopAsyncIteration:
                break
            except TimeoutError:
                logger.warning(
                    "[Diag-MA] stream inactive for %.0fs — closing session=%s",
                    stream_timeout,
                    session_id,
                )
                try:
                    await ws.send_json(
                        {
                            "type": "stream_timeout",
                            "session_id": session_id,
                            "timeout_seconds": stream_timeout,
                        }
                    )
                except Exception:  # noqa: BLE001
                    pass
                break
            except WebSocketDisconnect:
                # Client window closed mid-stream — bubble up so the caller's
                # asyncio.wait observes the task completion and the symmetric
                # WS→session forwarder can shut down too. Not an MA-side error.
                raise
            except Exception as exc:  # noqa: BLE001 — SSE transport collapse
                # Anything else from the SSE iterator is a transport-level
                # failure (TLS reset, ConnectionError, anthropic.APIStatusError
                # mid-stream, etc.). Without an explicit catch the task ended
                # silently, the WS client kept its socket open expecting
                # `agent.message` chunks that never arrived, and the technician
                # saw a frozen UI with no signal. Surface it to the WS so the
                # frontend can render a "session lost — reconnect" hint, then
                # break cleanly so the orchestrator's finally block runs.
                logger.exception(
                    "[Diag-MA] stream iterator failed session=%s exc=%s",
                    session_id,
                    type(exc).__name__,
                )
                try:
                    await ws.send_json(
                        {
                            "type": "stream_error",
                            "session_id": session_id,
                            "error": type(exc).__name__,
                            "message": str(exc)[:500],
                        }
                    )
                except Exception:  # noqa: BLE001
                    pass
                break

            etype = getattr(event, "type", None)

            if etype == "agent.message":
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
                    await ws.send_json({"type": "turn_cost", **cost})
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

            elif etype == "agent.custom_tool_use":
                events_by_id[event.id] = event
                tool_name = getattr(event, "name", None)
                tool_input = getattr(event, "input", {}) or {}
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
                            expand_result = await expand_pack(
                                device_slug=device_slug,
                                focus_symptoms=focus_symptoms,
                                focus_refdes=focus_refdes,
                                client=client,
                                memory_root=memory_root,
                                chunk_provider=_curator_provider,
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
                                    "text": json.dumps(expand_result, default=str),
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
                                    "text": json.dumps(sub_result, default=str),
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

                    result = await _dispatch_tool(
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
                                        "text": json.dumps(result_for_agent, default=str),
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
                await ws.send_json({"type": "session_terminated"})
                return

            elif etype == "session.error":
                err = getattr(event, "error", None)
                msg = getattr(err, "message", None) if err is not None else None
                await ws.send_json({"type": "error", "text": msg or "session error"})
