"""Python-level vocabulary drift detection for the knowledge pack.

The former LLM Auditor re-implemented a set diff in natural language. That work
is now deterministic code: we collect canonical identifiers from the Registry,
scan the 3 writer outputs for references, and emit DriftItem entries for any
reference the Registry does not back.

Keeping this pure Python keeps the LLM Auditor focused on what it judges well:
cross-file coherence and plausibility.
"""

from __future__ import annotations

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
) -> list[DriftItem]:
    """Return one DriftItem per (file, reason) bucket.

    Checks performed:
      - knowledge_graph: node ids of kind 'component' / 'net' must suffix-match
        a Registry canonical_name (components or signals respectively).
      - rules: every Cause.refdes must match a component canonical_name.
      - dictionary: every ComponentSheet.canonical_name must match.

    Symptoms ('sym:*') and free-form Rule.symptoms strings are out of scope —
    they are not indexed in the Registry by design.
    """
    component_names = {c.canonical_name for c in registry.components}
    signal_names = {s.canonical_name for s in registry.signals}

    drifts: list[DriftItem] = []

    kg_unknown_comp: list[str] = []
    kg_unknown_net: list[str] = []
    for node in knowledge_graph.nodes:
        if node.kind == "component":
            suffix = _strip_prefix(node.id, "comp:")
            if suffix is not None and suffix not in component_names:
                kg_unknown_comp.append(node.id)
        elif node.kind == "net":
            suffix = _strip_prefix(node.id, "net:")
            if suffix is not None and suffix not in signal_names:
                kg_unknown_net.append(node.id)
    if kg_unknown_comp:
        drifts.append(
            DriftItem(
                file="knowledge_graph",
                mentions=sorted(set(kg_unknown_comp)),
                reason="component node id not in registry.components[canonical_name]",
            )
        )
    if kg_unknown_net:
        drifts.append(
            DriftItem(
                file="knowledge_graph",
                mentions=sorted(set(kg_unknown_net)),
                reason="net node id not in registry.signals[canonical_name]",
            )
        )

    rules_unknown: list[str] = []
    for rule in rules.rules:
        for cause in rule.likely_causes:
            if cause.refdes not in component_names:
                rules_unknown.append(cause.refdes)
    if rules_unknown:
        drifts.append(
            DriftItem(
                file="rules",
                mentions=sorted(set(rules_unknown)),
                reason="Cause.refdes not in registry.components[canonical_name]",
            )
        )

    dict_unknown: list[str] = []
    for entry in dictionary.entries:
        if entry.canonical_name not in component_names:
            dict_unknown.append(entry.canonical_name)
    if dict_unknown:
        drifts.append(
            DriftItem(
                file="dictionary",
                mentions=sorted(set(dict_unknown)),
                reason="ComponentSheet.canonical_name not in registry.components[canonical_name]",
            )
        )

    return drifts


def _strip_prefix(value: str, prefix: str) -> str | None:
    if value.startswith(prefix):
        return value[len(prefix) :]
    return None
