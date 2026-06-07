"""Measurement journal endpoints — POST + GET under a repair.

Wire-thin shims over `api.tools.measurements.mb_*` so the UI's direct
clicks share the same persistence + classifier path the agent uses
through tool calls.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException

from api.config import get_settings
from api.pipeline.models import MeasurementCreate
from api.pipeline.orchestrator import _slugify
from api.pipeline.routes._helpers import _validate_repair_id
from api.tools.measurements import mb_list_measurements as _mb_list_measurements
from api.tools.measurements import mb_record_measurement as _mb_record_measurement

logger = logging.getLogger("wrench_board.pipeline.api")

router = APIRouter()


@router.post(
    "/packs/{device_slug}/repairs/{repair_id}/measurements",
    status_code=201,
)
async def post_measurement(
    device_slug: str,
    repair_id: str,
    body: MeasurementCreate,
) -> dict:
    """Append a measurement event to the repair journal and auto-classify it.

    Returns `{recorded, auto_classified_mode, timestamp}`. 400 when the
    target string fails parse (expected `rail:<name>` or `comp:<refdes>`).
    WS emission is deliberately skipped here — the tech's direct UI clicks
    are observed by the agent only when it polls the journal.
    """
    settings = get_settings()
    safe_repair_id = _validate_repair_id(repair_id)
    result = _mb_record_measurement(
        device_slug=_slugify(device_slug),
        repair_id=safe_repair_id,
        memory_root=Path(settings.memory_root),
        target=body.target,
        value=body.value,
        unit=body.unit,
        nominal=body.nominal,
        note=body.note,
        source="ui",
    )
    if not result.get("recorded"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.get("/packs/{device_slug}/repairs/{repair_id}/measurements")
async def get_measurements(
    device_slug: str,
    repair_id: str,
    target: str | None = None,
    since: str | None = None,
) -> dict:
    """Return the measurement journal for a repair, newest-first.

    Optional `?target=rail:+3V3` and `?since=<ISO-ts>` query filters.
    Always returns `{found, measurements}` — `measurements` is empty when
    the journal has no matching entries.
    """
    settings = get_settings()
    safe_repair_id = _validate_repair_id(repair_id)
    return _mb_list_measurements(
        device_slug=_slugify(device_slug),
        repair_id=safe_repair_id,
        memory_root=Path(settings.memory_root),
        target=target,
        since=since,
    )
