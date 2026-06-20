"""Shared helper — run an Anthropic request with a forced tool and Pydantic validation.

If the model returns a tool output that doesn't validate against the schema, we retry
once with the validation error surfaced in a follow-up system-suffix message. This
addresses the "200 OK but malformed tool shape" failure mode that's more common in
beta paths.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

import httpx
from anthropic import APIConnectionError, AsyncAnthropic
from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from api.pipeline.telemetry.token_stats import PhaseTokenStats

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger("wrench_board.pipeline.tool_call")

# Transient TRANSPORT failures (peer closed mid-stream, connect refused/reset).
# The SDK's max_retries only re-issues the INITIAL request — a disconnection
# while iterating the stream surfaces raw httpx errors (observed live
# 2026-06-10: one RemoteProtocolError killed a whole 92-page ingestion). These
# get their own small in-place retry budget per attempt: they are infra noise,
# NOT model-quality failures, so they don't consume a validation attempt and
# never touch the prompt (touching it would also bust the prompt cache).
_TRANSPORT_ERRORS = (httpx.TransportError, APIConnectionError)
_TRANSPORT_TRIES = 3
_TRANSPORT_BACKOFF_S = (2.0, 5.0)


async def _create_with_transport_retry(
    *,
    client: AsyncAnthropic,
    stream_kwargs: dict,
    log_label: str,
):
    """Issue ONE Messages-API streamed request with the in-place transport-retry budget.

    Extracted so every API call in the package (the single-shot forced-tool helper
    AND the agentic query loop) gets the identical retry semantics with no
    duplication. Behaviour is byte-for-byte the same as the loop it replaced:

      - Only `_TRANSPORT_ERRORS` (peer-closed mid-stream / connect refused/reset)
        are retried — these are infra noise, not model-quality failures, so they
        DON'T consume a validation attempt and DON'T touch the prompt (touching it
        would bust the upstream prompt cache).
      - Up to `_TRANSPORT_TRIES` attempts with `_TRANSPORT_BACKOFF_S` backoff; the
        last failure re-raises the underlying transport error untouched.
      - A non-transient error (e.g. a 400) is deterministic — it propagates on the
        first try (retrying it just burns time).

    Returns the final assembled message.
    """
    for transport_try in range(1, _TRANSPORT_TRIES + 1):
        try:
            async with client.messages.stream(**stream_kwargs) as stream:
                return await stream.get_final_message()
        except _TRANSPORT_ERRORS as exc:
            if transport_try >= _TRANSPORT_TRIES:
                logger.error(
                    "[%s] transport error persisted after %d tries: %s",
                    log_label, _TRANSPORT_TRIES, exc,
                )
                raise
            delay = _TRANSPORT_BACKOFF_S[min(transport_try - 1, len(_TRANSPORT_BACKOFF_S) - 1)]
            logger.warning(
                "[%s] transient transport error (%s: %s) — retrying in %.0fs (%d/%d)",
                log_label, type(exc).__name__, exc, delay, transport_try, _TRANSPORT_TRIES - 1,
            )
            await asyncio.sleep(delay)


def effort_for_model(model: str) -> str:
    """Effort knob paired with adaptive thinking, shared by every caller.

    `xhigh` is the Opus-tier sweet spot per Anthropic's 4.7/4.8 guide; falls
    back to `high` on Sonnet/Haiku where xhigh would 400. Centralised so the
    direct path (below) and the batch-vision twin can never drift apart.
    """
    return "xhigh" if str(model).startswith("claude-opus-4-") else "high"


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
    stats: PhaseTokenStats | None = None,
    thinking_budget: int | None = None,
) -> T:
    """Call the Messages API with `tool_choice` forced to `forced_tool_name`, validate.

    On validation failure, retry with a system suffix that tells the model what went
    wrong. Raises after `max_attempts` total attempts.
    """
    last_error: str | None = None
    effective_system: str | list[dict] = system
    # Thinking forces tool_choice="auto" (the API rejects thinking + forced tool),
    # so the model can return thinking-only with NO tool call. On that miss we drop
    # thinking for the retry → forced tool_choice → the tool is guaranteed (and we
    # stop burning the thinking budget on a page that already over-ran).
    thinking_active = thinking_budget is not None

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

        # tool_choice rules with thinking (Opus 4.7/4.8):
        #   - Default: `{"type": "tool", "name": forced_tool_name}` — fully
        #     forced, deterministic structured output.
        #   - When `thinking_budget` is set: ONLY `{"type": "auto"}` works.
        #     The Anthropic API rejects thinking + (`tool` | `any`) with
        #     "Thinking may not be enabled when tool_choice forces tool use"
        #     (verified live req_011CaRamyfazF6nwgzTJSQMu, 2026-04-26). With
        #     "auto" the model decides whether to call a tool; the system
        #     prompt explicitly tells it to always emit the tool (see
        #     page_vision SYSTEM_PROMPT). The parser falls through to retry
        #     with a system suffix if the model returns text instead.
        #   - Opus 4.7/4.8 also reject `thinking.type="enabled"` — only
        #     `"adaptive"` is accepted. The integer `thinking_budget` arg is
        #     preserved for source-compat but its value is unused under
        #     adaptive; we pair adaptive with `output_config.effort="high"`
        #     to nudge the model toward deeper reasoning.
        #
        # Streaming required for max_tokens >= ~16k (SDK refuses non-stream
        # otherwise with "operations that may take longer than 10 minutes").
        if thinking_active:
            tool_choice_param: dict = {"type": "auto"}
        else:
            tool_choice_param = {"type": "tool", "name": forced_tool_name}

        stream_kwargs: dict = dict(
            model=model,
            max_tokens=max_tokens,
            system=effective_system,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice_param,
        )
        if thinking_active:
            # Opus 4.7/4.8 default for `thinking.display` is "omitted" (silent),
            # so summarized blocks never reach observers. Opt back in.
            stream_kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
            stream_kwargs.setdefault("output_config", {})["effort"] = (
                effort_for_model(model)
            )

        response = await _create_with_transport_retry(
            client=client, stream_kwargs=stream_kwargs, log_label=log_label,
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
        if stats is not None:
            stats.record(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_read=cache_read,
                cache_write=cache_write,
                model=getattr(response, "model", None),
            )
        if cache_read > 0:
            logger.info("[Cache] Hit for %s (read=%d tokens)", log_label, cache_read)

        if tool_use is None:
            got = [b.type for b in response.content]
            last_error = f"Expected a tool_use block named '{forced_tool_name}', got blocks: {got}"
            logger.warning("[%s] %s", log_label, last_error)
            if thinking_active:
                # The model thought but never called the tool. Drop thinking so the
                # next attempt forces the tool (deterministic) instead of gambling
                # another thinking-only over-run.
                thinking_active = False
                logger.warning("[%s] disabling thinking → forced tool_choice on retry", log_label)
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


def _record_usage(response, stats: PhaseTokenStats | None, log_label: str, turn_desc: str) -> None:
    """Log + accumulate this turn's usage into `stats` (shared by both helpers).

    Pulled out of the body so the agentic loop accumulates EVERY turn the same
    way `call_with_forced_tool` does — input/output + cache read/write counters.
    """
    cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
    logger.info(
        "[%s] %s usage in=%d out=%d cache_read=%d cache_write=%d",
        log_label, turn_desc,
        response.usage.input_tokens, response.usage.output_tokens,
        cache_read, cache_write,
    )
    if stats is not None:
        stats.record(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read=cache_read,
            cache_write=cache_write,
            model=getattr(response, "model", None),
        )
    if cache_read > 0:
        logger.info("[Cache] Hit for %s (read=%d tokens)", log_label, cache_read)


def _answer_query_blocks(
    query_uses: list,
    query_handler: Callable[[dict], dict],
    log_label: str,
) -> list[dict]:
    """Run the deterministic `query_handler` once per query tool_use block and
    return a `tool_result` content block for each, keyed to its own id.

    Shared by the submit-with-accompanying-query path and the query-only path so
    BOTH answer every parallel query block the same way (the API mandates a
    tool_result per tool_use, and Opus fires query_graph in parallel). The handler
    is documented to never raise; a defensive guard still returns an is_error
    stub rather than leaving the block orphaned (an orphan 400s the next request,
    a stub keeps the protocol legal).
    """
    results: list[dict] = []
    for b in query_uses:
        try:
            payload = query_handler(b.input)
            results.append({
                "type": "tool_result",
                "tool_use_id": b.id,
                "content": json.dumps(payload, ensure_ascii=False),
            })
        except Exception as exc:  # handler contract is no-raise; belt-and-suspenders
            logger.warning("[%s] query_handler raised: %s", log_label, exc)
            results.append({
                "type": "tool_result",
                "tool_use_id": b.id,
                "is_error": True,
                "content": f"query failed: {exc}",
            })
    return results


def _looks_like_query(payload: object, query_tool: dict) -> bool:
    """True when a payload sent to the SUBMIT tool is in fact a query_graph
    call (all its keys belong to the query tool's input schema). Opus under
    tool_choice='any' sometimes routes a verification into the submit tool
    (e.g. {op:'who_powers', net:'X'}); recognising it lets the loop answer it
    as a query instead of burning a submit attempt and crashing the build."""
    if not isinstance(payload, dict) or not payload:
        return False
    qprops = set(query_tool.get("input_schema", {}).get("properties", {}))
    return bool(qprops) and set(payload).issubset(qprops)


# Once the soft query cap is hit the model is forced to submit, but a reviser on
# a dense pack often routes ONE last graph verification into the submit tool (a
# query-shaped payload). We answer that disguised query for a few more turns
# instead of failing it — the lookup is deterministic and free, and starving the
# model of it was what no-op'd the reviser. The grace is finite so a model that
# ONLY ever misroutes still terminates (then the payload falls through to the
# normal submit-validation / protocol-miss path, bounded by `max_attempts`).
_POST_CAP_QUERY_REROUTE_GRACE = 3


async def call_with_query_tools(
    *,
    client: AsyncAnthropic,
    model: str,
    system: str | list[dict],
    messages: list[dict],
    query_tool: dict,
    query_handler: Callable[[dict], dict],
    submit_tool: dict,
    submit_tool_name: str,
    output_schema: type[T],
    max_query_turns: int = 6,
    max_attempts: int = 2,
    max_tokens: int = 16000,
    log_label: str = "tool_call",
    stats: PhaseTokenStats | None = None,
) -> T:
    """Agentic variant: let the model call a deterministic query tool a few times
    to verify identifiers against the electrical graph, then call the submit tool.

    Each API call offers `[query_tool, submit_tool]`. While under the query cap,
    `tool_choice={"type":"any"}` lets the model pick query OR submit:

      - **query** → run `query_handler(block.input)` (deterministic, never raises)
        and feed the JSON-encoded result back as a `tool_result`, then loop. The
        handler call is counted against `max_query_turns`.
      - **submit** → validate `block.input` against `output_schema` (with the same
        `_try_unwrap` tolerance as `call_with_forced_tool`). Valid → return. Invalid
        → feed the validation error back as an `is_error` tool_result and retry,
        capped at `max_attempts` submit-validation attempts, then raise.

    Once `max_query_turns` queries have been answered, the request is re-issued
    with `tool_choice={"type":"tool","name":submit_tool_name}` so the next turn is
    a forced submit (no more graph lookups — we've spent the budget).

    **Protocol-miss policy:** a turn that does NOT yield a usable submit while we
    require one (the cap is hit, yet the model returned a query block or no tool
    block at all) is treated like a validation failure — it consumes one of the
    `max_attempts` and we re-request with submit forced. That single counter caps
    the WHOLE non-converging tail (validation failures AND protocol misses), so the
    loop can never spin forever on a recalcitrant model.

    Transport blips on any individual call get the shared in-place retry
    (`_create_with_transport_retry`) — they don't consume an attempt or a query turn.
    """
    convo: list[dict] = list(messages)  # local copy — we grow it as the agent works
    queries_used = 0
    submit_attempts = 0
    post_cap_reroutes = 0  # disguised queries answered AFTER the cap (bounded grace)
    last_error: str | None = None
    tools = [query_tool, submit_tool]

    # Streaming required for max_tokens >= ~16k (the SDK refuses non-stream above
    # the 10-minute bound), same as call_with_forced_tool.
    while True:
        # The query budget gates tool_choice: under it the model is free to query
        # OR submit ("any"); once spent, submit is forced so we always terminate.
        cap_reached = queries_used >= max_query_turns
        if cap_reached:
            tool_choice_param: dict = {"type": "tool", "name": submit_tool_name}
        else:
            tool_choice_param = {"type": "any"}

        stream_kwargs: dict = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=convo,
            tools=tools,
            tool_choice=tool_choice_param,
        )
        response = await _create_with_transport_retry(
            client=client, stream_kwargs=stream_kwargs, log_label=log_label,
        )
        _record_usage(
            response, stats, log_label,
            turn_desc=f"queries={queries_used} attempts={submit_attempts}",
        )

        # The Anthropic API requires a tool_result for EVERY tool_use block of the
        # preceding assistant turn — orphan ONE block and the NEXT request 400s.
        # Opus under tool_choice="any" routinely emits PARALLEL tool calls (two
        # query_graph blocks, or a query + a submit together), so we must collect
        # ALL of them and answer each by its own id — never `next()` a single one.
        all_tool_uses = [b for b in response.content if b.type == "tool_use"]
        submit_use = next(
            (b for b in all_tool_uses if b.name == submit_tool_name), None,
        )
        query_uses = [b for b in all_tool_uses if b.name == query_tool["name"]]

        # --- submit present + VALID: conversation ends, nothing to answer -------
        # A valid submit terminates regardless of any accompanying query blocks —
        # we return the object and never issue another request, so the parallel
        # query blocks can't be orphaned (there is no "next" call to reject them).
        if submit_use is not None:
            # Re-route a misrouted query: under tool_choice="any" the model
            # sometimes calls the SUBMIT tool with a query_graph payload (all keys
            # belong to the query tool). That is a verification, not a failed
            # submit — answer it via the query handler and continue, burning a
            # query turn, not a submit attempt. Allowed freely while the budget is
            # open; allowed for `_POST_CAP_QUERY_REROUTE_GRACE` more turns after
            # the cap, because a reviser on a dense pack often needs one last
            # lookup before it can emit a correct patch — failing it there is what
            # starved the reviser into a no-op. The grace is bounded so a model
            # that ONLY ever misroutes still terminates.
            reroute_ok = _looks_like_query(submit_use.input, query_tool) and (
                not cap_reached or post_cap_reroutes < _POST_CAP_QUERY_REROUTE_GRACE
            )
            if reroute_ok:
                tool_results = _answer_query_blocks(query_uses, query_handler, log_label)
                rerouted = _answer_query_blocks([submit_use], query_handler, log_label)
                tool_results.extend(rerouted)
                queries_used += len(query_uses) + 1
                if cap_reached:
                    post_cap_reroutes += 1
                logger.warning(
                    "[%s] re-routed a query payload mis-sent to %s (op/keys=%s)%s",
                    log_label, submit_tool_name, sorted(submit_use.input),
                    " [post-cap grace]" if cap_reached else "",
                )
                convo.append({"role": "assistant", "content": response.content})
                convo.append({"role": "user", "content": tool_results})
                continue
            submit_attempts += 1
            try:
                return output_schema.model_validate(submit_use.input)
            except ValidationError as exc:
                # Same defensive unwrap as the forced-tool helper: Opus sometimes
                # stringifies nested structures — recover before burning a retry.
                recovered = _try_unwrap(submit_use.input, output_schema)
                if recovered is not None:
                    logger.warning(
                        "[%s] recovered from stringified submit payload (attempt=%d)",
                        log_label, submit_attempts,
                    )
                    return recovered

                last_error = (
                    f"Validation failed for {submit_tool_name} payload:\n{exc}\n"
                    "Payload received: "
                    + json.dumps(submit_use.input, ensure_ascii=False, indent=2)[:2000]
                )
                logger.warning(
                    "[%s] attempt=%d submit validation failed: %s",
                    log_label, submit_attempts, str(exc).replace("\n", " ")[:500],
                )
                if submit_attempts >= max_attempts:
                    break
                # Build a tool_result for EVERY tool_use in the turn: an is_error
                # for the failed submit PLUS a normal handler result for each
                # accompanying query block. Answering only the submit would orphan
                # the query block(s) and 400 the retry. Each answered query also
                # counts against the cap (it really ran).
                tool_results = _answer_query_blocks(
                    query_uses, query_handler, log_label,
                )
                queries_used += len(query_uses)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": submit_use.id,
                    "is_error": True,
                    "content": last_error,
                })
                convo.append({"role": "assistant", "content": response.content})
                convo.append({"role": "user", "content": tool_results})
                continue

        # --- query-only path: honour ALL query blocks while the budget allows it -
        # `not cap_reached` gates on the budget BEFORE this turn; we answer the
        # whole batch even if it crosses the cap mid-way (every block of THIS turn
        # must be answered), and the cap then gates the NEXT request's tool_choice.
        if query_uses and not cap_reached:
            tool_results = _answer_query_blocks(query_uses, query_handler, log_label)
            queries_used += len(query_uses)
            convo.append({"role": "assistant", "content": response.content})
            convo.append({"role": "user", "content": tool_results})
            continue

        # --- protocol miss: no usable submit while one is required --------------
        # The cap is hit (submit is forced) yet the model queried anyway, or it
        # returned no usable tool block at all. Count it like a validation failure
        # (shared `submit_attempts` budget — this is what keeps the whole
        # non-converging tail bounded) and re-request with submit forced.
        submit_attempts += 1
        got = [b.type for b in response.content]
        last_error = (
            f"Expected a '{submit_tool_name}' tool_use, got blocks: {got} "
            f"(query budget {'exhausted' if cap_reached else 'available'})."
        )
        logger.warning("[%s] protocol miss (attempt=%d): %s", log_label, submit_attempts, last_error)
        if submit_attempts >= max_attempts:
            break
        # Force the budget closed so the re-request forces submit deterministically.
        queries_used = max_query_turns
        # CRITICAL: before the plain-text nudge, emit a stub is_error tool_result
        # for EVERY orphaned tool_use in this response. The old code appended the
        # assistant content (which may contain tool_use blocks) followed by a bare
        # TEXT user message → every one of those blocks was orphaned → 400. A
        # tool_use turn may only be followed by a user turn whose FIRST blocks are
        # tool_results covering all of them; the text nudge rides in the same turn.
        stub_results = [
            {
                "type": "tool_result",
                "tool_use_id": b.id,
                "is_error": True,
                "content": (
                    f"Ignored: the query budget is exhausted — you must call "
                    f"{submit_tool_name} now, not {b.name}."
                    if b.name == query_tool["name"]
                    else f"Ignored unexpected tool '{b.name}'. Call {submit_tool_name} now."
                ),
            }
            for b in all_tool_uses
        ]
        nudge_content = stub_results + [{
            "type": "text",
            "text": f"You must now call the {submit_tool_name} tool to finish.",
        }]
        convo.append({"role": "assistant", "content": response.content})
        convo.append({"role": "user", "content": nudge_content})

    raise RuntimeError(
        f"[{log_label}] Failed to produce a valid {submit_tool_name} output after "
        f"{submit_attempts} attempts. Last error:\n{last_error}"
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
