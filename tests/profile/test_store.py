# SPDX-License-Identifier: Apache-2.0
"""Profile store — disk I/O, atomicity, bump_skill."""

from pathlib import Path

import pytest

from api.profile.catalog import SKILL_EVIDENCES_CAP, SkillId
from api.profile.model import SkillEvidence, TechnicianProfile
from api.profile.store import bump_skill, load_profile, save_profile


@pytest.fixture
def tmp_memory_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    import api.config as _cfg
    _cfg._settings = None
    yield tmp_path
    _cfg._settings = None


def test_load_absent_file_returns_default(tmp_memory_root: Path):
    p = load_profile()
    default = TechnicianProfile.default()
    # updated_at is set at construction time and will differ by a few microseconds;
    # compare all fields except that timestamp.
    assert p.schema_version == default.schema_version
    assert p.identity == default.identity
    assert p.preferences == default.preferences
    assert p.tools == default.tools
    assert p.skills == default.skills


def test_save_then_load_roundtrips(tmp_memory_root: Path):
    p = TechnicianProfile.default()
    p.identity.name = "Alexis"
    p.tools.soldering_iron = True
    save_profile(p)
    restored = load_profile()
    assert restored.identity.name == "Alexis"
    assert restored.tools.soldering_iron is True
    assert (tmp_memory_root / "_profile" / "technician.json").exists()


def test_save_is_atomic_no_tmp_residue(tmp_memory_root: Path):
    save_profile(TechnicianProfile.default())
    listing = {f.name for f in (tmp_memory_root / "_profile").iterdir()}
    assert listing == {"technician.json"}


def _evidence(i: int) -> SkillEvidence:
    return SkillEvidence(
        repair_id=f"rep_{i}",
        device_slug="iphone-x",
        symptom="no_boot",
        action_summary=f"Reflow #{i} du PMIC U2 après short VDD_MAIN 1V8",
        date=f"2026-04-{(i % 28) + 1:02d}T10:00:00Z",
    )


def test_bump_skill_creates_record_first_time(tmp_memory_root: Path):
    result = bump_skill(SkillId.REFLOW_BGA, _evidence(1))
    assert result.usages == 1
    assert result.first_used is not None
    assert result.first_used == result.last_used
    p = load_profile()
    assert SkillId.REFLOW_BGA in p.skills


def test_bump_skill_increments_and_appends(tmp_memory_root: Path):
    bump_skill(SkillId.REFLOW_BGA, _evidence(1))
    result = bump_skill(SkillId.REFLOW_BGA, _evidence(2))
    assert result.usages == 2
    assert len(result.evidences) == 2


def test_bump_skill_caps_evidences_fifo(tmp_memory_root: Path):
    for i in range(SKILL_EVIDENCES_CAP + 5):
        bump_skill(SkillId.REFLOW_BGA, _evidence(i))
    p = load_profile()
    rec = p.skills[SkillId.REFLOW_BGA]
    assert rec.usages == SKILL_EVIDENCES_CAP + 5
    assert len(rec.evidences) == SKILL_EVIDENCES_CAP
    # FIFO: oldest were dropped — the first evidence we should still see
    # is for i == 5 (we dropped i == 0..4).
    assert rec.evidences[0].repair_id == "rep_5"
