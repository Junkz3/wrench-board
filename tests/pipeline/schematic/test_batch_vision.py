"""Unit tests for api.pipeline.schematic.batch_vision — the -50% vision pass.

No real Anthropic call: `client.messages.batches.*` is stubbed at the SDK
boundary. The critical invariants:

  - each batch request's `params` are the EXACT twin of the direct path's
    first attempt (same system/messages/tools/thinking knobs) — a drift here
    means the cheap pass silently extracts with different quality;
  - chunking keeps every submitted batch under the API's 256 MB body cap
    (base64 PNGs of a 92-page schematic can exceed it);
  - a failed/invalid per-page result is ABSENT from the return value (the
    orchestrator then routes that page through the direct retry machinery),
    never a crash of the whole pass;
  - cancellation / timeout cancel the remote batches (they keep billing
    otherwise).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.pipeline.schematic.batch_vision import (
    _chunk_by_size,
    build_page_request,
    extract_pages_batch,
)
from api.pipeline.schematic.page_vision import (
    SUBMIT_PAGE_TOOL_NAME,
    build_page_vision_params,
    extract_page,
)
from api.pipeline.schematic.renderer import RenderedPage

MODEL = "claude-opus-4-8"


def _rendered(tmp_path: Path, page_number: int = 1, png: bytes = b"\x89PNG\r\n\x1a\n") -> RenderedPage:
    p = tmp_path / f"page-{page_number:02d}.png"
    p.write_bytes(png)
    return RenderedPage(
        page_number=page_number,
        png_path=p,
        orientation="portrait",
        is_scanned=False,
        width_pt=595.0,
        height_pt=842.0,
    )


def _valid_payload(page: int) -> dict:
    return {
        "schema_version": "1.0",
        "page": page,
        "page_kind": "schematic",
        "orientation": "portrait",
        "confidence": 0.9,
        "nodes": [],
        "nets": [],
        "cross_page_refs": [],
        "typed_edges": [],
        "designer_notes": [],
        "ambiguities": [],
    }


def _succeeded_entry(page: int, payload: dict | None = None, *, tool: bool = True):
    content = []
    if tool:
        content.append(
            SimpleNamespace(
                type="tool_use",
                name=SUBMIT_PAGE_TOOL_NAME,
                input=payload if payload is not None else _valid_payload(page),
                id=f"toolu_{page}",
            )
        )
    else:
        content.append(SimpleNamespace(type="thinking", thinking="…"))
    message = SimpleNamespace(content=content)
    return SimpleNamespace(
        custom_id=f"page_{page:03d}",
        result=SimpleNamespace(type="succeeded", message=message),
    )


def _errored_entry(page: int):
    return SimpleNamespace(
        custom_id=f"page_{page:03d}",
        result=SimpleNamespace(type="errored", error=SimpleNamespace(type="api_error")),
    )


class _AsyncIterList:
    """Mimics the SDK's AsyncJSONLDecoder — plain async iteration."""

    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        self._it = iter(self._items)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


def _batch_client(
    *,
    entries_by_batch: dict[str, list] | None = None,
    statuses: list[str] | None = None,
):
    """Stub of AsyncAnthropic limited to `messages.batches.*`.

    `statuses` is consumed one retrieve() at a time (last value repeats), so a
    test can model in_progress → ended without real waiting.
    """
    entries_by_batch = entries_by_batch or {}
    statuses = list(statuses or ["ended"])
    created: list[dict] = []
    counter = {"n": 0}

    async def _create(*, requests):
        counter["n"] += 1
        bid = f"msgbatch_{counter['n']}"
        created.append({"id": bid, "requests": list(requests)})
        if bid not in entries_by_batch:
            entries_by_batch[bid] = []
        return SimpleNamespace(id=bid, processing_status="in_progress")

    retrieve_calls = {"n": 0}

    async def _retrieve(bid):
        retrieve_calls["n"] += 1
        idx = min(retrieve_calls["n"] - 1, len(statuses) - 1)
        return SimpleNamespace(
            id=bid,
            processing_status=statuses[idx],
            request_counts=SimpleNamespace(
                processing=0, succeeded=len(entries_by_batch.get(bid, [])),
                errored=0, canceled=0, expired=0,
            ),
        )

    async def _results(bid):
        return _AsyncIterList(entries_by_batch.get(bid, []))

    cancel = AsyncMock()
    batches = SimpleNamespace(
        create=AsyncMock(side_effect=_create),
        retrieve=AsyncMock(side_effect=_retrieve),
        results=AsyncMock(side_effect=_results),
        cancel=cancel,
    )
    client = SimpleNamespace(messages=SimpleNamespace(batches=batches))
    return client, created, cancel, retrieve_calls


# --------------------------------------------------------------------------
# Request construction
# --------------------------------------------------------------------------


def test_build_page_request_shape(tmp_path):
    rp = _rendered(tmp_path, 7)
    req = build_page_request(
        model=MODEL, rendered=rp, total_pages=12, device_label="MNT", grounding=None
    )
    assert req["custom_id"] == "page_007"
    params = req["params"]
    assert params["model"] == MODEL
    assert params["tool_choice"] == {"type": "auto"}
    assert params["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert params["output_config"] == {"effort": "xhigh"}
    assert params["tools"][0]["name"] == SUBMIT_PAGE_TOOL_NAME
    assert params["tools"][0]["cache_control"] == {"type": "ephemeral"}
    blocks = params["messages"][0]["content"]
    assert any(b.get("type") == "image" for b in blocks)


@pytest.mark.asyncio
async def test_params_are_twin_of_direct_path(tmp_path):
    """The batch params must mirror the direct path's first attempt exactly.

    Captures the kwargs `extract_page` hands the SDK stream and diffs them
    against `build_page_vision_params` — any knob drift fails here.
    """
    rp = _rendered(tmp_path, 3)
    payload = _valid_payload(3)

    captured = {}

    class _Ctx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get_final_message(self):
            tool_use = SimpleNamespace(
                type="tool_use", name=SUBMIT_PAGE_TOOL_NAME, input=payload, id="t"
            )
            usage = SimpleNamespace(
                input_tokens=1, output_tokens=1,
                cache_read_input_tokens=0, cache_creation_input_tokens=0,
            )
            return SimpleNamespace(content=[tool_use], usage=usage, model=MODEL)

    def _stream(**kwargs):
        captured.update(kwargs)
        return _Ctx()

    client = SimpleNamespace(messages=SimpleNamespace(stream=MagicMock(side_effect=_stream)))
    await extract_page(
        client=client, model=MODEL, rendered=rp, total_pages=12,
        device_label="MNT", grounding="GROUNDING BLOCK",
    )

    batch_params = build_page_vision_params(
        model=MODEL, rendered=rp, total_pages=12,
        device_label="MNT", grounding="GROUNDING BLOCK",
    )
    assert set(captured) == set(batch_params)
    for key in batch_params:
        assert captured[key] == batch_params[key], f"param drift on {key!r}"


def test_chunk_by_size_splits_oversized(tmp_path):
    rp_big = _rendered(tmp_path, 1, png=b"x" * 1000)
    reqs = [
        build_page_request(model=MODEL, rendered=rp_big, total_pages=3,
                           device_label=None, grounding=None)
        for _ in range(3)
    ]
    one_size = len(json.dumps(reqs[0]["params"]))
    chunks = _chunk_by_size(reqs, max_bytes=one_size + 10)
    assert [len(c) for c in chunks] == [1, 1, 1]
    # generous cap → single chunk
    assert [len(c) for c in _chunk_by_size(reqs, max_bytes=one_size * 10)] == [3]


# --------------------------------------------------------------------------
# extract_pages_batch
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_validated_graphs(tmp_path):
    pages = [_rendered(tmp_path, n) for n in (1, 2)]
    # The model emitted the WRONG page number on page 2 — the canonical
    # override must fix it, same guarantee as the direct path.
    wrong = _valid_payload(99)
    entries = {"msgbatch_1": [_succeeded_entry(1), _succeeded_entry(2, wrong)]}
    client, created, cancel, _ = _batch_client(entries_by_batch=entries)

    out = await extract_pages_batch(
        client=client, model=MODEL, pages=pages, total_pages=2,
        device_label="MNT", groundings=[None, None],
        poll_seconds=0.0, timeout_seconds=5.0,
    )
    assert sorted(out) == [1, 2]
    assert out[1].page == 1
    assert out[2].page == 2  # canonical override applied
    assert [r["custom_id"] for r in created[0]["requests"]] == ["page_001", "page_002"]
    cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_errored_and_invalid_entries_are_absent(tmp_path):
    pages = [_rendered(tmp_path, n) for n in (1, 2, 3)]
    entries = {
        "msgbatch_1": [
            _succeeded_entry(1),
            _errored_entry(2),
            _succeeded_entry(3, {"garbage": True}),  # fails validation, not unwrappable
        ]
    }
    client, _, _, _ = _batch_client(entries_by_batch=entries)
    out = await extract_pages_batch(
        client=client, model=MODEL, pages=pages, total_pages=3,
        device_label=None, groundings=[None, None, None],
        poll_seconds=0.0, timeout_seconds=5.0,
    )
    assert sorted(out) == [1]


@pytest.mark.asyncio
async def test_thinking_only_miss_is_absent(tmp_path):
    pages = [_rendered(tmp_path, 1)]
    entries = {"msgbatch_1": [_succeeded_entry(1, tool=False)]}
    client, _, _, _ = _batch_client(entries_by_batch=entries)
    out = await extract_pages_batch(
        client=client, model=MODEL, pages=pages, total_pages=1,
        device_label=None, groundings=[None],
        poll_seconds=0.0, timeout_seconds=5.0,
    )
    assert out == {}


@pytest.mark.asyncio
async def test_polls_until_ended(tmp_path):
    pages = [_rendered(tmp_path, 1)]
    entries = {"msgbatch_1": [_succeeded_entry(1)]}
    client, _, _, retrieve_calls = _batch_client(
        entries_by_batch=entries,
        statuses=["in_progress", "in_progress", "ended"],
    )
    out = await extract_pages_batch(
        client=client, model=MODEL, pages=pages, total_pages=1,
        device_label=None, groundings=[None],
        poll_seconds=0.0, timeout_seconds=5.0,
    )
    assert sorted(out) == [1]
    assert retrieve_calls["n"] == 3


@pytest.mark.asyncio
async def test_timeout_cancels_and_raises(tmp_path):
    pages = [_rendered(tmp_path, 1)]
    client, _, cancel, _ = _batch_client(statuses=["in_progress"])
    with pytest.raises(RuntimeError, match="timed out"):
        await extract_pages_batch(
            client=client, model=MODEL, pages=pages, total_pages=1,
            device_label=None, groundings=[None],
            poll_seconds=0.0, timeout_seconds=0.0,
        )
    cancel.assert_awaited()


@pytest.mark.asyncio
async def test_cancellation_cancels_remote_batches(tmp_path):
    pages = [_rendered(tmp_path, 1)]
    client, _, cancel, _ = _batch_client(statuses=["in_progress"])

    task = asyncio.ensure_future(
        extract_pages_batch(
            client=client, model=MODEL, pages=pages, total_pages=1,
            device_label=None, groundings=[None],
            poll_seconds=10.0, timeout_seconds=600.0,
        )
    )
    await asyncio.sleep(0.05)  # let it submit + enter the poll sleep
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cancel.assert_awaited()


@pytest.mark.asyncio
async def test_oversized_pages_submit_multiple_batches(tmp_path):
    big = b"x" * 50_000
    pages = [_rendered(tmp_path, n, png=big) for n in (1, 2)]
    entries = {
        "msgbatch_1": [_succeeded_entry(1)],
        "msgbatch_2": [_succeeded_entry(2)],
    }
    client, created, _, _ = _batch_client(entries_by_batch=entries)
    out = await extract_pages_batch(
        client=client, model=MODEL, pages=pages, total_pages=2,
        device_label=None, groundings=[None, None],
        poll_seconds=0.0, timeout_seconds=5.0,
        max_batch_bytes=80_000,  # each ~67k b64 → one page per batch
    )
    assert len(created) == 2
    assert sorted(out) == [1, 2]
