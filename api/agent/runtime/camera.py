"""Flow B dispatcher for the ``cam_capture`` custom tool.

Pushes a ``server.capture_request`` to the WS, awaits the matching
``client.capture_response`` (resolved via
:func:`_handle_client_capture_response`), then forwards the captured
frame to the MA session as the matching ``user.custom_tool_result``.
"""

from __future__ import annotations

import asyncio
import base64 as _b64
import secrets
from pathlib import Path

from anthropic import AsyncAnthropic
from fastapi import WebSocket

from api.agent import runtime_managed as _rm
from api.agent.macros import persist_macro
from api.agent.runtime._aux import logger
from api.session.state import SessionState


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
    timeout_s: float | None = None,
) -> None:
    """Flow B dispatcher: push capture_request, await response, send tool_result.

    Always sends back exactly one user.custom_tool_result for the given
    tool_use_id — either with the captured image (success) or is_error
    (timeout / decode failure / Files API failure / no-camera). Cleans up
    the pending Future on every exit path.

    `timeout_s` defaults to settings.ma_camera_capture_timeout_seconds
    when omitted (kept overridable for fast unit tests).
    """
    if timeout_s is None:
        timeout_s = _rm.get_settings().ma_camera_capture_timeout_seconds
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
