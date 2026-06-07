"""Files+Vision (Flow A + Flow B) client-frame handlers.

Each function consumes one specific client-side WS frame
(``client.capabilities``, ``client.upload_macro``,
``client.capture_response``, ``client.protocol_confirmation``) and
either updates session state, forwards a payload to the MA session, or
resolves a parked Future the runtime is waiting on.
"""

from __future__ import annotations

import base64 as _b64
from pathlib import Path

from anthropic import AsyncAnthropic

from api.agent.macros import persist_macro
from api.agent.runtime._aux import _MAX_MACRO_BYTES, logger
from api.session.state import SessionState


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


async def _handle_client_protocol_confirmation(
    *,
    session: SessionState,
    frame: dict,
) -> None:
    """Resolve the pending Future for the matching tool_use_id.

    Frame shape::

        {"type": "client.protocol_confirmation",
         "tool_use_id": "sevt_…",
         "decision": "accept" | "reject",
         "reason": "..." (optional, surfaced to the agent on reject)}

    Unknown tool_use_id is logged and dropped — the runtime owns the future
    lifecycle and a stale frame from a re-rendered modal should not crash.
    """
    tool_use_id = frame.get("tool_use_id")
    if not tool_use_id or tool_use_id not in session.pending_protocol_confirmations:
        logger.warning(
            "[Diag-MA] protocol_confirmation with unknown tool_use_id: %r",
            tool_use_id,
        )
        return
    fut = session.pending_protocol_confirmations[tool_use_id]
    if not fut.done():
        fut.set_result(frame)
