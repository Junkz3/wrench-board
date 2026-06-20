"""Python-level vocabulary drift detection for the knowledge pack.

The former LLM Auditor re-implemented a set diff in natural language. That work
is now deterministic code: we collect canonical identifiers from the Registry,
scan the 3 writer outputs for references, and emit DriftItem entries for any
reference the Registry does not back.

Keeping this pure Python keeps the LLM Auditor focused on what it judges well:
cross-file coherence and plausibility.

GROUND-TRUTH WIDENING (Task 4)
------------------------------
The Registry alone is a poor existence oracle: on real builds the web-derived
glossary covers only ~2 % of a board, so REAL identifiers (rail `PP1V2_S2`,
switch `SWV011`) read as drift and the revise-loop never converges. When a
compiled `GraphTruth` is available we widen the known-universe to
`registry ∪ graph`: an identifier is in-vocabulary if the Registry backs it OR
the schematic graph attests it (`has_component` / `has_net`). This both SILENCES
false drift on genuine schematic identifiers AND — via a free-text rail scan —
CATCHES phantom rails the writers only ever mention in prose, which the
registry-only check never saw because they were never structured names.

The widening is strictly OPTIONAL: with `graph_truth=None` the behaviour is the
pre-existing registry-only set diff (byte-identical reasons), except for the
graph-independent kg-orphan check below, which always runs.
"""

from __future__ import annotations

from collections.abc import Iterable

from api.pipeline.graph_truth import _RAIL_RE, GraphTruth
from api.pipeline.reconcile import find_contradicted_edges
from api.pipeline.schemas import (
    Dictionary,
    DriftItem,
    KnowledgeGraph,
    Registry,
    RulesSet,
)


def compute_drift(
    *,
    registry: Registry,
    knowledge_graph: KnowledgeGraph,
    rules: RulesSet,
    dictionary: Dictionary,
    graph_truth: GraphTruth | None = None,
) -> list[DriftItem]:
    """Return one DriftItem per (file, reason) bucket.

    Checks performed:
      - knowledge_graph: node ids of kind 'component' / 'net' must suffix-match
        a Registry canonical_name (components or signals respectively) — OR, when
        `graph_truth` is supplied, be attested by the compiled schematic graph.
      - rules: every Cause.refdes must be a known component (registry ∪ graph).
      - dictionary: every ComponentSheet.canonical_name must be a known component.
      - rules/dictionary FREE TEXT (graph mode only): any `PP…` rail mentioned in
        prose that exists in neither the registry signals nor the graph → drift.
      - knowledge_graph (always): any node touched by no edge is an orphan → drift.

    Symptoms ('N-S_*') and free-form Rule.symptoms strings are out of registry
    scope by design; the free-text rail scan only fires in graph mode, where the
    graph gives us a real existence oracle to judge a prose rail against.
    """
    component_names = {c.canonical_name for c in registry.components}
    signal_names = {s.canonical_name for s in registry.signals}

    # registry ∪ graph membership. The graph is the second authority only when
    # supplied; in registry-only mode these collapse to the bare set checks and
    # the reason strings keep their legacy text (no "schematic" suffix).
    def _known_component(name: str) -> bool:
        return name in component_names or (
            graph_truth is not None and graph_truth.has_component(name)
        )

    def _known_signal(name: str) -> bool:
        return name in signal_names or (
            graph_truth is not None and graph_truth.has_net(name)
        )

    # When the graph is consulted, the reason must say so — tests assert the word
    # "schematic" appears once a graph backs the universe. The suffix is appended
    # to the legacy reason so the registry-only path stays byte-identical.
    graph_suffix = " nor in the schematic graph" if graph_truth is not None else ""

    drifts: list[DriftItem] = []

    kg_unknown_comp: list[str] = []
    kg_unknown_net: list[str] = []
    for node in knowledge_graph.nodes:
        if node.kind == "component":
            # T8 : les IDs suivent désormais le pattern N-[A-Z0-9_-]{1,48}.
            # On strip le préfixe N- pour retrouver le nom canonique.
            suffix = _strip_prefix(node.id, "N-")
            if suffix is not None and not _known_component(suffix):
                kg_unknown_comp.append(node.id)
        elif node.kind == "net":
            # Le Cartographe émet les nets sous la forme N-NET_<canonical_name>
            # (ex. N-NET_PP3V0). On strip "N-NET_" (6 chars) pour retrouver le
            # canonical_name à comparer avec registry.signals. Avant T8, le strip
            # était "N-" (2 chars) ce qui donnait "NET_PP3V0" ≠ "PP3V0" → faux drift.
            suffix = _strip_prefix(node.id, "N-NET_")
            if suffix is not None and not _known_signal(suffix):
                kg_unknown_net.append(node.id)
    if kg_unknown_comp:
        drifts.append(
            DriftItem(
                file="knowledge_graph",
                mentions=sorted(set(kg_unknown_comp)),
                reason="component node id not in registry.components[canonical_name]"
                + graph_suffix,
            )
        )
    if kg_unknown_net:
        drifts.append(
            DriftItem(
                file="knowledge_graph",
                mentions=sorted(set(kg_unknown_net)),
                reason="net node id not in registry.signals[canonical_name]"
                + graph_suffix,
            )
        )

    rules_unknown: list[str] = []
    for rule in rules.rules:
        for cause in rule.likely_causes:
            if not _known_component(cause.refdes):
                rules_unknown.append(cause.refdes)
    if rules_unknown:
        drifts.append(
            DriftItem(
                file="rules",
                mentions=sorted(set(rules_unknown)),
                reason="Cause.refdes not in registry.components[canonical_name]"
                + graph_suffix,
            )
        )

    dict_unknown: list[str] = []
    for entry in dictionary.entries:
        if not _known_component(entry.canonical_name):
            dict_unknown.append(entry.canonical_name)
    if dict_unknown:
        drifts.append(
            DriftItem(
                file="dictionary",
                mentions=sorted(set(dict_unknown)),
                reason="ComponentSheet.canonical_name not in registry.components[canonical_name]"
                + graph_suffix,
            )
        )

    # --- Free-text rail scan (graph mode only) ----------------------------
    # The registry-only check can't see a rail that lives ONLY in prose — it has
    # no structured entry to diff. With a graph in hand we DO have an existence
    # oracle, so we scan the writers' free text for PP-rail tokens and flag any
    # that the registry signals don't list AND the graph can't attest. Refdes-
    # like tokens in prose are deliberately NOT scanned: their precision is too
    # low (an English word with a digit is indistinguishable from a real refdes),
    # so we'd manufacture false drift. One DriftItem per file bucket.
    if graph_truth is not None:
        rules_rail_drift = _scan_free_text_rails(
            _rules_free_text(rules), signal_names, graph_truth
        )
        if rules_rail_drift:
            drifts.append(
                DriftItem(
                    file="rules",
                    mentions=rules_rail_drift,
                    reason=_RAIL_FREE_TEXT_REASON,
                )
            )
        dict_rail_drift = _scan_free_text_rails(
            _dictionary_free_text(dictionary), signal_names, graph_truth
        )
        if dict_rail_drift:
            drifts.append(
                DriftItem(
                    file="dictionary",
                    mentions=dict_rail_drift,
                    reason=_RAIL_FREE_TEXT_REASON,
                )
            )

    # --- Contradicted power edges (graph mode only) -----------------------
    # A kg `powers`/`drives` edge the compiled graph attributes to a DIFFERENT
    # source-capable IC is a precision defect — the Cartographe over-credited a
    # salient PMIC for a rail a dedicated regulator actually sources (the
    # U7800→PP1V8_S0 / real-source-U8200 class). This needs the connectivity
    # authority, so it only runs with a graph. Each mention names the offending
    # edge AND the graph's real source, so the reviser can RE-ATTRIBUTE rather
    # than guess. The in-line-passive false positives (fuse/series-R/rectifier as
    # "source") are filtered inside find_contradicted_edges, never surfaced here.
    if graph_truth is not None:
        contradicted = find_contradicted_edges(knowledge_graph, graph_truth)
        if contradicted:
            drifts.append(
                DriftItem(
                    file="knowledge_graph",
                    mentions=[
                        f"{c.src} {c.relation} {c.rail} — graph source: "
                        f"{'/'.join(c.graph_sources)}"
                        for c in contradicted
                    ],
                    reason="edge contradicted by the schematic graph: the rail is "
                    "sourced by a different component than the kg claims",
                )
            )

    # --- Orphan kg nodes (always — graph or not) --------------------------
    # A node referenced by no edge contributes nothing to the typed graph; it is
    # a dangling assertion the Cartographe forgot to wire. This is a structural
    # defect independent of any registry/graph membership, so it runs in both
    # modes. An EMPTY kg (no nodes) yields nothing; a kg with nodes but zero
    # edges flags every node.
    # Self-edges (src == dst) are skipped: a node wired only to itself is still
    # dangling — it connects to nothing else in the typed graph, which is exactly
    # the defect this check exists to catch. The Cartographe schema doesn't forbid
    # self-loops, so without this skip a `N-U99 powers N-U99` edge would silently
    # mask an orphan.
    edged_ids: set[str] = set()
    for edge in knowledge_graph.edges:
        if edge.source_id == edge.target_id:
            continue
        edged_ids.add(edge.source_id)
        edged_ids.add(edge.target_id)
    orphans = sorted(
        {node.id for node in knowledge_graph.nodes if node.id not in edged_ids}
    )
    if orphans:
        drifts.append(
            DriftItem(
                file="knowledge_graph",
                mentions=orphans,
                reason="orphan node: id appears in no edge (neither endpoint)",
            )
        )

    return drifts


# Reason for a rail named only in free text that no authority backs. Kept as a
# module constant so the two file buckets (rules/dictionary) emit identical text.
_RAIL_FREE_TEXT_REASON = (
    "rail mentioned in free text exists neither in the registry nor in the "
    "schematic graph"
)


def _scan_free_text_rails(
    texts: Iterable[str | None], signal_names: set[str], graph_truth: GraphTruth
) -> list[str]:
    """Harvest PP-rail tokens from a stream of free-text blobs and return the
    sorted, deduped subset that is unknown to BOTH the registry signals and the
    graph. Empty list → no DriftItem for this bucket."""
    unknown: set[str] = set()
    for text in texts:
        if not text:
            continue
        for tok in _RAIL_RE.findall(text):
            if (
                tok in signal_names
                or graph_truth.has_net(tok)
                or graph_truth.has_net_family(tok)
            ):
                continue
            unknown.add(tok)
    return sorted(unknown)


def _rules_free_text(rules: RulesSet):
    """Yield every free-text blob in the rules file: symptoms, cause mechanisms,
    and each diagnostic step's action + expected."""
    for rule in rules.rules:
        yield from rule.symptoms
        for cause in rule.likely_causes:
            yield cause.mechanism
        for step in rule.diagnostic_steps:
            yield step.action
            yield step.expected


def _dictionary_free_text(dictionary: Dictionary):
    """Yield every free-text blob in the dictionary: each sheet's role, notes,
    and typical failure modes."""
    for entry in dictionary.entries:
        yield entry.role
        yield entry.notes
        yield from entry.typical_failure_modes


def _strip_prefix(value: str, prefix: str) -> str | None:
    if value.startswith(prefix):
        return value[len(prefix) :]
    return None
