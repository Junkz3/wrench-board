# SPDX-License-Identifier: Apache-2.0
"""LLM extraction pass.

Calls `call_with_forced_tool` with the `propose_scenarios` tool and
validates the output as a `ProposalsPayload`. Optionally (via
`rescue_with_opus`, added in Task 11) re-submits specific rejected
drafts to Opus.
"""

from __future__ import annotations

import logging

from anthropic import AsyncAnthropic

from api.pipeline.bench_generator.prompts import (
    FORCED_TOOL_NAME,
    SYSTEM_PROMPT,
    build_user_message,
    graph_summary,
)
from api.pipeline.bench_generator.schemas import (
    ProposalsPayload,
    ProposedScenarioDraft,
    Rejection,
)
from api.pipeline.schematic.schemas import ElectricalGraph
from api.pipeline.tool_call import call_with_forced_tool

logger = logging.getLogger("microsolder.bench_generator.extractor")


def _propose_tool() -> dict:
    return {
        "name": FORCED_TOOL_NAME,
        "description": (
            "Emit the full list of proposed scenarios for this device. "
            "One tool call, array of objects."
        ),
        "input_schema": ProposalsPayload.model_json_schema(),
    }


async def extract_drafts(
    *,
    client: AsyncAnthropic,
    model: str,
    raw_dump: str,
    rules_json: str,
    registry_json: str,
    graph: ElectricalGraph,
) -> ProposalsPayload:
    """Single-call extraction. Returns the validated payload."""
    user_message = build_user_message(
        raw_dump=raw_dump,
        rules_json=rules_json,
        registry_json=registry_json,
        graph=graph,
    )
    payload = await call_with_forced_tool(
        client=client,
        model=model,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
        tools=[_propose_tool()],
        forced_tool_name=FORCED_TOOL_NAME,
        output_schema=ProposalsPayload,
        log_label="bench_generator.extract",
    )
    logger.info(
        "[bench_generator.extract] device_slug=%s n_scenarios=%d",
        graph.device_slug,
        len(payload.scenarios),
    )
    return payload


_ELIGIBLE_MOTIVES = frozenset(
    {
        "evidence_span_not_literal",
        "refdes_not_in_graph",
    }
)


async def rescue_with_opus(
    *,
    client: AsyncAnthropic,
    model: str,
    rejections: list[Rejection],
    graph: ElectricalGraph,
) -> tuple[list[ProposedScenarioDraft], list[Rejection]]:
    """Re-submit drafts rejected with literal-span or refdes errors.

    Returns (rescued_drafts, still_rejected). Rejections that weren't
    eligible pass through untouched. Rescued drafts still have to go
    back through run_all() — no V-bypass."""
    rescued: list[ProposedScenarioDraft] = []
    still_rejected: list[Rejection] = []
    for rej in rejections:
        if rej.motive not in _ELIGIBLE_MOTIVES or rej.original_draft is None:
            still_rejected.append(rej)
            continue
        draft = rej.original_draft
        user = (
            f"Previous draft was rejected ({rej.motive}): "
            f"{rej.detail}\n\n"
            f"ORIGINAL DRAFT:\n{draft.model_dump_json(indent=2)}\n\n"
            f"VALID REFDES / RAILS FROM THE GRAPH:\n{graph_summary(graph)}\n\n"
            "Emit a CORRECTED scenario via propose_scenarios with exactly "
            "one entry. Preserve the original local_id. Keep source_url, "
            "source_quote, confidence intact. Fix only the spans and/or "
            "refdes so they satisfy the grounding + topology contracts."
        )
        try:
            payload = await call_with_forced_tool(
                client=client,
                model=model,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user}],
                tools=[_propose_tool()],
                forced_tool_name=FORCED_TOOL_NAME,
                output_schema=ProposalsPayload,
                log_label="bench_generator.rescue",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[bench_generator.rescue] local_id=%s Opus call failed: %s",
                rej.local_id,
                exc,
            )
            still_rejected.append(
                Rejection(
                    local_id=rej.local_id,
                    motive="opus_rescue_failed",
                    detail=f"Opus call raised: {exc}",
                    original_draft=draft,
                )
            )
            continue
        if not payload.scenarios:
            still_rejected.append(
                Rejection(
                    local_id=rej.local_id,
                    motive="opus_rescue_failed",
                    detail="Opus returned 0 scenarios",
                    original_draft=draft,
                )
            )
            continue
        rescued.append(payload.scenarios[0])
    return rescued, still_rejected
