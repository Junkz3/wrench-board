"""Schematic-domain endpoints — ingestion, pages, electrical graph,
boot sequence, passives, simulate, hypothesize.

Two clusters live here:
  1. PDF lifecycle  : POST /ingest-schematic, GET .../schematic.pages,
     GET /pages/{n}.png, GET /schematic (graph + analyzer/classifier
     overlays), GET /schematic/boot, GET /schematic/passives, plus the
     re-runnable Opus passes POST /analyze-boot and /classify-nets.
  2. Deterministic engines : POST /schematic/simulate, /hypothesize —
     drive `SimulationEngine` and `mb_hypothesize` over the compiled
     `ElectricalGraph`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from anthropic import AsyncAnthropic
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import ValidationError

import api.pipeline as _pkg  # noqa: PLC0415 — module-attribute lookups for patchability
from api.pipeline import live_graph
from api.pipeline.models import (
    HypothesizeRequest,
    IngestSchematicRequest,
    IngestSchematicResponse,
    SimulateRequest,
)
from api.pipeline.orchestrator import _slugify
from api.pipeline.routes._helpers import _validate_slug
from api.pipeline.routes.packs import _read_optional_json
from api.pipeline.schematic.grounding import extract_all_pages
from api.pipeline.schematic.renderer import render_pages
from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph
from api.pipeline.schematic.simulator import SimulationEngine
from api.tools.hypothesize import mb_hypothesize as _mb_hypothesize_tool

logger = logging.getLogger("wrench_board.pipeline.api")

router = APIRouter()


# ======================================================================
# Schematic ingestion — fire-and-forget PDF → ElectricalGraph
# ======================================================================


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
    _s = _pkg.get_settings()
    client = AsyncAnthropic(api_key=_s.anthropic_api_key, max_retries=_s.anthropic_max_retries)
    try:
        await _pkg.ingest_schematic(
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
    except Exception:  # noqa: BLE001 — fire-and-forget bg task; 202 already returned, just log
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


# ======================================================================
# Page rasterisation — lazy PNG render + per-page anchors
# ======================================================================


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
    # Single pdfplumber pass: scan metadata for render + grounding anchors.
    # pdftoppm then re-reads the PDF once for rasterisation; nothing parses it
    # per page anymore (the old loop opened it once per page just for anchors).
    # Anchors stay best-effort: if the grounding pass fails wholesale we still
    # rasterise (render_pages probes internally) and skip the overlay JSONs.
    try:
        extracts = extract_all_pages(pdf_path)
    except Exception:  # noqa: BLE001 — anchors are a non-critical search overlay
        logger.exception(
            "grounding pass failed on %s — rendering without anchors", pdf_path
        )
        render_pages(pdf_path, pages_dir, dpi=dpi)
        return
    render_meta = [
        {
            "page": e.page,
            "width": e.width,
            "height": e.height,
            "char_count": e.char_count,
            "line_count": e.line_count,
        }
        for e in extracts
    ]
    render_pages(pdf_path, pages_dir, dpi=dpi, metadata=render_meta)
    for e in extracts:
        g = e.grounding
        payload = {
            "page": g.page,
            "page_width_pt": g.page_width,
            "page_height_pt": g.page_height,
            "anchors": [
                {"refdes": rd, "x0": x0, "top": top, "x1": x1, "bottom": bot}
                for (rd, x0, top, x1, bot) in g.refdes_anchors
            ],
        }
        (pages_dir / f"page-{g.page:02d}.anchors.json").write_text(
            json.dumps(payload, indent=2)
        )


async def _ensure_pages_rendered(
    slug: str, memory_root: Path, owner_ref: str | None = None
) -> Path | None:
    """Lazy-render PNGs + anchors for a slug if they aren't on disk yet.

    Returns the pages directory on success, None when no source PDF can be
    found. The rasterisation is pushed to a thread so the event loop isn't
    blocked while `pdftoppm` runs (~1s/page at 150 DPI).

    T9 — per-owner: when `owner_ref` is set, the pages resolve to the tenant's
    active PDF cache (.cache_schematic/{hash}/schematic_pages/) keyed off the
    owner pin; the source PDF and rendered PNGs both live in that cache base.
    A managed tenant with no active schematic returns None → 404. owner None
    (self-host) keeps the historical slug-root behaviour, byte-identical.
    """
    pack_dir = memory_root / slug
    base = live_graph.resolve_cache_dir(pack_dir, owner_ref)
    if base is None:
        # Managed tenant with no active schematic pin → no pages.
        return None
    pages_dir = base / "schematic_pages"
    if _list_page_pngs(pages_dir):
        return pages_dir
    # Source PDF: the cached schematic.pdf for a managed owner, else the
    # legacy slug-root / board_assets lookup for self-host.
    if owner_ref is None:
        pdf_path = _find_schematic_pdf(slug, memory_root)
    else:
        cached_pdf = base / "schematic.pdf"
        pdf_path = cached_pdf if cached_pdf.is_file() else None
    if pdf_path is None:
        return None
    logger.info("[API] lazy-rendering schematic pages for slug=%s owner=%s", slug, owner_ref)
    await asyncio.to_thread(_render_and_extract_pages, pdf_path, pages_dir)
    return pages_dir


@router.get("/packs/{device_slug}/schematic/pages")
async def get_pack_schematic_pages(
    device_slug: str,
    x_owner_ref: str | None = Header(default=None, alias="X-Owner-Ref"),
) -> dict:
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
    settings = _pkg.get_settings()
    slug = _slugify(device_slug)
    memory_root = Path(settings.memory_root)
    pages_dir = await _ensure_pages_rendered(slug, memory_root, x_owner_ref)
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
async def get_pack_schematic_page_png(
    device_slug: str,
    page_n: int,
    x_owner_ref: str | None = Header(default=None, alias="X-Owner-Ref"),
) -> FileResponse:
    """Serve one rasterised page as PNG.

    `page_n` is the 1-based page number; filename on disk is zero-padded to
    match pdftoppm's output (`page-01.png`). Lazy-renders the full pack if
    the PNGs aren't on disk yet. T9 — per-owner via X-Owner-Ref.
    """
    settings = _pkg.get_settings()
    slug = _slugify(device_slug)
    memory_root = Path(settings.memory_root)
    pages_dir = await _ensure_pages_rendered(slug, memory_root, x_owner_ref)
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
async def get_pack_schematic(
    device_slug: str,
    x_owner_ref: str | None = Header(default=None, alias="X-Owner-Ref"),
) -> dict:
    """Return the compiled electrical graph for this device.

    404 when either the pack directory or `electrical_graph.json` is missing.
    The payload matches `api.pipeline.schematic.schemas.ElectricalGraph` —
    consumed by the Memory Bank UI for the D3 rail / boot-phase view.

    When `boot_sequence_analyzed.json` exists (Opus post-pass), we merge it
    into the payload under key `analyzed_boot_sequence` and also surface a
    `boot_sequence_source` flag (`"analyzer"` or `"compiler"`) so the UI can
    badge the timeline appropriately.

    T9 — per-owner: the cloud proxy injects `X-Owner-Ref` (= tenant_id); the
    graph + its overlays resolve to the tenant's active PDF cache. Absent
    header (self-host) → the slug root, unchanged.
    """
    settings = _pkg.get_settings()
    slug = _slugify(device_slug)
    pack_dir = Path(settings.memory_root) / slug
    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")
    base = live_graph.resolve_graph_dir(pack_dir, x_owner_ref)   # graphe = moat partagé (fallback canonique)
    graph_path = base / "electrical_graph.json" if base is not None else None
    if graph_path is None or not graph_path.exists():
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
    analyzed_path = base / "boot_sequence_analyzed.json"
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
    classified_path = base / "nets_classified.json"
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
    except (OSError, ValidationError):
        logger.exception("[API] analyze-boot: failed to load electrical_graph for %s", device_slug)
        return
    _s = _pkg.get_settings()
    client = AsyncAnthropic(api_key=_s.anthropic_api_key, max_retries=_s.anthropic_max_retries)
    try:
        from api.pipeline.schematic.boot_analyzer import (
            analyze_boot_sequence,  # lazy: module is optional WIP on evolve
        )
        analyzed = await analyze_boot_sequence(graph, client=client)
        (pack_dir / "boot_sequence_analyzed.json").write_text(analyzed.model_dump_json(indent=2))
        logger.info(
            "[API] analyze-boot finished for %s in %.1fs (phases=%d conf=%.2f)",
            device_slug,
            time.monotonic() - t0,
            len(analyzed.phases),
            analyzed.global_confidence,
        )
    except Exception:  # noqa: BLE001 — fire-and-forget bg task; 202 already returned, just log
        logger.exception("[API] analyze-boot failed for %s", device_slug)


@router.post("/packs/{device_slug}/schematic/analyze-boot", status_code=202)
async def post_analyze_boot(device_slug: str) -> dict:
    """Kick off an Opus boot-sequence analysis in the background.

    Re-runnable independently of the full schematic ingestion — useful when
    the prompt is improved or a newer model is available. Returns 202 with
    `{device_slug, started}`; the client polls `GET /packs/{slug}/schematic`
    to observe `boot_sequence_source` flipping from `compiler` to `analyzer`.
    """
    settings = _pkg.get_settings()
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
    except (OSError, ValidationError):
        logger.exception("[API] classify-nets: failed to load electrical_graph for %s", device_slug)
        return
    _s = _pkg.get_settings()
    client = AsyncAnthropic(api_key=_s.anthropic_api_key, max_retries=_s.anthropic_max_retries)
    try:
        classification = await _pkg.classify_nets(graph, client=client)
        (pack_dir / "nets_classified.json").write_text(classification.model_dump_json(indent=2))
        logger.info(
            "[API] classify-nets finished for %s in %.1fs (nets=%d model=%s)",
            device_slug,
            time.monotonic() - t0,
            len(classification.nets),
            classification.model_used,
        )
    except Exception:  # noqa: BLE001 — fire-and-forget bg task; 202 already returned, just log
        logger.exception("[API] classify-nets failed for %s", device_slug)


@router.post("/packs/{device_slug}/schematic/classify-nets", status_code=202)
async def post_classify_nets(device_slug: str) -> dict:
    """Kick off an Opus net classification in the background.

    Re-runnable independently — useful when the prompt improves or a new
    model drops. Returns 202; client polls `GET /packs/{slug}/schematic`
    and sees `net_domains_source` flip from 'regex' to 'opus'.
    """
    settings = _pkg.get_settings()
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
async def get_pack_schematic_boot(
    device_slug: str,
    x_owner_ref: str | None = Header(default=None, alias="X-Owner-Ref"),
) -> dict:
    """Return just the boot sequence + power rails — the "light" subset.

    The full `electrical_graph.json` can reach several hundred KB on real
    boards (449 components, ~2k pins on MNT Reform). For the initial boot
    timeline view the UI only needs rails and phases, so this route strips
    the heavy `components` / `nets` / `typed_edges` arrays server-side.

    T9 — per-owner: resolves to the tenant's active PDF graph via X-Owner-Ref.
    """
    settings = _pkg.get_settings()
    slug = _slugify(device_slug)
    graph_path = live_graph.resolve_graph_path(Path(settings.memory_root) / slug, x_owner_ref)
    if graph_path is None or not graph_path.exists():
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
async def get_schematic_passives(
    device_slug: str,
    x_owner_ref: str | None = Header(default=None, alias="X-Owner-Ref"),
) -> list[dict]:
    """Return classifier output per passive refdes (kind, role, confidence, source).

    Filters ICs out — only R/C/D/FB emitted. Used for debugging the passive
    classifier and for hand-written fixture generators to look up candidate
    refdes without deserializing the entire electrical_graph.json.

    T9 — per-owner: resolves to the tenant's active PDF graph via X-Owner-Ref.
    """
    settings = _pkg.get_settings()
    slug = _slugify(device_slug)
    graph_path = live_graph.resolve_graph_path(Path(settings.memory_root) / slug, x_owner_ref)
    if graph_path is None or not graph_path.exists():
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
async def post_simulate(
    device_slug: str,
    request: SimulateRequest,
    x_owner_ref: str | None = Header(default=None, alias="X-Owner-Ref"),
) -> dict:
    """Run the behavioral simulator on the compiled electrical graph.

    Accepts killed_refdes (sugar), explicit failures (causes), and
    rail_overrides (observations). Synchronous (< 10 ms on MNT-class
    boards). HTTP context is stateless — no probe_route enrichment
    here; clients that need a route go through the agent WS path.

    T9 — per-owner: resolves to the tenant's active PDF graph via X-Owner-Ref.
    """
    settings = _pkg.get_settings()
    slug = _slugify(device_slug)
    base = live_graph.resolve_graph_dir(Path(settings.memory_root) / slug, x_owner_ref)   # graphe = moat partagé (fallback canonique)
    graph_path = base / "electrical_graph.json" if base is not None else None
    if graph_path is None or not graph_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No schematic ingested yet for device_slug={slug!r}",
        )

    try:
        electrical = ElectricalGraph.model_validate_json(graph_path.read_text())
    except (OSError, ValidationError) as exc:
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
    ab_path = base / "boot_sequence_analyzed.json"
    if ab_path.exists():
        try:
            analyzed = AnalyzedBootSequence.model_validate_json(ab_path.read_text())
        except (OSError, ValidationError):
            analyzed = None

    tl = SimulationEngine(
        electrical,
        analyzed_boot=analyzed,
        killed_refdes=list(request.killed_refdes),
        failures=list(request.failures),
        rail_overrides=list(request.rail_overrides),
    ).run()
    return tl.model_dump()


@router.post("/packs/{device_slug}/schematic/hypothesize")
async def post_hypothesize(
    device_slug: str,
    request: HypothesizeRequest,
    x_owner_ref: str | None = Header(default=None, alias="X-Owner-Ref"),
) -> dict:
    """Rank candidate refdes-kills that explain the tech's observations.

    Same contract as mb_hypothesize tool. 400 on unknown refdes / rail,
    404 when no electrical_graph is on disk.

    T9 — per-owner: the cloud proxy injects `X-Owner-Ref` (= tenant_id); the
    electrical graph is resolved to that tenant's active PDF (owner None →
    slug root, self-host). The measurement journal stays at the slug root.
    """
    settings = _pkg.get_settings()
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
        owner_ref=x_owner_ref,
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
