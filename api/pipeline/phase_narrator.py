# SPDX-License-Identifier: Apache-2.0
"""Pipeline phase narrator — turns each phase artifact into a 2-3 sentence French narration.

The orchestrator emits `phase_finished` events as Scout / Registry / Writers / Auditor
complete. After each, this module reads the artifact written to `memory/{slug}/`,
asks Haiku for a friendly French summary, and returns the text. The caller publishes
that text as a `phase_narration` event on the same WS bus, where the landing UI renders
it as the user watches the pipeline build their device's knowledge in real time.

Failures here are non-fatal — narrations are nice-to-have, never blocking the pipeline.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from anthropic import AsyncAnthropic

from api.config import get_settings

logger = logging.getLogger("wrench_board.pipeline.phase_narrator")

_TOOL_NAME = "narrate"
_MAX_NARRATION_CHARS = 600   # cap to prevent runaway output
_MAX_ARTIFACT_CHARS = 8_000  # cap input fed to Haiku — narration is summary, not full read

_TOOL_SCHEMA = {
    "name": _TOOL_NAME,
    "description": "Emit a 2-3 sentence French narration of what the pipeline phase just produced.",
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "Two or three short French sentences for the technician watching the screen. "
                    "Use first person ('J'ai trouvé…', 'Je peux maintenant…'). Be concrete — "
                    "cite specific component counts, signal names, or rule counts. No markdown."
                ),
            },
        },
        "required": ["text"],
    },
}

# Per-phase artifact path (relative to memory_root/{slug}/)
_ARTIFACT_PATHS: dict[str, str] = {
    "scout": "raw_research_dump.md",
    "registry": "registry.json",
    "mapper": "refdes_attributions.json",
    "writers": "knowledge_graph.json",  # writers ship 3 files; we summarise via the graph
    "audit": "audit_verdict.json",
}

# Per-phase prompt directive — gives Haiku context about WHAT it's looking at and what's next.
_PHASE_PROMPTS: dict[str, str] = {
    "scout": (
        "Phase Scout terminée. Tu lis le dump brut de recherche web sur l'appareil. "
        "Résume en 2-3 phrases ce que tu sais de cet appareil maintenant : MCU/SoC, "
        "PMIC, rails principaux, symptômes connus. Termine par 'Je peux maintenant…'."
    ),
    "registry": (
        "Phase Registry terminée. Tu lis le vocabulaire canonique extrait du dump. "
        "Cite le nombre de composants et de signaux identifiés. Termine par "
        "'Je peux maintenant construire le graphe et les règles de diagnostic.'"
    ),
    "mapper": (
        "Phase Mapper terminée. Le mapper a relié les composants du registry aux refdes "
        "du graphe schematique chargé. Cite combien de mappings ont été produits."
    ),
    "writers": (
        "Phase Writers terminée. Trois sous-agents ont produit en parallèle : "
        "le graphe de connaissances, les règles de diagnostic, et le glossaire. "
        "Cite le nombre de nœuds du graphe (le JSON contient 'nodes')."
    ),
    "audit": (
        "Phase Auditor terminée. C'est la dernière étape — l'auditeur a validé la cohérence. "
        "Cite le verdict (APPROVED / NEEDS_REVISION / REJECTED) et conclus par "
        "'Je suis prêt à diagnostiquer.' uniquement si APPROVED."
    ),
}


async def narrate_phase(
    phase: str,
    slug: str,
    *,
    client: AsyncAnthropic,
    memory_root: Path | None = None,
) -> str:
    """Generate a French narration of the artifact produced by `phase` for `slug`.

    Returns "" when:
      - `phase` is unknown.
      - The artifact file does not exist on disk yet.
      - The Haiku call fails for any reason (logged but never raised).

    Side effects: reads from disk, one Anthropic API call. No writes.
    """
    if phase not in _ARTIFACT_PATHS:
        logger.debug("narrate_phase: unknown phase %r — skipping", phase)
        return ""

    settings = get_settings()
    root = memory_root or Path(settings.memory_root)
    artifact_path = root / slug / _ARTIFACT_PATHS[phase]

    if not artifact_path.exists():
        logger.debug("narrate_phase: artifact missing at %s — skipping", artifact_path)
        return ""

    try:
        raw = artifact_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("narrate_phase: read failed for %s: %s", artifact_path, exc)
        return ""

    excerpt = _trim_artifact(raw, phase)

    user_prompt = (
        f"{_PHASE_PROMPTS[phase]}\n\n"
        f"Artefact (extrait):\n```\n{excerpt}\n```"
    )

    try:
        response = await client.messages.create(
            model=settings.anthropic_model_fast,
            max_tokens=300,
            messages=[{"role": "user", "content": user_prompt}],
            tools=[_TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": _TOOL_NAME},
        )
    except Exception as exc:
        logger.warning("narrate_phase: Haiku call failed for phase=%s slug=%s: %s", phase, slug, exc)
        return ""

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == _TOOL_NAME:
            text = str((block.input or {}).get("text") or "").strip()
            if text:
                return text[:_MAX_NARRATION_CHARS]
            break

    return ""


def _trim_artifact(raw: str, phase: str) -> str:
    """Cap the artifact size we send to Haiku.

    For JSON phases we try to extract structural counts and a slice; for markdown
    we keep the head + tail to avoid blowing the prompt budget. Both fall back to
    a hard char cap.
    """
    if len(raw) <= _MAX_ARTIFACT_CHARS:
        return raw

    if phase == "scout":
        # Markdown — head + tail
        head = raw[: _MAX_ARTIFACT_CHARS // 2]
        tail = raw[-_MAX_ARTIFACT_CHARS // 2 :]
        return f"{head}\n[…]\n{tail}"

    # JSON phases — try to keep top-level structure, drop deep arrays
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            summary = {}
            for k, v in obj.items():
                if isinstance(v, list):
                    summary[k] = f"<{len(v)} items>"
                elif isinstance(v, dict):
                    summary[k] = f"<dict with {len(v)} keys>"
                else:
                    summary[k] = v
            return json.dumps(summary, indent=2, ensure_ascii=False)[: _MAX_ARTIFACT_CHARS]
    except json.JSONDecodeError:
        pass

    return raw[: _MAX_ARTIFACT_CHARS]
