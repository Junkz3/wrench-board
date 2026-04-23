# SPDX-License-Identifier: Apache-2.0
"""render_technician_block — default + rich profile snapshots."""

from api.profile.catalog import SkillId
from api.profile.model import SkillRecord, TechnicianProfile
from api.profile.prompt import render_technician_block


def test_default_profile_produces_minimal_block():
    p = TechnicianProfile.default()
    block = render_technician_block(p)
    assert "<technician_profile>" in block
    assert "</technician_profile>" in block
    assert "Niveau : beginner" in block
    assert "Verbosité cible : teaching" in block
    assert "Aucun outil déclaré" in block


def test_rich_profile_lists_tools_and_mastered_skills():
    p = TechnicianProfile.default()
    p.identity.name = "Alexis"
    p.identity.years_experience = 5
    p.identity.specialties = ["apple", "consoles"]
    p.tools.soldering_iron = True
    p.tools.hot_air = True
    p.tools.microscope = True
    p.tools.multimeter = True
    p.skills[SkillId.REFLOW_BGA] = SkillRecord(usages=12)
    p.skills[SkillId.JUMPER_WIRE] = SkillRecord(usages=18)
    p.skills[SkillId.MICROSOLDER_0201] = SkillRecord(usages=1)

    block = render_technician_block(p)
    assert "Alexis" in block
    assert "5 ans" in block
    assert "apple, consoles" in block
    assert "soldering_iron" in block
    assert "hot_air" in block
    # Non-disponibles list should include oscilloscope (unchecked)
    assert "oscilloscope" in block
    assert "reflow_bga (12)" in block
    assert "jumper_wire (18)" in block
    assert "microsolder_0201 (1)" in block


def test_block_always_contains_rules_preamble():
    p = TechnicianProfile.default()
    block = render_technician_block(p)
    assert "profile_track_skill" in block
