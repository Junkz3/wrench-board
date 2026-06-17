"""Orchestrator — full schematic ingestion for one device.

Renders the PDF page by page, extracts pdfplumber grounding, runs the Claude
vision pass in parallel with a cache-warmup sequence (page 1 first, then
`asyncio.gather` on the rest so the prompt-cache entry materialises before the
burst), merges the per-page graphs into a flat catalogue, compiles that into
an `ElectricalGraph`, and persists every artefact under `memory/{device_slug}/`.

Side-effect artefacts written:
- `schematic.pdf`                      — copy of the source PDF (for re-viewing)
- `schematic_pages/page-NN.png`        — rasterised page, shown by the web viewer
- `schematic_pages/page-NN.anchors.json` — refdes bboxes (for search highlight)
- `schematic_pages/page_XXX.json`      — one per page, raw vision output
- `schematic_graph.json`               — merged flat catalogue
- `electrical_graph.json`              — final interrogeable graph

Returns the `ElectricalGraph` for callers that want to act on it directly
without re-reading from disk.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from anthropic import AsyncAnthropic

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from api.config import get_settings
from api.pipeline.schematic.compiler import compile_electrical_graph
from api.pipeline.schematic.grounding import (
    extract_all_pages,
    extract_page_data,
    format_grounding_for_prompt,
)
from api.pipeline.schematic.merger import merge_pages
from api.pipeline.schematic.net_classifier import (
    apply_power_rail_classification,
    classify_nets,
)
from api.pipeline.schematic.page_vision import extract_page
from api.pipeline.schematic.passive_classifier import classify_passives
from api.pipeline.schematic.renderer import (
    SchematicPageLimitExceeded,
    ensure_renderable_pdf,
    probe_page_count,
    render_one_page,
    render_pages,
)
from api.pipeline.schematic.schemas import (
    ElectricalGraph,
    NetClassification,
    SchematicPageGraph,
)

# api.stock.parts_index is imported lazily inside _write_parts_index — a
# top-level import here triggers a circular load via api.pipeline.__init__
# (which re-exports ingest_schematic) → api.stock.__init__ → router →
# schemas → api.pipeline.schematic.schemas → us. Keep the import local.

logger = logging.getLogger("wrench_board.pipeline.schematic.orchestrator")


async def ingest_schematic(
    *,
    device_slug: str,
    pdf_path: Path,
    client: AsyncAnthropic,
    memory_root: Path | None = None,
    model: str | None = None,
    device_label: str | None = None,
    use_grounding: bool = True,
    cache_warmup_seconds: float | None = None,
    vision_concurrency: int | None = None,
    render_dpi: int = 200,
    on_event: Callable[[dict], Awaitable[None]] | None = None,
) -> ElectricalGraph:
    """Run the full ingestion pipeline for `pdf_path` and persist artefacts.

    Caller is responsible for providing a ready `AsyncAnthropic` client.
    `memory_root` defaults to the configured `memory` directory; callers may
    override for tests or alternate storage layouts.
    """
    settings = get_settings()
    model = model or settings.anthropic_model_main
    memory_root = memory_root or Path(settings.memory_root)
    warmup = (
        cache_warmup_seconds
        if cache_warmup_seconds is not None
        else settings.pipeline_cache_warmup_seconds
    )
    concurrency = (
        vision_concurrency
        if vision_concurrency is not None
        else settings.pipeline_vision_concurrency
    )
    device_label = device_label or pdf_path.stem

    pdf_path = Path(pdf_path).resolve()
    output_dir = Path(memory_root) / device_slug
    pages_dir = output_dir / "schematic_pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    persisted_pdf = output_dir / "schematic.pdf"
    try:
        if persisted_pdf.resolve() != pdf_path:
            shutil.copyfile(pdf_path, persisted_pdf)
    except OSError:
        logger.warning(
            "could not persist source PDF to %s", persisted_pdf, exc_info=True
        )

    # Some real-world schematics (XZZ library) carry non-standard objects that
    # pdfplumber/pdfminer can't parse → 0 pages probed → render 0 pages → an
    # EMPTY pack built on wasted Scout/writer tokens (observed live on an
    # iPhone 8 schematic). Repair via ghostscript up front (and FAIL loudly if
    # still unreadable). The repaired copy is used for BOTH render and grounding
    # below, which both read the PDF via pdfplumber/poppler.
    pdf_path = ensure_renderable_pdf(pdf_path, pages_dir)

    # Vision extraction. Two paths, same per-page cache + output:
    #   - batch (operator flag PIPELINE_VISION_BATCH): render + ground every
    #     page up front, send the uncached ones through the Message Batches API
    #     (-50% tokens, asynchronous), then load results from the per-page
    #     cache. Latency-insensitive; for offline rebuilds.
    #   - pipelined (default, synchronous): stream each page render → ground →
    #     vision so the ~330 s of pdftoppm + pdfplumber CPU overlaps the
    #     OTPM-bound vision wait instead of running as a barrier before it.
    common = {
        "pdf_path": pdf_path,
        "pages_dir": pages_dir,
        "render_dpi": render_dpi,
        "use_grounding": use_grounding,
        "client": client,
        "model": model,
        "device_label": device_label,
        "concurrency": concurrency,
        "warmup": warmup,
        "on_event": on_event,
    }
    if settings.pipeline_vision_batch:
        page_graphs = await _ingest_pages_bulk(**common)
    else:
        page_graphs = await _ingest_pages_pipelined(**common)

    schematic_graph = merge_pages(
        page_graphs,
        device_slug=device_slug,
        source_pdf=str(pdf_path),
    )
    (output_dir / "schematic_graph.json").write_text(
        schematic_graph.model_dump_json(indent=2)
    )
    logger.info(
        "merged: components=%d nets=%d edges=%d notes=%d ambiguities=%d",
        len(schematic_graph.components),
        len(schematic_graph.nets),
        len(schematic_graph.typed_edges),
        len(schematic_graph.designer_notes),
        len(schematic_graph.ambiguities),
    )

    page_confidences = {g.page: g.confidence for g in page_graphs}
    electrical = compile_electrical_graph(
        schematic_graph, page_confidences=page_confidences
    )
    (output_dir / "electrical_graph.json").write_text(
        electrical.model_dump_json(indent=2)
    )
    logger.info(
        "compiled: rails=%d boot_phases=%d degraded=%s global_conf=%.2f",
        len(electrical.power_rails),
        len(electrical.boot_sequence),
        electrical.quality.degraded_mode,
        electrical.quality.confidence_global,
    )

    # Three independent LLM post-passes, launched in parallel so their
    # wall-clock dominates (~45s instead of ~120s sequential):
    #   - boot analyzer  → refines boot_sequence from topology + notes
    #   - net classifier → tags every net with a functional domain
    #   - passive LLM    → fills the ~30% of passives the heuristic left role=None
    # Each step is isolated and graceful: on failure the compiler-only
    # artefacts remain valid and the pipeline completes.
    analyzer_results = await asyncio.gather(
        _run_boot_analyzer(electrical, client, output_dir),
        _run_net_classifier(electrical, client, output_dir),
        _run_passive_classifier_llm(electrical, client, output_dir),
        return_exceptions=False,
    )
    logger.info(
        "post-compile analyzers finished (boot=%s, nets=%s, passives=%s)",
        "ok" if analyzer_results[0] else "failed",
        "ok" if analyzer_results[1] else "failed",
        "ok" if analyzer_results[2] else "failed",
    )

    # If net_classifier succeeded, lift its `domain=power_rail` decisions
    # back into `SchematicGraph.nets[...].is_power` and re-compile. This
    # catches rails the vision pass missed (e.g. PVIN on MNT Reform) so
    # they show up in `electrical.power_rails` and unlock the downstream
    # cascades in hypothesize. We preserve any LLM-filled passive roles
    # across the re-compile.
    if analyzer_results[1]:
        electrical = _upgrade_rails_from_classification(
            electrical=electrical,
            schematic_graph=schematic_graph,
            page_confidences=page_confidences,
            output_dir=output_dir,
        )

    _write_parts_index(
        device_slug=device_slug, electrical=electrical, output_dir=output_dir
    )

    return electrical


# CPU prep (pdfplumber + pdftoppm) runs on worker threads under this cap so the
# event loop stays free for in-flight vision calls. 1 = strictly sequential CPU,
# safe on a low-vCPU VPS; the CPU stage is hidden under the vision wait anyway,
# so it never needs to be the throughput driver.
_RENDER_CONCURRENCY = 1


def _prepare_page(
    pdf_path: Path,
    page_number: int,
    total_pages: int,
    pages_dir: Path,
    render_dpi: int,
    use_grounding: bool,
) -> tuple[object, str | None]:
    """Render + ground a single page (the CPU unit of the pipeline).

    Returns ``(RenderedPage, grounding_text | None)``. Runs synchronously — the
    orchestrator dispatches it via ``asyncio.to_thread`` so the event loop keeps
    servicing in-flight vision calls while this page's pdftoppm + pdfplumber
    work happens. Persists the refdes-anchor JSON next to the PNG when grounding
    is enabled (same artefact the bulk path writes).
    """
    extract = extract_page_data(pdf_path, page_number, with_grounding=use_grounding)
    rendered = render_one_page(
        pdf_path,
        pages_dir,
        page_number,
        total_pages,
        dpi=render_dpi,
        width_pt=extract.width,
        height_pt=extract.height,
        char_count=extract.char_count,
        line_count=extract.line_count,
    )
    grounding_text: str | None = None
    g = extract.grounding
    if g is not None:
        grounding_text = format_grounding_for_prompt(g)
        anchors_payload = {
            "page": g.page,
            "page_width_pt": g.page_width,
            "page_height_pt": g.page_height,
            "anchors": [
                {"refdes": rd, "x0": x0, "top": top, "x1": x1, "bottom": bot}
                for (rd, x0, top, x1, bot) in g.refdes_anchors
            ],
        }
        (pages_dir / f"page-{page_number:02d}.anchors.json").write_text(
            json.dumps(anchors_payload, indent=2)
        )
        logger.info(
            "grounding page %d: refdes=%d nets=%d values=%d sheet=%s anchors=%d",
            page_number,
            len(g.refdes),
            len(g.net_labels),
            len(g.values),
            g.sheet_file,
            len(g.refdes_anchors),
        )
    return rendered, grounding_text


async def _ingest_pages_pipelined(
    *,
    pdf_path: Path,
    pages_dir: Path,
    render_dpi: int,
    use_grounding: bool,
    client: AsyncAnthropic,
    model: str,
    device_label: str | None,
    concurrency: int,
    warmup: float,  # noqa: ARG001 — page-1-first warmup supersedes the sleep
    on_event: Callable[[dict], Awaitable[None]] | None,
) -> list[SchematicPageGraph]:
    """Stream each page render → ground → vision so CPU overlaps the vision wait.

    Per page: render + ground on a thread (bounded by ``_RENDER_CONCURRENCY``),
    then a vision call (bounded by ``concurrency``). Page 1's vision completes
    before the rest start (the ``warm`` event) so pages 2..N read the shared
    system + tool prefix from cache rather than all racing to write it.
    """
    cap = get_settings().pipeline_schematic_max_pages
    total = probe_page_count(pdf_path)
    if total > cap:
        raise SchematicPageLimitExceeded(
            f"schematic has {total} pages, exceeds cap of {cap}"
        )
    if total == 0:
        raise RuntimeError(
            f"{pdf_path} probed to 0 pages — unrenderable even after repair"
        )
    logger.info(
        "pipelined schematic ingest: %d pages (vision concurrency=%d)",
        total,
        concurrency,
    )

    pages_done = 0

    async def _emit_page_done() -> None:
        nonlocal pages_done
        pages_done += 1
        if on_event is not None:
            await on_event({
                "type": "phase_step", "phase": "schematic_ingest", "step": "page",
                "index": pages_done, "total": total,
            })

    cpu_sem = asyncio.Semaphore(_RENDER_CONCURRENCY)
    vision_sem = asyncio.Semaphore(concurrency)
    warm = asyncio.Event()

    async def _process(idx: int) -> SchematicPageGraph:
        page_number = idx + 1
        try:
            cached_path = pages_dir / f"page_{page_number:03d}.json"
            if cached_path.exists():
                logger.info("vision skip page %d/%d (cached)", page_number, total)
                graph = SchematicPageGraph.model_validate_json(
                    cached_path.read_text()
                )
                await _emit_page_done()
                return graph
            async with cpu_sem:
                rendered, grounding_text = await asyncio.to_thread(
                    _prepare_page,
                    pdf_path,
                    page_number,
                    total,
                    pages_dir,
                    render_dpi,
                    use_grounding,
                )
            if idx != 0:
                await warm.wait()
            logger.info(
                "vision call page %d/%d (model=%s)", page_number, total, model
            )
            async with vision_sem:
                graph = await extract_page(
                    client=client,
                    model=model,
                    rendered=rendered,
                    total_pages=total,
                    device_label=device_label,
                    grounding=grounding_text,
                )
            cached_path.write_text(graph.model_dump_json(indent=2))
            await _emit_page_done()
            return graph
        finally:
            # Always release the warmup gate after page 1 resolves — on a cache
            # hit or an error too — so pages 2..N never deadlock waiting on it.
            if idx == 0:
                warm.set()

    return list(await asyncio.gather(*[_process(i) for i in range(total)]))


async def _ingest_pages_bulk(
    *,
    pdf_path: Path,
    pages_dir: Path,
    render_dpi: int,
    use_grounding: bool,
    client: AsyncAnthropic,
    model: str,
    device_label: str | None,
    concurrency: int,
    warmup: float,
    on_event: Callable[[dict], Awaitable[None]] | None,
) -> list[SchematicPageGraph]:
    """Render + ground every page up front, then vision via Message Batches.

    The batch path is latency-insensitive (async, up to ~1 h) but halves token
    cost, so it keeps the render-everything-first shape: the Batches API needs
    all PNGs + groundings together. Uncached pages go to the batch, results land
    in the per-page cache, and the bounded gather below loads them from disk
    (pages the batch could not produce fall back to a direct vision call).
    """
    grounding_texts: list[str | None]
    if use_grounding:
        extracts = extract_all_pages(pdf_path)
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
        logger.info("rendering %s → %s (dpi=%d)", pdf_path, pages_dir, render_dpi)
        rendered_pages = render_pages(
            pdf_path, pages_dir, dpi=render_dpi, metadata=render_meta
        )
        total = len(rendered_pages)
        logger.info("rendered %d pages", total)

        grounding_texts = [None] * total
        for i, e in enumerate(extracts):
            g = e.grounding
            grounding_texts[i] = format_grounding_for_prompt(g)
            anchors_payload = {
                "page": g.page,
                "page_width_pt": g.page_width,
                "page_height_pt": g.page_height,
                "anchors": [
                    {"refdes": rd, "x0": x0, "top": top, "x1": x1, "bottom": bot}
                    for (rd, x0, top, x1, bot) in g.refdes_anchors
                ],
            }
            (pages_dir / f"page-{g.page:02d}.anchors.json").write_text(
                json.dumps(anchors_payload, indent=2)
            )
    else:
        logger.info("rendering %s → %s (dpi=%d)", pdf_path, pages_dir, render_dpi)
        rendered_pages = render_pages(pdf_path, pages_dir, dpi=render_dpi)
        total = len(rendered_pages)
        logger.info("rendered %d pages", total)
        grounding_texts = [None] * total

    pages_done = 0

    async def _emit_page_done() -> None:
        nonlocal pages_done
        pages_done += 1
        if on_event is not None:
            await on_event({
                "type": "phase_step", "phase": "schematic_ingest", "step": "page",
                "index": pages_done, "total": total,
            })

    async def _one_page(idx: int) -> SchematicPageGraph:
        rp = rendered_pages[idx]
        cached_path = pages_dir / f"page_{rp.page_number:03d}.json"
        if cached_path.exists():
            logger.info(
                "vision skip page %d/%d (cached at %s)",
                rp.page_number,
                total,
                cached_path.name,
            )
            result = SchematicPageGraph.model_validate_json(cached_path.read_text())
            await _emit_page_done()
            return result
        logger.info(
            "vision call page %d/%d (model=%s)", rp.page_number, total, model
        )
        graph = await extract_page(
            client=client,
            model=model,
            rendered=rp,
            total_pages=total,
            device_label=device_label,
            grounding=grounding_texts[idx],
        )
        cached_path.write_text(graph.model_dump_json(indent=2))
        await _emit_page_done()
        return graph

    uncached_idx = [
        i
        for i in range(total)
        if not (pages_dir / f"page_{rendered_pages[i].page_number:03d}.json").exists()
    ]
    if uncached_idx:
        from api.pipeline.schematic import batch_vision

        logger.info(
            "vision batch mode: %d/%d page(s) uncached → Message Batches "
            "API (-50%% token price)",
            len(uncached_idx),
            total,
        )
        batch_graphs = await batch_vision.extract_pages_batch(
            client=client,
            model=model,
            pages=[rendered_pages[i] for i in uncached_idx],
            total_pages=total,
            device_label=device_label,
            groundings=[grounding_texts[i] for i in uncached_idx],
        )
        for page_number, graph in batch_graphs.items():
            (pages_dir / f"page_{page_number:03d}.json").write_text(
                graph.model_dump_json(indent=2)
            )
        missing = len(uncached_idx) - len(batch_graphs)
        if missing:
            logger.warning(
                "vision batch mode: %d page(s) failed in the batch — "
                "retrying via the direct path at full price",
                missing,
            )

    sem = asyncio.Semaphore(concurrency)

    async def _bounded(idx: int) -> SchematicPageGraph:
        async with sem:
            return await _one_page(idx)

    if total > 1:
        first = await _bounded(0)
        if warmup > 0:
            await asyncio.sleep(warmup)
        rest = await asyncio.gather(*[_bounded(i) for i in range(1, total)])
        return [first, *rest]
    return list(await asyncio.gather(*[_bounded(i) for i in range(total)]))


def _write_parts_index(
    *, device_slug: str, electrical: ElectricalGraph, output_dir: Path
) -> None:
    """Synthesize and persist memory/{slug}/parts_index.json.

    Reads sibling classification artefacts; tolerates them being absent
    (e.g. if their analyzer step failed earlier in this run).
    """
    # Local import — see top-of-file note on the circular dep.
    from api.stock.parts_index import build_parts_index

    passive_class = _safe_load_json(output_dir / "passive_classification_llm.json")
    nets_class = _safe_load_json(output_dir / "nets_classified.json")
    parts_index = build_parts_index(
        slug=device_slug,
        electrical_graph=electrical.model_dump(mode="json"),
        passive_classification=passive_class,
        nets_classified=nets_class,
    )
    (output_dir / "parts_index.json").write_text(
        parts_index.model_dump_json(indent=2)
    )
    logger.info(
        "[parts_index] wrote memory/%s/parts_index.json (%d entries)",
        device_slug,
        len(parts_index.entries),
    )


def _safe_load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _upgrade_rails_from_classification(
    *,
    electrical: ElectricalGraph,
    schematic_graph,
    page_confidences: dict[int, float],
    output_dir: Path,
) -> ElectricalGraph:
    """Apply net_classifier's power_rail decisions and re-compile.

    Returns the (possibly re-compiled) electrical graph. On any failure
    returns the input graph unchanged — the pipeline never regresses on
    a successful initial compile.
    """
    try:
        classification = NetClassification.model_validate_json(
            (output_dir / "nets_classified.json").read_text()
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "could not read nets_classified.json for re-compile",
            exc_info=True,
        )
        return electrical

    promoted = apply_power_rail_classification(schematic_graph, classification)
    if not promoted:
        return electrical

    # Snapshot every non-IC role (heuristic + LLM). `compile_electrical_graph`
    # re-runs the heuristic passive classifier from scratch, and its rules
    # are topology-sensitive — promoting nets can shift a pass-FET's rule
    # path from "load_switch" (exactly 2 rails + 1 non-rail) to no match
    # when the non-rail becomes a rail. Restoring the pre-promotion roles
    # guards against that regression.
    preserved_roles = {
        refdes: (comp.kind, comp.role)
        for refdes, comp in electrical.components.items()
        if comp.kind != "ic" and comp.role is not None
    }

    rails_before = len(electrical.power_rails)
    try:
        recompiled = compile_electrical_graph(
            schematic_graph, page_confidences=page_confidences
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "re-compile after net_classifier failed — keeping initial graph",
            exc_info=True,
        )
        return electrical

    enriched = dict(recompiled.components)
    for refdes, (kind, role) in preserved_roles.items():
        node = enriched.get(refdes)
        if node is None or role is None:
            continue
        if node.role is None:
            enriched[refdes] = node.model_copy(update={"kind": kind, "role": role})
    recompiled.__dict__["components"] = enriched

    (output_dir / "electrical_graph.json").write_text(
        recompiled.model_dump_json(indent=2)
    )
    logger.info(
        "re-compiled after net_classifier: rails=%d (+%d) promoted=%s",
        len(recompiled.power_rails),
        len(recompiled.power_rails) - rails_before,
        ",".join(sorted(promoted)),
    )
    return recompiled


async def _run_boot_analyzer(electrical, client, output_dir: Path) -> bool:
    """Run the Opus boot analyzer and persist. Returns True on success."""
    try:
        from api.pipeline.schematic.boot_analyzer import (
            analyze_boot_sequence,  # lazy: module is optional WIP on evolve
        )
        analyzed = await analyze_boot_sequence(electrical, client=client)
        (output_dir / "boot_sequence_analyzed.json").write_text(
            analyzed.model_dump_json(indent=2)
        )
        logger.info(
            "boot analyzer persisted (phases=%d sequencer=%s conf=%.2f)",
            len(analyzed.phases), analyzed.sequencer_refdes or "none",
            analyzed.global_confidence,
        )
        return True
    except Exception:  # noqa: BLE001
        logger.warning(
            "boot analyzer failed, proceeding with compiler-only boot_sequence",
            exc_info=True,
        )
        return False


async def _run_net_classifier(electrical, client, output_dir: Path) -> bool:
    """Run the Opus net classifier and persist. Returns True on success."""
    try:
        classification = await classify_nets(electrical, client=client)
        (output_dir / "nets_classified.json").write_text(
            classification.model_dump_json(indent=2)
        )
        logger.info(
            "net classifier persisted (nets=%d domains=%d model=%s)",
            len(classification.nets), len(classification.domain_summary),
            classification.model_used,
        )
        return True
    except Exception:  # noqa: BLE001
        logger.warning(
            "net classifier failed — rail/signal categorisation will be unavailable",
            exc_info=True,
        )
        return False


async def _run_passive_classifier_llm(
    electrical, client, output_dir: Path,
) -> bool:
    """Run the Opus passive role classifier and apply the LLM-filled roles.

    The heuristic classifier already ran inside `compile_electrical_graph`
    and wrote baseline kind/role onto every passive it could classify
    deterministically (~70% coverage on MNT Reform). This step fills the
    remaining passives whose role was null.

    Strategy:
      1. Call `classify_passives(electrical, client)` — returns merged
         heuristic + LLM output.
      2. Identify the refdes where the role changed from None to a value
         (the LLM fills).
      3. Apply the changes onto `electrical.components` in-memory.
      4. Persist a separate audit file `passive_classification_llm.json`.
      5. Re-write `electrical_graph.json` so the hypothesize engine sees
         the enriched roles on disk.

    Returns True on success, False on exception.
    """
    try:
        # Snapshot the pre-LLM heuristic role assignments so we can
        # detect fills vs. overrides (the merge logic in classify_passives
        # never overrides; this is a safety check).
        before = {
            refdes: (comp.kind, comp.role)
            for refdes, comp in electrical.components.items()
            if comp.kind != "ic"
        }

        merged = await classify_passives(electrical, client=client)

        # Apply fills — only where heuristic was None and LLM provided a role.
        filled = 0
        enriched = dict(electrical.components)
        for refdes, (kind, role, _conf) in merged.items():
            prev = before.get(refdes)
            if prev is None:
                continue
            prev_kind, prev_role = prev
            if prev_role is not None:
                continue  # heuristic already had it
            if role is None:
                continue  # LLM also gave up
            node = enriched.get(refdes)
            if node is None:
                continue
            enriched[refdes] = node.model_copy(update={"kind": kind, "role": role})
            filled += 1

        # Rebuild the electrical graph with the enriched components. Use
        # model_copy so the rest of the schema (rails, nets, boot_sequence,
        # quality) is untouched.
        if filled > 0:
            electrical_updated = electrical.model_copy(update={"components": enriched})
            # Mutate the caller's object — orchestrator uses the returned
            # `electrical` value and we want the in-memory view consistent
            # with the on-disk JSON.
            electrical.__dict__["components"] = enriched
            (output_dir / "electrical_graph.json").write_text(
                electrical_updated.model_dump_json(indent=2)
            )

        # Audit file — full merged assignments for debugging.
        (output_dir / "passive_classification_llm.json").write_text(
            json.dumps(
                {
                    "device_slug": electrical.device_slug,
                    "assignments": [
                        {
                            "refdes": refdes,
                            "kind": kind,
                            "role": role,
                            "confidence": conf,
                        }
                        for refdes, (kind, role, conf) in merged.items()
                    ],
                    "filled_by_llm": filled,
                    "unclassified": sum(1 for _, r, _ in merged.values() if r is None),
                },
                indent=2,
            )
        )
        logger.info(
            "passive classifier persisted (llm_filled=%d, total_passives=%d)",
            filled, len(merged),
        )
        return True
    except Exception:  # noqa: BLE001
        logger.warning(
            "passive LLM classifier failed — heuristic roles remain on disk",
            exc_info=True,
        )
        return False
