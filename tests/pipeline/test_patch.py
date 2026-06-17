"""Unit tests for the deterministic reviser-patch applicator.

These are pure-function tests — no LLM, no IO. They pin the core invariant of
the surgical reviser: a patch touches ONLY the records it names, and records it
does not name come out byte-identical. They also pin the failure policy: a
well-formed-but-inapplicable patch raises `PatchApplyError` (the caller turns
that into a safe no-op).
"""

from __future__ import annotations

import pytest

from api.pipeline.patch import (
    PatchApplyError,
    apply_dictionary_patch,
    apply_kg_patch,
    apply_rules_patch,
)
from api.pipeline.schemas import (
    Cause,
    ComponentSheet,
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

# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def _node(node_id: str, kind: str = "component", label: str | None = None) -> KnowledgeNode:
    return KnowledgeNode(id=node_id, kind=kind, label=label or node_id)


def _edge(src: str, tgt: str, relation: str = "powers") -> KnowledgeEdge:
    return KnowledgeEdge(source_id=src, target_id=tgt, relation=relation)


def _rule(rule_id: str, symptom: str = "no power") -> Rule:
    return Rule(
        id=rule_id,
        symptoms=[symptom],
        likely_causes=[Cause(refdes="U1", probability=0.5, mechanism="short")],
    )


def _sheet(name: str, role: str | None = "buck") -> ComponentSheet:
    return ComponentSheet(canonical_name=name, role=role)


def _kg() -> KnowledgeGraph:
    return KnowledgeGraph(
        nodes=[_node("N-U1"), _node("N-U2"), _node("N-RAIL", kind="net"), _node("N-ORPHAN")],
        edges=[_edge("N-U1", "N-RAIL"), _edge("N-U2", "N-RAIL")],
    )


# --------------------------------------------------------------------------
# Knowledge graph — the orphan-connect case the regression failed on
# --------------------------------------------------------------------------


def test_kg_add_edge_connects_orphan_and_leaves_rest_byte_identical():
    kg = _kg()
    before = {n.id: n.model_dump_json() for n in kg.nodes}

    patched = apply_kg_patch(kg, KnowledgeGraphPatch(add_edges=[_edge("N-ORPHAN", "N-RAIL")]))

    # The orphan is now connected; every node is preserved verbatim.
    after = {n.id: n.model_dump_json() for n in patched.nodes}
    assert after == before  # not one node mutated
    assert _edge("N-ORPHAN", "N-RAIL").model_dump() in [e.model_dump() for e in patched.edges]
    assert len(patched.edges) == 3


def test_kg_empty_patch_is_noop():
    kg = _kg()
    patched = apply_kg_patch(kg, KnowledgeGraphPatch())
    assert patched.model_dump_json() == kg.model_dump_json()


def test_kg_add_node_then_edge_in_same_patch():
    kg = _kg()
    patched = apply_kg_patch(
        kg,
        KnowledgeGraphPatch(
            add_nodes=[_node("N-Q1")],
            add_edges=[_edge("N-Q1", "N-RAIL")],
        ),
    )
    assert "N-Q1" in {n.id for n in patched.nodes}
    assert len(patched.edges) == 3


def test_kg_add_duplicate_node_raises():
    kg = _kg()
    with pytest.raises(PatchApplyError, match="already exists"):
        apply_kg_patch(kg, KnowledgeGraphPatch(add_nodes=[_node("N-U1")]))


def test_kg_update_node_replaces_by_id():
    kg = _kg()
    patched = apply_kg_patch(
        kg, KnowledgeGraphPatch(update_nodes=[_node("N-U1", label="U1 (corrected)")])
    )
    u1 = next(n for n in patched.nodes if n.id == "N-U1")
    assert u1.label == "U1 (corrected)"
    # Order preserved — update is in place, not append.
    assert [n.id for n in patched.nodes] == ["N-U1", "N-U2", "N-RAIL", "N-ORPHAN"]


def test_kg_update_missing_node_raises():
    kg = _kg()
    with pytest.raises(PatchApplyError, match="does not exist"):
        apply_kg_patch(kg, KnowledgeGraphPatch(update_nodes=[_node("N-NOPE")]))


def test_kg_remove_node_unknown_is_skipped():
    kg = _kg()
    patched = apply_kg_patch(kg, KnowledgeGraphPatch(remove_node_ids=["N-ORPHAN", "N-GHOST"]))
    assert "N-ORPHAN" not in {n.id for n in patched.nodes}
    assert len(patched.nodes) == 3


def test_kg_remove_edge_matched_on_triple_unknown_skipped():
    kg = _kg()
    patched = apply_kg_patch(
        kg,
        KnowledgeGraphPatch(
            remove_edges=[_edge("N-U1", "N-RAIL"), _edge("N-U9", "N-RAIL")]
        ),
    )
    keys = {(e.source_id, e.target_id, e.relation) for e in patched.edges}
    assert ("N-U1", "N-RAIL", "powers") not in keys
    assert ("N-U2", "N-RAIL", "powers") in keys


def test_kg_edge_add_is_idempotent():
    kg = _kg()
    patched = apply_kg_patch(kg, KnowledgeGraphPatch(add_edges=[_edge("N-U1", "N-RAIL")]))
    # Already present — no duplicate, no error.
    assert len(patched.edges) == 2


def test_kg_add_edge_to_unknown_node_raises_integrity():
    kg = _kg()
    with pytest.raises(PatchApplyError, match="unknown node"):
        apply_kg_patch(kg, KnowledgeGraphPatch(add_edges=[_edge("N-U1", "N-GHOST")]))


def test_kg_remove_node_still_referenced_by_edge_raises_integrity():
    kg = _kg()
    # Removing N-RAIL while N-U1→N-RAIL and N-U2→N-RAIL still exist dangles them.
    with pytest.raises(PatchApplyError, match="unknown node"):
        apply_kg_patch(kg, KnowledgeGraphPatch(remove_node_ids=["N-RAIL"]))


def test_kg_remove_node_and_its_edges_together_is_clean():
    kg = _kg()
    patched = apply_kg_patch(
        kg,
        KnowledgeGraphPatch(
            remove_node_ids=["N-RAIL"],
            remove_edges=[_edge("N-U1", "N-RAIL"), _edge("N-U2", "N-RAIL")],
        ),
    )
    assert "N-RAIL" not in {n.id for n in patched.nodes}
    assert patched.edges == []


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


def test_rules_update_replaces_and_preserves_siblings():
    rules = RulesSet(rules=[_rule("R-A"), _rule("R-B"), _rule("R-C")])
    before_b = next(r for r in rules.rules if r.id == "R-B").model_dump_json()

    fixed = _rule("R-A", symptom="no power on adapter connect")
    patched = apply_rules_patch(rules, RulesPatch(update_rules=[fixed]))

    assert next(r for r in patched.rules if r.id == "R-A").symptoms == [
        "no power on adapter connect"
    ]
    # The untouched sibling is byte-identical.
    assert next(r for r in patched.rules if r.id == "R-B").model_dump_json() == before_b


def test_rules_add_duplicate_raises():
    rules = RulesSet(rules=[_rule("R-A")])
    with pytest.raises(PatchApplyError, match="already exists"):
        apply_rules_patch(rules, RulesPatch(add_rules=[_rule("R-A")]))


def test_rules_update_missing_raises():
    rules = RulesSet(rules=[_rule("R-A")])
    with pytest.raises(PatchApplyError, match="does not exist"):
        apply_rules_patch(rules, RulesPatch(update_rules=[_rule("R-Z")]))


def test_rules_remove_unknown_skipped():
    rules = RulesSet(rules=[_rule("R-A"), _rule("R-B")])
    patched = apply_rules_patch(rules, RulesPatch(remove_rule_ids=["R-B", "R-GHOST"]))
    assert {r.id for r in patched.rules} == {"R-A"}


# --------------------------------------------------------------------------
# Dictionary
# --------------------------------------------------------------------------


def test_dictionary_add_update_remove():
    d = Dictionary(entries=[_sheet("U1"), _sheet("U2")])
    patched = apply_dictionary_patch(
        d,
        DictionaryPatch(
            add_entries=[_sheet("Q1")],
            update_entries=[_sheet("U1", role="pmic")],
            remove_entry_names=["U2"],
        ),
    )
    names = {e.canonical_name for e in patched.entries}
    assert names == {"U1", "Q1"}
    assert next(e for e in patched.entries if e.canonical_name == "U1").role == "pmic"


def test_dictionary_add_duplicate_raises():
    d = Dictionary(entries=[_sheet("U1")])
    with pytest.raises(PatchApplyError, match="already exists"):
        apply_dictionary_patch(d, DictionaryPatch(add_entries=[_sheet("U1")]))


def test_dictionary_update_missing_raises():
    d = Dictionary(entries=[_sheet("U1")])
    with pytest.raises(PatchApplyError, match="does not exist"):
        apply_dictionary_patch(d, DictionaryPatch(update_entries=[_sheet("U9")]))
