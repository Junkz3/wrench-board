# SPDX-License-Identifier: Apache-2.0
"""HTTP coverage for POST /pipeline/packs/{slug}/schematic/simulate.

Writes a synthetic electrical_graph.json to a tmp memory root, overrides
the settings.memory_root, and exercises the endpoint via TestClient.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.config as config_mod
from api.main import app
from api.pipeline.schematic.schemas import (
    BootPhase,
    ComponentNode,
    ElectricalGraph,
    NetNode,
    PowerRail,
    SchematicQualityReport,
)

SLUG = "demo-device"


def _build_graph() -> ElectricalGraph:
    components = {
        "U18": ComponentNode(refdes="U18", type="ic"),
        "U7":  ComponentNode(refdes="U7",  type="ic"),
        "U12": ComponentNode(refdes="U12", type="ic"),
    }
    return ElectricalGraph(
        device_slug=SLUG,
        components=components,
        nets={
            "VIN":  NetNode(label="VIN",  is_power=True, is_global=True),
            "+5V":  NetNode(label="+5V",  is_power=True, is_global=True),
            "+3V3": NetNode(label="+3V3", is_power=True, is_global=True),
        },
        power_rails={
            "VIN": PowerRail(label="VIN", consumers=["U18"]),
            "+5V": PowerRail(label="+5V", source_refdes="U7", consumers=["U12"]),
            "+3V3": PowerRail(label="+3V3", source_refdes="U12"),
        },
        typed_edges=[],
        boot_sequence=[
            BootPhase(index=1, name="PHASE 1 — VIN", rails_stable=["VIN"], components_entering=["U18"]),
            BootPhase(index=2, name="PHASE 2 — 5V", rails_stable=["+5V"], components_entering=["U7", "U12"]),
        ],
        designer_notes=[],
        ambiguities=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )


@pytest.fixture
def tmp_memory(tmp_path: Path, monkeypatch):
    memory_root = tmp_path / "memory"
    pack = memory_root / SLUG
    pack.mkdir(parents=True)
    (pack / "electrical_graph.json").write_text(_build_graph().model_dump_json(indent=2))

    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setenv("MEMORY_ROOT", str(memory_root))
    yield memory_root
    monkeypatch.setattr(config_mod, "_settings", None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_simulate_normal_boot_returns_full_timeline(tmp_memory: Path, client: TestClient):
    r = client.post(f"/pipeline/packs/{SLUG}/schematic/simulate", json={"killed_refdes": []})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["device_slug"] == SLUG
    assert payload["final_verdict"] in ("completed", "cascade")
    assert isinstance(payload["states"], list)
    assert len(payload["states"]) >= 1


def test_simulate_with_killed_refdes(tmp_memory: Path, client: TestClient):
    r = client.post(
        f"/pipeline/packs/{SLUG}/schematic/simulate",
        json={"killed_refdes": ["U7"]},
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["killed_refdes"] == ["U7"]


def test_simulate_unknown_refdes_returns_400(tmp_memory: Path, client: TestClient):
    r = client.post(
        f"/pipeline/packs/{SLUG}/schematic/simulate",
        json={"killed_refdes": ["Z999"]},
    )
    assert r.status_code == 400
    assert "Z999" in r.text


def test_simulate_no_graph_returns_404(tmp_path: Path, monkeypatch, client: TestClient):
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setenv("MEMORY_ROOT", str(memory_root))
    try:
        r = client.post("/pipeline/packs/nothing-here/schematic/simulate", json={})
        assert r.status_code == 404
    finally:
        monkeypatch.setattr(config_mod, "_settings", None)


def test_simulate_endpoint_accepts_failures(tmp_memory: Path, client: TestClient):
    r = client.post(
        f"/pipeline/packs/{SLUG}/schematic/simulate",
        json={
            "failures": [
                {"refdes": "U7", "mode": "regulating_low", "voltage_pct": 0.85}
            ]
        },
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["device_slug"] == SLUG


def test_simulate_endpoint_accepts_rail_overrides(tmp_memory: Path, client: TestClient):
    r = client.post(
        f"/pipeline/packs/{SLUG}/schematic/simulate",
        json={
            "rail_overrides": [
                {"label": "+5V", "state": "degraded", "voltage_pct": 0.85}
            ]
        },
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    # Endpoint returns the raw timeline — probe_route never populated server-side.
    assert "probe_route" not in payload


def test_simulate_endpoint_rejects_invalid_failure(tmp_memory: Path, client: TestClient):
    r = client.post(
        f"/pipeline/packs/{SLUG}/schematic/simulate",
        json={"failures": [{"refdes": "U7", "mode": "totally_made_up"}]},
    )
    # Pydantic 422 from FastAPI on invalid mode literal.
    assert r.status_code == 422
