"""Repair-session CRUD + the `POST /repairs` orchestration entry point.

Hosts the background helpers (`_run_pipeline_with_events`,
`_maybe_check_coverage`) and the `_persist_repair` dedup writer that
backs the home library. Pack enrichment is no longer auto-fired here:
an uncovered symptom opens the repair and the diagnostic agent triggers
`mb_expand_knowledge` on demand (plan-gated) only when it needs it.

Also re-exports `_run_pipeline_with_events` via the package
`__init__.py` — `tests/pipeline/test_pipeline_events_narration.py`
imports it directly under that name.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from anthropic import AsyncAnthropic
from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from pydantic import ValidationError

import api.pipeline as _pkg  # noqa: PLC0415 — module-attribute lookups for patchability
from api.agent.memory_stores import delete_repair_store
from api.pipeline import events
from api.pipeline.build_state import read_build_state
from api.pipeline.device_registry import get_device_registry_store, resolve_device
from api.pipeline.models import (
    DisambiguationCandidate,
    RepairRequest,
    RepairResponse,
    RepairSummary,
    ResolveDeviceRequest,
    ResolveDeviceResponse,
)
from api.pipeline.orchestrator import _slugify
from api.pipeline.routes._helpers import _validate_repair_id
from api.pipeline.routes.documents import persist_upload
from api.pipeline.routes.packs import _pack_is_complete

logger = logging.getLogger("wrench_board.pipeline.api")

router = APIRouter()


# ---------------------------------------------------------------------------
# Board-delta auto-generation helpers
# ---------------------------------------------------------------------------

def _should_autogenerate_delta(
    *,
    is_new: bool,
    board_number: str | None,
    allow_expand: bool,
    memory_root: Path,
    slug: str,
) -> bool:
    """Pure decision function: should we fire a board-delta auto-generation?

    Returns True only when ALL conditions hold:
    - this is a brand-new repair (``is_new=True``);
    - a board_number was supplied;
    - generation is allowed (``allow_expand=True`` — the front-door plan signal;
      self-host always sends True / omits the field, so it defaults to True);
    - no delta already exists on disk for (slug, board_number) — avoid respend.
    """
    if not is_new:
        return False
    if not board_number:
        return False
    if not allow_expand:
        return False
    # Lazy import keeps the cold-path fast when board_delta is not needed.
    from api.pipeline.board_delta.store import normalize_board_number, read_delta
    norm = normalize_board_number(board_number)
    if not norm:
        return False
    existing = read_delta(memory_root=memory_root, device_slug=slug, board_number=norm)
    return existing is None


async def _autogenerate_delta_task(
    *,
    device_label: str,
    board_number: str,
    slug: str,
    memory_root: Path,
    owner_ref: str | None,
) -> None:
    """Fire-and-forget background coroutine: generate and write a board delta.

    Mirrors the style of ``_run_pipeline_with_events``: best-effort, logs and
    swallows all exceptions so a delta failure never breaks the repair session.
    The AsyncAnthropic client MUST carry ``max_retries`` on the same line
    (governance grep in tests/agent/test_client_config.py).
    """
    from api.pipeline.board_delta.agent import generate_board_delta
    from api.pipeline.board_delta.store import write_delta

    settings = _pkg.get_settings()
    if not settings.anthropic_api_key:
        logger.info(
            "[BoardDelta] auto-gen skipped for slug=%r board=%r: no API key configured",
            slug, board_number,
        )
        return
    try:
        from datetime import UTC
        from datetime import datetime as _dt
        client = AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=settings.anthropic_max_retries)  # noqa: E501
        logger.info(
            "[BoardDelta] auto-generating delta for slug=%r board_number=%r",
            slug, board_number,
        )
        delta = await generate_board_delta(
            client=client,
            model=settings.anthropic_model_main,
            device_label=device_label,
            board_number=board_number,
        )
        delta.generated_at = _dt.now(UTC).isoformat()
        delta.generated_by_tenant = owner_ref
        write_delta(memory_root=memory_root, device_slug=slug, delta=delta)
        logger.info(
            "[BoardDelta] auto-gen complete for slug=%r board=%r coverage=%s",
            slug, board_number, delta.coverage,
        )
    except Exception:  # noqa: BLE001 — best-effort; never break repair creation
        logger.exception(
            "[BoardDelta] auto-gen failed for slug=%r board=%r", slug, board_number
        )


# Registry of in-flight pipeline tasks, keyed by device slug. The engine holds
# the only handle on a running pipeline, so cooperative cancellation lives here:
# POST /repairs/{slug}/cancel cancels the task and publishes a terminal event.
# Process-local (mirrors the events bus) — fine for the single-worker deploy; a
# multi-worker setup would need a shared signal.
_RUNNING: dict[str, asyncio.Task] = {}


def _register_running(slug: str, task: asyncio.Task) -> None:
    """Track a freshly-spawned pipeline task; auto-deregister when it settles."""
    _RUNNING[slug] = task

    def _done(t: asyncio.Task, s: str = slug) -> None:
        # Only drop our own entry — a newer run on the same slug must survive.
        if _RUNNING.get(s) is t:
            _RUNNING.pop(s, None)

    task.add_done_callback(_done)


def _slug_is_building(slug: str) -> bool:
    """True when a pipeline/expand for this slug is already in flight. The pack is
    SHARED device knowledge keyed by slug, so a second request for the same slug
    must NOT launch a duplicate build — that doubles the token spend AND overwrites
    the _RUNNING entry, orphaning the first task (breaking its cooperative cancel).
    The new (per-tenant) repair instead rides the in-flight build's
    /pipeline/progress/{slug} stream. Cross-tenant safe: slugs carry no tenant data.
    """
    task = _RUNNING.get(slug)
    return task is not None and not task.done()


# Count of in-flight RAM/cost-heavy schematic→graph builds (the full pipeline).
# Distinct from _RUNNING (which also tracks expand/analyze tasks): this bounds
# ONLY the heavy builds so concurrent distinct-device uploads can't OOM the host.
# Process-local (single-worker deploy, like _RUNNING).
_active_builds = 0

# FIFO d'attente des builds en surplus du cap. Chaque entrée :
# {"slug": str, "launch": callable() -> coroutine}. Au lieu de rejeter (503) le
# build de trop, on l'empile ici et l'utilisateur voit sa POSITION ; quand un slot
# se libère (done-callback d'un build), on dépile la tête et on la lance. La file
# est globale (le cap l'est) ; un slug déjà en file/build REJOINT (pas de doublon).
_build_queue: list[dict] = []


def _build_cap() -> int:
    """Max concurrent heavy builds; 0 = unlimited."""
    cap = _pkg.get_settings().pipeline_max_concurrent_builds
    return cap if cap and cap > 0 else 0


def _builds_at_capacity() -> bool:
    cap = _build_cap()
    return cap > 0 and _active_builds >= cap


def _slug_queued(slug: str) -> bool:
    return any(item["slug"] == slug for item in _build_queue)


def _queue_position(slug: str) -> int:
    """1-based position of `slug` in the queue, or 0 if not queued."""
    for i, item in enumerate(_build_queue, start=1):
        if item["slug"] == slug:
            return i
    return 0


def _enqueue_build(slug: str, launch) -> int:
    """Append a pending build (`launch` = zero-arg callable → coroutine).
    Returns its 1-based queue position."""
    _build_queue.append({"slug": slug, "launch": launch})
    return len(_build_queue)


async def _publish_queue_positions() -> None:
    """(Re)publish every queued build's current position on its slug's progress
    stream so the browser can show 'En attente — position N' and watch it shrink."""
    for i, item in enumerate(_build_queue, start=1):
        await events.publish(item["slug"], {"type": "queued", "position": i, "ahead": i - 1})


def _drain_queue() -> None:
    """Launch queued builds while a slot is free. Called when a build settles
    (done-callback) — that's when capacity opens up."""
    launched = False
    while not _builds_at_capacity() and _build_queue:
        item = _build_queue.pop(0)
        _register_build(item["slug"], asyncio.create_task(item["launch"]()))
        launched = True
    if launched and _build_queue:
        # The survivors all shifted up one slot → refresh their positions.
        asyncio.create_task(_publish_queue_positions())


def _register_build(slug: str, task: asyncio.Task) -> None:
    """Register a heavy build: track it in _RUNNING AND count it against the
    concurrency cap. When it settles, free the slot and drain the queue."""
    global _active_builds
    _active_builds += 1

    def _dec(_t: asyncio.Task) -> None:
        global _active_builds
        _active_builds -= 1
        _drain_queue()

    task.add_done_callback(_dec)
    _register_running(slug, task)


def _persist_repair(
    memory_root: Path,
    slug: str,
    device_label: str,
    symptom: str,
    owner_ref: str | None = None,
) -> tuple[str, bool]:
    """Write the repair metadata to memory/{slug}/repairs/{repair_id}.json.

    Returns `(repair_id, is_new)`. When an `open` or `in_progress` repair
    already exists on this `(slug, normalised_symptom)` **AND the same owner_ref**
    we **reuse its id** and return `is_new=False` — the caller short-circuits
    coverage + expand so the technician doesn't burn $0.40 of LLM tokens every
    time they resubmit the same form. Closed repairs do NOT block dedup: starting
    a new ticket on a previously-resolved symptom is intentional.

    `owner_ref` scopes the dedup to one owner: a multi-tenant front-door passes
    the tenant id so two tenants on the same (device, symptom) get SEPARATE
    repairs (no cross-tenant id reuse, no shared conversations). Standalone runs
    pass None → all repairs share one (None) owner, preserving single-tenant
    behaviour. The match is exact (None matches only None), so a tenant request
    never reuses a legacy ownerless repair and vice-versa.

    `status` starts at 'open' on a fresh repair and is updated as the
    session evolves.
    """
    repairs_dir = memory_root / slug / "repairs"
    repairs_dir.mkdir(parents=True, exist_ok=True)

    norm_symptom = symptom.strip().lower()
    if norm_symptom:
        for existing_path in repairs_dir.glob("*.json"):
            try:
                payload = json.loads(existing_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("status") not in ("open", "in_progress"):
                continue
            if (payload.get("symptom") or "").strip().lower() != norm_symptom:
                continue
            # Owner-scoped: never reuse another owner's repair (cross-tenant guard).
            if payload.get("owner_ref") != owner_ref:
                continue
            existing_id = payload.get("repair_id")
            if isinstance(existing_id, str) and existing_id:
                return existing_id, False

    repair_id = uuid.uuid4().hex[:12]
    payload = {
        "repair_id": repair_id,
        "device_slug": slug,
        "device_label": device_label,
        "symptom": symptom,
        "status": "open",
        "created_at": datetime.now(UTC).isoformat(),
    }
    if owner_ref is not None:
        payload["owner_ref"] = owner_ref
    (repairs_dir / f"{repair_id}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return repair_id, True


@router.get("/repairs", response_model=list[RepairSummary])
async def list_repairs() -> list[RepairSummary]:
    """Return every repair ever created, across every device, newest first.

    Powers the home library: each row is one client intervention the
    technician can open, reopen, or finish. Status drives the visual
    badge ('open' · 'in_progress' · 'closed').
    """
    settings = _pkg.get_settings()
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
        # Build state is per-PACK (per device_slug), shared by every repair on
        # that device — read it once per pack_dir, not once per repair file.
        marker = read_build_state(pack_dir)
        build_state = marker.get("status") if marker else None
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
                    board_number=payload.get("board_number") or None,
                    build_state=build_state,
                )
            )
    results.sort(key=lambda r: r.created_at, reverse=True)
    return results


@router.get("/repairs/{repair_id}", response_model=RepairSummary)
async def get_repair(repair_id: str) -> RepairSummary:
    """Return one repair's metadata — used to resume a session from its id."""
    settings = _pkg.get_settings()
    root = Path(settings.memory_root)
    if not root.exists():
        raise HTTPException(status_code=404, detail=f"No repair {repair_id!r}")
    for pack_dir in root.iterdir():
        if not pack_dir.is_dir():
            continue
        candidate = pack_dir / "repairs" / f"{repair_id}.json"
        if candidate.exists():
            payload = json.loads(candidate.read_text())
            marker = read_build_state(pack_dir)
            return RepairSummary(
                repair_id=payload.get("repair_id", repair_id),
                device_slug=payload.get("device_slug", pack_dir.name),
                device_label=payload.get("device_label", pack_dir.name),
                symptom=payload.get("symptom", ""),
                status=payload.get("status", "open"),
                created_at=payload.get("created_at", ""),
                board_number=payload.get("board_number") or None,
                build_state=marker.get("status") if marker else None,
            )
    raise HTTPException(status_code=404, detail=f"No repair {repair_id!r}")


@router.delete("/repairs/{repair_id}")
async def delete_repair(repair_id: str) -> dict:
    """Delete a repair: its disk artefacts AND any per-repair MA memory store.

    Scope:
      - removes `memory/{slug}/repairs/{repair_id}.json` (the metadata)
      - removes `memory/{slug}/repairs/{repair_id}/` (subdir with chat
        history, findings, managed.json marker, conversations…)
      - calls the Managed Agents API to delete the per-repair memory store
        named `wrench-board-repair-{slug}-{repair_id}` (if a marker is on
        disk). Best-effort: an MA failure logs but doesn't block disk
        cleanup, since a stranded store is recoverable manually whereas a
        half-deleted disk state is not.

    Does NOT touch the device-level pack (`memory/{slug}/*.json`) or the
    shared `device-{slug}` / `global-*` memory stores — that knowledge is
    reused by the next repair on the same device.
    """
    settings = _pkg.get_settings()
    root = Path(settings.memory_root)
    if not root.exists():
        raise HTTPException(status_code=404, detail=f"No repair {repair_id!r}")

    metadata_file: Path | None = None
    device_slug: str | None = None
    for pack_dir in root.iterdir():
        if not pack_dir.is_dir():
            continue
        candidate = pack_dir / "repairs" / f"{repair_id}.json"
        if candidate.exists():
            metadata_file = candidate
            device_slug = pack_dir.name
            break

    if metadata_file is None or device_slug is None:
        raise HTTPException(status_code=404, detail=f"No repair {repair_id!r}")

    # 1. MA cleanup (best-effort). Drives off the on-disk marker so that a
    # repair which never opened a session simply no-ops here.
    store_deleted = False
    try:
        client = AsyncAnthropic(api_key=settings.anthropic_api_key or "missing", max_retries=settings.anthropic_max_retries)  # noqa: E501
        store_deleted = await delete_repair_store(
            client, device_slug=device_slug, repair_id=repair_id
        )
    except Exception as exc:  # noqa: BLE001 — MA cleanup is best-effort; never block disk wipe
        logger.warning(
            "delete_repair: MA cleanup raised for repair=%s: %s — proceeding with disk",
            repair_id,
            exc,
        )

    # 2. Disk cleanup. Remove subdir first (best-effort), then the metadata
    # JSON. Order matters — list_repairs scans the JSONs, so wiping the
    # metadata last is what makes the repair disappear from the library.
    subdir = metadata_file.parent / repair_id
    dir_deleted = False
    if subdir.exists() and subdir.is_dir():
        try:
            shutil.rmtree(subdir)
            dir_deleted = True
        except OSError as exc:
            logger.error(
                "delete_repair: rmtree failed for %s: %s", subdir, exc
            )
            raise HTTPException(
                status_code=500,
                detail=f"Failed to remove repair directory: {exc}",
            ) from exc

    try:
        metadata_file.unlink()
    except OSError as exc:
        logger.error(
            "delete_repair: unlink failed for %s: %s", metadata_file, exc
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to remove repair metadata: {exc}",
        ) from exc

    return {
        "repair_id": repair_id,
        "device_slug": device_slug,
        "store_deleted": store_deleted,
        "dir_deleted": dir_deleted,
    }


@router.get("/repairs/{repair_id}/conversations")
def list_repair_conversations(repair_id: str) -> dict:
    """Return the conversation index for a repair.

    The repair's `device_slug` is inferred from the metadata file one level
    up in `memory/{slug}/repairs/{repair_id}.json` — clients don't pass it.
    """
    from api.agent.chat_history import list_conversations

    settings = _pkg.get_settings()
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


@router.delete("/repairs/{repair_id}/conversations/{conv_id}")
def delete_repair_conversation(repair_id: str, conv_id: str) -> dict:
    """Delete a single conversation from a repair.

    Wipes `memory/{slug}/repairs/{repair_id}/conversations/{conv_id}/` and
    drops the matching entry from `conversations/index.json`. The repair
    itself, its metadata file, sibling conversations, and the shared
    repair-level MA memory store are untouched. The per-tier MA sessions
    stored under the conv dir are dropped with it; their upstream Anthropic
    counterparts are left to expire naturally.
    """
    safe_repair_id = _validate_repair_id(repair_id)
    safe_conv_id = _validate_repair_id(conv_id)  # same shape constraints
    from api.agent.chat_history import delete_conversation

    settings = _pkg.get_settings()
    memory = Path(settings.memory_root)
    found_slug: str | None = None
    if memory.exists():
        for metadata_file in memory.glob(f"*/repairs/{safe_repair_id}.json"):
            found_slug = metadata_file.parent.parent.name
            break
    if not found_slug:
        raise HTTPException(status_code=404, detail=f"unknown repair_id {safe_repair_id}")

    try:
        removed = delete_conversation(
            device_slug=found_slug,
            repair_id=safe_repair_id,
            conv_id=safe_conv_id,
        )
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to remove conversation: {exc}",
        ) from exc

    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"unknown conversation {safe_conv_id} for repair {safe_repair_id}",
        )

    return {
        "repair_id": safe_repair_id,
        "conv_id": safe_conv_id,
        "device_slug": found_slug,
        "removed": True,
    }


@router.get("/repairs/{repair_id}/protocol")
async def get_repair_protocol(
    repair_id: str, device_slug: str, conv: str | None = None,
) -> dict:
    """Return the active protocol artifact for this repair (or {active: false}).

    `conv` is the conversation id to scope the lookup. When omitted, falls
    back to the legacy repair-root protocol pointer (kept for backward
    compatibility with pre-per-conv artefacts).
    """
    from api.tools.protocol import load_active_protocol
    settings = _pkg.get_settings()
    proto = load_active_protocol(
        Path(settings.memory_root), device_slug, repair_id, conv_id=conv,
    )
    if proto is None:
        return {"active": False}
    return {
        "active": True,
        "protocol_id": proto.protocol_id,
        "title": proto.title,
        "rationale": proto.rationale,
        "current_step_id": proto.current_step_id,
        "status": proto.status,
        "steps": [s.model_dump(mode="json") for s in proto.steps],
        "history": [h.model_dump(mode="json") for h in proto.history],
    }


async def _run_pipeline_with_events(
    device_label: str,
    slug: str,
    focus_symptom: str | None = None,
    *,
    confirmed_device_kind: str | None = None,
    user_device_kind: str | None = None,
    expect_schematic: bool = False,
    owner_ref: str | None = None,
    engine_repair_id: str | None = None,
) -> None:
    """Background task: run the pipeline, relaying its events on the bus.

    Every orchestrator event is forwarded onto the per-slug bus verbatim,
    including the live `phase_step` sub-steps (Scout rounds, schematic pages,
    each writer completing, audit rounds) the landing timeline renders as the
    phase's live line. (The old Haiku `phase_narration` hook was removed — an
    extra LLM call per phase for an after-the-fact sentence; the live
    sub-steps replaced it.)

    `focus_symptom`, when supplied, is threaded to Scout so the technician's
    reason-for-opening-the-repair is prioritised in the web_search rounds.

    `confirmed_device_kind`, when supplied (POST /packs/{slug}/confirm-kind),
    is threaded so the orchestrator trusts the technician's resolved device
    kind instead of re-detecting from the partial graph. Defaulted to None so
    the create_repair caller is unaffected.

    `user_device_kind`, when supplied (create_repair threads
    `request.device_kind`), is the technician's declared device class — a
    prior the graph classifier validates/overrides during reconcile. Keyword-
    only with a None default so the confirm-kind caller (which passes
    `confirmed_device_kind` instead) is unaffected; both params coexist.
    """
    t0 = time.monotonic()

    async def _on_event(ev: dict) -> None:
        await events.publish(slug, ev)

    try:
        await _pkg.generate_knowledge_pack(
            device_label,
            # Pin the pack directory to the repair's slug — without this the
            # orchestrator re-slugifies the rich label and builds a FRESH pack
            # next to the one holding the uploaded documents (graph=no).
            device_slug=slug,
            on_event=_on_event,
            focus_symptom=focus_symptom,
            confirmed_device_kind=confirmed_device_kind,
            user_device_kind=user_device_kind,
            expect_schematic=expect_schematic,
            # T13 build metering: the build's per-phase token spend is reported
            # to the cloud ledger as kind='build' under this tenant + repair
            # (no-op self-host — see api/pipeline/build_metering.py).
            owner_ref=owner_ref,
            engine_repair_id=engine_repair_id,
        )
    except Exception as exc:  # noqa: BLE001 — fire-and-forget bg task; report failure on event bus
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


async def _maybe_check_coverage(
    slug: str,
    symptom: str,
    memory_root: Path,
) -> CoverageCheck:  # noqa: F821 — forward-only type ref
    """Call the Haiku coverage classifier; on any failure, fall back to
    an uncovered verdict so the expand-pack path still fires.

    Lazy-imports `check_symptom_coverage` to avoid a module-load cycle:
    coverage → schemas → api.pipeline (this module)."""
    from api.pipeline.coverage import check_symptom_coverage
    from api.pipeline.schemas import CoverageCheck

    settings = _pkg.get_settings()
    if not settings.anthropic_api_key:
        return CoverageCheck(
            covered=False,
            matched_rule_id=None,
            confidence=0.0,
            reason="no Anthropic API key configured — treating as uncovered",
        )
    client = AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=settings.anthropic_max_retries)
    try:
        return await check_symptom_coverage(
            client=client,
            model=settings.anthropic_model_fast,
            device_slug=slug,
            symptom=symptom,
            memory_root=memory_root,
        )
    except Exception as exc:  # noqa: BLE001 — failure falls through to expand
        logger.warning(
            "[API] coverage check failed for slug=%r (%s); treating as uncovered",
            slug,
            exc,
        )
        return CoverageCheck(
            covered=False,
            matched_rule_id=None,
            confidence=0.0,
            reason=f"coverage classifier error: {exc}",
        )


@router.post("/resolve-device", response_model=ResolveDeviceResponse)
async def resolve_device_route(
    req: ResolveDeviceRequest,
    x_owner_ref: str | None = Header(default=None, alias="X-Owner-Ref"),
) -> ResolveDeviceResponse:
    """Resolve a free device label to a canonical identity (or the ambiguous
    candidate menu) WITHOUT creating a repair or building. The cloud front-door
    calls this before its quota gate so it adopts the canonical slug and gets
    disambiguation for free. A pinned device_slug is returned verbatim."""
    if req.device_slug:
        return ResolveDeviceResponse(canonical_slug=req.device_slug, ambiguous=False, candidates=[])
    memory_root = Path(_pkg.get_settings().memory_root)
    store = get_device_registry_store(memory_root)
    res = await resolve_device(req.device_label, store, owner_ref=x_owner_ref)
    return ResolveDeviceResponse(
        canonical_slug=res["canonical_slug"],
        ambiguous=bool(res["ambiguous"]),
        candidates=[
            DisambiguationCandidate(
                device_slug=c.get("canonicalKey"),
                family=c.get("family"),
                facets=c.get("facets") or {},
            )
            for c in res.get("candidates", [])
        ],
    )


@router.post("/repairs", response_model=RepairResponse)
async def create_repair(
    device_label: str = Form(...),
    symptom: str = Form(...),
    device_slug: str | None = Form(default=None),
    device_kind: str | None = Form(default=None),
    force_rebuild: bool = Form(default=False),
    owner_ref: str | None = Form(default=None),
    allow_expand: bool = Form(default=True),
    schematic_pending: bool = Form(default=False),
    board_number: str | None = Form(default=None),
    file: UploadFile | None = File(default=None),  # noqa: B008 — FastAPI DI idiom
) -> RepairResponse:
    """Register a repair and kick off the pipeline in the background.

    Multipart so the technician can attach a schematic at creation time: when
    `file` is present it is stashed into `memory/{slug}/uploads/` BEFORE the
    fire-and-forget generation, so the orchestrator's inline-ingest builds the
    electrical graph before Phase 1.5 (device-kind classification) runs.

    The response returns immediately with the generated repair_id and device_slug.
    Real-time pipeline progress is streamed via WS /pipeline/progress/{slug}.
    """
    try:
        request = RepairRequest(
            device_label=device_label,
            symptom=symptom,
            device_slug=device_slug,
            device_kind=device_kind,
            force_rebuild=force_rebuild,
            owner_ref=owner_ref,
            allow_expand=allow_expand,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    settings = _pkg.get_settings()
    memory_root = Path(settings.memory_root)
    # T9a device alias registry: when the slug isn't explicitly pinned, resolve
    # the free label to a canonical device identity so aliases of the same board
    # (board# / Apple model / EMC / marketing) land on ONE pack instead of N.
    # Best-effort: any registry hiccup degrades to the naive slugify (today's
    # behavior), so resolution can never block a repair.
    slug = request.device_slug or _slugify(request.device_label)
    resolution = None
    if not request.device_slug:
        try:
            store = get_device_registry_store(memory_root)
            resolution = await resolve_device(
                request.device_label, store, owner_ref=request.owner_ref
            )
            slug = resolution["canonical_slug"]
        except Exception:  # noqa: BLE001 - registry must never break a repair
            logger.warning("[API] device resolution failed for %r — using slug=%r",
                           request.device_label, slug, exc_info=True)

    # T9a confirm-on-uncertainty: a broad term that fans out to several siblings —
    # don't guess. Return the candidate menu; no repair created, no build started.
    if resolution is not None and resolution.get("ambiguous"):
        return RepairResponse(
            repair_id="",
            device_slug=slug,
            device_label=request.device_label,
            pipeline_started=False,
            pipeline_kind="none",
            needs_disambiguation=True,
            candidates=[
                DisambiguationCandidate(
                    device_slug=c.get("canonicalKey"),
                    family=c.get("family"),
                    facets=c.get("facets") or {},
                )
                for c in resolution.get("candidates", [])
            ],
        )
    pack_dir = memory_root / slug

    # Every "new repair" IS a repair session — persist the record
    # whether the pack is fresh or already on disk. Two repairs on the same
    # iPhone X are two separate sessions with two separate contexts; both
    # must be reopenable later from the library.
    repair_id, is_new = _persist_repair(
        memory_root, slug, request.device_label, request.symptom, request.owner_ref
    )
    # Persist board_number on the repair record when supplied. Only on a new
    # repair (is_new=True): a reused ticket keeps the board_number it was
    # created with so older sessions stay consistent. Normalised via
    # normalize_board_number (strips whitespace, canonical separator).
    if board_number and is_new:
        from api.pipeline.board_delta.store import normalize_board_number as _nbn
        repair_path = memory_root / slug / "repairs" / f"{repair_id}.json"
        try:
            payload = json.loads(repair_path.read_text(encoding="utf-8"))
            payload["board_number"] = _nbn(board_number)
            repair_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("[API] could not persist board_number on repair=%s: %s", repair_id, exc)

    # Auto-generate board delta when a board_number is supplied on a NEW repair
    # and the plan allows it. Fire-and-forget: the POST returns immediately;
    # the delta lands on disk in the background without blocking the tech.
    if _should_autogenerate_delta(
        is_new=is_new,
        board_number=board_number,
        allow_expand=allow_expand,
        memory_root=memory_root,
        slug=slug,
    ):
        asyncio.create_task(
            _autogenerate_delta_task(
                device_label=request.device_label,
                board_number=board_number,  # type: ignore[arg-type] — guarded by _should_autogenerate_delta
                slug=slug,
                memory_root=memory_root,
                owner_ref=request.owner_ref,
            )
        )

    # Lot 2 : owner-aware. Un build web-only mis en staging pour CE tenant compte
    # comme complet POUR LUI (il rouvre son repair sans relancer un build), mais
    # reste incomplet pour le commons → un autre tenant rebuild proprement.
    pack_complete = _pack_is_complete(pack_dir, owner_ref=request.owner_ref)

    # Branch 0 — the tech resubmitted the form for an already-open ticket
    # on the same (device, symptom). On a COMPLETE pack, reuse the existing
    # repair without burning a coverage check or an expand-pack round-trip
    # (without this short-circuit a stuck-on-low-confidence coverage classifier
    # loops and chews $0.40 of LLM tokens every retry). On an INCOMPLETE pack
    # (failed/interrupted build — see build_state), fall through to Branch 1
    # instead: the resubmit IS the 'relancer' flow, and the rebuild re-fires on
    # this same repair, riding the hash caches. force_rebuild also falls
    # through, so an explicit rebuild request on an open ticket is honored.
    if not is_new:
        if pack_complete and not request.force_rebuild:
            logger.info(
                "[API] /pipeline/repairs · reusing open repair=%s for slug=%r — no LLM run",
                repair_id,
                slug,
            )
            return RepairResponse(
                repair_id=repair_id,
                device_slug=slug,
                device_label=request.device_label,
                pipeline_started=False,
                pipeline_kind="none",
                coverage_reason="reusing existing open ticket on the same symptom",
            )
        logger.info(
            "[API] /pipeline/repairs · open repair=%s on INCOMPLETE pack for slug=%r — re-firing the build",
            repair_id,
            slug,
        )

    # Stash an attached schematic — either a fresh repair or a retry that is about
    # to re-fire the build (a complete-pack dedup hit returned above). Done BEFORE
    # the generation kickoff so the orchestrator's inline-ingest picks it up; we
    # deliberately do NOT trigger the documents-endpoint auto-pin/background ingest
    # here (it would race the inline-ingest).
    if file is not None and file.filename:
        await persist_upload(pack_dir / "uploads", "schematic_pdf", file)

    # Branch 1 — pack missing (or force_rebuild): fire the full pipeline
    # with the symptom threaded to Scout as a priority target.
    if not pack_complete or request.force_rebuild:
        # Stampede guard: a build for this shared-by-slug pack is already running →
        # the new (per-tenant) repair rides it instead of launching a duplicate.
        if _slug_is_building(slug):
            logger.info(
                "[API] /pipeline/repairs · slug=%r already building — repair=%s joins the in-flight pipeline",
                slug,
                repair_id,
            )
            return RepairResponse(
                repair_id=repair_id,
                device_slug=slug,
                device_label=request.device_label,
                pipeline_started=True,
                pipeline_kind="full",
            )
        # Already QUEUED for this slug → join it (no duplicate build), return its
        # current position so the UI shows the same waiting state.
        if _slug_queued(slug):
            pos = _queue_position(slug)
            return RepairResponse(
                repair_id=repair_id,
                device_slug=slug,
                device_label=request.device_label,
                pipeline_started=True,
                pipeline_kind="full",
                queued=True,
                queue_position=pos,
            )
        if request.force_rebuild and pack_complete:
            logger.info(
                "[API] /pipeline/repairs · force_rebuild=True · repair=%s regenerating pack for slug=%r",
                repair_id,
                slug,
            )

        # Launcher capturing the exact build args — run now if a slot is free,
        # else ENQUEUE it (the user sees a position; it starts when a slot frees).
        def _launch():
            return _run_pipeline_with_events(
                request.device_label,
                slug,
                focus_symptom=request.symptom,
                user_device_kind=request.device_kind,
                # A schematic uploaded via /documents (not the create body) lands
                # out-of-band — tell the pipeline to wait for its electrical graph
                # before device-kind classification. Skip when the file rode the
                # create body (inline-ingest handles it).
                expect_schematic=schematic_pending and file is None,
                owner_ref=request.owner_ref,
                engine_repair_id=repair_id,
            )

        if _builds_at_capacity():
            pos = _enqueue_build(slug, _launch)
            await events.publish(slug, {"type": "queued", "position": pos, "ahead": pos - 1})
            logger.info(
                "[API] /pipeline/repairs · build queued at position %d (cap=%d) for slug=%r",
                pos,
                _build_cap(),
                slug,
            )
            return RepairResponse(
                repair_id=repair_id,
                device_slug=slug,
                device_label=request.device_label,
                pipeline_started=True,
                pipeline_kind="full",
                queued=True,
                queue_position=pos,
            )

        logger.info(
            "[API] /pipeline/repairs · firing full pipeline for slug=%r · focus_symptom=yes",
            slug,
        )
        _register_build(slug, asyncio.create_task(_launch()))
        return RepairResponse(
            repair_id=repair_id,
            device_slug=slug,
            device_label=request.device_label,
            pipeline_started=True,
            pipeline_kind="full",
        )

    # Pack complete — compare the symptom against existing rules before
    # spending tokens on an expand round-trip.
    coverage = await _pkg._maybe_check_coverage(slug, request.symptom, memory_root)

    # Branch 2 — symptom already covered by an existing rule: skip LLM
    # work entirely and return the matched_rule_id so the UI can surface
    # the known diagnostic flow immediately.
    if (
        coverage.covered
        and coverage.confidence >= 0.7
        and coverage.matched_rule_id is not None
    ):
        logger.info(
            "[API] /pipeline/repairs · symptom covered by %s (confidence=%.2f) — no LLM run",
            coverage.matched_rule_id,
            coverage.confidence,
        )
        return RepairResponse(
            repair_id=repair_id,
            device_slug=slug,
            device_label=request.device_label,
            pipeline_started=False,
            pipeline_kind="none",
            matched_rule_id=coverage.matched_rule_id,
            coverage_reason=coverage.reason,
        )

    # Branch 3 — pack complete but symptom uncovered.
    #
    # We DO NOT auto-fire expand_pack here anymore. Enrichment (the paid
    # Scout + Clinicien pass, ~$0.40) is a *recourse*, not a step: the repair
    # opens normally and the diagnostic agent works the existing electrical
    # graph + rules first. Only if it comes up empty-handed does it PROPOSE
    # `mb_expand_knowledge` (with the tech's go-ahead), and that tool is itself
    # plan-gated (free tenants don't get it — see session_caps / manifest).
    #
    # This collapses the old double-trigger (auto-expand at create_repair AND
    # the agent tool) onto the single agent-driven path, kills the "spend on a
    # web search before the agent even looked" waste, and removes the need to
    # drop the ticket: it stays alive so the agent session can attach to it.
    # `allow_expand` (front-door flag) is now inert here — kept on the request
    # for skew-tolerance with an older cloud, but no longer changes behaviour.
    logger.info(
        "[API] /pipeline/repairs · pack complete for slug=%r; symptom uncovered — "
        "opening repair, agent works the graph (expand is on-demand, plan-gated)",
        slug,
    )
    return RepairResponse(
        repair_id=repair_id,
        device_slug=slug,
        device_label=request.device_label,
        pipeline_started=False,
        pipeline_kind="none",
        coverage_reason=coverage.reason,
    )


@router.post("/repairs/{device_slug}/cancel")
async def cancel_repair(device_slug: str) -> dict:
    """Cooperatively cancel a running pipeline for this device slug.

    The pipeline runs as a background task whose only handle lives in this
    process (`_RUNNING`). We cancel that task and publish a terminal
    `pipeline_failed(CANCELLED)` so every progress subscriber (the cloud relay,
    the browser timeline) stops waiting. Idempotent: cancelling a slug with no
    running pipeline is a no-op, not an error — the cloud must not 500 on a
    stale cancel.

    Unauthenticated like the other HTTP pipeline endpoints — the cloud is the
    gatekeeper (only it can reach the engine once deployed).
    """
    slug = _slugify(device_slug)
    task = _RUNNING.get(slug)
    if task is None or task.done():
        logger.info("[API] /pipeline/repairs/%s/cancel · nothing running", slug)
        return {"cancelled": False, "device_slug": slug, "reason": "no running pipeline"}

    task.cancel()
    await events.publish(
        slug,
        {
            "type": "pipeline_failed",
            "status": "CANCELLED",
            "error": "analysis cancelled",
        },
    )
    logger.info("[API] /pipeline/repairs/%s/cancel · pipeline cancelled", slug)
    return {"cancelled": True, "device_slug": slug}
