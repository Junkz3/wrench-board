"""Unit tests for compute_drift — the Python set-diff that replaced the
LLM Auditor's vocabulary check.
"""

from __future__ import annotations

from api.pipeline.drift import compute_drift
from api.pipeline.schemas import (
    Cause,
    ComponentSheet,
    Dictionary,
    KnowledgeEdge,
    KnowledgeGraph,
    KnowledgeNode,
    Registry,
    RegistryComponent,
    RegistrySignal,
    Rule,
    RulesSet,
)


def _base_registry() -> Registry:
    # T8 : kind en majuscules (PMIC, CAPACITOR), canonical_name toujours uppercase
    return Registry(
        device_label="Demo",
        components=[
            RegistryComponent(canonical_name="U7", kind="PMIC"),
            RegistryComponent(canonical_name="C29", kind="CAPACITOR"),
        ],
        signals=[RegistrySignal(canonical_name="3V3_RAIL", kind="POWER_RAIL")],
    )


def test_drift_empty_when_everything_matches():
    registry = _base_registry()
    kg = KnowledgeGraph(
        nodes=[
            # T8 : KnowledgeNode.id doit suivre le pattern N-[A-Z0-9_-]{1,48}.
            # Les nets suivent le sous-pattern N-NET_<canonical_name> (le Cartographe
            # émet N-NET_PP3V0, etc.) — compute_drift doit strip "N-NET_" pour
            # retrouver le canonical_name "3V3_RAIL" dans registry.signals.
            KnowledgeNode(id="N-U7", kind="component", label="PMIC"),
            KnowledgeNode(id="N-NET_3V3_RAIL", kind="net", label="3V3 rail"),
            KnowledgeNode(id="N-3V3-DEAD", kind="symptom", label="3V3 dead"),
        ],
        edges=[KnowledgeEdge(source_id="N-U7", target_id="N-NET_3V3_RAIL", relation="powers")],
    )
    rules = RulesSet(
        rules=[
            Rule(
                # T8 : Rule.id doit suivre le pattern R-[A-Z0-9_-]{1,48}
                id="R-DEMO-001",
                symptoms=["3V3 dead"],
                likely_causes=[Cause(refdes="U7", probability=0.8, mechanism="short")],
                confidence=0.8,
            )
        ]
    )
    dictionary = Dictionary(entries=[ComponentSheet(canonical_name="U7")])

    assert compute_drift(
        registry=registry, knowledge_graph=kg, rules=rules, dictionary=dictionary
    ) == []


def test_drift_detects_unknown_component_in_graph():
    registry = _base_registry()
    kg = KnowledgeGraph(
        nodes=[KnowledgeNode(id="N-U99", kind="component", label="Mystery")],
        edges=[],
    )
    rules = RulesSet(rules=[])
    dictionary = Dictionary(entries=[])

    drift = compute_drift(
        registry=registry, knowledge_graph=kg, rules=rules, dictionary=dictionary
    )
    assert len(drift) == 1
    assert drift[0].file == "knowledge_graph"
    # T8 : les IDs suivent le pattern N-[A-Z0-9_-]{1,48}
    assert drift[0].mentions == ["N-U99"]


def test_drift_detects_unknown_net_in_graph():
    registry = _base_registry()
    kg = KnowledgeGraph(
        # T8 : les nets suivent le pattern N-NET_<canonical_name> (le Cartographe
        # émet N-NET_PP3V0, N-NET_1V8_UNREGISTERED, etc.).
        nodes=[KnowledgeNode(id="N-NET_1V8_UNREGISTERED", kind="net", label="1.8V")],
        edges=[],
    )
    drift = compute_drift(
        registry=registry,
        knowledge_graph=kg,
        rules=RulesSet(rules=[]),
        dictionary=Dictionary(entries=[]),
    )
    assert len(drift) == 1
    assert drift[0].file == "knowledge_graph"
    assert drift[0].mentions == ["N-NET_1V8_UNREGISTERED"]


def test_drift_detects_unknown_cause_refdes():
    registry = _base_registry()
    rules = RulesSet(
        rules=[
            Rule(
                # T8 : Rule.id suit le pattern R-[A-Z0-9_-]{1,48}
                id="R-BOOT-001",
                symptoms=["boot loop"],
                likely_causes=[
                    Cause(refdes="U7", probability=0.5, mechanism="brownout"),
                    Cause(refdes="Q42", probability=0.3, mechanism="short"),
                ],
                confidence=0.6,
            )
        ]
    )
    drift = compute_drift(
        registry=registry,
        knowledge_graph=KnowledgeGraph(nodes=[], edges=[]),
        rules=rules,
        dictionary=Dictionary(entries=[]),
    )
    assert len(drift) == 1
    assert drift[0].file == "rules"
    assert drift[0].mentions == ["Q42"]


def test_drift_detects_unknown_dictionary_entry():
    registry = _base_registry()
    dictionary = Dictionary(entries=[ComponentSheet(canonical_name="U7"), ComponentSheet(canonical_name="Z1")])
    drift = compute_drift(
        registry=registry,
        knowledge_graph=KnowledgeGraph(nodes=[], edges=[]),
        rules=RulesSet(rules=[]),
        dictionary=dictionary,
    )
    assert len(drift) == 1
    assert drift[0].file == "dictionary"
    assert drift[0].mentions == ["Z1"]


def test_drift_dedups_repeated_mentions():
    registry = _base_registry()
    kg = KnowledgeGraph(
        nodes=[
            # T8 : IDs conformes au pattern N-[A-Z0-9_-]{1,48}
            KnowledgeNode(id="N-U99", kind="component", label="a"),
            KnowledgeNode(id="N-U99", kind="component", label="b"),
        ],
        edges=[],
    )
    drift = compute_drift(
        registry=registry,
        knowledge_graph=kg,
        rules=RulesSet(rules=[]),
        dictionary=Dictionary(entries=[]),
    )
    assert drift[0].mentions == ["N-U99"]


def test_drift_ignores_symptom_nodes():
    registry = _base_registry()
    kg = KnowledgeGraph(
        # T8 : ID conforme au pattern N-[A-Z0-9_-]{1,48}
        nodes=[KnowledgeNode(id="N-SYM-ANYTHING", kind="symptom", label="x")],
        edges=[],
    )
    drift = compute_drift(
        registry=registry,
        knowledge_graph=kg,
        rules=RulesSet(rules=[]),
        dictionary=Dictionary(entries=[]),
    )
    assert drift == []
