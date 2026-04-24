# SPDX-License-Identifier: Apache-2.0
"""Composed entrypoint: generate_from_pack.

1. Load pack → validate preconditions.
2. Call extractor.extract_drafts (+ optional rescue_with_opus).
3. Run validator.run_all.
4. Promote survivors into ProposedScenario (assign ids, timestamps, archive paths).
5. Score via scoring.score_accepted.
6. Write everything via writer.*.

No global state; all dependencies injected (client, paths, clocks).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path

from anthropic import AsyncAnthropic

from api.pipeline.bench_generator.errors import (
    BenchGeneratorPreconditionError,
)
from api.pipeline.bench_generator.extractor import (
    extract_drafts,
    rescue_with_opus,
)
from api.pipeline.bench_generator.schemas import (
    ProposedScenario,
    ProposedScenarioDraft,
    ReliabilityCard,
    RunManifest,
)
from api.pipeline.bench_generator.scoring import score_accepted
from api.pipeline.bench_generator.validator import run_all
from api.pipeline.bench_generator.writer import (
    update_latest_json,
    write_per_run_files,
    write_reliability_card,
    write_source_archives,
)
from api.pipeline.schematic.schemas import ElectricalGraph

logger = logging.getLogger("microsolder.bench_generator.orchestrator")


def _load_pack(pack_dir: Path) -> tuple[str, str, str, ElectricalGraph]:
    """Load the 4 inputs or raise BenchGeneratorPreconditionError."""
    graph_path = pack_dir / "electrical_graph.json"
    if not graph_path.exists():
        raise BenchGeneratorPreconditionError(
            f"no electrical_graph.json at {graph_path} — "
            "run schematic ingestion first (python -m api.pipeline.schematic.cli)"
        )
    dump_path = pack_dir / "raw_research_dump.md"
    if not dump_path.exists() or len(dump_path.read_text(encoding="utf-8")) < 500:
        raise BenchGeneratorPreconditionError(f"Scout dump at {dump_path} is empty or < 500 chars")
    raw_dump = dump_path.read_text(encoding="utf-8")
    rules_path = pack_dir / "rules.json"
    rules_json = rules_path.read_text(encoding="utf-8") if rules_path.exists() else "{}"
    registry_path = pack_dir / "registry.json"
    registry_json = registry_path.read_text(encoding="utf-8") if registry_path.exists() else "{}"
    graph = ElectricalGraph.model_validate_json(graph_path.read_text(encoding="utf-8"))
    return raw_dump, rules_json, registry_json, graph


def _promote(
    draft: ProposedScenarioDraft,
    *,
    device_slug: str,
    generated_by: str,
    generated_at: str,
    archive_subdir: str,
) -> ProposedScenario:
    """Build the promoted ProposedScenario from a validated draft.
    id = hash(source_quote) prefixed by slug for cross-device uniqueness."""
    quote_hash = hashlib.sha256(draft.source_quote.encode("utf-8")).hexdigest()[:8]
    scenario_id = f"{device_slug}-{draft.local_id}-{quote_hash}"
    return ProposedScenario(
        id=scenario_id,
        device_slug=device_slug,
        cause=draft.cause,
        expected_dead_rails=draft.expected_dead_rails,
        expected_dead_components=draft.expected_dead_components,
        source_url=draft.source_url,
        source_quote=draft.source_quote,
        source_archive=f"{archive_subdir}/{scenario_id}.txt",
        confidence=draft.confidence,
        generated_by=generated_by,
        generated_at=generated_at,
        validated_by_human=False,
        evidence=draft.evidence,
    )


async def generate_from_pack(
    *,
    device_slug: str,
    client: AsyncAnthropic,
    model: str,
    memory_root: Path,
    output_dir: Path,
    latest_path: Path,
    run_date: str,
    escalate_rejects: bool = False,
    opus_model: str = "claude-opus-4-7",
) -> dict:
    """Run the end-to-end bench generation. Returns a summary dict.

    Never raises on an empty-scenarios outcome (valid result for sparse
    packs); does raise BenchGeneratorPreconditionError on missing inputs."""
    pack_dir = memory_root / device_slug
    raw_dump, rules_json, registry_json, graph = _load_pack(pack_dir)

    # Capture mtimes for the manifest (traceability only).
    input_mtimes = {
        name: (pack_dir / name).stat().st_mtime
        for name in ("raw_research_dump.md", "rules.json", "registry.json", "electrical_graph.json")
        if (pack_dir / name).exists()
    }

    payload = await extract_drafts(
        client=client,
        model=model,
        raw_dump=raw_dump,
        rules_json=rules_json,
        registry_json=registry_json,
        graph=graph,
    )
    drafts = payload.scenarios
    n_proposed = len(drafts)

    accepted_drafts, rejects = run_all(drafts, graph)

    if escalate_rejects and rejects:
        rescued, rejects = await rescue_with_opus(
            client=client,
            model=opus_model,
            rejections=rejects,
            graph=graph,
        )
        if rescued:
            accepted_again, more_rejects = run_all(rescued, graph)
            accepted_drafts.extend(accepted_again)
            rejects.extend(more_rejects)

    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    generated_by = f"bench-gen-{model}"
    archive_subdir = "benchmark/auto_proposals/sources"

    accepted: list[ProposedScenario] = [
        _promote(
            d,
            device_slug=device_slug,
            generated_by=generated_by,
            generated_at=generated_at,
            archive_subdir=archive_subdir,
        )
        for d in accepted_drafts
    ]

    scorecard = score_accepted(graph, accepted)
    manifest = RunManifest(
        device_slug=device_slug,
        run_date=run_date,
        run_timestamp=generated_at,
        model=model,
        n_proposed=n_proposed,
        n_accepted=len(accepted),
        n_rejected=len(rejects),
        input_mtimes=input_mtimes,
        escalated_rejects=escalate_rejects,
    )

    write_per_run_files(
        output_dir=output_dir,
        run_date=run_date,
        slug=device_slug,
        accepted=accepted,
        rejected=rejects,
        manifest=manifest,
        scorecard=scorecard,
    )
    write_source_archives(
        archive_dir=output_dir / "sources",
        scenarios=accepted,
    )
    update_latest_json(
        latest_path=latest_path,
        slug=device_slug,
        scorecard=scorecard,
        run_date=run_date,
    )
    reliability_card = ReliabilityCard(
        device_slug=device_slug,
        score=scorecard.score,
        self_mrr=scorecard.self_mrr,
        cascade_recall=scorecard.cascade_recall,
        n_scenarios=scorecard.n_scenarios,
        generated_at=generated_at,
        source_run_date=run_date,
        notes=[
            "Based on auto-generated scenarios, not human-validated.",
            f"Per-scenario breakdown: benchmark/auto_proposals/{device_slug}-{run_date}.score.json",
        ],
    )
    write_reliability_card(memory_dir=pack_dir, card=reliability_card)

    logger.info(
        "[bench_generator] device=%s run_date=%s n_proposed=%d "
        "n_accepted=%d n_rejected=%d score=%.3f",
        device_slug,
        run_date,
        n_proposed,
        len(accepted),
        len(rejects),
        scorecard.score,
    )
    return {
        "n_proposed": n_proposed,
        "n_accepted": len(accepted),
        "n_rejected": len(rejects),
        "score": scorecard.score,
        "self_mrr": scorecard.self_mrr,
        "cascade_recall": scorecard.cascade_recall,
    }
