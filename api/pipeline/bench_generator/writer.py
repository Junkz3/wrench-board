# SPDX-License-Identifier: Apache-2.0
"""Atomic file writes for the bench generator.

Four per-run artefacts + the cross-run `_latest.json` aggregate + the
runtime-consumed `memory/{slug}/simulator_reliability.json` + source
archive snapshots. Every write uses tempfile + os.replace to avoid
half-written files on crash.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from pathlib import Path

from api.pipeline.bench_generator.schemas import (
    ProposedScenario,
    Rejection,
    RunManifest,
)
from api.pipeline.schematic.evaluator import Scorecard

logger = logging.getLogger("microsolder.bench_generator.writer")


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_s = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent,
    )
    tmp_path = Path(tmp_path_s)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _jsonl_dump(items: list[dict]) -> str:
    return "\n".join(json.dumps(it, ensure_ascii=False) for it in items) + "\n"


def write_per_run_files(
    *,
    output_dir: Path,
    run_date: str,
    slug: str,
    accepted: list[ProposedScenario],
    rejected: list[Rejection],
    manifest: RunManifest,
    scorecard: Scorecard,
) -> None:
    """Write the four per-run files atomically."""
    base = output_dir / f"{slug}-{run_date}"
    _atomic_write_text(
        Path(str(base) + ".jsonl"),
        _jsonl_dump([s.model_dump(exclude_none=False) for s in accepted]),
    )
    _atomic_write_text(
        Path(str(base) + ".rejected.jsonl"),
        _jsonl_dump([r.model_dump(exclude_none=False) for r in rejected]),
    )
    _atomic_write_text(
        Path(str(base) + ".manifest.json"),
        json.dumps(manifest.model_dump(), indent=2),
    )
    _atomic_write_text(
        Path(str(base) + ".score.json"),
        json.dumps(scorecard.model_dump(), indent=2),
    )
    logger.info(
        "[bench_generator.writer] wrote 4 files for slug=%s run_date=%s "
        "(n_accepted=%d, n_rejected=%d)",
        slug, run_date, len(accepted), len(rejected),
    )


def update_latest_json(
    *,
    latest_path: Path,
    slug: str,
    scorecard: Scorecard,
    run_date: str,
) -> None:
    """Merge this run's score into the aggregate _latest.json under an
    fcntl advisory lock so concurrent runs don't clobber each other."""
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(latest_path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            raw = fh.read()
            try:
                current = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                logger.warning(
                    "[writer] _latest.json unreadable — starting fresh",
                )
                current = {}
            current[slug] = {
                "score": scorecard.score,
                "self_mrr": scorecard.self_mrr,
                "cascade_recall": scorecard.cascade_recall,
                "n_scenarios": scorecard.n_scenarios,
                "run_date": run_date,
            }
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(current, indent=2))
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def write_source_archives(
    *,
    archive_dir: Path,
    scenarios: list[ProposedScenario],
) -> None:
    """One text file per accepted scenario. Overwritten on re-run."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    for s in scenarios:
        archive_path = archive_dir / f"{s.id}.txt"
        content = (
            f"{s.source_url}\n\n"
            f"---\n\n"
            f"{s.source_quote}\n"
        )
        _atomic_write_text(archive_path, content)
