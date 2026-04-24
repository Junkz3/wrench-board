# SPDX-License-Identifier: Apache-2.0
"""Stateless validation passes. Each check returns a `Rejection | None`.

Passes V1-V5 per spec §4.4:
  V1 — sanity (mode, url, quote length, required mode-specific fields)
  V2 — grounding (evidence_span ⊂ source_quote, literally)
  V3 — topology (refdes + rails exist in ElectricalGraph)
  V4 — mode/kind pertinence (mirrors evaluator._is_pertinent inline)
  V5 — dedup within run

The module is a collection of pure functions. No network, no filesystem,
no LLM. Tests are fast and deterministic.

`run_all(drafts, graph)` composes V1-V5 and returns
`(accepted: list[ProposedScenarioDraft], rejected: list[Rejection])`.
This task adds V1 and V5 only; V2/V3/V4/run_all come in subsequent tasks.
"""

from __future__ import annotations

import re

from api.pipeline.bench_generator.schemas import (
    ProposedScenarioDraft,
    Rejection,
)
from api.pipeline.schematic.schemas import ElectricalGraph

_URL_RE = re.compile(r"^https?://[^\s]+$")


def check_sanity(draft: ProposedScenarioDraft) -> Rejection | None:
    """V1: catch malformed drafts we can reject without touching the graph."""
    if len(draft.source_quote) < 50:
        return Rejection(
            local_id=draft.local_id,
            motive="source_quote_too_short",
            detail=f"quote length={len(draft.source_quote)}",
            original_draft=draft,
        )
    if not _URL_RE.match(draft.source_url):
        return Rejection(
            local_id=draft.local_id,
            motive="source_url_malformed",
            detail=draft.source_url[:80],
            original_draft=draft,
        )
    # Pydantic enforces FailureMode via Literal, value_ohms / voltage_pct via
    # model_validator. A draft that got here is already mode-consistent; the
    # Literal guard gives us unknown_mode protection for free.
    return None


def check_duplicates(
    drafts: list[ProposedScenarioDraft],
) -> tuple[list[ProposedScenarioDraft], list[Rejection]]:
    """V5: drop duplicates by (refdes, mode, rails_sorted, components_sorted).
    The first occurrence wins; later collisions are rejected."""
    seen: set[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = set()
    accepted: list[ProposedScenarioDraft] = []
    rejected: list[Rejection] = []
    for d in drafts:
        key = (
            d.cause.refdes,
            d.cause.mode,
            tuple(sorted(d.expected_dead_rails)),
            tuple(sorted(d.expected_dead_components)),
        )
        if key in seen:
            rejected.append(
                Rejection(
                    local_id=d.local_id,
                    motive="duplicate_in_run",
                    detail=f"collides on key={key}",
                    original_draft=d,
                )
            )
            continue
        seen.add(key)
        accepted.append(d)
    return accepted, rejected


def check_grounding(draft: ProposedScenarioDraft) -> Rejection | None:
    """V2: evidence spans must be literal substrings of source_quote, and
    every non-empty field must have at least one evidence entry."""
    quote = draft.source_quote

    # 2a. Every span is literal.
    for span in draft.evidence:
        if span.source_quote_substring not in quote:
            return Rejection(
                local_id=draft.local_id,
                motive="evidence_span_not_literal",
                detail=(
                    f"field={span.field!r} substring={span.source_quote_substring!r} not in quote"
                ),
                original_draft=draft,
            )

    evidence_fields = {e.field for e in draft.evidence}

    # 2b. Non-empty filled fields must have evidence.
    # cause.refdes is always present — require evidence.
    # cause.mode is always present — require evidence.
    required_evidence: set[str] = {"cause.refdes", "cause.mode"}
    if draft.cause.value_ohms is not None:
        required_evidence.add("cause.value_ohms")
    if draft.cause.voltage_pct is not None:
        required_evidence.add("cause.voltage_pct")
    if draft.expected_dead_rails:
        required_evidence.add("expected_dead_rails")
    if draft.expected_dead_components:
        required_evidence.add("expected_dead_components")

    missing = required_evidence - evidence_fields
    if missing:
        return Rejection(
            local_id=draft.local_id,
            motive="evidence_missing",
            detail=f"missing evidence for fields: {sorted(missing)}",
            original_draft=draft,
        )

    # 2c. Evidence on empty lists is invalid.
    if "expected_dead_rails" in evidence_fields and not draft.expected_dead_rails:
        return Rejection(
            local_id=draft.local_id,
            motive="evidence_field_empty",
            detail="evidence points at expected_dead_rails but list is empty",
            original_draft=draft,
        )
    if "expected_dead_components" in evidence_fields and not draft.expected_dead_components:
        return Rejection(
            local_id=draft.local_id,
            motive="evidence_field_empty",
            detail="evidence points at expected_dead_components but list is empty",
            original_draft=draft,
        )

    return None


def check_topology(draft: ProposedScenarioDraft, graph: ElectricalGraph) -> Rejection | None:
    """V3: every refdes and rail in the draft must exist in the graph."""
    if draft.cause.refdes not in graph.components:
        return Rejection(
            local_id=draft.local_id,
            motive="refdes_not_in_graph",
            detail=(
                f"cause.refdes={draft.cause.refdes!r} not among {len(graph.components)} components"
            ),
            original_draft=draft,
        )
    for rail in draft.expected_dead_rails:
        if rail not in graph.power_rails:
            return Rejection(
                local_id=draft.local_id,
                motive="rail_name_not_in_graph",
                detail=(f"expected rail {rail!r} not among {list(graph.power_rails)}"),
                original_draft=draft,
            )
    for refdes in draft.expected_dead_components:
        if refdes not in graph.components:
            return Rejection(
                local_id=draft.local_id,
                motive="component_not_in_graph",
                detail=f"expected dead component {refdes!r} not in graph",
                original_draft=draft,
            )
    return None


# Kept in sync with api/pipeline/schematic/evaluator._is_pertinent. We
# MIRROR the rules inline rather than import the private function — the
# duplication is ~15 lines, documented, and survives renames in evaluator.
_PASSIVE_R_ROLES_WITH_REAL_OPEN_CASCADE: frozenset[str] = frozenset(
    {
        "series",
        "damping",
        "inrush_limiter",
    }
)


def check_pertinence(draft: ProposedScenarioDraft, graph: ElectricalGraph) -> Rejection | None:
    """V4: reject (refdes, mode) pairs that don't produce an observable
    simulator effect. Mirror of evaluator._is_pertinent."""
    refdes = draft.cause.refdes
    mode = draft.cause.mode
    comp = graph.components.get(refdes)
    if comp is None:
        # Topology check already guards this — if we reach here we are
        # in a test fixture skipping V3. Be conservative and accept.
        return None
    kind = comp.kind or "ic"

    def _reject(detail: str) -> Rejection:
        return Rejection(
            local_id=draft.local_id,
            motive="mode_not_pertinent",
            detail=detail,
            original_draft=draft,
        )

    if kind == "ic" and mode == "regulating_low":
        sources_any = any(rail.source_refdes == refdes for rail in graph.power_rails.values())
        if not sources_any:
            return _reject(f"IC {refdes} sources no rail; regulating_low is silent")
    if kind == "passive_c" and mode == "leaky_short":
        in_decoupling = any(
            refdes in (rail.decoupling or []) for rail in graph.power_rails.values()
        )
        if not in_decoupling:
            return _reject(f"cap {refdes} not in any rail.decoupling; leaky_short silent")
    if kind == "passive_r" and mode == "open":
        role = (comp.role or "").lower()
        if role not in _PASSIVE_R_ROLES_WITH_REAL_OPEN_CASCADE:
            return _reject(f"resistor {refdes} role={role!r} — open produces no cascade")
    return None


def run_all(
    drafts: list[ProposedScenarioDraft],
    graph: ElectricalGraph,
) -> tuple[list[ProposedScenarioDraft], list[Rejection]]:
    """V1 → V2 → V3 → V4 (per draft, short-circuit on first failure) then
    V5 dedup over the survivors."""
    survivors: list[ProposedScenarioDraft] = []
    rejected: list[Rejection] = []
    for draft in drafts:
        for check in (check_sanity, check_grounding):
            rej = check(draft)
            if rej is not None:
                rejected.append(rej)
                break
        else:
            rej = check_topology(draft, graph)
            if rej is not None:
                rejected.append(rej)
                continue
            rej = check_pertinence(draft, graph)
            if rej is not None:
                rejected.append(rej)
                continue
            survivors.append(draft)
    deduped, dup_rejects = check_duplicates(survivors)
    rejected.extend(dup_rejects)
    return deduped, rejected
