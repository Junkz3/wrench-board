"""LLM-path tests for passive_classifier — fully mocked, no Anthropic calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from api.pipeline.schematic import passive_classifier
from api.pipeline.schematic.passive_classifier import (
    PassiveAssignment,
    PassiveClassification,
    classify_passives,
    classify_passives_llm,
)
from api.pipeline.schematic.schemas import (
    ComponentNode,
    ElectricalGraph,
    NetNode,
    PagePin,
    PowerRail,
    SchematicQualityReport,
)


def _graph_with_ambiguous_passives() -> ElectricalGraph:
    """Graph where the heuristic leaves 2 passives as role=None.

    - C100 on two signal nets (non-rail, non-GND) → heuristic gives
      ac_coupling (0.55). We DON'T return None from the heuristic for
      this case; it's used to verify the LLM output is IGNORED when the
      heuristic already has a role.
    - R77 between two unlabelled signal nets, no consumer IC
      → heuristic gives `damping` (0.4). Same as above.
    - R200 with only one pin labelled → heuristic returns None. LLM
      will fill this one.
    """
    comps = {
        "C100": ComponentNode(
            refdes="C100", type="capacitor",
            pins=[
                PagePin(number="1", net_label="SIG_A"),
                PagePin(number="2", net_label="SIG_B"),
            ],
        ),
        "R77": ComponentNode(
            refdes="R77", type="resistor",
            pins=[
                PagePin(number="1", net_label="SIG_X"),
                PagePin(number="2", net_label="SIG_Y"),
            ],
        ),
        "R200": ComponentNode(
            refdes="R200", type="resistor",
            pins=[PagePin(number="1", net_label="SOME_NET")],  # only 1 pin
        ),
    }
    nets = {
        "SIG_A": NetNode(label="SIG_A"),
        "SIG_B": NetNode(label="SIG_B"),
        "SIG_X": NetNode(label="SIG_X"),
        "SIG_Y": NetNode(label="SIG_Y"),
        "SOME_NET": NetNode(label="SOME_NET"),
    }
    return ElectricalGraph(
        device_slug="llm-merge-test",
        components=comps,
        nets=nets,
        power_rails={},
        typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )


def _fake_llm_output(device_slug: str, filled: list[tuple[str, str, str]]) -> PassiveClassification:
    """Build a PassiveClassification the LLM would return for the mocked batch."""
    return PassiveClassification(
        device_slug=device_slug,
        assignments=[
            PassiveAssignment(refdes=r, kind=k, role=role, confidence=0.8)
            for r, k, role in filled
        ],
        ambiguities=[],
        model_used="placeholder-overridden-downstream",
    )


# ----------------------------------------------------------------------
# classify_passives_llm — merge behaviour
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_fills_only_heuristic_none_roles():
    """LLM output must not override a role the heuristic already decided."""
    graph = _graph_with_ambiguous_passives()
    # Heuristic: C100 → ac_coupling, R77 → damping, R200 → None (single pin).
    # Mock LLM: tries to reclassify C100 as "tank" and R200 as "pull_up".
    mock_output = _fake_llm_output(
        graph.device_slug,
        [
            ("C100", "passive_c", "tank"),     # heuristic already has ac_coupling → ignored
            ("R200", "passive_r", "pull_up"),  # heuristic is None → this fills
        ],
    )
    with patch.object(
        passive_classifier, "call_with_forced_tool",
        new=AsyncMock(return_value=mock_output),
    ) as mocked:
        result = await classify_passives_llm(graph, client=object(), model="claude-sonnet-4-6")
    # C100 keeps heuristic role.
    assert result["C100"][1] == "ac_coupling"
    # R200 filled by LLM.
    assert result["R200"][1] == "pull_up"
    assert result["R200"][2] == 0.8
    # Only one batch fired (3 passives < _BATCH_SIZE).
    assert mocked.await_count == 1


@pytest.mark.asyncio
async def test_llm_failure_returns_heuristic_baseline():
    """Any exception in the LLM path returns the heuristic output, never raises."""
    graph = _graph_with_ambiguous_passives()
    with patch.object(
        passive_classifier, "call_with_forced_tool",
        new=AsyncMock(side_effect=RuntimeError("anthropic unreachable")),
    ):
        result = await classify_passives_llm(graph, client=object(), model="claude-sonnet-4-6")
    # Heuristic baseline intact — C100 still ac_coupling, R200 still None.
    assert result["C100"][1] == "ac_coupling"
    assert result["R200"][1] is None


@pytest.mark.asyncio
async def test_llm_skipped_when_heuristic_covers_all():
    """If the heuristic classified every passive, no LLM call is made."""
    # Build a graph where every passive has a rail pin and GND pin so the
    # heuristic decoupling rule matches.
    comps = {
        "C1": ComponentNode(
            refdes="C1", type="capacitor",
            pins=[
                PagePin(number="1", net_label="+3V3"),
                PagePin(number="2", net_label="GND"),
            ],
        ),
    }
    graph = ElectricalGraph(
        device_slug="all-covered-test",
        components=comps,
        nets={
            "+3V3": NetNode(label="+3V3", is_power=True),
            "GND": NetNode(label="GND"),
        },
        power_rails={"+3V3": PowerRail(label="+3V3", consumers=["C1"])},
        typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    mocked = AsyncMock()
    with patch.object(passive_classifier, "call_with_forced_tool", new=mocked):
        result = await classify_passives_llm(graph, client=object(), model="claude-sonnet-4-6")
    # No LLM call.
    assert mocked.await_count == 0
    # Heuristic output preserved.
    assert result["C1"][1] in {"decoupling", "filter"}


# ----------------------------------------------------------------------
# classify_passives — public entry point
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classify_passives_without_client_uses_heuristic():
    graph = _graph_with_ambiguous_passives()
    result = await classify_passives(graph, client=None)
    # Heuristic-only — R200 still None.
    assert result["R200"][1] is None
    # Result is sync-returned (heuristic), not from an LLM call.
    assert result["C100"][1] == "ac_coupling"


@pytest.mark.asyncio
async def test_classify_passives_with_client_routes_to_llm():
    graph = _graph_with_ambiguous_passives()
    mock_output = _fake_llm_output(
        graph.device_slug, [("R200", "passive_r", "pull_up")],
    )
    with patch.object(
        passive_classifier, "call_with_forced_tool",
        new=AsyncMock(return_value=mock_output),
    ) as mocked:
        result = await classify_passives(graph, client=object(), model="claude-sonnet-4-6")
    assert mocked.await_count == 1
    assert result["R200"][1] == "pull_up"


# ----------------------------------------------------------------------
# PassiveClassification schema round-trip
# ----------------------------------------------------------------------


def test_passive_classification_schema_round_trip():
    original = PassiveClassification(
        device_slug="test",
        assignments=[
            PassiveAssignment(refdes="C1", kind="passive_c", role="decoupling", confidence=0.9),
        ],
        ambiguities=["R200: no context found"],
        model_used="claude-sonnet-4-6",
    )
    restored = PassiveClassification.model_validate(original.model_dump())
    assert restored.assignments[0].refdes == "C1"
    assert restored.assignments[0].role == "decoupling"
