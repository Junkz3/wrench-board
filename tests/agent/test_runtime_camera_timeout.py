"""Flow B timeout: no client.capture_response → is_error tool_result."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.agent.runtime_managed import _dispatch_cam_capture
from api.session.state import SessionState


@pytest.mark.asyncio
async def test_cam_capture_timeout_returns_is_error(tmp_path: Path):
    session = SessionState()
    session.has_camera = True

    client = MagicMock()
    client.beta.files.upload = AsyncMock()
    client.beta.sessions.events.send = AsyncMock()
    ws = MagicMock()
    ws.send_json = AsyncMock()

    # Don't simulate any client.capture_response — let it time out.
    await _dispatch_cam_capture(
        client=client, session=session, ws=ws,
        memory_root=tmp_path, slug="iphone-x", repair_id="R1",
        ma_session_id="sesn_xyz", tool_use_id="sevt_tool123",
        tool_input={"reason": "test timeout"},
        timeout_s=0.2,  # short for fast test
    )

    # Files API never called
    client.beta.files.upload.assert_not_awaited()

    # Tool result sent with is_error
    client.beta.sessions.events.send.assert_awaited_once()
    send_call = client.beta.sessions.events.send.call_args
    sent = (send_call.kwargs.get("events")
            if "events" in send_call.kwargs
            else send_call.args[1])
    event = sent[0]
    assert event["type"] == "user.custom_tool_result"
    assert event["custom_tool_use_id"] == "sevt_tool123"
    assert event.get("is_error") is True
    text = [c for c in event["content"] if c.get("type") == "text"]
    assert text and "timeout" in text[0]["text"].lower()

    # Future cleaned up even on timeout
    assert len(session.pending_captures) == 0
