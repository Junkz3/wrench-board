from __future__ import annotations

from api.pipeline.bench_generator.schemas import (
    Cause,
    EvidenceSpan,
    ProposedScenarioDraft,
)
from api.pipeline.bench_generator.validator import (
    check_cause_rail_connection,
    check_duplicates,
    check_grounding,
    check_pertinence,
    check_rails_mentioned_in_quote,
    check_refdes_mentioned_in_quote,
    check_sanity,
    check_topology,
    run_all,
)


def _with_overrides(base: ProposedScenarioDraft, **changes) -> ProposedScenarioDraft:
    data = base.model_dump()
    data.update(changes)
    return ProposedScenarioDraft.model_validate(data)


def test_v1_accepts_clean_draft(sample_draft):
    rej = check_sanity(sample_draft)
    assert rej is None


def test_v1_rejects_too_short_quote(sample_draft):
    # this would already fail at Pydantic load time — V1 is defence in depth
    # when we later relax Pydantic. For now, construct through .model_construct
    # to bypass validation.
    d = ProposedScenarioDraft.model_construct(
        **{
            **sample_draft.model_dump(),
            "source_quote": "too short",
        }
    )
    rej = check_sanity(d)
    assert rej is not None
    assert rej.motive == "source_quote_too_short"


def test_v1_rejects_malformed_url(sample_draft):
    d = ProposedScenarioDraft.model_construct(
        **{
            **sample_draft.model_dump(),
            "source_url": "not-a-url",
        }
    )
    rej = check_sanity(d)
    assert rej is not None
    assert rej.motive == "source_url_malformed"


def test_v5_dedup_keeps_first(sample_draft):
    d1 = sample_draft
    d2 = _with_overrides(sample_draft, local_id="c19-short-dup")
    accepted, rejected = check_duplicates([d1, d2])
    assert [d.local_id for d in accepted] == ["c19-short"]
    assert [r.local_id for r in rejected] == ["c19-short-dup"]
    assert rejected[0].motive == "duplicate_in_run"


def test_v5_no_dup_no_rejection(sample_draft):
    d1 = sample_draft
    d2 = _with_overrides(
        sample_draft,
        local_id="c19-open",
        cause=Cause(refdes="C19", mode="open").model_dump(),
    )
    accepted, rejected = check_duplicates([d1, d2])
    assert len(accepted) == 2
    assert rejected == []


def test_v2_accepts_clean_grounding(sample_draft):
    rej = check_grounding(sample_draft)
    assert rej is None


def test_v2_rejects_nonliteral_span(sample_draft):
    bad = sample_draft.model_copy(deep=True)
    bad.evidence[0] = EvidenceSpan(
        field="cause.refdes",
        source_quote_substring="C 19",  # extra space — not literally in quote
        reasoning="wrong",
    )
    rej = check_grounding(bad)
    assert rej is not None
    assert rej.motive == "evidence_span_not_literal"


def test_v2_rejects_missing_evidence_for_nonempty_rails(sample_draft):
    """If expected_dead_rails is non-empty, at least one evidence must target it."""
    bad = sample_draft.model_copy(deep=True)
    bad.evidence = [e for e in bad.evidence if e.field != "expected_dead_rails"]
    rej = check_grounding(bad)
    assert rej is not None
    assert rej.motive == "evidence_missing"
    assert "expected_dead_rails" in rej.detail


def test_v2_rejects_evidence_on_empty_field(sample_draft):
    """If expected_dead_components is empty, no evidence may point at it."""
    bad = sample_draft.model_copy(deep=True)
    bad.evidence.append(
        EvidenceSpan(
            field="expected_dead_components",
            source_quote_substring="C19",
            reasoning="stale",
        )
    )
    rej = check_grounding(bad)
    assert rej is not None
    assert rej.motive == "evidence_field_empty"


def test_v3_accepts_known_refdes_and_rail(sample_draft, toy_graph):
    rej = check_topology(sample_draft, toy_graph)
    assert rej is None


def test_v3_rejects_unknown_refdes(sample_draft, toy_graph):
    bad = sample_draft.model_copy(deep=True)
    bad.cause = Cause(refdes="XZ999", mode="shorted")
    rej = check_topology(bad, toy_graph)
    assert rej is not None
    assert rej.motive == "refdes_not_in_graph"
    assert "XZ999" in rej.detail


def test_v3_rejects_unknown_rail(sample_draft, toy_graph):
    bad = sample_draft.model_copy(deep=True)
    bad.expected_dead_rails = ["+3V3", "+42V_MYSTERY"]
    rej = check_topology(bad, toy_graph)
    assert rej is not None
    assert rej.motive == "rail_name_not_in_graph"
    assert "+42V_MYSTERY" in rej.detail


def test_v3_rejects_unknown_component(sample_draft, toy_graph):
    bad = sample_draft.model_copy(deep=True)
    bad.expected_dead_components = ["U7", "U_HIDDEN"]
    rej = check_topology(bad, toy_graph)
    assert rej is not None
    assert rej.motive == "component_not_in_graph"
    assert "U_HIDDEN" in rej.detail


def test_v4_accepts_ic_dead(sample_draft, toy_graph):
    """dead is pertinent for any IC regardless of rail-sourcing."""
    d = sample_draft.model_copy(deep=True)
    d.cause = Cause(refdes="U1", mode="dead")  # U1 = cpu, sources no rail
    rej = check_pertinence(d, toy_graph)
    assert rej is None


def test_v4_rejects_regulating_low_on_non_source_ic(sample_draft, toy_graph):
    """U1 is a CPU — doesn't source any rail. regulating_low is nonsense."""
    d = sample_draft.model_copy(deep=True)
    d.cause = Cause(refdes="U1", mode="regulating_low", voltage_pct=0.85)
    rej = check_pertinence(d, toy_graph)
    assert rej is not None
    assert rej.motive == "mode_not_pertinent"


def test_v4_accepts_regulating_low_on_source_ic(sample_draft, toy_graph):
    """U7 is the +5V source — regulating_low is meaningful."""
    d = sample_draft.model_copy(deep=True)
    d.cause = Cause(refdes="U7", mode="regulating_low", voltage_pct=0.85)
    rej = check_pertinence(d, toy_graph)
    assert rej is None


def test_v4_rejects_leaky_short_on_non_decoupling_cap(sample_draft, toy_graph):
    """Add a cap that is NOT in any rail's decoupling list."""
    from api.pipeline.schematic.schemas import ComponentNode

    toy_graph.components["C99"] = ComponentNode(
        refdes="C99",
        type="capacitor",
        kind="passive_c",
        role="decoupling",
    )
    d = sample_draft.model_copy(deep=True)
    d.cause = Cause(refdes="C99", mode="leaky_short", value_ohms=200.0)
    rej = check_pertinence(d, toy_graph)
    assert rej is not None
    assert rej.motive == "mode_not_pertinent"


def test_v4_rejects_open_on_pullup_r(sample_draft, toy_graph):
    """role='pullup' is not in the open-cascading roles set."""
    d = sample_draft.model_copy(deep=True)
    d.cause = Cause(refdes="R100", mode="open")  # role = pullup
    rej = check_pertinence(d, toy_graph)
    assert rej is not None
    assert rej.motive == "mode_not_pertinent"


def test_v4_accepts_open_on_series_r(sample_draft, toy_graph):
    d = sample_draft.model_copy(deep=True)
    d.cause = Cause(refdes="R200", mode="open")  # role = series
    rej = check_pertinence(d, toy_graph)
    assert rej is None


def test_run_all_accepts_clean_draft(sample_draft, toy_graph):
    accepted, rejected = run_all([sample_draft], toy_graph)
    assert [d.local_id for d in accepted] == ["c19-short"]
    assert rejected == []


def test_run_all_partitions_mixed_batch(sample_draft, toy_graph):
    good = sample_draft
    bad_topology = sample_draft.model_copy(deep=True)
    bad_topology.local_id = "bad-topo"
    bad_topology.cause = Cause(refdes="XZ999", mode="shorted")
    bad_topology.evidence = [
        EvidenceSpan(
            field="cause.refdes", source_quote_substring="C19", reasoning="stale but literal"
        ),
        EvidenceSpan(field="cause.mode", source_quote_substring="short", reasoning="literal"),
    ]
    bad_topology.expected_dead_rails = []

    accepted, rejected = run_all([good, bad_topology], toy_graph)
    assert [d.local_id for d in accepted] == ["c19-short"]
    # V2b.1 catches the refdes absent from quote BEFORE V3's graph check.
    # "XZ999" is neither in the quote nor in the graph; the earlier check wins.
    assert [r.motive for r in rejected] == ["refdes_not_mentioned_in_quote"]


def test_run_all_short_circuits_per_draft(sample_draft, toy_graph):
    """A draft that fails V2 should not also generate a V3 rejection — only
    the first motive is reported."""
    bad = sample_draft.model_copy(deep=True)
    bad.local_id = "bad-both"
    bad.evidence[0] = EvidenceSpan(
        field="cause.refdes",
        source_quote_substring="NOT IN QUOTE",
        reasoning="wrong",
    )
    bad.cause = Cause(refdes="XZ999", mode="shorted")
    accepted, rejected = run_all([bad], toy_graph)
    assert accepted == []
    assert len(rejected) == 1
    assert rejected[0].motive == "evidence_span_not_literal"


# V2b semantic guardrails


def test_v2b_refdes_mentioned_accepts_when_present(sample_draft):
    # sample_draft quote mentions "C19" explicitly
    assert check_refdes_mentioned_in_quote(sample_draft) is None


def test_v2b_refdes_mentioned_rejects_when_absent(sample_draft):
    bad = sample_draft.model_copy(deep=True)
    bad.cause = Cause(refdes="U7", mode="dead")
    # quote talks about C19, not U7
    rej = check_refdes_mentioned_in_quote(bad)
    assert rej is not None
    assert rej.motive == "refdes_not_mentioned_in_quote"


def test_v2b_refdes_mentioned_is_case_insensitive(sample_draft):
    # Build a quote with lowercase version of the refdes
    q = "The c19 capacitor shorts and collapses the rail." + " padding." * 8
    d = sample_draft.model_copy(deep=True)
    d.source_quote = q
    d.evidence = [
        EvidenceSpan(
            field="cause.refdes",
            source_quote_substring="c19",
            reasoning="lowercase mention",
        ),
        EvidenceSpan(
            field="cause.mode",
            source_quote_substring="shorts",
            reasoning="mode",
        ),
        EvidenceSpan(
            field="expected_dead_rails",
            source_quote_substring="collapses the rail",
            reasoning="rail death",
        ),
    ]
    # Refdes mention is case-insensitive: 'C19' (upper) should still match 'c19' (lower)
    assert check_refdes_mentioned_in_quote(d) is None


def test_v2b_rails_mentioned_accepts_when_present(sample_draft):
    # sample_draft quote contains "+3V3"
    assert check_rails_mentioned_in_quote(sample_draft) is None


def test_v2b_rails_mentioned_rejects_when_absent(sample_draft):
    bad = sample_draft.model_copy(deep=True)
    bad.expected_dead_rails = ["+5V"]  # +5V is not mentioned in the C19 quote
    rej = check_rails_mentioned_in_quote(bad)
    assert rej is not None
    assert rej.motive == "rail_not_mentioned_in_quote"


def test_v2b_rails_mentioned_empty_list_passes(sample_draft):
    # Zero-cascade scenario: no rails expected, no check to do
    d = sample_draft.model_copy(deep=True)
    d.expected_dead_rails = []
    # Drop the now-orphan evidence entry for expected_dead_rails; V2 would
    # otherwise reject this draft for having evidence on an empty field.
    d.evidence = [e for e in d.evidence if e.field != "expected_dead_rails"]
    assert check_rails_mentioned_in_quote(d) is None


def test_v2b_cause_rail_connection_accepts_source(sample_draft, toy_graph):
    # C19 is in the decoupling list of +3V3 in toy_graph
    assert check_cause_rail_connection(sample_draft, toy_graph) is None


def test_v2b_cause_rail_connection_accepts_rail_source_ic(sample_draft, toy_graph):
    # U13 is the source of +3V3. Pointing cause at U13 with expected +3V3 is OK.
    d = sample_draft.model_copy(deep=True)
    d.cause = Cause(refdes="U13", mode="dead")
    assert check_cause_rail_connection(d, toy_graph) is None


def test_v2b_cause_rail_connection_rejects_unrelated_refdes(sample_draft, toy_graph):
    # U7 sources +5V, not +3V3. Draft claims U7 kills +3V3 — wrong topology.
    d = sample_draft.model_copy(deep=True)
    d.cause = Cause(refdes="U7", mode="dead")
    # expected_dead_rails stays [+3V3] from sample_draft
    rej = check_cause_rail_connection(d, toy_graph)
    assert rej is not None
    assert rej.motive == "cause_not_connected_to_rail"


def test_v2b_cause_rail_connection_empty_rails_passes(sample_draft, toy_graph):
    # No rails expected → no topology constraint
    d = sample_draft.model_copy(deep=True)
    d.expected_dead_rails = []
    d.evidence = [e for e in d.evidence if e.field != "expected_dead_rails"]
    assert check_cause_rail_connection(d, toy_graph) is None


def test_run_all_applies_v2b_before_v3(sample_draft, toy_graph):
    # Draft with unrelated refdes + rail that is valid in graph but not
    # mentioned in quote should be rejected by V2b.2 (rail_not_mentioned)
    # before reaching V3.
    d = sample_draft.model_copy(deep=True)
    d.expected_dead_rails = ["+5V"]  # +5V exists in graph but not in quote
    accepted, rejected = run_all([d], toy_graph)
    assert accepted == []
    assert len(rejected) == 1


# Registry-bridge acceptance (V2b.1+ relaxed path via registry)


def test_v2b_refdes_accepts_via_registry_functional_name(sample_draft, toy_graph, toy_registry):
    """Quote mentions a canonical functional name; cause.refdes is a
    plausible candidate (kind-compatible, shares rail token) → accept."""
    d = sample_draft.model_copy(deep=True)
    d.cause = Cause(refdes="U7", mode="dead")
    d.source_quote = (
        "The Buck regulator for +5V stopped delivering voltage, and the "
        "+5V rail went dead after the event — board won't boot."
    )
    d.expected_dead_rails = ["+5V"]
    d.evidence = [
        EvidenceSpan(
            field="cause.refdes",
            source_quote_substring="Buck regulator for +5V",
            reasoning="canonical name cited; U7 sources +5V",
        ),
        EvidenceSpan(
            field="cause.mode",
            source_quote_substring="stopped delivering voltage",
            reasoning="dead mode",
        ),
        EvidenceSpan(
            field="expected_dead_rails",
            source_quote_substring="+5V rail went dead",
            reasoning="rail dies with regulator",
        ),
    ]
    rej = check_refdes_mentioned_in_quote(d, toy_registry, toy_graph)
    assert rej is None, f"expected acceptance via registry, got {rej}"


def test_v2b_refdes_rejects_when_registry_citation_kind_mismatch(
    sample_draft, toy_graph, toy_registry
):
    """Quote cites the registry IC entry but cause.refdes is a cap.
    score_refdes_for_canonical rejects on kind mismatch."""
    d = sample_draft.model_copy(deep=True)
    d.cause = Cause(refdes="C19", mode="shorted")
    d.source_quote = (
        "The Buck regulator for +5V stopped delivering voltage on the board — "
        "no amount of restart brings it back."
    )
    d.expected_dead_rails = []
    d.evidence = [
        EvidenceSpan(
            field="cause.refdes",
            source_quote_substring="Buck regulator for +5V",
            reasoning="misattribution attempt",
        ),
        EvidenceSpan(
            field="cause.mode",
            source_quote_substring="stopped delivering voltage",
            reasoning="short",
        ),
    ]
    rej = check_refdes_mentioned_in_quote(d, toy_registry, toy_graph)
    assert rej is not None
    assert rej.motive == "refdes_not_mentioned_in_quote"


def test_v2b_rails_accepts_via_registry_alias(sample_draft, toy_registry):
    """Quote mentions '+5V_rail' alias; expected_dead_rails=['+5V']
    accepted because the registry signal maps +5V → ['+5V_rail', ...]."""
    d = sample_draft.model_copy(deep=True)
    d.source_quote = (
        "When the regulator fails, the +5V_rail collapses across the board "
        "and peripherals go silent entirely."
    )
    d.cause = Cause(refdes="U7", mode="dead")
    d.expected_dead_rails = ["+5V"]
    d.evidence = [
        EvidenceSpan(
            field="cause.refdes",
            source_quote_substring="regulator fails",
            reasoning="stub",
        ),
        EvidenceSpan(
            field="cause.mode",
            source_quote_substring="regulator fails",
            reasoning="stub",
        ),
        EvidenceSpan(
            field="expected_dead_rails",
            source_quote_substring="+5V_rail",
            reasoning="rail alias cited",
        ),
    ]
    rej = check_rails_mentioned_in_quote(d, toy_registry)
    assert rej is None


def test_v2b_rails_strict_when_no_registry(sample_draft):
    """Without registry, alias-path is unavailable; strict literal check only."""
    d = sample_draft.model_copy(deep=True)
    d.expected_dead_rails = ["+5V"]  # not in sample_draft quote
    rej = check_rails_mentioned_in_quote(d)
    assert rej is not None
    assert rej.motive == "rail_not_mentioned_in_quote"


# Registry-supplied refdes_candidates — strict bridge (no heuristic fallback)


def _registry_with_candidates(refdes_for_canonical: dict[str, list[str]]) -> dict:
    """Helper: build a toy registry whose canonicals carry refdes_candidates.

    `refdes_for_canonical` maps canonical_name → list of refdes the
    Registry Builder allegedly justified as candidates."""
    components = []
    for canonical, refdes_list in refdes_for_canonical.items():
        components.append(
            {
                "canonical_name": canonical,
                "aliases": [],
                "kind": "ic",
                "description": f"sources for {canonical}",
                "refdes_candidates": [
                    {
                        "refdes": r,
                        "confidence": 0.95,
                        "evidence": "dump quote ties canonical to refdes",
                    }
                    for r in refdes_list
                ],
            }
        )
    return {
        "schema_version": "1.0",
        "device_label": "toy-board",
        "taxonomy": {},
        "components": components,
        "signals": [],
    }


def test_v2b_refdes_accepts_when_in_registry_candidates(sample_draft, toy_graph):
    """Quote cites canonical 'main buck'; cause.refdes=U7 IS in
    refdes_candidates → V2b.1 accepts via the strict registry path."""
    registry = _registry_with_candidates({"main buck": ["U7"]})
    d = sample_draft.model_copy(deep=True)
    d.cause = Cause(refdes="U7", mode="dead")
    d.source_quote = (
        "The main buck failed and the regulator stopped converting input "
        "to its programmed output voltage on this board."
    )
    d.expected_dead_rails = []
    d.evidence = [
        EvidenceSpan(
            field="cause.refdes",
            source_quote_substring="main buck",
            reasoning="canonical cited",
        ),
        EvidenceSpan(
            field="cause.mode",
            source_quote_substring="failed",
            reasoning="dead mode",
        ),
    ]
    rej = check_refdes_mentioned_in_quote(d, registry, toy_graph)
    assert rej is None


def test_v2b_refdes_rejects_when_not_in_registry_candidates(sample_draft, toy_graph):
    """Same quote but cause.refdes=U13 (a different IC the heuristic WOULD
    accept) is NOT in refdes_candidates → registry takes precedence and
    rejects. No heuristic fallback for that canonical."""
    registry = _registry_with_candidates({"main buck": ["U7"]})
    d = sample_draft.model_copy(deep=True)
    d.cause = Cause(refdes="U13", mode="dead")
    d.source_quote = (
        "The main buck failed and the regulator stopped converting input "
        "to its programmed output voltage on this board."
    )
    d.expected_dead_rails = []
    d.evidence = [
        EvidenceSpan(
            field="cause.refdes",
            source_quote_substring="main buck",
            reasoning="canonical cited",
        ),
        EvidenceSpan(
            field="cause.mode",
            source_quote_substring="failed",
            reasoning="dead mode",
        ),
    ]
    rej = check_refdes_mentioned_in_quote(d, registry, toy_graph)
    assert rej is not None
    assert rej.motive == "refdes_not_mentioned_in_quote"


# Mapper-attributions strict bridge (Phase 2.5 output, supersedes registry/heuristic)


def _mapper_attributions(canonical_to_refdes: dict[str, list[str]]) -> list[dict]:
    """Helper: build a Mapper attributions list (the persisted JSON shape)."""
    out: list[dict] = []
    for canonical, refdes_list in canonical_to_refdes.items():
        for r in refdes_list:
            out.append({
                "canonical_name": canonical,
                "refdes": r,
                "confidence": 0.95,
                "evidence_kind": "literal_refdes_in_quote",
                "evidence_quote": "the dump fragment that justifies this attribution",
                "reasoning": "test fixture",
            })
    return out


def test_v2b_refdes_accepts_when_in_mapper_attributions(sample_draft, toy_graph):
    """Mapper attributions cover 'main buck' → ['U7'], cause.refdes=U7,
    quote cites 'main buck' canonical → V2b.1 accepts via the strict
    Mapper path."""
    registry = {
        "components": [
            {
                "canonical_name": "main buck",
                "aliases": [],
                "kind": "ic",
                "description": "main buck",
            }
        ],
    }
    attributions = _mapper_attributions({"main buck": ["U7"]})
    d = sample_draft.model_copy(deep=True)
    d.cause = Cause(refdes="U7", mode="dead")
    d.source_quote = (
        "The main buck failed and the regulator stopped converting input "
        "to its programmed output voltage on this board."
    )
    d.expected_dead_rails = []
    d.evidence = [
        EvidenceSpan(field="cause.refdes", source_quote_substring="main buck", reasoning="x"),
        EvidenceSpan(field="cause.mode", source_quote_substring="failed", reasoning="y"),
    ]
    rej = check_refdes_mentioned_in_quote(d, registry, toy_graph, attributions)
    assert rej is None


def test_v2b_refdes_rejects_when_not_in_mapper_attributions(sample_draft, toy_graph):
    """Mapper covers 'main buck' → ['U7'] only. cause.refdes=U13 — heuristic
    would have accepted (kind-compatible regulator) but the Mapper said no.
    V2b.1 rejects strictly."""
    registry = {
        "components": [
            {
                "canonical_name": "main buck",
                "aliases": [],
                "kind": "ic",
                "description": "main buck",
            }
        ],
    }
    attributions = _mapper_attributions({"main buck": ["U7"]})
    d = sample_draft.model_copy(deep=True)
    d.cause = Cause(refdes="U13", mode="dead")
    d.source_quote = (
        "The main buck failed and the regulator stopped converting input "
        "to its programmed output voltage on this board."
    )
    d.expected_dead_rails = []
    d.evidence = [
        EvidenceSpan(field="cause.refdes", source_quote_substring="main buck", reasoning="x"),
        EvidenceSpan(field="cause.mode", source_quote_substring="failed", reasoning="y"),
    ]
    rej = check_refdes_mentioned_in_quote(d, registry, toy_graph, attributions)
    assert rej is not None
    assert rej.motive == "refdes_not_mentioned_in_quote"


def test_v2b_refdes_falls_back_to_heuristic_when_no_mapper_for_canonical(
    sample_draft, toy_graph, toy_registry
):
    """attributions is non-empty but doesn't cover the canonical cited in
    the quote → V2b.1 falls back to the heuristic for that canonical, like
    before. (Mapper opted out of attributing 'Buck regulator for +5V'; the
    heuristic should still try.)"""
    attributions = _mapper_attributions({"Some other canonical": ["U99"]})
    d = sample_draft.model_copy(deep=True)
    d.cause = Cause(refdes="U7", mode="dead")
    d.source_quote = (
        "The Buck regulator for +5V stopped delivering voltage, and the "
        "+5V rail went dead after the event — board won't boot."
    )
    d.expected_dead_rails = ["+5V"]
    d.evidence = [
        EvidenceSpan(
            field="cause.refdes",
            source_quote_substring="Buck regulator for +5V",
            reasoning="canonical cited",
        ),
        EvidenceSpan(
            field="cause.mode",
            source_quote_substring="stopped delivering voltage",
            reasoning="dead",
        ),
        EvidenceSpan(
            field="expected_dead_rails",
            source_quote_substring="+5V rail went dead",
            reasoning="rail death",
        ),
    ]
    rej = check_refdes_mentioned_in_quote(d, toy_registry, toy_graph, attributions)
    assert rej is None  # heuristic still accepts because Mapper didn't cover this canonical
