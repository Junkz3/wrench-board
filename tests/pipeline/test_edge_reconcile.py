"""Unit tests for knowledge-graph EDGE reconciliation against the compiled
electrical graph.

The component-level `find_*_fictions` answer "does this refdes exist?". This is
the EDGE equivalent: "does the kg's `U powers RAIL` claim contradict the graph?"
A contradiction is the precise, deterministic signal that the Cartographe
over-attributed a rail to a salient power IC when the schematic shows a DIFFERENT
source-capable IC actually producing it (the macbook U7800→PP1V8_S0 class, whose
real source is the dedicated regulator U8200).

The discriminator is type-aware on purpose: `who_powers` often returns the
nearest IN-LINE element (a fuse, a series sense resistor, a rectifier diode) as a
rail's "source". A passive cannot GENERATE a rail, so it must never contradict an
IC attribution — only a different SOURCE-CAPABLE producer (ic / module) can.
"""

from __future__ import annotations

from api.pipeline.graph_truth import GraphTruth
from api.pipeline.reconcile import find_contradicted_edges, prune_contradicted_edges
from api.pipeline.schemas import KnowledgeEdge, KnowledgeGraph, KnowledgeNode
from api.pipeline.schematic.schemas import (
    ComponentNode,
    ElectricalGraph,
    NetNode,
    PowerRail,
    SchematicQualityReport,
    TypedEdge,
)


def _graph() -> ElectricalGraph:
    """A miniature board reproducing the three real shapes:
    - PP1V8_S0 sourced by U8200 (a dedicated regulator IC) — the over-attribution
      target: the kg will wrongly credit U7800.
    - PP3V3_S0 sourced by U8220 (IC) — the kg credits U8220 correctly (confirmed).
    - PPBUS_G3H sourced by F7000 (a FUSE) — who_powers returns an in-line passive;
      the kg credits U7000, which must NOT be flagged (passive can't contradict).
    """
    return ElectricalGraph(
        device_slug="mini",
        components={
            "U7800": ComponentNode(refdes="U7800", type="ic", kind="ic", pages=[1]),
            "U7000": ComponentNode(refdes="U7000", type="ic", kind="ic", pages=[1]),
            "U8200": ComponentNode(refdes="U8200", type="ic", kind="ic", pages=[2]),
            "U8220": ComponentNode(refdes="U8220", type="ic", kind="ic", pages=[2]),
            "D6905": ComponentNode(refdes="D6905", type="diode", kind="passive_d", pages=[1]),
            "F7000": ComponentNode(refdes="F7000", type="fuse", kind="ic", pages=[1]),
        },
        nets={
            "PP1V8_S0": NetNode(label="PP1V8_S0", is_power=True),
            "PP3V3_S0": NetNode(label="PP3V3_S0", is_power=True),
            "PPBUS_G3H": NetNode(label="PPBUS_G3H", is_power=True),
        },
        power_rails={
            "PP1V8_S0": PowerRail(label="PP1V8_S0", voltage_nominal=1.8, source_refdes="U8200"),
            "PP3V3_S0": PowerRail(label="PP3V3_S0", voltage_nominal=3.3, source_refdes="U8220"),
            "PPBUS_G3H": PowerRail(label="PPBUS_G3H", source_refdes="F7000"),
        },
        typed_edges=[
            TypedEdge(src="U8200", dst="PP1V8_S0", kind="powers"),
            TypedEdge(src="U8220", dst="PP3V3_S0", kind="powers"),
            TypedEdge(src="F7000", dst="PPBUS_G3H", kind="powers"),
        ],
        quality=SchematicQualityReport(total_pages=2, pages_parsed=2),
    )


def _kg(edges: list[KnowledgeEdge]) -> KnowledgeGraph:
    return KnowledgeGraph(nodes=[], edges=edges)


def _edge(src: str, rail: str, relation: str = "powers") -> KnowledgeEdge:
    return KnowledgeEdge(
        source_id=f"N-{src}", target_id=f"N-NET_{rail}", relation=relation
    )


def test_flags_ic_over_attribution_to_a_different_active_source():
    """kg says U7800 powers PP1V8_S0, but the graph's source is the dedicated
    regulator U8200 (also an IC). U7800 is not a producer → contradicted."""
    gt = GraphTruth(_graph())
    contras = find_contradicted_edges(_kg([_edge("U7800", "PP1V8_S0")]), gt)
    assert len(contras) == 1
    c = contras[0]
    assert c.src == "U7800"
    assert c.rail == "PP1V8_S0"
    assert c.relation == "powers"
    assert "U8200" in c.graph_sources


def test_does_not_flag_when_graph_source_is_an_inline_passive():
    """kg says U7000 powers PPBUS_G3H; the graph's only "source" is the fuse
    F7000. A fuse cannot generate a rail, so it cannot contradict U7000."""
    gt = GraphTruth(_graph())
    contras = find_contradicted_edges(_kg([_edge("U7000", "PPBUS_G3H")]), gt)
    assert contras == []


def test_does_not_flag_a_confirmed_edge():
    """kg credits U8220 for PP3V3_S0, which the graph also sources from U8220."""
    gt = GraphTruth(_graph())
    contras = find_contradicted_edges(_kg([_edge("U8220", "PP3V3_S0")]), gt)
    assert contras == []


def test_ignores_non_power_relations():
    """`senses` / `shares_net` are not production claims — a sense/shared-net edge
    to a rail with a different active source must NOT be flagged."""
    gt = GraphTruth(_graph())
    edges = [_edge("U7800", "PP1V8_S0", "senses"), _edge("U7800", "PP1V8_S0", "shares_net")]
    assert find_contradicted_edges(_kg(edges), gt) == []


def test_does_not_flag_when_src_is_a_passive():
    """A passive src (diode D6905) claiming to drive a rail is out of scope: we
    only catch IC/module over-attribution, where the discriminator is sound."""
    gt = GraphTruth(_graph())
    contras = find_contradicted_edges(_kg([_edge("D6905", "PP1V8_S0", "drives")]), gt)
    assert contras == []


def test_ignores_component_to_component_edges():
    """A `drives` edge whose target is another component (N-U…), not a rail
    (N-NET_…), is not a component→rail power claim."""
    gt = GraphTruth(_graph())
    edge = KnowledgeEdge(source_id="N-U7800", target_id="N-U8200", relation="drives")
    assert find_contradicted_edges(_kg([edge]), gt) == []


def test_drives_relation_is_also_checked():
    """`drives` to a rail is a production-adjacent claim and is scored the same
    way as `powers` when the src is an IC."""
    gt = GraphTruth(_graph())
    contras = find_contradicted_edges(_kg([_edge("U7800", "PP1V8_S0", "drives")]), gt)
    assert len(contras) == 1
    assert contras[0].relation == "drives"


# --- prune_contradicted_edges (the deterministic backstop) -----------------


def test_prune_removes_only_the_contradicted_edge():
    """The backstop drops the contradicted edge and leaves confirmed + artifact
    edges (and non-power edges) untouched."""
    gt = GraphTruth(_graph())
    kept_confirmed = _edge("U8220", "PP3V3_S0")
    kept_artifact = _edge("U7000", "PPBUS_G3H")
    kept_senses = _edge("U7800", "PP1V8_S0", "senses")
    contradicted = _edge("U7800", "PP1V8_S0")
    kg = _kg([contradicted, kept_confirmed, kept_artifact, kept_senses])

    pruned_kg, removed = prune_contradicted_edges(kg, gt)

    assert [(c.src, c.rail) for c in removed] == [("U7800", "PP1V8_S0")]
    remaining = {(e.source_id, e.target_id, e.relation) for e in pruned_kg.edges}
    assert remaining == {
        ("N-U8220", "N-NET_PP3V3_S0", "powers"),
        ("N-U7000", "N-NET_PPBUS_G3H", "powers"),
        ("N-U7800", "N-NET_PP1V8_S0", "senses"),
    }


def test_prune_is_a_noop_when_nothing_is_contradicted():
    """A clean kg passes through unchanged and reports nothing removed."""
    gt = GraphTruth(_graph())
    kg = _kg([_edge("U8220", "PP3V3_S0"), _edge("U7000", "PPBUS_G3H")])
    pruned_kg, removed = prune_contradicted_edges(kg, gt)
    assert removed == []
    assert len(pruned_kg.edges) == 2


def test_prune_drops_a_node_orphaned_by_the_prune():
    """Removing the false edge leaves its rail node edgeless — a dangling
    assertion that would just trade contradiction-drift for orphan-drift and keep
    the gate blocked. The backstop must drop the node it stranded, while keeping
    any node still wired by a surviving edge."""
    gt = GraphTruth(_graph())
    nodes = [
        KnowledgeNode(id="N-U7800", kind="component", label="pmic"),
        KnowledgeNode(id="N-U8220", kind="component", label="regulator"),
        KnowledgeNode(id="N-NET_PP1V8_S0", kind="net", label="1.8V"),
        KnowledgeNode(id="N-NET_PP3V3_S0", kind="net", label="3.3V"),
    ]
    kg = KnowledgeGraph(
        nodes=nodes,
        edges=[_edge("U7800", "PP1V8_S0"), _edge("U8220", "PP3V3_S0")],  # contra, confirmed
    )
    pruned_kg, removed = prune_contradicted_edges(kg, gt)

    assert len(removed) == 1
    surviving = {n.id for n in pruned_kg.nodes}
    # U7800 + its rail are stranded by the prune → both gone; the confirmed pair stays.
    assert surviving == {"N-U8220", "N-NET_PP3V3_S0"}


def test_prune_keeps_a_preexisting_orphan_node():
    """A node already edgeless BEFORE the prune is the Cartographe's own orphan
    (its own drift to answer) — the backstop only removes what IT stranded, so a
    pre-existing orphan is left untouched."""
    gt = GraphTruth(_graph())
    kg = KnowledgeGraph(
        nodes=[
            KnowledgeNode(id="N-U8220", kind="component", label="reg"),
            KnowledgeNode(id="N-NET_PP3V3_S0", kind="net", label="3.3V"),
            KnowledgeNode(id="N-C9999", kind="component", label="stray cap"),  # pre-orphan
        ],
        edges=[_edge("U8220", "PP3V3_S0")],
    )
    pruned_kg, removed = prune_contradicted_edges(kg, gt)
    assert removed == []  # nothing contradicted
    assert {n.id for n in pruned_kg.nodes} == {"N-U8220", "N-NET_PP3V3_S0", "N-C9999"}


def test_prune_does_not_mutate_the_input_kg():
    """The backstop returns a NEW graph; the caller's snapshot is left intact so
    the best-of bookkeeping in the orchestrator stays honest."""
    gt = GraphTruth(_graph())
    kg = _kg([_edge("U7800", "PP1V8_S0")])
    pruned_kg, removed = prune_contradicted_edges(kg, gt)
    assert len(kg.edges) == 1  # original untouched
    assert len(pruned_kg.edges) == 0
    assert len(removed) == 1
