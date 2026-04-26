# SPDX-License-Identifier: Apache-2.0
"""Pipeline orchestrator — enchaînement complet Phase 1 → 2 → 3 → 4 (+ revise loop).

Persists all intermediate artefacts under `memory/{device_slug}/` on disk:
    raw_research_dump.md
    registry.json
    knowledge_graph.json
    rules.json
    dictionary.json
    audit_verdict.json
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic

from api.agent.memory_seed import seed_memory_store_from_pack
from api.config import get_settings
from api.pipeline.auditor import run_auditor
from api.pipeline.drift import compute_drift
from api.pipeline.mapper import run_mapper
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


def _pack_path(device_label: str, root: Path) -> Path:
    return root / _slugify(device_label)


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
    client: AsyncAnthropic | None = None,
    memory_root: Path | None = None,
    max_revise_rounds: int | None = None,
    on_event: OnEvent | None = None,
    uploaded_documents_dir: Path | None = None,
    focus_symptom: str | None = None,
) -> PipelineResult:
    """Run the full pipeline for one device.

    Returns a `PipelineResult` with the on-disk path and the final audit verdict.
    Raises RuntimeError on REJECTED verdicts or terminal failures.

    When `on_event` is supplied, the orchestrator emits progress events at
    every phase transition. Event types:
      - pipeline_started      → {device_slug, device_label, model}
      - phase_started/finished → {phase: scout|registry|writers|audit, elapsed_s?}
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
    slug = _slugify(device_label)

    pack_dir = _pack_path(device_label, memory_root)
    pack_dir.mkdir(parents=True, exist_ok=True)

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

    graph = _load_existing_electrical_graph(pack_dir)

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
            stats=scout_stats,
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
            stats=registry_stats,
        )
        registry_stats.duration_s = time.monotonic() - t0
        phase_stats.append(registry_stats)
        (pack_dir / "registry.json").write_text(
            registry.model_dump_json(indent=2), encoding="utf-8"
        )
        logger.info("[Pipeline] Phase 2 complete · registry.json written")
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
        t0 = time.monotonic()
        await emit({"type": "phase_started", "phase": "audit"})
        rounds_used = 0
        verdict: AuditVerdict

        while True:
            code_drift = compute_drift(
                registry=registry,
                knowledge_graph=kg,
                rules=rules,
                dictionary=dictionary,
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
                stats=auditor_stats,
            )
            auditor_stats.duration_s = time.monotonic() - call_t0
            phase_stats.append(auditor_stats)
            (pack_dir / "audit_verdict.json").write_text(
                verdict.model_dump_json(indent=2), encoding="utf-8"
            )

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

            # NEEDS_REVISION
            if rounds_used >= max_revise_rounds:
                logger.error(
                    "[Pipeline] Max revise rounds (%d) exhausted with unresolved drift — rejecting.",
                    max_revise_rounds,
                )
                verdict = verdict.model_copy(
                    update={
                        "overall_status": "REJECTED",
                        "revision_brief": (
                            f"Max revise rounds ({max_revise_rounds}) exhausted with "
                            f"unresolved drift. Last brief: {verdict.revision_brief!r}"
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
                    f"Pipeline failed: {max_revise_rounds} revise rounds exhausted "
                    f"with unresolved drift. brief={verdict.revision_brief!r}"
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
            )
            _write_writer_outputs(pack_dir, kg, rules, dictionary)

        await emit({
            "type": "phase_finished",
            "phase": "audit",
            "elapsed_s": time.monotonic() - t0,
            "status": verdict.overall_status,
            "consistency_score": verdict.consistency_score,
            "revise_rounds_used": rounds_used,
        })

        # -------- Done ----------------------------------------------------------
        logger.info("Pipeline end · pack=%s · rounds=%d", pack_dir, rounds_used)
        logger.info("=" * 72)

        # Seed the device's Managed-Agents memory store with the freshly
        # approved pack so diagnostic sessions read canonical knowledge via
        # the /mnt/memory/ filesystem mount instead of re-loading JSON on
        # every tool call. No-op when ma_memory_store_enabled is False.
        seed_status = await seed_memory_store_from_pack(
            client=client, device_slug=slug, pack_dir=pack_dir
        )
        logger.info("[Pipeline] Memory-store seed status=%s", seed_status)

        await emit({
            "type": "pipeline_finished",
            "device_slug": slug,
            "status": verdict.overall_status,
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


def _wrap_on_event(on_event: OnEvent | None) -> OnEvent:
    """Return a safe emitter: None → noop; exceptions → log-and-swallow."""
    if on_event is None:
        return _noop_on_event

    async def safe(event: dict[str, Any]) -> None:
        try:
            await on_event(event)
        except Exception:
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
) -> tuple[KnowledgeGraph, RulesSet, Dictionary]:
    """Re-run each writer flagged by the auditor and return the updated tuple."""
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
    }

    for file_name in verdict.files_to_rewrite:
        if file_name == "knowledge_graph":
            kg = await run_single_writer_revision(
                file_name=file_name,
                previous_output_json=kg.model_dump_json(indent=2),
                **common_kwargs,
            )
        elif file_name == "rules":
            rules = await run_single_writer_revision(
                file_name=file_name,
                previous_output_json=rules.model_dump_json(indent=2),
                **common_kwargs,
            )
        elif file_name == "dictionary":
            dictionary = await run_single_writer_revision(
                file_name=file_name,
                previous_output_json=dictionary.model_dump_json(indent=2),
                **common_kwargs,
            )
        else:
            logger.warning("[Pipeline] Skipping unknown file_name in revise: %r", file_name)

    return kg, rules, dictionary
