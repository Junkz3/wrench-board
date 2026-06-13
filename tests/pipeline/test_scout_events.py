"""Tests for Scout's live `phase_step` emission (search-round sub-steps).

Scout runs web_search in a `for iteration in range(max_continuations + 1)`
loop of blocking `messages.create` calls. Each round emits a `phase_step
search_round` so the landing timeline can show "recherche web · tour N"
instead of a silent spinner. The Anthropic client is faked at the
`messages.create` boundary — no network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from api.pipeline import scout


def _fake_response(text: str) -> SimpleNamespace:
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
        return _fake_response("STM32 found. Symptom: dead screen. Source: http://x")


class _FakeClient:
    messages = _FakeMessages()


@pytest.mark.asyncio
async def test_scout_emits_search_round_step():
    steps: list[dict[str, Any]] = []

    async def collect(ev: dict[str, Any]) -> None:
        steps.append(ev)

    await scout.run_scout(
        client=_FakeClient(),
        model="claude-sonnet-4-6",
        device_label="Demo",
        max_continuations=0,
        min_symptoms=0,
        min_components=0,
        min_sources=0,
        max_retries=0,
        on_event=collect,
    )

    round_steps = [
        e for e in steps
        if e.get("type") == "phase_step" and e.get("step") == "search_round"
    ]
    assert len(round_steps) == 1
    assert round_steps[0]["phase"] == "scout"
    assert round_steps[0]["index"] == 1


@pytest.mark.asyncio
async def test_scout_runs_without_on_event():
    """on_event is optional — Scout must not crash when it's omitted."""
    dump = await scout.run_scout(
        client=_FakeClient(),
        model="claude-sonnet-4-6",
        device_label="Demo",
        max_continuations=0,
        min_symptoms=0,
        min_components=0,
        min_sources=0,
        max_retries=0,
    )
    assert isinstance(dump, str) and dump
