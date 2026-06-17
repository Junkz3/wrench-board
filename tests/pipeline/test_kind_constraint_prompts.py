"""Task 6 — Scout + Registry inject a device-kind constraint into their prompts.

These tests assert that the authoritative one-line constraint produced by
`device_kind_constraint(device_kind)` reaches the model-bound prompt:

- Scout: the user prompt assembled in `_build_user_prompt` and sent to
  `client.messages.create` must carry the constraint. We patch the single
  inner LLM seam (`AsyncAnthropic.messages.create`) and inspect its args.
- Registry: the `user_prompt` passed in `messages` to `call_with_forced_tool`
  must carry the constraint. We patch that seam and inspect its kwargs.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.pipeline.prompts import device_kind_constraint
from api.pipeline.registry import run_registry_builder
from api.pipeline.schemas import Registry
from api.pipeline.scout import run_scout


def test_device_kind_constraint_helper():
    """The helper emits a constraint for a known kind, '' for unknown/None."""
    assert device_kind_constraint("gpu_card").strip()
    assert "gpu_card" in device_kind_constraint("gpu_card")
    assert "discrete GPU graphics card" in device_kind_constraint("gpu_card")
    assert device_kind_constraint("unknown") == ""
    assert device_kind_constraint(None) == ""


def _fake_end_turn_response(text: str = "# Research Dump"):
    """Minimal AsyncAnthropic response: one text block, end_turn, zero usage."""
    block = SimpleNamespace(type="text", text=text)
    usage = SimpleNamespace(
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(content=[block], stop_reason="end_turn", usage=usage)


@pytest.mark.asyncio
async def test_scout_injects_device_kind_constraint():
    create = AsyncMock(return_value=_fake_end_turn_response())
    fake_client = SimpleNamespace(messages=SimpleNamespace(create=create))

    # Thresholds at 0 → the (thin) dump passes assess_dump, run_scout returns
    # cleanly after a single LLM call.
    dump = await run_scout(
        client=fake_client,
        model="m",
        device_label="MSI V311_11",
        device_kind="gpu_card",
        max_continuations=0,
        min_symptoms=0,
        min_components=0,
        min_sources=0,
        max_retries=0,
    )

    assert "# Research Dump" in dump
    create.assert_awaited()
    sent = str(create.await_args.kwargs.get("messages"))
    assert "gpu_card" in sent
    assert "discrete GPU graphics card" in sent


@pytest.mark.asyncio
async def test_registry_injects_device_kind_constraint():
    fake = Registry(device_label="MSI V311_11")
    with patch(
        "api.pipeline.registry.call_with_forced_tool",
        new=AsyncMock(return_value=fake),
    ) as m:
        await run_registry_builder(
            client=object(),
            model="m",
            device_label="MSI V311_11",
            raw_dump="d",
            device_kind="gpu_card",
        )

    sent = str(m.await_args.kwargs.get("messages"))
    assert "gpu_card" in sent
    assert "discrete GPU graphics card" in sent
