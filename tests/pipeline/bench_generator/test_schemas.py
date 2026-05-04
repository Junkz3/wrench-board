from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.pipeline.bench_generator.schemas import (
    Cause,
    EvidenceSpan,
    ProposalsPayload,
    ProposedScenarioDraft,
    Rejection,
    ReliabilityCard,
    RunManifest,
)


def test_cause_requires_value_ohms_for_leaky_short():
    with pytest.raises(ValidationError, match="value_ohms"):
        Cause(refdes="C19", mode="leaky_short")


def test_cause_requires_voltage_pct_for_regulating_low():
    with pytest.raises(ValidationError, match="voltage_pct"):
        Cause(refdes="U12", mode="regulating_low")


def test_cause_shorted_mode_accepts_no_extra_fields():
    c = Cause(refdes="C19", mode="shorted")
    assert c.value_ohms is None
    assert c.voltage_pct is None


def test_cause_rejects_unknown_mode():
    with pytest.raises(ValidationError):
        Cause(refdes="C19", mode="combusting")


def test_cause_forbids_extra_fields():
    with pytest.raises(ValidationError):
        Cause(refdes="C19", mode="shorted", haunted=True)


def test_evidence_span_requires_known_field():
    with pytest.raises(ValidationError):
        EvidenceSpan(
            field="cause.color",  # not in Literal
            source_quote_substring="snippet",
            reasoning="why",
        )


def test_proposed_scenario_draft_roundtrip():
    d = ProposedScenarioDraft(
        local_id="abc12345",
        cause=Cause(refdes="C19", mode="shorted"),
        expected_dead_rails=["+3V3"],
        expected_dead_components=[],
        source_url="https://example.com/x",
        source_quote=(
            "C19 is the output decoupling cap of the +3V3 regulator "
            "and a hard short collapses the rail to ground."
        ),
        confidence=0.82,
        evidence=[
            EvidenceSpan(
                field="cause.refdes",
                source_quote_substring="C19",
                reasoning="explicitly names C19",
            ),
            EvidenceSpan(
                field="expected_dead_rails",
                source_quote_substring="collapses the rail to ground",
                reasoning="quote asserts rail death",
            ),
        ],
        reasoning_summary="short on +3V3 decoupling cap collapses rail",
    )
    # round-trip through JSON
    assert d.model_validate_json(d.model_dump_json()) == d


def test_proposals_payload_bounds_scenarios():
    # empty is valid
    p = ProposalsPayload(scenarios=[])
    assert p.scenarios == []
    # too many rejected
    with pytest.raises(ValidationError):
        ProposalsPayload(scenarios=[_draft_stub() for _ in range(51)])


def test_rejection_carries_motive():
    r = Rejection(
        local_id="abc",
        motive="refdes_not_in_graph",
        detail="XZ999 not in 287 components",
    )
    assert r.motive == "refdes_not_in_graph"


def test_run_manifest_fields():
    m = RunManifest(
        device_slug="mnt-reform-motherboard",
        run_date="2026-04-24",
        run_timestamp="2026-04-24T21:00:00Z",
        model="claude-sonnet-4-6",
        n_proposed=8,
        n_accepted=5,
        n_rejected=3,
        input_mtimes={"raw_research_dump.md": 1714000000.0},
        escalated_rejects=False,
    )
    assert m.n_proposed == 8


def test_reliability_card_minimal():
    c = ReliabilityCard(
        device_slug="x",
        score=0.7,
        self_mrr=0.8,
        cascade_recall=0.55,
        n_scenarios=3,
        generated_at="2026-04-24T21:00:00Z",
        source_run_date="2026-04-24",
    )
    assert c.score == 0.7
    assert c.notes == []  # default


def _draft_stub() -> ProposedScenarioDraft:
    return ProposedScenarioDraft(
        local_id="x",
        cause=Cause(refdes="C1", mode="shorted"),
        expected_dead_rails=[],
        expected_dead_components=[],
        source_url="https://example.com/x",
        source_quote="A" * 60,
        confidence=0.5,
        evidence=[
            EvidenceSpan(
                field="cause.refdes",
                source_quote_substring="A",
                reasoning="stub",
            )
        ],
        reasoning_summary="stub",
    )
