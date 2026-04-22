"""Tests for POST /pipeline/repairs + WS /pipeline/progress/{slug}.

The real pipeline calls Anthropic and takes tens of seconds; these tests
patch `generate_knowledge_pack` to an instant stub and publish events
directly on the bus so we can validate wiring without network.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api import config as config_mod
from api.main import app
from api.pipeline import events


@pytest.fixture
def memory_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    yield tmp_path
    monkeypatch.setattr(config_mod, "_settings", None)


@pytest.fixture(autouse=True)
def _reset_bus():
    events.reset()
    yield
    events.reset()


@pytest.fixture
def client():
    return TestClient(app)


async def _fake_pipeline(device_label, **kwargs):
    """Drop-in replacement for generate_knowledge_pack: emits events, returns None."""
    on_event = kwargs.get("on_event")
    if on_event:
        await on_event({"type": "pipeline_started", "device_slug": "demo", "device_label": device_label})
        await on_event({"type": "phase_started", "phase": "scout"})
        await on_event({"type": "phase_finished", "phase": "scout", "elapsed_s": 0.01})
        await on_event({"type": "pipeline_finished", "device_slug": "demo", "status": "APPROVED",
                        "revise_rounds_used": 0, "consistency_score": 1.0})


def test_repairs_endpoint_returns_id_and_slug(memory_root, client):
    with patch("api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)):
        res = client.post(
            "/pipeline/repairs",
            json={"device_label": "Demo Pi", "symptom": "no 3V3 rail, device won't power on"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["device_slug"] == "demo-pi"
    assert len(body["repair_id"]) > 0
    assert body["pipeline_started"] is True


def test_repairs_endpoint_persists_symptom_file(memory_root, client):
    with patch("api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)):
        res = client.post(
            "/pipeline/repairs",
            json={"device_label": "Demo Pi", "symptom": "dead PMIC"},
        )
    body = res.json()
    repair_file = memory_root / body["device_slug"] / "repairs" / f"{body['repair_id']}.json"
    assert repair_file.exists()
    data = json.loads(repair_file.read_text())
    assert data["symptom"] == "dead PMIC"
    assert data["device_label"] == "Demo Pi"
    assert data["device_slug"] == "demo-pi"
    assert "created_at" in data


def test_repairs_endpoint_skips_pipeline_when_pack_already_complete(memory_root, client):
    """If the pack already has the 4 writer files, we skip the pipeline rebuild."""
    slug_dir = memory_root / "demo-pi"
    slug_dir.mkdir()
    # Bare-minimum content that makes _summarize_pack flag the pack as "complete".
    (slug_dir / "registry.json").write_text('{"schema_version":"1.0","device_label":"Demo Pi","components":[],"signals":[]}')
    (slug_dir / "knowledge_graph.json").write_text('{"schema_version":"1.0","nodes":[],"edges":[]}')
    (slug_dir / "rules.json").write_text('{"schema_version":"1.0","rules":[]}')
    (slug_dir / "dictionary.json").write_text('{"schema_version":"1.0","entries":[]}')

    with patch("api.pipeline.generate_knowledge_pack", new=AsyncMock(side_effect=_fake_pipeline)) as m:
        res = client.post(
            "/pipeline/repairs",
            json={"device_label": "Demo Pi", "symptom": "pack already exists on disk"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["pipeline_started"] is False
    # The pipeline must not have been kicked off.
    m.assert_not_called()


def test_repairs_endpoint_rejects_short_input(memory_root, client):
    res = client.post("/pipeline/repairs", json={"device_label": "x", "symptom": "tiny"})
    assert res.status_code == 422


def test_progress_ws_streams_events_from_the_bus(memory_root, client):
    """The WS relays every event published to its slug."""
    with client.websocket_connect("/pipeline/progress/demo-pi") as ws:
        # The server acknowledges the subscription with a "subscribed" frame
        # so the client knows events published from now on will be delivered.
        ack = json.loads(ws.receive_text())
        assert ack == {"type": "subscribed", "device_slug": "demo-pi"}

        async def push():
            # Tiny delay so the WS receive loop is already awaiting.
            await asyncio.sleep(0.05)
            await events.publish("demo-pi", {"type": "phase_started", "phase": "scout"})
            await events.publish("demo-pi", {"type": "pipeline_finished", "status": "APPROVED"})

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(push())
        finally:
            loop.close()

        ev1 = json.loads(ws.receive_text())
        ev2 = json.loads(ws.receive_text())
        assert ev1 == {"type": "phase_started", "phase": "scout"}
        assert ev2 == {"type": "pipeline_finished", "status": "APPROVED"}


def test_progress_ws_ignores_events_for_other_slugs(memory_root, client):
    with client.websocket_connect("/pipeline/progress/demo-pi") as ws:
        json.loads(ws.receive_text())  # subscribed ack

        async def push():
            await asyncio.sleep(0.05)
            await events.publish("other-device", {"type": "phase_started"})
            await events.publish("demo-pi", {"type": "pipeline_finished"})

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(push())
        finally:
            loop.close()

        # Only our slug's event arrives.
        ev = json.loads(ws.receive_text())
        assert ev == {"type": "pipeline_finished"}
