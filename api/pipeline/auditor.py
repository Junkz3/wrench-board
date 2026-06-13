"""Phase 4 — Auditor. Verifies internal consistency of the generated knowledge pack
and emits a structured verdict that drives the self-healing loop.

Vocabulary drift is pre-computed at code level by `api.pipeline.drift.compute_drift`
and passed to the LLM as ground truth — the LLM's real job is cross-file coherence
and plausibility judgment.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from anthropic import AsyncAnthropic

from api.pipeline.graph_truth import (
    QUERY_GRAPH_TOOL,
    GraphTruth,
    handle_query_graph,
)
from api.pipeline.prompts import (
    AUDITOR_SYSTEM,
    AUDITOR_USER_CONTEXT_TEMPLATE,
    AUDITOR_USER_DIRECTIVE_TEMPLATE,
)
from api.pipeline.schemas import (
    AuditVerdict,
    Dictionary,
    DriftItem,
    KnowledgeGraph,
    Registry,
    RulesSet,
)
from api.pipeline.tool_call import call_with_forced_tool, call_with_query_tools

if TYPE_CHECKING:
    from api.pipeline.telemetry.token_stats import PhaseTokenStats

logger = logging.getLogger("wrench_board.pipeline.auditor")


SUBMIT_AUDIT_TOOL_NAME = "submit_audit_verdict"


def _submit_audit_tool() -> dict:
    return {
        "name": SUBMIT_AUDIT_TOOL_NAME,
        "description": "Submit the structured audit verdict. Your only valid output.",
        "input_schema": AuditVerdict.model_json_schema(),
    }


async def run_auditor(
    *,
    client: AsyncAnthropic,
    model: str,
    device_label: str,
    registry: Registry,
    knowledge_graph: KnowledgeGraph,
    rules: RulesSet,
    dictionary: Dictionary,
    precomputed_drift: list[DriftItem],
    revision_brief: str = "",
    graph_truth: GraphTruth | None = None,
    ground_truth_report: str | None = None,
    max_query_turns: int = 8,
    stats: PhaseTokenStats | None = None,
) -> AuditVerdict:
    """Execute Phase 4 — return a validated `AuditVerdict`.

    `precomputed_drift` is the code-level set-diff result; the LLM must include
    it verbatim and focus on coherence + plausibility judgment.

    When a compiled electrical graph exists (`graph_truth` is not None), the
    auditor is upgraded from a single forced-tool call to the capped agentic
    `call_with_query_tools` loop: it receives (a) `ground_truth_report` — a
    deterministic existence report over every mentioned identifier — injected
    into the cached context block, and (b) the `query_graph` tool so it can
    VERIFY a refdes/rail/source against the REAL schematic before flagging it.

    This closes the macbook-air-m1 failure: anchored only on the thin web
    registry, the auditor accused real identifiers (SWV011, the U8100→PP1V2_S2
    rail) of being fabricated and its briefs would have corrupted true facts.

    Web-only packs (`graph_truth is None`) keep TODAY'S EXACT path — one forced
    `submit_audit_verdict` call, no ground-truth block, no query tool — so the
    self-host / no-graph flow is byte-for-byte unchanged.
    """
    logger.info(
        "[Auditor] Auditing knowledge pack for device=%r · precomputed_drift=%d items",
        device_label,
        len(precomputed_drift),
    )

    precomputed_drift_json = json.dumps(
        [item.model_dump() for item in precomputed_drift], indent=2
    )

    # The deterministic ground-truth report (when present) is framed as its own
    # block right after the precomputed-drift section and BEFORE the registry —
    # it is the EXISTENCE authority the model must read before trusting/doubting
    # any registry entry. Empty string when no graph, so the rendered context is
    # identical to the legacy web-only path. The block lives INSIDE the cached
    # context text so the prefix stays stable ACROSS THE QUERY TURNS OF ONE
    # API call (the agentic loop re-sends the same context per turn) — that is
    # the only cache scope that exists here anyway: between revise rounds the
    # kg/rules/dictionary JSON change, so the round's cache block is busted
    # regardless of this report.
    ground_truth_block = ""
    if ground_truth_report is not None:
        ground_truth_block = (
            "# Schematic ground truth (deterministic)\n" + ground_truth_report + "\n\n"
        )

    context_text = AUDITOR_USER_CONTEXT_TEMPLATE.format(
        device_label=device_label,
        precomputed_drift_json=precomputed_drift_json,
        ground_truth_block=ground_truth_block,
        registry_json=registry.model_dump_json(indent=2),
        knowledge_graph_json=knowledge_graph.model_dump_json(indent=2),
        rules_json=rules.model_dump_json(indent=2),
        dictionary_json=dictionary.model_dump_json(indent=2),
    )
    revision_block = ""
    if revision_brief:
        # Anti-ancrage (re-run réel macbook-air-m1, 2026-06-11) : présenté comme
        # « # Revision brief » nu, l'auditor RECOPIAIT les reproches du round
        # précédent sans re-vérifier — il a re-flagué 4 groupes de fixes déjà
        # appliqués sur disque et fait régresser le score (0.62→0.55 → early-stop
        # → échec). Le brief précédent est un HISTORIQUE de ce qui a été demandé,
        # pas une liste de griefs à reconduire.
        revision_block = (
            "# Previous revision brief — these fixes have ALREADY been applied "
            "by the revisers\n"
            f"{revision_brief}\n\n"
            "Before repeating ANY item above: re-verify it against the CURRENT "
            "files (and `query_graph` when available). An item that is resolved "
            "in the current files must NOT reappear in your brief or "
            "drift_report, and your consistency_score must rate the CURRENT "
            "files on their own merits — not the history of past rounds.\n\n"
        )
    directive_text = AUDITOR_USER_DIRECTIVE_TEMPLATE.format(
        revision_brief_block=revision_block,
    )

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": context_text,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": directive_text,
                },
            ],
        }
    ]

    # Dispatch on the presence of a graph. No graph → the existing single forced
    # `submit_audit_verdict` call, UNCHANGED (web-only / self-host path). A graph
    # → the agentic query loop: same system/messages/model, but the model may
    # call `query_graph` (bound to THIS pack's GraphTruth) up to `max_query_turns`
    # times to verify identifiers against the real schematic before it must submit.
    if graph_truth is None:
        verdict = await call_with_forced_tool(
            client=client,
            model=model,
            system=AUDITOR_SYSTEM,
            messages=messages,
            tools=[_submit_audit_tool()],
            forced_tool_name=SUBMIT_AUDIT_TOOL_NAME,
            output_schema=AuditVerdict,
            max_attempts=2,
            log_label="Auditor",
            stats=stats,
        )
    else:
        verdict = await call_with_query_tools(
            client=client,
            model=model,
            system=AUDITOR_SYSTEM,
            messages=messages,
            query_tool=QUERY_GRAPH_TOOL,
            # Closure binds the deterministic handler to this pack's graph — the
            # loop hands us only the raw tool input, never the graph.
            query_handler=lambda i: handle_query_graph(graph_truth, i),
            submit_tool=_submit_audit_tool(),
            submit_tool_name=SUBMIT_AUDIT_TOOL_NAME,
            output_schema=AuditVerdict,
            max_query_turns=max_query_turns,
            max_attempts=2,
            log_label="Auditor",
            stats=stats,
        )

    logger.info(
        "[Auditor] Verdict=%s · consistency=%.2f · files_to_rewrite=%s",
        verdict.overall_status,
        verdict.consistency_score,
        verdict.files_to_rewrite,
    )
    return verdict
