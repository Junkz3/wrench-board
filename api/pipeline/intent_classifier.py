"""Haiku-driven intent classifier for the landing hero.

Takes a free-text user input (e.g. "MNT Reform — pas de boot") and
returns up to 3 candidate device slugs ranked by confidence. Used by the
landing page to funnel a non-expert user into the right repair workspace
without asking them to pick from a dropdown.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class IntentCandidate(BaseModel):
    slug: str = Field(min_length=1, description="Canonical device slug (e.g. 'mnt-reform-motherboard').")
    label: str = Field(min_length=1, description="Human-readable device label (French).")
    confidence: float = Field(ge=0.0, le=1.0, description="Classifier confidence 0..1.")
    pack_exists: bool = Field(description="True if memory/{slug}/ exists on disk with a knowledge pack.")


class IntentClassification(BaseModel):
    symptoms: str = Field(default="", description="Normalised symptom description extracted from user input.")
    candidates: list[IntentCandidate] = Field(default_factory=list, max_length=3)
