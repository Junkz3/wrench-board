"""Unit tests for compute_drift — the Python set-diff that replaced the
LLM Auditor's vocabulary check.
"""

from __future__ import annotations

from api.pipeline.drift import compute_drift
from api.pipeline.graph_truth import GraphTruth
from api.pipeline.schemas import (
    Cause,
    ComponentSheet,
    DiagnosticStep,
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
from api.pipeline.schematic.schemas import (
    ComponentNode,
    ElectricalGraph,
    NetNode,
    PowerRail,
    SchematicQualityReport,
    TypedEdge,
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
        # The symptom node is wired to the net it indicates so the new orphan
        # check stays quiet — this "everything matches" fixture must be edge-clean.
        edges=[
            KnowledgeEdge(source_id="N-U7", target_id="N-NET_3V3_RAIL", relation="powers"),
            KnowledgeEdge(
                source_id="N-NET_3V3_RAIL", target_id="N-3V3-DEAD", relation="indicates"
            ),
        ],
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
        nodes=[
            KnowledgeNode(id="N-U99", kind="component", label="Mystery"),
            # A registry-known net so N-U99 can be wired to a real other node:
            # the edge keeps BOTH nodes non-orphan honestly (no self-loop) so the
            # orphan check stays quiet and this test asserts ONLY the membership drift.
            KnowledgeNode(id="N-NET_3V3_RAIL", kind="net", label="3V3 rail"),
        ],
        edges=[
            KnowledgeEdge(
                source_id="N-U99", target_id="N-NET_3V3_RAIL", relation="powers"
            )
        ],
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
        nodes=[
            KnowledgeNode(id="N-NET_1V8_UNREGISTERED", kind="net", label="1.8V"),
            # A registry-known component so the net is wired to a real other node
            # (no self-loop) → neither node is an orphan; this isolates the
            # net-membership drift assertion.
            KnowledgeNode(id="N-U7", kind="component", label="PMIC"),
        ],
        edges=[
            KnowledgeEdge(
                source_id="N-U7",
                target_id="N-NET_1V8_UNREGISTERED",
                relation="powers",
            )
        ],
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
            # A registry-known net so N-U99 is wired to a real other node (no
            # self-loop) → non-orphan; this test only exercises mention dedup.
            KnowledgeNode(id="N-NET_3V3_RAIL", kind="net", label="3V3 rail"),
        ],
        edges=[
            KnowledgeEdge(
                source_id="N-U99", target_id="N-NET_3V3_RAIL", relation="powers"
            )
        ],
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
        nodes=[
            KnowledgeNode(id="N-SYM-ANYTHING", kind="symptom", label="x"),
            # A registry-known net so the symptom indicates a real other node (no
            # self-loop) → non-orphan; isolates the "symptom nodes are ignored"
            # assertion.
            KnowledgeNode(id="N-NET_3V3_RAIL", kind="net", label="3V3 rail"),
        ],
        edges=[
            KnowledgeEdge(
                source_id="N-NET_3V3_RAIL",
                target_id="N-SYM-ANYTHING",
                relation="indicates",
            )
        ],
    )
    drift = compute_drift(
        registry=registry,
        knowledge_graph=kg,
        rules=RulesSet(rules=[]),
        dictionary=Dictionary(entries=[]),
    )
    assert drift == []


# ======================================================================
# Task 4 — graph-truth widening + free-text rail scan + kg orphan check
# ======================================================================


def _mini_graph() -> ElectricalGraph:
    """Mirror of tests/pipeline/test_graph_truth.py::_mini_graph — a tiny board
    whose REAL identifiers (switch SWV011, rail PP1V2_S2) are absent from the
    thin web Registry, so they exercise the registry∪graph widening."""
    return ElectricalGraph(
        device_slug="mini",
        components={
            "U8100": ComponentNode(refdes="U8100", type="ic", kind="ic", role="pmic", pages=[3]),
            "SWV011": ComponentNode(refdes="SWV011", type="switch", role="load_switch", pages=[5]),
            "C0042": ComponentNode(refdes="C0042", type="capacitor", kind="passive_c", pages=[3]),
        },
        nets={
            "PP1V2_S2": NetNode(label="PP1V2_S2", is_power=True),
            "PP1V8_S2": NetNode(label="PP1V8_S2", is_power=True),
            "SIG_EN": NetNode(label="SIG_EN"),
        },
        power_rails={
            "PP1V2_S2": PowerRail(
                label="PP1V2_S2",
                voltage_nominal=1.2,
                source_refdes="U8100",
                consumers=["C0042"],
            ),
        },
        typed_edges=[
            TypedEdge(src="U8100", dst="PP1V2_S2", kind="powers"),
            TypedEdge(src="PP1V2_S2", dst="C0042", kind="powers"),
        ],
        quality=SchematicQualityReport(total_pages=5, pages_parsed=5),
    )


class TestGraphTruthWidening:
    def test_graph_backed_identifier_is_not_drift(self):
        """A kg component node absent from the Registry but ATTESTED by the
        graph (SWV011) must NOT be flagged — the universe is registry∪graph."""
        registry = _base_registry()  # has U7/C29, never SWV011
        gt = GraphTruth(_mini_graph())
        kg = KnowledgeGraph(
            nodes=[
                KnowledgeNode(id="N-SWV011", kind="component", label="load switch"),
                # A graph-attested net (PP1V2_S2 ∈ _mini_graph nets) so the switch
                # is wired to a real other node (no self-loop) → non-orphan; both
                # ids are graph-known so ONLY the membership widening is exercised.
                KnowledgeNode(id="N-NET_PP1V2_S2", kind="net", label="1.2V rail"),
            ],
            edges=[
                KnowledgeEdge(
                    source_id="N-SWV011", target_id="N-NET_PP1V2_S2", relation="powers"
                )
            ],
        )
        drift = compute_drift(
            registry=registry,
            knowledge_graph=kg,
            rules=RulesSet(rules=[]),
            dictionary=Dictionary(entries=[]),
            graph_truth=gt,
        )
        assert all("SWV011" not in m for d in drift for m in d.mentions)

    def test_unknown_to_both_registry_and_graph_still_drifts(self):
        """A node unknown to BOTH the Registry and the graph (N-U9999) is real
        drift; with graph_truth set, the reason names the schematic graph too."""
        registry = _base_registry()
        gt = GraphTruth(_mini_graph())
        kg = KnowledgeGraph(
            nodes=[
                KnowledgeNode(id="N-U9999", kind="component", label="phantom"),
                # A graph-attested net (PP1V2_S2 ∈ _mini_graph nets) so the phantom
                # is wired to a real other node (no self-loop) → neither is an
                # orphan; only N-U9999's membership drift remains.
                KnowledgeNode(id="N-NET_PP1V2_S2", kind="net", label="1.2V rail"),
            ],
            edges=[
                KnowledgeEdge(
                    source_id="N-U9999", target_id="N-NET_PP1V2_S2", relation="powers"
                )
            ],
        )
        drift = compute_drift(
            registry=registry,
            knowledge_graph=kg,
            rules=RulesSet(rules=[]),
            dictionary=Dictionary(entries=[]),
            graph_truth=gt,
        )
        comp_drift = [d for d in drift if d.mentions == ["N-U9999"]]
        assert len(comp_drift) == 1
        assert "schematic" in comp_drift[0].reason

    def test_free_text_rail_unknown_to_both_drifts(self):
        """A rail cited only in free text (dictionary role) that exists in
        neither the Registry nor the graph → a free-text-scan DriftItem."""
        registry = _base_registry()
        gt = GraphTruth(_mini_graph())
        dictionary = Dictionary(
            entries=[
                ComponentSheet(
                    canonical_name="U7",  # known → no membership drift
                    role="Regulator feeding the PP9V9_FAKE rail under load.",
                )
            ]
        )
        drift = compute_drift(
            registry=registry,
            knowledge_graph=KnowledgeGraph(nodes=[], edges=[]),
            rules=RulesSet(rules=[]),
            dictionary=dictionary,
            graph_truth=gt,
        )
        rail_drift = [
            d
            for d in drift
            if d.file == "dictionary" and "PP9V9_FAKE" in d.mentions
        ]
        assert len(rail_drift) == 1
        assert "schematic" in rail_drift[0].reason
        assert "free text" in rail_drift[0].reason

    def test_free_text_rail_in_rules_bucket_drifts(self):
        """Le scan texte-libre couvre aussi le bucket RULES — un rail fantôme
        dans `DiagnosticStep.expected` (le seul champ nullable du flux rules,
        pour épingler le guard `if not text`) doit produire un DriftItem."""
        registry = _base_registry()
        gt = GraphTruth(_mini_graph())
        rules = RulesSet(
            rules=[
                Rule(
                    id="R-FREETEXT-001",
                    symptoms=["No boot"],
                    likely_causes=[
                        Cause(refdes="U7", probability=0.5, mechanism="dead die")
                    ],
                    diagnostic_steps=[
                        DiagnosticStep(action="Probe the rail", expected=None),
                        DiagnosticStep(
                            action="Probe again",
                            expected="PP9V9_FAKE at 9.9V",
                        ),
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
            graph_truth=gt,
        )
        rail_drift = [
            d for d in drift if d.file == "rules" and "PP9V9_FAKE" in d.mentions
        ]
        assert len(rail_drift) == 1
        assert "free text" in rail_drift[0].reason

    def test_free_text_rail_in_graph_but_not_registry_is_not_drift(self):
        """A rail in the graph but missing from the Registry (PP1V2_S2) cited in
        free text must NOT drift — the graph attests it."""
        registry = _base_registry()  # signals = {3V3_RAIL}, no PP1V2_S2
        gt = GraphTruth(_mini_graph())
        dictionary = Dictionary(
            entries=[
                ComponentSheet(
                    canonical_name="U7",
                    role="Sources the PP1V2_S2 rail at 1.2 V.",
                )
            ]
        )
        drift = compute_drift(
            registry=registry,
            knowledge_graph=KnowledgeGraph(nodes=[], edges=[]),
            rules=RulesSet(rules=[]),
            dictionary=dictionary,
            graph_truth=gt,
        )
        assert all("PP1V2_S2" not in m for d in drift for m in d.mentions)

    def test_free_text_rail_family_shorthand_is_not_drift(self):
        """A rail FAMILY shorthand in prose (PP1V2) whose only concrete member
        in the graph carries a suffix (PP1V2_S2) must NOT drift — technicians
        name the rail family, the graph attests a member `PP1V2_*`."""
        registry = _base_registry()  # signals = {3V3_RAIL}, no PP1V2*
        gt = GraphTruth(_mini_graph())  # graph net = PP1V2_S2
        dictionary = Dictionary(
            entries=[
                ComponentSheet(
                    canonical_name="U7",
                    role="Regulator feeding the PP1V2 rail family under load.",
                )
            ]
        )
        drift = compute_drift(
            registry=registry,
            knowledge_graph=KnowledgeGraph(nodes=[], edges=[]),
            rules=RulesSet(rules=[]),
            dictionary=dictionary,
            graph_truth=gt,
        )
        assert all("PP1V2" not in m for d in drift for m in d.mentions)

    def test_free_text_rail_partial_prefix_without_separator_still_drifts(self):
        """A token that is a bare character prefix of a net but NOT a family
        shorthand (PP1V — no `PP1V_*` member, only PP1V2_S2) must still drift:
        the `_` separator guard stops PP1V from masquerading as PP1V2_S2's
        family."""
        registry = _base_registry()
        gt = GraphTruth(_mini_graph())
        dictionary = Dictionary(
            entries=[
                ComponentSheet(
                    canonical_name="U7",
                    role="Feeds the PP1V rail.",
                )
            ]
        )
        drift = compute_drift(
            registry=registry,
            knowledge_graph=KnowledgeGraph(nodes=[], edges=[]),
            rules=RulesSet(rules=[]),
            dictionary=dictionary,
            graph_truth=gt,
        )
        assert any("PP1V" in m for d in drift for m in d.mentions)

    def test_orphan_node_without_graph_drifts(self):
        """A kg node touched by NO edge is an orphan → drift, independent of any
        graph. Here every id is registry-known so ONLY the orphan check fires."""
        registry = _base_registry()
        kg = KnowledgeGraph(
            nodes=[
                KnowledgeNode(id="N-U7", kind="component", label="PMIC"),
                KnowledgeNode(id="N-C29", kind="component", label="cap"),
            ],
            edges=[],  # both nodes orphaned
        )
        drift = compute_drift(
            registry=registry,
            knowledge_graph=kg,
            rules=RulesSet(rules=[]),
            dictionary=Dictionary(entries=[]),
        )
        orphan = [d for d in drift if "orphan" in d.reason]
        assert len(orphan) == 1
        assert orphan[0].file == "knowledge_graph"
        assert orphan[0].mentions == ["N-C29", "N-U7"]

    def test_self_loop_only_node_is_orphan(self):
        """A node whose ONLY edge is a self-loop (N-U7 powers N-U7) is still
        semantically dangling — a self-edge wires the node to nothing else, so
        it must trip the orphan check. The Cartographe schema does not forbid
        self-loops, so the orphan check must skip self-edges when computing the
        edged set."""
        registry = _base_registry()
        kg = KnowledgeGraph(
            nodes=[KnowledgeNode(id="N-U7", kind="component", label="PMIC")],
            edges=[
                KnowledgeEdge(source_id="N-U7", target_id="N-U7", relation="powers")
            ],
        )
        drift = compute_drift(
            registry=registry,
            knowledge_graph=kg,
            rules=RulesSet(rules=[]),
            dictionary=Dictionary(entries=[]),
        )
        orphan = [d for d in drift if "orphan" in d.reason]
        assert len(orphan) == 1
        assert orphan[0].file == "knowledge_graph"
        assert orphan[0].mentions == ["N-U7"]

    def test_no_graph_membership_reason_unchanged(self):
        """With graph_truth=None and an orphan-free kg, a registry-unknown cause
        refdes drifts with the EXACT legacy reason — no 'schematic' suffix."""
        registry = _base_registry()
        rules = RulesSet(
            rules=[
                Rule(
                    id="R-LEGACY-001",
                    symptoms=["boot loop"],
                    likely_causes=[Cause(refdes="Q42", probability=0.5, mechanism="short")],
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
        assert drift[0].reason == "Cause.refdes not in registry.components[canonical_name]"
        assert "schematic" not in drift[0].reason


def _two_regulator_graph() -> ElectricalGraph:
    """A board where rail PP1V8_X is sourced by the dedicated regulator U200,
    while U100 (also a real IC) exists but does NOT produce it — the shape the
    Cartographe over-attributes to the salient PMIC."""
    return ElectricalGraph(
        device_slug="mini",
        components={
            "U100": ComponentNode(refdes="U100", type="ic", kind="ic", pages=[1]),
            "U200": ComponentNode(refdes="U200", type="ic", kind="ic", pages=[2]),
        },
        nets={"PP1V8_X": NetNode(label="PP1V8_X", is_power=True)},
        power_rails={
            "PP1V8_X": PowerRail(label="PP1V8_X", voltage_nominal=1.8, source_refdes="U200"),
        },
        typed_edges=[TypedEdge(src="U200", dst="PP1V8_X", kind="powers")],
        quality=SchematicQualityReport(total_pages=2, pages_parsed=2),
    )


def _contradicted_kg() -> KnowledgeGraph:
    """U100 wrongly credited for PP1V8_X (graph source is U200)."""
    return KnowledgeGraph(
        nodes=[
            KnowledgeNode(id="N-U100", kind="component", label="pmic"),
            KnowledgeNode(id="N-NET_PP1V8_X", kind="net", label="1.8V"),
        ],
        edges=[KnowledgeEdge(source_id="N-U100", target_id="N-NET_PP1V8_X", relation="powers")],
    )


class TestEdgeContradictionDrift:
    def test_contradicted_power_edge_is_drift_in_graph_mode(self):
        gt = GraphTruth(_two_regulator_graph())
        drift = compute_drift(
            registry=Registry(device_label="Demo", components=[], signals=[]),
            knowledge_graph=_contradicted_kg(),
            rules=RulesSet(rules=[]),
            dictionary=Dictionary(entries=[]),
            graph_truth=gt,
        )
        edge_items = [d for d in drift if d.file == "knowledge_graph" and "contradict" in d.reason.lower()]
        assert len(edge_items) == 1
        # the mention must name the offending edge so the reviser can re-attribute
        joined = " ".join(edge_items[0].mentions)
        assert "U100" in joined and "PP1V8_X" in joined
        # and point at the graph's real source so the fix is actionable
        assert "U200" in joined

    def test_no_edge_contradiction_drift_without_graph(self):
        """Web-only packs (graph_truth=None) keep the legacy path — no edge
        contradiction check, since there is no connectivity authority to judge."""
        drift = compute_drift(
            registry=Registry(device_label="Demo", components=[], signals=[]),
            knowledge_graph=_contradicted_kg(),
            rules=RulesSet(rules=[]),
            dictionary=Dictionary(entries=[]),
            graph_truth=None,
        )
        assert all("contradict" not in d.reason.lower() for d in drift)
