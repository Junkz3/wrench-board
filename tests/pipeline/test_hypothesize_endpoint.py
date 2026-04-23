# tests/pipeline/test_hypothesize_endpoint.py
# SPDX-License-Identifier: Apache-2.0
"""HTTP coverage for POST /pipeline/packs/{slug}/schematic/hypothesize."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import config as config_mod
from api.main import app
from api.pipeline.schematic.schemas import (
    ComponentNode,
    ElectricalGraph,
    NetNode,
    PagePin,
    PowerRail,
    SchematicQualityReport,
)

SLUG = "demo-device"


def _build_graph() -> ElectricalGraph:
    return ElectricalGraph(
        device_slug=SLUG,
        components={
            "U7": ComponentNode(refdes="U7", type="ic", pins=[
                PagePin(number="1", role="power_in", net_label="VIN"),
                PagePin(number="2", role="power_out", net_label="+5V"),
            ]),
            "U12": ComponentNode(refdes="U12", type="ic", pins=[
                PagePin(number="1", role="power_in", net_label="+5V"),
            ]),
        },
        nets={
            "VIN": NetNode(label="VIN", is_power=True, is_global=True),
            "+5V": NetNode(label="+5V", is_power=True, is_global=True),
        },
        power_rails={
            "VIN": PowerRail(label="VIN", source_refdes=None),
            "+5V": PowerRail(label="+5V", source_refdes="U7", consumers=["U12"]),
        },
        typed_edges=[], boot_sequence=[], designer_notes=[], ambiguities=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )


@pytest.fixture
def tmp_memory(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    pack = tmp_path / SLUG
    pack.mkdir(parents=True)
    (pack / "electrical_graph.json").write_text(_build_graph().model_dump_json(indent=2))
    yield tmp_path
    monkeypatch.setattr(config_mod, "_settings", None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_hypothesize_happy_schema_b(tmp_memory: Path, client: TestClient):
    r = client.post(
        f"/pipeline/packs/{SLUG}/schematic/hypothesize",
        json={"state_rails": {"+5V": "dead"}, "state_comps": {"U12": "dead"}},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["device_slug"] == SLUG
    assert payload["hypotheses"][0]["kill_refdes"] == ["U7"]
    assert payload["hypotheses"][0]["kill_modes"] == ["dead"]


def test_hypothesize_accepts_metrics(tmp_memory: Path, client: TestClient):
    r = client.post(
        f"/pipeline/packs/{SLUG}/schematic/hypothesize",
        json={
            "state_rails": {"+5V": "dead"},
            "metrics_rails": {"+5V": {"measured": 0.02, "unit": "V", "nominal": 5.0}},
        },
    )
    assert r.status_code == 200
    # Measurement cited.
    assert "0.02" in r.text or "5.0" in r.text


def test_hypothesize_unknown_refdes_400(tmp_memory: Path, client: TestClient):
    r = client.post(
        f"/pipeline/packs/{SLUG}/schematic/hypothesize",
        json={"state_comps": {"Z999": "dead"}},
    )
    assert r.status_code == 400
    assert "Z999" in r.text


def test_hypothesize_no_graph_404(tmp_path: Path, monkeypatch, client: TestClient):
    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    try:
        r = client.post(
            "/pipeline/packs/nothing-here/schematic/hypothesize",
            json={"state_rails": {"+5V": "dead"}},
        )
        assert r.status_code == 404
    finally:
        monkeypatch.setattr(config_mod, "_settings", None)


def test_hypothesize_empty_body_returns_empty(tmp_memory: Path, client: TestClient):
    r = client.post(
        f"/pipeline/packs/{SLUG}/schematic/hypothesize",
        json={},
    )
    assert r.status_code == 200
    assert r.json()["hypotheses"] == []
