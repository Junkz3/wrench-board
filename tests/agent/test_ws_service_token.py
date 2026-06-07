"""Service-token enforcement on WebSocket endpoints.

The Origin allowlist (``enforce_ws_origin``) only stops *cross-origin
browser* pages — it deliberately accepts any client that sends no Origin
header (curl, websocat, the cloud relay). That is the right call for the
standalone workbench, but it leaves a hole the moment the engine is
deployed behind the wrenchboard-cloud gateway: anyone who learns the
engine's URL can ``websocat ws://engine/ws/diagnostic/iphone14`` directly,
bypass the cloud's auth + quota, and burn Anthropic credits.

``enforce_ws_service_token`` closes that hole. When a service token is
configured (``settings.engine_service_token``), the diagnostic WS handshake
must carry ``Authorization: Bearer <token>`` — the shared secret only the
cloud relay holds. The cloud is a server, so it CAN set the header (a
browser cannot, which is why the standalone workbench runs with the token
unset → enforcement off).

This mirrors ``test_ws_origin_auth.py``: a minimal FastAPI app mounts the
helper on a dummy WS route and we assert behavior on the wire (clean 1000
vs. policy 1008), exercising the same Starlette handshake path the real
``/ws/diagnostic`` route uses, without importing the heavyweight
``api.main.app``.

Policy under test:
    1. No token configured (empty) → accept anything (standalone/dev mode).
    2. Token configured + matching ``Authorization: Bearer <token>`` → accept.
    3. Token configured + missing Authorization header → close 1008.
    4. Token configured + wrong token → close 1008.
    5. Token configured + malformed header (no ``Bearer`` scheme) → close 1008.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.ws_security import enforce_ws_service_token

# ---------------------------------------------------------------------------
# Test app — mounts a single WS route guarded by enforce_ws_service_token.
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Mini app with one service-token-checked WS route.

    The handler is a 1-frame echo: accept, send ``"ready"``, close. Tests
    assert behavior on the wire (close code 1008 vs. clean 1000) rather than
    the helper's return value.
    """
    app = FastAPI()

    @app.websocket("/wsx")
    async def _guarded(websocket: WebSocket) -> None:
        if not await enforce_ws_service_token(websocket):
            return
        await websocket.accept()
        await websocket.send_text("ready")
        await websocket.close()

    return app


def _patch_token(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    """Override the engine_service_token setting the helper reads.

    Patch ``api.ws_security.get_settings`` rather than the process-wide
    cached Settings — self-contained and reversible, same convention as
    ``test_ws_origin_auth.py``.
    """
    monkeypatch.setattr(
        "api.ws_security.get_settings",
        lambda: SimpleNamespace(engine_service_token=token),
    )


# ---------------------------------------------------------------------------
# Wire-level behavior tests — drive the helper through a real handshake.
# ---------------------------------------------------------------------------


def test_no_token_configured_accepts_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty ``engine_service_token`` → enforcement off (standalone workbench /
    dev). A client with no Authorization header must be accepted."""
    _patch_token(monkeypatch, "")

    with TestClient(_build_app()) as client, client.websocket_connect("/wsx") as ws:
        assert ws.receive_text() == "ready"


def test_correct_token_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured token + matching ``Authorization: Bearer`` → handshake
    completes. This is the cloud relay's path."""
    _patch_token(monkeypatch, "svc-secret")

    with TestClient(_build_app()) as client, client.websocket_connect(
        "/wsx", headers={"authorization": "Bearer svc-secret"},
    ) as ws:
        assert ws.receive_text() == "ready"


def test_missing_authorization_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Token configured but no Authorization header → close 1008. This is the
    direct ``websocat ws://engine/...`` credit-burn attempt the token exists
    to block."""
    _patch_token(monkeypatch, "svc-secret")

    with TestClient(_build_app()) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/wsx") as ws:
                ws.receive_text()  # never reached
        assert exc.value.code == 1008, (
            f"missing service token must be rejected with 1008, got {exc.value.code}"
        )


def test_wrong_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Token configured + wrong bearer value → close 1008."""
    _patch_token(monkeypatch, "svc-secret")

    with TestClient(_build_app()) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                "/wsx", headers={"authorization": "Bearer nope"},
            ) as ws:
                ws.receive_text()  # never reached
        assert exc.value.code == 1008


def test_malformed_header_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Token configured + header without the ``Bearer`` scheme (e.g. the raw
    token) → close 1008. We accept only the explicit ``Bearer <token>`` form."""
    _patch_token(monkeypatch, "svc-secret")

    with TestClient(_build_app()) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                "/wsx", headers={"authorization": "svc-secret"},
            ) as ws:
                ws.receive_text()  # never reached
        assert exc.value.code == 1008
