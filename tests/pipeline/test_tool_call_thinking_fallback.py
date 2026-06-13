"""call_with_forced_tool: on a thinking-only response (no tool_use), the retry
must DROP thinking and force tool_choice so the tool is guaranteed — fixes the
'got blocks: [thinking]' failures + the wasted 64k-token retries seen on a real
92-page schematic build (thinking_budget forces tool_choice=auto → the model can
return thinking with no tool)."""
from __future__ import annotations

import pytest

from api.pipeline.schemas import RulesSet
from api.pipeline.tool_call import call_with_forced_tool

pytestmark = pytest.mark.asyncio

_VALID_RULESSET = {
    "schema_version": "1.0",
    "rules": [{
        "id": "R-X-001",
        "symptoms": ["no boot"],
        "likely_causes": [{"refdes": "U1", "probability": 0.5, "mechanism": "short"}],
        "diagnostic_steps": [],
        "confidence": 0.5,
        "sources": [],
    }],
}


class _Block:
    def __init__(self, type, name=None, input=None):
        self.type = type
        self.name = name
        self.input = input


class _Usage:
    input_tokens = 10
    output_tokens = 10
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _Resp:
    def __init__(self, content):
        self.content = content
        self.usage = _Usage()


class _Stream:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_final_message(self):
        return self._resp


class _Messages:
    def __init__(self, responses, calls):
        self._responses = responses
        self._calls = calls

    def stream(self, **kwargs):
        self._calls.append(kwargs)
        return _Stream(self._responses[len(self._calls) - 1])


class _Client:
    def __init__(self, responses, calls):
        self.messages = _Messages(responses, calls)


async def test_thinking_miss_drops_thinking_and_forces_tool_on_retry():
    calls: list[dict] = []
    responses = [
        _Resp([_Block("thinking")]),  # attempt 1: thinking-only, no tool_use
        _Resp([_Block("tool_use", name="submit_rules", input=_VALID_RULESSET)]),  # attempt 2
    ]
    client = _Client(responses, calls)

    result = await call_with_forced_tool(
        client=client,
        model="claude-opus-4-8",
        system="sys",
        messages=[{"role": "user", "content": "x"}],
        tools=[{}],
        forced_tool_name="submit_rules",
        output_schema=RulesSet,
        thinking_budget=24000,
        max_attempts=2,
    )

    assert isinstance(result, RulesSet)
    # Attempt 1: thinking on → tool_choice auto.
    assert calls[0]["tool_choice"] == {"type": "auto"}
    assert "thinking" in calls[0]
    # Attempt 2 (after the thinking-miss): thinking dropped → tool forced.
    assert calls[1]["tool_choice"] == {"type": "tool", "name": "submit_rules"}
    assert "thinking" not in calls[1]
