"""Tests for api.pipeline.schematic.merger.

Every scenario is driven by hand-made SchematicPageGraph fixtures — no PDF,
no Claude, no I/O. Covers stitching, pin union, NOSTUFF stickiness, type
conflicts, orphan cross-page detection, hierarchy ordering and edge/note
deduplication.
"""

from __future__ import annotations

from api.pipeline.schematic.merger import merge_pages
from api.pipeline.schematic.schemas import (
    ComponentValue,
    CrossPageRef,
    DesignerNote,
    PageNet,
    PageNode,
    PagePin,
    SchematicPageGraph,
    TypedEdge,
)


def _page(page: int, **kwargs) -> SchematicPageGraph:
    kwargs.setdefault("nodes", [])
    kwargs.setdefault("nets", [])
    kwargs.setdefault("cross_page_refs", [])
    kwargs.setdefault("typed_edges", [])
    kwargs.setdefault("designer_notes", [])
    kwargs.setdefault("ambiguities", [])
    return SchematicPageGraph(page=page, **kwargs)


def test_labelled_nets_stitch_across_pages():
    p1 = _page(
        1,
        nets=[
            PageNet(
                local_id="n1",
                label="+5V",
                is_power=True,
                is_global=True,
                connects=["U1.1"],
                page=1,
            )
        ],
    )
    p3 = _page(
        3,
        nets=[
            PageNet(
                local_id="n1",
                label="+5V",
                is_power=True,
                is_global=True,
                connects=["U7.5"],
                page=3,
            )
        ],
    )
    g = merge_pages([p1, p3], device_slug="demo", source_pdf="demo.pdf")
    plus5v = g.nets["+5V"]
    assert plus5v.pages == [1, 3]
    assert set(plus5v.connects) == {"U1.1", "U7.5"}
    assert plus5v.is_global is True


def test_unlabelled_net_gets_synthetic_local_key():
    p = _page(
        5,
        nets=[
            PageNet(
                local_id="net_0007",
                label=None,
                connects=["U11.2", "U12.3"],
                page=5,
            )
        ],
    )
    g = merge_pages([p], device_slug="demo", source_pdf="demo.pdf")
    assert "__local__5__net_0007" in g.nets
    assert g.nets["__local__5__net_0007"].connects == ["U11.2", "U12.3"]


def test_same_refdes_pins_are_unioned_across_pages():
    node_page1 = PageNode(
        refdes="U1",
        type="ic",
        page=1,
        pins=[
            PagePin(number="1", name="VDD", role="power_in", net_label="+5V"),
            PagePin(number="2", name="GND", role="ground", net_label="GND"),
        ],
    )
    node_page2 = PageNode(
        refdes="U1",
        type="ic",
        page=2,
        pins=[
            PagePin(number="2", name=None, role="unknown", net_label=None),
            PagePin(number="5", name="VSW", role="switch_node"),
        ],
    )
    g = merge_pages(
        [_page(1, nodes=[node_page1]), _page(2, nodes=[node_page2])],
        device_slug="demo",
        source_pdf="demo.pdf",
    )
    u1 = g.components["U1"]
    assert u1.pages == [1, 2]
    pin_numbers = {p.number for p in u1.pins}
    assert pin_numbers == {"1", "2", "5"}
    # Page 1 info on pin 2 is preserved when page 2 re-emits it with nulls.
    pin2 = next(p for p in u1.pins if p.number == "2")
    assert pin2.name == "GND"
    assert pin2.role == "ground"
    assert pin2.net_label == "GND"


def test_nostuff_is_sticky_across_pages():
    r117_dnp = PageNode(refdes="R117", type="resistor", page=3, populated=False)
    r117_ok = PageNode(refdes="R117", type="resistor", page=7, populated=True)
    g = merge_pages(
        [_page(3, nodes=[r117_dnp]), _page(7, nodes=[r117_ok])],
        device_slug="demo",
        source_pdf="demo.pdf",
    )
    assert g.components["R117"].populated is False


def test_pins_reconstructed_from_net_connects():
    """Vision sometimes captures connectivity net-side only (`net.connects`)
    and leaves `node.pins` empty — typical for a wall of decoupling caps drawn
    in a row between a rail and GND. The merger must back-fill `node.pins` from
    `net.connects` so downstream (compiler / simulator / hypothesize) can see
    the connectivity. Mirrors the real msi-v311_11 FBVDDQ decoupling bank.
    """
    cap = PageNode(refdes="C94", type="capacitor", page=8, pins=[])
    p = _page(
        8,
        nodes=[cap],
        nets=[
            PageNet(
                local_id="n1", label="FBVDDQ", is_power=True,
                connects=["C94.1"], page=8,
            ),
            PageNet(
                local_id="n2", label="GND", is_power=True,
                connects=["C94.2"], page=8,
            ),
        ],
    )
    g = merge_pages([p], device_slug="demo", source_pdf="demo.pdf")
    c94 = g.components["C94"]
    by_net = {pin.net_label: pin for pin in c94.pins}
    assert set(by_net) == {"FBVDDQ", "GND"}
    assert by_net["FBVDDQ"].number == "1"
    assert by_net["GND"].number == "2"
    # Connectivity-only reconstruction can't know the electrical role.
    assert all(pin.role == "unknown" for pin in c94.pins)


def test_net_connects_does_not_overwrite_explicit_pins():
    """An explicit `node.pins` entry wins over the `net.connects` back-fill —
    we never clobber a vision-emitted role/name with the role-less synthetic.
    """
    node = PageNode(
        refdes="U1", type="ic", page=1,
        pins=[PagePin(number="1", name="VDD", role="power_in", net_label="PP1V8")],
    )
    p = _page(
        1,
        nodes=[node],
        nets=[
            PageNet(local_id="n1", label="PP1V8", is_power=True,
                    connects=["U1.1"], page=1),
            PageNet(local_id="n2", label="GND", is_power=True,
                    connects=["U1.2"], page=1),
        ],
    )
    g = merge_pages([p], device_slug="demo", source_pdf="demo.pdf")
    u1 = g.components["U1"]
    pin1 = next(p for p in u1.pins if p.number == "1")
    assert pin1.role == "power_in"  # untouched
    assert pin1.name == "VDD"
    # Pin 2 (connects-only) is still back-filled.
    pin2 = next(p for p in u1.pins if p.number == "2")
    assert pin2.net_label == "GND"
    assert pin2.role == "unknown"


def test_net_connects_backfill_ignores_unknown_refdes():
    """A `connects` entry for a refdes with no node (cross-page stub, vision
    typo) must not synthesise a phantom component."""
    p = _page(
        1,
        nodes=[PageNode(refdes="C1", type="capacitor", page=1, pins=[])],
        nets=[
            PageNet(local_id="n1", label="VBUS", is_power=True,
                    connects=["C1.1", "C999.1"], page=1),
        ],
    )
    g = merge_pages([p], device_slug="demo", source_pdf="demo.pdf")
    assert "C999" not in g.components
    assert g.components["C1"].pins[0].net_label == "VBUS"


def test_cross_page_ref_stitches_to_labelled_net():
    ref_page = _page(
        3,
        cross_page_refs=[
            CrossPageRef(label="5V_PWR_EN", direction="in", at_pin="U7.7", page=3)
        ],
    )
    origin_page = _page(
        2,
        nets=[
            PageNet(
                local_id="n1",
                label="5V_PWR_EN",
                connects=["U_LPC.12"],
                page=2,
            )
        ],
    )
    g = merge_pages(
        [ref_page, origin_page], device_slug="demo", source_pdf="demo.pdf"
    )
    assert g.ambiguities == []
    assert "5V_PWR_EN" in g.nets


def test_orphan_cross_page_ref_is_flagged():
    ref_page = _page(
        3,
        cross_page_refs=[
            CrossPageRef(label="MYSTERY_NET", direction="out", page=3)
        ],
    )
    g = merge_pages([ref_page], device_slug="demo", source_pdf="demo.pdf")
    assert any(
        "MYSTERY_NET" in a.related_nets for a in g.ambiguities
    ), g.ambiguities


def test_null_label_cross_page_ref_is_flagged():
    page = _page(
        3,
        cross_page_refs=[
            CrossPageRef(label=None, direction="out", at_pin="U7.22", page=3)
        ],
    )
    g = merge_pages([page], device_slug="demo", source_pdf="demo.pdf")
    assert len(g.ambiguities) == 1
    assert "unreadable" in g.ambiguities[0].description.lower()


def test_two_cross_page_refs_share_a_label_without_a_net():
    # Both sides are off-page connectors, neither side owns a PageNet — still
    # considered stitched since both pages agree on the label.
    p3 = _page(
        3,
        cross_page_refs=[CrossPageRef(label="HSCLK", direction="out", page=3)],
    )
    p7 = _page(
        7,
        cross_page_refs=[CrossPageRef(label="HSCLK", direction="in", page=7)],
    )
    g = merge_pages([p3, p7], device_slug="demo", source_pdf="demo.pdf")
    assert g.ambiguities == []


def test_type_conflict_creates_ambiguity_and_keeps_first_type():
    g = merge_pages(
        [
            _page(1, nodes=[PageNode(refdes="U7", type="ic", page=1)]),
            _page(3, nodes=[PageNode(refdes="U7", type="connector", page=3)]),
        ],
        device_slug="demo",
        source_pdf="demo.pdf",
    )
    assert g.components["U7"].type == "ic"
    assert any(
        "U7" in a.related_refdes and "connector" in a.description
        for a in g.ambiguities
    )


def test_richer_component_value_wins():
    sparse = PageNode(
        refdes="C29",
        type="capacitor",
        value=ComponentValue(raw="100nF"),
        page=3,
    )
    rich = PageNode(
        refdes="C29",
        type="capacitor",
        value=ComponentValue(
            raw="100nF 0402 X7R 50V",
            primary="100nF",
            package="0402",
            temp_coef="X7R",
            voltage_rating="50V",
        ),
        page=7,
    )
    g = merge_pages(
        [_page(3, nodes=[sparse]), _page(7, nodes=[rich])],
        device_slug="demo",
        source_pdf="demo.pdf",
    )
    v = g.components["C29"].value
    assert v is not None
    assert v.package == "0402"
    assert v.temp_coef == "X7R"


def test_hierarchy_preserves_first_appearance_order_and_dedupes():
    pages = [
        _page(1, sheet_path=None),
        _page(3, sheet_path="/Reform 2 Power/Reform 2 Regulators/"),
        _page(5, sheet_path="/Reform 2 PCIe/"),
        _page(7, sheet_path="/Reform 2 Power/Reform 2 Regulators/"),  # dup
    ]
    g = merge_pages(pages, device_slug="demo", source_pdf="demo.pdf")
    assert g.hierarchy == [
        "/Reform 2 Power/Reform 2 Regulators/",
        "/Reform 2 PCIe/",
    ]


def test_typed_edges_are_deduplicated_across_pages():
    same_edge = TypedEdge(src="U7", dst="+5V", kind="powers", page=3)
    p3 = _page(3, typed_edges=[same_edge])
    p7 = _page(7, typed_edges=[same_edge.model_copy(update={"page": 7})])
    g = merge_pages([p3, p7], device_slug="demo", source_pdf="demo.pdf")
    powers_edges = [e for e in g.typed_edges if e.kind == "powers" and e.src == "U7"]
    assert len(powers_edges) == 1


def test_designer_notes_dedupe_on_full_key_not_on_text_alone():
    note = DesignerNote(
        text="Up to 780mA (!) consumed by USB hub",
        page=3,
        attached_to_refdes="U13",
    )
    p3a = _page(3, designer_notes=[note])
    p3b = _page(3, designer_notes=[note.model_copy()])  # exact duplicate
    p5 = _page(
        5,
        designer_notes=[
            DesignerNote(
                text="Up to 780mA (!) consumed by USB hub",
                page=5,  # same text, different page → keep both
                attached_to_refdes="U13",
            )
        ],
    )
    g = merge_pages([p3a, p3b, p5], device_slug="demo", source_pdf="demo.pdf")
    texts = [n.page for n in g.designer_notes]
    assert sorted(texts) == [3, 5]
