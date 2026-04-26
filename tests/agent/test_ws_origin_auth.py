# SPDX-License-Identifier: Apache-2.0
"""Origin allowlist on WebSocket endpoints.

The CORS middleware in ``api.main`` only fires for HTTP — the WebSocket
handshake bypasses it entirely. Without an Origin check at the handler
edge, any cross-origin browser page can ``new WebSocket("ws://workbench:
8000/ws/diagnostic/iphone14")`` and silently piggy-back on the active
session: read tokens streaming back, send `message` frames, drive the
boardview.

This file exercises ``api.ws_security.enforce_ws_origin`` end-to-end via
a minimal FastAPI app that mounts the helper on a dummy WS route. We
deliberately avoid importing ``api.main.app`` here — the production app
pulls in heavyweight runtime modules whose unrelated import errors would
mask the policy under test. The dummy app exercises the same Starlette /
FastAPI WS handshake path the real routes use, so the close-code 1008
behavior we assert is identical to what a browser would observe.

Policy under test:
    1. No ``Origin`` header → accept (curl / websocat / Python tests).
    2. Origin in allowlist → accept.
    3. Origin NOT in allowlist (and allowlist isn't ``*``) → close 1008.
    4. Allowlist contains ``*`` → accept anything (back-compat dev mode).
    5. Empty allowlist → accept (degraded permissive mode).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.ws_security import enforce_ws_origin

# ---------------------------------------------------------------------------
# Test app — mounts a single WS route guarded by enforce_ws_origin.
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Mini app with one Origin-checked WS route.

    The handler is a 1-frame echo: it accepts, sends ``"ready"``, then
    closes. Tests assert behavior on the wire — close code 1008 vs. a
    clean 1000 — instead of poking at the helper's return value.
    """
    app = FastAPI()

    @app.websocket("/wsx")
    async def _guarded(websocket: WebSocket) -> None:
        if not await enforce_ws_origin(websocket):
            return
        await websocket.accept()
        await websocket.send_text("ready")
        await websocket.close()

    return app


def _patch_origins(monkeypatch: pytest.MonkeyPatch, allow: str) -> None:
    """Override the CORS-origins setting that ``enforce_ws_origin`` reads.

    We patch ``api.ws_security.get_settings`` rather than mutating the
    process-wide cached Settings object — keeps the test self-contained
    and reversible.
    """
    monkeypatch.setattr(
        "api.ws_security.get_settings",
        lambda: SimpleNamespace(cors_allow_origins=allow),
    )


# ---------------------------------------------------------------------------
# Wire-level behavior tests — drive the helper through a real handshake.
# ---------------------------------------------------------------------------


def test_no_origin_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CLI client (curl, websocat, in-process TestClient) sends no Origin.
    The check must NOT reject — that would break every dev tool. Browser
    cross-origin attacks are still caught because browsers always stamp
    Origin on a WS handshake."""
    _patch_origins(monkeypatch, "http://localhost:8000")

    with TestClient(_build_app()) as client, client.websocket_connect("/wsx") as ws:
        assert ws.receive_text() == "ready"
        # Server-initiated close is propagated as WebSocketDisconnect on the
        # next read; assert we got the clean 1000, not a policy 1008.
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code != 1008


def test_allowed_origin_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Origin matches the allowlist exactly → handshake completes."""
    _patch_origins(monkeypatch, "http://localhost:8000,http://127.0.0.1:8000")

    with TestClient(_build_app()) as client, client.websocket_connect(
        "/wsx", headers={"origin": "http://localhost:8000"},
    ) as ws:
        assert ws.receive_text() == "ready"


def test_foreign_origin_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Origin not in allowlist → close code 1008 (Policy Violation).

    This is the actual CSRF-equivalent attack: a malicious page on
    ``evil.example.com`` opens a WebSocket against the workbench. The
    browser will stamp Origin; the server must reject before accepting.
    """
    _patch_origins(monkeypatch, "http://localhost:8000")

    with TestClient(_build_app()) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                "/wsx", headers={"origin": "http://evil.example.com"},
            ) as ws:
                ws.receive_text()  # should never be reached
        assert exc.value.code == 1008, (
            f"foreign Origin must be rejected with 1008, got {exc.value.code}"
        )


def test_wildcard_allows_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    """``cors_allow_origins='*'`` is the documented dev escape hatch — any
    Origin must pass, matching the CORS middleware's wildcard semantics."""
    _patch_origins(monkeypatch, "*")

    with TestClient(_build_app()) as client, client.websocket_connect(
        "/wsx", headers={"origin": "http://anything-goes.example.com"},
    ) as ws:
        assert ws.receive_text() == "ready"


def test_empty_allowlist_allows_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty allowlist (e.g. ``CORS_ALLOW_ORIGINS=''``) degrades to
    permissive mode — the server isn't configured to enforce, so it
    doesn't. Same back-compat principle as the wildcard."""
    _patch_origins(monkeypatch, "")

    with TestClient(_build_app()) as client, client.websocket_connect(
        "/wsx", headers={"origin": "http://random.example.com"},
    ) as ws:
        assert ws.receive_text() == "ready"


def test_wildcard_among_specific_origins_disables_enforcement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: if an operator drops ``*`` into a CSV list alongside
    explicit origins (``"http://localhost:8000,*"``), the wildcard still
    wins — we never want the helper to silently downgrade to ``"*" in
    list → reject everything else``."""
    _patch_origins(monkeypatch, "http://localhost:8000,*")

    with TestClient(_build_app()) as client, client.websocket_connect(
        "/wsx", headers={"origin": "http://random.example.com"},
    ) as ws:
        assert ws.receive_text() == "ready"
