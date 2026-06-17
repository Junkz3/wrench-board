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
from api.pipeline.graph_truth import GraphTruth
from api.pipeline.schemas import (
    ComponentSheet,
    Dictionary,
    DictionaryPatch,
    KnowledgeGraph,
    KnowledgeGraphPatch,
    KnowledgeNode,
    Registry,
    RegistryComponent,
    RegistrySignal,
    RulesPatch,
    RulesSet,
)
from api.pipeline.schematic.schemas import (
    ComponentNode,
    ElectricalGraph,
    SchematicQualityReport,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def registry() -> Registry:
    """Minimal registry with a couple of entries to give the prefix non-trivial JSON."""
    # T8 : kind en majuscules (PMIC, CAPACITOR, POWER_RAIL)
    return Registry(
        device_label="Demo Device",
        components=[
            RegistryComponent(canonical_name="U7", kind="PMIC", description="main PMIC"),
            RegistryComponent(canonical_name="C29", kind="CAPACITOR"),
        ],
        signals=[RegistrySignal(canonical_name="3V3_RAIL", kind="POWER_RAIL")],
    )


@pytest.fixture
def dummy_outputs():
    """The typed objects the fake `call_with_forced_tool` returns by schema.

    Initial writers submit a full artefact; revisers submit a PATCH. Both shapes
    are covered so a fake call keyed on `output_schema` resolves either path.
    """
    return {
        KnowledgeGraph: KnowledgeGraph(nodes=[], edges=[]),
        RulesSet: RulesSet(rules=[]),
        Dictionary: Dictionary(entries=[]),
        KnowledgeGraphPatch: KnowledgeGraphPatch(),
        RulesPatch: RulesPatch(),
        DictionaryPatch: DictionaryPatch(),
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


async def test_emits_phase_step_as_each_writer_completes(
    monkeypatch, registry, dummy_outputs
):
    """run_writers_parallel emits one `phase_step writer_done` per writer.

    The landing UI renders these live ("graphe ✓", "règles ✓", "dico ✓") as
    each of the 3 writers returns. The event carries the artifact's count so
    the line can read "graphe ✓ 142 nœuds".
    """
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        writers_mod,
        "call_with_forced_tool",
        _make_fake_call(captured, dummy_outputs),
    )

    steps: list[dict[str, Any]] = []

    async def collect(ev: dict[str, Any]) -> None:
        steps.append(ev)

    await writers_mod.run_writers_parallel(
        client=MagicMock(),
        cartographe_model="opus",
        clinicien_model="opus",
        lexicographe_model="haiku",
        device_label="Demo",
        raw_dump="# dump",
        registry=registry,
        cache_warmup_seconds=0.0,
        on_event=collect,
    )

    writer_steps = [
        e for e in steps
        if e.get("type") == "phase_step" and e.get("step") == "writer_done"
    ]
    assert {e["writer"] for e in writer_steps} == {"graph", "rules", "dict"}
    assert all(e["phase"] == "writers" for e in writer_steps)
    assert all("count" in e for e in writer_steps)


async def test_writers_run_without_on_event(monkeypatch, registry, dummy_outputs):
    """on_event is optional — omitting it must not crash the writers phase."""
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
    assert kg is not None and rules is not None and dictionary is not None


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
        cartographe_model="claude-opus-4-8",
        clinicien_model="claude-opus-4-8",
        lexicographe_model="claude-haiku-4-5",
        device_label="Demo",
        raw_dump="# dump",
        registry=registry,
        cache_warmup_seconds=0.0,
    )

    by_tool = {c["forced_tool_name"]: c["model"] for c in captured}
    assert by_tool[writers_mod.SUBMIT_KG_TOOL_NAME] == "claude-opus-4-8"
    assert by_tool[writers_mod.SUBMIT_RULES_TOOL_NAME] == "claude-opus-4-8"
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
        # current_kg/current_rules/current_dictionary are now REQUIRED (RC1 fix):
        # the reviser must see the up-to-date sibling files.
        current_kg=KnowledgeGraph(nodes=[], edges=[]),
        current_rules=RulesSet(rules=[]),
        current_dictionary=Dictionary(entries=[]),
    )

    assert len(captured) == 1
    msg = captured[0]["messages"][0]
    first_block = msg["content"][0]
    assert first_block["type"] == "text"
    assert first_block.get("cache_control", {}).get("type") == "ephemeral"
    # Same shape as run_writers_parallel: 2 blocks, [cached prefix, task suffix].
    assert len(msg["content"]) == 2
    # The forced tool must be the rules PATCH submitter (writer 2 revise surface).
    assert captured[0]["forced_tool_name"] == writers_mod.SUBMIT_RULES_PATCH_TOOL_NAME


# ---------------------------------------------------------------------------
# RC1 — revisers see the CURRENT siblings + ground truth, fixed kg→rules→dict order
# ---------------------------------------------------------------------------


def _mini_graph() -> ElectricalGraph:
    """A tiny compiled graph so the query-loop dispatch has a real GraphTruth."""
    return ElectricalGraph(
        device_slug="mini",
        components={
            "U7": ComponentNode(refdes="U7", type="ic", kind="ic", role="pmic", pages=[1]),
        },
        nets={},
        power_rails={},
        typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )


async def test_reviser_sees_current_siblings(monkeypatch, registry, dummy_outputs):
    """Revising `rules` must surface the CURRENT kg + dictionary as read-only
    sibling sections — and NOT a section for `rules` itself (that's the baseline).

    This is the RC1 fix: a reviser that only ever saw its own previous output
    re-aligned cross-file references against stale siblings. The captured request
    suffix must carry distinctive marker strings from both current siblings."""
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        writers_mod,
        "call_with_forced_tool",
        _make_fake_call(captured, dummy_outputs),
    )

    current_kg = KnowledgeGraph(
        nodes=[KnowledgeNode(id="N-U7", kind="component", label="KG_MARKER_SIBLING U7")],
        edges=[],
    )
    current_dictionary = Dictionary(
        entries=[ComponentSheet(canonical_name="U7", notes="DICT_MARKER_SIBLING note")],
    )

    await writers_mod.run_single_writer_revision(
        client=MagicMock(),
        cartographe_model="opus",
        clinicien_model="opus",
        lexicographe_model="haiku",
        device_label="Demo Device",
        raw_dump="# dump",
        registry=registry,
        file_name="rules",
        revision_brief="Add a missing 3V3 rule",
        previous_output_json="{}",
        current_kg=current_kg,
        current_rules=RulesSet(rules=[]),
        current_dictionary=current_dictionary,
    )

    assert len(captured) == 1
    # The siblings live in the revision SUFFIX (2nd content block) — the cached
    # prefix stays identical for the writer cache.
    suffix = captured[0]["messages"][0]["content"][1]["text"]
    assert "# Current sibling files" in suffix
    assert "## knowledge_graph (current)" in suffix
    assert "## dictionary (current)" in suffix
    assert "KG_MARKER_SIBLING" in suffix
    assert "DICT_MARKER_SIBLING" in suffix
    # The reviser's OWN file must NOT appear as a sibling — it's the baseline.
    assert "## rules (current)" not in suffix


async def test_apply_revisions_fixed_order_and_threading(registry):
    """`_apply_revisions` revises in FIXED kg→rules→dict order (NOT the auditor's
    `files_to_rewrite` order) AND threads the freshly-revised kg into the rules
    reviser as `current_kg` — the two halves of the RC1 fix."""
    from api.pipeline.orchestrator import _apply_revisions
    from api.pipeline.schemas import AuditVerdict

    # Distinct marker objects so we can prove the THREADED (revised) kg — not the
    # original — reaches the rules reviser.
    original_kg = KnowledgeGraph(
        nodes=[KnowledgeNode(id="N-ORIG", kind="component", label="ORIGINAL")], edges=[]
    )
    revised_kg = KnowledgeGraph(
        nodes=[KnowledgeNode(id="N-NEW", kind="component", label="REVISED")], edges=[]
    )

    # Marker pour prouver la 2e maille de la chaîne (rules → dictionary) :
    # le réviseur dictionary doit recevoir le RulesSet fraîchement révisé.
    revised_rules = RulesSet(rules=[])

    call_order: list[str] = []
    received_current_kg: dict[str, KnowledgeGraph] = {}
    received_current_rules: dict[str, RulesSet] = {}

    async def fake_revision(*, file_name, current_kg, current_rules, **kwargs):
        call_order.append(file_name)
        received_current_kg[file_name] = current_kg
        received_current_rules[file_name] = current_rules
        if file_name == "knowledge_graph":
            return revised_kg
        if file_name == "rules":
            return revised_rules
        return Dictionary(entries=[])

    import api.pipeline.orchestrator as orch_mod

    orig = orch_mod.run_single_writer_revision
    orch_mod.run_single_writer_revision = fake_revision  # type: ignore[assignment]
    try:
        # Auditor asks in a DELIBERATELY scrambled order — must be ignored.
        verdict = AuditVerdict(
            overall_status="NEEDS_REVISION",
            consistency_score=0.5,
            drift_report=[],
            files_to_rewrite=["rules", "knowledge_graph", "dictionary"],
            revision_brief="fix things",
        )
        await _apply_revisions(
            client=MagicMock(),
            cartographe_model="opus",
            clinicien_model="opus",
            lexicographe_model="haiku",
            device_label="Demo",
            raw_dump="# dump",
            registry=registry,
            verdict=verdict,
            current_kg=original_kg,
            current_rules=RulesSet(rules=[]),
            current_dictionary=Dictionary(entries=[]),
        )
    finally:
        orch_mod.run_single_writer_revision = orig  # type: ignore[assignment]

    # FIXED order regardless of the auditor's scrambled request.
    assert call_order == ["knowledge_graph", "rules", "dictionary"]
    # Threading: the rules reviser saw the FRESHLY revised kg, not the original.
    assert received_current_kg["rules"] is revised_kg
    assert received_current_kg["dictionary"] is revised_kg
    # ... et le réviseur dictionary a reçu le rules FRAÎCHEMENT révisé (chaîne
    # complète kg → rules → dictionary, pas seulement la 1re maille).
    assert received_current_rules["dictionary"] is revised_rules


async def test_reviser_tool_only_with_graph(monkeypatch, registry, dummy_outputs):
    """Dispatch mirrors the auditor: `graph_truth=None` → a single forced call
    offering ONLY the role's submit tool; a GraphTruth → the query loop offering
    `query_graph` + the role's submit tool."""
    # --- no graph → forced, submit-tool-only ---
    forced_captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        writers_mod,
        "call_with_forced_tool",
        _make_fake_call(forced_captured, dummy_outputs),
    )

    await writers_mod.run_single_writer_revision(
        client=MagicMock(),
        cartographe_model="opus",
        clinicien_model="opus",
        lexicographe_model="haiku",
        device_label="Demo",
        raw_dump="# dump",
        registry=registry,
        file_name="rules",
        revision_brief="fix",
        previous_output_json="{}",
        current_kg=KnowledgeGraph(nodes=[], edges=[]),
        current_rules=RulesSet(rules=[]),
        current_dictionary=Dictionary(entries=[]),
        graph_truth=None,
    )

    assert len(forced_captured) == 1
    tool_names = {t["name"] for t in forced_captured[0]["tools"]}
    assert tool_names == {writers_mod.SUBMIT_RULES_PATCH_TOOL_NAME}

    # --- graph present → query loop, query_graph + submit tool ---
    query_captured: dict[str, Any] = {}

    async def fake_query(*, query_tool, submit_tool, submit_tool_name, output_schema, **kw):
        query_captured["tools"] = [query_tool, submit_tool]
        return dummy_outputs[output_schema]

    monkeypatch.setattr(writers_mod, "call_with_query_tools", fake_query)

    await writers_mod.run_single_writer_revision(
        client=MagicMock(),
        cartographe_model="opus",
        clinicien_model="opus",
        lexicographe_model="haiku",
        device_label="Demo",
        raw_dump="# dump",
        registry=registry,
        file_name="rules",
        revision_brief="fix",
        previous_output_json="{}",
        current_kg=KnowledgeGraph(nodes=[], edges=[]),
        current_rules=RulesSet(rules=[]),
        current_dictionary=Dictionary(entries=[]),
        ground_truth_report="(report)",
        graph_truth=GraphTruth(_mini_graph()),
    )

    names = {t["name"] for t in query_captured["tools"]}
    assert names == {"query_graph", writers_mod.SUBMIT_RULES_PATCH_TOOL_NAME}


async def test_reviser_applies_patch_and_preserves_unflagged_records(monkeypatch, registry):
    """The reviser emits a PATCH; `run_single_writer_revision` applies it and
    returns the resulting artefact. The records the patch does NOT name come out
    byte-identical — the whole point of the surgical reviser."""
    from api.pipeline.schemas import KnowledgeEdge

    current_kg = KnowledgeGraph(
        nodes=[
            KnowledgeNode(id="N-U1", kind="component", label="U1"),
            KnowledgeNode(id="N-RAIL", kind="net", label="3V3"),
            KnowledgeNode(id="N-ORPHAN", kind="component", label="orphan"),
        ],
        edges=[KnowledgeEdge(source_id="N-U1", target_id="N-RAIL", relation="powers")],
    )
    before = {n.id: n.model_dump_json() for n in current_kg.nodes}

    # The fake reviser connects the orphan via a single add_edges op.
    async def fake_call(*, output_schema, **kwargs):
        assert output_schema is KnowledgeGraphPatch
        return KnowledgeGraphPatch(
            add_edges=[KnowledgeEdge(source_id="N-ORPHAN", target_id="N-RAIL", relation="powers")]
        )

    monkeypatch.setattr(writers_mod, "call_with_forced_tool", fake_call)

    result = await writers_mod.run_single_writer_revision(
        client=MagicMock(),
        cartographe_model="opus",
        clinicien_model="opus",
        lexicographe_model="haiku",
        device_label="Demo",
        raw_dump="# dump",
        registry=registry,
        file_name="knowledge_graph",
        revision_brief="Connect the orphan node",
        previous_output_json=current_kg.model_dump_json(),
        current_kg=current_kg,
        current_rules=RulesSet(rules=[]),
        current_dictionary=Dictionary(entries=[]),
    )

    assert isinstance(result, KnowledgeGraph)
    # Every node preserved verbatim; the orphan is now connected.
    assert {n.id: n.model_dump_json() for n in result.nodes} == before
    assert len(result.edges) == 2


async def test_reviser_inapplicable_patch_degrades_to_noop(monkeypatch, registry):
    """A well-formed-but-inapplicable patch (here: add of an id that already
    exists) is NOT fatal — the reviser returns the current artefact unchanged so
    the re-audit re-flags. Nothing corrupts."""
    current_kg = KnowledgeGraph(
        nodes=[KnowledgeNode(id="N-U1", kind="component", label="U1")], edges=[]
    )

    async def fake_call(*, output_schema, **kwargs):
        # Adds a node that already exists → PatchApplyError inside the applicator.
        return KnowledgeGraphPatch(
            add_nodes=[KnowledgeNode(id="N-U1", kind="component", label="dupe")]
        )

    monkeypatch.setattr(writers_mod, "call_with_forced_tool", fake_call)

    result = await writers_mod.run_single_writer_revision(
        client=MagicMock(),
        cartographe_model="opus",
        clinicien_model="opus",
        lexicographe_model="haiku",
        device_label="Demo",
        raw_dump="# dump",
        registry=registry,
        file_name="knowledge_graph",
        revision_brief="...",
        previous_output_json=current_kg.model_dump_json(),
        current_kg=current_kg,
        current_rules=RulesSet(rules=[]),
        current_dictionary=Dictionary(entries=[]),
    )

    # Unchanged — the inapplicable patch was a no-op, not a crash.
    assert result is current_kg


def test_clinicien_task_id_example_matches_pattern():
    """Le prompt ne doit pas enseigner un format d'id qui ne valide que grâce à
    la normalisation _normalize_id (RULE-/rule- → R-) : l'exemple doit être
    directement canonique."""
    import re as _re

    from api.pipeline.prompts import CLINICIEN_TASK
    from api.pipeline.schemas import _RULE_ID_PATTERN

    examples = _re.findall(r"'(rule-[^']*|R-[^']*)'", CLINICIEN_TASK)
    assert examples, "id example missing from CLINICIEN_TASK"
    for ex in examples:
        assert _re.fullmatch(_RULE_ID_PATTERN, ex), ex


async def test_apply_revisions_collects_reviser_stats():
    """Chaque appel réviseur doit verser un PhaseTokenStats dans le sink — sans
    ça, les tours query_graph des réviseurs (jusqu'à max_query_turns appels Opus
    par fichier) disparaissent de token_stats.json et du total facturable."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from api.pipeline.orchestrator import _apply_revisions
    from api.pipeline.schemas import AuditVerdict

    registry = Registry(device_label="Demo", components=[], signals=[])
    verdict = AuditVerdict(
        overall_status="NEEDS_REVISION",
        consistency_score=0.5,
        drift_report=[],
        files_to_rewrite=["knowledge_graph", "rules"],
        revision_brief="fix",
    )
    sink = []
    fake = AsyncMock(side_effect=[KnowledgeGraph(nodes=[], edges=[]), RulesSet(rules=[])])
    with patch("api.pipeline.orchestrator.run_single_writer_revision", new=fake):
        await _apply_revisions(
            client=MagicMock(),
            cartographe_model="opus",
            clinicien_model="opus",
            lexicographe_model="haiku",
            device_label="Demo",
            raw_dump="# dump",
            registry=registry,
            verdict=verdict,
            current_kg=KnowledgeGraph(nodes=[], edges=[]),
            current_rules=RulesSet(rules=[]),
            current_dictionary=Dictionary(entries=[]),
            stats_sink=sink,
            round_index=2,
        )
    assert [st.phase for st in sink] == [
        "reviser_knowledge_graph_round_2",
        "reviser_rules_round_2",
    ]
    # ... et chaque stats est bien passé à l'appel réviseur correspondant.
    assert [c.kwargs["stats"] for c in fake.await_args_list] == sink
