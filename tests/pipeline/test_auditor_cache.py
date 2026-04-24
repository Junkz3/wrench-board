"""Test that the auditor user message uses structured content blocks with cache_control.

P1+P2: verifies Block A (context, ephemeral-cached) and Block B (directive/delta) shape.
"""

from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_auditor_user_message_has_cached_context_block():
    """User message must be a list of two content blocks: [A cached, B delta]."""
    from api.pipeline import auditor as auditor_mod
    from api.pipeline.schemas import (
        AuditVerdict,
        Dictionary,
        KnowledgeGraph,
        Registry,
        RulesSet,
    )

    captured = {}

    async def fake_call(
        *, client, model, system, messages, tools, forced_tool_name, output_schema, **kw
    ):
        captured["messages"] = messages
        return AuditVerdict(
            overall_status="APPROVED",
            consistency_score=1.0,
            drift_report=[],
            files_to_rewrite=[],
            revision_brief="",
        )

    auditor_mod.call_with_forced_tool = fake_call  # type: ignore[attr-defined]

    await auditor_mod.run_auditor(
        client=MagicMock(),
        model="claude-opus-4-7",
        device_label="Demo",
        registry=Registry(
            device_label="Demo",
            components=[],
            signals=[],
        ),
        knowledge_graph=KnowledgeGraph(nodes=[], edges=[]),
        rules=RulesSet(rules=[]),
        dictionary=Dictionary(entries=[]),
        precomputed_drift=[],
    )

    msg = captured["messages"][0]
    assert isinstance(
        msg["content"], list
    ), "content must be structured blocks for cache_control to work"
    assert len(msg["content"]) >= 2

    block_a = msg["content"][0]
    assert block_a["type"] == "text"
    assert block_a.get("cache_control", {}).get("type") == "ephemeral", (
        "block A must be ephemeral-cached"
    )
    assert "Registry" in block_a["text"] or "registry" in block_a["text"]
