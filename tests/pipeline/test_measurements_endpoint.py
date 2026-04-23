# SPDX-License-Identifier: Apache-2.0
"""HTTP coverage for /pipeline/packs/{slug}/repairs/{repair_id}/measurements."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import config
from api.main import app

SLUG = "demo"
REPAIR = "r1"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def tmp_memory(tmp_path: Path, monkeypatch):
    memory_root = tmp_path / "memory"
    (memory_root / SLUG / "repairs" / REPAIR).mkdir(parents=True)
    # Reset the singleton so get_settings() will pick up the new MEMORY_ROOT.
    monkeypatch.setattr(config, "_settings", None, raising=False)
    monkeypatch.setenv("MEMORY_ROOT", str(memory_root))
    yield memory_root
    monkeypatch.setattr(config, "_settings", None, raising=False)


def test_post_measurement_records(tmp_memory: Path, client: TestClient):
    r = client.post(
        f"/pipeline/packs/{SLUG}/repairs/{REPAIR}/measurements",
        json={"target": "rail:+3V3", "value": 2.87, "unit": "V", "nominal": 3.3},
    )
    assert r.status_code == 201
    payload = r.json()
    assert payload["auto_classified_mode"] == "anomalous"


def test_get_measurements_returns_events(tmp_memory: Path, client: TestClient):
    client.post(
        f"/pipeline/packs/{SLUG}/repairs/{REPAIR}/measurements",
        json={"target": "rail:+3V3", "value": 2.87, "unit": "V", "nominal": 3.3},
    )
    r = client.get(f"/pipeline/packs/{SLUG}/repairs/{REPAIR}/measurements")
    assert r.status_code == 200
    assert len(r.json()["events"]) == 1


def test_get_measurements_filter_target(tmp_memory: Path, client: TestClient):
    client.post(
        f"/pipeline/packs/{SLUG}/repairs/{REPAIR}/measurements",
        json={"target": "rail:+3V3", "value": 2.87, "unit": "V", "nominal": 3.3},
    )
    client.post(
        f"/pipeline/packs/{SLUG}/repairs/{REPAIR}/measurements",
        json={"target": "comp:Q17", "value": 72.0, "unit": "°C"},
    )
    r = client.get(
        f"/pipeline/packs/{SLUG}/repairs/{REPAIR}/measurements?target=comp:Q17",
    )
    assert len(r.json()["events"]) == 1
    assert r.json()["events"][0]["target"] == "comp:Q17"
