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

from api._token_check import extract_bearer, token_matches
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


async def enforce_ws_service_token(websocket: WebSocket) -> bool:
    """Require the cloud gateway's service token on the WebSocket handshake.

    The Origin check above stops cross-origin *browsers* but accepts any
    non-browser client (no Origin header) — including ``websocat``. Once the
    engine is deployed behind wrenchboard-cloud that's a hole: anyone who
    learns the engine URL can open a diagnostic session directly, skipping the
    cloud's auth + quota and spending Anthropic credits. This check closes it.

    Policy (permissive by default, mirroring ``enforce_ws_origin``):

    1. ``settings.engine_service_token`` empty → enforcement off (standalone
       workbench / dev — a browser can't set the Authorization header, so the
       direct-to-engine dev flow runs with the token unset).
    2. Token configured and the request carries ``Authorization: Bearer
       <token>`` matching it → accept.
    3. Token configured and the header is missing, malformed (no ``Bearer``
       scheme), or carries the wrong value → close with code 1008 and return
       ``False``. The caller MUST stop processing (the socket is already
       closed).

    The token comparison is constant-time (``secrets.compare_digest``) so a
    rejected attempt leaks nothing about how many leading bytes matched.
    """
    expected = get_settings().engine_service_token
    if not expected:
        return True

    if token_matches(extract_bearer(websocket.headers.get("authorization", "")), expected):
        return True

    await websocket.close(code=1008, reason="Forbidden: service token required")
    return False
