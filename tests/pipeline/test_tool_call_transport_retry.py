"""call_with_forced_tool: transient TRANSPORT errors must be retried in place.

Live 2026-06-10 build: ONE `httpx.RemoteProtocolError: peer closed connection
without sending complete message body` on a page_vision stream killed the whole
schematic ingestion (`asyncio.gather`) → the pipeline ran graph-less and the
audit never converged. The SDK's max_retries only covers the initial request,
NOT a mid-stream disconnection — so the stream call itself needs a small
transport-retry budget. These retries must NOT consume a validation attempt and
must NOT pollute the system prompt with the validation-retry suffix.
"""
from __future__ import annotations

import anthropic
import httpx
import pytest

from api.pipeline import tool_call
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
    """Yields the queued outcome: an exception instance is RAISED mid-stream
    (from get_final_message — where a peer-closed surfaces), else returned."""

    def __init__(self, outcome):
        self._outcome = outcome

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_final_message(self):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class _Messages:
    def __init__(self, outcomes, calls):
        self._outcomes = outcomes
        self._calls = calls

    def stream(self, **kwargs):
        self._calls.append(kwargs)
        return _Stream(self._outcomes[len(self._calls) - 1])


class _Client:
    def __init__(self, outcomes, calls):
        self.messages = _Messages(outcomes, calls)


def _ok_response():
    return _Resp([_Block("tool_use", name="emit_rules", input=_VALID_RULESSET)])


def _args(client):
    return dict(
        client=client,
        model="claude-opus-4-8",
        system="sys",
        messages=[{"role": "user", "content": "go"}],
        tools=[{"name": "emit_rules"}],
        forced_tool_name="emit_rules",
        output_schema=RulesSet,
        log_label="test",
    )


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(tool_call, "_TRANSPORT_BACKOFF_S", (0.0, 0.0))


async def test_midstream_disconnect_is_retried_and_succeeds():
    calls: list[dict] = []
    boom = httpx.RemoteProtocolError("peer closed connection without sending complete message body")
    client = _Client([boom, _ok_response()], calls)

    result = await call_with_forced_tool(**_args(client))

    assert result.rules[0].id == "R-X-001"
    assert len(calls) == 2
    # A transport blip is NOT a validation failure: the second call's prompt is
    # byte-identical (no "PREVIOUS ATTEMPT FAILED VALIDATION" suffix).
    assert calls[1]["system"] == calls[0]["system"] == "sys"


async def test_api_connection_error_is_retried():
    calls: list[dict] = []
    boom = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    client = _Client([boom, _ok_response()], calls)

    result = await call_with_forced_tool(**_args(client))
    assert result.rules[0].id == "R-X-001"
    assert len(calls) == 2


async def test_transport_retries_exhausted_raises_the_transport_error():
    calls: list[dict] = []
    boom = httpx.RemoteProtocolError("peer closed connection")
    client = _Client([boom, boom, boom, boom], calls)

    with pytest.raises(httpx.RemoteProtocolError):
        await call_with_forced_tool(**_args(client))
    assert len(calls) == tool_call._TRANSPORT_TRIES


async def test_non_transient_errors_are_not_retried():
    calls: list[dict] = []
    boom = anthropic.BadRequestError(
        "bad request",
        response=httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com")),
        body=None,
    )
    client = _Client([boom, _ok_response()], calls)

    with pytest.raises(anthropic.BadRequestError):
        await call_with_forced_tool(**_args(client))
    assert len(calls) == 1  # a 400 is deterministic — retrying it just burns time
