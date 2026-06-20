"""Unit tests for registry↔schematic reconciliation — dropping web-registry
fictions (components attested by neither the compiled graph nor the raw
vision/OCR) before the Cartographe propagates them into the knowledge graph.
"""

from __future__ import annotations

from api.pipeline.graph_truth import GraphTruth
from api.pipeline.reconcile import (
    find_kg_fictions,
    find_registry_fictions,
    seen_refdes_from_texts,
)
from api.pipeline.schemas import KnowledgeGraph, KnowledgeNode
from api.pipeline.schemas import Registry, RegistryComponent, RegistrySignal
from api.pipeline.schematic.schemas import (
    ComponentNode,
    ElectricalGraph,
    NetNode,
    PowerRail,
    SchematicQualityReport,
    TypedEdge,
)


def _graph() -> ElectricalGraph:
    return ElectricalGraph(
        device_slug="mini",
        components={
            "U8100": ComponentNode(refdes="U8100", type="ic", role="pmic", pages=[3]),
        },
        nets={"PP1V2_S2": NetNode(label="PP1V2_S2", is_power=True)},
        power_rails={
            "PP1V2_S2": PowerRail(
                label="PP1V2_S2", voltage_nominal=1.2, source_refdes="U8100"
            )
        },
        typed_edges=[TypedEdge(src="U8100", dst="PP1V2_S2", kind="powers")],
        quality=SchematicQualityReport(total_pages=5, pages_parsed=5),
    )


def _registry() -> Registry:
    return Registry(
        device_label="Demo",
        components=[
            RegistryComponent(canonical_name="U8100", kind="PMIC"),  # in graph
            RegistryComponent(canonical_name="R7192", kind="RESISTOR"),  # seen in vision only
            RegistryComponent(canonical_name="U6903", kind="IC"),  # nowhere → fiction
        ],
        signals=[RegistrySignal(canonical_name="PP1V2_S2", kind="POWER_RAIL")],
    )


def test_fiction_is_attested_by_neither_graph_nor_vision():
    gt = GraphTruth(_graph())
    fictions = find_registry_fictions(_registry(), gt, seen_refdes={"R7192"})
    assert fictions == ["U6903"]


def test_component_in_graph_is_never_a_fiction():
    gt = GraphTruth(_graph())
    fictions = find_registry_fictions(_registry(), gt, seen_refdes=set())
    # U8100 is in the graph → kept even though seen_refdes is empty
    assert "U8100" not in fictions


def test_component_seen_in_vision_is_kept_even_if_untraced_in_graph():
    """R7192 is on the PDF (vision) but the compiler couldn't trace it — it is a
    real component, NOT a web fiction, so it must be kept."""
    gt = GraphTruth(_graph())
    fictions = find_registry_fictions(_registry(), gt, seen_refdes={"R7192"})
    assert "R7192" not in fictions


def test_seen_refdes_harvests_refdes_from_vision_and_ocr_text():
    """seen_refdes_from_texts regex-harvests refdes-shaped tokens from the raw
    page-vision JSON + OCR anchors, skipping PP-rail tokens."""
    texts = [
        '{"components":[{"refdes":"R7192","type":"resistor"}]}',  # vision page
        "anchor: U8100 at bbox 12,34; PP1V2_S2 net label",  # ocr anchors
    ]
    seen = seen_refdes_from_texts(texts)
    assert "R7192" in seen
    assert "U8100" in seen
    assert "PP1V2_S2" not in seen  # rails are not components


def test_find_kg_fictions_flags_unattested_component_nodes():
    """A knowledge-graph component node attested nowhere is a fiction; one in
    the graph (U8100) or seen in vision (R7192) is not."""
    gt = GraphTruth(_graph())
    kg = KnowledgeGraph(
        nodes=[
            KnowledgeNode(id="N-U8100", kind="component", label="pmic"),  # in graph
            KnowledgeNode(id="N-R7192", kind="component", label="res"),  # seen only
            KnowledgeNode(id="N-U6903", kind="component", label="ldo"),  # fiction
            KnowledgeNode(id="N-NET_PP1V2_S2", kind="net", label="rail"),  # not a component
        ],
        edges=[],
    )
    fictions = find_kg_fictions(kg, gt, seen_refdes={"R7192"})
    assert fictions == ["U6903"]
