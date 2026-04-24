# SPDX-License-Identifier: Apache-2.0
"""On-disk profile store.

Single file `memory/_profile/technician.json`. Writes are atomic via
tempfile + os.replace. Evidence history is FIFO-capped per skill.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from api.config import get_settings
from api.profile.catalog import SKILL_EVIDENCES_CAP, SkillId
from api.profile.model import SkillEvidence, SkillRecord, TechnicianProfile

_PROFILE_SUBDIR = "_profile"
_PROFILE_FILENAME = "technician.json"


def _profile_path() -> Path:
    root = Path(get_settings().memory_root)
    return root / _PROFILE_SUBDIR / _PROFILE_FILENAME


def profile_path() -> Path:
    """Public accessor for the profile file path (used by mtime-based caches)."""
    return _profile_path()


def load_profile() -> TechnicianProfile:
    path = _profile_path()
    if not path.exists():
        return TechnicianProfile.default()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return TechnicianProfile.model_validate(raw)
    except Exception:
        # Corrupt file → fall back to defaults rather than crashing the server.
        # The user can edit to recover; the corrupt file is left alone.
        return TechnicianProfile.default()


def save_profile(profile: TechnicianProfile) -> None:
    path = _profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    profile.updated_at = datetime.now(UTC).isoformat()
    payload = profile.model_dump(mode="json")
    # Atomic write: write to tmp in same dir, then os.replace.
    fd, tmp_path = tempfile.mkstemp(
        prefix=".technician.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def bump_skill(skill_id: SkillId, evidence: SkillEvidence) -> SkillRecord:
    profile = load_profile()
    rec = profile.skills.get(skill_id) or SkillRecord()
    rec.usages += 1
    rec.last_used = evidence.date
    if rec.first_used is None:
        rec.first_used = evidence.date
    rec.evidences.append(evidence)
    if len(rec.evidences) > SKILL_EVIDENCES_CAP:
        rec.evidences = rec.evidences[-SKILL_EVIDENCES_CAP:]
    profile.skills[skill_id] = rec
    save_profile(profile)
    return rec
