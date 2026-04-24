# SPDX-License-Identifier: Apache-2.0
"""Tests for the schematic HTTP surface (wired in `api/pipeline/__init__.py`).

Three endpoints under test:

- `POST /pipeline/ingest-schematic`       — fire-and-forget ingestion
- `GET  /pipeline/packs/{slug}/schematic` — full electrical_graph.json
- `GET  /pipeline/packs/{slug}/schematic/boot` — boot_sequence + rails subset
"""

from __future__ import annotations

import json
import sys
import types
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


# ======================================================================
# Analyzer overlay — boot_sequence_analyzed.json is surfaced by GET
# /schematic and POST /analyze-boot fires the Opus pass in background.
# ======================================================================


def _write_analyzed_payload(pack_dir):
    pack_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "device_slug": pack_dir.name,
        "phases": [
            {"index": 0, "name": "Always-on", "kind": "always-on",
             "rails_stable": ["+3V3_STANDBY"], "components_entering": ["U14"],
             "triggers_next": [], "evidence": ["note p4"], "confidence": 0.95},
        ],
        "sequencer_refdes": "LPC",
        "global_confidence": 0.9,
        "ambiguities": [],
        "model_used": "claude-opus-4-7",
    }
    (pack_dir / "boot_sequence_analyzed.json").write_text(json.dumps(payload, indent=2))


def test_get_schematic_surfaces_analyzer_when_present(memory_root, client):
    slug = "demo"
    (memory_root / slug).mkdir()
    (memory_root / slug / "electrical_graph.json").write_text(
        json.dumps(_make_electrical_graph(slug))
    )
    _write_analyzed_payload(memory_root / slug)

    res = client.get(f"/pipeline/packs/{slug}/schematic")
    assert res.status_code == 200
    body = res.json()
    assert body["boot_sequence_source"] == "analyzer"
    assert body["analyzed_boot_sequence"]["sequencer_refdes"] == "LPC"
    assert body["analyzed_boot_sequence"]["phases"][0]["kind"] == "always-on"


def test_get_schematic_defaults_to_compiler_source_without_analyzer(memory_root, client):
    slug = "demo"
    (memory_root / slug).mkdir()
    (memory_root / slug / "electrical_graph.json").write_text(
        json.dumps(_make_electrical_graph(slug))
    )

    res = client.get(f"/pipeline/packs/{slug}/schematic")
    assert res.status_code == 200
    body = res.json()
    assert body["boot_sequence_source"] == "compiler"
    assert "analyzed_boot_sequence" not in body


def test_post_analyze_boot_kicks_off_background_task(memory_root, client):
    slug = "demo"
    (memory_root / slug).mkdir()
    (memory_root / slug / "electrical_graph.json").write_text(
        json.dumps(_make_electrical_graph(slug))
    )

    fake_analyzed = type("FakeAnalyzed", (), {
        "model_dump_json": lambda self, **k: json.dumps({
            "schema_version": "1.0",
            "device_slug": slug,
            "phases": [],
            "sequencer_refdes": None,
            "global_confidence": 0.5,
            "ambiguities": [],
            "model_used": "test-model",
        }),
        "phases": [], "sequencer_refdes": None, "global_confidence": 0.5,
    })()

    # boot_analyzer is an optional WIP module that may not exist on this branch.
    # Inject a stub into sys.modules so that the lazy import inside the try block
    # resolves, and patch.object can intercept the call.
    stub_mod = types.ModuleType("api.pipeline.schematic.boot_analyzer")
    stub_mod.analyze_boot_sequence = AsyncMock(return_value=fake_analyzed)  # type: ignore[attr-defined]
    with patch.dict(sys.modules, {"api.pipeline.schematic.boot_analyzer": stub_mod}):
        with patch.object(stub_mod, "analyze_boot_sequence", new=AsyncMock(return_value=fake_analyzed)) as fake:
            res = client.post(f"/pipeline/packs/{slug}/schematic/analyze-boot")

    assert res.status_code == 202
    body = res.json()
    assert body["started"] is True
    assert body["device_slug"] == slug
    # Background task ran by the time the TestClient context drained.
    assert fake.await_count == 1


def test_post_analyze_boot_rejects_missing_pack(memory_root, client):
    res = client.post("/pipeline/packs/ghost/schematic/analyze-boot")
    assert res.status_code == 404


def test_post_analyze_boot_rejects_missing_graph(memory_root, client):
    slug = "demo"
    (memory_root / slug).mkdir()
    # pack dir exists but no electrical_graph.json yet
    res = client.post(f"/pipeline/packs/{slug}/schematic/analyze-boot")
    assert res.status_code == 404


# ======================================================================
# Net classification — classify-nets endpoint + GET /schematic overlay
# ======================================================================


def _write_nets_classified(pack_dir):
    pack_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "device_slug": pack_dir.name,
        "nets": {
            "+5V": {"label": "+5V", "domain": "power_rail",
                    "description": "5V main", "voltage_level": "rail 5V",
                    "confidence": 0.98},
        },
        "domain_summary": {"power_rail": 1},
        "ambiguities": [],
        "model_used": "claude-opus-4-7",
    }
    (pack_dir / "nets_classified.json").write_text(json.dumps(payload, indent=2))


def test_get_schematic_surfaces_net_classification_when_present(memory_root, client):
    slug = "demo"
    (memory_root / slug).mkdir()
    (memory_root / slug / "electrical_graph.json").write_text(
        json.dumps(_make_electrical_graph(slug))
    )
    _write_nets_classified(memory_root / slug)

    res = client.get(f"/pipeline/packs/{slug}/schematic")
    assert res.status_code == 200
    body = res.json()
    assert body["net_domains_source"] == "claude-opus-4-7"
    assert "+5V" in body["net_classification"]["nets"]


def test_get_schematic_net_domains_source_none_without_classification(memory_root, client):
    slug = "demo"
    (memory_root / slug).mkdir()
    (memory_root / slug / "electrical_graph.json").write_text(
        json.dumps(_make_electrical_graph(slug))
    )
    res = client.get(f"/pipeline/packs/{slug}/schematic")
    body = res.json()
    assert body["net_domains_source"] == "none"


def test_post_classify_nets_kicks_off_background(memory_root, client):
    slug = "demo"
    (memory_root / slug).mkdir()
    (memory_root / slug / "electrical_graph.json").write_text(
        json.dumps(_make_electrical_graph(slug))
    )
    fake_classification = type("Fake", (), {
        "model_dump_json": lambda self, **k: json.dumps({
            "schema_version": "1.0", "device_slug": slug,
            "nets": {}, "domain_summary": {}, "ambiguities": [],
            "model_used": "test-model",
        }),
        "nets": {}, "domain_summary": {}, "model_used": "test-model",
    })()
    with patch(
        "api.pipeline.classify_nets",
        new=AsyncMock(return_value=fake_classification),
    ) as fake:
        res = client.post(f"/pipeline/packs/{slug}/schematic/classify-nets")
    assert res.status_code == 202
    assert res.json()["started"] is True
    assert fake.await_count == 1


def test_post_classify_nets_404_missing_pack(memory_root, client):
    res = client.post("/pipeline/packs/ghost/schematic/classify-nets")
    assert res.status_code == 404


def test_post_classify_nets_404_missing_graph(memory_root, client):
    slug = "demo"
    (memory_root / slug).mkdir()
    res = client.post(f"/pipeline/packs/{slug}/schematic/classify-nets")
    assert res.status_code == 404


# ======================================================================
# GET /pipeline/packs/{slug}/schematic/passives
# ======================================================================


def test_get_schematic_passives_returns_classifier_output(memory_root, client):
    """Smoke — the endpoint returns kind/role/confidence per passive."""
    slug = "passives-endpoint-test"
    (memory_root / slug).mkdir()
    graph = {
        "schema_version": "1.0",
        "device_slug": slug,
        "components": {
            "C156": {
                "refdes": "C156",
                "type": "capacitor",
                "kind": "passive_c",
                "role": "decoupling",
                "value": "10µF",
                "pages": [1],
                "pins": [],
                "populated": True,
            },
            "U7": {
                "refdes": "U7",
                "type": "ic",
                "kind": "ic",
                "role": None,
                "value": None,
                "pages": [1],
                "pins": [],
                "populated": True,
            },
        },
        "nets": {},
        "power_rails": {},
        "typed_edges": [],
        "quality": {
            "total_pages": 1,
            "pages_parsed": 1,
            "confidence_global": 1.0,
        },
        "boot_sequence": [],
        "designer_notes": [],
        "ambiguities": [],
        "hierarchy": [],
    }
    (memory_root / slug / "electrical_graph.json").write_text(json.dumps(graph))

    res = client.get(f"/pipeline/packs/{slug}/schematic/passives")
    assert res.status_code == 200
    body = res.json()
    # Should be a list
    assert isinstance(body, list)
    # C156 should appear
    assert any(row["refdes"] == "C156" for row in body)
    # U7 is an IC and MUST NOT appear in the response.
    assert all(row["refdes"] != "U7" for row in body)
    row = next(r for r in body if r["refdes"] == "C156")
    assert row["kind"] == "passive_c"
    assert row["role"] == "decoupling"
    assert row["confidence"] == 0.7  # stubbed
    assert row["source"] == "heuristic"


def test_get_schematic_passives_404_when_missing(memory_root, client):
    res = client.get("/pipeline/packs/ghost/schematic/passives")
    assert res.status_code == 404
