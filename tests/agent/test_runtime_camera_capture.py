"""Flow B happy path: cam_capture → server.capture_request → client.capture_response."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.agent.runtime_managed import (
    _dispatch_cam_capture,
    _handle_client_capture_response,
)
from api.session.state import SessionState


@pytest.mark.asyncio
async def test_cam_capture_full_round_trip(tmp_path: Path):
    session = SessionState()
    session.has_camera = True
    bytes_data = b"\xff\xd8\xff\xe0captured_frame"

    fake_file = MagicMock(id="file_capture123")
    client = MagicMock()
    client.beta.files.upload = AsyncMock(return_value=fake_file)
    client.beta.sessions.events.send = AsyncMock()
    ws = MagicMock()
    ws.send_json = AsyncMock()

    # Schedule the frontend "response" to arrive after dispatch starts.
    async def simulate_frontend():
        # Wait for the dispatch to register a pending capture.
        for _ in range(50):
            if session.pending_captures:
                break
            await asyncio.sleep(0.02)
        request_id = next(iter(session.pending_captures))
        await _handle_client_capture_response(
            session=session,
            frame={
                "type": "client.capture_response",
                "request_id": request_id,
                "base64": base64.b64encode(bytes_data).decode("ascii"),
                "mime": "image/jpeg",
                "device_label": "HD USB Camera",
            },
        )

    asyncio.create_task(simulate_frontend())

    await _dispatch_cam_capture(
        client=client,
        session=session,
        ws=ws,
        memory_root=tmp_path,
        slug="iphone-x",
        repair_id="R1",
        ma_session_id="sesn_xyz",
        tool_use_id="sevt_tool123",
        tool_input={"reason": "looking at U2"},
        timeout_s=2.0,
    )

    # WS push happened (capture request sent to frontend)
    ws.send_json.assert_awaited()
    pushed = ws.send_json.call_args.args[0]
    assert pushed["type"] == "server.capture_request"
    assert "request_id" in pushed
    assert pushed["tool_use_id"] == "sevt_tool123"

    # Persisted on disk
    macros = list((tmp_path / "iphone-x" / "repairs" / "R1" / "macros").glob("*_capture.jpg"))
    assert len(macros) == 1

    # Files API was called
    client.beta.files.upload.assert_awaited_once()

    # Tool result sent back to MA
    client.beta.sessions.events.send.assert_awaited_once()
    send_call = client.beta.sessions.events.send.call_args
    sent = (send_call.kwargs.get("events")
            if "events" in send_call.kwargs
            else send_call.args[1])
    event = sent[0]
    assert event["type"] == "user.custom_tool_result"
    assert event["custom_tool_use_id"] == "sevt_tool123"
    img = [c for c in event["content"] if c.get("type") == "image"]
    assert img and img[0]["source"]["file_id"] == "file_capture123"
    text = [c for c in event["content"] if c.get("type") == "text"]
    assert text and "HD USB Camera" in text[0]["text"]

    # Future cleaned up
    assert len(session.pending_captures) == 0
