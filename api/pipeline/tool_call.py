"""Shared helper — run an Anthropic request with a forced tool and Pydantic validation.

If the model returns a tool output that doesn't validate against the schema, we retry
once with the validation error surfaced in a follow-up system-suffix message. This
addresses the "200 OK but malformed tool shape" failure mode that's more common in
beta paths.
"""

from __future__ import annotations

import json
import logging
from typing import TypeVar

from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger("microsolder.pipeline.tool_call")


async def call_with_forced_tool(
    *,
    client: AsyncAnthropic,
    model: str,
    system: str,
    messages: list[dict],
    tools: list[dict],
    forced_tool_name: str,
    output_schema: type[T],
    max_attempts: int = 2,
    log_label: str = "tool_call",
) -> T:
    """Call the Messages API with `tool_choice` forced to `forced_tool_name`, validate.

    On validation failure, retry with a system suffix that tells the model what went
    wrong. Raises after `max_attempts` total attempts.
    """
    last_error: str | None = None
    effective_system = system

    for attempt in range(1, max_attempts + 1):
        if attempt > 1 and last_error:
            effective_system = (
                system
                + "\n\n---\nPREVIOUS ATTEMPT FAILED VALIDATION:\n"
                + last_error
                + f"\n\nRetry — emit a valid {forced_tool_name} payload."
            )

        # NOTE — Anthropic rejects `thinking` when `tool_choice` forces a tool
        # (HTTP 400: "Thinking may not be enabled when tool_choice forces tool
        # use."). Forced structured output is deterministic by design; thinking
        # wouldn't help anyway.
        response = await client.messages.create(
            model=model,
            max_tokens=16000,
            system=effective_system,
            messages=messages,
            tools=tools,
            tool_choice={"type": "tool", "name": forced_tool_name},
        )

        tool_use = next(
            (b for b in response.content if b.type == "tool_use" and b.name == forced_tool_name),
            None,
        )

        cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        logger.info(
            "[%s] attempt=%d usage in=%d out=%d cache_read=%d cache_write=%d",
            log_label,
            attempt,
            response.usage.input_tokens,
            response.usage.output_tokens,
            cache_read,
            cache_write,
        )
        if cache_read > 0:
            logger.info("[Cache] Hit for %s (read=%d tokens)", log_label, cache_read)

        if tool_use is None:
            got = [b.type for b in response.content]
            last_error = f"Expected a tool_use block named '{forced_tool_name}', got blocks: {got}"
            logger.warning("[%s] %s", log_label, last_error)
            continue

        try:
            validated = output_schema.model_validate(tool_use.input)
            return validated
        except ValidationError as exc:
            # Defensive unwrap: under forced tool_choice without thinking, Opus
            # occasionally stringifies nested structures — e.g. sends
            # `{"rules": "<JSON of the whole RulesSet>"}` instead of the typed
            # list. Try to recover before burning another retry.
            recovered = _try_unwrap(tool_use.input, output_schema)
            if recovered is not None:
                logger.warning(
                    "[%s] recovered from stringified payload on attempt=%d",
                    log_label,
                    attempt,
                )
                return recovered

            last_error = (
                f"Validation failed for {forced_tool_name} payload:\n{exc}\n"
                "Payload received: "
                + json.dumps(tool_use.input, ensure_ascii=False, indent=2)[:2000]
            )
            logger.warning("[%s] attempt=%d validation failed", log_label, attempt)

    raise RuntimeError(
        f"[{log_label}] Failed to produce a valid {forced_tool_name} output after "
        f"{max_attempts} attempts. Last error:\n{last_error}"
    )


def _try_unwrap(payload: object, output_schema: type[T]) -> T | None:
    """Recover from two observed malformations of the tool input.

    Case A — top-level string fields contain JSON (the model double-encoded a
             nested list / dict). We json.loads every string value that looks
             like JSON, then re-validate.
    Case B — the whole target was collapsed into one field, e.g.
             `{"rules": "<JSON of {schema_version, rules}>"}`. After unwrap we
             try to validate each top-level value against the target schema.

    Returns a validated model or None if neither case applies.
    """
    if not isinstance(payload, dict):
        return None

    unwrapped: dict[str, object] = {}
    changed = False
    for key, value in payload.items():
        if isinstance(value, str):
            stripped = value.strip()
            if stripped and stripped[0] in "[{":
                try:
                    unwrapped[key] = json.loads(stripped)
                    changed = True
                    continue
                except (json.JSONDecodeError, ValueError):
                    pass
        unwrapped[key] = value

    if changed:
        try:
            return output_schema.model_validate(unwrapped)
        except ValidationError:
            pass

    # Case B — maybe one of the unwrapped values IS the target shape.
    for value in unwrapped.values():
        if isinstance(value, dict):
            try:
                return output_schema.model_validate(value)
            except ValidationError:
                continue

    return None
