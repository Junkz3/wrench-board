"""Build-state marker — records the outcome of a pack build on disk.

The orchestrator writes pack files incrementally (registry → writers → audit),
so a mid-pipeline failure leaves a PARTIAL pack behind. Before this marker,
`_pack_is_complete` only checked file presence — a pack whose audit blew up
*after* the 4 writer files landed looked complete, its surviving rules produced
phantom symptom coverage, and a retry never rebuilt (the live 2026-06-10
MacBook test). The marker is the build's truthfulness contract:

  building  → a pipeline is writing this pack right now (or crashed mid-write)
  complete  → the pipeline finished; the files can be trusted
  failed    → the pipeline died; whatever files exist are partial debris
  paused    → the Phase-1.5 kind-confirmation gate parked the build (not an error)

Back-compat: NO marker file = a pack built before the marker existed (every
self-host pack) — treated as complete by `_pack_is_complete` when the 4 files
are present. All writes are best-effort: a marker hiccup must never crash a
build (worst case we regress to the legacy file-presence heuristic).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("memorybank.api")

BUILD_STATE_FILE = "_build_state.json"


def read_build_state(pack_dir: Path) -> dict | None:
    """Parsed marker, or None when absent/corrupted (corrupted = legacy-safe)."""
    path = Path(pack_dir) / BUILD_STATE_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[BuildState] unreadable %s: %s — treating as absent", path, exc)
        return None


def _write(pack_dir: Path, status: str, **fields) -> None:
    payload = {"status": status, **fields}
    try:
        Path(pack_dir).mkdir(parents=True, exist_ok=True)
        (Path(pack_dir) / BUILD_STATE_FILE).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:  # best-effort: never crash a build over the marker
        logger.warning("[BuildState] could not write %s in %s: %s", status, pack_dir, exc)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def mark_building(pack_dir: Path) -> None:
    _write(pack_dir, "building", started_at=_now())


def mark_complete(pack_dir: Path) -> None:
    _write(pack_dir, "complete", finished_at=_now())


def mark_failed(pack_dir: Path, *, stage: str | None = None, error: str | None = None) -> None:
    _write(pack_dir, "failed", stage=stage, error=error, finished_at=_now())


def mark_paused(pack_dir: Path, *, reason: str | None = None) -> None:
    _write(pack_dir, "paused", reason=reason, paused_at=_now())


def finalize_failed_if_building(pack_dir: Path, *, error: str | None = None) -> None:
    """Orchestrator finally-hook: any exit that left the marker on 'building'
    (exception, task cancellation) is recorded as failed. Success/pause set
    their status before `finally` runs, so they pass through untouched."""
    state = read_build_state(pack_dir)
    if state is not None and state.get("status") == "building":
        mark_failed(pack_dir, stage=state.get("stage"), error=error)
