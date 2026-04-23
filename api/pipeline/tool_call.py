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
    system: str | list[dict],
    messages: list[dict],
    tools: list[dict],
    forced_tool_name: str,
    output_schema: type[T],
    max_attempts: int = 2,
    max_tokens: int = 16000,
    log_label: str = "tool_call",
) -> T:
    """Call the Messages API with `tool_choice` forced to `forced_tool_name`, validate.

    On validation failure, retry with a system suffix that tells the model what went
    wrong. Raises after `max_attempts` total attempts.
    """
    last_error: str | None = None
    effective_system: str | list[dict] = system

    for attempt in range(1, max_attempts + 1):
        if attempt > 1 and last_error:
            retry_suffix = (
                "\n\n---\nPREVIOUS ATTEMPT FAILED VALIDATION:\n"
                + last_error
                + f"\n\nRetry — emit a valid {forced_tool_name} payload."
            )
            # Suffix is appended without disturbing the upstream cache entry
            # (Anthropic's cache keys on a prefix — prepending or modifying the
            # first block would bust the cache on every retry).
            if isinstance(system, list):
                effective_system = list(system) + [
                    {"type": "text", "text": retry_suffix.lstrip()}
                ]
            else:
                effective_system = system + retry_suffix

        # NOTE — Anthropic rejects `thinking` when `tool_choice` forces a tool
        # (HTTP 400: "Thinking may not be enabled when tool_choice forces tool
        # use."). Forced structured output is deterministic by design; thinking
        # wouldn't help anyway.
        # Stream the response. For small max_tokens a plain `messages.create`
        # would work, but once we allow >=~16k output the SDK refuses the
        # non-streaming path with "Streaming is required for operations that
        # may take longer than 10 minutes." Streaming handles both regimes,
        # and the final Message object exposes `.content` / `.usage` exactly
        # like the non-streaming one, so the rest of this function is
        # indifferent to how the bytes arrived.
        async with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=effective_system,
            messages=messages,
            tools=tools,
            tool_choice={"type": "tool", "name": forced_tool_name},
        ) as stream:
            response = await stream.get_final_message()

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
            logger.warning(
                "[%s] attempt=%d validation failed: %s",
                log_label,
                attempt,
                str(exc).replace("\n", " ")[:500],
            )

    raise RuntimeError(
        f"[{log_label}] Failed to produce a valid {forced_tool_name} output after "
        f"{max_attempts} attempts. Last error:\n{last_error}"
    )


def _try_unwrap(payload: object, output_schema: type[T]) -> T | None:
    """Recover from three observed malformations of the tool input.

    Case A — a field contains JSON as a string (the model double-encoded a
             nested list / dict). `_deep_unwrap_strings` walks the whole
             payload and json.loads every string that looks like JSON, at
             any depth. Handles Haiku-class stringification where the
             pathology cascades several levels down.
    Case B — the whole target was collapsed into one field, e.g.
             `{"rules": "<JSON of {schema_version, rules}>"}`. After the deep
             unwrap we try to validate each top-level value against the
             target schema.

    Returns a validated model or None if nothing recovers a valid payload.
    """
    if not isinstance(payload, dict):
        return None

    unwrapped = _deep_unwrap_strings(payload)

    if unwrapped != payload:
        try:
            return output_schema.model_validate(unwrapped)
        except ValidationError as exc:
            logger.debug(
                "deep-unwrap revalidation failed: %s",
                str(exc).replace("\n", " ")[:300],
            )

    if isinstance(unwrapped, dict):
        for value in unwrapped.values():
            if isinstance(value, dict):
                try:
                    return output_schema.model_validate(value)
                except ValidationError:
                    continue

    return None


def _deep_unwrap_strings(obj: object) -> object:
    """Recursively parse any string whose content is JSON-like into the real value.

    Walks dicts and lists, and for every str whose stripped form begins with
    '[' or '{', attempts json.loads. The parsed result is then recursed into
    as well — some failures observed on Haiku were doubly stringified
    (a list of dicts where one dict's sub-field was itself a stringified
    list). Non-JSON strings and non-container values are returned unchanged.
    """
    if isinstance(obj, str):
        stripped = obj.strip()
        if stripped and stripped[0] in "[{":
            try:
                return _deep_unwrap_strings(json.loads(stripped))
            except (json.JSONDecodeError, ValueError):
                pass
        return obj
    if isinstance(obj, list):
        return [_deep_unwrap_strings(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _deep_unwrap_strings(v) for k, v in obj.items()}
    return obj
