# SPDX-License-Identifier: Apache-2.0
"""Pipeline phase narrator — turns each phase artifact into a 2-3 sentence narration.

The orchestrator emits `phase_finished` events as Scout / Registry / Writers / Auditor
complete. After each, this module reads the artifact written to `memory/{slug}/`,
asks Haiku for a friendly English summary, and returns the text. The caller publishes
that text as a `phase_narration` event on the same WS bus, where the landing UI renders
it as the user watches the pipeline build their device's knowledge in real time.

Failures here are non-fatal — narrations are nice-to-have, never blocking the pipeline.

SDK note (2026-04-26 audit): this module hand-rolls a forced `tool_choice` instead
of routing through `tool_call.call_with_forced_tool`. Migration was evaluated and
deferred — the helper would force a streaming `messages.stream` path (required for
its retry / deep-unwrap / telemetry features), but Haiku's 300-token narration is
trivially short and a raise after max_attempts would surface as a user-visible
narration failure, while today the broad except below silently degrades to "" as
designed. Telemetry contribution is negligible (~10 input tokens × 5 phases per
run). Re-evaluate if narration ever moves to a Pydantic-validated multi-field
schema or starts contributing meaningfully to per-phase token stats.
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
    "description": "Emit a 2-3 sentence narration of what the pipeline phase just produced.",
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "Two or three short sentences for the technician watching the screen. "
                    "Use first person ('I found…', 'I can now…'). Be concrete — "
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
        "Scout phase complete. You are reading the raw web-research dump on the device. "
        "Summarise in 2-3 sentences what you now know about this device: MCU/SoC, "
        "PMIC, main rails, known symptoms. End with 'I can now…'."
    ),
    "registry": (
        "Registry phase complete. You are reading the canonical vocabulary extracted "
        "from the dump. Cite the number of components and signals identified. End with "
        "'I can now build the knowledge graph and the diagnostic rules.'"
    ),
    "mapper": (
        "Mapper phase complete. The mapper linked registry components to the loaded "
        "schematic graph refdes. Cite how many mappings were produced."
    ),
    "writers": (
        "Writers phase complete. Three sub-agents produced in parallel: the knowledge "
        "graph, the diagnostic rules, and the glossary. Cite the number of graph "
        "nodes (the JSON contains 'nodes')."
    ),
    "audit": (
        "Auditor phase complete. This is the final step — the auditor validated the "
        "coherence. Cite the verdict (APPROVED / NEEDS_REVISION / REJECTED) and "
        "conclude with 'I am ready to diagnose.' only if APPROVED."
    ),
}


async def narrate_phase(
    phase: str,
    slug: str,
    *,
    client: AsyncAnthropic,
    memory_root: Path | None = None,
) -> str:
    """Generate a short narration of the artifact produced by `phase` for `slug`.

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
        f"Artifact (excerpt):\n```\n{excerpt}\n```"
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
