"""TechnicianProfile model — round-trip, defaults, validation."""

import pytest
from pydantic import ValidationError

from api.profile.catalog import SkillId, ToolId
from api.profile.model import (
    Identity,
    Preferences,
    SkillEvidence,
    SkillRecord,
    TechnicianProfile,
)


def test_default_profile_valid_and_empty():
    p = TechnicianProfile.default()
    assert p.schema_version == 1
    assert p.identity.name == ""
    assert p.identity.level_override is None
    assert p.preferences.verbosity == "auto"
    assert p.preferences.language == "en"
    # Every tool key present, all False.
    for tool in ToolId:
        assert getattr(p.tools, tool.value) is False
    assert p.skills == {}


def test_roundtrip_serialization():
    p = TechnicianProfile.default()
    p.identity.name = "Test Tech"
    p.tools.soldering_iron = True
    p.skills[SkillId.REFLOW_BGA] = SkillRecord(
        usages=2,
        first_used="2026-04-01T10:00:00Z",
        last_used="2026-04-02T11:00:00Z",
        evidences=[
            SkillEvidence(
                repair_id="rep_1",
                device_slug="iphone-x",
                symptom="no_boot",
                action_summary="Reflow du PMIC U2 après court-circuit VDD_MAIN",
                date="2026-04-02T11:00:00Z",
            )
        ],
    )
    payload = p.model_dump(mode="json")
    restored = TechnicianProfile.model_validate(payload)
    assert restored == p


def test_identity_level_override_rejects_unknown_value():
    with pytest.raises(ValidationError):
        Identity(level_override="wizard")


def test_preferences_verbosity_rejects_unknown_value():
    with pytest.raises(ValidationError):
        Preferences(verbosity="verbose")


def test_skills_dict_rejects_unknown_key():
    with pytest.raises(ValidationError):
        TechnicianProfile.model_validate(
            {"skills": {"not_a_skill_id": {"usages": 1}}}
        )


def test_custom_tools_default_empty_and_sanitized():
    from api.profile.model import TechnicianProfile
    assert TechnicianProfile.default().custom_tools == []
    # collapse whitespace, trim, drop blanks, case-dedupe, preserve order
    p = TechnicianProfile.model_validate(
        {"custom_tools": ["  Hot   tweezers ", "hot tweezers", "", "  ", "Glue gun"]}
    )
    assert p.custom_tools == ["Hot tweezers", "Glue gun"]


def test_custom_tools_capped_in_count_and_length():
    from api.profile.model import MAX_CUSTOM_TOOLS, TechnicianProfile
    long_name = "x" * 80
    p = TechnicianProfile.model_validate(
        {"custom_tools": [f"tool {i}" for i in range(50)] + [long_name]}
    )
    assert len(p.custom_tools) == MAX_CUSTOM_TOOLS
    p2 = TechnicianProfile.model_validate({"custom_tools": [long_name]})
    assert len(p2.custom_tools[0]) <= 40


def test_legacy_profile_without_custom_tools_loads():
    # A pre-custom_tools JSON simply lacks the key — must validate to default [].
    from api.profile.model import TechnicianProfile
    p = TechnicianProfile.model_validate(
        {"schema_version": 1, "identity": {}, "preferences": {},
         "tools": {}, "skills": {}, "updated_at": "x"}
    )
    assert p.custom_tools == []
