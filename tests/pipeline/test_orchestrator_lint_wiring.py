"""Wiring test — pack_lint findings are produced + persisted by the orchestrator.

Drives `generate_knowledge_pack` with every network phase mocked (same pattern
as test_orchestrator_events.py) far enough to reach the post-audit lint+persist
step, and asserts `pack_quality.json` is written with the findings structure.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from api.pipeline import orchestrator
from api.pipeline.schemas import (
    AuditVerdict,
    Cause,
    Dictionary,
    KnowledgeGraph,
    Registry,
    Rule,
    RulesSet,
)


@pytest.fixture
def approved_verdict() -> AuditVerdict:
    return AuditVerdict(
        overall_status="APPROVED",
        consistency_score=1.0,
        files_to_rewrite=[],
        drift_report=[],
        revision_brief="",
    )


async def _drive(tmp_path, *, registry, rules, verdict):
    kg = KnowledgeGraph(nodes=[], edges=[])
    dictionary = Dictionary(entries=[])
    with (
        patch(
            "api.pipeline.orchestrator.run_scout",
            new=AsyncMock(return_value="# dump"),
        ),
        patch(
            "api.pipeline.orchestrator.run_registry_builder",
            new=AsyncMock(return_value=registry),
        ),
        patch(
            "api.pipeline.orchestrator.run_writers_parallel",
            new=AsyncMock(return_value=(kg, rules, dictionary)),
        ),
        patch(
            "api.pipeline.orchestrator.run_auditor",
            new=AsyncMock(return_value=verdict),
        ),
    ):
        return await orchestrator.generate_knowledge_pack(
            "Demo",
            client=object(),
            memory_root=tmp_path,
        )


async def test_clean_pack_writes_empty_findings(tmp_path, approved_verdict):
    """No graph + benign rules → pack_quality.json with an empty findings list."""
    registry = Registry(device_label="Demo", components=[], signals=[])
    rules = RulesSet(rules=[])

    result = await _drive(
        tmp_path, registry=registry, rules=rules, verdict=approved_verdict
    )

    assert result.verdict.overall_status == "APPROVED"
    pq = tmp_path / "demo" / "pack_quality.json"
    assert pq.is_file()
    data = json.loads(pq.read_text(encoding="utf-8"))
    assert data == {"lint_findings": []}


async def test_mixed_kind_rule_finding_is_persisted(tmp_path, approved_verdict):
    """Rules mixing laptop + GPU markers → a reject finding lands on disk."""
    registry = Registry(device_label="Demo", components=[], signals=[])
    # The Clinicien rule text carries both a laptop and a GPU marker, which the
    # lint regexes flag as a mixed-kind pack. Both markers end up in the
    # serialized rules JSON that the orchestrator feeds to lint_pack.
    rules = RulesSet(
        rules=[
            Rule(
                id="R-MIXED-001",
                symptoms=["No power on the barrel jack 19V laptop input"],
                likely_causes=[
                    Cause(
                        refdes="U1",
                        probability=0.5,
                        mechanism="PCIe GPU graphics card rail short-to-ground",
                    )
                ],
            )
        ]
    )

    await _drive(tmp_path, registry=registry, rules=rules, verdict=approved_verdict)

    pq = tmp_path / "demo" / "pack_quality.json"
    assert pq.is_file()
    data = json.loads(pq.read_text(encoding="utf-8"))
    codes = {f["code"] for f in data["lint_findings"]}
    assert "mixed_kind_rule" in codes
    mixed = next(f for f in data["lint_findings"] if f["code"] == "mixed_kind_rule")
    assert mixed["severity"] == "reject"
    assert mixed["detail"]
