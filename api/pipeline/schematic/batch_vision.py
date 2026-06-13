"""Batch-mode vision pass — the same per-page Opus calls at 50% price.

Routes the per-page schematic vision calls through the Anthropic Message
Batches API (`POST /v1/messages/batches`): one request per uncached page,
asynchronous completion (usually <1h, hard-bounded at 24h by the API), half
the token price on input AND output. Same model, same prompt, same knobs —
the request params are built by `page_vision.build_page_vision_params`, the
shared twin of the direct path's first attempt, and a parity test pins them
together.

Designed for offline catalogue pre-builds (operator flag
`PIPELINE_VISION_BATCH`), not for tenant-facing builds where someone watches
the timeline. Failure policy: a page whose batch entry errored / expired /
failed validation is simply ABSENT from the returned mapping — the
orchestrator writes the successful pages to the per-page cache and lets the
existing direct path (with its full validation-retry + thinking-fallback
machinery) re-run the stragglers at full price. The whole pass never
half-succeeds silently.

Cost-safety: a submitted batch keeps billing whether or not we wait for it,
so cancellation (asyncio) and the poll timeout both best-effort cancel the
remote batches before propagating.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from pydantic import ValidationError

from api.config import get_settings
from api.pipeline.schematic.page_vision import (
    SUBMIT_PAGE_TOOL_NAME,
    build_page_vision_params,
    ensure_canonical_page,
)
from api.pipeline.schematic.schemas import SchematicPageGraph
from api.pipeline.tool_call import _try_unwrap

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

    from api.pipeline.schematic.renderer import RenderedPage

logger = logging.getLogger("wrench_board.pipeline.schematic.batch_vision")

_CUSTOM_ID_PREFIX = "page_"


def build_page_request(
    *,
    model: str,
    rendered: RenderedPage,
    total_pages: int,
    device_label: str | None,
    grounding: str | None,
) -> dict:
    """One Batches-API request entry for one page.

    `custom_id` carries the page number — batch results are NOT returned in
    submission order, it is the only way to re-associate them.
    """
    return {
        "custom_id": f"{_CUSTOM_ID_PREFIX}{rendered.page_number:03d}",
        "params": build_page_vision_params(
            model=model,
            rendered=rendered,
            total_pages=total_pages,
            device_label=device_label,
            grounding=grounding,
        ),
    }


def _chunk_by_size(requests: list[dict], max_bytes: int) -> list[list[dict]]:
    """Greedy split so each submitted batch stays under the API's body cap.

    Size is estimated from the serialised params — dominated by the base64
    PNG, so the estimate tracks the real wire size closely. A single
    oversized request still goes out alone (the API will reject it with a
    clear error rather than us silently dropping the page).
    """
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_size = 0
    for req in requests:
        size = len(json.dumps(req["params"], ensure_ascii=False))
        if current and current_size + size > max_bytes:
            chunks.append(current)
            current, current_size = [], 0
        current.append(req)
        current_size += size
    if current:
        chunks.append(current)
    return chunks


def _parse_entry(entry) -> tuple[int, SchematicPageGraph | None]:
    """Decode one batch result entry → (page_number, graph-or-None).

    Mirrors the direct path's tolerance: missing tool_use (thinking-only
    miss) and validation failures first go through `_try_unwrap` (the
    stringified-payload recovery), then give up with a warning — the page
    falls back to the direct retry machinery, never crashes the pass.
    """
    page_number = int(entry.custom_id.removeprefix(_CUSTOM_ID_PREFIX))
    rtype = entry.result.type
    if rtype != "succeeded":
        logger.warning(
            "[batch_vision] page %d entry %s — falling back to direct call",
            page_number,
            rtype,
        )
        return page_number, None

    message = entry.result.message
    tool_use = next(
        (
            b
            for b in message.content
            if b.type == "tool_use" and b.name == SUBMIT_PAGE_TOOL_NAME
        ),
        None,
    )
    if tool_use is None:
        got = [b.type for b in message.content]
        logger.warning(
            "[batch_vision] page %d: no %s tool_use (got %s) — direct fallback",
            page_number,
            SUBMIT_PAGE_TOOL_NAME,
            got,
        )
        return page_number, None

    try:
        graph = SchematicPageGraph.model_validate(tool_use.input)
    except ValidationError as exc:
        recovered = _try_unwrap(tool_use.input, SchematicPageGraph)
        if recovered is None:
            logger.warning(
                "[batch_vision] page %d failed validation (%s) — direct fallback",
                page_number,
                str(exc).replace("\n", " ")[:300],
            )
            return page_number, None
        logger.warning(
            "[batch_vision] page %d recovered from stringified payload",
            page_number,
        )
        graph = recovered
    return page_number, ensure_canonical_page(graph, page_number)


async def _cancel_batches(client: AsyncAnthropic, batch_ids: list[str]) -> None:
    """Best-effort remote cancel — an orphan batch keeps billing."""
    for bid in batch_ids:
        try:
            await client.messages.batches.cancel(bid)
            logger.info("[batch_vision] cancelled batch %s", bid)
        except Exception:  # noqa: BLE001 — cancellation is best-effort
            logger.warning("[batch_vision] could not cancel batch %s", bid, exc_info=True)


async def extract_pages_batch(
    *,
    client: AsyncAnthropic,
    model: str,
    pages: list[RenderedPage],
    total_pages: int,
    device_label: str | None,
    groundings: list[str | None],
    poll_seconds: float | None = None,
    timeout_seconds: float | None = None,
    max_batch_bytes: int | None = None,
) -> dict[int, SchematicPageGraph]:
    """Run the vision pass for `pages` through the Message Batches API.

    `groundings` is positionally aligned with `pages`. Returns a mapping
    page_number → validated graph; a page absent from the mapping failed
    inside the batch and must be re-run by the caller (direct path).

    Raises RuntimeError when the batches do not end within
    `timeout_seconds` (remote batches are cancelled first), and re-raises
    `asyncio.CancelledError` after cancelling the remote batches.
    """
    settings = get_settings()
    if poll_seconds is None:
        poll_seconds = settings.pipeline_vision_batch_poll_seconds
    if timeout_seconds is None:
        timeout_seconds = settings.pipeline_vision_batch_timeout_seconds
    if max_batch_bytes is None:
        max_batch_bytes = settings.pipeline_vision_batch_max_bytes

    requests = [
        build_page_request(
            model=model,
            rendered=rp,
            total_pages=total_pages,
            device_label=device_label,
            grounding=groundings[i],
        )
        for i, rp in enumerate(pages)
    ]
    chunks = _chunk_by_size(requests, max_batch_bytes)
    logger.info(
        "[batch_vision] submitting %d page(s) in %d batch(es) (model=%s)",
        len(requests),
        len(chunks),
        model,
    )

    batch_ids: list[str] = []
    try:
        for chunk in chunks:
            batch = await client.messages.batches.create(requests=chunk)
            batch_ids.append(batch.id)
            logger.info(
                "[batch_vision] batch %s submitted (%d pages)", batch.id, len(chunk)
            )

        # Poll every batch until ended. The loop clock is monotonic via the
        # event loop; the deadline cancels stragglers so an API-side stall
        # can't keep the build (and the billing) open forever.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        pending = set(batch_ids)
        while pending:
            for bid in sorted(pending):
                b = await client.messages.batches.retrieve(bid)
                counts = getattr(b, "request_counts", None)
                if counts is not None:
                    logger.info(
                        "[batch_vision] %s status=%s processing=%s succeeded=%s "
                        "errored=%s",
                        bid,
                        b.processing_status,
                        counts.processing,
                        counts.succeeded,
                        counts.errored,
                    )
                if b.processing_status == "ended":
                    pending.discard(bid)
            if not pending:
                break
            if loop.time() >= deadline:
                await _cancel_batches(client, sorted(pending))
                raise RuntimeError(
                    f"[batch_vision] timed out after {timeout_seconds:.0f}s "
                    f"waiting for batch(es) {sorted(pending)}"
                )
            await asyncio.sleep(poll_seconds)
    except asyncio.CancelledError:
        # The build was cancelled (engine-side cancel or shutdown) — the
        # remote batches would keep billing if left running.
        await _cancel_batches(client, batch_ids)
        raise

    out: dict[int, SchematicPageGraph] = {}
    for bid in batch_ids:
        decoder = await client.messages.batches.results(bid)
        async for entry in decoder:
            page_number, graph = _parse_entry(entry)
            if graph is not None:
                out[page_number] = graph
    logger.info(
        "[batch_vision] %d/%d page(s) extracted via batch", len(out), len(requests)
    )
    return out
