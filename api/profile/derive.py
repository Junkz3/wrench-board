# SPDX-License-Identifier: Apache-2.0
"""Pure derivation helpers — no I/O, safe to call from prompt rendering / HTTP."""

from __future__ import annotations

from typing import Literal

from api.profile.catalog import (
    LEARNING_THRESHOLD,
    MASTERED_LEVEL_CONFIRMED,
    MASTERED_LEVEL_EXPERT,
    MASTERED_LEVEL_INTERMEDIATE,
    MASTERY_THRESHOLD,
    PRACTICED_THRESHOLD,
    SKILLS_CATALOG,
)
from api.profile.model import TechnicianProfile

SkillStatus = Literal["unlearned", "learning", "practiced", "mastered"]


def skill_status(usages: int) -> SkillStatus:
    if usages >= MASTERY_THRESHOLD:
        return "mastered"
    if usages >= PRACTICED_THRESHOLD:
        return "practiced"
    if usages >= LEARNING_THRESHOLD:
        return "learning"
    return "unlearned"


def global_level(profile: TechnicianProfile) -> str:
    if profile.identity.level_override is not None:
        return profile.identity.level_override
    mastered_count = sum(
        1 for rec in profile.skills.values() if skill_status(rec.usages) == "mastered"
    )
    if mastered_count >= MASTERED_LEVEL_EXPERT:
        return "expert"
    if mastered_count >= MASTERED_LEVEL_CONFIRMED:
        return "confirmed"
    if mastered_count >= MASTERED_LEVEL_INTERMEDIATE:
        return "intermediate"
    return "beginner"


_LEVEL_TO_VERBOSITY = {
    "beginner": "teaching",
    "intermediate": "teaching",
    "confirmed": "normal",
    "expert": "concise",
}


def effective_verbosity(profile: TechnicianProfile) -> str:
    declared = profile.preferences.verbosity
    if declared != "auto":
        return declared
    return _LEVEL_TO_VERBOSITY[global_level(profile)]


def skills_by_status(profile: TechnicianProfile) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {
        "mastered": [],
        "practiced": [],
        "learning": [],
        "unlearned": [],
    }
    for entry in SKILLS_CATALOG:
        rec = profile.skills.get(entry.id)
        usages = rec.usages if rec is not None else 0
        buckets[skill_status(usages)].append(entry.id)
    return buckets
