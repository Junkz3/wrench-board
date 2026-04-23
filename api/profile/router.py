# SPDX-License-Identifier: Apache-2.0
"""HTTP surface for the technician profile."""

from __future__ import annotations

from fastapi import APIRouter

from api.profile.catalog import SKILLS_CATALOG, TOOLS_CATALOG
from api.profile.derive import effective_verbosity, global_level, skills_by_status
from api.profile.model import Identity, Preferences, ToolInventory
from api.profile.store import load_profile, save_profile

router = APIRouter(prefix="/profile", tags=["profile"])


def _envelope() -> dict:
    profile = load_profile()
    return {
        "profile": profile.model_dump(mode="json"),
        "derived": {
            "level": global_level(profile),
            "verbosity_effective": effective_verbosity(profile),
            "skills_by_status": skills_by_status(profile),
        },
        "catalog": {
            "tools": [
                {"id": t.id, "label": t.label, "group": t.group}
                for t in TOOLS_CATALOG
            ],
            "skills": [
                {"id": s.id, "label": s.label, "requires": list(s.requires)}
                for s in SKILLS_CATALOG
            ],
        },
    }


@router.get("")
def get_profile() -> dict:
    return _envelope()


@router.put("/identity")
def put_identity(identity: Identity) -> dict:
    profile = load_profile()
    profile.identity = identity
    save_profile(profile)
    return _envelope()


@router.put("/tools")
def put_tools(tools: ToolInventory) -> dict:
    profile = load_profile()
    profile.tools = tools
    save_profile(profile)
    return _envelope()


@router.put("/preferences")
def put_preferences(prefs: Preferences) -> dict:
    profile = load_profile()
    profile.preferences = prefs
    save_profile(profile)
    return _envelope()
