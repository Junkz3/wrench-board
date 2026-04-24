# SPDX-License-Identifier: Apache-2.0
"""Stateless validation passes. Each check returns a `Rejection | None`.

Passes V1-V5 per spec §4.4, extended with V2b semantic guardrails:
  V1  — sanity (mode, url, quote length, required mode-specific fields)
  V2  — grounding (evidence_span ⊂ source_quote, literally)
  V2b — semantic grounding: refdes + rails must be mentioned in quote,
        and cause.refdes must be topologically connected to its rails
  V3  — topology (refdes + rails exist in ElectricalGraph)
  V4  — mode/kind pertinence (mirrors evaluator._is_pertinent inline)
  V5  — dedup within run

V2b exists because V2's literal-span check proves no LLM invention but
does NOT verify the span semantically justifies the field it anchors.
A quote fragment "battery cell leaking" can be cited as evidence for
`cause.refdes=J1` (a connector) — structurally valid but semantically
wrong. V2b adds three orthogonal checks to close that gap.

The module is a collection of pure functions. No network, no filesystem,
no LLM.
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


def check_refdes_mentioned_in_quote(
    draft: ProposedScenarioDraft,
    registry: dict | None = None,
    graph: ElectricalGraph | None = None,
) -> Rejection | None:
    """V2b.1: cause.refdes must be grounded in source_quote.

    Accepts EITHER:
      (a) cause.refdes appears literally (case-insensitive) in source_quote.
      (b) A registry canonical_name or alias appears literally in
          source_quote, AND cause.refdes is a plausible candidate for
          that registry entry per `score_refdes_for_canonical` heuristic.

    Path (b) requires both `registry` and `graph`. When either is None,
    only path (a) applies — preserving strict behaviour for test
    fixtures that don't wire a registry.
    """
    from api.pipeline.bench_generator.prompts import score_refdes_for_canonical

    refdes = draft.cause.refdes
    quote_lc = draft.source_quote.lower()

    # Path (a)
    if refdes.lower() in quote_lc:
        return None

    # Path (b): functional-name bridge via registry
    if registry and graph:
        graph_comp = graph.components.get(refdes)
        refdes_kind = graph_comp.kind if graph_comp is not None else None
        for entry in registry.get("components", []) or []:
            canonical = entry.get("canonical_name", "")
            aliases = entry.get("aliases", []) or []
            reg_kind = entry.get("kind")
            all_names = [canonical] + aliases
            if not any(n and n.lower() in quote_lc for n in all_names):
                continue
            scored = score_refdes_for_canonical(
                refdes=refdes,
                refdes_kind=refdes_kind,
                canonical_name=canonical,
                aliases=aliases,
                reg_kind=reg_kind,
                graph=graph,
            )
            if scored is not None:
                return None

    return Rejection(
        local_id=draft.local_id,
        motive="refdes_not_mentioned_in_quote",
        detail=(
            f"cause.refdes={refdes!r} neither appears in source_quote nor "
            "maps to any registry canonical/alias cited in the quote"
        ),
        original_draft=draft,
    )


def check_rails_mentioned_in_quote(
    draft: ProposedScenarioDraft,
    registry: dict | None = None,
) -> Rejection | None:
    """V2b.2: every rail in expected_dead_rails must be grounded in
    source_quote.

    Accepts EITHER:
      (a) rail label appears literally (case-insensitive) in source_quote.
      (b) a registry signal canonical_name or alias that matches the rail
          label appears literally in source_quote.
    """
    quote_lc = draft.source_quote.lower()
    # Build rail-aliases index from registry.signals (keyed by canonical_name).
    rail_aliases: dict[str, list[str]] = {}
    if registry:
        for s in registry.get("signals", []) or []:
            name = (s.get("canonical_name") or "").lower()
            if not name:
                continue
            aliases = [a for a in (s.get("aliases") or []) if a]
            rail_aliases[name] = aliases

    for rail in draft.expected_dead_rails:
        rail_lc = rail.lower()
        if rail_lc in quote_lc:
            continue
        # Path (b): check if any alias for this rail is in the quote
        aliases = rail_aliases.get(rail_lc, [])
        if aliases and any(a.lower() in quote_lc for a in aliases):
            continue
        return Rejection(
            local_id=draft.local_id,
            motive="rail_not_mentioned_in_quote",
            detail=(
                f"expected rail {rail!r} neither appears in source_quote "
                "nor maps to any registry signal alias cited in the quote"
            ),
            original_draft=draft,
        )
    return None


def check_cause_rail_connection(
    draft: ProposedScenarioDraft, graph: ElectricalGraph
) -> Rejection | None:
    """V2b.3: EVERY rail in expected_dead_rails must be topologically
    connected to cause.refdes — either as its source or as a listed
    decoupling cap. A single unreachable rail fails the scenario.

    This is the real semantic check for cascade validity. Once V2b.1
    has grounded the refdes (literal or via registry bridge) and V2b.3
    has confirmed topology, V2b.2's rail-mentioned-in-quote requirement
    would be redundant and actively harms legitimate cascades where the
    forum quote doesn't name the rail label (almost always the case).

    Known limitation: series supply-chain elements (ferrite beads,
    damping resistors) that appear in the rail chain but aren't in
    `decoupling` will be rejected. This is a false-positive tradeoff
    accepted to eliminate the far-more-common false-positive of the
    LLM attaching an unrelated refdes to a generic rail mention.
    Callers can extend this with a typed-edge walk once the graph's
    edge semantics for supply chains is locked in.
    """
    if not draft.expected_dead_rails:
        return None
    refdes = draft.cause.refdes
    for rail_label in draft.expected_dead_rails:
        rail = graph.power_rails.get(rail_label)
        if rail is None:
            # Topology check already guards this; defer to V3.
            continue
        if rail.source_refdes == refdes:
            continue
        if refdes in (rail.decoupling or []):
            continue
        return Rejection(
            local_id=draft.local_id,
            motive="cause_not_connected_to_rail",
            detail=(
                f"cause.refdes={refdes!r} is neither source nor decoupling cap "
                f"of rail {rail_label!r} (expected_dead_rails={draft.expected_dead_rails})"
            ),
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
    registry: dict | None = None,
) -> tuple[list[ProposedScenarioDraft], list[Rejection]]:
    """V1 → V2 → V2b.1/V2b.2 → V3 → V2b.3 → V4 (per draft, short-circuit
    on first failure) then V5 dedup over the survivors.

    `registry`, when provided, relaxes V2b.1 and V2b.2 to accept matches
    via registry canonical_names / aliases in addition to literal refdes
    or rail mentions. Without it, the strict refdes-in-quote rule applies.
    """
    survivors: list[ProposedScenarioDraft] = []
    rejected: list[Rejection] = []
    for draft in drafts:
        # Per-draft chain, short-circuit on first failure
        rej = check_sanity(draft)  # V1
        if rej is not None:
            rejected.append(rej)
            continue
        rej = check_grounding(draft)  # V2
        if rej is not None:
            rejected.append(rej)
            continue
        rej = check_refdes_mentioned_in_quote(draft, registry, graph)  # V2b.1
        if rej is not None:
            rejected.append(rej)
            continue
        # V2b.2 (rail-mentioned-in-quote) is intentionally skipped. V2b.1
        # grounds the refdes, V2b.3 enforces cause→rail topology, which
        # together cover cascade validity. Requiring the rail label in the
        # quote was over-strict for forum sources that never name rails
        # (e.g. "the screen did not turn on" never says EDP_BL_VCC). See
        # commit message for the audit that motivated removing it.
        rej = check_topology(draft, graph)  # V3
        if rej is not None:
            rejected.append(rej)
            continue
        rej = check_cause_rail_connection(draft, graph)  # V2b.3
        if rej is not None:
            rejected.append(rej)
            continue
        rej = check_pertinence(draft, graph)  # V4
        if rej is not None:
            rejected.append(rej)
            continue
        survivors.append(draft)
    deduped, dup_rejects = check_duplicates(survivors)  # V5
    rejected.extend(dup_rejects)
    return deduped, rejected
