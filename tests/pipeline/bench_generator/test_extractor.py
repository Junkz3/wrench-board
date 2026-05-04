from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from api.pipeline.bench_generator.extractor import extract_drafts, rescue_with_opus
from api.pipeline.bench_generator.schemas import ProposalsPayload, Rejection


class _StubBlock:
    def __init__(self, name: str, payload: dict):
        self.type = "tool_use"
        self.name = name
        self.input = payload


class _StubResponse:
    def __init__(self, payload: dict):
        self.content = [_StubBlock("propose_scenarios", payload)]
        self.usage = MagicMock(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )


class _StubStream:
    def __init__(self, response: _StubResponse):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_final_message(self):
        return self._response


@pytest.mark.asyncio
async def test_extract_returns_payload(toy_graph, sample_draft):
    client = MagicMock()
    client.messages.stream = MagicMock(
        return_value=_StubStream(
            _StubResponse(
                {
                    "scenarios": [sample_draft.model_dump()],
                }
            )
        )
    )

    payload = await extract_drafts(
        client=client,
        model="claude-sonnet-4-6",
        raw_dump="dump " * 100,
        rules_json="{}",
        registry_json="{}",
        graph=toy_graph,
    )
    assert isinstance(payload, ProposalsPayload)
    assert len(payload.scenarios) == 1
    assert payload.scenarios[0].local_id == "c19-short"


@pytest.mark.asyncio
async def test_extract_empty_scenarios_is_valid(toy_graph):
    client = MagicMock()
    client.messages.stream = MagicMock(return_value=_StubStream(_StubResponse({"scenarios": []})))
    payload = await extract_drafts(
        client=client,
        model="claude-sonnet-4-6",
        raw_dump="dump " * 100,
        rules_json="{}",
        registry_json="{}",
        graph=toy_graph,
    )
    assert payload.scenarios == []


@pytest.mark.asyncio
async def test_rescue_filters_eligible_motives(toy_graph, sample_draft):
    """Only evidence_span_not_literal and refdes_not_in_graph are retried."""
    eligible = Rejection(
        local_id="e1",
        motive="evidence_span_not_literal",
        detail="",
        original_draft=sample_draft,
    )
    ineligible = Rejection(
        local_id="d1",
        motive="duplicate_in_run",
        detail="",
        original_draft=sample_draft,
    )

    client = MagicMock()
    # Mock returns nothing (no rescue) — we just check filtering
    client.messages.stream = MagicMock(return_value=_StubStream(_StubResponse({"scenarios": []})))
    rescued, still_rejected = await rescue_with_opus(
        client=client,
        model="claude-opus-4-7",
        rejections=[eligible, ineligible],
        graph=toy_graph,
    )
    # Eligible was fed to Opus (no scenario returned) -> opus_rescue_failed
    # Ineligible stays with original motive
    assert len(rescued) == 0
    assert len(still_rejected) == 2
    motives = {r.motive for r in still_rejected}
    assert "opus_rescue_failed" in motives
    assert "duplicate_in_run" in motives


@pytest.mark.asyncio
async def test_rescue_returns_corrected_draft(toy_graph, sample_draft):
    eligible = Rejection(
        local_id="e-1",
        motive="evidence_span_not_literal",
        detail="",
        original_draft=sample_draft,
    )
    corrected = sample_draft.model_dump()
    corrected["local_id"] = "e-1"  # keep id for traceability (min_length=3)
    client = MagicMock()
    client.messages.stream = MagicMock(
        return_value=_StubStream(_StubResponse({"scenarios": [corrected]}))
    )
    rescued, still_rejected = await rescue_with_opus(
        client=client,
        model="claude-opus-4-7",
        rejections=[eligible],
        graph=toy_graph,
    )
    assert len(rescued) == 1
    assert rescued[0].local_id == "e-1"
    assert still_rejected == []
