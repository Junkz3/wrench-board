"""render_technician_block — default + rich profile snapshots."""

from api.profile.catalog import SkillId
from api.profile.model import SkillRecord, TechnicianProfile
from api.profile.prompt import render_technician_block


def test_default_profile_produces_minimal_block():
    p = TechnicianProfile.default()
    block = render_technician_block(p)
    assert "<technician_profile>" in block
    assert "</technician_profile>" in block
    assert "Level: beginner" in block
    assert "Target verbosity: teaching" in block
    assert "no tool declared" in block


def test_rich_profile_lists_tools_and_mastered_skills():
    p = TechnicianProfile.default()
    p.identity.name = "Test Tech"
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
    assert "Test Tech" in block
    assert "5 years" in block
    assert "apple, consoles" in block
    assert "soldering_iron" in block
    assert "hot_air" in block
    # Unavailable list should include oscilloscope (unchecked)
    assert "oscilloscope" in block
    assert "reflow_bga (12)" in block
    assert "jumper_wire (18)" in block
    assert "microsolder_0201 (1)" in block


def test_block_always_contains_rules_preamble():
    p = TechnicianProfile.default()
    block = render_technician_block(p)
    assert "profile_track_skill" in block


def test_default_profile_declares_english_reply_language():
    p = TechnicianProfile.default()
    block = render_technician_block(p)
    assert "Reply language: English" in block


def test_french_pref_declares_french_reply_language():
    p = TechnicianProfile.default()
    p.preferences.language = "fr"
    block = render_technician_block(p)
    assert "Reply language: French" in block
    assert "Reply language: English" not in block


def test_chinese_pref_declares_chinese_reply_language():
    p = TechnicianProfile.default()
    p.preferences.language = "zh"
    block = render_technician_block(p)
    assert "Reply language: Chinese (Simplified)" in block
    assert "Reply language: English" not in block


def test_hindi_pref_declares_hindi_reply_language():
    p = TechnicianProfile.default()
    p.preferences.language = "hi"
    block = render_technician_block(p)
    assert "Reply language: Hindi" in block
    assert "Reply language: English" not in block


def test_system_prompt_includes_technician_block():
    from api.agent.manifest import render_system_prompt
    from api.session.state import SessionState

    prompt = render_system_prompt(SessionState(), device_slug="demo-pi")
    assert "<technician_profile>" in prompt
    assert "profile_check_skills" in prompt
    assert "profile_track_skill" in prompt


def test_custom_tools_appear_in_block():
    p = TechnicianProfile.default()
    p.custom_tools = ["Hot tweezers", "Glue gun"]
    block = render_technician_block(p)
    assert "Hot tweezers" in block
    assert "Glue gun" in block
