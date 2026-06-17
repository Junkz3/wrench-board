"""Unit tests for api.pipeline.graph_truth — the deterministic existence
ground-truth over the compiled electrical_graph.json.

Fixture style mirrors tests/pipeline/test_drift.py: a tiny hand-built graph
(`_mini_graph`) with just enough topology to exercise every query path, and
registry/kg/rules/dictionary minis for the mention-scan + enrichment tests.

The module under test exists because the web-derived Registry covers ~2 % of
a real board, so the LLM Auditor accuses REAL identifiers of being fabricated.
GraphTruth answers "does X exist / what powers it / at what voltage" from the
graph deterministically — queries, never dumps (2026-04-24 lesson: dumping the
graph into generative context produced 23/23 fabricated attributions).
"""

from __future__ import annotations

from api.pipeline.graph_truth import (
    QUERY_GRAPH_TOOL,
    GraphTruth,
    Mentions,
    build_ground_truth_report,
    enrich_registry_from_graph,
    extract_mentions,
    handle_query_graph,
)
from api.pipeline.schemas import (
    Cause,
    ComponentSheet,
    DiagnosticStep,
    Dictionary,
    KnowledgeGraph,
    KnowledgeNode,
    Registry,
    RegistryComponent,
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


def _mini_graph() -> ElectricalGraph:
    """A 3-component / 3-net board exercising every GraphTruth code path.

    - U8100  : PMIC (ic), sources the PP1V2_S2 rail.
    - SWV011 : a load switch (switch) — the kind of real refdes the web
               registry never sees and the auditor flags as fabricated.
    - C0042  : decoupling cap on PP1V2_S2 — present-but-unmentioned content
               that must NEVER leak into the compiled report.

    Power rail PP1V2_S2 = 1.2 V, sourced by U8100, consumed by C0042.
    Typed edges: U8100 -powers-> PP1V2_S2 -powers-> C0042 (the chain the
    who_powers / consumers_of resolvers walk).
    """
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


# ======================================================================
# Task 1 — GraphTruth read-only query index
# ======================================================================


def test_has_component():
    gt = GraphTruth(_mini_graph())
    assert gt.has_component("SWV011") is True
    assert gt.has_component("U9999") is False


def test_component_info():
    gt = GraphTruth(_mini_graph())
    info = gt.component_info("SWV011")
    assert info is not None
    # The ComponentNode model carries type/kind + role/pages — surface them.
    assert info["type"] == "switch"
    assert info["kind"] == "ic"  # SWV011 left at default kind="ic"
    assert info["role"] == "load_switch"
    assert info["pages"] == [5]
    assert gt.component_info("U9999") is None


def test_has_net_covers_nets_and_rails():
    gt = GraphTruth(_mini_graph())
    assert gt.has_net("SIG_EN") is True       # plain net
    assert gt.has_net("PP1V2_S2") is True      # net AND rail
    assert gt.has_net("PP9V9_FAKE") is False


def test_rail_info():
    gt = GraphTruth(_mini_graph())
    assert gt.rail_info("PP1V2_S2") == {
        "voltage_nominal": 1.2,
        "source_refdes": "U8100",
        "n_consumers": 1,
    }
    # SIG_EN is a net but not a rail → None.
    assert gt.rail_info("SIG_EN") is None
    assert gt.rail_info("PP9V9_FAKE") is None


def test_who_powers():
    gt = GraphTruth(_mini_graph())
    # From the typed_edge U8100 -powers-> PP1V2_S2 AND the rail.source_refdes,
    # deduped to a single U8100.
    assert gt.who_powers("PP1V2_S2") == ["U8100"]
    assert gt.who_powers("PP9V9_FAKE") == []


def test_consumers_of():
    gt = GraphTruth(_mini_graph())
    assert gt.consumers_of("PP1V2_S2") == ["C0042"]
    assert gt.consumers_of("PP9V9_FAKE") == []


def test_nets_of():
    gt = GraphTruth(_mini_graph())
    assert "PP1V2_S2" in gt.nets_of("U8100")
    assert gt.nets_of("U9999") == []


def test_search_case_insensitive_substring():
    gt = GraphTruth(_mini_graph())
    matches = gt.search("pp1v")
    assert "PP1V2_S2" in matches
    assert "PP1V8_S2" in matches
    assert gt.search("zzz_no_match") == []


def test_search_strips_trailing_star_and_dedups():
    gt = GraphTruth(_mini_graph())
    # Trailing '*' is stripped (the agent often types a glob); PP1V2_S2 lives
    # in both components-domain (no) and nets+rails — must appear exactly once.
    matches = gt.search("PP1V2_S2*")
    assert matches.count("PP1V2_S2") == 1


# ======================================================================
# Task 2 — Mentions / extract_mentions / build_ground_truth_report
# ======================================================================


def _mention_inputs():
    registry = Registry(
        device_label="Demo",
        components=[
            RegistryComponent(
                canonical_name="U8100",
                kind="PMIC",
                description="PMIC generating PP1V2_S2 and enable signals.",
            ),
        ],
        signals=[],
    )
    kg = KnowledgeGraph(
        nodes=[KnowledgeNode(id="N-U8100", kind="component", label="PMIC U8100")],
        edges=[],
    )
    rules = RulesSet(
        rules=[
            Rule(
                id="R-DEMO-001",
                symptoms=["PP1V8_S2 missing on cold boot"],
                likely_causes=[Cause(refdes="U8100", probability=0.7, mechanism="short")],
                diagnostic_steps=[DiagnosticStep(action="probe SWV011 pin 1", expected="1.2V")],
                confidence=0.7,
            )
        ]
    )
    dictionary = Dictionary(
        entries=[
            ComponentSheet(
                canonical_name="U8100",
                role="Sources PP1V2_S2; probe at SWV011 pin 1",
            )
        ]
    )
    return registry, kg, rules, dictionary


def test_extract_mentions_collects_refdes_and_rails():
    registry, kg, rules, dictionary = _mention_inputs()
    m = extract_mentions(registry, kg, rules, dictionary)
    assert isinstance(m, Mentions)
    # Rails come from structured signal names AND free text across all 4 files.
    assert "PP1V2_S2" in m.rails       # registry description + dictionary role
    assert "PP1V8_S2" in m.rails       # rule symptom free text
    # Refdes from structured fields + free text.
    assert "U8100" in m.refdes
    assert "SWV011" in m.refdes        # only ever appears in free text


def test_extract_mentions_excludes_english_words():
    registry = Registry(
        device_label="Demo",
        components=[RegistryComponent(canonical_name="U1", kind="IC")],
        signals=[],
    )
    rules = RulesSet(
        rules=[
            Rule(
                id="R-DEMO-002",
                symptoms=["THE BOARD IS DEAD AND USB FAILS"],
                likely_causes=[Cause(refdes="U1", probability=0.5, mechanism="dead")],
                confidence=0.5,
            )
        ]
    )
    m = extract_mentions(
        registry,
        KnowledgeGraph(nodes=[], edges=[]),
        rules,
        Dictionary(entries=[]),
    )
    # Plain English uppercase words have no digit → not refdes-shaped.
    assert "THE" not in m.refdes
    assert "USB" not in m.refdes
    # U1 has a trailing digit → it is a refdes.
    assert "U1" in m.refdes


def test_extract_mentions_excludes_bus_protocol_names():
    """Digit-bearing bus/protocol names (USB2, I2C, DDR4…) pattern-match the
    refdes regex but are NEVER components — a stopword frozenset drops them so
    the report doesn't carry misleading "component DDR4: ABSENT" noise lines.
    A real refdes (U1) in the same prose still survives."""
    registry = Registry(
        device_label="Demo",
        components=[RegistryComponent(canonical_name="U1", kind="IC")],
        signals=[],
    )
    rules = RulesSet(
        rules=[
            Rule(
                id="R-DEMO-003",
                symptoms=["USB2 bus dead, I2C fails on DDR4 init"],
                likely_causes=[Cause(refdes="U1", probability=0.5, mechanism="dead")],
                confidence=0.5,
            )
        ]
    )
    m = extract_mentions(
        registry,
        KnowledgeGraph(nodes=[], edges=[]),
        rules,
        Dictionary(entries=[]),
    )
    assert "USB2" not in m.refdes
    assert "I2C" not in m.refdes
    assert "DDR4" not in m.refdes
    # A genuine refdes in the same prose is unaffected.
    assert "U1" in m.refdes


def test_build_ground_truth_report_present_and_absent():
    gt = GraphTruth(_mini_graph())
    mentions = Mentions(
        refdes={"SWV011", "U9999"},
        rails={"PP1V2_S2", "PP9V9_FAKE", "SIG_EN"},
    )
    report = build_ground_truth_report(gt, mentions)

    assert "- component SWV011: present (switch)" in report
    assert "- component U9999: ABSENT from schematic" in report
    assert (
        "- rail/net PP1V2_S2: present — 1.2 V nominal, sourced by U8100, 1 consumers"
        in report
    )
    assert "- rail/net PP9V9_FAKE: ABSENT from schematic" in report
    # SIG_EN exists as a net but is not a rail.
    assert "- rail/net SIG_EN: present (net)" in report
    # The report MUST NOT leak unmentioned graph content.
    assert "C0042" not in report


# ======================================================================
# Task 3 — QUERY_GRAPH_TOOL + handle_query_graph
# ======================================================================


def test_query_graph_tool_schema():
    assert QUERY_GRAPH_TOOL["name"] == "query_graph"
    enum = QUERY_GRAPH_TOOL["input_schema"]["properties"]["op"]["enum"]
    assert set(enum) == {
        "component",
        "net",
        "rail",
        "who_powers",
        "consumers_of",
        "nets_of",
        "search",
    }
    assert QUERY_GRAPH_TOOL["input_schema"]["required"] == ["op", "name"]


def test_handle_query_graph_dispatch():
    gt = GraphTruth(_mini_graph())

    assert handle_query_graph(gt, {"op": "component", "name": "SWV011"}) == {
        "present": True,
        "type": "switch",
        "kind": "ic",
        "role": "load_switch",
        "pages": [5],
    }
    assert handle_query_graph(gt, {"op": "component", "name": "U9999"}) == {"present": False}

    assert handle_query_graph(gt, {"op": "net", "name": "SIG_EN"}) == {"present": True}
    assert handle_query_graph(gt, {"op": "net", "name": "PP9V9_FAKE"}) == {"present": False}

    assert handle_query_graph(gt, {"op": "rail", "name": "PP1V2_S2"}) == {
        "present": True,
        "voltage_nominal": 1.2,
        "source_refdes": "U8100",
        "n_consumers": 1,
    }
    # SIG_EN exists as a plain net but is NOT a rail: don't let the model infer
    # nonexistence — answer present:False but disambiguate with a note pointing
    # at op=net, so it doesn't conclude the label is fabricated.
    assert handle_query_graph(gt, {"op": "rail", "name": "SIG_EN"}) == {
        "present": False,
        "note": "exists as a plain net, not a power rail; use op=net",
    }

    assert handle_query_graph(gt, {"op": "who_powers", "name": "PP1V2_S2"}) == {
        "sources": ["U8100"]
    }
    assert handle_query_graph(gt, {"op": "consumers_of", "name": "PP1V2_S2"}) == {
        "consumers": ["C0042"]
    }
    nets = handle_query_graph(gt, {"op": "nets_of", "name": "U8100"})
    assert "PP1V2_S2" in nets["nets"]

    matches = handle_query_graph(gt, {"op": "search", "name": "pp1v"})
    assert "PP1V2_S2" in matches["matches"]
    assert "PP1V8_S2" in matches["matches"]


def test_handle_query_graph_unknown_op_never_raises():
    gt = GraphTruth(_mini_graph())
    out = handle_query_graph(gt, {"op": "frobnicate", "name": "X"})
    assert "error" in out
    assert "valid ops" in out["error"]


# ======================================================================
# Task 5 — enrich_registry_from_graph
# ======================================================================


def test_enrich_registry_adds_cited_rail():
    gt = GraphTruth(_mini_graph())
    reg = Registry(
        device_label="Demo",
        components=[
            RegistryComponent(
                canonical_name="U8100",
                kind="PMIC",
                description="PMIC generating PP1V2_S2 and enable signals.",
            ),
        ],
        signals=[],  # the rail is cited in the description but never defined.
    )
    added = enrich_registry_from_graph(reg, gt)
    assert added == ["PP1V2_S2"]

    sig = next(s for s in reg.signals if s.canonical_name == "PP1V2_S2")
    assert sig.nominal_voltage == 1.2
    assert sig.kind == "POWER_RAIL"


def test_enrich_registry_is_idempotent():
    gt = GraphTruth(_mini_graph())
    reg = Registry(
        device_label="Demo",
        components=[
            RegistryComponent(
                canonical_name="U8100",
                kind="PMIC",
                description="PMIC generating PP1V2_S2.",
            ),
        ],
        signals=[],
    )
    assert enrich_registry_from_graph(reg, gt) == ["PP1V2_S2"]
    assert enrich_registry_from_graph(reg, gt) == []


def test_enrich_registry_skips_rail_absent_from_graph():
    gt = GraphTruth(_mini_graph())
    reg = Registry(
        device_label="Demo",
        components=[
            RegistryComponent(
                canonical_name="U8100",
                kind="PMIC",
                description="cites a phantom rail PP9V9_FAKE not in the graph.",
            ),
        ],
        signals=[],
    )
    assert enrich_registry_from_graph(reg, gt) == []
    assert all(s.canonical_name != "PP9V9_FAKE" for s in reg.signals)


class TestRailOnlyLabel:
    def test_who_powers_resolves_rail_without_net_entry(self):
        """Un rail peut exister dans power_rails SANS NetNode (graphes réels) :
        l'index des arêtes `powers` doit quand même résoudre ses sources."""
        from api.pipeline.schematic.schemas import (
            ComponentNode,
            ElectricalGraph,
            PowerRail,
            SchematicQualityReport,
            TypedEdge,
        )
        g = ElectricalGraph(
            device_slug="testdev",
            components={"U1": ComponentNode(refdes="U1", type="ic")},
            nets={},  # PP5V0_RAILONLY n'a PAS d'entrée net
            power_rails={"PP5V0_RAILONLY": PowerRail(label="PP5V0_RAILONLY")},
            typed_edges=[TypedEdge(src="U1", dst="PP5V0_RAILONLY", kind="powers")],
            quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
        )
        gt = GraphTruth(g)
        assert gt.who_powers("PP5V0_RAILONLY") == ["U1"]
