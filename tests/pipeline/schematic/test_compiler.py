"""Tests for api.pipeline.schematic.compiler.

Fixtures are hand-made SchematicGraph instances — no merger, no vision.
The MNT regulator scenario (drawn from page 3 + page 1) exercises the full
chain: rail source detection, consumer aggregation, enable-net capture,
depends_on derivation, and Kahn-ordered boot phases.
"""

from __future__ import annotations

import pytest

from api.pipeline.schematic.compiler import (
    _parse_voltage_from_label,
    compile_electrical_graph,
)
from api.pipeline.schematic.schemas import (
    Ambiguity,
    ComponentNode,
    ComponentValue,
    NetNode,
    PagePin,
    SchematicGraph,
    TypedEdge,
)

# ----------------------------------------------------------------------
# Voltage parsing
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,expected",
    [
        ("+5V", 5.0),
        ("+3V3", 3.3),
        ("+1V8", 1.8),
        ("+0V8", 0.8),
        ("30V_GATE", 30.0),
        ("VCC_3V3", 3.3),
        ("AVDD_1V8", 1.8),
        ("3.3V", 3.3),
        ("1.8V", 1.8),
        ("LPC_VCC", None),
        ("V_BAT", None),
        ("USB_TX_P", None),
        ("GND", None),
    ],
)
def test_parse_voltage_from_label(label: str, expected: float | None):
    assert _parse_voltage_from_label(label) == expected


# ----------------------------------------------------------------------
# MNT-like regulator topology (scaffold)
# ----------------------------------------------------------------------


def _mnt_regulator_graph() -> SchematicGraph:
    """Draw 6 regulators + their rails + 1 consumer, modelled on page 3.

    Nets:  30V_GATE → U7(+5V), U12(+3V3), U14(LPC_VCC)
                       U17 ← 3V3_PWR_AUX (+1V8)
           +5V     → U13(+1V2), U19(+1V5)
    Enables: 5V_PWR_EN→U7, 3V3_PWR_EN→U12, 1V2_PWR_EN→U13, PCIE1_PWR_EN→U19
    Decoupling: C16 on 30V_GATE, C33 on +5V (U19 input)
    """
    components = {
        "U7": ComponentNode(
            refdes="U7",
            type="ic",
            value=ComponentValue(
                raw="LM2677SX-5",
                primary="LM2677SX-5",
                mpn="LM2677SX-5",
                description="5V buck converter, up to 5A",
            ),
            pages=[3],
        ),
        "U12": ComponentNode(
            refdes="U12",
            type="ic",
            value=ComponentValue(raw="LM2677SX-3.3", primary="LM2677SX-3.3"),
            pages=[3],
        ),
        "U13": ComponentNode(refdes="U13", type="ic", pages=[3]),
        "U14": ComponentNode(refdes="U14", type="ic", pages=[3]),
        "U17": ComponentNode(refdes="U17", type="ic", pages=[3]),
        "U19": ComponentNode(refdes="U19", type="ic", pages=[3]),
        "C16": ComponentNode(refdes="C16", type="capacitor", pages=[3]),
        "C33": ComponentNode(refdes="C33", type="capacitor", pages=[3]),
    }
    nets = {
        "30V_GATE": NetNode(label="30V_GATE", is_power=True, is_global=True, pages=[3]),
        "+5V": NetNode(label="+5V", is_power=True, is_global=True, pages=[3]),
        "+3V3": NetNode(label="+3V3", is_power=True, is_global=True, pages=[3]),
        "+1V2": NetNode(label="+1V2", is_power=True, is_global=True, pages=[3]),
        "+1V5": NetNode(label="+1V5", is_power=True, is_global=True, pages=[3]),
        "+1V8": NetNode(label="+1V8", is_power=True, is_global=True, pages=[3]),
        "LPC_VCC": NetNode(label="LPC_VCC", is_power=True, is_global=True, pages=[3]),
        "3V3_PWR_AUX": NetNode(
            label="3V3_PWR_AUX", is_power=True, is_global=True, pages=[3]
        ),
    }
    edges = [
        # Production
        TypedEdge(src="U7", dst="+5V", kind="powers", page=3),
        TypedEdge(src="U12", dst="+3V3", kind="powers", page=3),
        TypedEdge(src="U13", dst="+1V2", kind="powers", page=3),
        TypedEdge(src="U14", dst="LPC_VCC", kind="powers", page=3),
        TypedEdge(src="U17", dst="+1V8", kind="powers", page=3),
        TypedEdge(src="U19", dst="+1V5", kind="powers", page=3),
        # Consumption (these create depends_on edges downstream)
        TypedEdge(src="U7", dst="30V_GATE", kind="powered_by", page=3),
        TypedEdge(src="U12", dst="30V_GATE", kind="powered_by", page=3),
        TypedEdge(src="U14", dst="30V_GATE", kind="powered_by", page=3),
        TypedEdge(src="U13", dst="+5V", kind="powered_by", page=3),
        TypedEdge(src="U19", dst="+5V", kind="powered_by", page=3),
        TypedEdge(src="U17", dst="3V3_PWR_AUX", kind="powered_by", page=3),
        # Enables
        TypedEdge(src="5V_PWR_EN", dst="U7", kind="enables", page=3),
        TypedEdge(src="3V3_PWR_EN", dst="U12", kind="enables", page=3),
        TypedEdge(src="1V2_PWR_EN", dst="U13", kind="enables", page=3),
        TypedEdge(src="PCIE1_PWR_EN", dst="U19", kind="enables", page=3),
        # Decoupling
        TypedEdge(src="C16", dst="30V_GATE", kind="decouples", page=3),
        TypedEdge(src="C33", dst="+5V", kind="decouples", page=3),
    ]
    return SchematicGraph(
        device_slug="mnt-reform-motherboard",
        source_pdf="board_assets/mnt-reform-motherboard.pdf",
        page_count=12,
        components=components,
        nets=nets,
        typed_edges=edges,
    )


# ----------------------------------------------------------------------
# Power rails
# ----------------------------------------------------------------------


def test_power_rails_derive_source_and_voltage():
    elec = compile_electrical_graph(_mnt_regulator_graph())
    plus5v = elec.power_rails["+5V"]
    assert plus5v.source_refdes == "U7"
    assert plus5v.voltage_nominal == 5.0
    assert plus5v.source_type == "buck"


def test_power_rail_voltage_null_when_label_does_not_encode_it():
    elec = compile_electrical_graph(_mnt_regulator_graph())
    assert elec.power_rails["LPC_VCC"].voltage_nominal is None


def test_consumers_are_aggregated_per_rail():
    elec = compile_electrical_graph(_mnt_regulator_graph())
    assert set(elec.power_rails["+5V"].consumers) == {"U13", "U19"}
    assert elec.power_rails["30V_GATE"].consumers == ["U7", "U12", "U14"]


def test_enable_net_is_captured_on_the_rail_its_producer_controls():
    elec = compile_electrical_graph(_mnt_regulator_graph())
    assert elec.power_rails["+5V"].enable_net == "5V_PWR_EN"
    assert elec.power_rails["+3V3"].enable_net == "3V3_PWR_EN"
    assert elec.power_rails["+1V2"].enable_net == "1V2_PWR_EN"
    # U14 has no enable — always-on rail
    assert elec.power_rails["LPC_VCC"].enable_net is None


def test_decoupling_list_aggregates_decouples_edges():
    elec = compile_electrical_graph(_mnt_regulator_graph())
    assert elec.power_rails["30V_GATE"].decoupling == ["C16"]
    assert elec.power_rails["+5V"].decoupling == ["C33"]


# ----------------------------------------------------------------------
# depends_on derivation
# ----------------------------------------------------------------------


def test_depends_on_edges_are_generated_from_powered_by_chains():
    elec = compile_electrical_graph(_mnt_regulator_graph())
    deps = {
        (e.src, e.dst)
        for e in elec.typed_edges
        if e.kind == "depends_on"
    }
    # U13 and U19 both drink +5V, which is produced by U7.
    assert ("U13", "U7") in deps
    assert ("U19", "U7") in deps
    # U7/U12/U14 drink 30V_GATE — no producer in the graph, so no depends_on.
    for src in ("U7", "U12", "U14"):
        assert not any(d[0] == src for d in deps)


# ----------------------------------------------------------------------
# Boot sequence (Kahn levels)
# ----------------------------------------------------------------------


def test_boot_sequence_places_root_regulators_in_phase_one_and_downstream_in_phase_two():
    elec = compile_electrical_graph(_mnt_regulator_graph())
    assert len(elec.boot_sequence) == 2

    phase1 = elec.boot_sequence[0]
    assert phase1.index == 1
    assert set(phase1.components_entering) == {"U7", "U12", "U14", "U17"}
    assert set(phase1.rails_stable) == {"+5V", "+3V3", "LPC_VCC", "+1V8"}

    phase2 = elec.boot_sequence[1]
    assert phase2.index == 2
    assert set(phase2.components_entering) == {"U13", "U19"}
    assert set(phase2.rails_stable) == {"+1V2", "+1V5"}


def test_cycle_in_dependencies_reports_ambiguity_and_halts_schedule():
    components = {
        "A": ComponentNode(refdes="A", type="ic"),
        "B": ComponentNode(refdes="B", type="ic"),
    }
    nets = {
        "X": NetNode(label="X", is_power=True),
        "Y": NetNode(label="Y", is_power=True),
    }
    # A powers X, consumes Y ; B powers Y, consumes X  → cycle
    edges = [
        TypedEdge(src="A", dst="X", kind="powers"),
        TypedEdge(src="A", dst="Y", kind="powered_by"),
        TypedEdge(src="B", dst="Y", kind="powers"),
        TypedEdge(src="B", dst="X", kind="powered_by"),
    ]
    g = SchematicGraph(
        device_slug="demo",
        source_pdf="demo.pdf",
        page_count=1,
        components=components,
        nets=nets,
        typed_edges=edges,
    )
    elec = compile_electrical_graph(g)
    assert elec.boot_sequence == []
    assert any(
        "cycle" in a.description.lower() for a in elec.ambiguities
    ), elec.ambiguities


def test_empty_graph_does_not_crash_and_emits_no_phases():
    g = SchematicGraph(
        device_slug="empty",
        source_pdf="none.pdf",
        page_count=0,
    )
    elec = compile_electrical_graph(g)
    assert elec.boot_sequence == []
    assert elec.power_rails == {}
    assert elec.quality.total_pages == 0


# ----------------------------------------------------------------------
# Quality report
# ----------------------------------------------------------------------


def test_quality_report_counts_missing_values_and_mpns():
    g = _mnt_regulator_graph()
    elec = compile_electrical_graph(g)
    # Only U7 and U12 carry a ComponentValue with MPN in the fixture.
    assert elec.quality.components_without_value == 6  # 8 total - 2 with value
    assert elec.quality.components_without_mpn == 7  # 8 total - 1 with MPN


def test_quality_degraded_mode_triggers_on_low_confidence():
    g = _mnt_regulator_graph()
    elec = compile_electrical_graph(g, page_confidences={3: 0.5})
    assert elec.quality.confidence_global == pytest.approx(0.5)
    assert elec.quality.degraded_mode is True


def test_quality_degraded_mode_defaults_off_with_no_confidences():
    g = _mnt_regulator_graph()
    elec = compile_electrical_graph(g)
    assert elec.quality.confidence_global == 1.0
    assert elec.quality.degraded_mode is False


def test_orphan_cross_page_counts_only_unstitched_refs_not_net_naming_notes():
    """`orphan_cross_page_refs` must count unstitched cross-page connectors
    only — not every ambiguity that happens to name a net.

    The vision pass emits rich, honest net-naming notes ("GND symbol drawn
    but no GND label", "PP04xx are probe pads, couldn't tell if pad+signal
    are one net"). Each carries `related_nets`, but none is a broken cross-page
    connector. Counting them inflated iphone-11 to 222 orphans (5 real) and
    falsely flagged DEGRADED."""
    g = _mnt_regulator_graph()
    g.ambiguities.extend(
        [
            # Two genuine merger-emitted cross-page orphans.
            Ambiguity(
                description=(
                    "Cross-page ref 'wifi_mlb' on page 73 has no matching net "
                    "or counter-ref on other pages"
                ),
                page=73,
                related_nets=["wifi_mlb"],
            ),
            Ambiguity(
                description=(
                    "Cross-page connector with unreadable label on page 90"
                ),
                page=90,
            ),
            # Net-naming notes with related_nets — NOT cross-page orphans.
            Ambiguity(
                description="GND label is not present in the grounding net set",
                page=12,
                related_nets=["GND"],
            ),
            Ambiguity(
                description="PP04xx labels are probe/test pad identifiers",
                page=14,
                related_nets=["PP0453", "PP0413"],
            ),
        ]
    )
    elec = compile_electrical_graph(g)
    assert elec.quality.orphan_cross_page_refs == 2
    assert elec.quality.degraded_mode is False


# ----------------------------------------------------------------------
# Direction-tolerant edge handling (Sonnet / other models emit reversed
# `powered_by` edges). Both directions must yield the same derived graph.
# ----------------------------------------------------------------------


def _reverse_direction(g: SchematicGraph) -> SchematicGraph:
    """Flip every `powered_by` and `decouples` edge direction."""
    flipped = []
    for e in g.typed_edges:
        if e.kind in ("powered_by", "decouples"):
            flipped.append(TypedEdge(src=e.dst, dst=e.src, kind=e.kind, page=e.page))
        else:
            flipped.append(e)
    return g.model_copy(update={"typed_edges": flipped})


def test_power_rails_consumers_populated_regardless_of_powered_by_direction():
    canonical = _mnt_regulator_graph()
    reversed_graph = _reverse_direction(canonical)

    elec_canon = compile_electrical_graph(canonical)
    elec_rev = compile_electrical_graph(reversed_graph)

    assert set(elec_canon.power_rails["+5V"].consumers) == {"U13", "U19"}
    assert set(elec_rev.power_rails["+5V"].consumers) == {"U13", "U19"}
    assert elec_canon.power_rails["30V_GATE"].consumers == elec_rev.power_rails[
        "30V_GATE"
    ].consumers


def test_depends_on_edges_derive_from_either_direction():
    reversed_graph = _reverse_direction(_mnt_regulator_graph())
    elec = compile_electrical_graph(reversed_graph)
    deps = {(e.src, e.dst) for e in elec.typed_edges if e.kind == "depends_on"}
    assert ("U13", "U7") in deps
    assert ("U19", "U7") in deps


def test_boot_sequence_filters_out_non_component_strings():
    """A `powered_by` edge emitted as (rail_label, component) must not place
    the rail label (a string) into `components_entering` of any phase."""
    reversed_graph = _reverse_direction(_mnt_regulator_graph())
    elec = compile_electrical_graph(reversed_graph)
    all_entering: set[str] = set()
    for phase in elec.boot_sequence:
        all_entering.update(phase.components_entering)
    # Rail labels should NEVER appear as components.
    for rail_label in ("+5V", "+3V3", "+1V2", "+1V5", "+1V8", "30V_GATE"):
        assert rail_label not in all_entering
    # Real components should.
    assert all_entering == {"U7", "U12", "U13", "U14", "U17", "U19"}


def test_decouples_edge_direction_tolerant():
    """`decouples` edges emitted as (rail, passive) or (passive, rail) should
    both populate the rail's `decoupling` list."""
    g = _mnt_regulator_graph()
    reversed_g = _reverse_direction(g)
    elec = compile_electrical_graph(reversed_g)
    assert elec.power_rails["30V_GATE"].decoupling == ["C16"]
    assert elec.power_rails["+5V"].decoupling == ["C33"]


@pytest.mark.parametrize(
    "ground_label",
    ["GND", "AGND", "DGND", "PGND", "SGND", "GND_1", "AGND_CODEC"],
)
def test_ground_nets_excluded_from_power_rails(ground_label: str):
    """Vision tags GND nets with is_power=True; compiler must NOT promote them
    to power_rails. They have hundreds of pin connections that would drown
    every other rail in the downstream UI."""
    g = SchematicGraph(
        device_slug="demo",
        source_pdf="/tmp/demo.pdf",
        page_count=1,
        components={
            "U1": ComponentNode(refdes="U1", type="ic", pages=[1]),
        },
        nets={
            "+3V3": NetNode(label="+3V3", is_power=True, is_global=True, pages=[1]),
            ground_label: NetNode(label=ground_label, is_power=True, is_global=True, pages=[1]),
        },
        typed_edges=[],
    )
    elec = compile_electrical_graph(g)
    assert "+3V3" in elec.power_rails
    assert ground_label not in elec.power_rails


# ----------------------------------------------------------------------
# Phase 4 — passive classifier integration
# ----------------------------------------------------------------------


def test_compile_populates_passive_kind_and_role():
    """After compilation, every passive has kind=passive_* and a role
    (or null) on the ComponentNode."""
    from api.pipeline.schematic.compiler import compile_electrical_graph
    from api.pipeline.schematic.schemas import (
        ComponentNode,
        NetNode,
        PagePin,
        SchematicGraph,
        TypedEdge,
    )

    graph = SchematicGraph(
        device_slug="compiler-passive-test",
        source_pdf="n/a", page_count=1,
        components={
            "U1": ComponentNode(
                refdes="U1", type="ic",
                pins=[
                    PagePin(number="1", role="power_out", net_label="+3V3"),
                ],
            ),
            "U7": ComponentNode(
                refdes="U7", type="ic",
                pins=[
                    PagePin(number="1", role="power_in", net_label="+3V3"),
                ],
            ),
            "C156": ComponentNode(
                refdes="C156", type="capacitor",
                pins=[
                    PagePin(number="1", role="unknown", net_label="+3V3"),
                    PagePin(number="2", role="unknown", net_label="GND"),
                ],
            ),
        },
        nets={
            "+3V3": NetNode(label="+3V3", is_power=True, is_global=True),
            "GND":  NetNode(label="GND",  is_power=True, is_global=True),
        },
        typed_edges=[
            TypedEdge(src="U1", dst="+3V3", kind="powers"),
        ],
    )
    result = compile_electrical_graph(graph)
    # IC kept as-is
    assert result.components["U1"].kind == "ic"
    assert result.components["U1"].role is None
    # Passive classified
    assert result.components["C156"].kind == "passive_c"
    assert result.components["C156"].role in {"decoupling", "filter"}
    # PowerRail.decoupling populated with the refdes
    assert "C156" in result.power_rails["+3V3"].decoupling


# ----------------------------------------------------------------------
# Phantom-consumer scrub — a `powered_by` edge from the vision pass can
# wire a component onto a rail it has no `power_in` pin on (a feedback
# divider tapping VREF, or a plain vision misread). Such a consumer can
# never be killed by the rail dying, so it breaks the simulator's
# dead-rail-implies-dead-consumers contract. The scrub drops a consumer
# only when it HAS pins but none is `power_in` on that rail; a consumer
# with no pin data at all is trusted (edge-only wiring stays supported).
# ----------------------------------------------------------------------


def _phantom_consumer_graph() -> SchematicGraph:
    components = {
        "U_SRC": ComponentNode(
            refdes="U_SRC", type="ic", pages=[1],
            pins=[PagePin(number="1", role="power_out", net_label="VREF")],
        ),
        # Legitimate load: real power_in pin on VREF.
        "U_LOAD": ComponentNode(
            refdes="U_LOAD", type="ic", pages=[1],
            pins=[PagePin(number="1", role="power_in", net_label="VREF")],
        ),
        # Phantom #1: a resistor tapping VREF through a feedback pin, never a load.
        "R_FB": ComponentNode(
            refdes="R_FB", type="resistor", pages=[1],
            pins=[
                PagePin(number="1", role="feedback_in", net_label="VREF"),
                PagePin(number="2", role="ground", net_label="GND"),
            ],
        ),
        # Phantom #2: an IC with pins, but none on VREF (powered elsewhere).
        "U_ELSE": ComponentNode(
            refdes="U_ELSE", type="ic", pages=[1],
            pins=[PagePin(number="1", role="power_in", net_label="V_OTHER")],
        ),
        # Edge-only consumer with no pin data — must stay (mnt-style).
        "U_NOPIN": ComponentNode(refdes="U_NOPIN", type="ic", pages=[1]),
    }
    nets = {
        "VREF": NetNode(label="VREF", is_power=True, is_global=True, pages=[1]),
        "GND": NetNode(label="GND", is_power=True, pages=[1]),
        "V_OTHER": NetNode(label="V_OTHER", is_power=True, pages=[1]),
    }
    edges = [
        TypedEdge(src="U_SRC", dst="VREF", kind="powers", page=1),
        TypedEdge(src="U_LOAD", dst="VREF", kind="powered_by", page=1),
        TypedEdge(src="R_FB", dst="VREF", kind="powered_by", page=1),
        TypedEdge(src="U_ELSE", dst="VREF", kind="powered_by", page=1),
        TypedEdge(src="U_NOPIN", dst="VREF", kind="powered_by", page=1),
    ]
    return SchematicGraph(
        device_slug="phantom-demo", source_pdf="x.pdf", page_count=1,
        components=components, nets=nets, typed_edges=edges,
    )


def test_phantom_consumers_without_power_in_pin_are_scrubbed():
    elec = compile_electrical_graph(_phantom_consumer_graph())
    consumers = set(elec.power_rails["VREF"].consumers)
    assert "R_FB" not in consumers  # feedback tap, has pins but no power_in here
    assert "U_ELSE" not in consumers  # has pins, none on VREF
    assert "U_LOAD" in consumers  # real power_in load
    assert "U_NOPIN" in consumers  # no pin data — edge wiring trusted


def _dual_label_rail_graph() -> SchematicGraph:
    """A cap pin (`C1.1`) the vision pass placed on TWO power-net labels —
    the die-side (`VDD_DIE`) and the package-side (`PP_PKG`) of one physical
    rail (the Apple PP_/VDD_ convention). The coalescer merges them; `PP_PKG`
    wins canonical (it carries the source). An IC draws power on the die-side
    label, which is the one that gets dropped.
    """
    components = {
        "U_SRC": ComponentNode(
            refdes="U_SRC", type="ic", pages=[1],
            pins=[PagePin(number="2", role="power_out", net_label="PP_PKG")],
        ),
        "C1": ComponentNode(refdes="C1", type="capacitor", pages=[1]),
        "U_DIE": ComponentNode(
            refdes="U_DIE", type="ic", pages=[1],
            pins=[PagePin(number="1", role="power_in", net_label="VDD_DIE")],
        ),
    }
    nets = {
        "VDD_DIE": NetNode(label="VDD_DIE", is_power=True, pages=[1],
                           connects=["C1.1", "U_DIE.1"]),
        "PP_PKG": NetNode(label="PP_PKG", is_power=True, pages=[1],
                          connects=["C1.1", "U_SRC.2"]),
        "GND": NetNode(label="GND", is_power=True, pages=[1], connects=["C1.2"]),
    }
    edges = [TypedEdge(src="U_SRC", dst="PP_PKG", kind="powers", page=1)]
    return SchematicGraph(
        device_slug="dual-label", source_pdf="x.pdf", page_count=1,
        components=components, nets=nets, typed_edges=edges,
    )


def test_coalesced_alias_label_is_rewritten_on_component_pins():
    """When two rail labels coalesce into one physical rail, a consumer pin on
    the dropped label must be rewritten to the canonical rail — otherwise the
    pin points at a net that is no longer a rail, the simulator can't see the
    consumer draw from the surviving rail, and dead-rail-implies-dead-consumers
    (INV-5) breaks. Mirrors msi-v311_11's V_PROT→IFPAB_IOVDD / U1.
    """
    elec = compile_electrical_graph(_dual_label_rail_graph())
    assert "VDD_DIE" not in elec.power_rails  # dropped alias
    u_die = elec.components["U_DIE"]
    assert u_die.pins[0].net_label == "PP_PKG"  # rewritten to canonical
    assert "U_DIE" in elec.power_rails["PP_PKG"].consumers


# ----------------------------------------------------------------------
# Edge-only consumers (vision emitted wiring but no pins) must become
# killable: the simulator's cascade is purely pin-driven, so a pin-less
# consumer kept by _scrub_phantom_consumers ("trust the edge") could
# never die with its rail — INV-5 break, first seen on macbook-air-m1
# U7550 (powered_by PPBUS_AON, zero pins on page 79).
# ----------------------------------------------------------------------


def _edge_only_consumer_graph() -> SchematicGraph:
    components = {
        "U_SRC": ComponentNode(
            refdes="U_SRC", type="ic", pages=[1],
            pins=[PagePin(number="2", role="power_out", net_label="PP_MAIN")],
        ),
        # Pin-less consumer — vision saw the powered_by edge, no pins.
        "U_EDGE": ComponentNode(refdes="U_EDGE", type="ic", pages=[1], pins=[]),
        # Consumer with pins but none on PP_MAIN — phantom, must stay scrubbed.
        "U_PHANTOM": ComponentNode(
            refdes="U_PHANTOM", type="ic", pages=[1],
            pins=[PagePin(number="1", role="power_in", net_label="PP_OTHER")],
        ),
    }
    nets = {
        "PP_MAIN": NetNode(label="PP_MAIN", is_power=True, pages=[1],
                           connects=["U_SRC.2"]),
        "PP_OTHER": NetNode(label="PP_OTHER", is_power=True, pages=[1],
                            connects=["U_PHANTOM.1"]),
    }
    edges = [
        TypedEdge(src="U_SRC", dst="PP_MAIN", kind="powers", page=1),
        TypedEdge(src="U_EDGE", dst="PP_MAIN", kind="powered_by", page=1),
        TypedEdge(src="U_PHANTOM", dst="PP_MAIN", kind="powered_by", page=1),
    ]
    return SchematicGraph(
        device_slug="edge-only", source_pdf="x.pdf", page_count=1,
        components=components, nets=nets, typed_edges=edges,
    )


def test_edge_only_consumer_gets_synthetic_power_in_pin():
    elec = compile_electrical_graph(_edge_only_consumer_graph())
    rail = elec.power_rails["PP_MAIN"]
    assert "U_EDGE" in rail.consumers
    pins = elec.components["U_EDGE"].pins
    assert len(pins) == 1
    assert pins[0].role == "power_in"
    assert pins[0].net_label == "PP_MAIN"
    # The phantom keeps its real pins untouched and stays scrubbed.
    assert "U_PHANTOM" not in rail.consumers
    assert [p.net_label for p in elec.components["U_PHANTOM"].pins] == ["PP_OTHER"]


def test_edge_only_consumer_dies_with_its_rail():
    """End-to-end INV-5 contract on the mini-fixture: kill the rail source,
    the edge-only consumer must land in cascade_dead_components."""
    from api.pipeline.schematic.simulator import Failure, SimulationEngine

    elec = compile_electrical_graph(_edge_only_consumer_graph())
    tl = SimulationEngine(
        elec, failures=[Failure(refdes="U_SRC", mode="dead")]
    ).run()
    assert "PP_MAIN" in tl.cascade_dead_rails
    assert "U_EDGE" in tl.cascade_dead_components


# ----------------------------------------------------------------------
# Untraced-component evidence stamping
# ----------------------------------------------------------------------


def _alias_page_graph() -> SchematicGraph:
    """Power-alias-page shape from the A2337 incident: 'U9000' exists only as
    a section title (zero pins, zero net membership) yet sources a rail via a
    grouping-inferred `powers` edge; 'U2' is an edge-only consumer."""
    return SchematicGraph(
        device_slug="alias-demo",
        source_pdf="alias-demo.pdf",
        page_count=1,
        components={
            "U9000": ComponentNode(refdes="U9000", type="ic", pages=[79]),
            "U2": ComponentNode(refdes="U2", type="ic", pages=[79]),
            "U1": ComponentNode(
                refdes="U1",
                type="ic",
                pages=[79],
                pins=[
                    PagePin(number="1", name="VIN", role="power_in", net_label="+5V"),
                    PagePin(number="2", name="GND", role="ground", net_label="GND"),
                ],
            ),
        },
        nets={
            "+5V": NetNode(label="+5V", is_power=True, pages=[79], connects=["U1.1"]),
        },
        typed_edges=[
            TypedEdge(src="U9000", dst="+5V", kind="powers", page=79),
            TypedEdge(src="U2", dst="+5V", kind="powered_by", page=79),
        ],
    )


def test_compile_marks_pinless_components_untraced():
    eg = compile_electrical_graph(_alias_page_graph())
    assert eg.components["U9000"].evidence == "untraced"
    assert eg.components["U2"].evidence == "untraced"
    assert eg.components["U1"].evidence == "traced"
    assert eg.quality.components_untraced == 2


def test_untraced_stamp_survives_synthetic_pin_materialization():
    """`_synthesize_pins_for_edge_only_consumers` gives edge-only consumers
    synthetic `number="?"` pins — those must not flip the evidence back."""
    eg = compile_electrical_graph(_alias_page_graph())
    u2 = eg.components["U2"]
    if u2.pins:  # synthesized for the edge-only consumer
        assert all(p.number == "?" for p in u2.pins)
    assert u2.evidence == "untraced"


def test_untraced_source_still_sources_rail():
    """Marking is epistemic only — topology is preserved (the rail keeps its
    inferred producer so the simulator cascade stays intact)."""
    eg = compile_electrical_graph(_alias_page_graph())
    assert eg.power_rails["+5V"].source_refdes == "U9000"
