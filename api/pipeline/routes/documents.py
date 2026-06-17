"""Technician-supplied document uploads + active-source pin management.

Endpoints:
  POST /packs/{slug}/documents          — upload a file under uploads/
  GET  /packs/{slug}/documents          — list uploaded files
  GET  /packs/{slug}/sources            — list versioned sources + active pin
  PUT  /packs/{slug}/sources/{kind}     — switch the active pin
  GET/HEAD /packs/{slug}/boardview      — serve the active boardview file
  GET/HEAD /packs/{slug}/schematic.pdf  — serve the active schematic PDF

The auto-pin logic and `_apply_schematic_pin` cache-vs-reingest helper
are local to this module — they're shared by the POST /documents
auto-pin path and the explicit PUT /sources/{kind} switch.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from anthropic import AsyncAnthropic
from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

import api.pipeline as _pkg  # noqa: PLC0415 — module-attribute lookups for patchability
from api.pipeline import events, live_graph, sources
from api.pipeline.models import (
    DeleteSourceResponse,
    DocumentUploadResponse,
    SourceKindEntry,
    SourcesResponse,
    SourceVersion,
    SwitchSourceRequest,
    SwitchSourceResponse,
)
from api.pipeline.orchestrator import _slugify
from api.pipeline.routes._helpers import _validate_slug
from api.pipeline.routes.packs import _find_boardview

logger = logging.getLogger("wrench_board.pipeline.api")

router = APIRouter()


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


async def persist_upload(uploads_dir: Path, kind: str, file: UploadFile) -> tuple[Path, int]:
    """Stream an UploadFile into uploads_dir as `{ts}-{kind}-{safe_name}`.

    Chunked to disk (never holds the whole blob in memory) with the
    `_MAX_UPLOAD_BYTES` cap. Returns (target_path, bytes_written). Pure
    persistence — NO auto-pin / ingestion side effects (those stay in
    post_pack_document). Intended for reuse by create_repair (a later task)
    to stash a schematic attached at repair-creation time.
    """
    uploads_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    filename = _safe_filename(file.filename or "upload")
    target = uploads_dir / f"{timestamp}-{kind}-{filename}"
    total = 0
    try:
        with target.open("wb") as fh:
            while True:
                chunk = await file.read(1 << 20)  # 1 MiB
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_UPLOAD_BYTES:
                    fh.close()  # close before unlink (cross-platform safe; matches the original endpoint)
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
    return target, total


@router.post(
    "/packs/{device_slug}/documents",
    response_model=DocumentUploadResponse,
    status_code=201,
)
async def post_pack_document(
    device_slug: str,
    kind: str = Form(...),
    description: str | None = Form(default=None),
    file: UploadFile = File(...),  # noqa: B008 — FastAPI dependency-injection idiom
    x_owner_ref: str | None = Header(default=None, alias="X-Owner-Ref"),
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

    settings = _pkg.get_settings()
    uploads_dir = Path(settings.memory_root) / slug / "uploads"
    filename = _safe_filename(file.filename or "upload")
    target, total = await persist_upload(uploads_dir, kind, file)

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

    # Versioning glue. Two responsibilities:
    #   1. Archive the legacy schematic.pdf (one that pre-dates this system)
    #      into uploads/ + cache, so the technician can switch back to it.
    #   2. Auto-pin: the FIRST upload of a kind becomes active. We then
    #      materialise the pin (cache hit or kick off ingestion). Without
    #      step 2, the pin would point to a file that doesn't match the
    #      derived artefacts on disk — incoherent.
    if kind in sources.KNOWN_KINDS:
        pack_dir = uploads_dir.parent
        if kind == sources.SCHEMATIC_KIND:
            # MUST run before reading existing uploads — once we've added
            # the new file, list_uploads_for_kind would no longer be empty.
            _archive_legacy_schematic_if_present(pack_dir)
        # Auto-pin: the FIRST upload of a kind becomes active. "First" is
        # per-owner in managed mode (each tenant pins its own first upload),
        # global for self-host.
        if x_owner_ref is not None:
            owner_active = live_graph.read_owner_active(pack_dir, x_owner_ref)
            already_pinned = bool(owner_active.get(kind))
        else:
            already_pinned = bool(sources.read_active(pack_dir).get(kind))

        if not already_pinned:
            if kind == sources.SCHEMATIC_KIND:
                # Pin-write asymmetry: in managed mode the per-owner pointer is
                # written INSIDE `_apply_schematic_pin` (it needs the PDF hash);
                # in self-host the CALLER writes the global root pin first, here,
                # then calls the helper to materialise the graph. Cache hit (rare
                # for a brand-new upload) → instant; miss → background ingestion.
                if x_owner_ref is None:
                    pins = sources.read_active(pack_dir)
                    pins[kind] = target.name
                    sources.write_active(pack_dir, pins)
                try:
                    _apply_schematic_pin(slug, pack_dir, target.name, owner_ref=x_owner_ref)
                except OSError:
                    logger.warning(
                        "could not materialise schematic pin for %s",
                        target.name,
                        exc_info=True,
                    )
            elif x_owner_ref is not None:
                # Managé, kind sans graphe dérivé (boardview) — pin only.
                live_graph.write_owner_active(pack_dir, x_owner_ref, kind, target.name, None)
            else:
                pins = sources.read_active(pack_dir)
                pins[kind] = target.name
                sources.write_active(pack_dir, pins)

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
    settings = _pkg.get_settings()
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


# ─── Versioned sources — list + switch ─────────────────────────────────

# Vision pipeline ~ wall-clock per page on Opus 4.8 with grounding +
# fan-out parallelism: empirically 25-35s on iPhone X / MNT Reform sized
# packs. We bias slightly high (35s) so the countdown rarely undershoots
# the real completion. Updated value lives here so a single tweak
# propagates to both the response ETA and the doc string above.
_VISION_SECONDS_PER_PAGE = 35


def _count_pdf_pages(pdf_path: Path) -> int | None:
    """Return the page count without opening the file twice.

    pdfplumber is already in the dependency tree (used by the renderer +
    grounding extractor) so reusing it avoids pulling in a second PDF
    library. Returns None on any read failure — the caller treats that
    as "ETA unknown" rather than failing the switch.
    """
    try:
        import pdfplumber

        with pdfplumber.open(str(pdf_path)) as pdf:
            return len(pdf.pages)
    except Exception:  # noqa: BLE001 — pdfplumber may raise many parse-time errors; "ETA unknown" is fine
        logger.warning("could not count pages in %s", pdf_path, exc_info=True)
        return None


@router.get("/packs/{device_slug}/sources", response_model=SourcesResponse)
async def get_pack_sources(device_slug: str) -> SourcesResponse:
    """List every uploaded version per kind, with the active pin marked."""
    slug = _validate_slug(device_slug)
    settings = _pkg.get_settings()
    pack_dir = Path(settings.memory_root) / slug
    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")

    pins = sources.read_active(pack_dir)
    out_kinds: dict[str, SourceKindEntry] = {}
    for kind in (sources.SCHEMATIC_KIND, sources.BOARDVIEW_KIND):
        versions = [
            SourceVersion(**v.to_dict())
            for v in sources.list_versions(pack_dir, kind)
        ]
        out_kinds[kind] = SourceKindEntry(
            kind=kind, active=pins.get(kind), versions=versions
        )
    return SourcesResponse(
        device_slug=slug,
        schematic_pdf=out_kinds[sources.SCHEMATIC_KIND],
        boardview=out_kinds[sources.BOARDVIEW_KIND],
    )


# ── Per-slug ingestion serialisation (T9 — cross-tenant cache safety) ────────
#
# In managed mode the slug ROOT (`memory/{slug}/schematic.pdf` + derived files)
# is used as transient build scratch: clear → copy the target PDF → ingest →
# `write_through_cache(root, hash)` snapshots the result into the shared hash
# slot `.cache_schematic/{hash}/`. The root is SHARED across tenants on the same
# slug, so two concurrent managed cache-misses (tenant A/PDF-A/hash hA and
# tenant B/PDF-B/hash hB) would race: if B's copy clobbers the root before A's
# background ingest reads it, `write_through_cache(hA)` snapshots B's graph into
# hA's slot → silent cross-tenant corruption. Mirrors `_RUNNING` in repairs.py
# (process-local, fine for the single-worker deploy; a multi-worker setup would
# need a shared lock). The INVARIANT this guarantees: `write_through_cache(H)`
# only ever snapshots artefacts ingested from the PDF whose hash is H — because
# the clear+copy+ingest+write_through sequence is run to completion for one
# build before the next build on the same slug starts (new requests CHAIN onto
# the in-flight task instead of clobbering the root immediately).
_INGESTING: dict[str, asyncio.Task] = {}


def _schedule_managed_ingest(
    slug: str, pack_dir: Path, target: Path, pdf_hash: str
) -> None:
    """Serialise the clear+copy+ingest of `target` for this slug (managed mode).

    Chains onto any in-flight build for the same slug so the shared root is only
    ever used by ONE build at a time. The clear+copy MUST live inside the chained
    coroutine (not the caller) — otherwise a second sync call would clobber the
    root before the first build's background ingest reads it.
    """
    prior = _INGESTING.get(slug)

    async def _chained() -> None:
        # Wait for any in-flight build on this slug to finish using the root.
        if prior is not None and not prior.done():
            try:
                await prior
            except Exception:  # noqa: BLE001 — prior build's own errors are logged there
                pass
        # The prior build may have produced THIS exact hash (same PDF, e.g. a
        # double-click) — re-validate before re-ingesting so we don't redo work
        # or clobber a freshly-cached slot.
        if sources.is_cached(pack_dir, pdf_hash):
            return
        # Now we own the root exclusively for the duration of this build.
        sources.clear_in_place_schematic(pack_dir)
        shutil.copyfile(target, pack_dir / "schematic.pdf")
        await _reingest_and_cache(slug, pack_dir, pack_dir / "schematic.pdf", pdf_hash)

    task = asyncio.create_task(_chained())
    _INGESTING[slug] = task

    def _done(t: asyncio.Task, s: str = slug) -> None:
        # Only drop our own entry — a newer chained build must survive.
        if _INGESTING.get(s) is t:
            _INGESTING.pop(s, None)

    task.add_done_callback(_done)


async def _reingest_and_cache(slug: str, pack_dir: Path, pdf_path: Path, pdf_hash: str) -> None:
    """Run the schematic vision pipeline then write-through to the hashed cache.

    Used as a background task on PUT sources when the target hash is not
    cached. Errors are logged; the pin stays set so the UI can retry.
    """
    settings = _pkg.get_settings()
    api_key = settings.anthropic_api_key
    if not api_key:
        logger.error("[sources] cannot reingest without ANTHROPIC_API_KEY for %s", slug)
        return
    try:
        client = AsyncAnthropic(api_key=api_key, max_retries=4)

        # Publish only the per-page sub-steps onto the slug bus. The pipeline's
        # wait-gate (orchestrator: expect_schematic) owns this phase's
        # started/finished bracket — we just fill its live line with "page N/M"
        # while it polls for electrical_graph.json to land.
        async def _relay_page_step(ev: dict) -> None:
            if ev.get("type") == "phase_step":
                await events.publish(slug, ev)

        await _pkg.ingest_schematic(
            device_slug=slug,
            pdf_path=pdf_path,
            client=client,
            memory_root=Path(settings.memory_root),
            on_event=_relay_page_step,
        )
        # Persist this version's artefacts so a future switch back is instant.
        sources.write_through_cache(pack_dir, pdf_hash)
        logger.info("[sources] reingest complete for %s hash=%s", slug, pdf_hash)
    except Exception:  # noqa: BLE001 — fire-and-forget bg task; pin stays set so UI can retry
        logger.exception("[sources] reingest failed for %s hash=%s", slug, pdf_hash)


# Reserved filename used to surface a "legacy" schematic.pdf — one that
# pre-dates the versioning system and therefore lives at
# `memory/{slug}/schematic.pdf` without a matching entry in `uploads/`.
# The leading 0-timestamp sorts it to the bottom of the version list so
# newer real uploads stay on top, and the literal "baseline" suffix
# makes it scannable in the UI.
_LEGACY_SCHEMATIC_FILENAME = "00000000T000000Z-schematic_pdf-baseline.pdf"


def _archive_legacy_schematic_if_present(pack_dir: Path) -> str | None:
    """Snapshot an in-place schematic.pdf into `uploads/` + cache.

    Runs once per pack, on the first technician upload of a schematic_pdf.
    If `memory/{slug}/schematic.pdf` exists AND no upload of that kind has
    been recorded yet, this copies it into `uploads/` under a fixed
    "baseline" filename and (when a derived `electrical_graph.json` is
    present) writes through to the hashed cache so the user can switch
    back to it instantly later. Returns the archived filename, or None
    when nothing needed archiving.
    """
    legacy_pdf = pack_dir / "schematic.pdf"
    if not legacy_pdf.exists():
        return None
    # Already archived (or any other schematic_pdf upload exists)? Skip.
    existing = sources.list_uploads_for_kind(pack_dir, sources.SCHEMATIC_KIND)
    if existing:
        return None

    uploads_dir = pack_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    target = uploads_dir / _LEGACY_SCHEMATIC_FILENAME
    try:
        shutil.copyfile(legacy_pdf, target)
    except OSError:
        logger.warning("could not archive legacy schematic for %s", pack_dir.name, exc_info=True)
        return None

    # Cache the legacy artefacts under their hash so a switch back is free.
    if (pack_dir / "electrical_graph.json").exists():
        try:
            legacy_hash = sources.hash_pdf(legacy_pdf)
            sources.write_through_cache(pack_dir, legacy_hash)
        except OSError:
            logger.warning(
                "could not write-through legacy cache for %s",
                pack_dir.name,
                exc_info=True,
            )
    logger.info("[sources] archived legacy schematic for %s as %s", pack_dir.name, target.name)
    return _LEGACY_SCHEMATIC_FILENAME


def _apply_schematic_pin(
    slug: str, pack_dir: Path, target_filename: str, *, owner_ref: str | None = None
) -> tuple[Literal["cached", "rebuilding"], int | None, int | None]:
    """Materialise a schematic_pdf pin. Returns (status, eta, pages).

    Centralises the cache-vs-reingest decision shared by POST /documents
    auto-pin and PUT /sources/{kind}. Caller must already have:
      - validated the target file exists in `uploads/`
      - written the new pin (root pin for self-host; the managed per-owner
        pointer is written HERE, since it needs the PDF hash)

    Self-host (`owner_ref` None) — UNCHANGED:
      On `cached`: copies the cached artefacts back into the root; the new
      graph is live before the function returns.
      On `rebuilding`: copies the source PDF to `memory/{slug}/schematic.pdf`,
      drops stale derivatives, schedules a background ingestion task.

    Managé (`owner_ref` set) — T9 per-owner, NO root clobber:
      Writes the per-owner pointer (_sources/{owner}/) mapping schematic_pdf →
      {filename, hash}; readers (Task 3) resolve owner→hash→.cache_schematic/
      {hash}/ directly. On a cache hit we do NOT restore to the root (that root
      copy is exactly what clobbered cross-tenant). On a miss we still ingest
      via the SAME background task — the root is mere build scratch in managed
      mode, snapshotted to the shared hash-cache by `write_through_cache`; the
      per-slug stampede guard serialises concurrent builds.

    Managed root = transient build scratch: the managed cache-miss path wipes the
    slug root via `clear_in_place_schematic` and uses it as scratch for the vision
    pipeline (serialised per slug by `_schedule_managed_ingest`). Self-host and
    managed tenants therefore MUST NOT coexist on the same slug — a deployment is
    managed-only or self-host-only (the root pin/derivatives belong to one model).
    """
    target = pack_dir / "uploads" / target_filename
    pdf_hash = sources.hash_pdf(target)

    if owner_ref is not None:
        # Managé — pointeur per-owner (le hash y est stocké pour la résolution
        # owner→hash→cache partagé). Jamais de restore vers la racine.
        live_graph.write_owner_active(
            pack_dir, owner_ref, sources.SCHEMATIC_KIND, target_filename, pdf_hash
        )
        if sources.is_cached(pack_dir, pdf_hash):
            return "cached", None, None
        # Cache miss — la racine sert de scratch de build PARTAGÉ entre tenants ;
        # on sérialise le clear+copy+ingest per-slug (mirror de _RUNNING) pour que
        # write_through_cache(hash) ne snapshote que les artefacts de CE PDF.
        # Le clear+copy vit DANS la tâche chaînée (pas ici) — sinon un 2e appel
        # sync écraserait la racine avant que le 1er build ne l'ait lue.
        _schedule_managed_ingest(slug, pack_dir, target, pdf_hash)
        # On compte les pages depuis l'upload (la racine peut ne pas encore
        # contenir le PDF tant que la tâche chaînée n'a pas démarré).
        pages = _count_pdf_pages(target)
        eta = pages * _VISION_SECONDS_PER_PAGE if pages else None
        return "rebuilding", eta, pages

    # Self-host (owner None) — comportement racine inchangé.
    if sources.is_cached(pack_dir, pdf_hash):
        sources.restore_from_cache(pack_dir, pdf_hash)
        return "cached", None, None

    # Cache miss — drop stale derived files first (so detect helpers report
    # has_electrical_graph=False during build), then install the new PDF
    # in place and kick off the vision pipeline as a background task.
    sources.clear_in_place_schematic(pack_dir)
    shutil.copyfile(target, pack_dir / "schematic.pdf")
    asyncio.create_task(
        _reingest_and_cache(slug, pack_dir, pack_dir / "schematic.pdf", pdf_hash)
    )
    pages = _count_pdf_pages(pack_dir / "schematic.pdf")
    eta = pages * _VISION_SECONDS_PER_PAGE if pages else None
    return "rebuilding", eta, pages


@router.put(
    "/packs/{device_slug}/sources/{kind}",
    response_model=SwitchSourceResponse,
)
async def switch_pack_source(
    device_slug: str,
    kind: str,
    payload: SwitchSourceRequest,
    x_owner_ref: str | None = Header(default=None, alias="X-Owner-Ref"),
) -> SwitchSourceResponse:
    """Pin a different uploaded version as the active source for this kind.

    For `boardview` the switch is just a pin update — the new file takes
    effect at the next WS open (`SessionState.from_device`).
    For `schematic_pdf` the response distinguishes three statuses:
      - `cached`     : we found the target PDF's hash in the cache, copied
        the cached artefacts back into place; the new graph is live now.
      - `rebuilding` : the hash is unknown, the source PDF was copied to
        `memory/{slug}/schematic.pdf`, the in-place derived files were
        cleared, and a background ingest_schematic was kicked off. The
        Schematic / Electrical graph cards stay in `building` until done.
      - `pinned`     : (rare) pin updated but the target file is missing
        from disk; nothing else changed.
    """
    slug = _validate_slug(device_slug)
    if kind not in sources.KNOWN_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown kind={kind!r} — allowed: {list(sources.KNOWN_KINDS)}",
        )

    settings = _pkg.get_settings()
    pack_dir = Path(settings.memory_root) / slug
    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")

    target = pack_dir / "uploads" / payload.filename
    if not target.exists() or not target.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"upload {payload.filename!r} not found in {slug!r}/uploads",
        )
    # Defense in depth: filename must contain the kind marker.
    if f"-{kind}-" not in payload.filename:
        raise HTTPException(
            status_code=422,
            detail=f"filename does not match kind={kind!r}",
        )

    # Pin write. Managé (owner set) → per-owner pointer ; pour schematic le
    # hash est écrit par le helper, donc on n'écrit ici que le boardview
    # (pin-only). Self-host → pin racine global, inchangé.
    if x_owner_ref is None:
        pins = sources.read_active(pack_dir)
        pins[kind] = payload.filename
        sources.write_active(pack_dir, pins)

    if kind == sources.BOARDVIEW_KIND:
        if x_owner_ref is not None:
            live_graph.write_owner_active(
                pack_dir, x_owner_ref, sources.BOARDVIEW_KIND, payload.filename, None
            )
        return SwitchSourceResponse(
            device_slug=slug,
            kind=kind,
            active=payload.filename,
            status="pinned",
            detail="boardview pin updated; effective at next WS open.",
        )

    # schematic_pdf — delegate to the shared helper so the cache-or-reingest
    # logic stays consistent between explicit switch and auto-pin paths. In
    # managed mode the helper writes the per-owner pointer (with the hash).
    try:
        status, eta, pages = _apply_schematic_pin(slug, pack_dir, payload.filename, owner_ref=x_owner_ref)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not apply pin: {exc}") from exc

    if status == "cached":
        return SwitchSourceResponse(
            device_slug=slug,
            kind=kind,
            active=payload.filename,
            status="cached",
            detail="cache hit; electrical graph restored from cache.",
        )
    return SwitchSourceResponse(
        device_slug=slug,
        kind=kind,
        active=payload.filename,
        status="rebuilding",
        detail="cache miss; vision pipeline launched in background.",
        eta_seconds=eta,
        page_count=pages,
    )


@router.delete(
    "/packs/{device_slug}/sources/{kind}/versions/{filename}",
    response_model=DeleteSourceResponse,
)
async def delete_pack_source_version(
    device_slug: str,
    kind: str,
    filename: str,
    x_owner_ref: str | None = Header(default=None, alias="X-Owner-Ref"),
) -> DeleteSourceResponse:
    """Drop one uploaded version of a source. Auto-switches the pin on active deletes.

    Behaviour:
      - non-active version: file unlinked, pin unchanged → `deleted`.
      - active version with remaining versions: newest remaining becomes
        the new pin; same cache-or-reingest logic as PUT /sources/{kind}
        → `switched_cached` | `switched_rebuilding`.
      - active version with no remaining versions: pin cleared and (for
        schematic) the in-place schematic.pdf + derived files removed
        → `cleared`.
    """
    slug = _validate_slug(device_slug)
    if kind not in sources.KNOWN_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown kind={kind!r} — allowed: {list(sources.KNOWN_KINDS)}",
        )
    if f"-{kind}-" not in filename:
        raise HTTPException(
            status_code=422,
            detail=f"filename does not match kind={kind!r}",
        )

    settings = _pkg.get_settings()
    pack_dir = Path(settings.memory_root) / slug
    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")

    target = pack_dir / "uploads" / filename
    if not target.exists() or not target.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"upload {filename!r} not found in {slug!r}/uploads",
        )

    sidecar = pack_dir / "uploads" / f"{filename}.description.txt"
    try:
        target.unlink()
        if sidecar.exists():
            sidecar.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not delete upload: {exc}") from exc

    # Managé (owner set) — la suppression d'une version n'est pas le vecteur de
    # fuite (le clobber racine l'est) ; on garde simple : si le fichier supprimé
    # était le pin actif de CE tenant, on le retire de son pointeur per-owner.
    # Pas de réingestion automatique ni de pin racine partagé.
    if x_owner_ref is not None:
        owner_active = live_graph.read_owner_active(pack_dir, x_owner_ref)
        owner_entry = owner_active.get(kind) or {}
        if owner_entry.get("filename") == filename:
            live_graph.clear_owner_active(pack_dir, x_owner_ref, kind)
            return DeleteSourceResponse(
                device_slug=slug,
                kind=kind,
                deleted_filename=filename,
                new_active=None,
                status="cleared",
                detail="active version dropped; per-owner pin cleared.",
            )
        return DeleteSourceResponse(
            device_slug=slug,
            kind=kind,
            deleted_filename=filename,
            new_active=(owner_active.get(kind) or {}).get("filename"),
            status="deleted",
            detail="non-active version dropped; per-owner pin unchanged.",
        )

    # Self-host only — the managed path returned above, so the root pin read is
    # dead work for managed tenants (they have no shared root pin). Compute it
    # here so it runs solely on the self-host branch.
    pins = sources.read_active(pack_dir)
    was_active = pins.get(kind) == filename

    if not was_active:
        return DeleteSourceResponse(
            device_slug=slug,
            kind=kind,
            deleted_filename=filename,
            new_active=pins.get(kind),
            status="deleted",
            detail="non-active version dropped; pin unchanged.",
        )

    # Active was just deleted — pick newest remaining (list_uploads_for_kind
    # returns newest-first). None means no versions left for this kind.
    remaining = sources.list_uploads_for_kind(pack_dir, kind)
    if not remaining:
        # No replacement — clear the pin entirely. For schematic, also drop
        # in-place derivatives so detect helpers report has_*=False.
        pins.pop(kind, None)
        sources.write_active(pack_dir, pins)
        if kind == sources.SCHEMATIC_KIND:
            sources.clear_in_place_schematic(pack_dir)
            legacy_pdf = pack_dir / "schematic.pdf"
            if legacy_pdf.exists():
                try:
                    legacy_pdf.unlink()
                except OSError:
                    logger.warning(
                        "could not drop legacy schematic.pdf after clearing pin for %s",
                        slug,
                        exc_info=True,
                    )
        return DeleteSourceResponse(
            device_slug=slug,
            kind=kind,
            deleted_filename=filename,
            new_active=None,
            status="cleared",
            detail="active version dropped; no replacement available.",
        )

    new_active = remaining[0]["filename"]
    pins[kind] = new_active
    sources.write_active(pack_dir, pins)

    if kind == sources.BOARDVIEW_KIND:
        return DeleteSourceResponse(
            device_slug=slug,
            kind=kind,
            deleted_filename=filename,
            new_active=new_active,
            status="switched_cached",
            detail=f"active version dropped; pin moved to {new_active}.",
        )

    # schematic_pdf — reuse the shared cache-or-reingest helper.
    try:
        status, eta, pages = _apply_schematic_pin(slug, pack_dir, new_active)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not apply pin: {exc}") from exc

    if status == "cached":
        return DeleteSourceResponse(
            device_slug=slug,
            kind=kind,
            deleted_filename=filename,
            new_active=new_active,
            status="switched_cached",
            detail="active version dropped; replacement restored from cache.",
        )
    return DeleteSourceResponse(
        device_slug=slug,
        kind=kind,
        deleted_filename=filename,
        new_active=new_active,
        status="switched_rebuilding",
        detail="active version dropped; replacement vision pipeline launched.",
        eta_seconds=eta,
        page_count=pages,
    )


_BOARDVIEW_MEDIA_TYPES = {
    ".brd":       "application/octet-stream",
    ".brd2":      "application/octet-stream",
    ".kicad_pcb": "application/octet-stream",
    ".asc":       "text/plain",
    ".bdv":       "application/octet-stream",
    ".bv":        "application/octet-stream",
    ".cad":       "application/octet-stream",
    ".cst":       "application/octet-stream",
    ".f2b":       "application/octet-stream",
    ".fz":        "application/octet-stream",
    ".gr":        "application/octet-stream",
    ".tvw":       "application/octet-stream",
}


@router.api_route("/packs/{device_slug}/boardview", methods=["GET", "HEAD"])
async def get_pack_boardview(device_slug: str) -> FileResponse:
    """Serve the active boardview file for this device.

    Resolution chain (same as `_find_boardview`):
      1. `active_sources.json` pin -> `memory/{slug}/uploads/<pinned>`.
      2. `board_assets/{slug}.<ext>` (in-repo demo boards).
      3. `memory/{slug}/uploads/*-boardview-*` (alphabetical first).
    Returns 404 when none resolves. Served with the original filename in
    `Content-Disposition` so the frontend can preserve the extension when
    re-POSTing to `/api/board/parse` (extension drives parser dispatch).
    """
    settings = _pkg.get_settings()
    slug = _slugify(device_slug)
    pack_dir = Path(settings.memory_root) / slug
    path = _find_boardview(slug, pack_dir)
    if path is None:
        raise HTTPException(
            status_code=404,
            detail=f"No boardview on disk for device_slug={slug!r}",
        )
    media_type = _BOARDVIEW_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{path.name}"'},
    )


@router.api_route("/packs/{device_slug}/schematic.pdf", methods=["GET", "HEAD"])
async def get_pack_schematic_pdf(device_slug: str) -> FileResponse:
    """Serve the source schematic PDF for this device.

    Lookup order:
    1. `memory/{slug}/schematic.pdf` — persisted by `ingest_schematic`.
    2. `board_assets/{slug}.pdf` — fallback for devices whose schematic
       ships pre-rendered in the repo.
    Returns 404 when neither exists. Served as `application/pdf` with
    `Content-Disposition: inline` so the browser's native viewer handles
    pagination, zoom, and search.
    """
    settings = _pkg.get_settings()
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
