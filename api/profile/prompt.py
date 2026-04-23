# SPDX-License-Identifier: Apache-2.0
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

    tools_have_str = ", ".join(tools_have) if tools_have else "Aucun outil déclaré"
    tools_missing_str = ", ".join(tools_missing) if tools_missing else "—"
    mastered_str = ", ".join(mastered) if mastered else "—"
    practiced_str = ", ".join(practiced) if practiced else "—"
    learning_str = ", ".join(learning) if learning else "—"

    return (
        "<technician_profile>\n"
        f"Nom : {name} · {years} ans d'XP · Niveau : {level}\n"
        f"Verbosité cible : {verbosity} "
        "(ajuste si le tech demande plus/moins de détail)\n"
        f"Spécialités : {specs}\n"
        f"Outils disponibles : {tools_have_str}\n"
        f"Outils NON disponibles : {tools_missing_str}\n"
        f"Compétences maîtrisées (≥10×) : {mastered_str}\n"
        f"Compétences pratiquées (3-9×) : {practiced_str}\n"
        f"Compétences en apprentissage (1-2×) : {learning_str}\n"
        "Règles :\n"
        "  - NE propose JAMAIS une action qui requiert un outil non dispo "
        "— propose un workaround ou demande.\n"
        "  - Pour les compétences mastered, va direct au fait "
        "(refdes, geste, fin). Pour learning ou unlearned, détaille les étapes "
        "et les risques.\n"
        "  - Quand le tech confirme avoir exécuté une action, appelle "
        "profile_track_skill avec une evidence claire (refdes, symptôme, "
        "geste résolu).\n"
        "</technician_profile>"
    )
