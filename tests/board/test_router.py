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


def test_parse_returns_501_for_stub_parser_extensions():
    """A registered but not-yet-implemented format must yield 501, not 500."""
    r = client.post(
        "/api/board/parse",
        files={"file": ("something.fz", b"any content", "application/octet-stream")},
    )
    assert r.status_code == 501
    body = r.json()["detail"]
    assert body["detail"] == "parser-not-implemented"
    assert "PCB Repair Tool" in body["message"]


def test_parse_rejects_unknown_extension():
    r = client.post("/api/board/parse", files={"file": ("weird.xyz", b"garbage", "application/octet-stream")})
    assert r.status_code == 415
    assert r.json()["detail"]["detail"] == "unsupported-format"


def test_parse_rejects_malformed_brd():
    r = client.post("/api/board/parse", files={"file": ("bad.brd", b"not a brd file at all\n", "application/octet-stream")})
    assert r.status_code in (415, 422)
    assert "detail" in r.json()["detail"]
