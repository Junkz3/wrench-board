# SPDX-License-Identifier: Apache-2.0
"""Phase 3 — 3 Writers running in parallel with a shared, cached prefix.

The 3 writers (Cartographe / Clinicien / Lexicographe) share:
- Identical `tools` array (all 3 submit_* tools declared)
- Identical `system` prompt (`WRITER_SYSTEM`)
- Identical user-message prefix containing the raw dump + registry, with a
  `cache_control: ephemeral` breakpoint

They differ only in:
- The user-message suffix (per-writer task instructions)
- `tool_choice` — each forced to its specific submit_* tool

We launch writer 1 first and `asyncio.sleep(CACHE_WARMUP_SECONDS)` before dispatching
writers 2 and 3, so Anthropic has time to materialize the cache entry from writer 1's
request and serve it to the others.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from anthropic import AsyncAnthropic

from api.pipeline.prompts import (
    CARTOGRAPHE_TASK,
    CLINICIEN_TASK,
    LEXICOGRAPHE_TASK,
    WRITER_SHARED_USER_PREFIX_TEMPLATE,
    WRITER_SYSTEM,
)
from api.pipeline.schemas import Dictionary, KnowledgeGraph, Registry, RulesSet
from api.pipeline.tool_call import call_with_forced_tool

if TYPE_CHECKING:
    from api.pipeline.telemetry.token_stats import PhaseTokenStats

logger = logging.getLogger("microsolder.pipeline.writers")


# Tool names — must match the forced tool_choice calls below.
SUBMIT_KG_TOOL_NAME = "submit_knowledge_graph"
SUBMIT_RULES_TOOL_NAME = "submit_rules"
SUBMIT_DICT_TOOL_NAME = "submit_dictionary"


def _submit_kg_tool() -> dict:
    return {
        "name": SUBMIT_KG_TOOL_NAME,
        "description": "Cartographe output — typed knowledge graph.",
        "input_schema": KnowledgeGraph.model_json_schema(),
    }


def _submit_rules_tool() -> dict:
    return {
        "name": SUBMIT_RULES_TOOL_NAME,
        "description": "Clinicien output — diagnostic rules.",
        "input_schema": RulesSet.model_json_schema(),
    }


def _submit_dict_tool() -> dict:
    return {
        "name": SUBMIT_DICT_TOOL_NAME,
        "description": "Lexicographe output — component sheets.",
        "input_schema": Dictionary.model_json_schema(),
    }


def _all_writer_tools() -> list[dict]:
    """Every writer receives the full set of 3 tools so the tools-layer cache is shared."""
    return [_submit_kg_tool(), _submit_rules_tool(), _submit_dict_tool()]


def _build_shared_user_messages(
    *,
    device_label: str,
    raw_dump: str,
    registry: Registry,
    task_suffix: str,
) -> list[dict]:
    """Build the per-writer message list. The first content block carries the
    `cache_control: ephemeral` marker so the prefix caches across the 3 writers.
    """
    shared_prefix = WRITER_SHARED_USER_PREFIX_TEMPLATE.format(
        device_label=device_label,
        raw_dump=raw_dump,
        registry_json=registry.model_dump_json(indent=2),
    )
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": shared_prefix,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": task_suffix,
                },
            ],
        }
    ]


async def _run_single_writer(
    *,
    client: AsyncAnthropic,
    model: str,
    device_label: str,
    raw_dump: str,
    registry: Registry,
    task_suffix: str,
    forced_tool_name: str,
    output_schema,
    log_label: str,
    stats: PhaseTokenStats | None = None,
):
    messages = _build_shared_user_messages(
        device_label=device_label,
        raw_dump=raw_dump,
        registry=registry,
        task_suffix=task_suffix,
    )
    return await call_with_forced_tool(
        client=client,
        model=model,
        system=WRITER_SYSTEM,
        messages=messages,
        tools=_all_writer_tools(),
        forced_tool_name=forced_tool_name,
        output_schema=output_schema,
        max_attempts=2,
        log_label=log_label,
        stats=stats,
    )


async def run_writers_parallel(
    *,
    client: AsyncAnthropic,
    cartographe_model: str,
    clinicien_model: str,
    lexicographe_model: str,
    device_label: str,
    raw_dump: str,
    registry: Registry,
    cache_warmup_seconds: float = 1.0,
    writer_stats: dict[str, PhaseTokenStats] | None = None,
) -> tuple[KnowledgeGraph, RulesSet, Dictionary]:
    """Launch the 3 writers with a staggered start for cache warming.

    Writer 1 (Cartographe) goes first — it writes the cache. We sleep briefly, then
    fire writers 2 (Clinicien) and 3 (Lexicographe) concurrently.

    Prompt cache is model-scoped, so Cartographe + Clinicien (same model) share a
    cache entry, while Lexicographe — typically a cheaper model — writes its own.
    That split costs one extra cache_creation per run but saves far more on the
    per-component extraction tokens.
    """
    logger.info(
        "[Writers] Starting parallel writers "
        "(cart=%s clin=%s lex=%s · cache_warmup=%.1fs) for device=%r",
        cartographe_model,
        clinicien_model,
        lexicographe_model,
        cache_warmup_seconds,
        device_label,
    )

    kg_task = asyncio.create_task(
        _run_single_writer(
            client=client,
            model=cartographe_model,
            device_label=device_label,
            raw_dump=raw_dump,
            registry=registry,
            task_suffix=CARTOGRAPHE_TASK,
            forced_tool_name=SUBMIT_KG_TOOL_NAME,
            output_schema=KnowledgeGraph,
            log_label="Cartographe",
            stats=writer_stats.get("cartographe") if writer_stats else None,
        ),
        name="writer-cartographe",
    )

    logger.info(
        "[Writers] Cartographe dispatched · waiting %.1fs for cache warm-up", cache_warmup_seconds
    )
    await asyncio.sleep(cache_warmup_seconds)

    rules_task = asyncio.create_task(
        _run_single_writer(
            client=client,
            model=clinicien_model,
            device_label=device_label,
            raw_dump=raw_dump,
            registry=registry,
            task_suffix=CLINICIEN_TASK,
            forced_tool_name=SUBMIT_RULES_TOOL_NAME,
            output_schema=RulesSet,
            log_label="Clinicien",
            stats=writer_stats.get("clinicien") if writer_stats else None,
        ),
        name="writer-clinicien",
    )
    dict_task = asyncio.create_task(
        _run_single_writer(
            client=client,
            model=lexicographe_model,
            device_label=device_label,
            raw_dump=raw_dump,
            registry=registry,
            task_suffix=LEXICOGRAPHE_TASK,
            forced_tool_name=SUBMIT_DICT_TOOL_NAME,
            output_schema=Dictionary,
            log_label="Lexicographe",
            stats=writer_stats.get("lexicographe") if writer_stats else None,
        ),
        name="writer-lexicographe",
    )

    logger.info("[Writers] Clinicien + Lexicographe dispatched in parallel")
    kg, rules, dictionary = await asyncio.gather(kg_task, rules_task, dict_task)

    logger.info(
        "[Writers] All 3 writers complete · kg.nodes=%d rules=%d dict.entries=%d",
        len(kg.nodes),
        len(rules.rules),
        len(dictionary.entries),
    )
    return kg, rules, dictionary


async def run_single_writer_revision(
    *,
    client: AsyncAnthropic,
    cartographe_model: str,
    clinicien_model: str,
    lexicographe_model: str,
    device_label: str,
    raw_dump: str,
    registry: Registry,
    file_name: str,
    revision_brief: str,
    previous_output_json: str,
) -> KnowledgeGraph | RulesSet | Dictionary:
    """Re-run one writer with a revision brief from the Auditor.

    Must use the same model that produced the original output, so the revised
    artefact stays coherent with the first pass (same taste, same shape).
    """
    # Import here to avoid circular import if orchestrator ever imports this module.
    from api.pipeline.prompts import REVISER_USER_TEMPLATE

    mapping = {
        "knowledge_graph": (
            SUBMIT_KG_TOOL_NAME,
            KnowledgeGraph,
            "Cartographe-Revise",
            cartographe_model,
        ),
        "rules": (SUBMIT_RULES_TOOL_NAME, RulesSet, "Clinicien-Revise", clinicien_model),
        "dictionary": (
            SUBMIT_DICT_TOOL_NAME,
            Dictionary,
            "Lexicographe-Revise",
            lexicographe_model,
        ),
    }
    if file_name not in mapping:
        raise ValueError(f"Unknown file_name for revision: {file_name!r}")

    tool_name, output_schema, log_label, model = mapping[file_name]

    # Keep the shared cached prefix identical so the cache still serves.
    shared_prefix = WRITER_SHARED_USER_PREFIX_TEMPLATE.format(
        device_label=device_label,
        raw_dump=raw_dump,
        registry_json=registry.model_dump_json(indent=2),
    )
    revision_suffix = REVISER_USER_TEMPLATE.format(
        revision_brief=revision_brief,
        previous_output_json=previous_output_json,
        tool_name=tool_name,
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
                {
                    "type": "text",
                    "text": revision_suffix,
                },
            ],
        }
    ]

    logger.info("[Revise] Rewriting file=%r", file_name)
    return await call_with_forced_tool(
        client=client,
        model=model,
        system=WRITER_SYSTEM,
        messages=messages,
        tools=_all_writer_tools(),
        forced_tool_name=tool_name,
        output_schema=output_schema,
        max_attempts=2,
        log_label=log_label,
    )
