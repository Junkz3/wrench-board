# SPDX-License-Identifier: Apache-2.0
"""Tests for the schematic HTTP surface (wired in `api/pipeline/__init__.py`).

Three endpoints under test:

- `POST /pipeline/ingest-schematic`       — fire-and-forget ingestion
- `GET  /pipeline/packs/{slug}/schematic` — full electrical_graph.json
- `GET  /pipeline/packs/{slug}/schematic/boot` — boot_sequence + rails subset
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api import config as config_mod
from api.main import app


@pytest.fixture
def memory_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")  # AsyncAnthropic ctor
    yield tmp_path
    monkeypatch.setattr(config_mod, "_settings", None)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def pdf_file(tmp_path: Path) -> Path:
    p = tmp_path / "demo.pdf"
    p.write_bytes(b"%PDF-1.4\n% fake content\n")
    return p


def _make_electrical_graph(slug: str) -> dict:
    """Minimal but schema-complete electrical graph payload."""
    return {
        "schema_version": "1.0",
        "device_slug": slug,
        "components": {
            "U7": {
                "refdes": "U7",
                "type": "ic",
                "value": None,
                "pages": [1],
                "pins": [],
                "populated": True,
            }
        },
        "nets": {},
        "power_rails": {
            "+5V": {
                "label": "+5V",
                "voltage_nominal": 5.0,
                "source_refdes": "U7",
                "source_type": "buck",
                "enable_net": None,
                "consumers": ["U1"],
                "decoupling": ["C1"],
            },
            "+3V3": {
                "label": "+3V3",
                "voltage_nominal": 3.3,
                "source_refdes": "U1",
                "source_type": "ldo",
                "enable_net": None,
                "consumers": [],
                "decoupling": [],
            },
        },
        "typed_edges": [],
        "boot_sequence": [
            {
                "index": 1,
                "name": "PHASE 1",
                "rails_stable": ["+5V"],
                "components_entering": ["U7"],
                "triggers_next": [],
            },
            {
                "index": 2,
                "name": "PHASE 2",
                "rails_stable": ["+3V3"],
                "components_entering": ["U1"],
                "triggers_next": [],
            },
        ],
        "designer_notes": [],
        "ambiguities": [],
        "quality": {
            "total_pages": 1,
            "pages_parsed": 1,
            "orphan_cross_page_refs": 0,
            "nets_unresolved": 0,
            "components_without_value": 0,
            "components_without_mpn": 0,
            "confidence_global": 0.95,
            "degraded_mode": False,
        },
        "hierarchy": [],
    }


# ======================================================================
# POST /pipeline/ingest-schematic
# ======================================================================


def test_ingest_schematic_accepts_and_returns_202(memory_root, client, pdf_file):
    with patch(
        "api.pipeline.ingest_schematic", new=AsyncMock(return_value=None)
    ) as fake_ingest:
        res = client.post(
            "/pipeline/ingest-schematic",
            json={"device_slug": "demo-pi", "pdf_path": str(pdf_file)},
        )
    assert res.status_code == 202
    body = res.json()
    assert body["device_slug"] == "demo-pi"
    assert body["pdf_path"] == str(pdf_file)
    assert body["started"] is True
    # TestClient drains the event loop on context exit, so the background
    # task has run by the time we assert.
    assert fake_ingest.await_count == 1
    kwargs = fake_ingest.await_args.kwargs
    assert kwargs["device_slug"] == "demo-pi"
    assert kwargs["pdf_path"] == pdf_file
    assert kwargs["device_label"] is None


def test_ingest_schematic_forwards_device_label(memory_root, client, pdf_file):
    with patch(
        "api.pipeline.ingest_schematic", new=AsyncMock(return_value=None)
    ) as fake_ingest:
        client.post(
            "/pipeline/ingest-schematic",
            json={
                "device_slug": "demo-pi",
                "pdf_path": str(pdf_file),
                "device_label": "Demo Pi v1",
            },
        )
    assert fake_ingest.await_args.kwargs["device_label"] == "Demo Pi v1"


def test_ingest_schematic_rejects_missing_pdf(memory_root, client, tmp_path):
    nowhere = tmp_path / "nowhere.pdf"  # not created
    res = client.post(
        "/pipeline/ingest-schematic",
        json={"device_slug": "demo", "pdf_path": str(nowhere)},
    )
    assert res.status_code == 404
    assert "not found" in res.json()["detail"].lower()


def test_ingest_schematic_rejects_non_pdf(memory_root, client, tmp_path):
    notpdf = tmp_path / "plain.txt"
    notpdf.write_text("not a pdf")
    res = client.post(
        "/pipeline/ingest-schematic",
        json={"device_slug": "demo", "pdf_path": str(notpdf)},
    )
    assert res.status_code == 400
    assert ".pdf" in res.json()["detail"]


def test_ingest_schematic_rejects_invalid_slug(memory_root, client, pdf_file):
    # Non-slug characters should be rejected — otherwise path traversal
    # via `../` into the device_slug could write outside memory_root.
    res = client.post(
        "/pipeline/ingest-schematic",
        json={"device_slug": "../evil", "pdf_path": str(pdf_file)},
    )
    assert res.status_code == 422


def test_ingest_schematic_resolves_relative_path(memory_root, client, tmp_path, monkeypatch):
    # Simulate the server running from a working directory that contains the PDF.
    monkeypatch.chdir(tmp_path)
    relative = Path("relative.pdf")
    (tmp_path / relative).write_bytes(b"%PDF-1.4\n")
    with patch(
        "api.pipeline.ingest_schematic", new=AsyncMock(return_value=None)
    ) as fake_ingest:
        res = client.post(
            "/pipeline/ingest-schematic",
            json={"device_slug": "demo", "pdf_path": str(relative)},
        )
    assert res.status_code == 202
    # The path we forward to the orchestrator must be resolved (absolute).
    resolved = fake_ingest.await_args.kwargs["pdf_path"]
    assert resolved.is_absolute()
    assert resolved.exists()


# ======================================================================
# GET /pipeline/packs/{slug}/schematic
# ======================================================================


def test_get_schematic_returns_full_graph(memory_root, client):
    slug = "demo"
    (memory_root / slug).mkdir()
    graph = _make_electrical_graph(slug)
    (memory_root / slug / "electrical_graph.json").write_text(json.dumps(graph))

    res = client.get(f"/pipeline/packs/{slug}/schematic")
    assert res.status_code == 200
    body = res.json()
    assert body["device_slug"] == slug
    assert "U7" in body["components"]
    assert "+5V" in body["power_rails"]
    assert body["quality"]["pages_parsed"] == 1


def test_get_schematic_404_when_pack_missing(memory_root, client):
    res = client.get("/pipeline/packs/ghost/schematic")
    assert res.status_code == 404


def test_get_schematic_404_when_graph_absent(memory_root, client):
    slug = "demo"
    (memory_root / slug).mkdir()
    # pack exists but no electrical_graph.json
    res = client.get(f"/pipeline/packs/{slug}/schematic")
    assert res.status_code == 404
    assert "schematic" in res.json()["detail"].lower()


# ======================================================================
# GET /pipeline/packs/{slug}/schematic/boot
# ======================================================================


def test_get_schematic_boot_returns_subset(memory_root, client):
    slug = "demo"
    (memory_root / slug).mkdir()
    graph = _make_electrical_graph(slug)
    (memory_root / slug / "electrical_graph.json").write_text(json.dumps(graph))

    res = client.get(f"/pipeline/packs/{slug}/schematic/boot")
    assert res.status_code == 200
    body = res.json()
    # Subset only: boot_sequence + power_rails. No heavy components payload.
    assert set(body.keys()) >= {"boot_sequence", "power_rails"}
    assert "components" not in body
    assert "nets" not in body
    assert len(body["boot_sequence"]) == 2
    assert "+5V" in body["power_rails"]
    assert body["power_rails"]["+5V"]["source_refdes"] == "U7"


def test_get_schematic_boot_404_when_absent(memory_root, client):
    res = client.get("/pipeline/packs/ghost/schematic/boot")
    assert res.status_code == 404
