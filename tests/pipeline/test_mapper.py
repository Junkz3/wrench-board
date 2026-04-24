# SPDX-License-Identifier: Apache-2.0
"""Tests for the Refdes Mapper agent — phase 2.5.

The LLM call is forced-tool and Pydantic-validated by call_with_forced_tool;
the protection that *actually* prevents fabrication is the server-side
post-validator. These tests exercise that validator on hand-crafted
attribution lists and assert each of the three rules drops the correct
shapes (canonical missing, refdes not in graph, evidence_quote not in dump,
refdes not in quote for literal kind, MPN absent / not in quote for MPN
kind).

Prompt assembly is also tested so we don't regress on how the user
message is composed.
"""

from __future__ import annotations

from api.pipeline.mapper import (
    _build_graph_block,
    _validate_attributions,
)
from api.pipeline.schemas import (
    DeviceTaxonomy,
    RefdesAttribution,
    RefdesMappings,
    Registry,
    RegistryComponent,
)
from api.pipeline.schematic.schemas import (
    ComponentNode,
    ComponentValue,
    ElectricalGraph,
    PowerRail,
    SchematicQualityReport,
)


def _toy_registry() -> Registry:
    return Registry(
        device_label="demo",
        taxonomy=DeviceTaxonomy(brand="MNT", model="Reform"),
        components=[
            RegistryComponent(canonical_name="LPC controller", kind="ic"),
            RegistryComponent(canonical_name="main buck", kind="ic"),
        ],
    )


def _toy_graph() -> ElectricalGraph:
    return ElectricalGraph(
        device_slug="demo",
        components={
            "U7": ComponentNode(
                refdes="U7",
                type="ic",
                kind="ic",
                role="buck_regulator",
                value=ComponentValue(raw="LM2677SX-5", mpn="LM2677SX-5"),
            ),
            "U14": ComponentNode(
                refdes="U14",
                type="ic",
                kind="ic",
                role="mcu",
                value=ComponentValue(raw="LPC11U24FBD48", mpn="LPC11U24FBD48"),
            ),
        },
        power_rails={
            "+5V": PowerRail(label="+5V", voltage_nominal=5.0, source_refdes="U7"),
        },
        boot_sequence=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )


# --- Validator: rule 1 (canonical in registry) ----------------------------


def test_validator_drops_unknown_canonical() -> None:
    dump = "the LPC controller (U14) is dead and won't ack"
    mappings = RefdesMappings(
        device_slug="demo",
        attributions=[
            RefdesAttribution(
                canonical_name="this canonical does not exist in registry",
                refdes="U14",
                confidence=0.9,
                evidence_kind="literal_refdes_in_quote",
                evidence_quote="the LPC controller (U14) is dead and won't ack",
                reasoning="evidence quote contains U14 literally",
            )
        ],
    )
    out = _validate_attributions(mappings, raw_dump=dump, registry=_toy_registry(), graph=_toy_graph())
    assert out.attributions == []


# --- Validator: rule 2 (refdes in graph) ----------------------------------


def test_validator_drops_refdes_not_in_graph() -> None:
    dump = "the LPC controller (U99) is dead"
    mappings = RefdesMappings(
        device_slug="demo",
        attributions=[
            RefdesAttribution(
                canonical_name="LPC controller",
                refdes="U99",  # not in graph
                confidence=0.9,
                evidence_kind="literal_refdes_in_quote",
                evidence_quote="the LPC controller (U99) is dead",
                reasoning="quote cites U99 literally",
            )
        ],
    )
    out = _validate_attributions(mappings, raw_dump=dump, registry=_toy_registry(), graph=_toy_graph())
    assert out.attributions == []


# --- Validator: rule 3 (evidence_quote ⊂ dump, literal) -------------------


def test_validator_drops_quote_not_in_dump() -> None:
    dump = "the LPC controller (U14) is dead"
    mappings = RefdesMappings(
        device_slug="demo",
        attributions=[
            RefdesAttribution(
                canonical_name="LPC controller",
                refdes="U14",
                confidence=0.9,
                evidence_kind="literal_refdes_in_quote",
                # Subtle: extra whitespace breaks the substring match.
                evidence_quote="the  LPC  controller  (U14)  is  dead",
                reasoning="paraphrase / non-literal",
            )
        ],
    )
    out = _validate_attributions(mappings, raw_dump=dump, registry=_toy_registry(), graph=_toy_graph())
    assert out.attributions == []


# --- Validator: rule 4 (literal_refdes_in_quote — refdes ∈ quote) ---------


def test_validator_drops_literal_refdes_kind_when_refdes_absent() -> None:
    dump = "the LPC controller is dead and won't acknowledge i2c reset"
    mappings = RefdesMappings(
        device_slug="demo",
        attributions=[
            RefdesAttribution(
                canonical_name="LPC controller",
                refdes="U14",
                confidence=0.9,
                evidence_kind="literal_refdes_in_quote",
                evidence_quote="the LPC controller is dead and won't acknowledge",
                reasoning="quote is literal but refdes U14 missing",
            )
        ],
    )
    out = _validate_attributions(mappings, raw_dump=dump, registry=_toy_registry(), graph=_toy_graph())
    assert out.attributions == []


def test_validator_keeps_literal_refdes_kind_when_refdes_present_case_insensitive() -> None:
    dump = "the lpc controller (u14) is dead and won't ack on i2c reset"
    mappings = RefdesMappings(
        device_slug="demo",
        attributions=[
            RefdesAttribution(
                canonical_name="LPC controller",
                refdes="U14",
                confidence=0.95,
                evidence_kind="literal_refdes_in_quote",
                evidence_quote="the lpc controller (u14) is dead and won't ack",
                reasoning="lowercase u14 is the same refdes — case-insensitive match",
            )
        ],
    )
    out = _validate_attributions(mappings, raw_dump=dump, registry=_toy_registry(), graph=_toy_graph())
    assert len(out.attributions) == 1
    assert out.attributions[0].refdes == "U14"


# --- Validator: rule 5 (mpn_match_in_quote — graph MPN ∈ quote) -----------


def test_validator_drops_mpn_kind_when_no_mpn_in_graph() -> None:
    dump = "the buck regulator failed open"
    graph = _toy_graph()
    # Strip the MPN on U7 so the contract can't hold.
    graph.components["U7"].value = None
    mappings = RefdesMappings(
        device_slug="demo",
        attributions=[
            RefdesAttribution(
                canonical_name="main buck",
                refdes="U7",
                confidence=0.85,
                evidence_kind="mpn_match_in_quote",
                evidence_quote="the buck regulator failed open and won't restart",
                reasoning="dump mentions buck — but graph has no MPN to anchor the match",
            )
        ],
    )
    out = _validate_attributions(mappings, raw_dump=dump + " and won't restart", registry=_toy_registry(), graph=graph)
    assert out.attributions == []


def test_validator_drops_mpn_kind_when_mpn_not_in_quote() -> None:
    # Dump cites a different MPN than the graph holds for U7.
    dump = "the LM2596 buck regulator failed open"
    mappings = RefdesMappings(
        device_slug="demo",
        attributions=[
            RefdesAttribution(
                canonical_name="main buck",
                refdes="U7",
                confidence=0.85,
                evidence_kind="mpn_match_in_quote",
                evidence_quote="the LM2596 buck regulator failed open",
                reasoning="model named the wrong MPN — graph says LM2677SX-5",
            )
        ],
    )
    out = _validate_attributions(mappings, raw_dump=dump, registry=_toy_registry(), graph=_toy_graph())
    assert out.attributions == []


def test_validator_keeps_mpn_kind_when_mpn_literal_in_quote() -> None:
    dump = "the LM2677SX-5 buck regulator failed open on the bench"
    mappings = RefdesMappings(
        device_slug="demo",
        attributions=[
            RefdesAttribution(
                canonical_name="main buck",
                refdes="U7",
                confidence=0.85,
                evidence_kind="mpn_match_in_quote",
                evidence_quote="the LM2677SX-5 buck regulator failed open",
                reasoning="dump cites the exact MPN that the graph ties to U7",
            )
        ],
    )
    out = _validate_attributions(mappings, raw_dump=dump, registry=_toy_registry(), graph=_toy_graph())
    assert len(out.attributions) == 1
    assert out.attributions[0].refdes == "U7"
    assert out.attributions[0].evidence_kind == "mpn_match_in_quote"


# --- Empty mapping is a legitimate output ---------------------------------


def test_validator_passes_empty_mapping() -> None:
    out = _validate_attributions(
        RefdesMappings(device_slug="demo", attributions=[]),
        raw_dump="anything",
        registry=_toy_registry(),
        graph=_toy_graph(),
    )
    assert out.attributions == []


# --- Multiple attributions, mixed validity --------------------------------


def test_validator_partitions_mixed_batch() -> None:
    dump = (
        "Forum thread: the LPC controller (U14) does not respond to power-on. "
        "A separate post says LM2677SX-5 buck stops switching after 30 minutes."
    )
    mappings = RefdesMappings(
        device_slug="demo",
        attributions=[
            # legit literal_refdes
            RefdesAttribution(
                canonical_name="LPC controller",
                refdes="U14",
                confidence=0.95,
                evidence_kind="literal_refdes_in_quote",
                evidence_quote="the LPC controller (U14) does not respond to power-on",
                reasoning="dump cites U14 literally",
            ),
            # legit mpn match
            RefdesAttribution(
                canonical_name="main buck",
                refdes="U7",
                confidence=0.85,
                evidence_kind="mpn_match_in_quote",
                evidence_quote="LM2677SX-5 buck stops switching after 30 minutes",
                reasoning="dump cites the exact MPN U7 carries",
            ),
            # bogus: refdes not in graph
            RefdesAttribution(
                canonical_name="main buck",
                refdes="U99",
                confidence=0.5,
                evidence_kind="literal_refdes_in_quote",
                evidence_quote="the LPC controller (U14) does not respond to power-on",
                reasoning="invalid refdes",
            ),
        ],
    )
    out = _validate_attributions(mappings, raw_dump=dump, registry=_toy_registry(), graph=_toy_graph())
    assert {a.refdes for a in out.attributions} == {"U14", "U7"}


# --- Prompt / graph block assembly ----------------------------------------


def test_graph_block_lists_components_with_mpn_and_rails() -> None:
    block = _build_graph_block(_toy_graph())
    assert "## Components" in block
    assert "U7: mpn=LM2677SX-5" in block
    assert "U14: mpn=LPC11U24FBD48" in block
    assert "## Power rails" in block
    assert "+5V: voltage=5.00V source=U7" in block
