"""Smoke test: GET /health must return 200 and the expected JSON shape."""

from __future__ import annotations

from fastapi.testclient import TestClient

from api import __version__
from api.main import app


def test_health_returns_ok() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload == {"status": "ok", "version": __version__}
