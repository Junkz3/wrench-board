"""Agent-facing tool handlers for the technician profile.

Three tools:
  - profile_get: full read (identity, level, verbosity, tool bitmap, skills
    grouped by status).
  - profile_check_skills(candidate_skills): per-skill status + tool
    availability.
  - profile_track_skill(skill_id, evidence): bump usages, append evidence.
    Guards: skill must be in catalogue, evidence.action_summary must be
    >= EVIDENCE_MIN_CHARS.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from api.profile.catalog import SKILLS_CATALOG, SkillId, ToolId
from api.profile.derive import effective_verbosity, global_level, skill_status
from api.profile.model import SkillEvidence
from api.profile.store import bump_skill, load_profile

if TYPE_CHECKING:
    from api.session.state import SessionState

EVIDENCE_MIN_CHARS = 20

_SKILL_LOOKUP = {entry.id: entry for entry in SKILLS_CATALOG}


def _skills_summary(profile) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {
        "mastered": [], "practiced": [], "learning": []
    }
    for entry in SKILLS_CATALOG:
        rec = profile.skills.get(entry.id)
        if rec is None or rec.usages == 0:
            continue
        status = skill_status(rec.usages)
        if status == "unlearned":
            continue
        out[status].append({"id": entry.id, "usages": rec.usages})
    return out


def profile_get(session: SessionState | None = None) -> dict[str, Any]:
    from api.profile.store import profile_path
    path = profile_path()
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        mtime = 0.0

    if session is not None and session.profile_cache is not None:
        cached_mtime, cached_data = session.profile_cache
        if cached_mtime >= mtime:
            return cached_data

    profile = load_profile()
    data = {
        "identity": {
            "name": profile.identity.name,
            "avatar": profile.identity.avatar,
            "years_experience": profile.identity.years_experience,
            "specialties": profile.identity.specialties,
        },
        "level": global_level(profile),
        "verbosity_effective": effective_verbosity(profile),
        "tools_available": [
            t.value for t in ToolId if getattr(profile.tools, t.value)
        ],
        "tools_missing": [
            t.value for t in ToolId if not getattr(profile.tools, t.value)
        ],
        "skills_summary": _skills_summary(profile),
    }
    if session is not None:
        session.profile_cache = (mtime, data)
    return data


def profile_check_skills(candidate_skills: list[str]) -> dict[str, Any]:
    profile = load_profile()
    out: dict[str, Any] = {}
    for sid in candidate_skills:
        entry = _SKILL_LOOKUP.get(sid)
        if entry is None:
            out[sid] = {"error": "not_in_catalog"}
            continue
        rec = profile.skills.get(entry.id)
        usages = rec.usages if rec is not None else 0
        missing = [
            req for req in entry.requires
            if not getattr(profile.tools, req)
        ]
        out[sid] = {
            "status": skill_status(usages),
            "usages": usages,
            "tools_ok": len(missing) == 0,
            "missing_tools": missing,
        }
    return out


def _closest_skill_matches(skill_id: str, limit: int = 3) -> list[str]:
    # Simple prefix / substring heuristic.
    needle = skill_id.lower()
    scored = []
    for entry in SKILLS_CATALOG:
        cand = entry.id
        if cand.startswith(needle[:3]) or needle[:3] in cand:
            scored.append(cand)
    return scored[:limit]


def profile_track_skill(skill_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
    entry = _SKILL_LOOKUP.get(skill_id)
    if entry is None:
        return {
            "error": "unknown_skill",
            "closest_matches": _closest_skill_matches(skill_id),
        }

    action_summary = (evidence or {}).get("action_summary", "")
    if len(action_summary.strip()) < EVIDENCE_MIN_CHARS:
        return {
            "error": "evidence_too_thin",
            "min_chars": EVIDENCE_MIN_CHARS,
            "got_chars": len(action_summary.strip()),
        }

    try:
        ev = SkillEvidence.model_validate(evidence)
    except ValidationError as exc:
        # Narrow the catch to ValidationError so a real bug in
        # `bump_skill` / `load_profile` (AttributeError, RuntimeError) is
        # NOT silently coerced into the documented `invalid_evidence`
        # error channel — callers would mis-attribute the failure to
        # bad input when it's actually an internal regression.
        return {"error": "invalid_evidence", "detail": str(exc)}

    profile = load_profile()
    prev = profile.skills.get(SkillId(skill_id))
    usages_before = prev.usages if prev is not None else 0
    status_before = skill_status(usages_before)

    rec = bump_skill(SkillId(skill_id), ev)
    status_after = skill_status(rec.usages)

    return {
        "skill_id": skill_id,
        "usages_before": usages_before,
        "usages_after": rec.usages,
        "status_before": status_before,
        "status_after": status_after,
        "promoted": status_before != status_after,
    }
