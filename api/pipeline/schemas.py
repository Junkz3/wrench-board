"""Pydantic V2 schemas for the knowledge generation pipeline.

Every structured output of Phases 2–4 is declared here. These classes double as:
- Runtime validators for tool outputs (via `Class.model_validate(...)`)
- JSON Schema sources for the forced-tool definitions (via `Class.model_json_schema()`)
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ======================================================================
# PHASE 2 — Registry (the canonical glossary)
# ======================================================================


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
    components: list[RegistryComponent] = Field(default_factory=list)
    signals: list[RegistrySignal] = Field(default_factory=list)


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
