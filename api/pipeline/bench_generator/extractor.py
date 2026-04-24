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
)
from api.pipeline.bench_generator.schemas import ProposalsPayload
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
