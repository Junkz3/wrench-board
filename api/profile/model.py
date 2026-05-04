"""Pydantic v2 models for the technician profile.

Source of truth for both runtime validation and the JSON Schema surface
exposed to agent tools. Mirrors the on-disk shape described in
docs/superpowers/specs/2026-04-23-technician-profile-design.md §2.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from api.profile.catalog import SkillId

LevelValue = Literal["beginner", "intermediate", "confirmed", "expert"]
VerbosityValue = Literal["auto", "concise", "normal", "teaching"]
LanguageValue = Literal["fr", "en"]


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
    language: LanguageValue = "fr"


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
    tools: ToolInventory = Field(default_factory=ToolInventory)
    skills: dict[SkillId, SkillRecord] = Field(default_factory=dict)
    updated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )

    @classmethod
    def default(cls) -> TechnicianProfile:
        return cls()
