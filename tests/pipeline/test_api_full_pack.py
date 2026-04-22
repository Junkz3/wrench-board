"""Tests for GET /pipeline/packs/{device_slug}/full — Memory Bank backing endpoint."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import config as config_mod
from api.main import app

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "demo-pack"


@pytest.fixture
def memory_root(tmp_path, monkeypatch):
    """Redirect settings.memory_root to an isolated tmp dir per test."""
    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    yield tmp_path
    monkeypatch.setattr(config_mod, "_settings", None)


@pytest.fixture
def client():
    return TestClient(app)


def _copy_demo_pack(memory_root: Path, slug: str = "demo-pi") -> Path:
    dst = memory_root / slug
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("registry.json", "knowledge_graph.json", "rules.json", "dictionary.json"):
        shutil.copy(FIXTURE_ROOT / name, dst / name)
    return dst


def test_full_pack_returns_all_sections(memory_root, client):
    _copy_demo_pack(memory_root)

    res = client.get("/pipeline/packs/demo-pi/full")
    assert res.status_code == 200
    body = res.json()

    assert set(body.keys()) == {
        "device_slug",
        "device_label",
        "registry",
        "knowledge_graph",
        "rules",
        "dictionary",
        "audit_verdict",
    }
    assert body["device_slug"] == "demo-pi"
    assert body["device_label"] == "Demo Pi"
    assert body["registry"]["components"][0]["canonical_name"] == "U7"
    assert body["knowledge_graph"]["nodes"][0]["id"] == "cmp_U7"
    assert body["rules"]["rules"][0]["id"] == "rule-demo-001"
    assert body["dictionary"]["entries"][0]["canonical_name"] == "U7"
    # Fixture has no audit_verdict.json — must be null, never invented.
    assert body["audit_verdict"] is None


def test_full_pack_returns_null_for_missing_files(memory_root, client):
    """Hard rule #5: absent file => null, never fake data."""
    slug_dir = memory_root / "partial"
    slug_dir.mkdir()
    # Only registry is present.
    shutil.copy(FIXTURE_ROOT / "registry.json", slug_dir / "registry.json")

    res = client.get("/pipeline/packs/partial/full")
    assert res.status_code == 200
    body = res.json()

    assert body["device_slug"] == "partial"
    assert body["registry"] is not None
    assert body["knowledge_graph"] is None
    assert body["rules"] is None
    assert body["dictionary"] is None
    assert body["audit_verdict"] is None
    # device_label falls back to the slug when registry provides no label.
    # But registry IS present here with label "Demo Pi" (we copied demo fixture).
    assert body["device_label"] == "Demo Pi"


def test_full_pack_device_label_falls_back_to_slug_when_registry_absent(memory_root, client):
    slug_dir = memory_root / "bare-board"
    slug_dir.mkdir()
    # No files at all.
    res = client.get("/pipeline/packs/bare-board/full")
    assert res.status_code == 200
    body = res.json()
    assert body["device_slug"] == "bare-board"
    assert body["device_label"] == "bare-board"
    assert body["registry"] is None


def test_full_pack_includes_audit_verdict_when_present(memory_root, client):
    pack = _copy_demo_pack(memory_root, slug="audited")
    (pack / "audit_verdict.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "overall_status": "APPROVED",
                "consistency_score": 0.93,
                "files_to_rewrite": [],
                "drift_report": [],
                "revision_brief": "",
            }
        )
    )

    res = client.get("/pipeline/packs/audited/full")
    assert res.status_code == 200
    body = res.json()
    assert body["audit_verdict"]["overall_status"] == "APPROVED"
    assert body["audit_verdict"]["consistency_score"] == 0.93


def test_full_pack_404_when_slug_does_not_exist(memory_root, client):
    res = client.get("/pipeline/packs/nope/full")
    assert res.status_code == 404


def test_full_pack_rejects_invalid_json_gracefully(memory_root, client):
    slug_dir = memory_root / "broken"
    slug_dir.mkdir()
    (slug_dir / "registry.json").write_text("{ not valid json")

    res = client.get("/pipeline/packs/broken/full")
    assert res.status_code == 422
