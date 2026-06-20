"""Pipeline orchestrator — full Phase 1 → 2 → 3 → 4 chain (+ revise loop).

Persists all intermediate artefacts under `memory/{device_slug}/` on disk:
    raw_research_dump.md
    registry.json
    knowledge_graph.json
    rules.json
    dictionary.json
    audit_verdict.json
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic

from api.agent.memory_seed import seed_memory_store_from_pack
from api.config import get_settings
from api.pipeline import build_state, device_kind
from api.pipeline.auditor import run_auditor
from api.pipeline.build_metering import report_build_phases
from api.pipeline.device_kind import KindResolution
from api.pipeline.device_registry import get_device_registry_store, register_from_registry
from api.pipeline.drift import compute_drift
from api.pipeline.graph_truth import (
    GraphTruth,
    build_ground_truth_report,
    enrich_registry_from_graph,
    extract_mentions,
)
from api.pipeline.mapper import run_mapper
from api.pipeline.pack_lint import LintFinding, lint_pack
from api.pipeline.qa import graph_coverage
from api.pipeline.reconcile import (
    find_registry_fictions,
    load_seen_refdes,
    prune_contradicted_edges,
)
from api.pipeline.registry import run_registry_builder
from api.pipeline.schemas import (
    AuditVerdict,
    Dictionary,
    KnowledgeGraph,
    PipelineResult,
    RefdesMappings,
    Registry,
    RulesSet,
)
from api.pipeline.schematic.schemas import ElectricalGraph
from api.pipeline.scout import run_scout
from api.pipeline.telemetry.token_stats import PhaseTokenStats, write_token_stats
from api.pipeline.writers import run_single_writer_revision, run_writers_parallel

logger = logging.getLogger("wrench_board.pipeline.orchestrator")


# When the caller signals a schematic is being ingested out-of-band (via the
# /packs/{slug}/documents endpoint, not the repair-create body), the pipeline
# waits up to this long for `electrical_graph.json` to land before Phase 1.5,
# so device-kind classification + the Mapper run grounded. On timeout it
# degrades to a blind run (the pack still builds; the graph surfaces later).
_SCHEMATIC_WAIT_TIMEOUT_S = 300.0
_SCHEMATIC_WAIT_POLL_S = 2.0


# Upload kinds the orchestrator recognises in `memory/{slug}/uploads/`.
# Filenames follow `{ISO-timestamp}-{kind}-{original-filename}`. Anything
# whose filename doesn't match this pattern is left in `other` and not
# threaded into the prompts.
_UPLOAD_KINDS = {"schematic_pdf", "boardview", "datasheet", "notes", "other"}
_UPLOAD_NAME_RE = re.compile(
    r"^(?P<ts>[^-]+(?:-[^-]+)*?)-(?P<kind>[a-z_]+)-(?P<filename>.+)$"
)


@dataclass(frozen=True)
class UploadedDocuments:
    """Grouped technician uploads found under `memory/{slug}/uploads/`.

    Schematic and boardview slots are most-recent-wins (keyed off the
    timestamp prefix); datasheets, notes, and other accumulate.
    """

    schematic_pdf: Path | None = None
    boardview: Path | None = None
    datasheets: list[Path] = field(default_factory=list)
    notes: list[Path] = field(default_factory=list)
    other: list[Path] = field(default_factory=list)

    def is_empty(self) -> bool:
        return (
            self.schematic_pdf is None
            and self.boardview is None
            and not self.datasheets
            and not self.notes
            and not self.other
        )


def scan_uploads(uploads_dir: Path) -> UploadedDocuments:
    """List the files under `uploads_dir` and group them by kind.

    Empty / missing directories return an empty `UploadedDocuments`.
    Filenames that don't match `{ts}-{kind}-{name}` land in `other`,
    so the technician's manually-dropped files are not silently lost.
    """
    if not uploads_dir.exists() or not uploads_dir.is_dir():
        return UploadedDocuments()

    schematic_pdf: Path | None = None
    schematic_pdf_ts: str | None = None
    boardview: Path | None = None
    boardview_ts: str | None = None
    datasheets: list[Path] = []
    notes: list[Path] = []
    other: list[Path] = []

    for path in sorted(uploads_dir.iterdir()):
        if not path.is_file():
            continue
        match = _UPLOAD_NAME_RE.match(path.name)
        if match is None or match.group("kind") not in _UPLOAD_KINDS:
            other.append(path)
            continue
        kind = match.group("kind")
        ts = match.group("ts")
        if kind == "schematic_pdf":
            if schematic_pdf_ts is None or ts > schematic_pdf_ts:
                schematic_pdf = path
                schematic_pdf_ts = ts
        elif kind == "boardview":
            if boardview_ts is None or ts > boardview_ts:
                boardview = path
                boardview_ts = ts
        elif kind == "datasheet":
            datasheets.append(path)
        elif kind == "notes":
            notes.append(path)
        else:  # "other"
            other.append(path)

    return UploadedDocuments(
        schematic_pdf=schematic_pdf,
        boardview=boardview,
        datasheets=datasheets,
        notes=notes,
        other=other,
    )


def _stage_if_private(
    memory_root: Path,
    pack_dir: Path,
    slug: str,
    owner_ref: str | None,
    coverage_verdict: str | None = None,
) -> bool:
    """Lot 2 + Lot 3 — after a build completes, decide SHARED vs per-owner PRIVATE.

    A build is kept PRIVATE (relocated to `_staged/{owner_ref}/`) in a managed
    context (`owner_ref` set) when EITHER:
      - it is web-only (no electrical graph was produced = no schematic), OR
      - the graph↔boardview QA gate returned FAIL (Lot 3) — an incomplete source
        PDF must not become the authoritative shared pack.

    Private packs are seen only by the requesting tenant
    (`load_effective_pack(owner_ref)`); the slug stays free for a future clean
    build and other tenants never get served the unverified pack. A
    schematic-backed build that PASSes/WARNs, or self-host (owner_ref None),
    stays at the root → migrated to the shared `baseline/` as before.

    Returns True when the pack was staged private (caller then SKIPS the shared
    MA device-store seed — that mount is cross-tenant).
    """
    if owner_ref is None:
        return False  # self-host: always shared, byte-identical legacy behaviour
    web_only = not (pack_dir / "electrical_graph.json").exists()
    coverage_failed = coverage_verdict == "FAIL"
    if not (web_only or coverage_failed):
        return False
    from api.pipeline.pack_migrate import stage_web_only_pack

    stage_web_only_pack(memory_root, slug, owner_ref=owner_ref)
    logger.info(
        "[Pipeline] managed build for slug=%r staged PRIVATE to owner=%s "
        "(reason=%s, commons untouched)",
        slug,
        owner_ref,
        "web_only" if web_only else "coverage_fail",
    )
    return True


def _load_existing_electrical_graph(pack_dir: Path) -> ElectricalGraph | None:
    """Load `electrical_graph.json` if present and parseable. None otherwise."""
    path = pack_dir / "electrical_graph.json"
    if not path.exists():
        return None
    try:
        return ElectricalGraph.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — corrupted artefact must not abort
        logger.exception(
            "[Pipeline] electrical_graph.json at %s is malformed; "
            "continuing without graph for Scout/Registry",
            path,
        )
        return None

OnEvent = Callable[[dict[str, Any]], Awaitable[None]]


async def _noop_on_event(_event: dict[str, Any]) -> None:
    """Default on_event callback — swallow the event."""


def _slugify(label: str) -> str:
    """Turn a device label into a safe directory slug."""
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", label.strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "unknown-device"


def _get_client() -> AsyncAnthropic:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and set your key."
        )
    return AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=settings.anthropic_max_retries)


async def generate_knowledge_pack(
    device_label: str,
    *,
    device_slug: str | None = None,
    client: AsyncAnthropic | None = None,
    memory_root: Path | None = None,
    max_revise_rounds: int | None = None,
    on_event: OnEvent | None = None,
    uploaded_documents_dir: Path | None = None,
    focus_symptom: str | None = None,
    user_device_kind: str | None = None,
    confirmed_device_kind: str | None = None,
    expect_schematic: bool = False,
    # T13 build metering: tenant + repair identity for the kind='build' usage
    # reports (None = self-host / untracked → reports no-op or land as anon).
    owner_ref: str | None = None,
    engine_repair_id: str | None = None,
) -> PipelineResult:
    """Run the full pipeline for one device.

    Returns a `PipelineResult` with the on-disk path and the final audit verdict.
    Raises RuntimeError on REJECTED verdicts or terminal failures.

    When `on_event` is supplied, the orchestrator emits progress events at
    every phase transition. Event types:
      - pipeline_started      → {device_slug, device_label, model}
      - phase_started/finished → {phase: device_kind|scout|registry|writers|audit, elapsed_s?}
      - pipeline_paused       → {reason: needs_kind_confirmation, device_slug, ...}
      - pipeline_finished     → {status, revise_rounds_used, consistency_score}
      - pipeline_failed       → {status, error} (REJECTED or unexpected exception)

    The callback is awaited between phases but errors inside it are swallowed
    with a warning — UI delivery must never crash the pipeline.
    """
    settings = get_settings()
    client = client or _get_client()
    memory_root = memory_root or Path(settings.memory_root)
    max_revise_rounds = (
        max_revise_rounds if max_revise_rounds is not None else settings.pipeline_max_revise_rounds
    )
    emit = _wrap_on_event(on_event)

    # Per-phase model distribution. Opus handles synthesis + judgment (graph,
    # rules, audit); Sonnet handles extraction (web research, registry, per-component
    # sheets) — cheaper and plenty for those shapes.
    model_main = settings.anthropic_model_main  # Opus
    model_sonnet = settings.anthropic_model_sonnet  # Sonnet
    models_by_role = {
        "scout": model_sonnet,
        "registry": model_sonnet,
        "mapper": model_sonnet,
        "cartographe": model_main,
        "clinicien": model_main,
        "lexicographe": model_sonnet,
        "auditor": model_main,
    }
    # A pinned slug (create_repair's `device_slug`, or the cloud's canonical
    # slug) decides the pack directory — re-slugifying the rich label here
    # would build a FRESH pack next to the one holding the uploaded documents
    # (live pilot 2026-06-12: pinned `macbook-pro-m1`, label slugified to
    # `macbook-pro-13-m1-2020-…` → graph=no, schematic ignored).
    slug = device_slug or _slugify(device_label)

    pack_dir = memory_root / slug
    pack_dir.mkdir(parents=True, exist_ok=True)
    # Truthfulness contract: the pack writes are incremental, so record that a
    # build is in flight — any exit that doesn't reach mark_complete/mark_paused
    # below leaves the pack vetoed as incomplete (no phantom coverage, a retry
    # rebuilds riding the caches). See api/pipeline/build_state.py.
    build_state.mark_building(pack_dir)

    # ---- Technician-supplied documents ----------------------------------
    # Default search location is the device's per-pack uploads directory;
    # callers (tests) can point at any other directory. An empty / missing
    # directory leaves every optional input as None — Scout and Registry
    # then run their legacy paths byte-for-byte.
    uploads_dir = uploaded_documents_dir or (pack_dir / "uploads")
    uploads = scan_uploads(uploads_dir)
    if not uploads.is_empty():
        logger.info(
            "[Pipeline] Found uploads in %s · schematic=%s boardview=%s datasheets=%d notes=%d other=%d",
            uploads_dir,
            uploads.schematic_pdf.name if uploads.schematic_pdf else "—",
            uploads.boardview.name if uploads.boardview else "—",
            len(uploads.datasheets),
            len(uploads.notes),
            len(uploads.other),
        )

    # If a schematic PDF was uploaded and no electrical_graph yet exists,
    # ingest the schematic INLINE before Scout. Failure logs and falls
    # through — the pipeline still runs without a graph.
    if (
        uploads.schematic_pdf is not None
        and not (pack_dir / "electrical_graph.json").exists()
    ):
        try:
            from api.pipeline.schematic.orchestrator import ingest_schematic

            t_ing = time.monotonic()
            await emit({"type": "phase_started", "phase": "schematic_ingest"})
            await ingest_schematic(
                device_slug=slug,
                pdf_path=uploads.schematic_pdf,
                client=client,
                memory_root=memory_root,
                device_label=device_label,
                on_event=emit,
            )
            logger.info(
                "[Pipeline] Schematic ingestion complete · pack=%s · elapsed=%.1fs",
                pack_dir,
                time.monotonic() - t_ing,
            )
            await emit({
                "type": "phase_finished",
                "phase": "schematic_ingest",
                "elapsed_s": time.monotonic() - t_ing,
            })
        except Exception:  # noqa: BLE001 — falling back is fine, we just lose enrichment
            logger.exception(
                "[Pipeline] Inline schematic ingestion failed — continuing without graph"
            )

    # A technician-attached schematic does NOT land in uploads/: it rides the
    # dedicated /packs/{slug}/documents endpoint (encrypted, tenant-scoped) and
    # is ingested out-of-band, writing electrical_graph.json when done. When the
    # caller signalled one is coming (`expect_schematic`), wait for that graph
    # before Phase 1.5 so device-kind classification + the Mapper run grounded
    # on the real topology instead of blind. We poll by LOADING (not stat): the
    # ingest rewrites the file several times (compile → boot → passives) without
    # atomic renames, so a present-but-half-written file parses to None and we
    # simply keep waiting. The first parseable graph is complete enough for
    # Phase 1.5 (it only reads rail names + component families) and is held in
    # memory, so later enrichment rewrites can't race the downstream load.
    graph: ElectricalGraph | None = None
    if (
        expect_schematic
        and uploads.schematic_pdf is None
        and not (pack_dir / "electrical_graph.json").exists()
    ):
        await emit({"type": "phase_started", "phase": "schematic_ingest"})
        t_wait = time.monotonic()
        deadline = t_wait + _SCHEMATIC_WAIT_TIMEOUT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(_SCHEMATIC_WAIT_POLL_S)
            graph = _load_existing_electrical_graph(pack_dir)
            if graph is not None:
                break
        elapsed = time.monotonic() - t_wait
        if graph is not None:
            logger.info(
                "[Pipeline] Schematic graph arrived after %.1fs (pack=%s)",
                elapsed, pack_dir,
            )
            await emit({
                "type": "phase_finished",
                "phase": "schematic_ingest",
                "elapsed_s": elapsed,
            })
        else:
            logger.warning(
                "[Pipeline] Expected schematic graph never arrived within %.0fs "
                "— continuing without graph (pack=%s)",
                _SCHEMATIC_WAIT_TIMEOUT_S, pack_dir,
            )
            await emit({
                "type": "phase_finished",
                "phase": "schematic_ingest",
                "elapsed_s": elapsed,
                "timed_out": True,
            })

    # Inline-ingest path (schema in uploads/) or already-on-disk graph: load it
    # here. Skipped only when the wait-gate above already produced one.
    if graph is None:
        graph = _load_existing_electrical_graph(pack_dir)

    # The deterministic existence ground-truth over the compiled graph. Built
    # ONCE here (the graph is now final) and threaded through Phase 2.6 (registry
    # enrichment), compute_drift (registry ∪ graph universe), the auditor (report
    # + query_graph tool), and the revisers (graph-grounded fixes). None when no
    # graph exists → every downstream consumer keeps its legacy web-only path.
    # Construction is defensive: indexing the graph must never abort a build, so
    # an unexpected/half-built graph object degrades to graph_truth=None (the
    # web-only path) rather than crashing — same discipline as the loader above.
    graph_truth: GraphTruth | None = None
    if graph is not None:
        try:
            graph_truth = GraphTruth(graph)
        except Exception:  # noqa: BLE001 — a bad graph must not abort the build
            logger.exception(
                "[Pipeline] GraphTruth construction failed — continuing without "
                "graph ground-truth (pack=%s)",
                pack_dir,
            )

    logger.info("=" * 72)
    logger.info(
        "Pipeline start · device=%r · models=%s · pack=%s · graph=%s",
        device_label,
        models_by_role,
        pack_dir,
        "yes" if graph is not None else "no",
    )
    logger.info("=" * 72)

    await emit({
        "type": "pipeline_started",
        "device_slug": slug,
        "device_label": device_label,
        "models": models_by_role,
        "uploads": {
            "schematic_pdf": uploads.schematic_pdf.name if uploads.schematic_pdf else None,
            "boardview": uploads.boardview.name if uploads.boardview else None,
            "datasheets": [p.name for p in uploads.datasheets],
        },
    })

    phase_stats: list[PhaseTokenStats] = []

    try:
        # -------- Phase 1.5 — Device-kind classification + confirmation gate -----
        # Resolve the device class BEFORE any Scout spend, so a graph/declaration
        # disagreement pauses the pipeline instead of burning a research call on
        # the wrong device family. Two entry modes:
        #   • re-run after the technician confirmed a disagreement
        #     (`confirmed_device_kind` set) — trust it, clear the pending file;
        #   • fresh run — classify from the graph (when present) and reconcile
        #     with the technician's declared kind. A low-confidence verdict or a
        #     user↔graph mismatch short-circuits with NEEDS_KIND_CONFIRMATION.
        # The resolved kind is threaded into Scout + Registry as a constraint and
        # stamped onto the registry taxonomy. See device_kind.py.
        if confirmed_device_kind is not None:
            resolved_kind: str | None = confirmed_device_kind
            device_kind.clear_pending_kind(pack_dir)
            resolution = KindResolution(
                resolved_kind=confirmed_device_kind,
                status="confirmed",
                user_declared=user_device_kind,
                graph_inferred=None,
                confidence=None,
                evidence="user-confirmed",
            )
            device_kind.write_kind_provenance(pack_dir, resolution, resolved_by="user")
            logger.info(
                "[Pipeline] Phase 1.5 · re-run with confirmed device_kind=%r",
                confirmed_device_kind,
            )
        else:
            verdict_kind = None
            if graph is not None:
                t_kind = time.monotonic()
                await emit({"type": "phase_started", "phase": "device_kind"})
                kind_stats = PhaseTokenStats(phase="device_kind")
                try:
                    verdict_kind = await device_kind.classify_device_kind(
                        client=client,
                        model=models_by_role["registry"],
                        device_label=device_label,
                        graph=graph,
                        stats=kind_stats,
                    )
                except Exception:  # noqa: BLE001 — fall back to user-declared kind
                    logger.exception(
                        "[Pipeline] Phase 1.5 classification failed — "
                        "falling back to declared kind"
                    )
                    verdict_kind = None
                finally:
                    kind_stats.duration_s = time.monotonic() - t_kind
                    phase_stats.append(kind_stats)
                await emit({
                    "type": "phase_finished",
                    "phase": "device_kind",
                    "elapsed_s": time.monotonic() - t_kind,
                })

            resolution = device_kind.reconcile_kind(
                user_declared=user_device_kind, verdict=verdict_kind
            )
            if resolution.status == "needs_confirmation":
                device_kind.write_pending_kind(pack_dir, resolution)
                logger.info(
                    "[Pipeline] Phase 1.5 · device-kind needs confirmation · "
                    "user=%r graph=%r conf=%s — pausing before Scout",
                    resolution.user_declared,
                    resolution.graph_inferred,
                    resolution.confidence,
                )
                await emit({
                    "type": "pipeline_paused",
                    "reason": "needs_kind_confirmation",
                    "device_slug": slug,
                    "user_declared": resolution.user_declared,
                    "graph_inferred": resolution.graph_inferred,
                    "confidence": resolution.confidence,
                    "evidence": resolution.evidence,
                })
                # A legitimate early exit, not a failure — keep the finally-hook
                # from recording the parked build as failed. The pack still
                # counts as incomplete until the confirmed re-run completes.
                build_state.mark_paused(pack_dir, reason="needs_kind_confirmation")
                return PipelineResult(
                    device_slug=slug,
                    disk_path=str(pack_dir),
                    status="NEEDS_KIND_CONFIRMATION",
                    verdict=None,
                )
            resolved_kind = resolution.resolved_kind
            device_kind.write_kind_provenance(
                pack_dir,
                resolution,
                resolved_by="graph" if verdict_kind is not None else "user",
            )

        logger.info("[Pipeline] Phase 1.5 · resolved device_kind=%r", resolved_kind)

        # -------- Phase 1 — Scout ------------------------------------------------
        # Scout runs blind: no graph / board / datasheets in its prompt. The
        # 2026-04-24 enrichment was reverted after URL-by-URL audit found 23/23
        # fabricated refdes attributions when Scout was given the graph as
        # context. The function→refdes bridge is now Phase 2.5 (Mapper) — a
        # forced-tool agent with deterministic post-validation. See
        # docs/superpowers/specs/2026-04-25-refdes-mapper-agent.md.
        t0 = time.monotonic()
        await emit({"type": "phase_started", "phase": "scout"})
        scout_stats = PhaseTokenStats(phase="scout")
        raw_dump = await run_scout(
            client=client,
            model=models_by_role["scout"],
            device_label=device_label,
            focus_symptom=focus_symptom,
            min_symptoms=settings.pipeline_scout_min_symptoms,
            min_components=settings.pipeline_scout_min_components,
            min_sources=settings.pipeline_scout_min_sources,
            max_retries=settings.pipeline_scout_max_retries,
            device_kind=resolved_kind,
            stats=scout_stats,
            on_event=emit,
        )
        scout_stats.duration_s = time.monotonic() - t0
        phase_stats.append(scout_stats)
        (pack_dir / "raw_research_dump.md").write_text(raw_dump, encoding="utf-8")
        logger.info("[Pipeline] Phase 1 complete · raw_research_dump.md written")
        await emit({"type": "phase_finished", "phase": "scout", "elapsed_s": scout_stats.duration_s})

        # -------- Phase 2 — Registry --------------------------------------------
        t0 = time.monotonic()
        await emit({"type": "phase_started", "phase": "registry"})
        registry_stats = PhaseTokenStats(phase="registry")
        # Registry runs without the graph too — it focuses on canonical
        # vocabulary extraction. The function→refdes bridge moves to Phase 2.5
        # below. Legacy `refdes_candidates` field on RegistryComponent stays
        # in the schema for back-compat with packs already on disk.
        registry = await run_registry_builder(
            client=client,
            model=models_by_role["registry"],
            device_label=device_label,
            raw_dump=raw_dump,
            device_kind=resolved_kind,
            stats=registry_stats,
        )
        registry_stats.duration_s = time.monotonic() - t0
        phase_stats.append(registry_stats)
        # Stamp the resolved device class onto the canonical taxonomy so
        # downstream consumers (UI, agent prompt) read it from registry.json.
        if resolved_kind is not None:
            registry.taxonomy.device_kind = resolved_kind
        # -------- Phase 2.6 — deterministic registry enrichment from graph -----
        # Close the registry-cites-an-undefined-rail gap BEFORE persist: a
        # component description that names a real rail (PP1V2_S2) but which the
        # web-derived signals never listed leaves the drift check with no
        # canonical entry → the auditor calls the rail fabricated. This is a
        # cheap, deterministic, no-LLM step (a cited-but-absent rail is never
        # invented), so it emits no event — just an info log of the added signals.
        if graph_truth is not None:
            added_signals = enrich_registry_from_graph(registry, graph_truth)
            if added_signals:
                logger.info(
                    "[Pipeline] Phase 2.6 · registry enriched from graph · +%d signals: %s",
                    len(added_signals),
                    added_signals,
                )
            # -------- Phase 2.7 — drop web-registry fictions ------------------
            # When a schematic is the authority, a component the registry names
            # but the graph AND the raw vision/OCR attest NOWHERE is a web
            # fiction (e.g. U6903 on a MacBook whose 3V3 rail is really sourced
            # by R6999). Left in, the Cartographe trusts it and wires a phantom
            # `powers` edge the auditor then rejects. The triple-negative
            # (graph ∪ vision) makes the purge safe against vision recall gaps:
            # a real-but-untraced part still shows in the page vision and stays.
            seen = load_seen_refdes(pack_dir)
            fictions = set(find_registry_fictions(registry, graph_truth, seen))
            if fictions:
                registry.components = [
                    c for c in registry.components if c.canonical_name not in fictions
                ]
                logger.info(
                    "[Pipeline] Phase 2.7 · dropped %d registry fiction(s) "
                    "(unattested by graph+vision): %s",
                    len(fictions),
                    sorted(fictions),
                )
        (pack_dir / "registry.json").write_text(
            registry.model_dump_json(indent=2), encoding="utf-8"
        )
        logger.info("[Pipeline] Phase 2 complete · registry.json written")
        # T9a: enrich the device alias registry (the "carnet") with the facets
        # Scout/Registry discovered (board#/model/EMC/marketing + family) so a
        # later input by ANY of them resolves to this pack — the cross-facet
        # dedup bridge. Best-effort; never disturbs a build.
        with contextlib.suppress(Exception):
            _carnet = get_device_registry_store(pack_dir.parent)
            await register_from_registry(_carnet, pack_dir.name, registry.model_dump())
        await emit({
            "type": "phase_finished",
            "phase": "registry",
            "elapsed_s": registry_stats.duration_s,
            "counts": {
                "components": len(registry.components),
                "signals": len(registry.signals),
            },
            "taxonomy": registry.taxonomy.model_dump(),
        })

        # -------- Phase 2.5 — Refdes Mapper (only when a graph is loaded) -------
        # See docs/superpowers/specs/2026-04-25-refdes-mapper-agent.md.
        # Maps registry canonical names → graph refdes via forced-tool +
        # server-side validation. Failure is silent: mapper errors degrade to
        # an empty mappings file, and bench-gen falls back to its rail-overlap
        # heuristic. Skipped entirely when no graph is loaded.
        mappings: RefdesMappings | None = None
        if graph is not None:
            t_map = time.monotonic()
            await emit({"type": "phase_started", "phase": "mapper"})
            mapper_stats = PhaseTokenStats(phase="mapper")
            try:
                mappings = await run_mapper(
                    client=client,
                    model=models_by_role["mapper"],
                    device_label=device_label,
                    device_slug=slug,
                    raw_dump=raw_dump,
                    registry=registry,
                    graph=graph,
                    stats=mapper_stats,
                )
                mapper_stats.duration_s = time.monotonic() - t_map
                phase_stats.append(mapper_stats)
                (pack_dir / "refdes_attributions.json").write_text(
                    mappings.model_dump_json(indent=2),
                    encoding="utf-8",
                )
                logger.info(
                    "[Pipeline] Phase 2.5 complete · refdes_attributions.json written · n=%d",
                    len(mappings.attributions),
                )
                await emit({
                    "type": "phase_finished",
                    "phase": "mapper",
                    "elapsed_s": time.monotonic() - t_map,
                    "counts": {"attributions": len(mappings.attributions)},
                })
            except Exception:  # noqa: BLE001 — non-fatal: bench-gen has a heuristic fallback
                logger.exception(
                    "[Pipeline] Phase 2.5 mapper failed — continuing without attributions"
                )
                # Persist an empty attributions file so downstream consumers
                # observe "graph was present but mapper produced nothing"
                # rather than "graph was absent".
                empty = RefdesMappings(device_slug=slug, attributions=[])
                (pack_dir / "refdes_attributions.json").write_text(
                    empty.model_dump_json(indent=2),
                    encoding="utf-8",
                )

        # -------- Phase 3 — Writers (parallel) ----------------------------------
        t0 = time.monotonic()
        await emit({"type": "phase_started", "phase": "writers"})
        w_stats = {
            "cartographe": PhaseTokenStats(phase="writer_cartographe"),
            "clinicien": PhaseTokenStats(phase="writer_clinicien"),
            "lexicographe": PhaseTokenStats(phase="writer_lexicographe"),
        }
        kg, rules, dictionary = await run_writers_parallel(
            client=client,
            cartographe_model=models_by_role["cartographe"],
            clinicien_model=models_by_role["clinicien"],
            lexicographe_model=models_by_role["lexicographe"],
            device_label=device_label,
            raw_dump=raw_dump,
            registry=registry,
            cache_warmup_seconds=settings.pipeline_cache_warmup_seconds,
            writer_stats=w_stats,
            on_event=emit,
        )
        writers_elapsed = time.monotonic() - t0
        for ws in w_stats.values():
            ws.duration_s = writers_elapsed
            phase_stats.append(ws)
        _write_writer_outputs(pack_dir, kg, rules, dictionary)
        logger.info("[Pipeline] Phase 3 complete · 3 writer files written")
        await emit({
            "type": "phase_finished",
            "phase": "writers",
            "elapsed_s": writers_elapsed,
            "counts": {
                "nodes": len(kg.nodes),
                "edges": len(kg.edges),
                "rules": len(rules.rules),
                "entries": len(dictionary.entries),
            },
        })

        # -------- Phase 4 — Audit + self-healing loop ---------------------------
        # CONVERGENCE POLICY (Task 10). The naive loop revised until APPROVED or
        # max-rounds, then hard-failed — losing a ~$100 build over a near-miss the
        # auditor had a precise brief for, and worse, sometimes shipping a rewrite
        # that scored LOWER than an earlier round (the macbook-air-m1 0.78 → 0.42
        # collapse). Three rules fix that:
        #   • EARLY-STOP on regression — a trajectory that drops below the prior
        #     round never recovered in practice, and each extra round costs real
        #     Opus $. The moment the score regresses we stop revising.
        #   • BEST-OF snapshot — we track the highest-scoring round's full
        #     (kg, rules, dictionary, verdict) and never ship something worse than
        #     one we already produced.
        #   • ACCEPTANCE FLOOR — when we stop without an APPROVED, the best
        #     snapshot is accepted WITH WARNINGS iff it clears
        #     `pipeline_accept_score` AND has empty deterministic drift (the hard
        #     gate — a high LLM score with real refdes drift is NOT shippable).
        #     floor=0 disables this and restores the legacy hard-fail.
        t0 = time.monotonic()
        await emit({"type": "phase_started", "phase": "audit"})
        rounds_used = 0
        verdict: AuditVerdict
        # prev_score: the consistency of the PREVIOUS round, to detect regression.
        # best: the highest-scoring (score, kg, rules, dictionary, verdict) so far.
        # accepted_with_warnings: set when the floor path rescues a near-miss.
        prev_score: float | None = None
        best: tuple[float, KnowledgeGraph, RulesSet, Dictionary, AuditVerdict] | None = None
        accepted_with_warnings = False

        while True:
            # Live sub-step: round 0 is the initial audit, round N>=1 a revision
            # pass. The landing UI renders these into the audit phase's live line.
            await emit({"type": "phase_step", "phase": "audit", "step": "round", "index": rounds_used})
            # Build the mention-scoped ground-truth report for this round's
            # artefacts when a graph exists — it's what the auditor reads instead
            # of the raw graph (anti-fabrication discipline) and what grounds the
            # revisers' fixes. None with no graph → both keep the web-only path.
            report = (
                build_ground_truth_report(
                    graph_truth, extract_mentions(registry, kg, rules, dictionary)
                )
                if graph_truth is not None
                else None
            )
            code_drift = compute_drift(
                registry=registry,
                knowledge_graph=kg,
                rules=rules,
                dictionary=dictionary,
                graph_truth=graph_truth,
            )
            logger.info(
                "[Pipeline] Pre-computed drift · items=%d · files=%s",
                len(code_drift),
                sorted({item.file for item in code_drift}),
            )
            auditor_phase_name = "auditor" if rounds_used == 0 else f"auditor_rev_{rounds_used}"
            auditor_stats = PhaseTokenStats(phase=auditor_phase_name)
            previous_brief = verdict.revision_brief if rounds_used > 0 else ""  # noqa: F821 — verdict is bound on the prior loop iteration; rounds_used==0 short-circuits
            call_t0 = time.monotonic()
            verdict = await run_auditor(
                client=client,
                model=models_by_role["auditor"],
                device_label=device_label,
                registry=registry,
                knowledge_graph=kg,
                rules=rules,
                dictionary=dictionary,
                precomputed_drift=code_drift,
                revision_brief=previous_brief,
                graph_truth=graph_truth,
                ground_truth_report=report,
                max_query_turns=settings.pipeline_graph_query_turns_auditor,
                stats=auditor_stats,
            )
            auditor_stats.duration_s = time.monotonic() - call_t0
            phase_stats.append(auditor_stats)
            (pack_dir / "audit_verdict.json").write_text(
                verdict.model_dump_json(indent=2), encoding="utf-8"
            )

            # Snapshot the best round seen so far BEFORE any terminal decision, so
            # the floor path below can fall back to it (and APPROVED naturally is
            # its own best). Strict > keeps the EARLIEST round on a tie — fewer $.
            if best is None or verdict.consistency_score > best[0]:
                best = (verdict.consistency_score, kg, rules, dictionary, verdict)

            if verdict.overall_status == "APPROVED":
                logger.info("[Pipeline] Phase 4 APPROVED on round=%d", rounds_used)
                break

            if verdict.overall_status == "REJECTED":
                logger.error("[Pipeline] Auditor REJECTED the pack — aborting")
                await emit({
                    "type": "pipeline_failed",
                    "status": "REJECTED",
                    "error": verdict.revision_brief or "auditor rejected the pack",
                })
                raise RuntimeError(
                    f"Pipeline failed: auditor rejected the pack. "
                    f"brief={verdict.revision_brief!r}"
                )

            # NEEDS_REVISION. A regression (score dropped vs the previous round)
            # is a STOP signal — on real builds it never recovered and each round
            # is real Opus spend. We stop here the same way we'd stop on exhausted
            # rounds: fall back to the best snapshot (floor or hard-fail).
            regression = prev_score is not None and verdict.consistency_score < prev_score
            prev_score = verdict.consistency_score

            if rounds_used >= max_revise_rounds or regression:
                score, b_kg, b_rules, b_dict, b_verdict = best
                # EDGE BACKSTOP. The revise-loop saw every graph-contradicted power
                # edge as drift and got its chance to re-attribute; whatever survived
                # must be pruned DETERMINISTICALLY before the gate, or it reads as
                # residual drift and sinks an otherwise-shippable pack to REJECTED
                # (the macbook U7800→PP1V8_S0 class). Same discipline as the registry
                # fiction purge — drop the false edge, never invent the right one.
                if graph_truth is not None:
                    b_kg, pruned_edges = prune_contradicted_edges(b_kg, graph_truth)
                    if pruned_edges:
                        logger.warning(
                            "[Pipeline] Edge backstop pruned %d graph-contradicted "
                            "edge(s) from the best snapshot: %s",
                            len(pruned_edges),
                            [f"{c.src}->{c.rail}" for c in pruned_edges],
                        )
                # ACCEPTANCE FLOOR. The best snapshot is shippable WITH WARNINGS
                # iff (a) the floor is enabled (>0), (b) it clears the floor, and
                # (c) it has ZERO deterministic drift — the registry∪graph set-diff
                # over the SAME artefacts we'd ship. The drift check is the hard
                # gate: a high LLM score next to a real undefined refdes is exactly
                # the corruptible state we must not auto-publish. Re-running it on
                # the best snapshot (not the last round) keeps the gate honest.
                floor = settings.pipeline_accept_score
                acceptable = floor > 0 and score >= floor and not compute_drift(
                    registry=registry,
                    knowledge_graph=b_kg,
                    rules=b_rules,
                    dictionary=b_dict,
                    graph_truth=graph_truth,
                )
                if acceptable:
                    # Adopt the best snapshot and persist it — we may have rewritten
                    # PAST it into a worse round, so write the best back to disk so
                    # the on-disk pack matches the verdict we ship.
                    kg, rules, dictionary = b_kg, b_rules, b_dict
                    # Le verdict PERSISTÉ doit dire ce qui a été décidé : un
                    # audit_verdict.json en NEEDS_REVISION sur un pack shippé
                    # mentirait à tout consommateur disque (pack-admin, UI
                    # qualité). Le brief résiduel reste lisible dans le même
                    # fichier ET dans pack_quality.audit_warnings.
                    # NOTE Literal : "APPROVED_WITH_WARNINGS" n'est PAS dans le
                    # Literal d'AuditVerdict — c'est voulu (le schéma du tool
                    # submit_audit_verdict est généré depuis ce modèle ; élargir
                    # le Literal autoriserait l'auditor LLM à l'émettre).
                    # model_copy(update=) ne revalide pas, et le seul lecteur du
                    # fichier (routes/packs.py) lit du JSON brut sans Pydantic.
                    verdict = b_verdict.model_copy(
                        update={"overall_status": "APPROVED_WITH_WARNINGS"}
                    )
                    _write_writer_outputs(pack_dir, kg, rules, dictionary)
                    (pack_dir / "audit_verdict.json").write_text(
                        verdict.model_dump_json(indent=2), encoding="utf-8"
                    )
                    accepted_with_warnings = True
                    logger.warning(
                        "[Pipeline] Phase 4 accepted WITH WARNINGS · best score=%.2f "
                        "(floor=%.2f) · reason=%s — remaining brief persisted to pack_quality",
                        score,
                        floor,
                        "regression" if regression else "rounds exhausted",
                    )
                    break

                # Not acceptable → legacy hard-fail. Stamp the verdict REJECTED
                # with a brief that records WHY (the score vs floor / the drift),
                # persist it, emit pipeline_failed, and raise.
                logger.error(
                    "[Pipeline] Phase 4 unrecoverable · best score=%.2f (floor=%.2f) · "
                    "reason=%s — rejecting.",
                    score,
                    floor,
                    "regression" if regression else "rounds exhausted",
                )
                verdict = b_verdict.model_copy(
                    update={
                        "overall_status": "REJECTED",
                        "revision_brief": (
                            f"Unrecoverable after {rounds_used} revise round(s) "
                            f"({'score regression' if regression else 'rounds exhausted'}); "
                            f"best score {score:.2f} below floor {floor:.2f} or with residual "
                            f"deterministic drift. Last brief: {b_verdict.revision_brief!r}"
                        ),
                    }
                )
                (pack_dir / "audit_verdict.json").write_text(
                    verdict.model_dump_json(indent=2), encoding="utf-8"
                )
                await emit({
                    "type": "pipeline_failed",
                    "status": "REJECTED",
                    "error": verdict.revision_brief,
                })
                raise RuntimeError(
                    f"Pipeline failed: unrecoverable after {rounds_used} revise round(s). "
                    f"brief={verdict.revision_brief!r}"
                )

            rounds_used += 1
            logger.info(
                "[Pipeline] Revise round=%d · files=%s · brief=%r",
                rounds_used,
                verdict.files_to_rewrite,
                verdict.revision_brief[:200],
            )
            kg, rules, dictionary = await _apply_revisions(
                client=client,
                cartographe_model=models_by_role["cartographe"],
                clinicien_model=models_by_role["clinicien"],
                lexicographe_model=models_by_role["lexicographe"],
                device_label=device_label,
                raw_dump=raw_dump,
                registry=registry,
                verdict=verdict,
                current_kg=kg,
                current_rules=rules,
                current_dictionary=dictionary,
                ground_truth_report=report,
                graph_truth=graph_truth,
                max_query_turns=settings.pipeline_graph_query_turns_reviser,
                stats_sink=phase_stats,
                round_index=rounds_used,
            )
            _write_writer_outputs(pack_dir, kg, rules, dictionary)

        # Post-loop edge backstop — covers the APPROVED path (the floor path
        # already pruned its snapshot before the gate). If the auditor approved a
        # pack that still carries a graph-contradicted edge, drop it now so the
        # shipped kg never contradicts the schematic. Idempotent → a no-op when the
        # floor branch already cleaned the snapshot.
        if graph_truth is not None:
            kg, post_pruned = prune_contradicted_edges(kg, graph_truth)
            if post_pruned:
                logger.warning(
                    "[Pipeline] Edge backstop pruned %d graph-contradicted edge(s) "
                    "post-acceptance: %s",
                    len(post_pruned),
                    [f"{c.src}->{c.rail}" for c in post_pruned],
                )
                _write_writer_outputs(pack_dir, kg, rules, dictionary)

        await emit({
            "type": "phase_finished",
            "phase": "audit",
            "elapsed_s": time.monotonic() - t0,
            # APPROVED_WITH_WARNINGS surfaces the floor-rescue to the UI as a
            # distinct, non-failure terminal state (the residual brief lives in
            # pack_quality.audit_warnings).
            "status": "APPROVED_WITH_WARNINGS" if accepted_with_warnings else verdict.overall_status,
            "consistency_score": verdict.consistency_score,
            "revise_rounds_used": rounds_used,
        })

        # -------- Pack lint — deterministic pre-persist quality signal ----------
        # Cheap regex checks over the final rules + registry + graph rails.
        # Findings are a SIGNAL only: they're logged + persisted to
        # `pack_quality.json` but never abort the pipeline (auto-publish
        # blocking on `reject` severity is deferred to sub-project B).
        graph_rails = set(graph.power_rails.keys()) if graph is not None else None
        lint_findings = lint_pack(
            registry=registry,
            rules_text=rules.model_dump_json(),
            graph_rails=graph_rails,
        )
        if lint_findings:
            logger.warning(
                "[Pipeline] pack_lint: %d finding(s): %s",
                len(lint_findings),
                [f"{f.code}/{f.severity}" for f in lint_findings],
            )
        # When the pack was accepted below APPROVED, attach the residual audit
        # state (the score + the unresolved brief + the deterministic drift) to
        # pack_quality. This is the auditable trace of what we shipped-with-known-
        # gaps — re-servable by a quality UI so a tech sees the caveats. Clean
        # APPROVED runs pass None and the key is absent.
        _write_pack_quality(
            pack_dir,
            lint_findings,
            audit_warnings={
                "consistency_score": verdict.consistency_score,
                "revision_brief": verdict.revision_brief,
                "drift_report": [d.model_dump() for d in verdict.drift_report],
            }
            if accepted_with_warnings
            else None,
        )
        logger.info("[Pipeline] pack_quality.json written")

        # Every pack file is on disk and audited — flip the marker BEFORE the
        # pipeline_finished emit so a subscriber reacting to the event (the
        # cloud's outcome-follow, a retry POST) never reads a stale 'building'.
        build_state.mark_complete(pack_dir)

        # -------- Done ----------------------------------------------------------
        logger.info("Pipeline end · pack=%s · rounds=%d", pack_dir, rounds_used)
        logger.info("=" * 72)

        # Lot 3 — graph↔boardview QA gate. When the build produced a graph AND a
        # boardview was supplied, write coverage_report.json + get a PASS/WARN/FAIL
        # verdict. Best-effort (never crashes the build).
        coverage_verdict = graph_coverage.run_coverage_gate(pack_dir, uploads.boardview)

        # Lot 2 + Lot 3 — a web-only managed build (no graph) OR a schematic build
        # that FAILED coverage is relocated to the requesting tenant's PRIVATE
        # staging layer instead of the shared commons. Done after mark_complete
        # (pipeline finished reading root) and before the seed (which mounts the
        # SHARED device store — must not mirror a private pack). Self-host /
        # schematic-backed builds that PASS/WARN stay shared.
        staged_private = _stage_if_private(
            memory_root, pack_dir, slug, owner_ref, coverage_verdict
        )

        # Seed the device's Managed-Agents memory store with the freshly
        # approved pack so diagnostic sessions read canonical knowledge via
        # the /mnt/memory/ filesystem mount instead of re-loading JSON on
        # every tool call. No-op when ma_memory_store_enabled is False. SKIPPED
        # for a private web-only pack — the device store is cross-tenant; the
        # owning tenant's agent reads its staged pack live via load_effective_pack.
        if staged_private:
            seed_status = "skipped_web_only_private"
        else:
            seed_status = await seed_memory_store_from_pack(
                client=client, device_slug=slug, pack_dir=pack_dir
            )
        logger.info("[Pipeline] Memory-store seed status=%s", seed_status)

        # Le verdict adopté sur le chemin accept-with-warnings est le snapshot
        # best (NEEDS_REVISION) — le statut FINAL du build, lui, doit dire ce qui
        # a été décidé, pas ce que l'auditor pensait du round. Le cloud ne lit
        # que le TYPE de l'événement, mais l'UI moteur (et tout futur abonné)
        # lit `status` : NEEDS_REVISION sur un build complet serait un mensonge.
        final_status = (
            "APPROVED_WITH_WARNINGS" if accepted_with_warnings else verdict.overall_status
        )
        await emit({
            "type": "pipeline_finished",
            "device_slug": slug,
            "status": final_status,
            "revise_rounds_used": rounds_used,
            "consistency_score": verdict.consistency_score,
            "memory_store_seed": seed_status,
        })

        tokens_used_total = sum(s.input_tokens + s.output_tokens for s in phase_stats)
        cache_read_tokens_total = sum(s.cache_read_input_tokens for s in phase_stats)
        cache_write_tokens_total = sum(s.cache_creation_input_tokens for s in phase_stats)
        return PipelineResult(
            device_slug=slug,
            disk_path=str(pack_dir),
            verdict=verdict,
            revise_rounds_used=rounds_used,
            tokens_used_total=tokens_used_total,
            cache_read_tokens_total=cache_read_tokens_total,
            cache_write_tokens_total=cache_write_tokens_total,
        )
    except RuntimeError:
        raise
    except Exception as exc:  # pragma: no cover — defensive wrapper
        logger.exception("[Pipeline] Unexpected failure")
        await emit({"type": "pipeline_failed", "status": "ERROR", "error": str(exc)})
        raise
    finally:
        # Any exit that didn't reach mark_complete/mark_paused above (REJECTED
        # verdict, unexpected exception, task cancellation) leaves the marker on
        # 'building' — record it as failed so the partial pack stops counting as
        # complete. Catches ALL exits, including CancelledError, without touching
        # the except structure above.
        _in_flight_exc = sys.exc_info()[1]
        build_state.finalize_failed_if_building(
            pack_dir, error=str(_in_flight_exc) if _in_flight_exc else "pipeline did not complete"
        )
        # Always persist telemetry — even on failure, so prior-phase tokens
        # aren't lost and the failure can be diagnosed post-mortem.
        try:
            if phase_stats:
                write_token_stats(pack_dir / "token_stats.json", phase_stats)
                logger.info(
                    "[Pipeline] token_stats.json written · phases=%d",
                    len(phase_stats),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Pipeline] Failed to write token_stats.json: %s", exc)
        # T13 build metering: report the build's per-phase spend to the cloud
        # ledger (kind='build'). In-memory stats — never re-read from disk, so a
        # re-run can't double-report a previous run's file. Hard no-op when the
        # cloud target is unconfigured (self-host); best-effort otherwise.
        try:
            if phase_stats:
                report_build_phases(
                    owner_ref=owner_ref,
                    engine_repair_id=engine_repair_id,
                    stats=phase_stats,
                )
        except Exception as exc:  # noqa: BLE001 — metering must never mask the build outcome
            logger.warning("[Pipeline] build metering report failed: %s", exc)


def _wrap_on_event(on_event: OnEvent | None) -> OnEvent:
    """Return a safe emitter: None → noop; exceptions → log-and-swallow."""
    if on_event is None:
        return _noop_on_event

    async def safe(event: dict[str, Any]) -> None:
        try:
            await on_event(event)
        except Exception:  # noqa: BLE001 — listener failures must not abort pipeline
            logger.warning("[Pipeline] on_event listener raised; swallowing", exc_info=True)

    return safe


def _write_writer_outputs(
    pack_dir: Path,
    kg: KnowledgeGraph,
    rules: RulesSet,
    dictionary: Dictionary,
) -> None:
    (pack_dir / "knowledge_graph.json").write_text(kg.model_dump_json(indent=2), encoding="utf-8")
    (pack_dir / "rules.json").write_text(rules.model_dump_json(indent=2), encoding="utf-8")
    (pack_dir / "dictionary.json").write_text(
        dictionary.model_dump_json(indent=2), encoding="utf-8"
    )


def _write_pack_quality(
    pack_dir: Path,
    findings: list[LintFinding],
    audit_warnings: dict | None = None,
) -> None:
    """Persist deterministic lint findings as the pack-quality signal.

    A clean pack writes an empty `lint_findings` list — the artefact is
    always present so downstream consumers can distinguish "linted, clean"
    from "never linted".

    `audit_warnings`, when provided, records that the pack was accepted BELOW
    APPROVED (the floor-rescue path): the consistency score, the unresolved
    revision brief, and the drift report at the moment of acceptance. It's the
    auditable trace of what remained — re-servable by a quality UI so the caveats
    travel with the pack. Omitted (key absent) on a clean APPROVED build.
    """
    payload = {"lint_findings": [asdict(f) for f in findings]}
    if audit_warnings is not None:
        payload["audit_warnings"] = audit_warnings
    (pack_dir / "pack_quality.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


async def _apply_revisions(
    *,
    client: AsyncAnthropic,
    cartographe_model: str,
    clinicien_model: str,
    lexicographe_model: str,
    device_label: str,
    raw_dump: str,
    registry: Registry,
    verdict: AuditVerdict,
    current_kg: KnowledgeGraph,
    current_rules: RulesSet,
    current_dictionary: Dictionary,
    ground_truth_report: str | None = None,
    graph_truth: GraphTruth | None = None,
    max_query_turns: int = 4,
    stats_sink: list[PhaseTokenStats] | None = None,
    round_index: int = 0,
) -> tuple[KnowledgeGraph, RulesSet, Dictionary]:
    """Re-run each writer flagged by the auditor and return the updated tuple.

    RC1 of the convergence bug — two coupled fixes live HERE:

    1. FIXED revise order `(knowledge_graph, rules, dictionary)`, NOT the auditor's
       `files_to_rewrite` order. On the real macbook-air-m1 build, revising in the
       auditor's order let rules/dictionary realign against a kg that was ITSELF
       about to change — order-dependent, non-deterministic convergence.

    2. THREADING the freshly-revised artefacts forward: after kg is revised, the
       rules reviser receives the NEW kg as `current_kg` (and so on). The reviser
       aligns cross-file references against the up-to-date siblings, so the three
       files re-align on the state that ACTUALLY exists post-revision — not the
       stale snapshot each reviser used to see (which collapsed consistency
       0.78 → 0.42). `kg`/`rules`/`dictionary` below are the loop-local, always-
       current trio; we pass them as the `current_*` siblings on every call.
    """
    kg, rules, dictionary = current_kg, current_rules, current_dictionary

    common_kwargs = {
        "client": client,
        "cartographe_model": cartographe_model,
        "clinicien_model": clinicien_model,
        "lexicographe_model": lexicographe_model,
        "device_label": device_label,
        "raw_dump": raw_dump,
        "registry": registry,
        "revision_brief": verdict.revision_brief,
        "ground_truth_report": ground_truth_report,
        "graph_truth": graph_truth,
        "max_query_turns": max_query_turns,
    }

    requested = set(verdict.files_to_rewrite)
    # Warn on any name the auditor asked for that isn't one of the known three,
    # preserving the legacy skip behaviour (we just no longer iterate its order).
    for unknown in requested - {"knowledge_graph", "rules", "dictionary"}:
        logger.warning("[Pipeline] Skipping unknown file_name in revise: %r", unknown)

    def _reviser_stats(file_name: str) -> PhaseTokenStats | None:
        """Un PhaseTokenStats par appel réviseur, collecté dans le sink du
        caller. Sans lui, les tours query_graph du réviseur (jusqu'à
        max_query_turns appels Opus par fichier) seraient absents de
        token_stats.json et du total facturable — le gap grandit avec
        l'outillage graphe, il n'est plus négligeable."""
        if stats_sink is None:
            return None
        st = PhaseTokenStats(phase=f"reviser_{file_name}_round_{round_index}")
        stats_sink.append(st)
        return st

    # FIXED order — the auditor's order no longer matters; we always revise the
    # graph first so the downstream revisers see the corrected kg as their sibling.
    for file_name in ("knowledge_graph", "rules", "dictionary"):
        if file_name not in requested:
            continue
        if file_name == "knowledge_graph":
            kg = await run_single_writer_revision(
                file_name=file_name,
                previous_output_json=kg.model_dump_json(indent=2),
                current_kg=kg,
                current_rules=rules,
                current_dictionary=dictionary,
                stats=_reviser_stats(file_name),
                **common_kwargs,
            )
        elif file_name == "rules":
            rules = await run_single_writer_revision(
                file_name=file_name,
                previous_output_json=rules.model_dump_json(indent=2),
                current_kg=kg,  # the FRESHLY revised kg, not the original
                current_rules=rules,
                current_dictionary=dictionary,
                stats=_reviser_stats(file_name),
                **common_kwargs,
            )
        elif file_name == "dictionary":
            dictionary = await run_single_writer_revision(
                file_name=file_name,
                previous_output_json=dictionary.model_dump_json(indent=2),
                current_kg=kg,  # freshly revised kg + rules thread forward
                current_rules=rules,
                current_dictionary=dictionary,
                stats=_reviser_stats(file_name),
                **common_kwargs,
            )

    return kg, rules, dictionary
