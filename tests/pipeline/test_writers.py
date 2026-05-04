"""Tests for `api.pipeline.writers.run_writers_parallel` orchestration.

`writers.py` is phase 3 of the knowledge factory: 3 LLM writers (Cartographe,
Clinicien, Lexicographe) launch in parallel, sharing a cache-controlled prompt
prefix. Writer 1 dispatches first; an `asyncio.sleep(cache_warmup_seconds)`
gives Anthropic time to materialize the ephemeral cache entry before writers
2 and 3 arrive. These tests pin that contract:

- ordering: Cartographe is dispatched before Clinicien + Lexicographe
- warmup: an `asyncio.sleep` is awaited between writer 1 and writers 2+3
- parallelism: Clinicien + Lexicographe overlap on the event loop
- cache prefix sameness: the ephemeral-cached block is identical across
  the 3 writers (otherwise no cache hit)
- model attribution: the right model is passed to each writer
- failure semantics: an exception in any writer fails the whole gather
- shared tool manifest: every writer sees all 3 submit_* tools

The Anthropic client is mocked at the `call_with_forced_tool` boundary —
no network calls, no real `messages.stream`. Each fake call captures its
kwargs and bumps a monotonic order counter so we can prove sequencing
without sleeping for real wall-clock time.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any
from unittest.mock import MagicMock

import pytest

from api.pipeline import writers as writers_mod
from api.pipeline.schemas import (
    Dictionary,
    KnowledgeGraph,
    Registry,
    RegistryComponent,
    RegistrySignal,
    RulesSet,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def registry() -> Registry:
    """Minimal registry with a couple of entries to give the prefix non-trivial JSON."""
    return Registry(
        device_label="Demo Device",
        components=[
            RegistryComponent(canonical_name="U7", kind="pmic", description="main PMIC"),
            RegistryComponent(canonical_name="C29", kind="capacitor"),
        ],
        signals=[RegistrySignal(canonical_name="3V3_RAIL", kind="power_rail")],
    )


@pytest.fixture
def dummy_outputs():
    """The 3 typed objects the fake `call_with_forced_tool` returns by schema."""
    return {
        KnowledgeGraph: KnowledgeGraph(nodes=[], edges=[]),
        RulesSet: RulesSet(rules=[]),
        Dictionary: Dictionary(entries=[]),
    }


# ---------------------------------------------------------------------------
# Mock factory — captures kwargs + monotonic order on every fake call
# ---------------------------------------------------------------------------


def _make_fake_call(
    captured: list[dict[str, Any]],
    dummy_outputs: dict[type, Any],
    *,
    fail_for_tool: str | None = None,
    fail_exception: Exception | None = None,
):
    """Return an async fake `call_with_forced_tool`.

    Each invocation:
    - records its kwargs + a monotonically increasing `order` index +
      `start` / `end` event-loop timestamps,
    - awaits `asyncio.sleep(0)` so concurrent writers can interleave (so a
      sequential gather would actually serialise on the event loop),
    - returns the right typed dummy object based on `output_schema`,
    - or raises if `fail_for_tool` matches `forced_tool_name`.
    """
    counter = itertools.count(1)

    async def fake_call(*, output_schema, forced_tool_name, **kwargs):
        order = next(counter)
        start = asyncio.get_event_loop().time()
        record = {
            "order": order,
            "start": start,
            "forced_tool_name": forced_tool_name,
            "model": kwargs.get("model"),
            "messages": kwargs.get("messages"),
            "tools": kwargs.get("tools"),
            "system": kwargs.get("system"),
            "log_label": kwargs.get("log_label"),
        }
        captured.append(record)
        # Yield to the loop so concurrent tasks actually overlap (writer 2 + 3).
        await asyncio.sleep(0)
        record["end"] = asyncio.get_event_loop().time()

        if fail_for_tool and forced_tool_name == fail_for_tool:
            raise fail_exception or RuntimeError(f"boom in {forced_tool_name}")

        return dummy_outputs[output_schema]

    return fake_call


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_cartographe_dispatched_before_clinicien_and_lexicographe(
    monkeypatch, registry, dummy_outputs
):
    """Writer 1 (Cartographe) must hit `call_with_forced_tool` before writers 2+3."""
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        writers_mod,
        "call_with_forced_tool",
        _make_fake_call(captured, dummy_outputs),
    )

    await writers_mod.run_writers_parallel(
        client=MagicMock(),
        cartographe_model="opus",
        clinicien_model="opus",
        lexicographe_model="haiku",
        device_label="Demo",
        raw_dump="# dump",
        registry=registry,
        cache_warmup_seconds=0.0,
    )

    by_tool = {c["forced_tool_name"]: c["order"] for c in captured}
    assert by_tool[writers_mod.SUBMIT_KG_TOOL_NAME] == 1, (
        f"Cartographe must dispatch first, got order map: {by_tool}"
    )
    assert by_tool[writers_mod.SUBMIT_RULES_TOOL_NAME] > 1
    assert by_tool[writers_mod.SUBMIT_DICT_TOOL_NAME] > 1


async def test_cache_warmup_sleep_is_awaited_between_writer1_and_writers_2_3(
    monkeypatch, registry, dummy_outputs
):
    """An `asyncio.sleep(cache_warmup_seconds)` must run between W1 dispatch and W2+W3."""
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        writers_mod,
        "call_with_forced_tool",
        _make_fake_call(captured, dummy_outputs),
    )

    real_sleep = asyncio.sleep
    sleep_calls: list[float] = []

    async def spy_sleep(seconds: float):
        sleep_calls.append(seconds)
        # Delegate to the real sleep so ordering / yielding still works.
        await real_sleep(seconds)

    monkeypatch.setattr(writers_mod.asyncio, "sleep", spy_sleep)

    await writers_mod.run_writers_parallel(
        client=MagicMock(),
        cartographe_model="opus",
        clinicien_model="opus",
        lexicographe_model="haiku",
        device_label="Demo",
        raw_dump="# dump",
        registry=registry,
        cache_warmup_seconds=0.5,
    )

    # The warmup value must appear in the awaited sleep durations.
    assert 0.5 in sleep_calls, f"Expected cache_warmup_seconds=0.5 to be awaited, got {sleep_calls}"


async def test_default_cache_warmup_falls_back_to_settings(
    monkeypatch, registry, dummy_outputs
):
    """When cache_warmup_seconds is omitted, the function reads
    `Settings.pipeline_cache_warmup_seconds`. Pinning this prevents the
    drift the previous `1.0` literal default introduced — that value is
    exactly the one the settings comment documents as having caused
    cache misses, so any caller who forgot the kwarg got the worst of
    both worlds.
    """
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        writers_mod,
        "call_with_forced_tool",
        _make_fake_call(captured, dummy_outputs),
    )

    sleep_calls: list[float] = []
    real_sleep = asyncio.sleep

    async def spy_sleep(seconds: float):
        sleep_calls.append(seconds)
        await real_sleep(seconds)

    monkeypatch.setattr(writers_mod.asyncio, "sleep", spy_sleep)
    monkeypatch.setattr(
        writers_mod,
        "get_settings",
        lambda: type("S", (), {"pipeline_cache_warmup_seconds": 0.42})(),
    )

    # cache_warmup_seconds intentionally omitted — must come from settings.
    await writers_mod.run_writers_parallel(
        client=MagicMock(),
        cartographe_model="opus",
        clinicien_model="opus",
        lexicographe_model="haiku",
        device_label="Demo",
        raw_dump="# dump",
        registry=registry,
    )

    assert 0.42 in sleep_calls, (
        f"Expected fallback to settings.pipeline_cache_warmup_seconds=0.42, "
        f"got sleep_calls={sleep_calls}"
    )


async def test_clinicien_and_lexicographe_run_in_parallel(
    monkeypatch, registry, dummy_outputs
):
    """Writers 2 + 3 must overlap — proven by interleaved start/end timestamps.

    With sequential awaits, `start_3 >= end_2`. With true parallelism (via
    `asyncio.create_task` + `gather`), `start_3 < end_2` because they yield to
    each other through `asyncio.sleep(0)`.
    """
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        writers_mod,
        "call_with_forced_tool",
        _make_fake_call(captured, dummy_outputs),
    )

    await writers_mod.run_writers_parallel(
        client=MagicMock(),
        cartographe_model="opus",
        clinicien_model="opus",
        lexicographe_model="haiku",
        device_label="Demo",
        raw_dump="# dump",
        registry=registry,
        cache_warmup_seconds=0.0,
    )

    by_tool = {c["forced_tool_name"]: c for c in captured}
    rules_call = by_tool[writers_mod.SUBMIT_RULES_TOOL_NAME]
    dict_call = by_tool[writers_mod.SUBMIT_DICT_TOOL_NAME]

    # Both writers 2 + 3 must have *started* before either finished.
    assert rules_call["start"] <= dict_call["end"]
    assert dict_call["start"] <= rules_call["end"]


async def test_cached_prefix_block_identical_across_writers(
    monkeypatch, registry, dummy_outputs
):
    """The ephemeral-cached first content block must be byte-identical for all 3 writers.

    Anthropic's prompt cache keys on the prefix; any drift -> cache miss ->
    burned tokens. This is the load-bearing invariant of phase 3's design.
    """
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        writers_mod,
        "call_with_forced_tool",
        _make_fake_call(captured, dummy_outputs),
    )

    await writers_mod.run_writers_parallel(
        client=MagicMock(),
        cartographe_model="opus",
        clinicien_model="opus",
        lexicographe_model="haiku",
        device_label="Demo Device",
        raw_dump="# the raw research dump\n\nSome content.",
        registry=registry,
        cache_warmup_seconds=0.0,
    )

    assert len(captured) == 3
    prefixes = []
    for call in captured:
        msgs = call["messages"]
        first_block = msgs[0]["content"][0]
        # Block-level invariants: ephemeral cache marker on identical text.
        assert first_block["type"] == "text"
        assert first_block.get("cache_control", {}).get("type") == "ephemeral"
        prefixes.append(first_block["text"])

    assert prefixes[0] == prefixes[1] == prefixes[2], (
        "Cached prefix must be byte-identical across writers; otherwise the cache misses."
    )
    # Suffix (task instructions) is the only allowed point of divergence.
    suffixes = [call["messages"][0]["content"][1]["text"] for call in captured]
    assert len(set(suffixes)) == 3, "Each writer must carry a distinct task suffix"


async def test_each_writer_receives_full_tool_manifest(
    monkeypatch, registry, dummy_outputs
):
    """All 3 writers must declare the same 3 submit_* tools (shared tools-layer cache)."""
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        writers_mod,
        "call_with_forced_tool",
        _make_fake_call(captured, dummy_outputs),
    )

    await writers_mod.run_writers_parallel(
        client=MagicMock(),
        cartographe_model="opus",
        clinicien_model="opus",
        lexicographe_model="haiku",
        device_label="Demo",
        raw_dump="# dump",
        registry=registry,
        cache_warmup_seconds=0.0,
    )

    expected_tool_names = {
        writers_mod.SUBMIT_KG_TOOL_NAME,
        writers_mod.SUBMIT_RULES_TOOL_NAME,
        writers_mod.SUBMIT_DICT_TOOL_NAME,
    }
    for call in captured:
        names = {t["name"] for t in call["tools"]}
        assert names == expected_tool_names, (
            f"Writer {call['forced_tool_name']} got tools {names}, expected {expected_tool_names}"
        )


async def test_each_writer_uses_its_assigned_model(
    monkeypatch, registry, dummy_outputs
):
    """Cartographe + Clinicien typically share a model (Opus); Lexicographe runs cheaper."""
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        writers_mod,
        "call_with_forced_tool",
        _make_fake_call(captured, dummy_outputs),
    )

    await writers_mod.run_writers_parallel(
        client=MagicMock(),
        cartographe_model="claude-opus-4-7",
        clinicien_model="claude-opus-4-7",
        lexicographe_model="claude-haiku-4-5",
        device_label="Demo",
        raw_dump="# dump",
        registry=registry,
        cache_warmup_seconds=0.0,
    )

    by_tool = {c["forced_tool_name"]: c["model"] for c in captured}
    assert by_tool[writers_mod.SUBMIT_KG_TOOL_NAME] == "claude-opus-4-7"
    assert by_tool[writers_mod.SUBMIT_RULES_TOOL_NAME] == "claude-opus-4-7"
    assert by_tool[writers_mod.SUBMIT_DICT_TOOL_NAME] == "claude-haiku-4-5"


async def test_writer_failure_propagates_via_gather(
    monkeypatch, registry, dummy_outputs
):
    """`asyncio.gather` is fail-fast by default — a single writer raising must surface.

    The orchestrator depends on this: a malformed writer output must abort
    the phase rather than silently dropping one of the 3 artefacts.
    """
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        writers_mod,
        "call_with_forced_tool",
        _make_fake_call(
            captured,
            dummy_outputs,
            fail_for_tool=writers_mod.SUBMIT_RULES_TOOL_NAME,
            fail_exception=RuntimeError("clinicien validation failed"),
        ),
    )

    with pytest.raises(RuntimeError, match="clinicien validation failed"):
        await writers_mod.run_writers_parallel(
            client=MagicMock(),
            cartographe_model="opus",
            clinicien_model="opus",
            lexicographe_model="haiku",
            device_label="Demo",
            raw_dump="# dump",
            registry=registry,
            cache_warmup_seconds=0.0,
        )


async def test_returns_typed_outputs_in_writer_order(
    monkeypatch, registry, dummy_outputs
):
    """`run_writers_parallel` returns `(KnowledgeGraph, RulesSet, Dictionary)` in order."""
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        writers_mod,
        "call_with_forced_tool",
        _make_fake_call(captured, dummy_outputs),
    )

    kg, rules, dictionary = await writers_mod.run_writers_parallel(
        client=MagicMock(),
        cartographe_model="opus",
        clinicien_model="opus",
        lexicographe_model="haiku",
        device_label="Demo",
        raw_dump="# dump",
        registry=registry,
        cache_warmup_seconds=0.0,
    )

    assert isinstance(kg, KnowledgeGraph)
    assert isinstance(rules, RulesSet)
    assert isinstance(dictionary, Dictionary)


async def test_revision_uses_same_cached_prefix_as_initial_writers(
    monkeypatch, registry, dummy_outputs
):
    """Revision rerun must reuse the exact ephemeral-cached prefix shape, so the
    Auditor-driven self-healing loop still hits the writer cache instead of
    paying the full prefix cost on every round."""
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        writers_mod,
        "call_with_forced_tool",
        _make_fake_call(captured, dummy_outputs),
    )

    await writers_mod.run_single_writer_revision(
        client=MagicMock(),
        cartographe_model="opus",
        clinicien_model="opus",
        lexicographe_model="haiku",
        device_label="Demo Device",
        raw_dump="# the raw research dump\n\nSome content.",
        registry=registry,
        file_name="rules",
        revision_brief="Add a missing 3V3 rule",
        previous_output_json="{}",
    )

    assert len(captured) == 1
    msg = captured[0]["messages"][0]
    first_block = msg["content"][0]
    assert first_block["type"] == "text"
    assert first_block.get("cache_control", {}).get("type") == "ephemeral"
    # Same shape as run_writers_parallel: 2 blocks, [cached prefix, task suffix].
    assert len(msg["content"]) == 2
    # The forced tool must be the rules submitter (writer 2 surface).
    assert captured[0]["forced_tool_name"] == writers_mod.SUBMIT_RULES_TOOL_NAME
