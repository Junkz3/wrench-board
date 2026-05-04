"""Pattern 4 round-trip for ``bv_propose_protocol``.

The runtime gates the proposal on an explicit tech accept / reject before
materialising the protocol. These tests exercise the three exit paths of
:func:`api.agent.runtime_managed._dispatch_protocol_with_confirmation` :

* accept  → the actual tool dispatches, a ``protocol_proposed`` WS event
  is emitted, and the agent receives a normal ``user.custom_tool_result``.
* reject  → no on-disk protocol; the agent receives an ``is_error``
  ``user.custom_tool_result`` carrying the tech's reason.
* timeout → no on-disk protocol; the agent receives an ``is_error``
  ``user.custom_tool_result`` so the MA session never stays stuck on
  ``requires_action``. The Future is always cleaned up.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.agent.runtime_managed import (
    _dispatch_protocol_with_confirmation,
    _handle_client_protocol_confirmation,
)
from api.session.state import SessionState


def _proposal_payload() -> dict[str, Any]:
    return {
        "title": "Hunt VBUS short",
        "rationale": "Suspected short on VBUS rail downstream of Q1.",
        "rule_inspirations": ["rule_vbus_short_q1"],
        "steps": [
            {
                "type": "numeric",
                "target": "Q1",
                "instruction": "Mesurer VBUS au drain de Q1.",
                "rationale": "Confirme la chute attendue.",
                "unit": "V",
                "nominal": 5.0,
                "pass_range": [4.7, 5.3],
            },
            {
                "type": "boolean",
                "target": "C5",
                "instruction": "C5 visiblement endommagée ?",
                "rationale": "Cause typique du court.",
                "expected": False,
            },
        ],
    }


def _extract_sent_event(send_mock: AsyncMock, *, idx: int = 0) -> dict[str, Any]:
    call = send_mock.await_args_list[idx]
    if "events" in call.kwargs:
        events = call.kwargs["events"]
    else:
        events = call.args[1]
    return events[0]


@pytest.mark.asyncio
async def test_accept_dispatches_protocol_and_emits_event(tmp_path: Path) -> None:
    session = SessionState()
    client = MagicMock()
    client.beta.sessions.events.send = AsyncMock()
    ws = MagicMock()
    ws.send_json = AsyncMock()
    payload = _proposal_payload()
    eid = "sevt_proto_accept_1"

    async def simulate_tech_accept() -> None:
        # Wait for dispatch to register the pending future before resolving.
        for _ in range(50):
            if eid in session.pending_protocol_confirmations:
                break
            await asyncio.sleep(0.01)
        await _handle_client_protocol_confirmation(
            session=session,
            frame={
                "type": "client.protocol_confirmation",
                "tool_use_id": eid,
                "decision": "accept",
            },
        )

    asyncio.create_task(simulate_tech_accept())

    await _dispatch_protocol_with_confirmation(
        client=client,
        session=session,
        ws=ws,
        memory_root=tmp_path,
        device_slug="iphone-x",
        repair_id="R-accept-1",
        conv_id="C1",
        ma_session_id="sesn_accept",
        tool_use_id=eid,
        tool_input=payload,
        session_mirrors=None,
        timeout_s=2.0,
    )

    # Modal request was sent first.
    pending_frames = [
        c.args[0] for c in ws.send_json.await_args_list
        if c.args and isinstance(c.args[0], dict)
        and c.args[0].get("type") == "protocol_pending_confirmation"
    ]
    assert pending_frames, "expected protocol_pending_confirmation WS frame"
    pending = pending_frames[0]
    assert pending["tool_use_id"] == eid
    assert pending["title"] == "Hunt VBUS short"
    assert pending["step_count"] == 2

    # protocol_proposed was emitted post-accept (real tool succeeded — the
    # protocol module persisted it on disk and emitted the event).
    proposed_frames = [
        c.args[0] for c in ws.send_json.await_args_list
        if c.args and isinstance(c.args[0], dict)
        and c.args[0].get("type") == "protocol_proposed"
    ]
    assert proposed_frames, "expected protocol_proposed WS frame on accept"

    # Tool result back to MA: success (no is_error).
    client.beta.sessions.events.send.assert_awaited_once()
    event = _extract_sent_event(client.beta.sessions.events.send)
    assert event["type"] == "user.custom_tool_result"
    assert event["custom_tool_use_id"] == eid
    assert event.get("is_error") is not True

    # Future cleaned up.
    assert eid not in session.pending_protocol_confirmations

    # Active protocol persisted on disk under repairs/{rid}/.../protocols/.
    proto_dir = tmp_path / "iphone-x" / "repairs" / "R-accept-1"
    assert proto_dir.exists()
    proto_files = list(proto_dir.rglob("protocols/*.json"))
    assert proto_files, f"expected a protocol file under {proto_dir}"


@pytest.mark.asyncio
async def test_reject_returns_is_error_with_reason(tmp_path: Path) -> None:
    session = SessionState()
    client = MagicMock()
    client.beta.sessions.events.send = AsyncMock()
    ws = MagicMock()
    ws.send_json = AsyncMock()
    payload = _proposal_payload()
    eid = "sevt_proto_reject_1"
    reason = "trop long, on commence par mesurer C5 directement"

    async def simulate_tech_reject() -> None:
        for _ in range(50):
            if eid in session.pending_protocol_confirmations:
                break
            await asyncio.sleep(0.01)
        await _handle_client_protocol_confirmation(
            session=session,
            frame={
                "type": "client.protocol_confirmation",
                "tool_use_id": eid,
                "decision": "reject",
                "reason": reason,
            },
        )

    asyncio.create_task(simulate_tech_reject())

    await _dispatch_protocol_with_confirmation(
        client=client,
        session=session,
        ws=ws,
        memory_root=tmp_path,
        device_slug="iphone-x",
        repair_id="R-reject-1",
        conv_id="C1",
        ma_session_id="sesn_reject",
        tool_use_id=eid,
        tool_input=payload,
        session_mirrors=None,
        timeout_s=2.0,
    )

    # Tool result back to MA: is_error with the tech's reason.
    client.beta.sessions.events.send.assert_awaited_once()
    event = _extract_sent_event(client.beta.sessions.events.send)
    assert event["type"] == "user.custom_tool_result"
    assert event["custom_tool_use_id"] == eid
    assert event["is_error"] is True
    text_blocks = [c for c in event["content"] if c.get("type") == "text"]
    assert text_blocks
    text = text_blocks[0]["text"]
    assert reason in text
    assert "rejected" in text.lower()

    # No protocol_proposed WS frame on reject.
    proposed_frames = [
        c.args[0] for c in ws.send_json.await_args_list
        if c.args and isinstance(c.args[0], dict)
        and c.args[0].get("type") == "protocol_proposed"
    ]
    assert not proposed_frames, "no protocol_proposed should be emitted on reject"

    # Future cleaned up.
    assert eid not in session.pending_protocol_confirmations

    # No protocol on disk.
    proto_dir = tmp_path / "iphone-x" / "repairs" / "R-reject-1"
    if proto_dir.exists():
        assert not any(proto_dir.rglob("protocols/*.json"))


@pytest.mark.asyncio
async def test_timeout_returns_is_error_and_cleans_up(tmp_path: Path) -> None:
    session = SessionState()
    client = MagicMock()
    client.beta.sessions.events.send = AsyncMock()
    ws = MagicMock()
    ws.send_json = AsyncMock()
    payload = _proposal_payload()
    eid = "sevt_proto_timeout_1"

    # No frontend response simulated → wait_for must time out.
    await _dispatch_protocol_with_confirmation(
        client=client,
        session=session,
        ws=ws,
        memory_root=tmp_path,
        device_slug="iphone-x",
        repair_id="R-timeout-1",
        conv_id="C1",
        ma_session_id="sesn_timeout",
        tool_use_id=eid,
        tool_input=payload,
        session_mirrors=None,
        timeout_s=0.2,
    )

    # Tool result back to MA: is_error with a timeout message.
    client.beta.sessions.events.send.assert_awaited_once()
    event = _extract_sent_event(client.beta.sessions.events.send)
    assert event["type"] == "user.custom_tool_result"
    assert event["custom_tool_use_id"] == eid
    assert event["is_error"] is True
    text_blocks = [c for c in event["content"] if c.get("type") == "text"]
    assert text_blocks
    assert "timed out" in text_blocks[0]["text"].lower()

    # WS got a hint frame so the modal can dismiss itself.
    timeout_frames = [
        c.args[0] for c in ws.send_json.await_args_list
        if c.args and isinstance(c.args[0], dict)
        and c.args[0].get("type") == "protocol_confirmation_timeout"
    ]
    assert timeout_frames, "expected protocol_confirmation_timeout WS frame"
    assert timeout_frames[0]["tool_use_id"] == eid

    # Future cleaned up even on timeout.
    assert eid not in session.pending_protocol_confirmations


@pytest.mark.asyncio
async def test_handle_client_protocol_confirmation_unknown_id_drops(
    tmp_path: Path,
) -> None:
    """A stale frame from a re-rendered modal must not crash the loop."""
    session = SessionState()
    # No future registered — handler should warn-and-return, not raise.
    await _handle_client_protocol_confirmation(
        session=session,
        frame={
            "type": "client.protocol_confirmation",
            "tool_use_id": "sevt_unknown",
            "decision": "accept",
        },
    )
    assert session.pending_protocol_confirmations == {}
