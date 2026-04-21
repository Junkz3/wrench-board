"""Phase 4 — Auditor. Verifies internal consistency of the generated knowledge pack
and emits a structured verdict that drives the self-healing loop.
"""

from __future__ import annotations

import logging

from anthropic import AsyncAnthropic

from api.pipeline.prompts import AUDITOR_SYSTEM, AUDITOR_USER_TEMPLATE
from api.pipeline.schemas import AuditVerdict, Dictionary, KnowledgeGraph, Registry, RulesSet
from api.pipeline.tool_call import call_with_forced_tool

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
) -> AuditVerdict:
    """Execute Phase 4 — return a validated `AuditVerdict`."""
    logger.info("[Auditor] Auditing knowledge pack for device=%r", device_label)

    user_prompt = AUDITOR_USER_TEMPLATE.format(
        device_label=device_label,
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
    )

    logger.info(
        "[Auditor] Verdict=%s · consistency=%.2f · files_to_rewrite=%s",
        verdict.overall_status,
        verdict.consistency_score,
        verdict.files_to_rewrite,
    )
    return verdict
