from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from api.pipeline.board_delta.agent import generate_board_delta
from api.pipeline.board_delta.schemas import DeltaBoard


# ---------------------------------------------------------------------------
# Fake Anthropic client for Phase A (web_search research)
# ---------------------------------------------------------------------------

_RESEARCH_TEXT = "ISL9240 charger found on 820-02016. Source: http://example.com/repair"


def _fake_research_response(text: str = _RESEARCH_TEXT) -> SimpleNamespace:
    """Minimal stand-in for an Anthropic Message: end_turn, one text block."""
    block = SimpleNamespace(type="text", text=text)
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(stop_reason="end_turn", content=[block], usage=usage)


class _FakeMessages:
    async def create(self, **_: Any) -> SimpleNamespace:
        return _fake_research_response()


class _FakeClient:
    messages = _FakeMessages()


# ---------------------------------------------------------------------------
# Fake call_with_forced_tool for Phase B (structuring)
# ---------------------------------------------------------------------------


class _FakeForced:
    """Stands in for call_with_forced_tool: returns a fixed DeltaBoard and
    records all keyword arguments passed to it."""

    def __init__(self, delta: DeltaBoard) -> None:
        self.delta = delta
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> DeltaBoard:
        self.calls.append(kwargs)
        return self.delta


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_validated_delta_and_stamps_keys(monkeypatch):
    fixed = DeltaBoard(
        device_label="MacBook Air M1",
        board_number="raw-input",
        coverage="rich",
        signature_ics=[{"part": "ISL9240", "role": "charger", "source_url": "http://x"}],
    )
    fake = _FakeForced(fixed)
    monkeypatch.setattr("api.pipeline.board_delta.agent.call_with_forced_tool", fake)

    out = await generate_board_delta(
        client=_FakeClient(),
        model="claude-sonnet-4-6",
        device_label="MacBook Air M1",
        board_number="820-02016",
    )
    assert out.board_number == "820-02016"
    assert out.device_label == "MacBook Air M1"
    assert out.coverage == "rich"


@pytest.mark.asyncio
async def test_empty_model_output_is_forced_to_none(monkeypatch):
    bogus = DeltaBoard(device_label="x", board_number="x", coverage="rich")
    monkeypatch.setattr(
        "api.pipeline.board_delta.agent.call_with_forced_tool", _FakeForced(bogus)
    )
    out = await generate_board_delta(
        client=_FakeClient(),
        model="m",
        device_label="Obscure",
        board_number="zz-001",
    )
    assert out.coverage == "none"
    assert out.is_empty()


@pytest.mark.asyncio
async def test_emit_board_delta_tool_defined_and_passed(monkeypatch):
    """Phase B must pass a non-empty tools list containing emit_board_delta.

    Regression for the bug where tools=[] was forwarded verbatim — the
    Anthropic API returns 400 when tool_choice forces a tool that isn't defined
    in the tools list.
    """
    fixed = DeltaBoard(
        device_label="MacBook Air M1",
        board_number="820-02016",
        coverage="rich",
        signature_ics=[{"part": "ISL9240", "role": "charger", "source_url": "http://x"}],
    )
    fake = _FakeForced(fixed)
    monkeypatch.setattr("api.pipeline.board_delta.agent.call_with_forced_tool", fake)

    await generate_board_delta(
        client=_FakeClient(),
        model="claude-sonnet-4-6",
        device_label="MacBook Air M1",
        board_number="820-02016",
    )

    assert len(fake.calls) == 1, "call_with_forced_tool should be called exactly once"
    call_kwargs = fake.calls[0]
    tools = call_kwargs.get("tools", [])
    assert tools, "tools must be non-empty — empty list causes 400 from the Anthropic API"
    emit_tool = next((t for t in tools if t.get("name") == "emit_board_delta"), None)
    assert emit_tool is not None, "tools must contain a tool named 'emit_board_delta'"
    assert emit_tool.get("input_schema"), "emit_board_delta tool must have a non-empty input_schema"


@pytest.mark.asyncio
async def test_research_text_passed_to_forced_tool(monkeypatch):
    """Phase A research text must be the user content fed to Phase B."""
    fixed = DeltaBoard(
        device_label="MacBook Air M1",
        board_number="raw-input",
        coverage="thin",
        signature_ics=[{"part": "CD3217B12", "role": "USB-C CC controller", "source_url": "http://y"}],
    )
    fake = _FakeForced(fixed)
    monkeypatch.setattr("api.pipeline.board_delta.agent.call_with_forced_tool", fake)

    await generate_board_delta(
        client=_FakeClient(),
        model="claude-sonnet-4-6",
        device_label="MacBook Air M1",
        board_number="820-02016",
    )

    assert len(fake.calls) == 1, "call_with_forced_tool should be called exactly once"
    msgs = fake.calls[0]["messages"]
    # The research text from Phase A must appear as the user message content
    assert any(
        _RESEARCH_TEXT in (m.get("content", "") if isinstance(m.get("content"), str) else "")
        for m in msgs
    ), f"Research text not found in messages passed to call_with_forced_tool: {msgs}"
