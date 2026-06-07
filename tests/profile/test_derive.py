"""Pure derivation helpers: skill_status, global_level, effective_verbosity."""

import pytest

from api.profile.catalog import SkillId
from api.profile.derive import (
    effective_verbosity,
    global_level,
    skill_status,
    skills_by_status,
)
from api.profile.model import SkillRecord, TechnicianProfile


@pytest.mark.parametrize(
    "usages,expected",
    [
        (0, "unlearned"),
        (1, "learning"),
        (2, "learning"),
        (3, "practiced"),
        (9, "practiced"),
        (10, "mastered"),
        (99, "mastered"),
    ],
)
def test_skill_status_thresholds(usages, expected):
    assert skill_status(usages) == expected


def _profile_with_mastered(count: int) -> TechnicianProfile:
    p = TechnicianProfile.default()
    for i in range(count):
        p.skills[SkillId(list(SkillId)[i].value)] = SkillRecord(usages=10)
    return p


@pytest.mark.parametrize(
    "mastered_count,expected",
    [
        (0, "beginner"),
        (1, "intermediate"),
        (2, "intermediate"),
        (3, "confirmed"),
        (7, "confirmed"),
        (8, "expert"),
        (12, "expert"),
    ],
)
def test_global_level_without_override(mastered_count, expected):
    profile = _profile_with_mastered(mastered_count)
    assert global_level(profile) == expected


def test_global_level_respects_override():
    profile = _profile_with_mastered(0)
    profile.identity.level_override = "expert"
    assert global_level(profile) == "expert"


@pytest.mark.parametrize(
    "level,expected",
    [
        ("beginner", "teaching"),
        ("intermediate", "teaching"),
        ("confirmed", "normal"),
        ("expert", "concise"),
    ],
)
def test_effective_verbosity_auto_maps_from_level(level, expected):
    profile = TechnicianProfile.default()
    profile.preferences.verbosity = "auto"
    profile.identity.level_override = level
    assert effective_verbosity(profile) == expected


def test_effective_verbosity_explicit_wins_over_auto():
    profile = TechnicianProfile.default()
    profile.preferences.verbosity = "concise"
    profile.identity.level_override = "beginner"
    assert effective_verbosity(profile) == "concise"


def test_skills_by_status_buckets_all_catalog_entries():
    profile = TechnicianProfile.default()
    profile.skills[SkillId.REFLOW_BGA] = SkillRecord(usages=12)
    profile.skills[SkillId.REBALLING] = SkillRecord(usages=4)
    profile.skills[SkillId.JUMPER_WIRE] = SkillRecord(usages=1)
    buckets = skills_by_status(profile)
    assert "reflow_bga" in buckets["mastered"]
    assert "reballing" in buckets["practiced"]
    assert "jumper_wire" in buckets["learning"]
    # Any skill not present in profile.skills is unlearned.
    assert "pop_rework" in buckets["unlearned"]
    total = sum(len(v) for v in buckets.values())
    assert total == 15  # every catalogue skill accounted for
