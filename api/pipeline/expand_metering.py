"""T13 expand-side reporting: POST the knowledge-expansion's per-phase token
usage to the wrenchboard-cloud metering ledger with ``kind='expand'``.

``expand_pack`` (the Pro "living memory bank" moat — focused Scout + Registry +
Clinicien on an existing device) makes its OWN Anthropic calls, outside the
diagnostic agent's metered turn stream, so its spend was a blind spot: the agent
reported its reasoning, the build reported its pipeline, but the durable
knowledge enrichment cost nothing in the ledger. This closes that gap.

The cloud buckets ``kind='expand'`` apart from BOTH the chat budget (the budget
gates filter ``kind='agent'``) and the build slot system — it is pure
operator-visibility, never a gate, so reporting it can't lock a tenant out.

Self-host integrity: rides :mod:`api.agent.cloud_metering`, a hard no-op when
``cloud_metering_url``/``cloud_metering_token`` are unset — the engine never
phones home outside a managed deployment. Best-effort: a dropped report only
costs a ledger row, never an expansion.
"""
from __future__ import annotations

import logging
from uuid import uuid4

from api.agent.cloud_metering import cloud_metering_enabled, fire_and_forget_report
from api.pipeline.telemetry.token_stats import PhaseTokenStats

logger = logging.getLogger("wrench_board.pipeline.expand_metering")


def report_expand_phases(
    *,
    owner_ref: str | None,
    device_slug: str,
    stats: list[PhaseTokenStats],
    expansion_id: str | None = None,
    engine_repair_id: str | None = None,
) -> None:
    """Fire one ``kind='expand'`` metering report per non-empty expansion phase.

    Keyed on ``expansion_id`` (unique per expansion, minted here if absent) so a
    re-fired expansion can't be deduped against an earlier one. ``device_slug``
    scopes the event_id since an expansion is pack-level (no repair backs it).

    No-op when cloud metering is unconfigured (self-host) — checked first so the
    hot path costs nothing.
    """
    if not cloud_metering_enabled():
        return
    eid = expansion_id or f"E-{uuid4().hex[:8]}"
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
            event_id=f"{device_slug}:expand:{eid}:{s.phase}",
            kind="expand",
        )
        reported += 1
    if reported:
        logger.info(
            "[ExpandMetering] reported %d phase(s) · slug=%s owner=%s expansion=%s",
            reported, device_slug, owner_ref, eid,
        )
