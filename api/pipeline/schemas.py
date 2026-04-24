# SPDX-License-Identifier: Apache-2.0
"""Pydantic V2 schemas for the knowledge generation pipeline.

Every structured output of Phases 2–4 is declared here. These classes double as:
- Runtime validators for tool outputs (via `Class.model_validate(...)`)
- JSON Schema sources for the forced-tool definitions (via `Class.model_json_schema()`)
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ======================================================================
# PHASE 2.5 — Device taxonomy (brand > model > version hierarchy)
# ======================================================================


class DeviceTaxonomy(BaseModel):
    """Hierarchical classification extracted from the raw dump by the taxonomist.

    Every field is nullable — the extractor MUST output null rather than
    invent when a source doesn't state the fact (hard rule #5). Populated
    after the Registry Builder so the writers see the final taxonomy, and
    used by the UI to group devices by brand > model > version.
    """

    model_config = ConfigDict(extra="forbid")

    brand: str | None = Field(
        default=None,
        description=(
            "Manufacturer name as spelled in the sources — 'Apple', 'MNT', "
            "'Raspberry Pi', 'Samsung'. Null when the sources don't name one."
        ),
    )
    model: str | None = Field(
        default=None,
        description=(
            "Product line / model name — 'iPhone X', 'Reform', 'Model B', "
            "'Galaxy S21'. Null when genuinely unspecified."
        ),
    )
    version: str | None = Field(
        default=None,
        description=(
            "Free-form revision or variant: a model-id (A1901), a PCB rev "
            "(Rev 2.0), a generation (Gen 11), or a year (2021). Null otherwise."
        ),
    )
    form_factor: str | None = Field(
        default=None,
        description=(
            "The physical board being worked on — 'motherboard', 'logic board', "
            "'mainboard', 'daughterboard', 'charging board'. Use the term the "
            "community uses most often in the dump."
        ),
    )


# ======================================================================
# PHASE 2 — Registry (the canonical glossary)
# ======================================================================


class RefdesCandidate(BaseModel):
    """A graph refdes proposed as a match for a registry canonical_name.

    Emitted only when the Registry Builder is given an `ElectricalGraph`
    at phase-2 time (technician supplied a schematic). Each candidate
    must justify its mapping in `evidence` — either by quoting a source
    that ties the canonical to the refdes (via MPN / datasheet) or by
    citing an inference from a technician-supplied BOM. Never fabricate.
    """

    model_config = ConfigDict(extra="forbid")

    refdes: str = Field(
        description="Refdes from the supplied ElectricalGraph (e.g. U7, C29, J1)."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Subjective confidence in this canonical→refdes mapping.",
    )
    evidence: str = Field(
        description=(
            "One sentence justifying the mapping. Either a paraphrased quote "
            "from the dump (with URL when available) or 'inference from BOM "
            "MPN match' / 'inference from schematic MPN match'. Never empty."
        ),
        min_length=4,
    )


class RegistryComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_name: str = Field(
        description=(
            "The primary identifier. Use the exact refdes when it appears in the sources "
            "(e.g. U7, C29). Otherwise use a logical alias (e.g. 'main PMIC')."
        )
    )
    logical_alias: str | None = Field(
        default=None,
        description=(
            "A human-readable logical name, used when canonical_name is a cryptic refdes. "
            "Null if canonical_name is already human-readable."
        ),
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="Other names by which this component is known in the sources.",
    )
    kind: Literal[
        "ic",
        "pmic",
        "capacitor",
        "resistor",
        "inductor",
        "connector",
        "fuse",
        "switch",
        "crystal",
        "coil",
        "unknown",
    ] = "unknown"
    description: str = Field(
        default="",
        description="One sentence describing the role of the component.",
    )
    refdes_candidates: list[RefdesCandidate] | None = Field(
        default=None,
        description=(
            "Graph refdes candidates that match this canonical_name, emitted "
            "only when an ElectricalGraph is supplied at registry time. Each "
            "candidate carries its own evidence. Null on legacy packs and on "
            "any pipeline run where the technician did not supply a schematic."
        ),
    )


class RegistrySignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_name: str = Field(
        description="Canonical name of the signal/net/rail (e.g. 3V3_RAIL, VDD_CORE, USB_DP1)."
    )
    aliases: list[str] = Field(default_factory=list)
    kind: Literal["power_rail", "signal", "reference", "clock", "data_bus", "unknown"] = "unknown"
    nominal_voltage: float | None = Field(
        default=None,
        description="Nominal voltage in V if applicable (e.g. 3.3 for 3V3_RAIL). Null otherwise.",
    )


class Registry(BaseModel):
    """Phase 2 output — the canonical vocabulary all downstream writers must respect."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    device_label: str = Field(
        description="Human-readable device identifier (e.g. 'MNT Reform motherboard')."
    )
    taxonomy: DeviceTaxonomy = Field(
        default_factory=DeviceTaxonomy,
        description=(
            "Hierarchical classification (brand > model > version > form_factor). "
            "Fields are individually nullable — leave null when the sources don't "
            "state the fact rather than guessing (hard rule #5)."
        ),
    )
    components: list[RegistryComponent] = Field(default_factory=list)
    signals: list[RegistrySignal] = Field(default_factory=list)


# ======================================================================
# PHASE 2.5 — Refdes Mapper (canonical_name → graph refdes attribution)
# ======================================================================
#
# Runs only when an ElectricalGraph is loaded for the device. Output is
# server-side-validated against three deterministic rules before persist:
#   1. evidence_quote is a literal substring of the raw dump,
#   2. for literal_refdes_in_quote: refdes appears literally in evidence_quote,
#   3. for mpn_match_in_quote: graph.components[refdes].value.mpn appears
#      literally in evidence_quote (MPN comes only from the graph — the
#      LLM cannot invent it).
# Failed attributions are dropped, not retried. An empty mapping is a
# valid output. See docs/superpowers/specs/2026-04-25-refdes-mapper-agent.md.


EvidenceKind = Literal[
    "literal_refdes_in_quote",
    "mpn_match_in_quote",
]


class RefdesAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_name: str = Field(
        description=(
            "Must match a `canonical_name` of a component in the registry "
            "supplied to the Mapper. Otherwise the attribution is dropped."
        ),
    )
    refdes: str = Field(
        description=(
            "Must exist in `graph.components`. Otherwise dropped. The mapper "
            "MUST NOT invent a refdes that is not in the supplied graph."
        ),
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Subjective confidence in this attribution. Use ~0.95 for direct "
            "literal refdes mentions, ~0.85 for MPN matches, lower as evidence "
            "thins."
        ),
    )
    evidence_kind: EvidenceKind = Field(
        description=(
            "How the attribution is justified. Closed enum — the only two "
            "legitimate kinds are direct literal refdes mention OR MPN match. "
            "Topology / rail-overlap / functional similarity are NOT valid."
        ),
    )
    evidence_quote: str = Field(
        min_length=30,
        max_length=600,
        description=(
            "A literal substring of the raw research dump (≥30 chars) that "
            "supports the attribution. For `literal_refdes_in_quote` the "
            "refdes must appear in this quote (case-insensitive). For "
            "`mpn_match_in_quote` the graph's MPN for this refdes must appear "
            "in this quote (case-sensitive). Server validates both literally."
        ),
    )
    reasoning: str = Field(
        max_length=240,
        description=(
            "One sentence explaining why this canonical→refdes mapping holds. "
            "E.g. 'dump quote mentions the LM2677 buck; graph U7.value.mpn is "
            "LM2677SX-5'."
        ),
    )


class RefdesMappings(BaseModel):
    """Phase 2.5 output — typed canonical→refdes attributions, persisted as
    `memory/{slug}/refdes_attributions.json`."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    device_slug: str
    attributions: list[RefdesAttribution] = Field(default_factory=list)


# ======================================================================
# PHASE 3 — Writer outputs
# ======================================================================


# --- Writer 1 — Cartographe -----------------------------------------------------


class KnowledgeNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable identifier for this node (e.g. 'comp:U7', 'sym:3v3-dead').")
    kind: Literal["component", "symptom", "net"]
    label: str
    properties: dict[str, str] = Field(
        default_factory=dict,
        description="Free-form key/value properties. Values must be strings.",
    )


class KnowledgeEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    target_id: str
    relation: Literal["causes", "powers", "decouples", "connects", "measured_at", "part_of"]


class KnowledgeGraph(BaseModel):
    """Phase 3 Writer 1 (Cartographe) output — typed graph of the device domain."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    nodes: list[KnowledgeNode] = Field(default_factory=list)
    edges: list[KnowledgeEdge] = Field(default_factory=list)


# --- Writer 2 — Clinicien ------------------------------------------------------


class Cause(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refdes: str = Field(description="canonical_name from the registry. Must match exactly.")
    probability: float = Field(ge=0.0, le=1.0)
    mechanism: str = Field(
        description="Short phrase describing how this component fails (e.g. 'short-to-ground')."
    )


class DiagnosticStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(description="Concrete action, e.g. 'measure 3V3_RAIL at TP18'.")
    expected: str | None = Field(
        default=None,
        description="Expected value or range, e.g. '3.3V ± 5%'. Null if informational.",
    )


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable identifier, e.g. 'rule-reform-001'.")
    symptoms: list[str] = Field(min_length=1)
    likely_causes: list[Cause] = Field(min_length=1)
    diagnostic_steps: list[DiagnosticStep] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    sources: list[str] = Field(
        default_factory=list,
        description="URLs or citation markers supporting this rule.",
    )


class RulesSet(BaseModel):
    """Phase 3 Writer 2 (Clinicien) output — diagnostic decision tree."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    rules: list[Rule] = Field(default_factory=list)


# --- Writer 3 — Lexicographe ---------------------------------------------------


class ComponentSheet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_name: str = Field(description="Must match a canonical_name in the registry.")
    role: str | None = None
    package: str | None = None
    typical_failure_modes: list[str] = Field(default_factory=list)
    notes: str | None = None


class Dictionary(BaseModel):
    """Phase 3 Writer 3 (Lexicographe) output — per-component technical sheets."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    entries: list[ComponentSheet] = Field(default_factory=list)


# ======================================================================
# PHASE 4 — Audit verdict
# ======================================================================


FileName = Literal["knowledge_graph", "rules", "dictionary"]


class DriftItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: FileName
    mentions: list[str] = Field(
        description="The strings (refdes or names) that failed validation against the registry."
    )
    reason: str


class AuditVerdict(BaseModel):
    """Phase 4 output — structured QA result driving the self-healing loop."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    overall_status: Literal["APPROVED", "NEEDS_REVISION", "REJECTED"]
    consistency_score: float = Field(ge=0.0, le=1.0)
    files_to_rewrite: list[FileName] = Field(default_factory=list)
    drift_report: list[DriftItem] = Field(default_factory=list)
    revision_brief: str = Field(
        default="",
        description="Actionable description of what the Reviser must change. Empty when APPROVED.",
    )


# ======================================================================
# Orchestrator return type
# ======================================================================


class PipelineResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_slug: str
    disk_path: str
    verdict: AuditVerdict
    revise_rounds_used: int
    tokens_used_total: int
    cache_read_tokens_total: int
    cache_write_tokens_total: int
