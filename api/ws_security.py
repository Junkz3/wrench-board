"""WebSocket-level security helpers.

The CORS middleware in ``api.main`` only fires for HTTP requests; the
WebSocket handshake bypasses it entirely. Without an explicit Origin
check, any web page on any host can ``new WebSocket("ws://workbench:8000/
ws/diagnostic/iphone14")`` and silently piggy-back on the technician's
session — read tokens, inject `message` frames, drive the boardview.

`enforce_ws_origin` runs an Origin allowlist (from
``settings.cors_allow_origins``) *before* the handshake completes. On
rejection it closes the socket with RFC 6455 close code 1008
("Policy Violation") and returns ``False`` so the caller can early-exit.
"""

from __future__ import annotations

from fastapi import WebSocket

from api.config import get_settings


def _allowed_origins() -> list[str]:
    """Return the list of allowed origins from settings.

    Mirrors the CSV-parsing convention used elsewhere for CORS-style
    allowlists so both the HTTP middleware and the WS guard share one
    source of truth.
    """
    raw = get_settings().cors_allow_origins
    return [o.strip() for o in raw.split(",") if o.strip()]


async def enforce_ws_origin(websocket: WebSocket) -> bool:
    """Validate the WebSocket Origin header against the configured allowlist.

    Policy (permissive — picks security without breaking dev tooling):

    1. Empty allowlist or ``"*"`` in the list → accept anything (back-compat
       dev mode, matches the CORS middleware's wildcard semantics).
    2. No ``Origin`` header on the request → accept. Browsers always send
       Origin on a WebSocket handshake (the ``WebSocket`` constructor
       sets it automatically), so a missing header indicates a non-browser
       client (curl, websocat, Python's ``websockets``, internal test
       harness). Cross-origin browser attacks — the actual threat model
       here — are still blocked because the browser will always stamp
       Origin.
    3. Origin present and listed → accept.
    4. Origin present and NOT listed → close with code 1008 and return
       ``False``. The caller MUST stop processing in that case (the socket
       is already closed; further sends raise).

    Returns ``True`` when the handshake may proceed, ``False`` when the
    socket has been closed.
    """
    allowed = _allowed_origins()
    if not allowed or "*" in allowed:
        return True

    origin = websocket.headers.get("origin")
    if not origin:
        # Non-browser client — Origin is optional outside browsers.
        return True

    if origin in allowed:
        return True

    await websocket.close(code=1008, reason="Forbidden origin")
    return False
