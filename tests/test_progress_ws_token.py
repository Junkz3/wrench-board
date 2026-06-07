"""Service-token enforcement on the progress WebSocket endpoint.

``/pipeline/progress/{slug}`` était protégé uniquement par ``enforce_ws_origin``,
ce qui laissait une faille identique à celle du WS diagnostic : quiconque
connaît l'URL moteur peut s'abonner directement au flux progress, contournant
le cloud front-door.

Ce fichier s'assure que ``enforce_ws_service_token`` est ajouté dans le même
ordre que ``/ws/diagnostic`` (origin → service_token → accept), et qu'il est
no-op en mode self-host (token vide).

Approche : on attaque le VRAI routeur ``api.pipeline.routes.progress`` en le
montant sur une FastAPI minimale, et on stub ``api.ws_security.get_settings``
pour les deux attributs qu'il faut (``cors_allow_origins`` + ``engine_service_token``)
— même convention que ``test_ws_service_token.py`` et ``test_ws_origin_auth.py``.
On stub aussi ``api.pipeline.events`` pour court-circuiter la queue asyncio réelle
(events.subscribe est synchrone — le stub doit l'être aussi).

Cas couverts :
    1. Token configuré + PAS de header Authorization → fermeture 1008.
    2. Token configuré + ``Authorization: Bearer <token>`` correct → accept.
    3. Token vide (self-host) → accept sans bearer.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.pipeline.routes.progress import router as progress_router

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Mini app qui monte uniquement le routeur progress."""
    app = FastAPI()
    app.include_router(progress_router)
    return app


def _patch_settings(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    """Surcharge get_settings pour les deux guards de la route progress.

    ``enforce_ws_origin`` lit ``cors_allow_origins``; ``enforce_ws_service_token``
    lit ``engine_service_token``. Le namespace doit exposer les deux.
    cors_allow_origins vide → enforce_ws_origin accepte tout (mode permissif).
    """
    monkeypatch.setattr(
        "api.ws_security.get_settings",
        lambda: SimpleNamespace(cors_allow_origins="", engine_service_token=token),
    )


def _stub_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remplace events.subscribe / unsubscribe (fonctions synchrones) pour
    éviter toute dépendance d'état externe.

    La queue retournée contient un event terminal pour que la boucle ``while
    True`` du handler se termine proprement si un test atteint le accept().
    """
    def _subscribe(slug: str) -> asyncio.Queue:  # type: ignore[type-arg]
        q: asyncio.Queue = asyncio.Queue()  # type: ignore[type-arg]
        q.put_nowait({"type": "pipeline_finished", "slug": slug})
        return q

    def _unsubscribe(slug: str, queue: asyncio.Queue) -> None:  # type: ignore[type-arg]
        pass

    monkeypatch.setattr("api.pipeline.routes.progress.events.subscribe", _subscribe)
    monkeypatch.setattr("api.pipeline.routes.progress.events.unsubscribe", _unsubscribe)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_rejects_without_token_when_managed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Token configuré + aucun header Authorization → fermeture 1008.

    Simule un accès direct (websocat, curl) à l'URL moteur sans passer par
    le relay cloud. Le guard doit s'exécuter AVANT websocket.accept() et
    fermer la connexion avec le code 1008 Policy Violation.
    """
    _patch_settings(monkeypatch, "svc-secret")
    _stub_events(monkeypatch)

    with TestClient(_build_app()) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/progress/iphone14") as ws:
                ws.receive_text()  # jamais atteint si le guard fonctionne
        assert exc.value.code == 1008, (
            f"service token manquant doit provoquer 1008, obtenu {exc.value.code}"
        )


def test_accepts_with_correct_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Token configuré + ``Authorization: Bearer <token>`` correct → accept.

    Chemin normal du relay cloud (qui envoie déjà le bearer sur ce dial).
    """
    _patch_settings(monkeypatch, "svc-secret")
    _stub_events(monkeypatch)

    with TestClient(_build_app()) as client, client.websocket_connect(
        "/progress/iphone14",
        headers={"authorization": "Bearer svc-secret"},
    ) as ws:
        # Le premier message est l'ack "subscribed" émis par la route après accept()
        data = json.loads(ws.receive_text())
        assert data["type"] == "subscribed", (
            f"connexion acceptée avec bon token, premier message inattendu : {data}"
        )


def test_accepts_without_token_in_selfhost(monkeypatch: pytest.MonkeyPatch) -> None:
    """Token vide → pas d'enforcement (self-host / standalone workbench).

    Un client sans header Authorization doit être accepté comme avant.
    """
    _patch_settings(monkeypatch, "")
    _stub_events(monkeypatch)

    with TestClient(_build_app()) as client, client.websocket_connect(
        "/progress/iphone14",
        # Pas de header Authorization
    ) as ws:
        data = json.loads(ws.receive_text())
        assert data["type"] == "subscribed", (
            f"self-host sans token : connexion doit être acceptée, reçu : {data}"
        )
