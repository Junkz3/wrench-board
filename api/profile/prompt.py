"""Render the <technician_profile> block injected into the agent prompt."""

from __future__ import annotations

from api.profile.catalog import SkillId, ToolId
from api.profile.derive import effective_verbosity, global_level, skills_by_status
from api.profile.model import TechnicianProfile


def _group_skill_ids_with_usages(
    profile: TechnicianProfile, ids: list[str]
) -> list[str]:
    out = []
    for sid in ids:
        rec = profile.skills.get(SkillId(sid))
        usages = rec.usages if rec is not None else 0
        out.append(f"{sid} ({usages})")
    return out


def render_technician_block(profile: TechnicianProfile) -> str:
    level = global_level(profile)
    verbosity = effective_verbosity(profile)
    buckets = skills_by_status(profile)

    tools_have = [t.value for t in ToolId if getattr(profile.tools, t.value)]
    tools_missing = [t.value for t in ToolId if not getattr(profile.tools, t.value)]

    mastered = _group_skill_ids_with_usages(profile, buckets["mastered"])
    practiced = _group_skill_ids_with_usages(profile, buckets["practiced"])
    learning = _group_skill_ids_with_usages(profile, buckets["learning"])

    name = profile.identity.name or "—"
    years = profile.identity.years_experience
    specs = ", ".join(profile.identity.specialties) or "—"

    tools_have_str = ", ".join(tools_have) if tools_have else "no tool declared"
    tools_missing_str = ", ".join(tools_missing) if tools_missing else "—"
    mastered_str = ", ".join(mastered) if mastered else "—"
    practiced_str = ", ".join(practiced) if practiced else "—"
    learning_str = ", ".join(learning) if learning else "—"

    return (
        "<technician_profile>\n"
        f"Name: {name} · {years} years XP · Level: {level}\n"
        f"Target verbosity: {verbosity} "
        "(adjust if the tech asks for more/less detail)\n"
        f"Specialties: {specs}\n"
        f"Tools available: {tools_have_str}\n"
        f"Tools NOT available: {tools_missing_str}\n"
        f"Skills mastered (≥10×): {mastered_str}\n"
        f"Skills practiced (3-9×): {practiced_str}\n"
        f"Skills learning (1-2×): {learning_str}\n"
        "Rules:\n"
        "  - NEVER propose an action that requires an unavailable tool "
        "— propose a workaround or ask.\n"
        "  - For mastered skills, get straight to the point "
        "(refdes, gesture, done). For learning or unlearned skills, "
        "detail the steps and the risks.\n"
        "  - When the tech confirms they performed an action, call "
        "profile_track_skill with clear evidence (refdes, symptom, "
        "gesture resolved).\n"
        "</technician_profile>"
    )
