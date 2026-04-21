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
from pathlib import Path

from anthropic import AsyncAnthropic

from api.config import get_settings
from api.pipeline.auditor import run_auditor
from api.pipeline.registry import run_registry_builder
from api.pipeline.schemas import (
    AuditVerdict,
    Dictionary,
    KnowledgeGraph,
    PipelineResult,
    Registry,
    RulesSet,
)
from api.pipeline.scout import run_scout
from api.pipeline.writers import run_single_writer_revision, run_writers_parallel

logger = logging.getLogger("microsolder.pipeline.orchestrator")


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
    return AsyncAnthropic(api_key=settings.anthropic_api_key)


async def generate_knowledge_pack(
    device_label: str,
    *,
    client: AsyncAnthropic | None = None,
    memory_root: Path | None = None,
    max_revise_rounds: int | None = None,
) -> PipelineResult:
    """Run the full pipeline for one device.

    Returns a `PipelineResult` with the on-disk path and the final audit verdict.
    Raises RuntimeError on REJECTED verdicts or terminal failures.
    """
    settings = get_settings()
    client = client or _get_client()
    memory_root = memory_root or Path(settings.memory_root)
    max_revise_rounds = (
        max_revise_rounds if max_revise_rounds is not None else settings.pipeline_max_revise_rounds
    )

    model = settings.anthropic_model_main  # claude-opus-4-7

    pack_dir = _pack_path(device_label, memory_root)
    pack_dir.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 72)
    logger.info("Pipeline start · device=%r · model=%s · pack=%s", device_label, model, pack_dir)
    logger.info("=" * 72)

    # -------- Phase 1 — Scout ------------------------------------------------
    raw_dump = await run_scout(client=client, model=model, device_label=device_label)
    (pack_dir / "raw_research_dump.md").write_text(raw_dump, encoding="utf-8")
    logger.info("[Pipeline] Phase 1 complete · raw_research_dump.md written")

    # -------- Phase 2 — Registry --------------------------------------------
    registry = await run_registry_builder(
        client=client, model=model, device_label=device_label, raw_dump=raw_dump
    )
    (pack_dir / "registry.json").write_text(registry.model_dump_json(indent=2), encoding="utf-8")
    logger.info("[Pipeline] Phase 2 complete · registry.json written")

    # -------- Phase 3 — Writers (parallel) ----------------------------------
    kg, rules, dictionary = await run_writers_parallel(
        client=client,
        model=model,
        device_label=device_label,
        raw_dump=raw_dump,
        registry=registry,
        cache_warmup_seconds=settings.pipeline_cache_warmup_seconds,
    )
    _write_writer_outputs(pack_dir, kg, rules, dictionary)
    logger.info("[Pipeline] Phase 3 complete · 3 writer files written")

    # -------- Phase 4 — Audit + self-healing loop ---------------------------
    rounds_used = 0
    verdict: AuditVerdict

    while True:
        verdict = await run_auditor(
            client=client,
            model=model,
            device_label=device_label,
            registry=registry,
            knowledge_graph=kg,
            rules=rules,
            dictionary=dictionary,
        )
        (pack_dir / "audit_verdict.json").write_text(
            verdict.model_dump_json(indent=2), encoding="utf-8"
        )

        if verdict.overall_status == "APPROVED":
            logger.info("[Pipeline] Phase 4 APPROVED on round=%d", rounds_used)
            break

        if verdict.overall_status == "REJECTED":
            logger.error("[Pipeline] Auditor REJECTED the pack — aborting")
            raise RuntimeError(
                f"Pipeline failed: auditor rejected the pack. brief={verdict.revision_brief!r}"
            )

        # NEEDS_REVISION
        if rounds_used >= max_revise_rounds:
            logger.warning(
                "[Pipeline] Max revise rounds reached (%d). Accepting pack with residual issues.",
                max_revise_rounds,
            )
            break

        rounds_used += 1
        logger.info(
            "[Pipeline] Revise round=%d · files=%s · brief=%r",
            rounds_used,
            verdict.files_to_rewrite,
            verdict.revision_brief[:200],
        )
        kg, rules, dictionary = await _apply_revisions(
            client=client,
            model=model,
            device_label=device_label,
            raw_dump=raw_dump,
            registry=registry,
            verdict=verdict,
            current_kg=kg,
            current_rules=rules,
            current_dictionary=dictionary,
        )
        _write_writer_outputs(pack_dir, kg, rules, dictionary)

    # -------- Done ----------------------------------------------------------
    logger.info("Pipeline end · pack=%s · rounds=%d", pack_dir, rounds_used)
    logger.info("=" * 72)

    # NOTE: token totals are aggregated via the stdout logs of `call_with_forced_tool`
    # and `run_scout`; wiring them into PipelineResult precisely will require threading
    # a counter through each call site. V2 returns zeros for now — the logs are the
    # source of truth for this hackathon run.
    return PipelineResult(
        device_slug=_slugify(device_label),
        disk_path=str(pack_dir),
        verdict=verdict,
        revise_rounds_used=rounds_used,
        tokens_used_total=0,
        cache_read_tokens_total=0,
        cache_write_tokens_total=0,
    )


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
    model: str,
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

    for file_name in verdict.files_to_rewrite:
        if file_name == "knowledge_graph":
            previous_json = kg.model_dump_json(indent=2)
            kg = await run_single_writer_revision(
                client=client,
                model=model,
                device_label=device_label,
                raw_dump=raw_dump,
                registry=registry,
                file_name=file_name,
                revision_brief=verdict.revision_brief,
                previous_output_json=previous_json,
            )
        elif file_name == "rules":
            previous_json = rules.model_dump_json(indent=2)
            rules = await run_single_writer_revision(
                client=client,
                model=model,
                device_label=device_label,
                raw_dump=raw_dump,
                registry=registry,
                file_name=file_name,
                revision_brief=verdict.revision_brief,
                previous_output_json=previous_json,
            )
        elif file_name == "dictionary":
            previous_json = dictionary.model_dump_json(indent=2)
            dictionary = await run_single_writer_revision(
                client=client,
                model=model,
                device_label=device_label,
                raw_dump=raw_dump,
                registry=registry,
                file_name=file_name,
                revision_brief=verdict.revision_brief,
                previous_output_json=previous_json,
            )
        else:
            logger.warning("[Pipeline] Skipping unknown file_name in revise: %r", file_name)

    return kg, rules, dictionary
