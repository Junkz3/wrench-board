"""Pipeline package — FastAPI router for the knowledge-generation factory.

Exposes:
    POST /pipeline/generate          — run the full pipeline (blocking).
    POST /pipeline/repairs           — create a repair + fire-and-forget pipeline.
    WS   /pipeline/progress/{slug}   — stream live pipeline progress events.
    GET  /pipeline/packs             — list packs on disk.
    GET  /pipeline/packs/{slug}      — pack metadata.
    GET  /pipeline/packs/{slug}/full — every JSON artefact in one payload.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from api.config import get_settings
from api.pipeline import events
from api.pipeline.graph_transform import pack_to_graph_payload
from api.pipeline.orchestrator import _slugify, generate_knowledge_pack
from api.pipeline.schemas import PipelineResult

logger = logging.getLogger("microsolder.pipeline.api")

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


class GenerateRequest(BaseModel):
    device_label: str = Field(
        min_length=2,
        max_length=200,
        description="Human-readable device identifier (e.g. 'MNT Reform motherboard').",
    )


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

    Returns the generated repair_id. The file gives the diagnostic conversation
    a durable record of the client's original complaint even if the session
    closes mid-flight.
    """
    repair_id = uuid.uuid4().hex[:12]
    repairs_dir = memory_root / slug / "repairs"
    repairs_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "repair_id": repair_id,
        "device_slug": slug,
        "device_label": device_label,
        "symptom": symptom,
        "created_at": datetime.now(UTC).isoformat(),
    }
    (repairs_dir / f"{repair_id}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return repair_id


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

    # Short-circuit: when the pack is already complete AND the client didn't
    # ask for a rebuild, we skip BOTH the pipeline run AND the repair record.
    # No session is actually starting — the user is just opening the pack —
    # so we don't need to persist a repair UUID on disk. Saves us from
    # accumulating junk files in memory/{slug}/repairs/ for every browse.
    if _pack_is_complete(pack_dir) and not request.force_rebuild:
        logger.info(
            "[API] /pipeline/repairs · pack already complete for slug=%r — opening existing pack",
            slug,
        )
        return RepairResponse(
            repair_id="",
            device_slug=slug,
            device_label=request.device_label,
            pipeline_started=False,
        )

    # Real session starting — persist the repair record.
    repair_id = _persist_repair(memory_root, slug, request.device_label, request.symptom)

    if request.force_rebuild and _pack_is_complete(pack_dir):
        logger.info(
            "[API] /pipeline/repairs · force_rebuild=True · regenerating pack for slug=%r",
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
        await websocket.send_text(
            json.dumps({"type": "subscribed", "device_slug": slug})
        )
        while True:
            event = await queue.get()
            await websocket.send_text(json.dumps(event))
    except WebSocketDisconnect:
        logger.info("[API] /pipeline/progress/%s · client disconnected", slug)
    finally:
        events.unsubscribe(slug, queue)


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


__all__ = ["router"]
