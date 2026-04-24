# SPDX-License-Identifier: Apache-2.0
"""profile_get / profile_check_skills / profile_track_skill handlers."""

from pathlib import Path

import pytest

from api.profile.catalog import SkillId
from api.profile.model import SkillRecord, TechnicianProfile
from api.profile.store import save_profile
from api.profile.tools import (
    EVIDENCE_MIN_CHARS,
    profile_check_skills,
    profile_get,
    profile_track_skill,
)


@pytest.fixture
def memroot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    import api.config as _cfg
    _cfg._settings = None
    yield tmp_path
    _cfg._settings = None


def test_profile_get_default_shape(memroot: Path):
    out = profile_get()
    assert set(out["identity"].keys()) >= {"name", "years_experience", "specialties"}
    assert out["level"] == "beginner"
    assert out["verbosity_effective"] == "teaching"
    assert "soldering_iron" in out["tools_missing"]
    assert out["skills_summary"]["mastered"] == []


def test_profile_check_skills_reports_tools_ok_flag(memroot: Path):
    p = TechnicianProfile.default()
    p.tools.multimeter = True
    p.skills[SkillId.SHORT_ISOLATION] = SkillRecord(usages=4)
    save_profile(p)

    out = profile_check_skills(["short_isolation", "reballing"])
    assert out["short_isolation"]["status"] == "practiced"
    assert out["short_isolation"]["tools_ok"] is True
    assert out["reballing"]["tools_ok"] is False
    assert set(out["reballing"]["missing_tools"]) == {"bga_rework", "reballing_kit"}


def test_profile_check_skills_rejects_unknown(memroot: Path):
    out = profile_check_skills(["short_isolation", "nonsense_skill"])
    assert out["nonsense_skill"] == {"error": "not_in_catalog"}
    assert "status" in out["short_isolation"]


def test_profile_track_skill_rejects_thin_evidence(memroot: Path):
    out = profile_track_skill(
        "reflow_bga",
        {"repair_id": "r1", "device_slug": "ix", "symptom": "dead",
         "action_summary": "short", "date": "2026-04-22T10:00:00Z"},
    )
    assert out["error"] == "evidence_too_thin"
    assert out.get("min_chars") == EVIDENCE_MIN_CHARS


def test_profile_track_skill_rejects_unknown_id(memroot: Path):
    out = profile_track_skill(
        "not_a_skill",
        {"repair_id": "r1", "device_slug": "ix", "symptom": "dead",
         "action_summary": "an action summary that is long enough to pass the guard",
         "date": "2026-04-22T10:00:00Z"},
    )
    assert out["error"] == "unknown_skill"
    assert "closest_matches" in out


def test_profile_track_skill_happy_path_promotes(memroot: Path):
    p = TechnicianProfile.default()
    p.skills[SkillId.REFLOW_BGA] = SkillRecord(usages=9)
    save_profile(p)

    out = profile_track_skill(
        "reflow_bga",
        {"repair_id": "r1", "device_slug": "ix", "symptom": "no_boot",
         "action_summary": "Reflow du PMIC U2 après court-circuit VDD_MAIN",
         "date": "2026-04-22T10:00:00Z"},
    )
    assert out["usages_before"] == 9
    assert out["usages_after"] == 10
    assert out["status_before"] == "practiced"
    assert out["status_after"] == "mastered"
    assert out["promoted"] is True


def test_profile_get_caches_within_session(memroot: Path, monkeypatch):
    """Second profile_get on the same session must not re-read disk."""
    from api.session.state import SessionState
    from api.profile import tools as profile_tools

    calls: list[str] = []
    orig = profile_tools.load_profile
    def spy():
        calls.append("load")
        return orig()
    monkeypatch.setattr(profile_tools, "load_profile", spy)

    session = SessionState()
    profile_tools.profile_get(session=session)
    profile_tools.profile_get(session=session)

    assert len(calls) == 1, f"expected 1 load, got {len(calls)}"
