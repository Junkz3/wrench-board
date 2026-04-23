# SPDX-License-Identifier: Apache-2.0
"""Catalogue integrity: ids unique, requires resolve, thresholds monotonic."""

from api.profile.catalog import (
    MASTERY_THRESHOLD,
    PRACTICED_THRESHOLD,
    LEARNING_THRESHOLD,
    MASTERED_LEVEL_EXPERT,
    MASTERED_LEVEL_CONFIRMED,
    MASTERED_LEVEL_INTERMEDIATE,
    SKILLS_CATALOG,
    SkillId,
    TOOLS_CATALOG,
    ToolId,
)


def test_tool_ids_unique_and_match_enum():
    ids = [t.id for t in TOOLS_CATALOG]
    assert len(ids) == len(set(ids)), "duplicate tool id"
    assert set(ids) == {m.value for m in ToolId}


def test_skill_ids_unique_and_match_enum():
    ids = [s.id for s in SKILLS_CATALOG]
    assert len(ids) == len(set(ids)), "duplicate skill id"
    assert set(ids) == {m.value for m in SkillId}


def test_every_skill_requires_resolves_to_known_tool():
    tool_ids = {t.id for t in TOOLS_CATALOG}
    for skill in SKILLS_CATALOG:
        for req in skill.requires:
            assert req in tool_ids, f"skill {skill.id} requires unknown tool {req}"


def test_skill_status_thresholds_monotonic():
    assert 0 < LEARNING_THRESHOLD < PRACTICED_THRESHOLD < MASTERY_THRESHOLD


def test_level_thresholds_monotonic():
    assert (
        MASTERED_LEVEL_INTERMEDIATE
        < MASTERED_LEVEL_CONFIRMED
        < MASTERED_LEVEL_EXPERT
    )
