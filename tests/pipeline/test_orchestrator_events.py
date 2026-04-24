"""Tests for the on_event callback in generate_knowledge_pack.

The orchestrator talks to Anthropic at every phase — these tests mock every
phase helper to isolate the event-emission contract from the network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from api.pipeline import orchestrator
from api.pipeline.schemas import (
    AuditVerdict,
    Dictionary,
    KnowledgeGraph,
    Registry,
    RulesSet,
)


@pytest.fixture
def dummy_registry() -> Registry:
    return Registry(device_label="Demo", components=[], signals=[])


@pytest.fixture
def dummy_outputs(dummy_registry: Registry):
    return (
        KnowledgeGraph(nodes=[], edges=[]),
        RulesSet(rules=[]),
        Dictionary(entries=[]),
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


async def test_pipeline_emits_phase_events_in_order(
    tmp_path, dummy_registry, dummy_outputs, approved_verdict
):
    kg, rules, dictionary = dummy_outputs
    events: list[dict[str, Any]] = []

    async def collect(ev: dict[str, Any]) -> None:
        events.append(ev)

    with (
        patch("api.pipeline.orchestrator.run_scout", new=AsyncMock(return_value="# dump")),
        patch(
            "api.pipeline.orchestrator.run_registry_builder",
            new=AsyncMock(return_value=dummy_registry),
        ),
        patch(
            "api.pipeline.orchestrator.run_writers_parallel",
            new=AsyncMock(return_value=(kg, rules, dictionary)),
        ),
        patch(
            "api.pipeline.orchestrator.run_auditor",
            new=AsyncMock(return_value=approved_verdict),
        ),
    ):
        result = await orchestrator.generate_knowledge_pack(
            "Demo",
            client=object(),  # unused with all phases mocked
            memory_root=tmp_path,
            on_event=collect,
        )

    assert result.verdict.overall_status == "APPROVED"

    # Expect: pipeline_started → (phase scout s/f) → (registry) → (writers) → (audit) → pipeline_finished
    types = [(e["type"], e.get("phase")) for e in events]
    assert types == [
        ("pipeline_started", None),
        ("phase_started", "scout"),
        ("phase_finished", "scout"),
        ("phase_started", "registry"),
        ("phase_finished", "registry"),
        ("phase_started", "writers"),
        ("phase_finished", "writers"),
        ("phase_started", "audit"),
        ("phase_finished", "audit"),
        ("pipeline_finished", None),
    ]

    start = events[0]
    assert start["device_slug"] == "demo"
    assert start["device_label"] == "Demo"

    done = events[-1]
    assert done["status"] == "APPROVED"
    assert done["revise_rounds_used"] == 0


async def test_pipeline_emits_pipeline_failed_on_rejected_verdict(
    tmp_path, dummy_registry, dummy_outputs
):
    kg, rules, dictionary = dummy_outputs
    rejected = AuditVerdict(
        overall_status="REJECTED",
        consistency_score=0.0,
        files_to_rewrite=[],
        drift_report=[],
        revision_brief="hopeless",
    )
    events: list[dict[str, Any]] = []

    async def collect(ev: dict[str, Any]) -> None:
        events.append(ev)

    with (
        patch("api.pipeline.orchestrator.run_scout", new=AsyncMock(return_value="# dump")),
        patch(
            "api.pipeline.orchestrator.run_registry_builder",
            new=AsyncMock(return_value=dummy_registry),
        ),
        patch(
            "api.pipeline.orchestrator.run_writers_parallel",
            new=AsyncMock(return_value=(kg, rules, dictionary)),
        ),
        patch(
            "api.pipeline.orchestrator.run_auditor",
            new=AsyncMock(return_value=rejected),
        ),
    ):
        with pytest.raises(RuntimeError):
            await orchestrator.generate_knowledge_pack(
                "Demo",
                client=object(),
                memory_root=tmp_path,
                on_event=collect,
            )

    # Must have emitted a pipeline_failed event before raising, so the UI can
    # flip the stepper into its error state instead of hanging on "audit".
    failures = [e for e in events if e["type"] == "pipeline_failed"]
    assert len(failures) == 1
    assert failures[0]["status"] == "REJECTED"


async def test_pipeline_rejects_when_max_revise_rounds_exhausted(
    tmp_path, dummy_registry, dummy_outputs
):
    """NEEDS_REVISION that never clears must end in REJECTED, not silent-accept.

    The pre-refactor code fell through the loop with a warning and returned
    the unresolved verdict — dangerous for a diagnostic tool under the
    anti-hallucination hard rule. Now max-rounds exhaustion is fatal.
    """
    kg, rules, dictionary = dummy_outputs
    needs_revision = AuditVerdict(
        overall_status="NEEDS_REVISION",
        consistency_score=0.4,
        files_to_rewrite=["rules"],
        drift_report=[],
        revision_brief="unresolved drift on U99",
    )
    events: list[dict[str, Any]] = []

    async def collect(ev: dict[str, Any]) -> None:
        events.append(ev)

    with (
        patch("api.pipeline.orchestrator.run_scout", new=AsyncMock(return_value="# dump")),
        patch(
            "api.pipeline.orchestrator.run_registry_builder",
            new=AsyncMock(return_value=dummy_registry),
        ),
        patch(
            "api.pipeline.orchestrator.run_writers_parallel",
            new=AsyncMock(return_value=(kg, rules, dictionary)),
        ),
        patch(
            "api.pipeline.orchestrator.run_auditor",
            new=AsyncMock(return_value=needs_revision),
        ),
        patch(
            "api.pipeline.orchestrator.run_single_writer_revision",
            new=AsyncMock(side_effect=[rules]),
        ),
    ):
        with pytest.raises(RuntimeError, match="revise rounds exhausted"):
            await orchestrator.generate_knowledge_pack(
                "Demo",
                client=object(),
                memory_root=tmp_path,
                max_revise_rounds=1,
                on_event=collect,
            )

    failures = [e for e in events if e["type"] == "pipeline_failed"]
    assert len(failures) == 1
    assert failures[0]["status"] == "REJECTED"

    persisted = (tmp_path / "demo" / "audit_verdict.json").read_text()
    assert '"overall_status": "REJECTED"' in persisted


async def test_pipeline_runs_without_on_event(tmp_path, dummy_registry, dummy_outputs, approved_verdict):
    """on_event is optional — the orchestrator must not crash when it's None."""
    kg, rules, dictionary = dummy_outputs
    with (
        patch("api.pipeline.orchestrator.run_scout", new=AsyncMock(return_value="# dump")),
        patch(
            "api.pipeline.orchestrator.run_registry_builder",
            new=AsyncMock(return_value=dummy_registry),
        ),
        patch(
            "api.pipeline.orchestrator.run_writers_parallel",
            new=AsyncMock(return_value=(kg, rules, dictionary)),
        ),
        patch(
            "api.pipeline.orchestrator.run_auditor",
            new=AsyncMock(return_value=approved_verdict),
        ),
    ):
        result = await orchestrator.generate_knowledge_pack(
            "Demo",
            client=object(),
            memory_root=tmp_path,
        )
    assert result.verdict.overall_status == "APPROVED"


async def test_pipeline_writes_token_stats_even_on_failure(
    tmp_path, dummy_registry, dummy_outputs
):
    """token_stats.json must be persisted even when the pipeline raises RuntimeError.

    This is critical for post-mortem diagnostics — if Phase 4 (REJECTED / max
    revise rounds) fails, we still need to preserve the tokens spent on
    Phases 1-3.
    """
    kg, rules, dictionary = dummy_outputs
    rejected = AuditVerdict(
        overall_status="REJECTED",
        consistency_score=0.0,
        files_to_rewrite=[],
        drift_report=[],
        revision_brief="hopeless",
    )

    def mock_scout_with_stats(*args, **kwargs):
        """Capture stats by appending to the stats kwarg."""
        stats = kwargs.get("stats")
        if stats:
            stats.input_tokens = 100
            stats.output_tokens = 50
            stats.cache_read_input_tokens = 0
            stats.cache_creation_input_tokens = 0
        return "# dump"

    def mock_registry_with_stats(*args, **kwargs):
        stats = kwargs.get("stats")
        if stats:
            stats.input_tokens = 200
            stats.output_tokens = 150
            stats.cache_read_input_tokens = 10
            stats.cache_creation_input_tokens = 0
        return dummy_registry

    with (
        patch(
            "api.pipeline.orchestrator.run_scout",
            new=AsyncMock(side_effect=mock_scout_with_stats),
        ),
        patch(
            "api.pipeline.orchestrator.run_registry_builder",
            new=AsyncMock(side_effect=mock_registry_with_stats),
        ),
        patch(
            "api.pipeline.orchestrator.run_writers_parallel",
            new=AsyncMock(return_value=(kg, rules, dictionary)),
        ),
        patch(
            "api.pipeline.orchestrator.run_auditor",
            new=AsyncMock(return_value=rejected),
        ),
    ):
        with pytest.raises(RuntimeError, match="auditor rejected"):
            await orchestrator.generate_knowledge_pack(
                "Demo",
                client=object(),
                memory_root=tmp_path,
            )

    # token_stats.json must exist even though the pipeline raised
    stats_path = tmp_path / "demo" / "token_stats.json"
    assert stats_path.exists(), "token_stats.json must be written even on failure"

    # Verify the content contains at least the stats we accumulated
    stats_content = stats_path.read_text()
    assert "scout" in stats_content
    assert "registry" in stats_content
