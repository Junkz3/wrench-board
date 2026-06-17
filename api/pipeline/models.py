"""Pydantic DTOs for the pipeline HTTP/WS surface — request/response shapes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from api.pipeline.schemas import _DeviceKind
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
    # Built at the end of schematic ingestion — flags whether `stock_search`
    # can match this device's components against the donor inventory.
    has_parts_index: bool
    # Build-state marker (api/pipeline/build_state.py): "building" | "complete" |
    # "failed" | "paused", or None for a legacy pack with no marker (completeness
    # is trusted on file presence alone). Lets the hosted front-door / UI show a
    # per-repair "analyse en cours / prête" badge without re-deriving completeness.
    build_state: str | None = None


# --- Taxonomy ---------------------------------------------------------------

class TaxonomyPackEntry(BaseModel):
    device_slug: str
    device_label: str
    version: str | None
    form_factor: str | None
    complete: bool
    # Readiness signals for the new-repair search: graph ready (schematic
    # already ingested) and the resolved device class. Let the technician see
    # that an existing device needs no schematic upload.
    has_electrical_graph: bool = False
    # Parts index present = the device's components are searchable/harvestable.
    # This is the signal the Stock "add donor" selector uses to mark a device
    # "ready" vs "waiting for graph" (a pack can exist with no parts_index yet).
    has_parts_index: bool = False
    device_kind: str | None = None
    # T9a: every carnet alias of this device (board# / Apple model / EMC /
    # codename / marketing) so the new-repair autocomplete matches any of them,
    # not just the label. Empty when the device has no registry fiche yet.
    aliases: list[str] = Field(default_factory=list)


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
    device_kind: _DeviceKind | None = Field(
        default=None,
        description="Technician-declared device class (prior). Validated/overridden "
        "by the graph classifier. One of the device_kind enum values or null.",
    )
    force_rebuild: bool = Field(
        default=False,
        description=(
            "When true, run the pipeline even if the pack is already complete on "
            "disk. The existing files get overwritten as each phase writes out. "
            "Use sparingly — a rebuild costs tokens."
        ),
    )
    owner_ref: str | None = Field(
        default=None,
        description=(
            "Opaque owner reference supplied by a multi-tenant front-door (the "
            "wrench-board-cloud passes the tenant id). When set, repair-reuse "
            "dedup is scoped to the same owner_ref so two owners diagnosing the "
            "same (device, symptom) get SEPARATE repairs — their private "
            "conversations/measurements never collide. The engine treats it as an "
            "opaque tag, NOT a security boundary (the front-door is the gatekeeper); "
            "unset for standalone/self-host, where all repairs share one owner."
        ),
    )
    allow_expand: bool = Field(
        default=True,
        description=(
            "Capability flag from a multi-tenant front-door: when False, a "
            "complete-pack + uncovered-symptom request must NOT fire the targeted "
            "expand round (it is LLM spend) — the engine answers expand_blocked=True, "
            "removes the just-persisted ticket, and the front-door maps that to its "
            "paywall. Plan policy lives in the front-door; the engine just honors "
            "the flag (like owner_ref, this is NOT a security boundary). Default "
            "True = standalone/self-host behaviour unchanged."
        ),
    )


class DisambiguationCandidate(BaseModel):
    """One candidate board when a free-text device label is ambiguous (T9a): the
    term fans out to several same-family siblings and the tech must pick one."""

    device_slug: str
    family: str | None = None
    facets: dict[str, list[str]] = Field(default_factory=dict)


class ResolveDeviceRequest(BaseModel):
    """Resolve a free device label to a canonical identity without creating a
    repair or starting a build (T9a). The cloud calls this BEFORE its quota gate
    so it can adopt the canonical slug and surface ambiguity for free."""

    device_label: str = Field(min_length=1, max_length=200)
    device_slug: str | None = Field(
        default=None, description="When pinned, returned verbatim — no resolution."
    )


class ResolveDeviceResponse(BaseModel):
    canonical_slug: str
    ambiguous: bool = False
    candidates: list[DisambiguationCandidate] = Field(default_factory=list)


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
    queued: bool = Field(
        default=False,
        description=(
            "True when the build was accepted but is WAITING behind the concurrent-"
            "build cap rather than running immediately. The UI shows a waiting state; "
            "the build starts (and the normal pipeline events flow) once a slot frees."
        ),
    )
    queue_position: int | None = Field(
        default=None,
        description="1-based position in the build queue when `queued` is true (1 = next to run); null otherwise.",
    )
    expand_blocked: bool = Field(
        default=False,
        description=(
            "True when the pack is complete, the symptom is uncovered, and the "
            "caller sent allow_expand=false: no expand was launched and the ticket "
            "was NOT kept (repair_id is empty) so a later allowed retry re-enters "
            "the normal flow instead of dedup-reusing a dead ticket. The front-door "
            "maps this to its paywall."
        ),
    )
    needs_disambiguation: bool = Field(
        default=False,
        description=(
            "True when the free-text device label was ambiguous (matched several "
            "same-family boards) and no device_slug was pinned: NO repair was "
            "created and NO pipeline started. The UI shows `candidates` so the tech "
            "picks one, then re-submits with the chosen device_slug."
        ),
    )
    candidates: list[DisambiguationCandidate] = Field(
        default_factory=list,
        description="The candidate boards to choose from when needs_disambiguation is true.",
    )


class RepairSummary(BaseModel):
    repair_id: str
    device_slug: str
    device_label: str
    symptom: str
    status: str
    created_at: str
    board_number: str | None = Field(
        default=None,
        description="Board revision number (e.g. 820-02016) when the repair was created with one.",
    )
    build_state: str | None = Field(
        default=None,
        description=(
            "Knowledge-pack build state of this repair's device, mirrored from "
            "the pack's `_build_state.json` marker: 'building' | 'complete' | "
            "'failed' | 'paused'. None when no marker exists (legacy / self-host "
            "packs built before the marker) — treated as ready. Drives the "
            "home-library tile badge and the live-timeline resume on landing load."
        ),
    )


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


class DeleteSourceResponse(BaseModel):
    device_slug: str
    kind: str
    deleted_filename: str
    # The pin after the delete — None when no versions remain for this kind.
    new_active: str | None
    # `deleted` = non-active version dropped, pin unchanged.
    # `switched_cached` = active version dropped, new pin restored from cache.
    # `switched_rebuilding` = active version dropped, new pin queued for re-ingest.
    # `cleared` = active version dropped and no versions remain.
    status: Literal["deleted", "switched_cached", "switched_rebuilding", "cleared"]
    detail: str
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
