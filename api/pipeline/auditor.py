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

from api.pipeline.prompts import AUDITOR_SYSTEM, AUDITOR_USER_TEMPLATE
from api.pipeline.schemas import (
    AuditVerdict,
    Dictionary,
    DriftItem,
    KnowledgeGraph,
    Registry,
    RulesSet,
)
from api.pipeline.tool_call import call_with_forced_tool

if TYPE_CHECKING:
    from api.pipeline.telemetry.token_stats import PhaseTokenStats

logger = logging.getLogger("microsolder.pipeline.auditor")


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
    stats: PhaseTokenStats | None = None,
) -> AuditVerdict:
    """Execute Phase 4 — return a validated `AuditVerdict`.

    `precomputed_drift` is the code-level set-diff result; the LLM must include
    it verbatim and focus on coherence + plausibility judgment.
    """
    logger.info(
        "[Auditor] Auditing knowledge pack for device=%r · precomputed_drift=%d items",
        device_label,
        len(precomputed_drift),
    )

    precomputed_drift_json = json.dumps(
        [item.model_dump() for item in precomputed_drift], indent=2
    )

    user_prompt = AUDITOR_USER_TEMPLATE.format(
        device_label=device_label,
        precomputed_drift_json=precomputed_drift_json,
        registry_json=registry.model_dump_json(indent=2),
        knowledge_graph_json=knowledge_graph.model_dump_json(indent=2),
        rules_json=rules.model_dump_json(indent=2),
        dictionary_json=dictionary.model_dump_json(indent=2),
    )

    verdict = await call_with_forced_tool(
        client=client,
        model=model,
        system=AUDITOR_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        tools=[_submit_audit_tool()],
        forced_tool_name=SUBMIT_AUDIT_TOOL_NAME,
        output_schema=AuditVerdict,
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
