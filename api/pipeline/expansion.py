"""Targeted pack expansion — grow an existing device's memory bank in place.

When the diagnostic agent calls `mb_get_rules_for_symptoms` and comes back
empty-handed, it can call `mb_expand_knowledge(focus_symptoms, focus_refdes)`
to trigger THIS pipeline. We don't rebuild the whole pack; we:

1. Append a targeted Scout run to the existing `raw_research_dump.md`
   (cumulative — the dump grows with every expansion).
2. Re-run the Registry Builder on the enriched dump to absorb any new
   components/signals the Scout surfaced.
3. Re-run the Clinicien on the enriched dump + merged registry to produce
   an updated rule set focused on the new symptom area.
4. Persist — registry.json and rules.json get overwritten with the merged
   result. Any new canonical names and rule_ids are counted for telemetry.

Cost per expansion: ~$0.40 (Scout Sonnet + Registry Sonnet + Clinicien Opus)
vs ~$1.5 for a full rebuild. Cartographe/Lexicographe/Auditor are skipped —
the graph can drift slightly between expansions and the full audit waits
for the next rebuild. Acceptable trade-off for the "living memory bank" UX.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic

from api.config import get_settings
from api.pipeline.prompts import (
    CLINICIEN_TASK,
    SCOUT_RETRY_SUFFIX,
    SCOUT_SYSTEM,
    WRITER_SHARED_USER_PREFIX_TEMPLATE,
    WRITER_SYSTEM,
)
from api.pipeline.registry import run_registry_builder
from api.pipeline.schemas import Registry, RulesSet
from api.pipeline.tool_call import call_with_forced_tool
from api.pipeline.writers import SUBMIT_RULES_TOOL_NAME, _submit_rules_tool

logger = logging.getLogger("wrench_board.pipeline.expansion")

# Signature for the optional "chunk provider" injected by the MA runtime
# when it wants to spawn a KnowledgeCurator sub-agent instead of running
# the inline `_run_targeted_scout` ourselves. Returns the same Markdown
# chunk shape so the rest of the pipeline (Registry + Clinicien) is
# oblivious to who produced it.
ChunkProvider = Callable[..., Awaitable[str]]


TARGETED_SCOUT_TEMPLATE = """\
Research the following device with a FOCUSED scope on specific symptoms the
technician is hitting right now. Produce the same Markdown dump format defined
in your system prompt, BUT dedicate your searches strictly to the focus areas
below. The existing knowledge pack already covers other failure modes — your
job is to fill the gap for THESE symptoms.

Device: {device_label}

Focus symptoms (target these):
{focus_block}

{refdes_block}

Search plan for this expansion:
- 4–8 searches total, each scoped to one focus symptom + the device.
- Probe the specialized microsoldering families first (r/boardrepair, Rossmann,
  NorthridgeFix, iPadRehab, badcaps, EEVblog). These are where component-level
  audio / RF / charging threads live in detail.
- For each focus refdes if any, search the refdes + device together
  (e.g. "iPhone X U3101 no sound", "iPhone X Meson audio codec").
- Do NOT re-cover power-rail / boot / charge topics already in the pack —
  we already have those; new material only.

Stop when you have 3–6 concrete symptom bullets with traceable sources.
"""


def _format_focus_block(focus_symptoms: list[str]) -> str:
    return "\n".join(f"  - {s}" for s in focus_symptoms)


def _format_refdes_block(focus_refdes: list[str]) -> str:
    if not focus_refdes:
        return ""
    return (
        "Focus refdes (look up these specific component identifiers):\n"
        + "\n".join(f"  - {r}" for r in focus_refdes)
    )


async def _run_targeted_scout(
    *,
    client: AsyncAnthropic,
    model: str,
    device_label: str,
    focus_symptoms: list[str],
    focus_refdes: list[str],
) -> str:
    """Run a Scout turn focused on specific symptoms, return the Markdown chunk.

    Reuses SCOUT_SYSTEM so the output shape is identical to the main Scout —
    same headings, same bullet format — which means the downstream Registry
    + Clinicien parse it without adaptation.
    """
    user_prompt = TARGETED_SCOUT_TEMPLATE.format(
        device_label=device_label,
        focus_block=_format_focus_block(focus_symptoms),
        refdes_block=_format_refdes_block(focus_refdes),
    )
    messages: list[dict] = [{"role": "user", "content": user_prompt}]
    web_search_tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": 8}

    logger.info(
        "[Expand·Scout] targeting device=%r symptoms=%s refdes=%s",
        device_label, focus_symptoms, focus_refdes,
    )

    # Single pass; we don't expect long pause_turn chains on a narrow scope.
    attempt = 0
    response = None
    effort = "xhigh" if str(model).startswith("claude-opus-4-") else "high"
    while attempt < 2:
        response = await client.messages.create(
            model=model,
            max_tokens=8000,
            system=SCOUT_SYSTEM,
            messages=messages,
            tools=[web_search_tool],
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": effort},
        )
        if response.stop_reason == "pause_turn":
            messages = [
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": response.content},
            ]
            attempt += 1
            continue
        break

    if response is None:
        raise RuntimeError("targeted scout did not run")

    text_parts = [block.text for block in response.content if block.type == "text"]
    chunk = "\n\n".join(t for t in text_parts if t.strip())
    if not chunk:
        # Try one retry with the broadening suffix before giving up.
        logger.warning(
            "[Expand·Scout] empty first pass for focus=%s · retrying broader",
            focus_symptoms,
        )
        response = await client.messages.create(
            model=model,
            max_tokens=8000,
            system=SCOUT_SYSTEM,
            messages=[{"role": "user", "content": user_prompt + SCOUT_RETRY_SUFFIX}],
            tools=[web_search_tool],
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": effort},
        )
        text_parts = [block.text for block in response.content if block.type == "text"]
        chunk = "\n\n".join(t for t in text_parts if t.strip())
        if not chunk:
            raise RuntimeError("targeted scout produced no output after retry")

    logger.info("[Expand·Scout] produced chunk length=%d chars", len(chunk))
    return chunk


def _append_scout_chunk(pack_dir: Path, chunk: str, focus_symptoms: list[str]) -> None:
    """Append the new chunk to raw_research_dump.md with a separator header.

    The cumulative dump is the durable raw memory — every expansion leaves a
    traceable footprint. Registry + rules are re-derived on the FULL cumulative
    dump on the next Clinicien call.
    """
    path = pack_dir / "raw_research_dump.md"
    header = (
        "\n\n---\n"
        f"## Expansion {time.strftime('%Y-%m-%dT%H:%M:%S')} — focus: "
        f"{', '.join(focus_symptoms)}\n\n"
    )
    with path.open("a", encoding="utf-8") as f:
        f.write(header)
        f.write(chunk)
        f.write("\n")


async def _run_clinicien_on_full_dump(
    *,
    client: AsyncAnthropic,
    model: str,
    device_label: str,
    raw_dump: str,
    registry: Registry,
) -> RulesSet:
    """Re-run the Clinicien on the FULL (cumulative) dump + merged registry.

    The output REPLACES the existing rules.json — the new ruleset covers
    both previously-known failure modes and the expansion focus area. Ids
    may be renumbered between runs; rule content stays stable because the
    same prompt + same sources are used.
    """
    shared_prefix = WRITER_SHARED_USER_PREFIX_TEMPLATE.format(
        device_label=device_label,
        raw_dump=raw_dump,
        registry_json=registry.model_dump_json(indent=2),
    )
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": shared_prefix,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": CLINICIEN_TASK},
            ],
        }
    ]
    return await call_with_forced_tool(
        client=client,
        model=model,
        system=WRITER_SYSTEM,
        messages=messages,
        tools=[_submit_rules_tool()],
        forced_tool_name=SUBMIT_RULES_TOOL_NAME,
        output_schema=RulesSet,
        max_attempts=2,
        log_label="Clinicien-Expand",
    )


async def expand_pack(
    *,
    device_slug: str,
    focus_symptoms: list[str],
    focus_refdes: list[str] | None = None,
    client: AsyncAnthropic | None = None,
    memory_root: Path | None = None,
    chunk_provider: ChunkProvider | None = None,
) -> dict[str, Any]:
    """Grow the on-disk pack for `device_slug` around a focus symptom area.

    Returns a summary dict:
        {
          "expanded": True,
          "focus_symptoms": [...],
          "new_rules_count": int,      # rules whose IDs didn't exist before
          "new_components_count": int, # registry.components added
          "new_signals_count": int,    # registry.signals added
          "total_rules_after": int,
          "dump_bytes_added": int,
        }

    Raises RuntimeError on hard failures (missing pack, empty Scout).
    """
    settings = get_settings()
    memory_root = memory_root or Path(settings.memory_root)
    pack_dir = memory_root / device_slug
    if not pack_dir.exists():
        raise RuntimeError(f"no pack on disk for slug={device_slug!r}")

    focus_refdes = focus_refdes or []
    if not focus_symptoms:
        raise RuntimeError("expand_pack requires at least one focus symptom")

    if client is None:
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        client = AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=settings.anthropic_max_retries)

    # Load current state.
    existing_registry = Registry.model_validate_json(
        (pack_dir / "registry.json").read_text(encoding="utf-8")
    )
    existing_rules_raw = json.loads((pack_dir / "rules.json").read_text(encoding="utf-8"))
    existing_rule_ids = {r["id"] for r in existing_rules_raw.get("rules", [])}
    existing_components = {c.canonical_name for c in existing_registry.components}
    existing_signals = {s.canonical_name for s in existing_registry.signals}

    device_label = existing_registry.device_label

    # 1. Targeted Scout → new chunk.
    model_sonnet = settings.anthropic_model_sonnet
    model_main = settings.anthropic_model_main
    if chunk_provider is not None:
        # Runtime opted in to a Managed-Agent KnowledgeCurator session
        # instead of an inline messages.create Scout. Same output shape.
        logger.info("[Expand] using injected chunk_provider (MA curator)")
        chunk = await chunk_provider(
            device_label=device_label,
            focus_symptoms=focus_symptoms,
            focus_refdes=focus_refdes,
        )
    else:
        chunk = await _run_targeted_scout(
            client=client,
            model=model_sonnet,
            device_label=device_label,
            focus_symptoms=focus_symptoms,
            focus_refdes=focus_refdes,
        )
    _append_scout_chunk(pack_dir, chunk, focus_symptoms)
    dump_bytes_added = len(chunk)

    # 2. Re-run Registry on the FULL cumulative dump → absorbs new items.
    full_dump = (pack_dir / "raw_research_dump.md").read_text(encoding="utf-8")
    new_registry = await run_registry_builder(
        client=client, model=model_sonnet,
        device_label=device_label, raw_dump=full_dump,
    )
    # Preserve taxonomy from the pre-existing registry if the new one
    # somehow regressed to all-null (it shouldn't — same sources). Empty
    # taxonomy from a single-symptom-focused re-run would lose the brand.
    if existing_registry.taxonomy and not any(
        getattr(new_registry.taxonomy, field)
        for field in ("brand", "model", "version", "form_factor")
    ):
        new_registry.taxonomy = existing_registry.taxonomy

    (pack_dir / "registry.json").write_text(
        new_registry.model_dump_json(indent=2), encoding="utf-8"
    )

    # 3. Re-run Clinicien on full dump + merged registry → new rule set.
    new_rules = await _run_clinicien_on_full_dump(
        client=client, model=model_main,
        device_label=device_label, raw_dump=full_dump, registry=new_registry,
    )
    (pack_dir / "rules.json").write_text(
        new_rules.model_dump_json(indent=2), encoding="utf-8"
    )

    # 4. Count deltas for telemetry / UI feedback.
    new_component_names = {c.canonical_name for c in new_registry.components}
    new_signal_names = {s.canonical_name for s in new_registry.signals}
    new_rule_ids = {r.id for r in new_rules.rules}
    summary = {
        "expanded": True,
        "focus_symptoms": focus_symptoms,
        "focus_refdes": focus_refdes,
        "new_rules_count": len(new_rule_ids - existing_rule_ids),
        "new_components_count": len(new_component_names - existing_components),
        "new_signals_count": len(new_signal_names - existing_signals),
        "total_rules_after": len(new_rule_ids),
        "dump_bytes_added": dump_bytes_added,
    }
    logger.info("[Expand] done · %s", summary)
    return summary
