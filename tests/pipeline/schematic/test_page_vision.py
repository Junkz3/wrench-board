"""Unit tests for api.pipeline.schematic.page_vision.

These do NOT hit the real Anthropic API. The client is mocked at the
`messages.create()` boundary; we assert the constructed tool schema, the
image attachment, and the `page` override that guards against the model
emitting a wrong page number in its payload.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.pipeline.schematic.page_vision import (
    SUBMIT_PAGE_TOOL_NAME,
    extract_page,
)
from api.pipeline.schematic.renderer import RenderedPage


class _MockStreamCtx:
    """Async context manager mimicking `client.messages.stream(...)`.

    The real SDK returns an object that exposes `get_final_message()` which
    resolves to the same shape as `messages.create()`'s return. For tests we
    just hand back the pre-built response.
    """

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get_final_message(self):
        return self._response


def _stream_client(response):
    """Build a stub client whose `messages.stream(...)` returns our ctx.

    Wraps the constructor in a MagicMock so tests can assert `call_args`.
    """
    ctx_factory = MagicMock(side_effect=lambda **_: _MockStreamCtx(response))
    return SimpleNamespace(messages=SimpleNamespace(stream=ctx_factory)), ctx_factory


def _build_mock_response(page_number: int = 3, input_payload: dict | None = None):
    """Craft a SimpleNamespace mimicking an anthropic.types.Message."""
    if input_payload is None:
        input_payload = {
            "schema_version": "1.0",
            "page": page_number,
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
    tool_use = SimpleNamespace(
        type="tool_use",
        name=SUBMIT_PAGE_TOOL_NAME,
        input=input_payload,
        id="toolu_stub",
    )
    usage = SimpleNamespace(
        input_tokens=12000,
        output_tokens=800,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(content=[tool_use], usage=usage)


@pytest.fixture
def png_file(tmp_path: Path) -> Path:
    # Smallest valid PNG (1x1 transparent) so the file read + base64 path
    # exercises without requiring a real rendered page.
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452"
        "00000001000000010806000000"
        "1f15c4890000000a49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
    )
    path = tmp_path / "page_003.png"
    path.write_bytes(png_bytes)
    return path


@pytest.fixture
def rendered(png_file: Path) -> RenderedPage:
    return RenderedPage(
        page_number=3,
        png_path=png_file,
        orientation="portrait",
        is_scanned=False,
        width_pt=595.0,
        height_pt=842.0,
    )


@pytest.mark.asyncio
async def test_extract_page_validates_and_returns_schematic_page_graph(
    rendered: RenderedPage,
):
    mock_client, _ = _stream_client(_build_mock_response())
    graph = await extract_page(
        client=mock_client,
        model="claude-opus-4-7",
        rendered=rendered,
        total_pages=12,
        device_label="MNT Reform v2.5",
    )
    assert graph.page == 3
    assert graph.orientation == "portrait"
    assert graph.confidence == 0.9


@pytest.mark.asyncio
async def test_extract_page_registers_the_submit_schematic_page_tool(
    rendered: RenderedPage,
):
    mock_client, stream_mock = _stream_client(_build_mock_response())

    await extract_page(
        client=mock_client,
        model="claude-opus-4-7",
        rendered=rendered,
        total_pages=12,
    )

    kwargs = stream_mock.call_args.kwargs
    # tool_choice is `auto` (not `tool`) since we run extended thinking on
    # the vision call; Anthropic rejects forced tool use alongside thinking.
    # Registering a single tool still guarantees the model emits the
    # structured payload — auto + 1 tool is functionally equivalent here.
    assert kwargs["tool_choice"] == {"type": "auto"}
    tool_defs = kwargs["tools"]
    assert len(tool_defs) == 1
    assert tool_defs[0]["name"] == SUBMIT_PAGE_TOOL_NAME
    assert tool_defs[0]["input_schema"]["type"] == "object"


@pytest.mark.asyncio
async def test_extract_page_sends_the_png_as_base64_image_block(
    rendered: RenderedPage,
):
    mock_client, stream_mock = _stream_client(_build_mock_response())

    await extract_page(
        client=mock_client,
        model="claude-opus-4-7",
        rendered=rendered,
        total_pages=12,
    )

    (user_msg,) = stream_mock.call_args.kwargs["messages"]
    image_blocks = [b for b in user_msg["content"] if b["type"] == "image"]
    assert len(image_blocks) == 1
    source = image_blocks[0]["source"]
    assert source["type"] == "base64"
    assert source["media_type"] == "image/png"
    assert isinstance(source["data"], str) and len(source["data"]) > 0


@pytest.mark.asyncio
async def test_extract_page_overrides_wrong_page_number_from_model(
    rendered: RenderedPage,
):
    bad_payload = {
        "schema_version": "1.0",
        "page": 99,  # model lied — canonical is 3
        "page_kind": "schematic",
        "orientation": "portrait",
        "confidence": 1.0,
        "nodes": [],
        "nets": [],
        "cross_page_refs": [],
        "typed_edges": [],
        "designer_notes": [],
        "ambiguities": [],
    }
    mock_client, _ = _stream_client(_build_mock_response(input_payload=bad_payload))
    graph = await extract_page(
        client=mock_client,
        model="claude-opus-4-7",
        rendered=rendered,
        total_pages=12,
    )
    assert graph.page == 3


@pytest.mark.asyncio
async def test_extract_page_mentions_scanned_hint_when_flagged(
    png_file: Path,
):
    scanned = RenderedPage(
        page_number=7,
        png_path=png_file,
        orientation="portrait",
        is_scanned=True,
        width_pt=595.0,
        height_pt=842.0,
    )
    mock_client, stream_mock = _stream_client(_build_mock_response(page_number=7))

    await extract_page(
        client=mock_client,
        model="claude-opus-4-7",
        rendered=scanned,
        total_pages=12,
    )

    (user_msg,) = stream_mock.call_args.kwargs["messages"]
    text_blocks = "\n".join(
        b["text"] for b in user_msg["content"] if b["type"] == "text"
    )
    assert "rasterised" in text_blocks.lower() or "scan" in text_blocks.lower()
