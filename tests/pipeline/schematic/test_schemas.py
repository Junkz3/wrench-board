"""Smoke tests for api.pipeline.schematic.schemas.

These verify that the models instantiate, round-trip through JSON, reject
unknown fields, and expose a valid JSON Schema for use as forced-tool
`input_schema`. They do NOT assert specific field behaviour — that lives
with the merger / value_parser tests once those modules land.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.pipeline.schematic.schemas import (
    Ambiguity,
    BootPhase,
    ComponentNode,
    ComponentValue,
    CrossPageRef,
    DesignerNote,
    ElectricalGraph,
    NetNode,
    PageNet,
    PageNode,
    PagePin,
    PowerRail,
    SchematicGraph,
    SchematicPageGraph,
    SchematicQualityReport,
    TypedEdge,
)


def _make_regulator_page() -> SchematicPageGraph:
    """Build a realistic per-page fixture modelled on page 3 of MNT Reform v2.5."""
    u7 = PageNode(
        refdes="U7",
        type="ic",
        value=ComponentValue(
            raw="LM2677SX-5",
            primary="LM2677SX-5",
            mpn="LM2677SX-5",
            description="5V buck converter, up to 5A",
        ),
        page=3,
        pins=[
            PagePin(number="1", name="VIN", role="power_in", net_label="30V_GATE"),
            PagePin(number="2", name="FB", role="feedback_in"),
            PagePin(number="5", name="VSW", role="switch_node"),
            PagePin(number="7", name="ON/OFF", role="enable_in", net_label="5V_PWR_EN"),
        ],
    )
    c16 = PageNode(
        refdes="C16",
        type="capacitor",
        value=ComponentValue(raw="100uF", primary="100µF"),
        page=3,
    )
    r117 = PageNode(
        refdes="R117",
        type="resistor",
        value=ComponentValue(raw="10k", primary="10kΩ", description="NOSTUFF"),
        page=3,
        populated=False,
    )
    return SchematicPageGraph(
        page=3,
        sheet_name="Reform 2 Regulators",
        sheet_path="/Reform 2 Power/Reform 2 Regulators/",
        page_kind="schematic",
        confidence=0.95,
        nodes=[u7, c16, r117],
        nets=[
            PageNet(
                local_id="net_0001",
                label="30V_GATE",
                is_power=True,
                is_global=True,
                connects=["U7.1", "C16.1"],
                page=3,
            ),
            PageNet(
                local_id="net_0002",
                label="+5V",
                is_power=True,
                is_global=True,
                connects=["U7.5"],
                page=3,
            ),
        ],
        cross_page_refs=[
            CrossPageRef(
                label="5V_PWR_EN",
                direction="in",
                at_pin="U7.7",
                page=3,
            ),
        ],
        typed_edges=[
            TypedEdge(src="U7", dst="+5V", kind="powers", page=3),
            TypedEdge(src="U7", dst="30V_GATE", kind="powered_by", page=3),
            TypedEdge(src="5V_PWR_EN", dst="U7", kind="enables", page=3),
            TypedEdge(src="C16", dst="30V_GATE", kind="decouples", page=3),
        ],
        designer_notes=[
            DesignerNote(
                text="Main system power converters, enabled by LPC",
                page=3,
                attached_to_refdes="U7",
            )
        ],
    )


def test_schematic_page_graph_roundtrip():
    page = _make_regulator_page()
    blob = page.model_dump_json()
    rebuilt = SchematicPageGraph.model_validate_json(blob)
    assert rebuilt == page


def test_component_value_nullable_fields_default_to_none():
    v = ComponentValue(raw="100nF")
    assert v.primary is None
    assert v.mpn is None
    assert v.package is None
    assert v.polarity_marker is False


def test_vision_facing_models_silently_drop_unknown_fields():
    """Vision-reachable leaves use `extra='ignore'` — avoids costly retries
    when the model emits a legitimate but uncatalogued field like
    `enable_out`, `target_label`, or `polarity_marker` on the wrong layer.
    Strict schemas are a false guardrail here; the real anti-hallucination
    check is refdes/net validation against the registry downstream."""
    tolerant_cases: list[tuple[type, dict]] = [
        (ComponentValue, {"raw": "100nF", "hallucinated": 42}),
        (
            PagePin,
            {"number": "1", "name": "VIN", "role": "power_in", "foo": "bar"},
        ),
        (
            PageNode,
            {"refdes": "U7", "type": "ic", "page": 3, "bogus": True},
        ),
        (
            PageNet,
            {"local_id": "net_0001", "connects": [], "page": 3, "extra": 1},
        ),
        (
            CrossPageRef,
            {"label": "X", "direction": "in", "page": 3, "target_label": "X_2"},
        ),
        (
            TypedEdge,
            {"src": "U7", "dst": "+5V", "kind": "powers", "wat": 1},
        ),
    ]
    for cls, kwargs in tolerant_cases:
        instance = cls(**kwargs)
        # The extra field must not land on the validated model.
        for extra_key in ("hallucinated", "foo", "bogus", "extra", "target_label", "wat"):
            assert not hasattr(instance, extra_key) or getattr(instance, extra_key, None) is None


def test_internal_models_still_forbid_unknown_fields():
    """Internal models (merger/compiler outputs) aren't fed by the LLM, so
    strict rejection stays useful as a bug-catcher on our own code."""
    strict_cases: list[tuple[type, dict]] = [
        (
            ComponentNode,
            {"refdes": "U7", "type": "ic", "hallucinated": 42},
        ),
        (
            NetNode,
            {"label": "+5V", "bogus": True},
        ),
        (
            PowerRail,
            {"label": "+5V", "wat": 1},
        ),
        (
            BootPhase,
            {"index": 1, "name": "x", "extra": 1},
        ),
    ]
    for cls, kwargs in strict_cases:
        with pytest.raises(ValidationError):
            cls(**kwargs)


def test_populated_defaults_to_true_and_can_be_false():
    ok = PageNode(refdes="R1", type="resistor", page=1)
    assert ok.populated is True

    dnp = PageNode(refdes="R117", type="resistor", page=3, populated=False)
    assert dnp.populated is False


def test_schematic_graph_indexed_by_refdes_and_label():
    u7 = ComponentNode(refdes="U7", type="ic", pages=[1, 3])
    r117 = ComponentNode(refdes="R117", type="resistor", pages=[3], populated=False)
    plus5v = NetNode(
        label="+5V",
        is_power=True,
        is_global=True,
        pages=[1, 3, 5, 8],
        connects=["U7.5", "U22.3"],
    )
    graph = SchematicGraph(
        device_slug="mnt-reform-motherboard",
        source_pdf="board_assets/mnt-reform-motherboard.pdf",
        page_count=12,
        hierarchy=["/Reform 2 Power/Reform 2 Regulators/"],
        components={"U7": u7, "R117": r117},
        nets={"+5V": plus5v},
    )
    assert graph.components["U7"].refdes == "U7"
    assert graph.nets["+5V"].is_global is True
    assert graph.page_count == 12


def test_electrical_graph_requires_quality_report():
    with pytest.raises(ValidationError):
        ElectricalGraph(device_slug="x")  # missing `quality`

    graph = ElectricalGraph(
        device_slug="mnt-reform-motherboard",
        quality=SchematicQualityReport(total_pages=12, pages_parsed=12),
        power_rails={
            "+5V": PowerRail(
                label="+5V",
                voltage_nominal=5.0,
                source_refdes="U7",
                source_type="buck",
                enable_net="5V_PWR_EN",
            )
        },
        boot_sequence=[
            BootPhase(
                index=1,
                name="PHASE 1 — Cold plug",
                rails_stable=["LPC_VCC"],
                triggers_next=["5V_PWR_EN", "3V3_PWR_EN"],
            ),
        ],
    )
    assert graph.quality.degraded_mode is False
    assert graph.power_rails["+5V"].source_refdes == "U7"


def test_confidence_bounds_are_enforced():
    with pytest.raises(ValidationError):
        SchematicPageGraph(page=1, confidence=1.5)
    with pytest.raises(ValidationError):
        SchematicQualityReport(
            total_pages=1, pages_parsed=1, confidence_global=-0.1
        )


def test_every_model_exposes_a_json_schema():
    """All models must expose a JSON schema usable as a forced-tool input
    shape. Internal models are strict (`additionalProperties: false`); vision
    models are deliberately permissive (no `additionalProperties` constraint)
    to keep the LLM from failing validation on long-tail fields."""
    for cls in (
        ComponentValue,
        PagePin,
        PageNode,
        PageNet,
        CrossPageRef,
        TypedEdge,
        DesignerNote,
        Ambiguity,
        SchematicPageGraph,
        ComponentNode,
        NetNode,
        SchematicGraph,
        PowerRail,
        BootPhase,
        SchematicQualityReport,
        ElectricalGraph,
    ):
        schema = cls.model_json_schema()
        assert schema["type"] == "object"
        assert "properties" in schema

    strict_classes = (
        ComponentNode,
        NetNode,
        SchematicGraph,
        PowerRail,
        BootPhase,
        SchematicQualityReport,
        ElectricalGraph,
    )
    for cls in strict_classes:
        assert cls.model_json_schema().get("additionalProperties") is False


def test_component_node_defaults_to_ic_kind_and_null_role():
    """Phase 1 data on disk reloads unchanged — default kind="ic"."""
    node = ComponentNode(refdes="U7", type="ic")
    assert node.kind == "ic"
    assert node.role is None


def test_component_node_accepts_passive_kind():
    node = ComponentNode(
        refdes="C156", type="capacitor",
        kind="passive_c", role="decoupling",
    )
    assert node.kind == "passive_c"
    assert node.role == "decoupling"


def test_component_node_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        ComponentNode(refdes="Q5", type="transistor", kind="passive_q")


def test_component_node_role_is_free_form_string():
    """Role follows the PinRole pattern — free-form string, not enum."""
    node = ComponentNode(
        refdes="R42", type="resistor",
        kind="passive_r", role="some_new_role_not_yet_canonical",
    )
    assert node.role == "some_new_role_not_yet_canonical"


def test_component_node_round_trip_preserves_kind_and_role():
    original = ComponentNode(
        refdes="FB2", type="ferrite",
        kind="passive_fb", role="filter",
    )
    restored = ComponentNode.model_validate(original.model_dump())
    assert restored.kind == "passive_fb"
    assert restored.role == "filter"
