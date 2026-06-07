"""Pattern 4 round-trip dispatcher for ``bv_propose_protocol``.

The agent emits a tool_use; we park on a Future, ask the tech to
accept/reject, and only on accept do we materialise the protocol.
Always closes the loop with exactly one ``user.custom_tool_result``
(success / reject / timeout).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from anthropic import AsyncAnthropic
from fastapi import WebSocket

from api.agent import runtime_managed as _rm
from api.agent._session_mirrors import SessionMirrors as _SessionMirrors
from api.agent.runtime._aux import _safe_tool_result_text, logger
from api.session.state import SessionState


async def _dispatch_protocol_with_confirmation(
    *,
    client: AsyncAnthropic,
    session: SessionState,
    ws: WebSocket,
    memory_root: Path,
    device_slug: str,
    repair_id: str | None,
    conv_id: str | None,
    ma_session_id: str,
    tool_use_id: str,
    tool_input: dict,
    session_mirrors: _SessionMirrors | None,
    timeout_s: float | None = None,
) -> None:
    """Pattern 4 round-trip for ``bv_propose_protocol`` (tech confirmation).

    1. Push ``protocol_pending_confirmation`` over WS so the UI can render
       a modal summarising the proposed protocol.
    2. Park on a Future, registered in
       ``session.pending_protocol_confirmations[tool_use_id]``.
    3. On ``client.protocol_confirmation`` (resolved by
       :func:`_handle_client_protocol_confirmation`) :

       * **accept** → run the regular dispatch via :func:`_dispatch_tool`,
         emit the ``protocol_proposed`` WS event, send the agent a normal
         ``user.custom_tool_result``.
       * **reject** → send the agent an ``is_error`` ``user.custom_tool_result``
         carrying the tech's reason; no protocol is materialised on disk.

    4. On timeout → ``is_error`` tool_result so the MA session never stays
       stuck on ``requires_action``. The Future is always cleaned up.
    """
    if timeout_s is None:
        timeout_s = _rm.get_settings().ma_protocol_confirmation_timeout_seconds

    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    session.pending_protocol_confirmations[tool_use_id] = fut

    # Lightweight projection of the proposal for the modal — title +
    # rationale + step count + step previews. The full payload would
    # bloat the WS frame and the modal only needs the gist.
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
    try:
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

        try:
            response = await asyncio.wait_for(fut, timeout=timeout_s)
        except TimeoutError:
            logger.warning(
                "[Diag-MA] protocol confirmation timeout after %.0fs eid=%s",
                timeout_s,
                tool_use_id,
            )
            try:
                await ws.send_json({
                    "type": "protocol_confirmation_timeout",
                    "tool_use_id": tool_use_id,
                    "timeout_seconds": timeout_s,
                })
            except Exception:  # noqa: BLE001 — best-effort UI hint
                pass
            await client.beta.sessions.events.send(
                ma_session_id,
                events=[{
                    "type": "user.custom_tool_result",
                    "custom_tool_use_id": tool_use_id,
                    "is_error": True,
                    "content": [{
                        "type": "text",
                        "text": (
                            f"Protocol confirmation timed out after "
                            f"{timeout_s:.0f}s — the technician did not "
                            "respond. Try again with a tighter, more "
                            "obvious protocol or ask in chat first."
                        ),
                    }],
                }],
            )
            return

        decision = str(response.get("decision") or "").lower().strip()
        reason = str(response.get("reason") or "").strip()

        if decision == "accept":
            result = await _rm._dispatch_tool(
                "bv_propose_protocol",
                tool_input,
                device_slug,
                memory_root,
                client,
                session,
                ma_session_id,
                repair_id=repair_id,
                session_mirrors=session_mirrors,
                conv_id=conv_id,
            )
            single_event = result.get("event")
            if result.get("ok") and single_event is not None:
                try:
                    await ws.send_json(
                        single_event if isinstance(single_event, dict)
                        else single_event.model_dump(by_alias=True)
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[Diag-MA] protocol_proposed WS push failed eid=%s",
                        tool_use_id,
                    )
            result_for_agent = {
                k: v for k, v in result.items() if k not in ("event", "events")
            }
            await client.beta.sessions.events.send(
                ma_session_id,
                events=[{
                    "type": "user.custom_tool_result",
                    "custom_tool_use_id": tool_use_id,
                    "content": [{
                        "type": "text",
                        "text": _safe_tool_result_text(result_for_agent),
                    }],
                }],
            )
            return

        # decision == "reject" (or any non-accept value treated as reject so
        # an unexpected payload from a stale UI never silently materialises
        # the protocol).
        deny_text = (
            f"Technician rejected the proposed protocol. "
            f"Reason: {reason}" if reason
            else "Technician rejected the proposed protocol with no reason given."
        )
        deny_text += (
            " Do not re-emit the same protocol; either ask a clarifying "
            "question, propose a different approach, or wait for further "
            "instruction."
        )
        await client.beta.sessions.events.send(
            ma_session_id,
            events=[{
                "type": "user.custom_tool_result",
                "custom_tool_use_id": tool_use_id,
                "is_error": True,
                "content": [{
                    "type": "text",
                    "text": deny_text,
                }],
            }],
        )
    finally:
        session.pending_protocol_confirmations.pop(tool_use_id, None)
