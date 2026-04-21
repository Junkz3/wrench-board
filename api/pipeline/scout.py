"""Phase 1 — Scout. Autonomous web research using the native Claude web_search tool.

Output: a single Markdown document (the "raw research dump"). No JSON, no structured form.
"""

from __future__ import annotations

import logging

from anthropic import AsyncAnthropic

from api.pipeline.prompts import SCOUT_SYSTEM, SCOUT_USER_TEMPLATE

logger = logging.getLogger("microsolder.pipeline.scout")


async def run_scout(
    *,
    client: AsyncAnthropic,
    model: str,
    device_label: str,
    max_continuations: int = 3,
) -> str:
    """Execute Phase 1 — return the raw research Markdown dump.

    Handles server-side `pause_turn` iterations: if Anthropic's internal web_search loop
    hits its cap, we re-send the message and let the model resume where it paused.
    """
    logger.info("[Scout] Starting research for device=%r", device_label)

    user_prompt = SCOUT_USER_TEMPLATE.format(device_label=device_label)
    messages: list[dict] = [{"role": "user", "content": user_prompt}]

    web_search_tool = {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 12,
    }

    total_input = 0
    total_output = 0

    for iteration in range(max_continuations + 1):
        logger.info("[Scout] API call iteration=%d", iteration + 1)
        response = await client.messages.create(
            model=model,
            max_tokens=16000,
            system=SCOUT_SYSTEM,
            messages=messages,
            tools=[web_search_tool],
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
        )

        total_input += response.usage.input_tokens
        total_output += response.usage.output_tokens

        if response.stop_reason == "pause_turn":
            logger.info("[Scout] pause_turn — extending conversation to continue")
            messages = [
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": response.content},
            ]
            continue

        if response.stop_reason == "end_turn":
            logger.info(
                "[Scout] Research complete · tokens in=%d out=%d",
                total_input,
                total_output,
            )
            break

        # stop_reason == "max_tokens" or "refusal" — surface clearly
        logger.warning("[Scout] Unexpected stop_reason=%r", response.stop_reason)
        break
    else:
        logger.warning(
            "[Scout] Hit max_continuations=%d without natural end_turn", max_continuations
        )

    # Extract final text blocks. Server-side web_search results are inline in the
    # response but we only want the narrative text the Scout produced.
    text_parts = [block.text for block in response.content if block.type == "text"]
    dump = "\n\n".join(t for t in text_parts if t.strip())

    if not dump:
        raise RuntimeError(
            "[Scout] Produced no text output. Response had "
            f"{len(response.content)} content blocks with types "
            f"{[b.type for b in response.content]}"
        )

    logger.info("[Scout] Web search finished · dump_length=%d chars", len(dump))
    return dump
