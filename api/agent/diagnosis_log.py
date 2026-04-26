# SPDX-License-Identifier: Apache-2.0
"""Per-repair append-only log of every hypothesize() call during a session.

JSONL store at memory/{slug}/repairs/{repair_id}/diagnosis_log.jsonl, same
best-effort semantics as the measurement memory: IO errors are logged
and swallowed so the diagnostic session never fails on a write miss.

Used by the field-calibrated corpus builder to reconstruct how the
solver's ranking evolved over the course of a repair.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("wrench_board.agent.diagnosis_log")


class DiagnosisLogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: str
    observations: dict           # raw Observations.model_dump()
    hypotheses_top5: list[dict]  # [{kill_refdes, kill_modes, score, narrative}]
    pruning_stats: dict          # {single_candidates_tested, two_fault_pairs_tested, wall_ms}


def _log_path(memory_root: Path, device_slug: str, repair_id: str) -> Path:
    return memory_root / device_slug / "repairs" / repair_id / "diagnosis_log.jsonl"


def append_diagnosis(
    *,
    memory_root: Path,
    device_slug: str,
    repair_id: str,
    observations: dict,
    hypotheses_top5: list[dict],
    pruning_stats: dict,
) -> DiagnosisLogEntry | None:
    """Append one DiagnosisLogEntry to the repair's log, return the entry.

    Returns None if the write fails (best-effort — never raises).
    """
    from datetime import UTC, datetime

    try:
        entry = DiagnosisLogEntry(
            timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            observations=observations,
            hypotheses_top5=hypotheses_top5,
            pruning_stats=pruning_stats,
        )
    except ValueError as exc:
        logger.warning("append_diagnosis: invalid payload: %s", exc)
        return None

    path = _log_path(memory_root, device_slug, repair_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")
    except OSError as exc:
        logger.warning("append_diagnosis: IO error for %s/%s: %s", device_slug, repair_id, exc)
        return None

    return entry


def load_diagnosis_log(
    *,
    memory_root: Path,
    device_slug: str,
    repair_id: str,
) -> list[DiagnosisLogEntry]:
    """Return the ordered list of DiagnosisLogEntries for a repair."""
    path = _log_path(memory_root, device_slug, repair_id)
    if not path.exists():
        return []
    entries: list[DiagnosisLogEntry] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(DiagnosisLogEntry.model_validate_json(line))
            except ValueError:
                logger.warning("load_diagnosis_log: skipping malformed line in %s", path)
                continue
    except OSError as exc:
        logger.warning("load_diagnosis_log: IO error for %s/%s: %s", device_slug, repair_id, exc)
    return entries
