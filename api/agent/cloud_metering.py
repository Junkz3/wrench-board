"""Best-effort token-usage reporting to the wrenchboard-cloud (T13).

The diagnostic agent's per-LLM-call token cost is the tenant-private billing
unit. At each ``span.model_request_end`` the live forwarder fires
``report_turn_usage`` here, which POSTs the raw token counts + model name to the
cloud's ``POST /internal/metering/diagnostic`` endpoint (the cloud prices it and
appends to its idempotent ledger, keyed on ``event_id``).

Standalone / self-host integrity: when ``cloud_metering_url`` /
``cloud_metering_token`` are unset (the default), this is a hard no-op — the
engine never phones home. Mirrors the permissive-by-default convention of
``engine_service_token`` / ``cors_allow_origins`` in :mod:`api.config`.

Like :mod:`api.agent.memory_stores`, every failure (network, HTTP, malformed
config) degrades to a WARNING log and a silent return — a missing usage report
must never disturb a live diagnostic turn. The cloud keeps the per-repair quota
as its hard guard, so a dropped report only costs a ledger row.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from api.config import get_settings

logger = logging.getLogger("wrench_board.agent.cloud_metering")

_METERING_PATH = "/internal/metering/diagnostic"
# Short timeout: this is fire-and-forget and must never stall the agent turn.
_HTTP_TIMEOUT = 10.0

# Keep strong references to in-flight reports. asyncio only holds a weak
# reference to a bare create_task() result, so without this the task can be
# garbage-collected mid-flight before it completes (CPython docs warning).
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def cloud_metering_enabled() -> bool:
    """True only when both the cloud target URL and service token are configured."""
    settings = get_settings()
    return bool(settings.cloud_metering_url and settings.cloud_metering_token)


async def report_turn_usage(
    *,
    owner_ref: str | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    engine_repair_id: str | None,
    event_id: str,
    kind: str = "agent",
) -> None:
    """POST one LLM call's token usage to the cloud. No-op when unconfigured.

    Cache tokens ride the report so the cloud prices them at their own tiers
    (read 0.1x, creation 1.25x input). Dropping them billed hot turns — mostly
    cache_read under prompt caching — as full input (~10x overcharge).

    `kind` buckets the spend cloud-side: 'agent' (interactive chat — bounded by
    the per-plan budget gates) vs 'build' (the one-shot pipeline pack build —
    slot-gated separately, excluded from the chat budget). The cloud rejects
    anything else, loudly.

    Best-effort: any failure is logged at WARNING and swallowed.
    """
    settings = get_settings()
    base = settings.cloud_metering_url
    token = settings.cloud_metering_token
    if not base or not token:
        return

    url = base.rstrip("/") + _METERING_PATH
    payload = {
        "owner_ref": owner_ref,
        "model": model,
        "kind": kind,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "engine_repair_id": engine_repair_id,
        "event_id": event_id,
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as http:
            resp = await http.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
    except Exception as exc:  # noqa: BLE001 - best-effort, never disturb the turn
        logger.warning("[CloudMetering] report raised for event=%s: %s", event_id, exc)
        return
    if resp.status_code != 202:
        logger.warning(
            "[CloudMetering] report event=%s returned %d: %s",
            event_id,
            resp.status_code,
            resp.text[:200],
        )


def fire_and_forget_report(
    *,
    owner_ref: str | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    engine_repair_id: str | None,
    event_id: str,
    kind: str = "agent",
) -> None:
    """Schedule a metering report without blocking the caller (the agent turn).

    No-op when metering is unconfigured, so the self-host hot path never even
    spawns a task. Otherwise spawns a background task and holds a strong
    reference to it until it finishes.
    """
    if not cloud_metering_enabled():
        return
    task = asyncio.create_task(
        report_turn_usage(
            owner_ref=owner_ref,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            engine_repair_id=engine_repair_id,
            event_id=event_id,
            kind=kind,
        )
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
