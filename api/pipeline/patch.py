"""Deterministic applicator for reviser patches.

Pure, sync, no IO, no LLM. A reviser emits a typed delta (`KnowledgeGraphPatch`
/ `RulesPatch` / `DictionaryPatch` in `schemas`); these functions apply it to
the current artefact and return a NEW, schema-validated artefact. Records the
patch does not name are preserved verbatim — that is the whole point: the full
re-emit's collateral-regression surface does not exist here.

Op order within one patch is fixed and documented per function: updates, then
removes, then adds. Removes-before-adds lets a record be replaced by
remove+add (re-adding a just-removed id is legal).

Failure policy: a well-formed-but-inapplicable patch (add of an id that already
exists, update/remove of an id semantics aside, or an edge that would dangle)
raises `PatchApplyError`. The caller (`run_single_writer_revision`) treats that
as a no-op for the file and lets the re-audit re-flag — nothing corrupts.
"""

from __future__ import annotations

from api.pipeline.schemas import (
    Dictionary,
    DictionaryPatch,
    KnowledgeEdge,
    KnowledgeGraph,
    KnowledgeGraphPatch,
    KnowledgeNode,
    Rule,
    RulesPatch,
    RulesSet,
)


class PatchApplyError(Exception):
    """A well-formed patch that cannot apply cleanly to the current artefact.

    Raised on a referential or addressing conflict (add of an existing id,
    update/remove dangling an edge, edge to a non-existent node). The caller
    degrades this to a safe no-op for the file.
    """


def _edge_key(edge: KnowledgeEdge) -> tuple[str, str, str]:
    return (edge.source_id, edge.target_id, edge.relation)


def apply_kg_patch(current: KnowledgeGraph, patch: KnowledgeGraphPatch) -> KnowledgeGraph:
    """Apply a `KnowledgeGraphPatch`, returning a fresh validated `KnowledgeGraph`.

    Nodes are keyed by `id` (order-preserving); edges are a set keyed by
    (source_id, target_id, relation). After all ops, every edge endpoint must
    reference a node that still exists, else `PatchApplyError`.
    """
    nodes: dict[str, KnowledgeNode] = {n.id: n for n in current.nodes}

    # 1. updates (must exist) — in place, order preserved.
    for node in patch.update_nodes:
        if node.id not in nodes:
            raise PatchApplyError(f"update_nodes: node id {node.id!r} does not exist")
        nodes[node.id] = node
    # 2. removes (skip unknown).
    for node_id in patch.remove_node_ids:
        nodes.pop(node_id, None)
    # 3. adds (must not collide with a surviving node).
    for node in patch.add_nodes:
        if node.id in nodes:
            raise PatchApplyError(
                f"add_nodes: node id {node.id!r} already exists (use update_nodes)"
            )
        nodes[node.id] = node

    # Edges: order-preserving set. Drop removed, then append adds not already present.
    removed_keys = {_edge_key(e) for e in patch.remove_edges}
    edges: list[KnowledgeEdge] = [e for e in current.edges if _edge_key(e) not in removed_keys]
    present = {_edge_key(e) for e in edges}
    for edge in patch.add_edges:
        key = _edge_key(edge)
        if key not in present:  # re-adding an identical edge is a no-op, not an error
            edges.append(edge)
            present.add(key)

    # Referential integrity — no dangling endpoints.
    node_ids = set(nodes)
    for edge in edges:
        for endpoint in (edge.source_id, edge.target_id):
            if endpoint not in node_ids:
                raise PatchApplyError(
                    f"edge {_edge_key(edge)} references unknown node {endpoint!r}"
                )

    return KnowledgeGraph(nodes=list(nodes.values()), edges=edges)


def apply_rules_patch(current: RulesSet, patch: RulesPatch) -> RulesSet:
    """Apply a `RulesPatch`, returning a fresh validated `RulesSet`. Rules keyed by `id`."""
    rules: dict[str, Rule] = {r.id: r for r in current.rules}

    for rule in patch.update_rules:
        if rule.id not in rules:
            raise PatchApplyError(f"update_rules: rule id {rule.id!r} does not exist")
        rules[rule.id] = rule
    for rule_id in patch.remove_rule_ids:
        rules.pop(rule_id, None)
    for rule in patch.add_rules:
        if rule.id in rules:
            raise PatchApplyError(
                f"add_rules: rule id {rule.id!r} already exists (use update_rules)"
            )
        rules[rule.id] = rule

    return RulesSet(rules=list(rules.values()))


def apply_dictionary_patch(current: Dictionary, patch: DictionaryPatch) -> Dictionary:
    """Apply a `DictionaryPatch`, returning a fresh validated `Dictionary`.

    Entries keyed by `canonical_name`.
    """
    entries = {e.canonical_name: e for e in current.entries}

    for entry in patch.update_entries:
        if entry.canonical_name not in entries:
            raise PatchApplyError(
                f"update_entries: canonical_name {entry.canonical_name!r} does not exist"
            )
        entries[entry.canonical_name] = entry
    for name in patch.remove_entry_names:
        entries.pop(name, None)
    for entry in patch.add_entries:
        if entry.canonical_name in entries:
            raise PatchApplyError(
                f"add_entries: canonical_name {entry.canonical_name!r} already exists "
                "(use update_entries)"
            )
        entries[entry.canonical_name] = entry

    return Dictionary(entries=list(entries.values()))
