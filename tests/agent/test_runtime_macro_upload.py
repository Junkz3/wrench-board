# SPDX-License-Identifier: Apache-2.0
"""Flow A handler: client.upload_macro injects user.message into MA session."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.agent.runtime_managed import _handle_client_upload_macro
from api.session.state import SessionState


@pytest.mark.asyncio
async def test_upload_macro_persists_and_injects_user_message(tmp_path: Path):
    session = SessionState()
    bytes_data = b"\xff\xd8\xff\xe0fake_jpeg"
    frame = {
        "type": "client.upload_macro",
        "base64": base64.b64encode(bytes_data).decode("ascii"),
        "mime": "image/jpeg",
        "filename": "macro_001.jpg",
    }

    fake_file = MagicMock(id="file_abc123")
    client = MagicMock()
    client.beta.files.upload = AsyncMock(return_value=fake_file)
    client.beta.sessions.events.send = AsyncMock()

    await _handle_client_upload_macro(
        client=client,
        session=session,
        memory_root=tmp_path,
        slug="iphone-x",
        repair_id="R1",
        ma_session_id="sesn_xyz",
        frame=frame,
    )

    # Persisted to disk
    macros_dir = tmp_path / "iphone-x" / "repairs" / "R1" / "macros"
    files = list(macros_dir.glob("*_manual.jpg"))
    assert len(files) == 1
    assert files[0].read_bytes() == bytes_data

    # Files API was called
    client.beta.files.upload.assert_awaited_once()

    # MA session received the user.message with image block
    client.beta.sessions.events.send.assert_awaited_once()
    send_call = client.beta.sessions.events.send.call_args
    events = (send_call.kwargs.get("events")
              if "events" in send_call.kwargs
              else send_call.args[1])
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "user.message"
    image_blocks = [b for b in event["content"] if b.get("type") == "image"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["source"] == {"type": "file", "file_id": "file_abc123"}


@pytest.mark.asyncio
async def test_upload_macro_rejects_oversized_payload(tmp_path: Path):
    session = SessionState()
    # 10 MB of bytes → exceeds the 5 MB cap
    big_bytes = b"\x00" * (10 * 1024 * 1024)
    frame = {
        "type": "client.upload_macro",
        "base64": base64.b64encode(big_bytes).decode("ascii"),
        "mime": "image/png",
        "filename": "huge.png",
    }
    client = MagicMock()
    client.beta.files.upload = AsyncMock()
    client.beta.sessions.events.send = AsyncMock()

    with pytest.raises(ValueError, match="too large"):
        await _handle_client_upload_macro(
            client=client, session=session, memory_root=tmp_path,
            slug="iphone-x", repair_id="R1", ma_session_id="sesn_xyz", frame=frame,
        )
    client.beta.files.upload.assert_not_awaited()
    client.beta.sessions.events.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_macro_rejects_invalid_base64(tmp_path: Path):
    session = SessionState()
    frame = {
        "type": "client.upload_macro",
        "base64": "###not-base64###",
        "mime": "image/png",
        "filename": "bad.png",
    }
    client = MagicMock()
    client.beta.files.upload = AsyncMock()
    client.beta.sessions.events.send = AsyncMock()

    with pytest.raises(ValueError, match="invalid base64"):
        await _handle_client_upload_macro(
            client=client, session=session, memory_root=tmp_path,
            slug="iphone-x", repair_id="R1", ma_session_id="sesn_xyz", frame=frame,
        )
