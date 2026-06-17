"""Pydantic v2 models for the technician profile.

Source of truth for both runtime validation and the JSON Schema surface
exposed to agent tools. Mirrors the on-disk shape described in
docs/superpowers/specs/2026-04-23-technician-profile-design.md §2.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.profile.catalog import SkillId

# Free-text tools the tech declares on top of the closed catalogue. Bounded so a
# pasted essay can't bloat the agent prompt: collapse whitespace, cap each name,
# drop blanks/case-dupes, cap the count.
MAX_CUSTOM_TOOLS = 20
MAX_CUSTOM_TOOL_LEN = 40


def clean_custom_tools(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        name = " ".join(str(raw).split())[:MAX_CUSTOM_TOOL_LEN].strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= MAX_CUSTOM_TOOLS:
            break
    return out

LevelValue = Literal["beginner", "intermediate", "confirmed", "expert"]
VerbosityValue = Literal["auto", "concise", "normal", "teaching"]
LanguageValue = Literal["fr", "en", "zh", "hi"]


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    avatar: str = ""  # 1 emoji or up to 2 letters
    years_experience: int = Field(default=0, ge=0, le=80)
    specialties: list[str] = Field(default_factory=list)
    level_override: LevelValue | None = None


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verbosity: VerbosityValue = "auto"
    language: LanguageValue = "en"


class State(BaseModel):
    """One-shot onboarding / lifecycle flags, persisted per technician (and so
    per tenant in cloud, via the X-Owner-Ref-scoped profile store).

    These replace the browser-local `wb_onboarding_seen` / `wb_first_diag_seen`
    flags as the source of truth: a guided tour completed on one device must not
    replay on another. The frontend keeps localStorage as a fast pre-gate cache.

    Defaulted bools so pre-state JSON files still load (the missing `state` key
    falls back to defaults; extra="forbid" only rejects UNKNOWN keys)."""

    model_config = ConfigDict(extra="forbid")

    onboarding_seen: bool = False  # landing cockpit guided tour
    first_diag_seen: bool = False  # first-diagnostic workspace coaching


class ToolInventory(BaseModel):
    """Bitmap of owned tools, one bool field per ToolId."""

    model_config = ConfigDict(extra="forbid")

    soldering_iron: bool = False
    hot_air: bool = False
    microscope: bool = False
    oscilloscope: bool = False
    multimeter: bool = False
    bga_rework: bool = False
    preheater: bool = False
    bench_psu: bool = False
    thermal_camera: bool = False
    reballing_kit: bool = False
    uv_lamp: bool = False
    stencil_printer: bool = False


class SkillEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repair_id: str
    device_slug: str
    symptom: str
    action_summary: str
    date: str  # ISO 8601


class SkillRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usages: int = Field(default=0, ge=0)
    first_used: str | None = None
    last_used: str | None = None
    evidences: list[SkillEvidence] = Field(default_factory=list)


class TechnicianProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    identity: Identity = Field(default_factory=Identity)
    preferences: Preferences = Field(default_factory=Preferences)
    state: State = Field(default_factory=State)
    tools: ToolInventory = Field(default_factory=ToolInventory)
    custom_tools: list[str] = Field(default_factory=list)
    skills: dict[SkillId, SkillRecord] = Field(default_factory=dict)
    updated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )

    # Defaulted + sanitized, so pre-custom_tools JSON files still load (the missing
    # key falls back to []); extra="forbid" only rejects UNKNOWN keys, not missing.
    @field_validator("custom_tools")
    @classmethod
    def _sanitize_custom_tools(cls, v: list[str]) -> list[str]:
        return clean_custom_tools(v)

    @classmethod
    def default(cls) -> TechnicianProfile:
        return cls()
