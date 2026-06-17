"""HTTP surface: GET /profile + 3 PUTs."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    import api.config as _cfg
    _cfg._settings = None  # reset settings cache (see T4 for pattern)
    from api.main import app
    with TestClient(app) as c:
        yield c
    _cfg._settings = None


def test_get_returns_envelope_with_profile_derived_catalog(client: TestClient):
    res = client.get("/profile")
    assert res.status_code == 200
    body = res.json()
    assert "profile" in body
    assert "derived" in body
    assert "catalog" in body
    # Catalog payload shape
    assert {e["id"] for e in body["catalog"]["tools"]}.issuperset({"soldering_iron"})
    assert {e["id"] for e in body["catalog"]["skills"]}.issuperset({"reflow_bga"})
    # Derived payload shape
    assert body["derived"]["level"] == "beginner"
    assert body["derived"]["verbosity_effective"] == "teaching"
    assert "mastered" in body["derived"]["skills_by_status"]


def test_put_identity_persists(client: TestClient):
    res = client.put(
        "/profile/identity",
        json={
            "name": "Test Tech",
            "avatar": "AC",
            "years_experience": 5,
            "specialties": ["apple"],
            "level_override": None,
        },
    )
    assert res.status_code == 200
    assert res.json()["profile"]["identity"]["name"] == "Test Tech"
    # Re-read independently
    assert client.get("/profile").json()["profile"]["identity"]["name"] == "Test Tech"


def test_put_tools_is_full_replace(client: TestClient):
    body = {tool: False for tool in (
        "soldering_iron", "hot_air", "microscope", "oscilloscope",
        "multimeter", "bga_rework", "preheater", "bench_psu",
        "thermal_camera", "reballing_kit", "uv_lamp", "stencil_printer"
    )}
    body["soldering_iron"] = True
    body["hot_air"] = True
    res = client.put("/profile/tools", json=body)
    assert res.status_code == 200
    tools = res.json()["profile"]["tools"]
    assert tools["soldering_iron"] is True
    assert tools["hot_air"] is True
    assert tools["microscope"] is False


def test_put_preferences_persists(client: TestClient):
    res = client.put(
        "/profile/preferences",
        json={"verbosity": "concise", "language": "en"},
    )
    assert res.status_code == 200
    prefs = res.json()["profile"]["preferences"]
    assert prefs["verbosity"] == "concise"
    assert prefs["language"] == "en"


def test_state_defaults_to_unseen(client: TestClient):
    state = client.get("/profile").json()["profile"]["state"]
    assert state == {"onboarding_seen": False, "first_diag_seen": False}


def test_put_state_patches_one_flag_without_clobbering_the_other(client: TestClient):
    # Flip first_diag_seen only…
    res = client.put("/profile/state", json={"first_diag_seen": True})
    assert res.status_code == 200
    state = res.json()["profile"]["state"]
    assert state == {"onboarding_seen": False, "first_diag_seen": True}
    # …then onboarding_seen only — the earlier flag must survive (patch, not replace).
    res = client.put("/profile/state", json={"onboarding_seen": True})
    assert res.status_code == 200
    assert res.json()["profile"]["state"] == {
        "onboarding_seen": True,
        "first_diag_seen": True,
    }
    # Independent re-read confirms persistence.
    assert client.get("/profile").json()["profile"]["state"] == {
        "onboarding_seen": True,
        "first_diag_seen": True,
    }


def test_put_state_rejects_unknown_keys(client: TestClient):
    res = client.put("/profile/state", json={"nope": True})
    assert res.status_code == 422


def test_put_identity_rejects_unknown_level_override(client: TestClient):
    res = client.put(
        "/profile/identity",
        json={
            "name": "X", "avatar": "", "years_experience": 0,
            "specialties": [], "level_override": "wizard",
        },
    )
    assert res.status_code == 422


def test_put_custom_tools_persists_and_sanitizes(client: TestClient):
    res = client.put(
        "/profile/custom-tools",
        json={"custom_tools": ["  Hot tweezers  ", "Hot tweezers", "", "Glue gun"]},
    )
    assert res.status_code == 200
    saved = res.json()["profile"]["custom_tools"]
    assert saved == ["Hot tweezers", "Glue gun"]  # trimmed, deduped, blanks dropped
    # GET round-trips it
    assert client.get("/profile").json()["profile"]["custom_tools"] == [
        "Hot tweezers", "Glue gun",
    ]


def test_put_custom_tools_rejects_unknown_keys(client: TestClient):
    res = client.put("/profile/custom-tools", json={"nope": ["x"]})
    assert res.status_code == 422
