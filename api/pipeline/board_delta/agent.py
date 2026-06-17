from __future__ import annotations

import logging

from anthropic import AsyncAnthropic

from api.pipeline.tool_call import call_with_forced_tool
from api.pipeline.board_delta.prompts import (
    DELTA_SYSTEM,
    DELTA_STRUCTURE_SYSTEM,
    DELTA_USER_TEMPLATE,
)
from api.pipeline.board_delta.schemas import DeltaBoard
from api.pipeline.board_delta.store import normalize_board_number

logger = logging.getLogger("wrench_board.pipeline.board_delta")

_WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 8}

_MAX_RESEARCH_CONTINUATIONS = 3

_EMIT_BOARD_DELTA_TOOL = {
    "name": "emit_board_delta",
    "description": "Structured per-revision board repair context extracted from the research notes.",
    "input_schema": DeltaBoard.model_json_schema(),
}


async def _research_board(
    *,
    client: AsyncAnthropic,
    model: str,
    device_label: str,
    board_number: str,
) -> str:
    """Phase A: run a web_search-enabled conversation and return raw research text.

    Mirrors the pause_turn continuation loop in scout._scout_once.  At most
    `_MAX_RESEARCH_CONTINUATIONS` extra turns are allowed; the final response's
    text blocks are concatenated and returned.
    """
    user_prompt = DELTA_USER_TEMPLATE.format(
        device_label=device_label, board_number=board_number
    )
    messages: list[dict] = [{"role": "user", "content": user_prompt}]

    response = None
    for iteration in range(_MAX_RESEARCH_CONTINUATIONS + 1):
        logger.info("[BoardDelta] research iteration=%d", iteration + 1)
        response = await client.messages.create(
            model=model,
            max_tokens=8000,
            system=DELTA_SYSTEM,
            messages=messages,
            tools=[_WEB_SEARCH_TOOL],
        )

        if response.stop_reason == "pause_turn":
            logger.info("[BoardDelta] pause_turn — continuing research")
            messages = [
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": response.content},
            ]
            continue

        # end_turn, max_tokens, or refusal — stop here
        break
    else:
        logger.warning(
            "[BoardDelta] Hit max continuations=%d without end_turn",
            _MAX_RESEARCH_CONTINUATIONS,
        )

    assert response is not None
    text_parts = [block.text for block in response.content if block.type == "text"]
    research_text = "\n\n".join(t for t in text_parts if t.strip())

    if not research_text:
        logger.warning("[BoardDelta] research phase produced no text output")
        research_text = "(no research retrieved)"

    logger.info("[BoardDelta] research complete, %d chars", len(research_text))
    return research_text


async def generate_board_delta(
    *,
    client: AsyncAnthropic,
    model: str,
    device_label: str,
    board_number: str,
) -> DeltaBoard:
    """Run a focused web search and return a validated DeltaBoard.

    Two phases:
      A. _research_board — web_search-enabled, returns free text.
      B. call_with_forced_tool — structures the text into DeltaBoard, NO web_search.

    This split avoids mixing a native server tool (web_search) with a forced
    custom tool_choice in the same request, which the Anthropic API may reject.

    The model fills the content; we own the keys (device_label + normalized
    board_number) and the honesty rule: empty content -> coverage='none'.
    """
    norm = normalize_board_number(board_number)

    # Phase A: research
    research_text = await _research_board(
        client=client,
        model=model,
        device_label=device_label,
        board_number=board_number,
    )

    # Phase B: structure
    delta = await call_with_forced_tool(
        client=client,
        model=model,
        system=DELTA_STRUCTURE_SYSTEM,
        messages=[{"role": "user", "content": research_text}],
        tools=[_EMIT_BOARD_DELTA_TOOL],
        forced_tool_name="emit_board_delta",
        output_schema=DeltaBoard,
        max_attempts=2,
        log_label="[BoardDelta]",
    )

    delta.device_label = device_label
    delta.board_number = norm
    if delta.is_empty():
        delta.coverage = "none"
    logger.info("[BoardDelta] %s / %s -> coverage=%s", device_label, norm, delta.coverage)
    return delta
