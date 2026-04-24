"""Haiku-driven intent classifier for the landing hero.

Takes a free-text user input (e.g. "MNT Reform — pas de boot") and
returns up to 3 candidate device slugs ranked by confidence. Used by the
landing page to funnel a non-expert user into the right repair workspace
without asking them to pick from a dropdown.
"""
from __future__ import annotations

import json
from pathlib import Path

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field

from api.config import get_settings


class IntentCandidate(BaseModel):
    slug: str = Field(min_length=1, description="Canonical device slug (e.g. 'mnt-reform-motherboard').")
    label: str = Field(min_length=1, description="Human-readable device label (French).")
    confidence: float = Field(ge=0.0, le=1.0, description="Classifier confidence 0..1.")
    pack_exists: bool = Field(description="True if memory/{slug}/ exists on disk with a knowledge pack.")


class IntentClassification(BaseModel):
    symptoms: str = Field(default="", description="Normalised symptom description extracted from user input.")
    candidates: list[IntentCandidate] = Field(default_factory=list, max_length=3)


# ---------------------------------------------------------------------------
# Forced-tool classifier
# ---------------------------------------------------------------------------

_TOOL_NAME = "report_intent"

_TOOL_SCHEMA = {
    "name": _TOOL_NAME,
    "description": "Report the user's diagnostic intent: symptoms + 0..3 candidate devices ranked by confidence.",
    "input_schema": {
        "type": "object",
        "properties": {
            "symptoms": {
                "type": "string",
                "description": "One-sentence normalised description of what the user says is wrong (in French).",
            },
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string", "description": "Canonical device slug, lowercase, hyphenated."},
                        "label": {"type": "string", "description": "French human-readable label."},
                        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    },
                    "required": ["slug", "label", "confidence"],
                },
                "maxItems": 3,
            },
        },
        "required": ["symptoms", "candidates"],
    },
}


def _get_memory_root() -> Path:
    return Path(get_settings().memory_root)


def _list_known_packs() -> list[tuple[str, str]]:
    """Return [(slug, label)] for every directory under memory/ that has a registry.json with a device_label."""
    root = _get_memory_root()
    if not root.exists():
        return []
    out: list[tuple[str, str]] = []
    for pack_dir in sorted(root.iterdir(), key=lambda p: p.name):
        if not pack_dir.is_dir() or pack_dir.name.startswith("_"):
            continue
        registry = pack_dir / "registry.json"
        if not registry.exists():
            out.append((pack_dir.name, pack_dir.name))
            continue
        try:
            data = json.loads(registry.read_text(encoding="utf-8"))
            label = data.get("device_label") or pack_dir.name
        except (OSError, json.JSONDecodeError):
            label = pack_dir.name
        out.append((pack_dir.name, label))
    return out


def _build_system_prompt() -> str:
    packs = _list_known_packs()
    if packs:
        catalog = "\n".join(f"- `{slug}` — {label}" for slug, label in packs)
    else:
        catalog = "(no packs on disk yet)"
    return (
        "You are a strict intent classifier for a hardware repair workbench.\n"
        "Given a free-text user description (in French or English), decide which device they are talking about\n"
        "and extract a one-sentence symptom summary.\n\n"
        "Always call the `report_intent` tool. Return 0 to 3 candidates ranked by confidence.\n"
        "Prefer slugs from the catalog below when there is any plausible match.\n"
        "If the user input is vague or off-topic, return an empty `candidates` list rather than guessing.\n\n"
        "Catalog of known device slugs:\n"
        f"{catalog}\n"
    )


async def classify_intent(text: str, *, client: AsyncAnthropic) -> IntentClassification:
    """Run a Haiku one-shot forced-tool classifier.

    The caller is responsible for instantiating the AsyncAnthropic client (so tests
    can pass in a mock). Side effects: none on disk; only an Anthropic API call.
    """
    settings = get_settings()
    response = await client.messages.create(
        model=settings.anthropic_model_fast,
        max_tokens=512,
        system=[
            {"type": "text", "text": _build_system_prompt(), "cache_control": {"type": "ephemeral"}}
        ],
        messages=[{"role": "user", "content": text}],
        tools=[_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": _TOOL_NAME},
    )

    payload: dict | None = None
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == _TOOL_NAME:
            payload = block.input
            break
    if payload is None:
        return IntentClassification(symptoms="", candidates=[])

    raw_candidates = payload.get("candidates") or []
    raw_candidates = sorted(raw_candidates, key=lambda c: c.get("confidence", 0.0), reverse=True)[:3]

    known_slugs = {slug for slug, _ in _list_known_packs()}
    cleaned: list[IntentCandidate] = []
    for c in raw_candidates:
        slug = (c.get("slug") or "").strip()
        if not slug:
            continue
        cleaned.append(
            IntentCandidate(
                slug=slug,
                label=c.get("label") or slug,
                confidence=float(c.get("confidence") or 0.0),
                pack_exists=slug in known_slugs,
            )
        )

    return IntentClassification(symptoms=str(payload.get("symptoms") or ""), candidates=cleaned)
