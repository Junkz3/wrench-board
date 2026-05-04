"""Pydantic DTOs for the pipeline HTTP/WS surface — request/response shapes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from api.pipeline.schematic.simulator import Failure, RailOverride

# --- Generate ---------------------------------------------------------------

class GenerateRequest(BaseModel):
    device_label: str = Field(
        min_length=2,
        max_length=200,
        description="Human-readable device identifier (e.g. 'MNT Reform motherboard').",
    )


# --- Simulate ---------------------------------------------------------------

class SimulateRequest(BaseModel):
    killed_refdes: list[str] = Field(default_factory=list)
    failures: list[Failure] = Field(default_factory=list)
    rail_overrides: list[RailOverride] = Field(default_factory=list)


# --- Schematic ingest -------------------------------------------------------

class IngestSchematicRequest(BaseModel):
    device_slug: str = Field(
        min_length=1,
        max_length=120,
        description="Canonical slug of the device — no path separators, lowercase kebab-case.",
    )
    pdf_path: str = Field(
        min_length=1,
        description=(
            "Filesystem path to the schematic PDF. Absolute or relative to the "
            "server's working directory. Must exist and have a .pdf suffix."
        ),
    )
    device_label: str | None = Field(
        default=None,
        description="Optional human-readable label threaded into the vision prompt.",
    )


class IngestSchematicResponse(BaseModel):
    device_slug: str
    pdf_path: str
    started: bool


# --- Pack discovery ---------------------------------------------------------

class PackSummary(BaseModel):
    device_slug: str
    disk_path: str
    has_raw_dump: bool
    has_registry: bool
    has_knowledge_graph: bool
    has_rules: bool
    has_dictionary: bool
    has_audit_verdict: bool
    # Inputs the technician may have provided. Drive the data-aware
    # repair dashboard (cards explicitly switch between "imported" and
    # "to import" based on these flags).
    has_boardview: bool
    boardview_format: str | None
    has_schematic_pdf: bool
    has_electrical_graph: bool


# --- Taxonomy ---------------------------------------------------------------

class TaxonomyPackEntry(BaseModel):
    device_slug: str
    device_label: str
    version: str | None
    form_factor: str | None
    complete: bool


class TaxonomyTree(BaseModel):
    """Packs grouped by brand > model > version, with fallback bucket for
    registries missing brand or model (hard rule #4 = null rather than invent).
    """

    brands: dict[str, dict[str, list[TaxonomyPackEntry]]] = Field(default_factory=dict)
    uncategorized: list[TaxonomyPackEntry] = Field(default_factory=list)


# --- Repairs ----------------------------------------------------------------

class RepairRequest(BaseModel):
    device_label: str = Field(
        min_length=2,
        max_length=200,
        description="Human-readable device identifier (e.g. 'MNT Reform motherboard').",
    )
    device_slug: str | None = Field(
        default=None,
        description=(
            "Canonical slug of an existing pack on disk. When provided, the "
            "backend uses this directly instead of slugifying device_label — "
            "avoids the drift case where the Registry Builder rewrote the label "
            "after the pack's directory was already named from the initial slug."
        ),
    )
    symptom: str = Field(
        min_length=5,
        max_length=2000,
        description="Free-form description of what the client observes.",
    )
    force_rebuild: bool = Field(
        default=False,
        description=(
            "When true, run the pipeline even if the pack is already complete on "
            "disk. The existing files get overwritten as each phase writes out. "
            "Use sparingly — a rebuild costs tokens."
        ),
    )


class RepairResponse(BaseModel):
    repair_id: str
    device_slug: str
    device_label: str
    pipeline_started: bool = Field(
        description="True when a background pipeline run was kicked off — False when "
        "the pack is already complete on disk and no rebuild is needed."
    )
    pipeline_kind: Literal["full", "expand", "none"] = Field(
        default="none",
        description=(
            "What the backend decided to run given the pack state and the symptom: "
            "'full' = complete pipeline (Scout→Registry→Writers→Audit) with the "
            "symptom threaded to Scout as a priority target; 'expand' = targeted "
            "Scout+Clinicien round on the symptom only (pack already existed); "
            "'none' = no LLM work kicked off because the symptom is already "
            "covered by an existing rule."
        ),
    )
    matched_rule_id: str | None = Field(
        default=None,
        description=(
            "When the coverage classifier found an existing rule that already "
            "covers this symptom, its id is returned here so the UI can surface "
            "the known diagnostic flow immediately instead of waiting on an "
            "expand round-trip."
        ),
    )
    coverage_reason: str | None = Field(
        default=None,
        description=(
            "One-sentence explanation from the coverage classifier — set alongside "
            "`matched_rule_id` when the symptom is already covered, otherwise null."
        ),
    )


class RepairSummary(BaseModel):
    repair_id: str
    device_slug: str
    device_label: str
    symptom: str
    status: str
    created_at: str


# --- Pack expansion ---------------------------------------------------------

class ExpandRequest(BaseModel):
    focus_symptoms: list[str] = Field(
        min_length=1,
        description="Symptom phrases the tech is hunting — in any language, any casing.",
    )
    focus_refdes: list[str] = Field(
        default_factory=list,
        description="Optional refdes to probe specifically (e.g. U3101 for audio codec).",
    )


# --- Document upload --------------------------------------------------------

class DocumentUploadResponse(BaseModel):
    device_slug: str
    kind: str
    stored_path: str
    filename: str
    size_bytes: int


# --- Sources ----------------------------------------------------------------

class SourceVersion(BaseModel):
    filename: str
    timestamp: str
    original_name: str
    size_bytes: int
    is_active: bool


class SourceKindEntry(BaseModel):
    kind: str
    active: str | None
    versions: list[SourceVersion]


class SourcesResponse(BaseModel):
    device_slug: str
    schematic_pdf: SourceKindEntry
    boardview: SourceKindEntry


class SwitchSourceRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)


class SwitchSourceResponse(BaseModel):
    device_slug: str
    kind: str
    active: str
    status: Literal["pinned", "cached", "rebuilding"]
    detail: str
    # Populated only when status="rebuilding" — heuristic ETA so the UI
    # can show a countdown without polling for progress events.
    eta_seconds: int | None = None
    page_count: int | None = None


# --- Hypothesize ------------------------------------------------------------

class HypothesizeRequest(BaseModel):
    state_comps: dict[str, str] = Field(default_factory=dict)
    state_rails: dict[str, str] = Field(default_factory=dict)
    metrics_comps: dict[str, dict] = Field(default_factory=dict)
    metrics_rails: dict[str, dict] = Field(default_factory=dict)
    max_results: int = Field(default=5, ge=1, le=20)
    repair_id: str | None = None


# --- Measurements -----------------------------------------------------------

class MeasurementCreate(BaseModel):
    target: str
    value: float
    unit: str
    nominal: float | None = None
    note: str | None = None
