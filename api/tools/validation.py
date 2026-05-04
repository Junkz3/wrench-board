"""mb_validate_finding — persist a repair outcome + emit WS event.

Called by the agent at the end of a diagnostic session once the tech
has clicked « Marquer fix » and Claude has confirmed the fixes via
chat. Writes outcome.json and fans out simulation.repair_validated to
the UI so the dashboard can flip to a « validated » state.

The MA memory mirror (copying outcome.json to `/outcomes/{repair_id}.json`
in the device's memory store) is a SEPARATE async helper
`mirror_outcome_to_memory` — the runtime fires it as a background task
after the tool returns. That way `mb_validate_finding` stays sync and
its existing test surface is untouched, while validated outcomes still
show up in the agent's cross-repair memory search for future sessions.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api.agent.validation import RepairOutcome, ValidatedFix, load_outcome, write_outcome

logger = logging.getLogger("wrench_board.tools.validation")

# Per-async-context emitter — each WS session sets its own; concurrent
# sessions never cross-talk. See api/tools/measurements.py for the same
# pattern and rationale.
_ws_emitter: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "wrench_board_validation_ws_emitter", default=None,
)


def set_ws_emitter(emitter: Callable[[dict[str, Any]], None] | None) -> None:
    _ws_emitter.set(emitter)


def _emit(event: dict[str, Any]) -> None:
    emitter = _ws_emitter.get()
    if emitter is not None:
        try:
            emitter(event)
        except Exception:   # noqa: BLE001 — best-effort broadcast
            pass


def _known_refdes(memory_root: Path, device_slug: str) -> set[str] | None:
    """Return the refdes set from the device's electrical_graph, or None if absent."""
    graph_path = memory_root / device_slug / "electrical_graph.json"
    if not graph_path.exists():
        return None
    try:
        from api.pipeline.schematic.schemas import ElectricalGraph
        eg = ElectricalGraph.model_validate_json(graph_path.read_text())
        return set(eg.components.keys())
    except (OSError, ValueError):
        return None


def mb_validate_finding(
    *,
    device_slug: str,
    repair_id: str,
    memory_root: Path,
    fixes: list[dict],
    tech_note: str | None = None,
    agent_confidence: str = "high",
) -> dict[str, Any]:
    """Persist a RepairOutcome for this repair. Emits WS event on success.

    Each fix is a dict {refdes, mode, rationale}. Rejects empty fixes,
    invalid modes, or unknown refdes (when a graph is available).
    """
    if not fixes:
        return {"validated": False, "reason": "empty_fixes"}

    parsed_fixes: list[ValidatedFix] = []
    for raw in fixes:
        try:
            parsed_fixes.append(ValidatedFix.model_validate(raw))
        except ValueError as exc:
            return {"validated": False, "reason": "invalid_fix", "detail": str(exc)}

    known = _known_refdes(memory_root, device_slug)
    if known is not None:
        invalid = sorted(f.refdes for f in parsed_fixes if f.refdes not in known)
        if invalid:
            return {
                "validated": False,
                "reason": "unknown_refdes",
                "invalid_refdes": invalid,
            }

    try:
        outcome = RepairOutcome(
            validated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            repair_id=repair_id,
            device_slug=device_slug,
            fixes=parsed_fixes,
            tech_note=tech_note,
            agent_confidence=agent_confidence,  # type: ignore[arg-type]
        )
    except ValueError as exc:
        return {"validated": False, "reason": "invalid_outcome", "detail": str(exc)}

    if not write_outcome(memory_root=memory_root, outcome=outcome):
        return {"validated": False, "reason": "io_error"}

    _emit({
        "type": "simulation.repair_validated",
        "repair_id": repair_id,
        "fixes_count": len(parsed_fixes),
    })
    return {
        "validated": True,
        "repair_id": repair_id,
        "fixes_count": len(parsed_fixes),
        "validated_at": outcome.validated_at,
    }


async def mirror_outcome_to_memory(
    *,
    client: Any,   # AsyncAnthropic — not imported at top-level to avoid cycle
    device_slug: str,
    repair_id: str,
    memory_root: Path,
) -> str:
    """Best-effort mirror of the repair's outcome.json into the device's MA
    memory store under `/outcomes/{repair_id}.json`.

    Called as a fire-and-forget task by the runtime after
    `mb_validate_finding` returns — closes the cross-repair learning
    loop so validated fixes become searchable via `memory_search` on
    future sessions for the same device. Never raises.

    Returns one of:
      - "mirrored" — successful upsert on some attempt
      - "skipped:flag_disabled" — ma_memory_store_enabled is False
      - "skipped:no_outcome" — no outcome.json on disk yet
      - "skipped:no_store" — memory store id could not be resolved
      - "error:ensure_store_failed" — ensure_memory_store raised
      - "error:upsert_failed" — all 3 upsert attempts failed
    """
    from api.agent.memory_stores import ensure_memory_store, upsert_memory
    from api.config import get_settings

    settings = get_settings()
    if not settings.ma_memory_store_enabled:
        return "skipped:flag_disabled"

    outcome = load_outcome(
        memory_root=memory_root, device_slug=device_slug, repair_id=repair_id,
    )
    if outcome is None:
        return "skipped:no_outcome"

    try:
        store_id = await ensure_memory_store(client, device_slug)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[ValidationMirror] ensure_memory_store failed for %s: %s",
            device_slug, exc,
        )
        return "error:ensure_store_failed"
    if store_id is None:
        return "skipped:no_store"

    last_exc: Exception | None = None
    delays = (0.5, 1.0, 2.0)
    for attempt in range(len(delays)):
        try:
            result = await upsert_memory(
                client,
                store_id=store_id,
                path=f"/outcomes/{repair_id}.json",
                content=outcome.model_dump_json(indent=2),
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning(
                "[ValidationMirror] upsert attempt %d/%d failed for %s/%s: %s",
                attempt + 1, len(delays), device_slug, repair_id, exc,
            )
            if attempt < len(delays) - 1:
                await asyncio.sleep(delays[attempt])
            continue
        if result is not None:
            logger.info(
                "[ValidationMirror] mirrored %s/%s on attempt %d",
                device_slug, repair_id, attempt + 1,
            )
            return "mirrored"
        if attempt < len(delays) - 1:
            await asyncio.sleep(delays[attempt])
    logger.warning(
        "[ValidationMirror] giving up after %d attempts for %s/%s: %s",
        len(delays), device_slug, repair_id, last_exc,
    )
    return "error:upsert_failed"
