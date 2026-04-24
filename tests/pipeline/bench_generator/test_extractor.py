# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from api.pipeline.bench_generator.extractor import extract_drafts
from api.pipeline.bench_generator.schemas import ProposalsPayload


class _StubBlock:
    def __init__(self, name: str, payload: dict):
        self.type = "tool_use"
        self.name = name
        self.input = payload


class _StubResponse:
    def __init__(self, payload: dict):
        self.content = [_StubBlock("propose_scenarios", payload)]
        self.usage = MagicMock(
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
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
        return_value=_StubStream(_StubResponse({
            "scenarios": [sample_draft.model_dump()],
        }))
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
    client.messages.stream = MagicMock(
        return_value=_StubStream(_StubResponse({"scenarios": []}))
    )
    payload = await extract_drafts(
        client=client, model="claude-sonnet-4-6",
        raw_dump="dump " * 100, rules_json="{}", registry_json="{}",
        graph=toy_graph,
    )
    assert payload.scenarios == []
