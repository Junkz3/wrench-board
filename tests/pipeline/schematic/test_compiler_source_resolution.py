"""Compiler — power-rail source resolution through in-line pass elements.

The vision pass labels an in-line element (a protection fuse, a series sense
resistor) as a rail's producer; a pass element cannot GENERATE a rail. These
tests pin the deterministic resolution:

  - a pass element {fuse, resistor, ferrite, inductor} is never a source,
  - the source is traced through 2-pin pass elements to the active boundary,
  - a controlled power FET resolves to its controlling IC (recursively, so a
    FET→FET→IC cascade reaches the real controller),
  - every resolved source carries a provenance + confidence tag.

End-to-end through `compile_electrical_graph` on hand-made SchematicGraphs —
no merger, no vision.
"""

from __future__ import annotations

from api.pipeline.schematic.compiler import compile_electrical_graph
from api.pipeline.schematic.schemas import (
    ComponentNode,
    NetNode,
    PagePin,
    SchematicGraph,
    TypedEdge,
)


def _comp(refdes: str, type_: str, pins: list[tuple[str, str, str]]) -> ComponentNode:
    return ComponentNode(
        refdes=refdes,
        type=type_,
        pages=[1],
        pins=[PagePin(number=n, role=r, net_label=net) for n, r, net in pins],
    )


def _sg(components, nets, edges) -> SchematicGraph:
    return SchematicGraph(
        device_slug="t",
        source_pdf="t.pdf",
        page_count=1,
        components={c.refdes: c for c in components},
        nets={label: NetNode(label=label, is_power=power) for label, power in nets},
        typed_edges=edges,
    )


def test_source_resolves_through_fuse_to_the_ic():
    """IC produces RAIL_REG; a fuse bridges RAIL_REG↔RAIL and vision wrongly
    emits powers(fuse, RAIL). The rail must resolve to the IC, not the fuse."""
    g = _sg(
        components=[
            _comp("U1", "ic", [("1", "power_out", "RAIL_REG")]),
            _comp("F1", "fuse", [("1", "unknown", "RAIL_REG"), ("2", "unknown", "RAIL")]),
        ],
        nets=[("RAIL_REG", True), ("RAIL", True)],
        edges=[
            TypedEdge(src="U1", dst="RAIL_REG", kind="powers"),
            TypedEdge(src="F1", dst="RAIL", kind="powers"),  # vision mistake
        ],
    )
    elec = compile_electrical_graph(g)
    assert elec.power_rails["RAIL"].source_refdes == "U1"
    assert elec.power_rails["RAIL"].source_provenance == "through_pass_element"


def test_source_resolves_through_series_resistor_to_the_ic():
    g = _sg(
        components=[
            _comp("U1", "ic", [("1", "power_out", "RAIL_VR")]),
            _comp("R1", "resistor", [("1", "unknown", "RAIL_VR"), ("2", "unknown", "RAIL")]),
        ],
        nets=[("RAIL_VR", True), ("RAIL", True)],
        edges=[
            TypedEdge(src="U1", dst="RAIL_VR", kind="powers"),
            TypedEdge(src="R1", dst="RAIL", kind="powers"),
        ],
    )
    elec = compile_electrical_graph(g)
    assert elec.power_rails["RAIL"].source_refdes == "U1"
    assert elec.power_rails["RAIL"].source_provenance == "through_pass_element"


def test_pass_element_alone_is_never_the_source():
    """powers(fuse, RAIL) with no active source upstream → the rail is unsourced,
    NOT attributed to the fuse."""
    g = _sg(
        components=[
            _comp("F1", "fuse", [("1", "unknown", "UPSTREAM"), ("2", "unknown", "RAIL")]),
        ],
        nets=[("RAIL", True), ("UPSTREAM", True)],
        edges=[TypedEdge(src="F1", dst="RAIL", kind="powers")],
    )
    elec = compile_electrical_graph(g)
    assert elec.power_rails["RAIL"].source_refdes != "F1"
    assert elec.power_rails["RAIL"].source_provenance == "unresolved"


def test_fet_source_resolves_to_controlling_ic():
    g = _sg(
        components=[
            _comp("Q1", "transistor", [("1", "power_out", "RAIL")]),
            _comp("U1", "ic", []),
        ],
        nets=[("RAIL", True)],
        edges=[
            TypedEdge(src="Q1", dst="RAIL", kind="powers"),
            TypedEdge(src="U1", dst="Q1", kind="enables"),
        ],
    )
    elec = compile_electrical_graph(g)
    assert elec.power_rails["RAIL"].source_refdes == "U1"
    assert elec.power_rails["RAIL"].source_provenance == "fet_controller"


def test_fet_chain_resolves_to_controlling_ic():
    """Cascaded load switches: RAIL←Q1, Q1 enabled by Q2, Q2 enabled by U1.
    The recursive resolution must reach U1, not stop at the intermediate Q2."""
    g = _sg(
        components=[
            _comp("Q1", "transistor", [("1", "power_out", "RAIL")]),
            _comp("Q2", "transistor", []),
            _comp("U1", "ic", []),
        ],
        nets=[("RAIL", True)],
        edges=[
            TypedEdge(src="Q1", dst="RAIL", kind="powers"),
            TypedEdge(src="Q2", dst="Q1", kind="enables"),
            TypedEdge(src="U1", dst="Q2", kind="enables"),
        ],
    )
    elec = compile_electrical_graph(g)
    assert elec.power_rails["RAIL"].source_refdes == "U1"


def test_uncontrolled_fet_stays_the_source():
    g = _sg(
        components=[_comp("Q1", "transistor", [("1", "power_out", "RAIL")])],
        nets=[("RAIL", True)],
        edges=[TypedEdge(src="Q1", dst="RAIL", kind="powers")],
    )
    elec = compile_electrical_graph(g)
    assert elec.power_rails["RAIL"].source_refdes == "Q1"
    assert elec.power_rails["RAIL"].source_provenance == "direct"


def test_capacitor_is_not_a_power_path():
    """A cap bridging two rails is a decoupler, not a power path — no propagation."""
    g = _sg(
        components=[
            _comp("U1", "ic", [("1", "power_out", "RAIL_REG")]),
            _comp("C1", "capacitor", [("1", "unknown", "RAIL_REG"), ("2", "unknown", "RAIL")]),
        ],
        nets=[("RAIL_REG", True), ("RAIL", True)],
        edges=[TypedEdge(src="U1", dst="RAIL_REG", kind="powers")],
    )
    elec = compile_electrical_graph(g)
    assert elec.power_rails["RAIL"].source_refdes is None
    assert elec.power_rails["RAIL"].source_provenance == "unresolved"


def test_direct_producer_is_marked_direct_high_confidence():
    g = _sg(
        components=[_comp("U1", "ic", [("1", "power_out", "RAIL")])],
        nets=[("RAIL", True)],
        edges=[TypedEdge(src="U1", dst="RAIL", kind="powers")],
    )
    elec = compile_electrical_graph(g)
    rail = elec.power_rails["RAIL"]
    assert rail.source_refdes == "U1"
    assert rail.source_provenance == "direct"
    assert rail.source_confidence == "high"


def test_does_not_bridge_through_an_inductor():
    """A switching converter's inductor is directional (it sits between the input
    rail and the switch node). The bridge must NOT propagate the downstream
    converter back onto the input rail — the iphone-11 PP_VDD_MAIN→boost-IC bug.
    Inductors are excluded from bridging (ferrite still bridges, for filters)."""
    g = _sg(
        components=[
            _comp("UB", "ic", [("1", "power_out", "RAIL_BOOST")]),
            _comp("L1", "inductor", [("1", "unknown", "RAIL_MAIN"), ("2", "unknown", "RAIL_BOOST")]),
        ],
        nets=[("RAIL_MAIN", True), ("RAIL_BOOST", True)],
        edges=[TypedEdge(src="UB", dst="RAIL_BOOST", kind="powers")],
    )
    elec = compile_electrical_graph(g)
    assert elec.power_rails["RAIL_MAIN"].source_refdes != "UB"
    assert elec.power_rails["RAIL_MAIN"].source_provenance == "unresolved"


def test_ferrite_still_bridges():
    """Ferrite-filtered sub-rails must still resolve (the legitimate filter case),
    so excluding inductors doesn't throw out filter propagation entirely."""
    g = _sg(
        components=[
            _comp("U1", "ic", [("1", "power_out", "RAIL_CLEAN")]),
            _comp("FB1", "ferrite", [("1", "unknown", "RAIL_CLEAN"), ("2", "unknown", "RAIL_FILT")]),
        ],
        nets=[("RAIL_CLEAN", True), ("RAIL_FILT", True)],
        edges=[TypedEdge(src="U1", dst="RAIL_CLEAN", kind="powers")],
    )
    elec = compile_electrical_graph(g)
    assert elec.power_rails["RAIL_FILT"].source_refdes == "U1"


def test_bridge_rejects_a_source_that_consumes_the_destination():
    """Direction guard: U1 produces RAIL_A and a fuse bridges RAIL_A↔RAIL_B, but
    U1 is `powered_by` RAIL_B (it consumes it). Propagating U1 as RAIL_B's source
    would be backwards — the guard must reject it."""
    g = _sg(
        components=[
            _comp("U1", "ic", [("1", "power_out", "RAIL_A"), ("2", "power_in", "RAIL_B")]),
            _comp("F1", "fuse", [("1", "unknown", "RAIL_A"), ("2", "unknown", "RAIL_B")]),
        ],
        nets=[("RAIL_A", True), ("RAIL_B", True)],
        edges=[
            TypedEdge(src="U1", dst="RAIL_A", kind="powers"),
            TypedEdge(src="U1", dst="RAIL_B", kind="powered_by"),
        ],
    )
    elec = compile_electrical_graph(g)
    assert elec.power_rails["RAIL_B"].source_refdes != "U1"


def test_fet_control_cycle_terminates():
    """A mutual enables cycle with no IC must terminate (no infinite loop) and
    leave the source at a FET."""
    g = _sg(
        components=[
            _comp("Q1", "transistor", [("1", "power_out", "RAIL")]),
            _comp("Q2", "transistor", []),
        ],
        nets=[("RAIL", True)],
        edges=[
            TypedEdge(src="Q1", dst="RAIL", kind="powers"),
            TypedEdge(src="Q2", dst="Q1", kind="enables"),
            TypedEdge(src="Q1", dst="Q2", kind="enables"),
        ],
    )
    elec = compile_electrical_graph(g)
    assert elec.power_rails["RAIL"].source_refdes in {"Q1", "Q2"}
