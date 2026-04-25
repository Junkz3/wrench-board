# SPDX-License-Identifier: Apache-2.0
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

from anthropic import AsyncAnthropic

from api.config import get_settings
from api.pipeline.schematic.compiler import compile_electrical_graph
from api.pipeline.schematic.grounding import (
    extract_grounding,
    format_grounding_for_prompt,
)
from api.pipeline.schematic.merger import merge_pages
from api.pipeline.schematic.net_classifier import (
    apply_power_rail_classification,
    classify_nets,
)
from api.pipeline.schematic.page_vision import extract_page
from api.pipeline.schematic.passive_classifier import classify_passives
from api.pipeline.schematic.renderer import render_pages
from api.pipeline.schematic.schemas import (
    ElectricalGraph,
    NetClassification,
    SchematicPageGraph,
)

logger = logging.getLogger("microsolder.pipeline.schematic.orchestrator")


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
    render_dpi: int = 200,
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

    # Rasterise directly into the persistent pages dir — the PNGs are
    # durable artefacts consumed by the web PDF viewer, not just vision
    # input. Poppler's `pdftoppm` is idempotent on the same path (overwrites
    # on re-ingest) so this doubles as a cache.
    logger.info("rendering %s → %s (dpi=%d)", pdf_path, pages_dir, render_dpi)
    rendered_pages = render_pages(pdf_path, pages_dir, dpi=render_dpi)
    total = len(rendered_pages)
    logger.info("rendered %d pages", total)

    grounding_texts: list[str | None] = [None] * total
    if use_grounding:
        for i, page in enumerate(rendered_pages):
            g = extract_grounding(pdf_path, page.page_number)
            grounding_texts[i] = format_grounding_for_prompt(g)
            # Persist the refdes anchors next to the PNG so the web viewer
            # can overlay highlight rectangles when the user searches.
            anchors_payload = {
                "page": g.page,
                "page_width_pt": g.page_width,
                "page_height_pt": g.page_height,
                "anchors": [
                    {"refdes": rd, "x0": x0, "top": top, "x1": x1, "bottom": bot}
                    for (rd, x0, top, x1, bot) in g.refdes_anchors
                ],
            }
            (pages_dir / f"page-{page.page_number:02d}.anchors.json").write_text(
                json.dumps(anchors_payload, indent=2)
            )
            logger.info(
                "grounding page %d: refdes=%d nets=%d values=%d sheet=%s anchors=%d",
                page.page_number,
                len(g.refdes),
                len(g.net_labels),
                len(g.values),
                g.sheet_file,
                len(g.refdes_anchors),
            )

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
            return SchematicPageGraph.model_validate_json(cached_path.read_text())
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
        return graph

    # Fan every page out immediately. The earlier pattern serialised page
    # 1 so its `cache_write` would land before the rest arrived, but with
    # explicit `cache_control` breakpoints on the system prompt + tool
    # schema that dance buys nothing — Anthropic's cache key is the
    # prefix, not the order of arrival, and the ephemeral entry persists
    # for the ~minute-long burst. Parallel from t=0 cuts wall-time ~2×.
    # `cache_warmup_seconds` is retained on the signature for callers
    # that still want a warmup (default 0 = no wait).
    if warmup > 0 and total > 1:
        await asyncio.sleep(warmup)
    page_graphs = await asyncio.gather(
        *[_one_page(i) for i in range(total)]
    )

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

    return electrical


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
        from api.pipeline.schematic.boot_analyzer import analyze_boot_sequence  # lazy: module is optional WIP on evolve
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
