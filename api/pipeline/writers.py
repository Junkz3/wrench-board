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

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from anthropic import AsyncAnthropic

from api.config import get_settings
from api.pipeline.patch import (
    PatchApplyError,
    apply_dictionary_patch,
    apply_kg_patch,
    apply_rules_patch,
)
from api.pipeline.prompts import (
    CARTOGRAPHE_TASK,
    CLINICIEN_TASK,
    LEXICOGRAPHE_TASK,
    WRITER_SHARED_USER_PREFIX_TEMPLATE,
    WRITER_SYSTEM,
)
from api.pipeline.schemas import (
    Dictionary,
    DictionaryPatch,
    KnowledgeGraph,
    KnowledgeGraphPatch,
    Registry,
    RulesPatch,
    RulesSet,
)
from api.pipeline.tool_call import call_with_forced_tool, call_with_query_tools

if TYPE_CHECKING:
    from api.pipeline.graph_truth import GraphTruth
    from api.pipeline.telemetry.token_stats import PhaseTokenStats

logger = logging.getLogger("wrench_board.pipeline.writers")


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


# Reviser patch tools — the revise path forces ONE of these (per file_name)
# instead of the full submit_* tool, so the reviser emits a surgical delta the
# `api.pipeline.patch` applicator applies to the current artefact.
SUBMIT_KG_PATCH_TOOL_NAME = "submit_knowledge_graph_patch"
SUBMIT_RULES_PATCH_TOOL_NAME = "submit_rules_patch"
SUBMIT_DICT_PATCH_TOOL_NAME = "submit_dictionary_patch"


def _submit_kg_patch_tool() -> dict:
    return {
        "name": SUBMIT_KG_PATCH_TOOL_NAME,
        "description": "Surgical delta over the knowledge graph — only the nodes/edges you change.",
        "input_schema": KnowledgeGraphPatch.model_json_schema(),
    }


def _submit_rules_patch_tool() -> dict:
    return {
        "name": SUBMIT_RULES_PATCH_TOOL_NAME,
        "description": "Surgical delta over the rules — only the rules you change.",
        "input_schema": RulesPatch.model_json_schema(),
    }


def _submit_dict_patch_tool() -> dict:
    return {
        "name": SUBMIT_DICT_PATCH_TOOL_NAME,
        "description": "Surgical delta over the dictionary — only the entries you change.",
        "input_schema": DictionaryPatch.model_json_schema(),
    }


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
        max_attempts=5,
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
    cache_warmup_seconds: float | None = None,
    writer_stats: dict[str, PhaseTokenStats] | None = None,
    on_event: Callable[[dict], Awaitable[None]] | None = None,
) -> tuple[KnowledgeGraph, RulesSet, Dictionary]:
    """Launch the 3 writers with a staggered start for cache warming.

    Writer 1 (Cartographe) goes first — it writes the cache. We sleep briefly, then
    fire writers 2 (Clinicien) and 3 (Lexicographe) concurrently.

    Prompt cache is model-scoped, so Cartographe + Clinicien (same model) share a
    cache entry, while Lexicographe — typically a cheaper model — writes its own.
    That split costs one extra cache_creation per run but saves far more on the
    per-component extraction tokens.

    `cache_warmup_seconds` falls back to `Settings.pipeline_cache_warmup_seconds`
    when None — that setting is the single source of truth for the empirically
    tuned warmup window (3.0s, see `api/config.py`); the param exists only so
    tests can drop it to 0 without monkeypatching settings.
    """
    if cache_warmup_seconds is None:
        cache_warmup_seconds = get_settings().pipeline_cache_warmup_seconds
    logger.info(
        "[Writers] Starting parallel writers "
        "(cart=%s clin=%s lex=%s · cache_warmup=%.1fs) for device=%r",
        cartographe_model,
        clinicien_model,
        lexicographe_model,
        cache_warmup_seconds,
        device_label,
    )

    async def _emit_done(coro, writer: str, count_fn):
        """Await one writer, then emit a live `phase_step` as it completes.

        Wrapping each writer (rather than emitting after the gather) is what
        makes the landing line tick "graphe ✓ … règles ✓ … dico ✓" as the 3
        finish at their own pace, not all at once.
        """
        result = await coro
        if on_event is not None:
            await on_event({
                "type": "phase_step", "phase": "writers", "step": "writer_done",
                "writer": writer, "count": count_fn(result),
            })
        return result

    kg_task = asyncio.create_task(
        _emit_done(
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
            "graph",
            lambda kg: len(kg.nodes),
        ),
        name="writer-cartographe",
    )

    logger.info(
        "[Writers] Cartographe dispatched · waiting %.1fs for cache warm-up", cache_warmup_seconds
    )
    await asyncio.sleep(cache_warmup_seconds)

    rules_task = asyncio.create_task(
        _emit_done(
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
            "rules",
            lambda rules: len(rules.rules),
        ),
        name="writer-clinicien",
    )
    dict_task = asyncio.create_task(
        _emit_done(
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
            "dict",
            lambda d: len(d.entries),
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


# The reviser mapping. Each entry is the SURGICAL-PATCH surface for one writer
# role: the patch tool name, the patch schema the reviser emits, and the
# deterministic applicator that turns that patch into the new artefact. Keyed by
# the canonical file_name (knowledge_graph / rules / dictionary). The reviser
# emits a delta — not the whole artefact — so unflagged records are preserved
# verbatim (no collateral-regression surface).
_REVISE_MAPPING = {
    "knowledge_graph": (
        SUBMIT_KG_PATCH_TOOL_NAME, KnowledgeGraphPatch, apply_kg_patch, "Cartographe-Revise"
    ),
    "rules": (
        SUBMIT_RULES_PATCH_TOOL_NAME, RulesPatch, apply_rules_patch, "Clinicien-Revise"
    ),
    "dictionary": (
        SUBMIT_DICT_PATCH_TOOL_NAME, DictionaryPatch, apply_dictionary_patch, "Lexicographe-Revise"
    ),
}


def _submit_patch_tool_for(file_name: str) -> dict:
    """The single patch-submit tool object for one writer role (by file_name)."""
    return {
        "knowledge_graph": _submit_kg_patch_tool,
        "rules": _submit_rules_patch_tool,
        "dictionary": _submit_dict_patch_tool,
    }[file_name]()


def _build_siblings_block(
    *,
    file_name: str,
    current_kg: KnowledgeGraph,
    current_rules: RulesSet,
    current_dictionary: Dictionary,
) -> str:
    """Render the TWO files that are NOT `file_name` as `## <name> (current)`
    JSON sections — the up-to-date cross-file context the reviser must align with.

    RC1 (the convergence bug): when a reviser only ever saw its OWN previous
    output, revise-round-1 re-aligned each of kg/rules/dictionary against the
    STALE versions of the other two, collapsing a state that no longer existed.
    Handing the reviser the CURRENT siblings (read-only) is the fix — it aligns
    cross-file references against reality, not memory. The reviser's own file is
    excluded (it's the BASELINE in `previous_output_json`, not a sibling)."""
    artefacts = {
        "knowledge_graph": current_kg,
        "rules": current_rules,
        "dictionary": current_dictionary,
    }
    sections: list[str] = []
    for name, artefact in artefacts.items():
        if name == file_name:
            continue  # the reviser edits this one — it's the baseline, not a sibling
        sections.append(
            f"## {name} (current)\n```json\n{artefact.model_dump_json(indent=2)}\n```"
        )
    return "\n\n".join(sections)


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
    current_kg: KnowledgeGraph,
    current_rules: RulesSet,
    current_dictionary: Dictionary,
    ground_truth_report: str | None = None,
    graph_truth: GraphTruth | None = None,
    max_query_turns: int = 4,
    stats: PhaseTokenStats | None = None,
) -> KnowledgeGraph | RulesSet | Dictionary:
    """Re-run one writer with a revision brief from the Auditor.

    Must use the same model that produced the original output, so the revised
    artefact stays coherent with the first pass (same taste, same shape).

    The reviser emits a SURGICAL PATCH (a typed delta), not the whole artefact:
    it forces the role's `submit_*_patch` tool, and `apply_fn` applies that delta
    to the current artefact. Records the reviser does not name are preserved
    verbatim — that removes the full re-emit's collateral-regression surface.

    The reviser sees, as READ-ONLY context, the CURRENT versions of the two
    sibling files (`current_kg`/`current_rules`/`current_dictionary` minus its
    own) so it aligns cross-file references against reality — the RC1 fix (see
    `_build_siblings_block`). When a `graph_truth` is supplied it ALSO gets the
    mention-scoped ground-truth report + the `query_graph` tool to verify
    existence/voltage/source against the real schematic before writing.

    A well-formed-but-inapplicable patch (`PatchApplyError`) degrades to a
    no-op: the current artefact is returned unchanged and the re-audit re-flags.
    Nothing corrupts — a previously-silent regression becomes a visible no-op.
    """
    # Import here to avoid circular import if orchestrator ever imports this module.
    from api.pipeline.graph_truth import QUERY_GRAPH_TOOL, handle_query_graph
    from api.pipeline.prompts import REVISER_OPS_HELP, REVISER_USER_TEMPLATE

    model_for = {
        "knowledge_graph": cartographe_model,
        "rules": clinicien_model,
        "dictionary": lexicographe_model,
    }
    current_for = {
        "knowledge_graph": current_kg,
        "rules": current_rules,
        "dictionary": current_dictionary,
    }
    if file_name not in _REVISE_MAPPING:
        raise ValueError(f"Unknown file_name for revision: {file_name!r}")

    tool_name, patch_schema, apply_fn, log_label = _REVISE_MAPPING[file_name]
    model = model_for[file_name]
    current_artefact = current_for[file_name]

    # Read-only sibling context (the up-to-date OTHER two files) + optional
    # deterministic ground-truth. Both ride the revision SUFFIX — the shared
    # cached prefix message structure stays IDENTICAL so the writer cache serves.
    siblings_block = _build_siblings_block(
        file_name=file_name,
        current_kg=current_kg,
        current_rules=current_rules,
        current_dictionary=current_dictionary,
    )
    ground_truth_block = (
        "\n# Schematic ground truth (deterministic — verify via query_graph "
        "before doubting)\n" + ground_truth_report + "\n"
        if ground_truth_report
        else ""
    )

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
        ops_help=REVISER_OPS_HELP[file_name],
        ground_truth_block=ground_truth_block,
        siblings_block=siblings_block,
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

    logger.info("[Revise] Patching file=%r (graph=%s)", file_name, graph_truth is not None)

    # Dispatch on the presence of a graph — mirrors the auditor. No graph → the
    # single forced-tool call (tools = ONLY this role's patch tool, so the reviser
    # can't accidentally emit a sibling's shape). A graph → the capped agentic
    # loop where the reviser may verify identifiers against the real schematic
    # before it submits. Either way the model returns a PATCH, not the artefact.
    # A reviser that can't produce a valid patch must NEVER crash the whole
    # build: keep the current artefact (no-op) and let the re-audit re-flag,
    # exactly like an inapplicable patch below. The 5-attempt budget gives the
    # model room to recover (a misrouted query is already absorbed upstream by
    # the loop's re-route, so these attempts count only genuine submit misses).
    try:
        if graph_truth is None:
            patch = await call_with_forced_tool(
                client=client,
                model=model,
                system=WRITER_SYSTEM,
                messages=messages,
                tools=[_submit_patch_tool_for(file_name)],
                forced_tool_name=tool_name,
                output_schema=patch_schema,
                max_attempts=5,
                log_label=log_label,
                stats=stats,
            )
        else:
            patch = await call_with_query_tools(
                client=client,
                model=model,
                system=WRITER_SYSTEM,
                messages=messages,
                query_tool=QUERY_GRAPH_TOOL,
                # Closure binds the deterministic handler to this pack's graph —
                # the loop hands us only the raw tool input, never the graph.
                query_handler=lambda i: handle_query_graph(graph_truth, i),
                submit_tool=_submit_patch_tool_for(file_name),
                submit_tool_name=tool_name,
                output_schema=patch_schema,
                max_query_turns=max_query_turns,
                max_attempts=5,
                log_label=log_label,
                stats=stats,
            )
    except RuntimeError as exc:
        logger.warning(
            "[Revise] file=%r reviser produced no valid patch (%s) — keeping "
            "current artefact (no-op)",
            file_name,
            exc,
        )
        return current_artefact

    # Apply the delta deterministically. A well-formed-but-inapplicable patch
    # (`PatchApplyError`) degrades to a no-op: keep the current artefact, log it,
    # let the re-audit re-flag. This converts what used to be a silent re-emit
    # regression into a visible, safe no-op.
    try:
        return apply_fn(current_artefact, patch)
    except PatchApplyError as exc:
        logger.warning(
            "[Revise] file=%r patch inapplicable (%s) — keeping current artefact (no-op)",
            file_name,
            exc,
        )
        return current_artefact
