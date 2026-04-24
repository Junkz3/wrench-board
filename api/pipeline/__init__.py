# SPDX-License-Identifier: Apache-2.0
"""Pipeline package — FastAPI router for the knowledge-generation factory.

Exposes:
    POST /pipeline/generate                       — run the full pipeline (blocking).
    POST /pipeline/repairs                        — create a repair + fire-and-forget pipeline.
    POST /pipeline/ingest-schematic               — fire-and-forget PDF → ElectricalGraph.
    WS   /pipeline/progress/{slug}                — stream live pipeline progress events.
    GET  /pipeline/packs                          — list packs on disk.
    GET  /pipeline/packs/{slug}                   — pack metadata.
    GET  /pipeline/packs/{slug}/full              — every JSON artefact in one payload.
    GET  /pipeline/packs/{slug}/schematic         — compiled electrical graph.
    GET  /pipeline/packs/{slug}/schematic/boot    — boot_sequence + power_rails subset.
    GET  /pipeline/packs/{slug}/schematic/passives — passive classifier output (kind, role, confidence).
    POST /pipeline/packs/{slug}/schematic/simulate — run behavioral simulator, returns full timeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from anthropic import APIConnectionError, APIError, APITimeoutError, AsyncAnthropic
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from api.agent.field_reports import list_field_reports
from api.config import get_settings
from api.pipeline import events
from api.pipeline.expansion import expand_pack
from api.pipeline.intent_classifier import IntentClassification, classify_intent
from api.pipeline.graph_transform import pack_to_graph_payload
from api.pipeline.orchestrator import _slugify, generate_knowledge_pack
from api.pipeline.schemas import PipelineResult
from api.pipeline.schematic.grounding import extract_grounding
from api.pipeline.schematic.net_classifier import classify_nets
from api.pipeline.schematic.orchestrator import ingest_schematic
from api.pipeline.schematic.renderer import render_pages
from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph
from api.pipeline.schematic.simulator import (
    Failure,
    RailOverride,
    SimulationEngine,
)
from api.tools.hypothesize import mb_hypothesize as _mb_hypothesize_tool
from api.tools.measurements import mb_list_measurements as _mb_list_measurements
from api.tools.measurements import mb_record_measurement as _mb_record_measurement

logger = logging.getLogger("microsolder.pipeline.api")

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


# Repair ids are generated via `uuid.uuid4().hex[:12]` → 12 hex chars. We
# keep the validator permissive enough to accept legacy / manually-seeded
# ids (short alphanumeric + `._-`) while rejecting anything that could
# escape the `memory/{slug}/repairs/{repair_id}/` subtree when used as a
# filesystem path segment.
_REPAIR_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _validate_repair_id(repair_id: str) -> str:
    """Return `repair_id` when safe to use as a path segment, else raise 400.

    Rejects empty, `..`, anything with `/` or `\\`, and anything outside
    the `[A-Za-z0-9._-]` alphabet. Path traversal defense in depth for
    the measurement HTTP routes that append `repair_id` into a filesystem
    path without further sanitisation.
    """
    if not repair_id or repair_id in {".", ".."} or not _REPAIR_ID_RE.match(repair_id):
        raise HTTPException(status_code=400, detail={"reason": "invalid_repair_id"})
    return repair_id


class GenerateRequest(BaseModel):
    device_label: str = Field(
        min_length=2,
        max_length=200,
        description="Human-readable device identifier (e.g. 'MNT Reform motherboard').",
    )


class SimulateRequest(BaseModel):
    killed_refdes: list[str] = Field(default_factory=list)
    failures: list[Failure] = Field(default_factory=list)
    rail_overrides: list[RailOverride] = Field(default_factory=list)


@router.post("/generate", response_model=PipelineResult)
async def generate(request: GenerateRequest) -> PipelineResult:
    """Run the full pipeline synchronously and return the result on completion.

    Expect this call to block for ~30–120 seconds depending on Scout web_search usage
    and whether the Auditor triggers revise rounds.
    """
    logger.info("[API] /pipeline/generate · device=%r", request.device_label)
    try:
        return await generate_knowledge_pack(request.device_label)
    except RuntimeError as exc:
        logger.exception("[API] Pipeline failed for device=%r", request.device_label)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ======================================================================
# Schematic ingestion — fire-and-forget PDF → ElectricalGraph
# ======================================================================


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


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


def _validate_slug(slug: str) -> str:
    """Reject inputs that aren't already canonical kebab-case slugs.

    The GET routes happily slugify user input, but the ingestion POST needs
    stricter guarantees: the slug becomes a directory name under memory_root
    and a non-canonical value like "../evil" or "bad..slug" must never reach
    disk. Consecutive dots are rejected even though the character class allows
    a single `.` — `..` is a path-traversal marker by any reasonable reading.
    """
    if not _SLUG_RE.fullmatch(slug) or ".." in slug:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid device_slug {slug!r} — must match "
                "^[a-z0-9][a-z0-9._-]*$ with no '..' sequences."
            ),
        )
    return slug


def _resolve_pdf_path(pdf_path: str) -> Path:
    """Resolve + validate a PDF path received over HTTP.

    Absolute paths are taken verbatim, relative paths are resolved against the
    server's current working directory (where uvicorn was launched — the
    `board_assets/` convention makes `board_assets/foo.pdf` the common shape).
    Existence and .pdf suffix are enforced before we fire any background task,
    so the caller never has to poll a task that was doomed from the start.
    """
    p = Path(pdf_path)
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    else:
        p = p.resolve()
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"PDF not found: {pdf_path}")
    if p.suffix.lower() != ".pdf":
        raise HTTPException(
            status_code=400,
            detail=f"pdf_path must be a .pdf file (got suffix {p.suffix!r}).",
        )
    return p


async def _run_schematic_in_background(
    device_slug: str, pdf_path: Path, device_label: str | None
) -> None:
    """Background task: instantiate a client and run the ingestion.

    Exceptions are logged and swallowed — the initial 202 has already been
    sent, so there is no HTTP response to fail. A future iteration can wire
    status onto the events bus the way the knowledge factory does.
    """
    t0 = time.monotonic()
    _s = get_settings()
    client = AsyncAnthropic(api_key=_s.anthropic_api_key, max_retries=_s.anthropic_max_retries)
    try:
        await ingest_schematic(
            device_slug=device_slug,
            pdf_path=pdf_path,
            client=client,
            device_label=device_label,
        )
        logger.info(
            "[API] schematic ingestion finished for slug=%r (%.1fs)",
            device_slug,
            time.monotonic() - t0,
        )
    except Exception:
        logger.exception("[API] schematic ingestion failed for slug=%r", device_slug)


@router.post(
    "/ingest-schematic",
    response_model=IngestSchematicResponse,
    status_code=202,
)
async def post_ingest_schematic(
    request: IngestSchematicRequest,
) -> IngestSchematicResponse:
    """Kick off a schematic ingestion in the background and return 202.

    Input validation is blocking (slug shape, PDF existence, .pdf suffix).
    Ingestion wall-time is ~5 minutes for a dozen pages, so the caller polls
    `GET /pipeline/packs/{slug}/schematic` until it returns 200.
    """
    slug = _validate_slug(request.device_slug)
    pdf_path = _resolve_pdf_path(request.pdf_path)
    logger.info("[API] /pipeline/ingest-schematic · slug=%r · pdf=%s", slug, pdf_path)
    asyncio.create_task(_run_schematic_in_background(slug, pdf_path, request.device_label))
    return IngestSchematicResponse(
        device_slug=slug,
        pdf_path=str(pdf_path),
        started=True,
    )


class PackSummary(BaseModel):
    device_slug: str
    disk_path: str
    has_raw_dump: bool
    has_registry: bool
    has_knowledge_graph: bool
    has_rules: bool
    has_dictionary: bool
    has_audit_verdict: bool


def _summarize_pack(pack_dir: Path) -> PackSummary:
    return PackSummary(
        device_slug=pack_dir.name,
        disk_path=str(pack_dir),
        has_raw_dump=(pack_dir / "raw_research_dump.md").exists(),
        has_registry=(pack_dir / "registry.json").exists(),
        has_knowledge_graph=(pack_dir / "knowledge_graph.json").exists(),
        has_rules=(pack_dir / "rules.json").exists(),
        has_dictionary=(pack_dir / "dictionary.json").exists(),
        has_audit_verdict=(pack_dir / "audit_verdict.json").exists(),
    )


@router.get("/packs", response_model=list[PackSummary])
async def list_packs() -> list[PackSummary]:
    settings = get_settings()
    root = Path(settings.memory_root)
    if not root.exists():
        return []
    return sorted(
        (_summarize_pack(d) for d in root.iterdir() if d.is_dir()),
        key=lambda s: s.device_slug,
    )


class TaxonomyPackEntry(BaseModel):
    device_slug: str
    device_label: str
    version: str | None
    form_factor: str | None
    complete: bool


class TaxonomyTree(BaseModel):
    """Packs grouped by brand > model > version, with fallback bucket for
    registries missing brand or model (hard rule #5 = null rather than invent).
    """

    brands: dict[str, dict[str, list[TaxonomyPackEntry]]] = Field(default_factory=dict)
    uncategorized: list[TaxonomyPackEntry] = Field(default_factory=list)


@router.get("/taxonomy", response_model=TaxonomyTree)
async def get_taxonomy() -> TaxonomyTree:
    """Scan every pack's registry.json and group by taxonomy.

    A pack lands in `brands[brand][model]` when both `taxonomy.brand` and
    `taxonomy.model` are present; otherwise it falls to `uncategorized`. The UI
    uses this to populate the 'New repair' modal's accordion by manufacturer
    and the home section headers.
    """
    settings = get_settings()
    root = Path(settings.memory_root)
    tree = TaxonomyTree()
    if not root.exists():
        return tree

    for pack_dir in sorted(root.iterdir(), key=lambda p: p.name):
        if not pack_dir.is_dir():
            continue
        registry = _read_optional_json(pack_dir / "registry.json")
        if registry is None:
            continue

        taxonomy = registry.get("taxonomy") or {}
        brand = taxonomy.get("brand")
        model = taxonomy.get("model")

        entry = TaxonomyPackEntry(
            device_slug=pack_dir.name,
            device_label=registry.get("device_label") or pack_dir.name,
            version=taxonomy.get("version"),
            form_factor=taxonomy.get("form_factor"),
            complete=_pack_is_complete(pack_dir),
        )

        if brand and model:
            tree.brands.setdefault(brand, {}).setdefault(model, []).append(entry)
        else:
            tree.uncategorized.append(entry)

    return tree


@router.get("/packs/{device_slug}", response_model=PackSummary)
async def get_pack(device_slug: str) -> PackSummary:
    settings = get_settings()
    root = Path(settings.memory_root)
    # Normalize: accept either a raw slug or a device_label.
    slug = _slugify(device_slug)
    pack_dir = root / slug
    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")
    return _summarize_pack(pack_dir)


def _read_optional_json(path: Path) -> dict | None:
    """Return the parsed JSON at path, or None if the file is absent.

    Raises HTTPException(422) if the file exists but is not valid JSON.
    """
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid JSON in {path.name}: {exc}",
        ) from exc


@router.get("/packs/{device_slug}/full")
async def get_pack_full(device_slug: str) -> dict:
    """Return every JSON artefact of a pack in a single payload.

    Missing files become `null` — never fabricated (hard rule #5). Consumed by
    the Memory Bank UI so it can render all five sections in one fetch.
    """
    settings = get_settings()
    slug = _slugify(device_slug)
    pack_dir = Path(settings.memory_root) / slug
    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")

    registry = _read_optional_json(pack_dir / "registry.json")
    knowledge_graph = _read_optional_json(pack_dir / "knowledge_graph.json")
    rules = _read_optional_json(pack_dir / "rules.json")
    dictionary = _read_optional_json(pack_dir / "dictionary.json")
    audit_verdict = _read_optional_json(pack_dir / "audit_verdict.json")

    device_label = (registry or {}).get("device_label") or slug

    return {
        "device_slug": slug,
        "device_label": device_label,
        "registry": registry,
        "knowledge_graph": knowledge_graph,
        "rules": rules,
        "dictionary": dictionary,
        "audit_verdict": audit_verdict,
    }


@router.get("/packs/{device_slug}/findings")
async def list_device_findings(device_slug: str, limit: int = 50) -> list[dict]:
    """Return every field report recorded for this device, newest first.

    Mirrors what `mb_list_findings` sees at agent-tool scope, exposed to the
    web UI so the Journal dashboard can render the cross-session memory
    without a WS round-trip. Strictly JSON-on-disk — no MA memory-store.
    """
    return list_field_reports(device_slug=_validate_slug(device_slug), limit=limit)


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


def _pack_is_complete(pack_dir: Path) -> bool:
    """A pack is 'complete' when the 4 writer files are present — audit is optional."""
    return all(
        (pack_dir / name).exists()
        for name in ("registry.json", "knowledge_graph.json", "rules.json", "dictionary.json")
    )


def _persist_repair(
    memory_root: Path,
    slug: str,
    device_label: str,
    symptom: str,
) -> str:
    """Write the repair metadata to memory/{slug}/repairs/{repair_id}.json.

    Returns the generated repair_id. The file is the durable record of one
    client intervention on this device — reopenable from the home library
    so the technician can come back to any past session. `status` starts at
    'open' and is updated as the session evolves.
    """
    repair_id = uuid.uuid4().hex[:12]
    repairs_dir = memory_root / slug / "repairs"
    repairs_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "repair_id": repair_id,
        "device_slug": slug,
        "device_label": device_label,
        "symptom": symptom,
        "status": "open",
        "created_at": datetime.now(UTC).isoformat(),
    }
    (repairs_dir / f"{repair_id}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return repair_id


class RepairSummary(BaseModel):
    repair_id: str
    device_slug: str
    device_label: str
    symptom: str
    status: str
    created_at: str


@router.get("/repairs", response_model=list[RepairSummary])
async def list_repairs() -> list[RepairSummary]:
    """Return every repair ever created, across every device, newest first.

    Powers the home library: each row is one client intervention the
    technician can open, reopen, or finish. Status drives the visual
    badge ('open' · 'in_progress' · 'closed').
    """
    settings = get_settings()
    root = Path(settings.memory_root)
    results: list[RepairSummary] = []
    if not root.exists():
        return results

    for pack_dir in root.iterdir():
        if not pack_dir.is_dir():
            continue
        repairs_dir = pack_dir / "repairs"
        if not repairs_dir.exists():
            continue
        for path in repairs_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text())
            except json.JSONDecodeError:
                logger.warning("Skipping malformed repair file: %s", path)
                continue
            results.append(
                RepairSummary(
                    repair_id=payload.get("repair_id", path.stem),
                    device_slug=payload.get("device_slug", pack_dir.name),
                    device_label=payload.get("device_label", pack_dir.name),
                    symptom=payload.get("symptom", ""),
                    status=payload.get("status", "open"),
                    created_at=payload.get("created_at", ""),
                )
            )
    results.sort(key=lambda r: r.created_at, reverse=True)
    return results


@router.get("/repairs/{repair_id}", response_model=RepairSummary)
async def get_repair(repair_id: str) -> RepairSummary:
    """Return one repair's metadata — used to resume a session from its id."""
    settings = get_settings()
    root = Path(settings.memory_root)
    if not root.exists():
        raise HTTPException(status_code=404, detail=f"No repair {repair_id!r}")
    for pack_dir in root.iterdir():
        if not pack_dir.is_dir():
            continue
        candidate = pack_dir / "repairs" / f"{repair_id}.json"
        if candidate.exists():
            payload = json.loads(candidate.read_text())
            return RepairSummary(
                repair_id=payload.get("repair_id", repair_id),
                device_slug=payload.get("device_slug", pack_dir.name),
                device_label=payload.get("device_label", pack_dir.name),
                symptom=payload.get("symptom", ""),
                status=payload.get("status", "open"),
                created_at=payload.get("created_at", ""),
            )
    raise HTTPException(status_code=404, detail=f"No repair {repair_id!r}")


@router.get("/repairs/{repair_id}/conversations")
def list_repair_conversations(repair_id: str) -> dict:
    """Return the conversation index for a repair.

    The repair's `device_slug` is inferred from the metadata file one level
    up in `memory/{slug}/repairs/{repair_id}.json` — clients don't pass it.
    """
    from api.agent.chat_history import list_conversations

    settings = get_settings()
    memory = Path(settings.memory_root)
    found_slug: str | None = None
    if memory.exists():
        for metadata_file in memory.glob(f"*/repairs/{repair_id}.json"):
            found_slug = metadata_file.parent.parent.name
            break
    if not found_slug:
        raise HTTPException(status_code=404, detail=f"unknown repair_id {repair_id}")
    convs = list_conversations(device_slug=found_slug, repair_id=repair_id)
    return {
        "device_slug": found_slug,
        "repair_id": repair_id,
        "conversations": convs,
    }


async def _run_pipeline_with_events(device_label: str, slug: str) -> None:
    """Background task: run the pipeline, relaying its events on the bus."""
    t0 = time.monotonic()
    try:
        await generate_knowledge_pack(
            device_label,
            on_event=lambda ev: events.publish(slug, ev),
        )
    except Exception as exc:
        logger.exception("[API] background pipeline failed for slug=%r", slug)
        await events.publish(
            slug,
            {
                "type": "pipeline_failed",
                "status": "ERROR",
                "error": str(exc),
                "elapsed_s": time.monotonic() - t0,
            },
        )


@router.post("/repairs", response_model=RepairResponse)
async def create_repair(request: RepairRequest) -> RepairResponse:
    """Register a repair and kick off the pipeline in the background.

    The response returns immediately with the generated repair_id and device_slug.
    Real-time pipeline progress is streamed via WS /pipeline/progress/{slug}.
    If the pack is already complete on disk we skip the pipeline to save tokens —
    the client can proceed straight to the Memory Bank.
    """
    settings = get_settings()
    memory_root = Path(settings.memory_root)
    # Prefer the explicit slug when the client picked an existing pack — this
    # protects us from Registry-rewrite drift (the LLM can amend device_label
    # after the pack's directory was named from the original call slug).
    slug = request.device_slug or _slugify(request.device_label)
    pack_dir = memory_root / slug

    # Every "nouvelle réparation" IS a repair session — persist the record
    # whether the pack is fresh or already on disk. Two repairs on the same
    # iPhone X are two separate sessions with two separate contexts; both
    # must be reopenable later from the library.
    repair_id = _persist_repair(memory_root, slug, request.device_label, request.symptom)

    if _pack_is_complete(pack_dir) and not request.force_rebuild:
        logger.info(
            "[API] /pipeline/repairs · pack already complete for slug=%r — repair=%s opens existing pack",
            slug,
            repair_id,
        )
        return RepairResponse(
            repair_id=repair_id,
            device_slug=slug,
            device_label=request.device_label,
            pipeline_started=False,
        )

    if request.force_rebuild and _pack_is_complete(pack_dir):
        logger.info(
            "[API] /pipeline/repairs · force_rebuild=True · repair=%s regenerating pack for slug=%r",
            repair_id,
            slug,
        )

    logger.info("[API] /pipeline/repairs · firing pipeline for slug=%r", slug)
    # Fire-and-forget. Errors land on the progress WS as pipeline_failed.
    asyncio.create_task(_run_pipeline_with_events(request.device_label, slug))
    return RepairResponse(
        repair_id=repair_id,
        device_slug=slug,
        device_label=request.device_label,
        pipeline_started=True,
    )


@router.websocket("/progress/{device_slug}")
async def progress_ws(websocket: WebSocket, device_slug: str) -> None:
    """Stream pipeline events for this slug until the client disconnects.

    Emits a `{type:"subscribed", device_slug}` ack as soon as the subscription
    is live, so the client knows it won't miss subsequent events. Terminal
    events (pipeline_finished / pipeline_failed) are still delivered normally;
    it's up to the client to close the socket when it's done consuming.
    """
    slug = _slugify(device_slug)
    await websocket.accept()
    queue = events.subscribe(slug)
    try:
        await websocket.send_text(json.dumps({"type": "subscribed", "device_slug": slug}))
        while True:
            event = await queue.get()
            await websocket.send_text(json.dumps(event))
    except WebSocketDisconnect:
        logger.info("[API] /pipeline/progress/%s · client disconnected", slug)
    finally:
        events.unsubscribe(slug, queue)


class ExpandRequest(BaseModel):
    focus_symptoms: list[str] = Field(
        min_length=1,
        description="Symptom phrases the tech is hunting — in any language, any casing.",
    )
    focus_refdes: list[str] = Field(
        default_factory=list,
        description="Optional refdes to probe specifically (e.g. U3101 for audio codec).",
    )


@router.post("/packs/{device_slug}/expand")
async def expand_device_pack(device_slug: str, request: ExpandRequest) -> dict:
    """Grow an existing pack's memory bank around a focus symptom area.

    Called by the diagnostic agent via the `mb_expand_knowledge` tool when
    the current ruleset comes up empty for a live symptom. Runs a targeted
    Scout + Registry + Clinicien mini-pipeline and merges the output into
    the existing pack. See api/pipeline/expansion.py for the mechanics.
    """
    slug = _slugify(device_slug)
    logger.info(
        "[API] /packs/%s/expand · focus=%s · refdes=%s",
        slug,
        request.focus_symptoms,
        request.focus_refdes,
    )
    try:
        return await expand_pack(
            device_slug=slug,
            focus_symptoms=request.focus_symptoms,
            focus_refdes=request.focus_refdes,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/packs/{device_slug}/graph")
async def get_pack_graph(device_slug: str) -> dict:
    """Return the combined graph payload ({nodes, edges}) consumed by web/index.html."""
    settings = get_settings()
    slug = _slugify(device_slug)
    pack_dir = Path(settings.memory_root) / slug
    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")

    try:
        registry = json.loads((pack_dir / "registry.json").read_text())
        knowledge_graph = json.loads((pack_dir / "knowledge_graph.json").read_text())
        rules = json.loads((pack_dir / "rules.json").read_text())
        dictionary = json.loads((pack_dir / "dictionary.json").read_text())
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Pack for {slug!r} is incomplete: {exc.filename}",
        ) from exc

    return pack_to_graph_payload(
        registry=registry,
        knowledge_graph=knowledge_graph,
        rules=rules,
        dictionary=dictionary,
    )


# ======================================================================
# Technician-supplied document uploads — feeds Scout / Registry enrichment
# ======================================================================


_UPLOAD_KINDS = {"schematic_pdf", "boardview", "datasheet", "notes", "other"}
# Defense in depth — clamp the upload size at a sane ceiling so a 1 GB
# blob doesn't fill /tmp during a multipart parse. 50 MB is enough for
# any schematic PDF or datasheet we've seen in the wild.
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def _safe_filename(name: str) -> str:
    """Return a path-segment-safe version of `name`.

    Strips directory components, control characters, and leading dots so
    nothing the technician uploads can escape `memory/{slug}/uploads/`.
    """
    base = Path(name).name  # drop any directory traversal
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-_.")
    return cleaned or "upload"


class DocumentUploadResponse(BaseModel):
    device_slug: str
    kind: str
    stored_path: str
    filename: str
    size_bytes: int


@router.post(
    "/packs/{device_slug}/documents",
    response_model=DocumentUploadResponse,
    status_code=201,
)
async def post_pack_document(
    device_slug: str,
    kind: str = Form(...),
    description: str | None = Form(default=None),
    file: UploadFile = File(...),
) -> DocumentUploadResponse:
    """Persist a technician-supplied document under `memory/{slug}/uploads/`.

    Triggers no processing — the orchestrator picks the file up on the
    next `POST /pipeline/generate` (or `/pipeline/repairs`) call. The
    `kind` decides how the file is consumed downstream:
    `schematic_pdf` triggers an inline `ingest_schematic` if the device
    has no `electrical_graph.json` yet; `boardview` is parsed into a
    `Board`; `datasheet` is listed for Scout to cite via `local://`;
    `notes` and `other` are stored but not fed into prompts.
    """
    slug = _validate_slug(device_slug)
    if kind not in _UPLOAD_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown kind={kind!r} — allowed: {sorted(_UPLOAD_KINDS)}",
        )

    settings = get_settings()
    uploads_dir = Path(settings.memory_root) / slug / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    filename = _safe_filename(file.filename or "upload")
    target = uploads_dir / f"{timestamp}-{kind}-{filename}"

    # Stream the upload to disk in chunks so we never hold the entire
    # blob in memory and we can abort cleanly on the size cap.
    total = 0
    try:
        with target.open("wb") as fh:
            while True:
                chunk = await file.read(1 << 20)  # 1 MiB
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_UPLOAD_BYTES:
                    fh.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"upload exceeds {_MAX_UPLOAD_BYTES // (1024 * 1024)} MiB cap",
                    )
                fh.write(chunk)
    except HTTPException:
        raise
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"could not persist upload: {exc}") from exc
    finally:
        await file.close()

    if description:
        # Best-effort breadcrumb — failures don't fail the upload.
        try:
            (uploads_dir / f"{target.name}.description.txt").write_text(
                description.strip(), encoding="utf-8"
            )
        except OSError:
            logger.warning(
                "could not persist description sidecar for %s",
                target,
                exc_info=True,
            )

    logger.info(
        "[API] /pipeline/packs/%s/documents · kind=%s file=%s bytes=%d",
        slug,
        kind,
        target.name,
        total,
    )
    return DocumentUploadResponse(
        device_slug=slug,
        kind=kind,
        stored_path=str(target),
        filename=filename,
        size_bytes=total,
    )


@router.get("/packs/{device_slug}/documents")
async def list_pack_documents(device_slug: str) -> dict:
    """List every upload persisted for this pack, grouped by kind."""
    slug = _validate_slug(device_slug)
    settings = get_settings()
    uploads_dir = Path(settings.memory_root) / slug / "uploads"
    if not uploads_dir.exists():
        return {"device_slug": slug, "uploads": []}

    items: list[dict] = []
    for path in sorted(uploads_dir.iterdir()):
        if not path.is_file() or path.name.endswith(".description.txt"):
            continue
        match = re.match(r"^(?P<ts>[^-]+(?:-[^-]+)*?)-(?P<kind>[a-z_]+)-(?P<filename>.+)$", path.name)
        if match is None:
            kind = "other"
            timestamp = ""
            original = path.name
        else:
            kind = match.group("kind")
            timestamp = match.group("ts")
            original = match.group("filename")
        sidecar = uploads_dir / f"{path.name}.description.txt"
        description = (
            sidecar.read_text(encoding="utf-8") if sidecar.exists() else None
        )
        items.append(
            {
                "name": path.name,
                "kind": kind,
                "timestamp": timestamp,
                "filename": original,
                "size_bytes": path.stat().st_size,
                "description": description,
            }
        )
    return {"device_slug": slug, "uploads": items}


@router.api_route("/packs/{device_slug}/schematic.pdf", methods=["GET", "HEAD"])
async def get_pack_schematic_pdf(device_slug: str) -> FileResponse:
    """Serve the source schematic PDF for this device.

    Lookup order:
    1. `memory/{slug}/schematic.pdf` — persisted by `ingest_schematic`.
    2. `board_assets/{slug}.pdf` — fallback for demo devices whose schematic
       was never re-ingested but ships in the repo.
    Returns 404 when neither exists. Served as `application/pdf` with
    `Content-Disposition: inline` so the browser's native viewer handles
    pagination, zoom, and search.
    """
    settings = get_settings()
    slug = _slugify(device_slug)
    candidates = [
        Path(settings.memory_root) / slug / "schematic.pdf",
        Path.cwd() / "board_assets" / f"{slug}.pdf",
    ]
    for path in candidates:
        if path.exists() and path.is_file():
            return FileResponse(
                path,
                media_type="application/pdf",
                headers={"Content-Disposition": f'inline; filename="{slug}.pdf"'},
            )
    raise HTTPException(
        status_code=404,
        detail=f"No schematic PDF on disk for device_slug={slug!r}",
    )


def _find_schematic_pdf(slug: str, memory_root: Path) -> Path | None:
    """Return the source PDF for a slug, or None if neither location has it."""
    for candidate in (
        memory_root / slug / "schematic.pdf",
        Path.cwd() / "board_assets" / f"{slug}.pdf",
    ):
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _list_page_pngs(pages_dir: Path) -> list[Path]:
    """Return every rendered page PNG sorted by page number, [] when missing."""
    if not pages_dir.exists():
        return []
    return sorted(
        pages_dir.glob("page-*.png"),
        key=lambda p: int(p.stem.rsplit("-", 1)[1]),
    )


def _render_and_extract_pages(pdf_path: Path, pages_dir: Path, dpi: int = 150) -> None:
    """Rasterise the PDF to PNGs + persist refdes anchors per page.

    Idempotent: safe to call on a pages_dir that already contains page JSONs
    — the PNGs are simply re-written. Extracts grounding per page to emit
    `page-NN.anchors.json` next to the PNG (same layout as the orchestrator).
    """
    pages_dir.mkdir(parents=True, exist_ok=True)
    rendered = render_pages(pdf_path, pages_dir, dpi=dpi)
    for rp in rendered:
        try:
            g = extract_grounding(pdf_path, rp.page_number)
        except Exception:
            logger.exception(
                "grounding failed on page %d of %s — skipping anchors",
                rp.page_number,
                pdf_path,
            )
            continue
        payload = {
            "page": g.page,
            "page_width_pt": g.page_width,
            "page_height_pt": g.page_height,
            "anchors": [
                {"refdes": rd, "x0": x0, "top": top, "x1": x1, "bottom": bot}
                for (rd, x0, top, x1, bot) in g.refdes_anchors
            ],
        }
        (pages_dir / f"page-{rp.page_number:02d}.anchors.json").write_text(
            json.dumps(payload, indent=2)
        )


async def _ensure_pages_rendered(slug: str, memory_root: Path) -> Path | None:
    """Lazy-render PNGs + anchors for a slug if they aren't on disk yet.

    Returns the pages directory on success, None when no source PDF can be
    found. The rasterisation is pushed to a thread so the event loop isn't
    blocked while `pdftoppm` runs (~1s/page at 150 DPI).
    """
    pages_dir = memory_root / slug / "schematic_pages"
    if _list_page_pngs(pages_dir):
        return pages_dir
    pdf_path = _find_schematic_pdf(slug, memory_root)
    if pdf_path is None:
        return None
    logger.info("[API] lazy-rendering schematic pages for slug=%s", slug)
    await asyncio.to_thread(_render_and_extract_pages, pdf_path, pages_dir)
    return pages_dir


@router.get("/packs/{device_slug}/schematic/pages")
async def get_pack_schematic_pages(device_slug: str) -> dict:
    """Return the page index for the in-app PDF viewer.

    Payload shape:
    ```
    {
      "device_slug": "<slug>",
      "count": <int>,
      "pages": [
        {
          "n": 1,
          "url": "/pipeline/packs/<slug>/schematic/pages/1.png",
          "width_pt":  <float>,
          "height_pt": <float>,
          "anchors":   [{"refdes": "U13", "x0":..,"top":..,"x1":..,"bottom":..}, ...]
        }, ...
      ]
    }
    ```
    PNGs are lazy-rendered on first call when the pack has never been
    ingested but a PDF source exists (either persisted or in board_assets).
    404 when no PDF can be found anywhere.
    """
    settings = get_settings()
    slug = _slugify(device_slug)
    memory_root = Path(settings.memory_root)
    pages_dir = await _ensure_pages_rendered(slug, memory_root)
    if pages_dir is None:
        raise HTTPException(
            status_code=404,
            detail=f"No schematic PDF on disk for device_slug={slug!r}",
        )
    pngs = _list_page_pngs(pages_dir)
    if not pngs:
        raise HTTPException(
            status_code=500,
            detail=f"Rendered no pages for device_slug={slug!r}",
        )
    pages: list[dict] = []
    for png in pngs:
        n = int(png.stem.rsplit("-", 1)[1])
        anchors_file = pages_dir / f"page-{n:02d}.anchors.json"
        anchors_payload = _read_optional_json(anchors_file) or {}
        pages.append(
            {
                "n": n,
                "url": f"/pipeline/packs/{slug}/schematic/pages/{n}.png",
                "width_pt": anchors_payload.get("page_width_pt", 0.0),
                "height_pt": anchors_payload.get("page_height_pt", 0.0),
                "anchors": anchors_payload.get("anchors", []),
            }
        )
    return {"device_slug": slug, "count": len(pages), "pages": pages}


@router.api_route(
    "/packs/{device_slug}/schematic/pages/{page_n}.png",
    methods=["GET", "HEAD"],
)
async def get_pack_schematic_page_png(device_slug: str, page_n: int) -> FileResponse:
    """Serve one rasterised page as PNG.

    `page_n` is the 1-based page number; filename on disk is zero-padded to
    match pdftoppm's output (`page-01.png`). Lazy-renders the full pack if
    the PNGs aren't on disk yet.
    """
    settings = get_settings()
    slug = _slugify(device_slug)
    memory_root = Path(settings.memory_root)
    pages_dir = await _ensure_pages_rendered(slug, memory_root)
    if pages_dir is None:
        raise HTTPException(
            status_code=404,
            detail=f"No schematic PDF on disk for device_slug={slug!r}",
        )
    # pdftoppm pads to max(2, len(str(page_count))) digits — scan rather than
    # guess, so we don't have to know the total page count here.
    candidates = [
        pages_dir / f"page-{page_n:02d}.png",
        pages_dir / f"page-{page_n:03d}.png",
        pages_dir / f"page-{page_n}.png",
    ]
    for path in candidates:
        if path.exists():
            return FileResponse(path, media_type="image/png")
    raise HTTPException(
        status_code=404,
        detail=f"Page {page_n} not found for device_slug={slug!r}",
    )


@router.get("/packs/{device_slug}/schematic")
async def get_pack_schematic(device_slug: str) -> dict:
    """Return the compiled electrical graph for this device.

    404 when either the pack directory or `electrical_graph.json` is missing.
    The payload matches `api.pipeline.schematic.schemas.ElectricalGraph` —
    consumed by the Memory Bank UI for the D3 rail / boot-phase view.

    When `boot_sequence_analyzed.json` exists (Opus post-pass), we merge it
    into the payload under key `analyzed_boot_sequence` and also surface a
    `boot_sequence_source` flag (`"analyzer"` or `"compiler"`) so the UI can
    badge the timeline appropriately.
    """
    settings = get_settings()
    slug = _slugify(device_slug)
    pack_dir = Path(settings.memory_root) / slug
    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")
    graph_path = pack_dir / "electrical_graph.json"
    if not graph_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No schematic ingested yet for device_slug={slug!r}",
        )
    try:
        payload = json.loads(graph_path.read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Malformed electrical_graph.json for {slug!r}: {exc}",
        ) from exc

    # Opt-in overlay: Opus-refined boot sequence lives in its own file so we
    # can re-run the analyzer without re-doing the vision pass.
    analyzed_path = pack_dir / "boot_sequence_analyzed.json"
    if analyzed_path.exists():
        try:
            payload["analyzed_boot_sequence"] = json.loads(analyzed_path.read_text())
            payload["boot_sequence_source"] = "analyzer"
        except json.JSONDecodeError:
            payload["boot_sequence_source"] = "compiler"
            logger.warning(
                "boot_sequence_analyzed.json malformed for %s, falling back to compiler",
                slug,
            )
    else:
        payload["boot_sequence_source"] = "compiler"

    # Same pattern for the net classifier — nets_classified.json when
    # present, fallback to an empty state (UI can still run the regex
    # classifier in-browser if needed).
    classified_path = pack_dir / "nets_classified.json"
    if classified_path.exists():
        try:
            classification = json.loads(classified_path.read_text())
            payload["net_classification"] = classification
            payload["net_domains_source"] = classification.get("model_used", "regex")
        except json.JSONDecodeError:
            payload["net_domains_source"] = "none"
            logger.warning(
                "nets_classified.json malformed for %s",
                slug,
            )
    else:
        payload["net_domains_source"] = "none"

    return payload


async def _run_boot_analyzer_in_background(device_slug: str, pack_dir: Path) -> None:
    """Background task — load the electrical graph, run Opus, persist analyzer output."""
    t0 = time.monotonic()
    graph_path = pack_dir / "electrical_graph.json"
    try:
        graph = ElectricalGraph.model_validate_json(graph_path.read_text())
    except Exception:
        logger.exception("[API] analyze-boot: failed to load electrical_graph for %s", device_slug)
        return
    _s = get_settings()
    client = AsyncAnthropic(api_key=_s.anthropic_api_key, max_retries=_s.anthropic_max_retries)
    try:
        from api.pipeline.schematic.boot_analyzer import analyze_boot_sequence  # lazy: module is optional WIP on evolve
        analyzed = await analyze_boot_sequence(graph, client=client)
        (pack_dir / "boot_sequence_analyzed.json").write_text(analyzed.model_dump_json(indent=2))
        logger.info(
            "[API] analyze-boot finished for %s in %.1fs (phases=%d conf=%.2f)",
            device_slug,
            time.monotonic() - t0,
            len(analyzed.phases),
            analyzed.global_confidence,
        )
    except Exception:
        logger.exception("[API] analyze-boot failed for %s", device_slug)


@router.post("/packs/{device_slug}/schematic/analyze-boot", status_code=202)
async def post_analyze_boot(device_slug: str) -> dict:
    """Kick off an Opus boot-sequence analysis in the background.

    Re-runnable independently of the full schematic ingestion — useful when
    the prompt is improved or a newer model is available. Returns 202 with
    `{device_slug, started}`; the client polls `GET /packs/{slug}/schematic`
    to observe `boot_sequence_source` flipping from `compiler` to `analyzer`.
    """
    settings = get_settings()
    slug = _slugify(device_slug)
    pack_dir = Path(settings.memory_root) / slug
    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")
    if not (pack_dir / "electrical_graph.json").exists():
        raise HTTPException(
            status_code=404,
            detail=f"No schematic ingested yet for device_slug={slug!r}",
        )
    logger.info("[API] /packs/%s/schematic/analyze-boot · queued", slug)
    asyncio.create_task(_run_boot_analyzer_in_background(slug, pack_dir))
    return {"device_slug": slug, "started": True}


async def _run_net_classifier_in_background(device_slug: str, pack_dir: Path) -> None:
    """Background task — run Opus net classifier and persist."""
    t0 = time.monotonic()
    graph_path = pack_dir / "electrical_graph.json"
    try:
        graph = ElectricalGraph.model_validate_json(graph_path.read_text())
    except Exception:
        logger.exception("[API] classify-nets: failed to load electrical_graph for %s", device_slug)
        return
    _s = get_settings()
    client = AsyncAnthropic(api_key=_s.anthropic_api_key, max_retries=_s.anthropic_max_retries)
    try:
        classification = await classify_nets(graph, client=client)
        (pack_dir / "nets_classified.json").write_text(classification.model_dump_json(indent=2))
        logger.info(
            "[API] classify-nets finished for %s in %.1fs (nets=%d model=%s)",
            device_slug,
            time.monotonic() - t0,
            len(classification.nets),
            classification.model_used,
        )
    except Exception:
        logger.exception("[API] classify-nets failed for %s", device_slug)


@router.post("/packs/{device_slug}/schematic/classify-nets", status_code=202)
async def post_classify_nets(device_slug: str) -> dict:
    """Kick off an Opus net classification in the background.

    Re-runnable independently — useful when the prompt improves or a new
    model drops. Returns 202; client polls `GET /packs/{slug}/schematic`
    and sees `net_domains_source` flip from 'regex' to 'opus'.
    """
    settings = get_settings()
    slug = _slugify(device_slug)
    pack_dir = Path(settings.memory_root) / slug
    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")
    if not (pack_dir / "electrical_graph.json").exists():
        raise HTTPException(
            status_code=404,
            detail=f"No schematic ingested yet for device_slug={slug!r}",
        )
    logger.info("[API] /packs/%s/schematic/classify-nets · queued", slug)
    asyncio.create_task(_run_net_classifier_in_background(slug, pack_dir))
    return {"device_slug": slug, "started": True}


@router.get("/packs/{device_slug}/schematic/boot")
async def get_pack_schematic_boot(device_slug: str) -> dict:
    """Return just the boot sequence + power rails — the "light" subset.

    The full `electrical_graph.json` can reach several hundred KB on real
    boards (449 components, ~2k pins on MNT Reform). For the initial boot
    timeline view the UI only needs rails and phases, so this route strips
    the heavy `components` / `nets` / `typed_edges` arrays server-side.
    """
    settings = get_settings()
    slug = _slugify(device_slug)
    graph_path = Path(settings.memory_root) / slug / "electrical_graph.json"
    if not graph_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No schematic ingested yet for device_slug={slug!r}",
        )
    try:
        graph = json.loads(graph_path.read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Malformed electrical_graph.json for {slug!r}: {exc}",
        ) from exc
    return {
        "device_slug": graph.get("device_slug", slug),
        "boot_sequence": graph.get("boot_sequence", []),
        "power_rails": graph.get("power_rails", {}),
        "quality": graph.get("quality"),
    }


@router.get("/packs/{device_slug}/schematic/passives")
async def get_schematic_passives(device_slug: str) -> list[dict]:
    """Return classifier output per passive refdes (kind, role, confidence, source).

    Filters ICs out — only R/C/D/FB emitted. Used for debugging the passive
    classifier and for hand-written fixture generators to look up candidate
    refdes without deserializing the entire electrical_graph.json.
    """
    settings = get_settings()
    slug = _slugify(device_slug)
    graph_path = Path(settings.memory_root) / slug / "electrical_graph.json"
    if not graph_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No schematic ingested yet for device_slug={slug!r}",
        )
    try:
        graph = json.loads(graph_path.read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Malformed electrical_graph.json for {slug!r}: {exc}",
        ) from exc

    components = graph.get("components", {})
    return [
        {
            "refdes": refdes,
            "kind": comp.get("kind", "ic"),
            "role": comp.get("role"),
            "confidence": 0.7,  # classifier confidence not yet persisted on
            # ComponentNode — follow-up phase. Stubbed here.
            "source": "heuristic",
        }
        for refdes, comp in components.items()
        if comp.get("kind", "ic") != "ic"
    ]


@router.post("/packs/{device_slug}/schematic/simulate")
async def post_simulate(device_slug: str, request: SimulateRequest) -> dict:
    """Run the behavioral simulator on the compiled electrical graph.

    Accepts killed_refdes (sugar), explicit failures (causes), and
    rail_overrides (observations). Synchronous (< 10 ms on MNT-class
    boards). HTTP context is stateless — no probe_route enrichment
    here; clients that need a route go through the agent WS path.
    """
    settings = get_settings()
    slug = _slugify(device_slug)
    pack_dir = Path(settings.memory_root) / slug
    graph_path = pack_dir / "electrical_graph.json"
    if not pack_dir.exists() or not graph_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No schematic ingested yet for device_slug={slug!r}",
        )

    try:
        electrical = ElectricalGraph.model_validate_json(graph_path.read_text())
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Malformed electrical_graph for {slug!r}: {exc}",
        ) from exc

    invalid = [
        r
        for r in list(request.killed_refdes) + [f.refdes for f in request.failures]
        if r not in electrical.components
    ]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown refdes: {invalid}",
        )
    invalid_rails = [
        o.label for o in request.rail_overrides if o.label not in electrical.power_rails
    ]
    if invalid_rails:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown rails: {invalid_rails}",
        )

    analyzed: AnalyzedBootSequence | None = None
    ab_path = pack_dir / "boot_sequence_analyzed.json"
    if ab_path.exists():
        try:
            analyzed = AnalyzedBootSequence.model_validate_json(ab_path.read_text())
        except Exception:
            analyzed = None

    tl = SimulationEngine(
        electrical,
        analyzed_boot=analyzed,
        killed_refdes=list(request.killed_refdes),
        failures=list(request.failures),
        rail_overrides=list(request.rail_overrides),
    ).run()
    return tl.model_dump()


class HypothesizeRequest(BaseModel):
    state_comps: dict[str, str] = Field(default_factory=dict)
    state_rails: dict[str, str] = Field(default_factory=dict)
    metrics_comps: dict[str, dict] = Field(default_factory=dict)
    metrics_rails: dict[str, dict] = Field(default_factory=dict)
    max_results: int = Field(default=5, ge=1, le=20)
    repair_id: str | None = None


@router.post("/packs/{device_slug}/schematic/hypothesize")
async def post_hypothesize(device_slug: str, request: HypothesizeRequest) -> dict:
    """Rank candidate refdes-kills that explain the tech's observations.

    Same contract as mb_hypothesize tool. 400 on unknown refdes / rail,
    404 when no electrical_graph is on disk.
    """
    settings = get_settings()
    slug = _slugify(device_slug)
    result = _mb_hypothesize_tool(
        device_slug=slug,
        memory_root=Path(settings.memory_root),
        state_comps=request.state_comps or None,
        state_rails=request.state_rails or None,
        metrics_comps=request.metrics_comps or None,
        metrics_rails=request.metrics_rails or None,
        max_results=request.max_results,
        repair_id=request.repair_id,
    )
    if not result.get("found"):
        reason = result.get("reason", "unknown")
        if reason == "no_schematic_graph":
            raise HTTPException(status_code=404, detail=f"No schematic for {slug!r}")
        if reason in ("unknown_refdes", "unknown_rail"):
            raise HTTPException(status_code=400, detail=result)
        raise HTTPException(status_code=422, detail=result)
    result.pop("found", None)
    return result


class MeasurementCreate(BaseModel):
    target: str
    value: float
    unit: str
    nominal: float | None = None
    note: str | None = None


@router.post(
    "/packs/{device_slug}/repairs/{repair_id}/measurements",
    status_code=201,
)
async def post_measurement(
    device_slug: str,
    repair_id: str,
    body: MeasurementCreate,
) -> dict:
    """Append a measurement event to the repair journal and auto-classify it.

    Returns `{recorded, auto_classified_mode, timestamp}`. 400 when the
    target string fails parse (expected `rail:<name>` or `comp:<refdes>`).
    WS emission is deliberately skipped here — the tech's direct UI clicks
    are observed by the agent only when it polls the journal.
    """
    settings = get_settings()
    safe_repair_id = _validate_repair_id(repair_id)
    result = _mb_record_measurement(
        device_slug=_slugify(device_slug),
        repair_id=safe_repair_id,
        memory_root=Path(settings.memory_root),
        target=body.target,
        value=body.value,
        unit=body.unit,
        nominal=body.nominal,
        note=body.note,
        source="ui",
    )
    if not result.get("recorded"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.get("/packs/{device_slug}/repairs/{repair_id}/measurements")
async def get_measurements(
    device_slug: str,
    repair_id: str,
    target: str | None = None,
    since: str | None = None,
) -> dict:
    """Return the measurement journal for a repair, newest-first.

    Optional `?target=rail:+3V3` and `?since=<ISO-ts>` query filters.
    Always returns `{found, events}` — `events` is empty when the journal
    has no matching entries.
    """
    settings = get_settings()
    safe_repair_id = _validate_repair_id(repair_id)
    return _mb_list_measurements(
        device_slug=_slugify(device_slug),
        repair_id=safe_repair_id,
        memory_root=Path(settings.memory_root),
        target=target,
        since=since,
    )


class ClassifyIntentRequest(BaseModel):
    # pattern requires at least one non-whitespace char — rejects blank/whitespace-only input as 422
    text: str = Field(min_length=1, max_length=400, pattern=r"\S")


@router.post("/classify-intent", response_model=IntentClassification)
async def classify_intent_route(payload: ClassifyIntentRequest) -> IntentClassification:
    """Run the landing-page intent classifier (Haiku forced tool)."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="Anthropic API key not configured")
    client_anth = AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=settings.anthropic_max_retries)
    try:
        return await classify_intent(payload.text.strip(), client=client_anth)
    except (APIError, APIConnectionError, APITimeoutError, asyncio.TimeoutError) as exc:
        raise HTTPException(status_code=503, detail=f"intent classifier failed: {exc}") from exc


__all__ = ["router"]
