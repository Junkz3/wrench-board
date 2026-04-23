"""Pydantic V2 schemas for the schematic ingestion pipeline.

Three compilation levels, each a full artefact on disk:

1. `SchematicPageGraph` — per page, produced by Claude vision via forced tool
   use. One JSON object per PDF page in `memory/{slug}/schematic_pages/`.
2. `SchematicGraph` — flat catalogue after the deterministic merger stitches
   net labels and cross-page references. Persisted as `schematic_graph.json`.
3. `ElectricalGraph` — final interrogeable artefact: typed edges, power rails,
   boot sequence, and a quality report. Persisted as `electrical_graph.json`.

Every shape doubles as a JSON Schema source for the forced-tool `input_schema`
on the vision call, so descriptions are authored for the model, not for human
readers. Follows hard rule #5 — nullable fields rather than fabrication.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ======================================================================
# Shared enums (expressed as Literal unions to match api/pipeline/schemas.py)
# ======================================================================


ComponentType = Literal[
    "resistor",
    "capacitor",
    "inductor",
    "ferrite",
    "ic",
    "transistor",
    "diode",
    "led",
    "connector",
    "crystal",
    "oscillator",
    "fuse",
    "switch",
    "relay",
    "transformer",
    "module",
    "power_symbol",
    "test_point",
    "mounting",
    "antenna",
    "other",
]


# Pin roles and edge kinds are declared as free-form strings rather than
# closed `Literal` unions. A closed enum is a tempting anti-hallucination
# guardrail but in practice the long-tail of legitimate roles (e.g.
# `enable_out` for an MCU driving a regulator EN, `sync_in`, `vref`, `bias`,
# `thermal_pad`, `test_point`) keeps growing — every rejection is a retry,
# which doubles the token spend for zero quality gain. Compiler logic uses
# set membership against a canonical subset (_POWER_PIN_ROLES etc.), so
# unknown values simply don't match and fall through safely.
#
# The canonical values below remain authoritative documentation for what the
# vision model is expected to emit in the common case; they surface in the
# schema `description` fields and the system prompt, but are not enforced.
PinRole = str
"""Canonical values (non-enforced): power_in · power_out · switch_node ·
enable_in · enable_out · power_good_out · reset_in · reset_out · clock_in ·
clock_out · ground · feedback_in · bus_pin · signal_in · signal_out ·
signal_inout · terminal · no_connect · unknown."""


EdgeKind = str
"""Canonical values (non-enforced): powers · powered_by · enables · resets ·
clocks · produces_signal · consumes_signal · decouples · filters · depends_on."""


ComponentKind = Literal[
    "ic",
    "passive_r",
    "passive_c",
    "passive_d",
    "passive_fb",
]
"""Kind of component in the electrical graph. `ic` is the Phase 1 default
(active components: ICs, modules, transistors, connectors, LEDs, crystals,
oscillators). Passive kinds (`passive_r`, `passive_c`, `passive_d`,
`passive_fb`) are Phase 4 additions and are assigned by the passive role
classifier during `compile_electrical_graph`. `passive_q` reserved for a
future Phase 4.5 and intentionally not included."""


PageKind = Literal[
    "cover",
    "schematic",
    "bom",
    "notes",
    "block_diagram",
    "layout",
    "other",
]


CrossPageDirection = Literal["in", "out", "bidir", "subsheet"]


# ======================================================================
# Leaf models (reused across the three levels)
# ======================================================================


class ComponentValue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    raw: str = Field(
        description=(
            "Exact value string as printed on the schematic, e.g. '100nF', '4.7k 1%', "
            "'LM2677SX-5'. Required — if no value is visible, omit the whole ComponentValue."
        )
    )
    primary: str | None = Field(
        default=None,
        description=(
            "Cleaned primary value with unit, e.g. '100nF', '4.7kΩ', '12MHz', "
            "'LM2677SX-5' for an IC. Null if `raw` is already canonical."
        ),
    )
    package: str | None = Field(
        default=None,
        description="Package / footprint if printed ('0402', 'SOT-23', 'QFN-64'). Null otherwise.",
    )
    mpn: str | None = Field(
        default=None,
        description="Manufacturer part number if printed. Null if absent — never invent.",
    )
    mpn_alternate: str | None = Field(
        default=None,
        description="Alternate MPN listed on the page (e.g. 'TE 1717254-1 or TE 1475005-1').",
    )
    tolerance: str | None = Field(
        default=None,
        description="Tolerance if printed ('±1%', '±5%'). Null otherwise.",
    )
    voltage_rating: str | None = Field(
        default=None,
        description="Voltage rating if printed ('50V', '25V'). Null otherwise.",
    )
    temp_coef: str | None = Field(
        default=None,
        description="Dielectric / temp coefficient for caps ('X7R', 'C0G'). Null otherwise.",
    )
    polarity_marker: bool = Field(
        default=False,
        description="True if a pin-1 marker or polarity band is visible on the symbol.",
    )
    description: str | None = Field(
        default=None,
        description="Free-form designer comment if annotated near the component.",
    )


class PagePin(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: str = Field(
        description="Pin number or name as printed. Kept as string because BGA uses 'A3'."
    )
    name: str | None = Field(
        default=None,
        description="Pin functional name if printed inside the symbol ('VIN', 'EN', 'SW').",
    )
    role: PinRole = Field(
        default="unknown",
        description=(
            "Role inferred from pin name + component type. Preferred canonical values: "
            "power_in, power_out, switch_node, enable_in, enable_out, power_good_out, "
            "reset_in, reset_out, clock_in, clock_out, ground, feedback_in, bus_pin, "
            "signal_in, signal_out, signal_inout, terminal, no_connect. Use "
            "'unknown' when the role cannot be determined — never guess. Free-form "
            "values are accepted when the canonical list falls short (e.g. "
            "'sync_in', 'vref', 'thermal_pad')."
        ),
    )
    net_label: str | None = Field(
        default=None,
        description=(
            "Label of the net this pin connects to, if labelled on the page. Null when "
            "the wire is unlabelled (local short); the merger will fall back to a "
            "page-local synthetic id."
        ),
    )


class PageNode(BaseModel):
    model_config = ConfigDict(extra="ignore")

    refdes: str = Field(
        description="Reference designator as printed (e.g. 'U7', 'C29', 'R42', 'J15')."
    )
    type: ComponentType = Field(description="Component family.")
    value: ComponentValue | None = Field(
        default=None,
        description="Component value(s) if any text is printed near the symbol.",
    )
    page: int = Field(description="1-based page number this component lives on.")
    pins: list[PagePin] = Field(
        default_factory=list,
        description="Pins of this component as visible on this page.",
    )
    populated: bool = Field(
        default=True,
        description=(
            "False if marked DNP / NOSTUFF / DNI on the page. Populated components "
            "default to True."
        ),
    )


class PageNet(BaseModel):
    model_config = ConfigDict(extra="ignore")

    local_id: str = Field(
        description=(
            "Identifier unique within this page (e.g. 'net_0001'). Used to stitch "
            "pins together when the net has no global label."
        )
    )
    label: str | None = Field(
        default=None,
        description=(
            "Net label if one is printed on the wire ('+3V3', 'IMX_JTAG_TCK'). Null "
            "for unlabelled wires — those stay local to the page by design."
        ),
    )
    is_power: bool = Field(
        default=False,
        description="True for VCC/VDD/GND/+xVy-style rails detected by symbol or name.",
    )
    is_global: bool = Field(
        default=False,
        description=(
            "True if the net must fuse globally across every page (GND, primary "
            "power rails). Nets with `is_global=True` bypass hierarchical scoping."
        ),
    )
    connects: list[str] = Field(
        default_factory=list,
        description="Pin references attached to this net on this page ('U7.1', 'C29.2').",
    )
    page: int


class CrossPageRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str | None = Field(
        default=None,
        description=(
            "Text read next to the off-page connector symbol. The merger stitches by "
            "this label. Null only when the connector carries no legible label."
        ),
    )
    direction: CrossPageDirection = Field(
        description=(
            "'in' / 'out' for off-page arrows, 'bidir' for named nets, 'subsheet' for "
            "KiCad-style hierarchical sheet references."
        )
    )
    at_pin: str | None = Field(
        default=None,
        description="Pin the reference is rooted on, if any ('U7.22'). Null otherwise.",
    )
    target_hint: str | None = Field(
        default=None,
        description=(
            "Free-form location hint ('page 5, zone B3', 'reform2-power.kicad_sch'). "
            "Used as a secondary stitch key when `label` is ambiguous."
        ),
    )
    page: int


class TypedEdge(BaseModel):
    model_config = ConfigDict(extra="ignore")

    src: str = Field(description="Refdes or net label the edge originates from.")
    dst: str = Field(description="Refdes or net label the edge points to.")
    kind: EdgeKind = Field(
        description=(
            "Semantic relationship type. Preferred canonical values: powers · "
            "powered_by · enables · resets · clocks · produces_signal · "
            "consumes_signal · decouples · filters · depends_on. Free-form "
            "values are accepted but will be ignored by compiler logic."
        )
    )
    page: int | None = Field(
        default=None,
        description="Page the edge was inferred on. Null for edges derived globally at merge time.",
    )


class DesignerNote(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str = Field(description="Exact text of the designer note as printed.")
    page: int
    attached_to_refdes: str | None = Field(
        default=None,
        description="Refdes the note visually refers to, if any.",
    )
    attached_to_net: str | None = Field(
        default=None,
        description="Net label the note visually refers to, if any.",
    )


class Ambiguity(BaseModel):
    model_config = ConfigDict(extra="ignore")

    description: str = Field(
        description="One sentence describing what could not be determined on this page."
    )
    page: int
    related_refdes: list[str] = Field(default_factory=list)
    related_nets: list[str] = Field(default_factory=list)


# ======================================================================
# Level 1 — per-page output (Claude vision target)
# ======================================================================


class SchematicPageGraph(BaseModel):
    """Structured output of a single-page Claude vision pass.

    Each field is designed to be producible from a single rendered page image
    without cross-page context. Net stitching, refdes deduplication, and boot
    sequence derivation all happen downstream in the merger.
    """

    model_config = ConfigDict(extra="ignore")

    schema_version: Literal["1.0"] = "1.0"
    page: int = Field(description="1-based page number within the source PDF.")
    sheet_name: str | None = Field(
        default=None,
        description="Title-block sheet name if printed ('Reform 2 Regulators').",
    )
    sheet_path: str | None = Field(
        default=None,
        description=(
            "Hierarchical sheet path if printed ('/Reform 2 Power/Reform 2 Regulators/'). "
            "Used to rebuild the sheet tree in the merged graph."
        ),
    )
    page_kind: PageKind = Field(
        default="schematic",
        description=(
            "Category of page content. Non-schematic kinds ('notes', 'block_diagram', "
            "'layout') must not produce topology — only `designer_notes`."
        ),
    )
    orientation: Literal["portrait", "landscape"] = "portrait"
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description=(
            "Self-reported confidence in the completeness of this page extraction. "
            "Lower when pins are unreadable, nets are unlabelled, or many ambiguities."
        ),
    )
    nodes: list[PageNode] = Field(default_factory=list)
    nets: list[PageNet] = Field(default_factory=list)
    cross_page_refs: list[CrossPageRef] = Field(default_factory=list)
    typed_edges: list[TypedEdge] = Field(
        default_factory=list,
        description=(
            "Edges inferred at page scale: 'U7 powers +5V', '5V_PWR_EN enables U7', "
            "'C16 decouples 30V_GATE'. Derived from pin names and symbol layout."
        ),
    )
    designer_notes: list[DesignerNote] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)


# ======================================================================
# Level 2 — merged flat catalogue (merger output)
# ======================================================================


class ComponentNode(BaseModel):
    """A component unified across pages (same refdes = same node)."""

    model_config = ConfigDict(extra="forbid")

    refdes: str
    type: ComponentType
    kind: ComponentKind = "ic"          # Phase 4 addition — defaults to "ic" so
                                         # every Phase 1 electrical_graph.json reloads
                                         # untouched.
    role: str | None = None              # Phase 4 addition — passive role per
                                         # spec 2026-04-24 (§Data shapes). Free-form
                                         # string, canonical values non-enforced.
    value: ComponentValue | None = None
    pages: list[int] = Field(default_factory=list)
    pins: list[PagePin] = Field(default_factory=list)
    populated: bool = True


class NetNode(BaseModel):
    """A net unified across pages (same label = same node)."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(
        description=(
            "Global label. For page-local unlabelled wires, the merger synthesises "
            "'__local__{page}__{local_id}' so every pin ends up on exactly one net."
        )
    )
    is_power: bool = False
    is_global: bool = False
    pages: list[int] = Field(default_factory=list)
    connects: list[str] = Field(default_factory=list)


class SchematicGraph(BaseModel):
    """Flat catalogue: every component and net fused across pages."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    device_slug: str
    source_pdf: str = Field(description="Path of the source PDF, relative to repo root.")
    page_count: int
    hierarchy: list[str] = Field(
        default_factory=list,
        description="Sheet paths encountered, ordered by first appearance.",
    )
    components: dict[str, ComponentNode] = Field(
        default_factory=dict,
        description="Keyed by refdes — duplicate refdes across pages are merged.",
    )
    nets: dict[str, NetNode] = Field(
        default_factory=dict,
        description="Keyed by label (or synthetic '__local__…' id for unlabelled wires).",
    )
    typed_edges: list[TypedEdge] = Field(default_factory=list)
    designer_notes: list[DesignerNote] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)


# ======================================================================
# Level 3 — final electrical graph (boot sequence derived)
# ======================================================================


class PowerRail(BaseModel):
    """A net elevated to a power-rail role with its producer and consumers."""

    model_config = ConfigDict(extra="forbid")

    label: str
    voltage_nominal: float | None = Field(
        default=None,
        description="Nominal voltage in V, inferred from label or source IC. Null when unknown.",
    )
    source_refdes: str | None = Field(
        default=None,
        description="Refdes of the component producing this rail (buck / LDO / connector).",
    )
    source_type: str | None = Field(
        default=None,
        description="Free-form producer kind ('buck', 'ldo', 'battery', 'external').",
    )
    enable_net: str | None = Field(
        default=None,
        description="Net that gates the producer's EN pin, when applicable.",
    )
    consumers: list[str] = Field(
        default_factory=list,
        description="Refdes list of components powered by this rail.",
    )
    decoupling: list[str] = Field(
        default_factory=list,
        description="Refdes list of decoupling caps near consumers of this rail.",
    )


class BootPhase(BaseModel):
    """A phase of the power-on sequence, derived by topological sort."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=1, description="1-based phase ordering in the boot sequence.")
    name: str = Field(description="Short label ('PHASE 2 — LPC sequences main rails').")
    rails_stable: list[str] = Field(
        default_factory=list,
        description="Rails that are up and stable by the end of this phase.",
    )
    components_entering: list[str] = Field(
        default_factory=list,
        description="Refdes of components that become active during this phase.",
    )
    triggers_next: list[str] = Field(
        default_factory=list,
        description="Signals asserted at the end of this phase that start the next.",
    )


class SchematicQualityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_pages: int
    pages_parsed: int
    orphan_cross_page_refs: int = 0
    nets_unresolved: int = 0
    components_without_value: int = 0
    components_without_mpn: int = 0
    confidence_global: float = Field(default=1.0, ge=0.0, le=1.0)
    degraded_mode: bool = Field(
        default=False,
        description=(
            "True if `confidence_global` < 0.7 or orphan cross-page refs exceed a "
            "threshold. Callers (the diagnostic agent) should prefix answers with a "
            "disclaimer when this is set."
        ),
    )


class ElectricalGraph(BaseModel):
    """Final artefact — interrogeable by the diagnostic agent via `mb_schematic_graph`."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    device_slug: str
    components: dict[str, ComponentNode] = Field(default_factory=dict)
    nets: dict[str, NetNode] = Field(default_factory=dict)
    power_rails: dict[str, PowerRail] = Field(default_factory=dict)
    typed_edges: list[TypedEdge] = Field(default_factory=list)
    boot_sequence: list[BootPhase] = Field(default_factory=list)
    designer_notes: list[DesignerNote] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    quality: SchematicQualityReport
    hierarchy: list[str] = Field(default_factory=list)


# ======================================================================
# Analyzed boot sequence — Opus post-pass that refines the compiler's
# topological boot_sequence using designer_notes + enable edges.
# ======================================================================


class AnalyzedBootTrigger(BaseModel):
    model_config = ConfigDict(extra="ignore")

    net_label: str = Field(
        description="Signal name that transitions at the end of the current phase "
        "to unlock the next ('5V_PWR_EN' asserts, 'PG_3V3' goes high)."
    )
    from_refdes: str | None = Field(
        default=None,
        description="Refdes of the component driving this trigger (LPC, PMIC). "
        "Null when the trigger comes from a passive (resistor divider, RC).",
    )
    rationale: str = Field(
        description="One sentence explaining why this specific signal gates the next "
        "phase. Must cite the evidence (designer note, enable edge).",
    )


class AnalyzedBootPhase(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int = Field(
        ge=0,
        description="Phase index. 0 = always-on / standby. 1+ = sequenced.",
    )
    name: str = Field(
        description="Short descriptive label ('Always-on standby', 'LPC asserts main rails')."
    )
    kind: str = Field(
        description="Class of phase: 'always-on' (lives whenever power is physically "
        "present) · 'sequenced' (gated by enable signal from the sequencer) · "
        "'on-demand' (user / OS triggers the phase asynchronously — USB plug, PCIe boot).",
    )
    rails_stable: list[str] = Field(
        default_factory=list,
        description="Rails that are up and stable at the end of this phase.",
    )
    components_entering: list[str] = Field(
        default_factory=list,
        description="Refdes of components that become active during this phase.",
    )
    triggers_next: list[AnalyzedBootTrigger] = Field(
        default_factory=list,
        description="Signals asserted at the end of this phase that start the next. "
        "Empty for the last phase.",
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="Short quoted excerpts from designer notes and/or explicit enable "
        "edges that justify this phase's placement. Cite specifically: 'designer "
        "note p3 U7: \"Main system power converters, enabled by LPC\"' or "
        "'edge: 5V_PWR_EN enables U7'.",
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Self-reported confidence for this phase's placement.",
    )


class AnalyzedBootSequence(BaseModel):
    """Opus-refined boot sequence — real ordering, not just topological.

    Produced by `api.pipeline.schematic.boot_analyzer`. Persisted beside the
    electrical_graph as `boot_sequence_analyzed.json`. When present, the
    frontend and `mb_schematic_graph` prefer it over the compiler's
    topological-only `boot_sequence`.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    device_slug: str
    phases: list[AnalyzedBootPhase]
    sequencer_refdes: str | None = Field(
        default=None,
        description="Refdes of the component orchestrating the sequence — usually an "
        "MCU (LPC, EC) or PMIC. Null when the board uses only passive sequencing "
        "(RC delays, cascaded PG).",
    )
    global_confidence: float = Field(
        default=1.0, ge=0.0, le=1.0,
        description="Overall confidence in the entire sequence reconstruction.",
    )
    ambiguities: list[str] = Field(
        default_factory=list,
        description="Sentences describing what couldn't be resolved (e.g. 'Q3 "
        "inrush-limiter timing relative to U2 charger start is unclear').",
    )
    model_used: str = Field(
        description="Anthropic model id that produced this analysis.",
    )


# ======================================================================
# Classified nets — Opus post-pass that tags every net with a functional
# domain (hdmi, usb, power_seq, …) + a one-sentence description so the
# agent / UI can answer "give me the HDMI-related components" in one call.
# ======================================================================


class ClassifiedNet(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str = Field(description="Net label as it appears in the schematic.")
    domain: str = Field(
        description=(
            "Functional domain this net belongs to. Preferred canonical values "
            "(non-enforced): hdmi · usb · pcie · ethernet · audio · display · "
            "storage · debug · power_seq · power_rail · clock · reset · "
            "control (I2C/SPI/UART control) · ground · misc. Use 'misc' "
            "sparingly — prefer a specific domain when any hint exists."
        ),
    )
    description: str = Field(
        default="",
        description=(
            "ONE SHORT SENTENCE describing what this net carries and its role. "
            "Example: 'HDMI Hot Plug Detect — logic high when a monitor is "
            "connected'. Keep under 140 chars."
        ),
    )
    voltage_level: str | None = Field(
        default=None,
        description=(
            "Expected electrical characteristic when healthy: '3V3 logic', "
            "'differential ±500mV', 'rail 5V', 'open-drain pull-up'. Null if "
            "unclear."
        ),
    )
    confidence: float = Field(
        default=1.0, ge=0.0, le=1.0,
        description="Confidence in the domain assignment, 0..1.",
    )


class NetClassification(BaseModel):
    """Opus-produced classification of every compiled net on the board.

    Persisted alongside the electrical graph as `nets_classified.json`.
    Consumed by `mb_schematic_graph(query='net_domain')` and the UI filter
    so the tech can type 'hdmi' and see the relevant components light up.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    device_slug: str
    nets: dict[str, ClassifiedNet] = Field(
        default_factory=dict,
        description="Map of net label → classification. One entry per net.",
    )
    domain_summary: dict[str, int] = Field(
        default_factory=dict,
        description="Per-domain count of classified nets, useful for stats.",
    )
    ambiguities: list[str] = Field(
        default_factory=list,
        description="Sentences describing nets whose domain couldn't be decided.",
    )
    model_used: str = Field(
        description="Anthropic model id that produced this classification.",
    )
