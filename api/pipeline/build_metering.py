"""T13 build-side reporting: POST the pipeline's per-phase token usage to the
wrenchboard-cloud metering ledger with ``kind='build'``.

The pack build is the expensive one-shot (10-40 € of LLM spend) and was the
blind spot of the cloud's cost telemetry — only the interactive agent reported.
The orchestrator calls :func:`report_build_phases` in its terminal ``finally``
(right where it persists ``token_stats.json``), so success AND failure spend
both land in the ledger. The cloud buckets ``kind='build'`` apart from the
chat budget (a build must not eat the tenant's monthly chat ceiling — builds
are slot-gated by the cloud's NovelBuildGuard instead).

Self-host integrity: rides :mod:`api.agent.cloud_metering`, which is a hard
no-op when ``cloud_metering_url``/``cloud_metering_token`` are unset — the
engine never phones home outside a managed deployment. Best-effort like every
metering path: a dropped report only costs a ledger row, never a build.
"""
from __future__ import annotations

import logging
from uuid import uuid4

from api.agent.cloud_metering import cloud_metering_enabled, fire_and_forget_report
from api.pipeline.telemetry.token_stats import PhaseTokenStats

logger = logging.getLogger("wrench_board.pipeline.build_metering")


def report_build_phases(
    *,
    owner_ref: str | None,
    engine_repair_id: str | None,
    stats: list[PhaseTokenStats],
    run_id: str | None = None,
) -> None:
    """Fire one ``kind='build'`` metering report per non-empty pipeline phase.

    ``run_id`` (a fresh token per pipeline run, minted here when not supplied)
    keeps event_ids unique across re-runs of the SAME repair — the « relancer »
    flow re-fires the pipeline on the same repair_id, and its second build is
    real spend that must not be deduped against the first.

    No-op when cloud metering is unconfigured (self-host) — checked before any
    work so the hot path costs nothing.
    """
    if not cloud_metering_enabled():
        return
    rid = run_id or uuid4().hex[:12]
    reported = 0
    for s in stats:
        if s.call_count == 0 and not (s.input_tokens or s.output_tokens):
            continue  # phase never called the model — nothing to bill
        fire_and_forget_report(
            owner_ref=owner_ref,
            # None on a legacy/edge path → the cloud prices at its `default` tier.
            model=s.model or "unknown",
            input_tokens=s.input_tokens,
            output_tokens=s.output_tokens,
            cache_read_input_tokens=s.cache_read_input_tokens,
            cache_creation_input_tokens=s.cache_creation_input_tokens,
            engine_repair_id=engine_repair_id,
            # 'pack' prefix when no repair backs the run (confirm-kind rebuild);
            # rid keeps event_ids unique across re-runs of the same repair.
            event_id=f"{engine_repair_id or 'pack'}:build:{rid}:{s.phase}",
            kind="build",
        )
        reported += 1
    if reported:
        logger.info(
            "[BuildMetering] reported %d phase(s) · repair=%s owner=%s run=%s",
            reported, engine_repair_id, owner_ref, rid,
        )


def report_delta_usage(
    *,
    owner_ref: str | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    event_id: str,
    kind: str = "delta",
    engine_repair_id: str | None = None,
) -> None:
    """Fire one ``kind='delta'`` metering report for a board-delta generation.

    No-op when cloud metering is unconfigured (self-host) — so the engine
    never phones home outside a managed deployment. Best-effort like every
    metering path: a dropped report only costs a ledger row.
    """
    if not cloud_metering_enabled():
        return
    fire_and_forget_report(
        owner_ref=owner_ref,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        engine_repair_id=engine_repair_id,
        event_id=event_id,
        kind=kind,
    )
    logger.info(
        "[BuildMetering] delta reported · event_id=%s owner=%s kind=%s",
        event_id, owner_ref, kind,
    )
