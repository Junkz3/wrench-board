"""HTTP surface for the technician profile."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header
from pydantic import BaseModel, ConfigDict, Field

from api.profile.catalog import SKILLS_CATALOG, TOOLS_CATALOG
from api.profile.derive import effective_verbosity, global_level, skills_by_status
from api.profile.model import Identity, Preferences, ToolInventory, clean_custom_tools
from api.profile.store import load_profile, save_profile


class CustomToolsUpdate(BaseModel):
    """Body for PUT /profile/custom-tools — free-text tools off-catalogue."""

    model_config = ConfigDict(extra="forbid")
    custom_tools: list[str] = Field(default_factory=list)


class StateUpdate(BaseModel):
    """Body for PUT /profile/state — partial patch of onboarding flags.

    Each field is optional: the client sends only the flag it is flipping, so
    two independent tours can mark themselves seen without clobbering each
    other (full-replace would reset the flag the caller didn't include)."""

    model_config = ConfigDict(extra="forbid")
    onboarding_seen: bool | None = None
    first_diag_seen: bool | None = None

router = APIRouter(prefix="/profile", tags=["profile"])

# The multi-tenant cloud front-door injects X-Owner-Ref (the tenant id) on every
# call; self-host / single-tenant leaves it unset (None → shared _profile root).
OwnerRef = Annotated[str | None, Header(alias="X-Owner-Ref")]


def _envelope(owner_ref: str | None = None) -> dict:
    profile = load_profile(owner_ref)
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
def get_profile(owner_ref: OwnerRef = None) -> dict:
    return _envelope(owner_ref)


@router.put("/identity")
def put_identity(identity: Identity, owner_ref: OwnerRef = None) -> dict:
    profile = load_profile(owner_ref)
    profile.identity = identity
    save_profile(profile, owner_ref)
    return _envelope(owner_ref)


@router.put("/tools")
def put_tools(tools: ToolInventory, owner_ref: OwnerRef = None) -> dict:
    profile = load_profile(owner_ref)
    profile.tools = tools
    save_profile(profile, owner_ref)
    return _envelope(owner_ref)


@router.put("/custom-tools")
def put_custom_tools(body: CustomToolsUpdate, owner_ref: OwnerRef = None) -> dict:
    profile = load_profile(owner_ref)
    profile.custom_tools = clean_custom_tools(body.custom_tools)
    save_profile(profile, owner_ref)
    return _envelope(owner_ref)


@router.put("/preferences")
def put_preferences(prefs: Preferences, owner_ref: OwnerRef = None) -> dict:
    profile = load_profile(owner_ref)
    profile.preferences = prefs
    save_profile(profile, owner_ref)
    return _envelope(owner_ref)


@router.put("/state")
def put_state(body: StateUpdate, owner_ref: OwnerRef = None) -> dict:
    profile = load_profile(owner_ref)
    if body.onboarding_seen is not None:
        profile.state.onboarding_seen = body.onboarding_seen
    if body.first_diag_seen is not None:
        profile.state.first_diag_seen = body.first_diag_seen
    save_profile(profile, owner_ref)
    return _envelope(owner_ref)
