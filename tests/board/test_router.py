# SPDX-License-Identifier: Apache-2.0
"""Integration tests for POST /api/board/parse."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app

FIXTURE_DIR = Path(__file__).parent / "fixtures"
ASSETS_DIR = Path(__file__).parent.parent.parent / "board_assets"

client = TestClient(app)


def test_parse_returns_board_json_for_minimal_fixture():
    with (FIXTURE_DIR / "minimal.brd").open("rb") as fh:
        r = client.post("/api/board/parse", files={"file": ("minimal.brd", fh, "application/octet-stream")})
    assert r.status_code == 200
    body = r.json()
    assert body["source_format"] == "brd"
    assert body["board_id"] == "minimal"
    assert len(body["parts"]) == 2
    assert {p["refdes"] for p in body["parts"]} == {"R1", "C1"}


def test_parse_accepts_mnt_reform_brd2_fixture():
    if not (ASSETS_DIR / "mnt-reform-motherboard.brd").exists():
        pytest.skip("MNT Reform fixture not present")
    with (ASSETS_DIR / "mnt-reform-motherboard.brd").open("rb") as fh:
        r = client.post("/api/board/parse", files={"file": ("mnt-reform-motherboard.brd", fh, "application/octet-stream")})
    assert r.status_code == 200
    body = r.json()
    assert body["source_format"] == "brd2"
    assert len(body["parts"]) > 100
    assert len(body["pins"]) > 1000


def test_parse_rejects_empty_upload():
    r = client.post("/api/board/parse", files={"file": ("empty.brd", b"", "application/octet-stream")})
    assert r.status_code == 400
    assert r.json()["detail"]["detail"] == "empty-file"


def test_parse_rejects_oversized_upload(monkeypatch: pytest.MonkeyPatch):
    """An upload exceeding board_upload_max_bytes is refused with 413 before parsing."""
    from api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "board_upload_max_bytes", 1024, raising=True)
    big_payload = b"x" * 2048  # 2 KB, over the 1 KB test cap
    r = client.post(
        "/api/board/parse",
        files={"file": ("huge.brd", big_payload, "application/octet-stream")},
    )
    assert r.status_code == 413
    body = r.json()["detail"]
    assert body["detail"] == "file-too-large"
    assert body["max_bytes"] == 1024


def test_parse_fz_without_key_returns_422_with_clear_hint(monkeypatch):
    """Uploading a .fz with no MICROSOLDER_FZ_KEY configured should yield
    a 422 with a distinct `fz-key-missing` detail so the frontend can
    prompt the technician for the key rather than failing opaquely."""
    monkeypatch.delenv("MICROSOLDER_FZ_KEY", raising=False)
    r = client.post(
        "/api/board/parse",
        files={"file": ("something.fz", b"any content", "application/octet-stream")},
    )
    assert r.status_code == 422
    body = r.json()["detail"]
    assert body["detail"] == "fz-key-missing"
    assert "MICROSOLDER_FZ_KEY" in body["message"]


def test_parse_rejects_unknown_extension():
    r = client.post("/api/board/parse", files={"file": ("weird.xyz", b"garbage", "application/octet-stream")})
    assert r.status_code == 415
    assert r.json()["detail"]["detail"] == "unsupported-format"


def test_parse_rejects_malformed_brd():
    r = client.post("/api/board/parse", files={"file": ("bad.brd", b"not a brd file at all\n", "application/octet-stream")})
    assert r.status_code in (415, 422)
    assert "detail" in r.json()["detail"]


def test_parse_accepts_mnt_reform_kicad_pcb_fixture():
    fixture = ASSETS_DIR / "mnt-reform-motherboard.kicad_pcb"
    if not fixture.exists():
        pytest.skip("MNT Reform .kicad_pcb fixture not present")
    with fixture.open("rb") as fh:
        r = client.post(
            "/api/board/parse",
            files={"file": ("mnt-reform-motherboard.kicad_pcb", fh, "application/octet-stream")},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["source_format"] == "kicad_pcb"
    # Rich metadata should be populated
    parts_with_value = [p for p in body["parts"] if p.get("value")]
    assert len(parts_with_value) > 100
    parts_with_footprint = [p for p in body["parts"] if p.get("footprint")]
    assert len(parts_with_footprint) == len(body["parts"])
