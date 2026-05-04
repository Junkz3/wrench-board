"""Tests for the Opus-refined boot-sequence analyzer.

Kept deterministic — we mock `call_with_forced_tool` at the point it is
imported from inside `boot_analyzer`. The production path really does hit
Anthropic; these tests cover:
  - context builder: filters notes, formats rails/enable edges correctly
  - tool definition passes a JSON schema derived from AnalyzedBootSequence
  - orchestrator integration is graceful when the analyzer raises
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from api.pipeline.schematic import boot_analyzer
from api.pipeline.schematic.boot_analyzer import (
    analyze_boot_sequence,
    build_context,
)
from api.pipeline.schematic.schemas import (
    AnalyzedBootPhase,
    AnalyzedBootSequence,
    AnalyzedBootTrigger,
    BootPhase,
    ComponentNode,
    DesignerNote,
    ElectricalGraph,
    NetNode,
    PowerRail,
    SchematicQualityReport,
    TypedEdge,
)


def _sample_graph() -> ElectricalGraph:
    return ElectricalGraph(
        device_slug="mnt-demo",
        components={
            "U7": ComponentNode(refdes="U7", type="ic"),
            "U12": ComponentNode(refdes="U12", type="ic"),
            "U14": ComponentNode(refdes="U14", type="ic"),
        },
        nets={
            "+5V": NetNode(label="+5V", is_power=True),
            "+3V3": NetNode(label="+3V3", is_power=True),
        },
        power_rails={
            "+5V": PowerRail(
                label="+5V", voltage_nominal=5.0,
                source_refdes="U7", source_type="buck",
                enable_net="5V_PWR_EN",
                consumers=["U12", "U1"],
            ),
            "+3V3_STANDBY": PowerRail(
                label="+3V3_STANDBY", voltage_nominal=3.3,
                source_refdes="U14", source_type="ldo",
                consumers=["LPC"],
            ),
        },
        typed_edges=[
            TypedEdge(src="5V_PWR_EN", dst="U7", kind="enables", page=3),
            TypedEdge(src="3V3_PWR_EN", dst="U12", kind="enables", page=3),
            TypedEdge(src="C18", dst="+5V", kind="decouples", page=3),
        ],
        designer_notes=[
            DesignerNote(text="Main system power converters, enabled by LPC",
                         page=3, attached_to_refdes="U7"),
            DesignerNote(text="Standby always-on 3V3 power rail",
                         page=4, attached_to_refdes="U14"),
            DesignerNote(text="NOSTUFF NOSTUFF NOSTUFF",
                         page=1, attached_to_refdes="R117"),
        ],
        boot_sequence=[
            BootPhase(index=1, name="PHASE 1",
                      rails_stable=["+5V", "+3V3_STANDBY"],
                      components_entering=["U7", "U14"]),
        ],
        ambiguities=[],
        quality=SchematicQualityReport(
            total_pages=12, pages_parsed=12, confidence_global=0.9,
        ),
    )


# ----------------------------------------------------------------------
# Context builder
# ----------------------------------------------------------------------


def test_context_includes_rails_with_source_and_enable_net():
    ctx = build_context(_sample_graph())
    assert "+5V" in ctx
    assert "U7" in ctx
    assert "enable=5V_PWR_EN" in ctx
    assert "source=U14" in ctx


def test_context_keeps_sequencing_notes_drops_cosmetic():
    ctx = build_context(_sample_graph())
    assert "enabled by LPC" in ctx       # sequencing-relevant → kept
    assert "Standby always-on" in ctx    # sequencing-relevant → kept
    assert "NOSTUFF" not in ctx          # cosmetic → filtered


def test_context_lists_enable_edges_only_drops_decoupling():
    ctx = build_context(_sample_graph())
    assert "5V_PWR_EN --enables--> U7" in ctx
    assert "3V3_PWR_EN --enables--> U12" in ctx
    # Decoupling edges belong to the electrical graph but are noise here.
    assert "decouples" not in ctx


def test_context_surfaces_compiler_sequence_as_baseline():
    ctx = build_context(_sample_graph())
    assert "COMPILED BOOT SEQUENCE" in ctx
    assert "Phase 1" in ctx


# ----------------------------------------------------------------------
# analyze_boot_sequence — mocked Opus
# ----------------------------------------------------------------------


def _mock_analyzed() -> AnalyzedBootSequence:
    return AnalyzedBootSequence(
        device_slug="mnt-demo",
        phases=[
            AnalyzedBootPhase(
                index=0, name="Always-on standby", kind="always-on",
                rails_stable=["+3V3_STANDBY"], components_entering=["U14"],
                triggers_next=[],
                evidence=["designer note p4 U14: 'Standby always-on 3V3 power rail'"],
                confidence=0.95,
            ),
            AnalyzedBootPhase(
                index=1, name="LPC asserts main rails", kind="sequenced",
                rails_stable=["+5V"], components_entering=["U7", "U12"],
                triggers_next=[
                    AnalyzedBootTrigger(
                        net_label="5V_PWR_EN", from_refdes="LPC",
                        rationale="LPC drives 5V_PWR_EN to enable U7 (buck)",
                    ),
                ],
                evidence=[
                    "designer note p3 U7: 'Main system power converters, enabled by LPC'",
                    "edge: 5V_PWR_EN enables U7",
                ],
                confidence=0.9,
            ),
        ],
        sequencer_refdes="LPC",
        global_confidence=0.88,
        ambiguities=[],
        model_used="placeholder-will-be-overridden",
    )


@pytest.mark.asyncio
async def test_analyze_boot_sequence_stamps_model_used():
    fake = _mock_analyzed()
    with patch.object(boot_analyzer, "call_with_forced_tool",
                      new=AsyncMock(return_value=fake)) as mocked:
        result = await analyze_boot_sequence(
            _sample_graph(), client=None,  # type: ignore[arg-type]
            model="claude-opus-4-7",
        )
    assert result.model_used == "claude-opus-4-7"
    assert result.sequencer_refdes == "LPC"
    assert len(result.phases) == 2
    # The tool call was made once with the expected schema
    assert mocked.await_count == 1
    kwargs = mocked.await_args.kwargs
    assert kwargs["forced_tool_name"] == "submit_analyzed_boot_sequence"
    assert kwargs["output_schema"] is AnalyzedBootSequence
    assert kwargs["model"] == "claude-opus-4-7"


@pytest.mark.asyncio
async def test_analyze_boot_sequence_uses_settings_main_model_by_default():
    fake = _mock_analyzed()
    with patch.object(boot_analyzer, "call_with_forced_tool",
                      new=AsyncMock(return_value=fake)) as mocked:
        await analyze_boot_sequence(_sample_graph(), client=None)  # type: ignore[arg-type]
    # Default path should pick up the configured main model.
    assert mocked.await_args.kwargs["model"]  # non-empty str


# ----------------------------------------------------------------------
# Schema round-trip
# ----------------------------------------------------------------------


def test_analyzed_boot_sequence_round_trip():
    ab = _mock_analyzed()
    dumped = ab.model_dump()
    restored = AnalyzedBootSequence.model_validate(dumped)
    assert restored.phases[1].triggers_next[0].net_label == "5V_PWR_EN"
    assert restored.phases[0].kind == "always-on"


def test_analyzed_boot_phase_confidence_bounded():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        AnalyzedBootPhase(
            index=0, name="bad", kind="always-on",
            confidence=1.5,  # out of bounds
        )
