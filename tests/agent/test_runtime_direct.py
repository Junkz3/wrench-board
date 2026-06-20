"""Targeted unit tests for `api.agent.runtime_direct`.

These tests fill gaps left by the existing coverage:

  - `tests/agent/test_ws_flow.py` exercises the streaming + tool-use loop with a
    `FakeWS` double, but doesn't probe model selection by tier, the kwargs
    forwarded to `messages.stream`, the on-disk JSONL persistence, or the
    AsyncAnthropic resilience knob.
  - `tests/agent/test_diagnostic_ws_e2e.py` covers the full TestClient WS
    round-trip but stops short of asserting model identity, tool result content
    flow back into the prompt, or what happens when the SDK raises mid-stream.

Everything here is fast, deterministic, and uses mocks for `AsyncAnthropic`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.board.model import Board, Layer, Part, Pin, Point
from api.session.state import SessionState

# ---------------------------------------------------------------------------
# Doubles — kept self-contained. We re-implement (not import) the FakeWS /
# FakeStream pair so this file stays decoupled from the helpers in
# test_ws_flow.py / test_diagnostic_ws_e2e.py (those are private to their
# modules and could change shape).
# ---------------------------------------------------------------------------


class FakeWS:
    """Minimal WebSocket double that captures every send_json payload."""

    def __init__(self, user_messages: list[str] | None = None) -> None:
        self.sent: list[dict] = []
        self._inbox: asyncio.Queue[str] = asyncio.Queue()
        for m in user_messages or []:
            self._inbox.put_nowait(json.dumps({"type": "message", "text": m}))
        self._closed = False

    async def accept(self) -> None:
        return

    async def close(self) -> None:
        self._closed = True

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def receive_text(self) -> str:
        if self._closed or self._inbox.empty():
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect
        return await self._inbox.get()


class _FakeStream:
    """Async context manager + iterator mirroring `client.messages.stream`."""

    def __init__(self, events: list[tuple], final_message: MagicMock) -> None:
        self._events = list(events)
        self._final = final_message
        self._snapshot_content: list = []

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        event, new_snapshot_content = self._events.pop(0)
        self._snapshot_content = new_snapshot_content
        return event

    @property
    def current_message_snapshot(self) -> SimpleNamespace:
        return SimpleNamespace(content=list(self._snapshot_content))

    async def get_final_message(self) -> MagicMock:
        return self._final


_FAKE_USAGE = SimpleNamespace(
    input_tokens=10,
    output_tokens=5,
    cache_read_input_tokens=0,
    cache_creation_input_tokens=0,
)


def _stream_text(text: str) -> tuple[list[tuple], MagicMock]:
    block = MagicMock(type="text", text=text)
    stop = MagicMock()
    stop.type = "content_block_stop"
    stop.index = 0
    final = MagicMock(content=[block], stop_reason="end_turn", usage=_FAKE_USAGE)
    return [(stop, [block])], final


def _stream_tool_use(
    name: str, tool_input: dict, tool_id: str = "toolu_1"
) -> tuple[list[tuple], MagicMock]:
    block = MagicMock(type="tool_use", input=tool_input, id=tool_id)
    block.name = name
    final = MagicMock(content=[block], stop_reason="tool_use", usage=_FAKE_USAGE)
    return [], final


def _board_with_u7() -> Board:
    return Board(
        board_id="t",
        file_hash="sha256:x",
        source_format="t",
        outline=[],
        parts=[
            Part(
                refdes="U7", layer=Layer.TOP, is_smd=True,
                bbox=(Point(x=0, y=0), Point(x=10, y=10)), pin_refs=[0, 1],
            )
        ],
        pins=[
            Pin(part_refdes="U7", index=1, pos=Point(x=2, y=2), layer=Layer.TOP),
            Pin(part_refdes="U7", index=2, pos=Point(x=8, y=8), layer=Layer.TOP),
        ],
        nets=[],
        nails=[],
    )


def _stub_session(monkeypatch: pytest.MonkeyPatch, board: Board | None = None) -> None:
    def _from_device(_slug: str) -> SessionState:
        s = SessionState()
        if board is not None:
            s.set_board(board)
        return s
    monkeypatch.setattr(
        "api.agent.runtime_direct.SessionState.from_device",
        staticmethod(_from_device),
    )


def _patch_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    api_key: str = "sk-fake",
    memory_root: str | Path = "/tmp/runtime-direct-tests",
    max_retries: int = 5,
) -> MagicMock:
    """Patch `get_settings` and return the underlying MagicMock for assertions."""
    import api.agent.runtime_direct as rt
    settings = MagicMock(
        anthropic_api_key=api_key,
        memory_root=str(memory_root),
        anthropic_model_main="claude-opus-4-8",
        anthropic_max_retries=max_retries,
        chat_history_backend="jsonl",
        ma_stream_event_timeout_seconds=600.0,
    )
    monkeypatch.setattr(rt, "get_settings", lambda: settings)
    # chat_history.get_settings() is also called when persisting JSONL —
    # patch it on that module too so the backend check resolves to "jsonl".
    import api.agent.chat_history as ch
    monkeypatch.setattr(ch, "get_settings", lambda: settings)
    return settings


def _install_stream_recorder(
    monkeypatch: pytest.MonkeyPatch,
    scripted: list[tuple[list[tuple], MagicMock]],
) -> tuple[MagicMock, list[dict]]:
    """Install a fake AsyncAnthropic, return (client, kwargs_log).

    The kwargs_log accumulates one dict per `messages.stream(**kwargs)` call so
    tests can assert model identity, tool count, system prompt presence, etc.
    """
    import api.agent.runtime_direct as rt

    iterator = iter(scripted)
    kwargs_log: list[dict] = []
    client = MagicMock()

    def _stream_factory(**kwargs):
        kwargs_log.append(dict(kwargs))
        events, final = next(iterator)
        return _FakeStream(events, final)

    client.messages.stream = _stream_factory

    constructor_kwargs: dict = {}

    def _factory(**kw):
        constructor_kwargs.update(kw)
        return client

    monkeypatch.setattr(rt, "AsyncAnthropic", _factory)
    client._constructor_kwargs = constructor_kwargs  # type: ignore[attr-defined]
    return client, kwargs_log


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_missing_api_key_emits_error_and_closes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """No API key → error frame, WS closed, no Anthropic client constructed."""
    _stub_session(monkeypatch)
    _patch_settings(monkeypatch, api_key="", memory_root=tmp_path)

    import api.agent.runtime_direct as rt
    constructed: list[bool] = []

    def _factory(**_kw):
        constructed.append(True)
        return MagicMock()
    monkeypatch.setattr(rt, "AsyncAnthropic", _factory)

    ws = FakeWS([])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    assert ws._closed is True
    assert ws.sent and ws.sent[0]["type"] == "error"
    assert ws.sent[0]["code"] == "missing_api_key"
    # The bail-out path is hit before AsyncAnthropic(...) is invoked.
    assert constructed == []


@pytest.mark.parametrize(
    "tier,expected_model",
    [
        ("fast", "claude-haiku-4-5"),
        ("normal", "claude-sonnet-4-6"),
        ("deep", "claude-opus-4-8"),
    ],
)
async def test_tier_query_param_picks_the_right_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    tier: str, expected_model: str,
) -> None:
    """The tier→model table in run_diagnostic_session_direct is the public
    contract that the WS query param drives. Each value must hit the matching
    Anthropic model identifier."""
    _stub_session(monkeypatch)
    _patch_settings(monkeypatch, memory_root=tmp_path)
    _client, kwargs_log = _install_stream_recorder(
        monkeypatch, [_stream_text("ok")]
    )

    import api.agent.runtime_direct as rt
    ws = FakeWS(["hello"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier=tier)

    assert kwargs_log, "stream() was never called"
    assert kwargs_log[0]["model"] == expected_model
    # session_ready also surfaces the resolved model so the frontend can
    # display it to the tech.
    ready = next(m for m in ws.sent if m.get("type") == "session_ready")
    assert ready["model"] == expected_model


async def test_unknown_tier_falls_back_to_model_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """`tier_to_model.get(tier, settings.anthropic_model_main)` — a tier outside
    the table degrades to the configured `anthropic_model_main`."""
    _stub_session(monkeypatch)
    settings = _patch_settings(monkeypatch, memory_root=tmp_path)
    settings.anthropic_model_main = "claude-opus-4-8"
    _client, kwargs_log = _install_stream_recorder(
        monkeypatch, [_stream_text("ok")]
    )

    import api.agent.runtime_direct as rt
    ws = FakeWS(["go"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="bogus-tier")

    assert kwargs_log[0]["model"] == "claude-opus-4-8"


async def test_anthropic_client_built_with_max_retries_from_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Resilience: the AsyncAnthropic constructor must receive the configured
    `anthropic_max_retries` so transient 5xx / 529 are retried by the SDK
    rather than bubbling on the first hiccup."""
    _stub_session(monkeypatch)
    _patch_settings(monkeypatch, memory_root=tmp_path, max_retries=7)
    client, _kwargs = _install_stream_recorder(
        monkeypatch, [_stream_text("ok")]
    )

    import api.agent.runtime_direct as rt
    ws = FakeWS(["ping"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    constructor_kwargs = client._constructor_kwargs  # type: ignore[attr-defined]
    assert constructor_kwargs.get("max_retries") == 7
    assert constructor_kwargs.get("api_key") == "sk-fake"


async def test_mb_tool_dispatch_feeds_result_back_into_next_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Full mb_* tool-use cycle: agent calls mb_list_measurements (a tool that
    returns clean structured data without needing a knowledge pack on disk),
    then on the SECOND turn the runtime must feed the JSON-encoded result back
    as a tool_result block in the messages list passed to messages.stream."""
    _stub_session(monkeypatch, _board_with_u7())
    _patch_settings(monkeypatch, memory_root=tmp_path)

    import api.agent.runtime_direct as rt

    iter_scripted = iter([
        _stream_tool_use(
            "mb_list_measurements", {"target": "PP3V3"}, tool_id="toolu_meas1"
        ),
        _stream_text("No measurements yet."),
    ])
    captured: list[list[dict]] = []
    client = MagicMock()

    def _stream_factory(**kwargs):
        captured.append(list(kwargs["messages"]))
        events, final = next(iter_scripted)
        return _FakeStream(events, final)
    client.messages.stream = _stream_factory
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda **_kw: client)

    ws = FakeWS(["any measurements?"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    # 2 stream calls: first emits tool_use, second receives the tool_result.
    assert len(captured) == 2, f"expected 2 stream calls, saw {len(captured)}"
    second = captured[1]
    tool_result_blocks = [
        block
        for msg in second
        for block in (msg.get("content") if isinstance(msg.get("content"), list) else [])
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert tool_result_blocks, "tool_result was never appended to the next turn"
    decoded = json.loads(tool_result_blocks[0]["content"])
    # mb_list_measurements returns {"found": True, "measurements": []} on
    # an empty repair — proves the tool actually ran rather than being
    # short-circuited. The runtime still strips top-level 'event'/'events'
    # keys before forwarding to the agent (those are reserved for WS-emit);
    # 'measurements' is intentionally not in that reserved set so it makes
    # it through.
    assert decoded.get("found") is True
    assert decoded.get("measurements") == []
    # tool_use_id must round-trip so the agent can correlate the result.
    assert tool_result_blocks[0]["tool_use_id"] == "toolu_meas1"
    # The tool_use frame must also have hit the WS for the frontend to
    # surface it in the activity log.
    types = [m.get("type") for m in ws.sent]
    assert "tool_use" in types


async def test_unknown_tool_name_returns_unknown_tool_to_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A tool name that doesn't match any mb_*/bv_*/profile_* prefix lands in
    _dispatch_mb_tool's else branch and returns the structured-null
    {'ok': False, 'reason': 'unknown-tool'}. The agent gets that on the next
    turn — never a fabricated success."""
    _stub_session(monkeypatch)
    _patch_settings(monkeypatch, memory_root=tmp_path)

    import api.agent.runtime_direct as rt
    captured: list[list[dict]] = []

    iter_scripted = iter([
        _stream_tool_use("mb_phantom_tool", {"foo": "bar"}),
        _stream_text("Tool was unknown."),
    ])
    client = MagicMock()

    def _stream_factory(**kwargs):
        captured.append(list(kwargs["messages"]))
        events, final = next(iter_scripted)
        return _FakeStream(events, final)
    client.messages.stream = _stream_factory
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda **_kw: client)

    ws = FakeWS(["call something weird"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    assert len(captured) == 2
    tool_results = [
        block
        for msg in captured[1]
        for block in (msg.get("content") if isinstance(msg.get("content"), list) else [])
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    decoded = json.loads(tool_results[0]["content"])
    assert decoded.get("ok") is False
    assert decoded.get("reason") == "unknown-tool"


async def test_stock_tool_dispatch_runs_and_feeds_result_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Regression: stock_* tools must dispatch in direct mode (the prod default).

    Before the fix they fell into _dispatch_mb_tool's else branch and came back
    as {'ok': False, 'reason': 'unknown-tool'} — the agent reported success to
    the tech but nothing was written. Here we script a stock_mark_donor call and
    assert the runtime actually ran the stock tool (created donor_id returned),
    not the unknown-tool fallback.
    """
    _stub_session(monkeypatch)
    _patch_settings(monkeypatch, memory_root=tmp_path)
    # The stock package reads get_settings().memory_root through its own module
    # bindings (it `from api.config import get_settings`), so the rt-level patch
    # doesn't reach it — patch both stock modules at the tmp_path.
    import api.stock.store as stock_store
    import api.stock.tools as stock_tools
    settings = MagicMock(memory_root=str(tmp_path))
    monkeypatch.setattr(stock_store, "get_settings", lambda: settings)
    monkeypatch.setattr(stock_tools, "get_settings", lambda: settings)
    # mark_donor requires memory/{device_slug}/ to exist.
    (tmp_path / "macbook-air-m1").mkdir(parents=True)

    import api.agent.runtime_direct as rt
    captured: list[list[dict]] = []

    iter_scripted = iter([
        _stream_tool_use(
            "stock_mark_donor",
            {"device_slug": "macbook-air-m1", "label": "bench donor #1"},
            tool_id="toolu_donor1",
        ),
        _stream_text("Marked the board as a donor."),
    ])
    client = MagicMock()

    def _stream_factory(**kwargs):
        captured.append(list(kwargs["messages"]))
        events, final = next(iter_scripted)
        return _FakeStream(events, final)
    client.messages.stream = _stream_factory
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda **_kw: client)

    ws = FakeWS(["I have a MacBook Air M1 board on my bench as a donor"])
    await rt.run_diagnostic_session_direct(ws, "macbook-air-m1", tier="fast")

    assert len(captured) == 2, "stock tool result was never fed back to the agent"
    tool_results = [
        block
        for msg in captured[1]
        for block in (msg.get("content") if isinstance(msg.get("content"), list) else [])
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert tool_results, "no tool_result block on the second turn"
    decoded = json.loads(tool_results[0]["content"])
    # Proof the real stock tool ran end-to-end (not the unknown-tool fallback).
    assert decoded.get("created") is True
    assert decoded.get("donor_id"), "stock_mark_donor returned no donor_id"
    assert "unknown-tool" not in json.dumps(decoded)
    assert tool_results[0]["tool_use_id"] == "toolu_donor1"
    # And the inventory was actually persisted on disk.
    inv = json.loads((tmp_path / "_stock" / "inventory.json").read_text())
    assert any(
        d.get("device_slug") == "macbook-air-m1"
        for d in inv.get("donors", {}).values()
    ), "donor was not persisted to inventory.json"


async def test_chat_history_persisted_to_jsonl_under_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """End-to-end disk persistence: opening a fresh repair, sending one user
    message, and letting the agent reply must produce a messages.jsonl with
    user+assistant entries under
    `memory/{slug}/repairs/{repair_id}/conversations/{conv_id}/`. Uses the
    real chat_history module (no monkeypatching) on a tmp_path memory root."""
    slug, repair_id = "demo-pi", "R1"
    # Seed the repair meta so the runtime's intro builder doesn't no-op.
    repairs = tmp_path / slug / "repairs"
    repairs.mkdir(parents=True, exist_ok=True)
    (repairs / f"{repair_id}.json").write_text(
        json.dumps({"device_label": "Demo Device", "symptom": "no boot"})
    )

    _stub_session(monkeypatch)
    _patch_settings(monkeypatch, memory_root=tmp_path)
    _install_stream_recorder(
        monkeypatch, [_stream_text("Have you measured PP3V3?")]
    )

    import api.agent.runtime_direct as rt
    ws = FakeWS(["the device is dead"])
    await rt.run_diagnostic_session_direct(
        ws, slug, tier="fast", repair_id=repair_id,
    )

    # Find the conversation directory the runtime materialized. There should
    # be exactly one conv after a single fresh open + one user message.
    conv_dir = tmp_path / slug / "repairs" / repair_id / "conversations"
    convs = [p for p in conv_dir.iterdir() if p.is_dir()]
    assert len(convs) == 1, f"expected 1 conv dir, got {convs}"
    jsonl = convs[0] / "messages.jsonl"
    assert jsonl.exists(), "messages.jsonl was never written"

    lines = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    # We expect at least three records: the intro user message (deferred-flush),
    # the live user message, and the assistant turn.
    roles = [rec.get("event", {}).get("role") for rec in lines]
    assert "user" in roles
    assert "assistant" in roles
    # Confirm the live user input made it through with the ctx_tag prefix.
    user_contents = [
        rec["event"]["content"]
        for rec in lines
        if rec.get("event", {}).get("role") == "user"
        and isinstance(rec["event"].get("content"), str)
    ]
    assert any("the device is dead" in c for c in user_contents), user_contents


async def test_streaming_emits_text_before_turn_cost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Streaming contract: each text block must hit the WS before the
    associated turn_cost frame for that turn — i.e. the runtime flushes at
    each content_block_stop instead of buffering the whole response and
    emitting cost first."""
    _stub_session(monkeypatch)
    _patch_settings(monkeypatch, memory_root=tmp_path)
    _install_stream_recorder(
        monkeypatch, [_stream_text("partial reply")]
    )

    import api.agent.runtime_direct as rt
    ws = FakeWS(["go"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    types = [m.get("type") for m in ws.sent]
    msg_idx = next(
        i for i, m in enumerate(ws.sent)
        if m.get("type") == "message" and m.get("role") == "assistant"
    )
    cost_idx = types.index("turn_cost")
    assert msg_idx < cost_idx, (
        "assistant text must be flushed before turn_cost — streaming over polling"
    )


async def test_stream_kwargs_include_system_prompt_and_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The cached prefix contract: every messages.stream call passes a
    `system` array (list of typed text blocks with cache_control) and a
    `tools` list whose final entry carries cache_control=ephemeral. Without
    this, prompt caching never kicks in and every turn pays full input
    tokens."""
    _stub_session(monkeypatch, _board_with_u7())
    _patch_settings(monkeypatch, memory_root=tmp_path)
    _client, kwargs_log = _install_stream_recorder(
        monkeypatch, [_stream_text("ok")]
    )

    import api.agent.runtime_direct as rt
    ws = FakeWS(["hi"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    assert kwargs_log
    kw = kwargs_log[0]
    # system is a list of typed text blocks (not a bare string).
    assert isinstance(kw["system"], list)
    assert kw["system"][0]["type"] == "text"
    assert kw["system"][0].get("cache_control") == {"type": "ephemeral"}
    # tools last entry is cache-marked.
    tools = kw["tools"]
    assert tools, "tool manifest empty"
    assert tools[-1].get("cache_control") == {"type": "ephemeral"}


async def test_opus_tier_attaches_thinking_and_xhigh_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Opus 4.7/4.8 only: adaptive thinking + xhigh effort are added to the call
    kwargs so the deep-tier reasoning profile is engaged. Lower tiers must
    NOT receive these (Sonnet/Haiku 400 on `effort=xhigh`)."""
    _stub_session(monkeypatch)
    _patch_settings(monkeypatch, memory_root=tmp_path)
    _client, kwargs_log = _install_stream_recorder(
        monkeypatch, [_stream_text("ok")]
    )

    import api.agent.runtime_direct as rt
    ws = FakeWS(["hi"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="deep")

    assert kwargs_log
    kw = kwargs_log[0]
    assert kw.get("thinking") == {"type": "adaptive", "display": "summarized"}
    assert kw.get("output_config") == {"effort": "xhigh"}

    # Now the negative case for fast tier.
    _client, kwargs_log_fast = _install_stream_recorder(
        monkeypatch, [_stream_text("ok")]
    )
    ws2 = FakeWS(["hi"])
    await rt.run_diagnostic_session_direct(ws2, "demo-pi", tier="fast")
    kw2 = kwargs_log_fast[0]
    assert "thinking" not in kw2
    assert "output_config" not in kw2


def test_normalize_message_strips_parsed_output_from_blocks() -> None:
    """SDK ≥0.97 streaming returns ParsedTextBlock carrying a `parsed_output`
    field. It's OUTPUT-only — re-sending it as input content is a 400 ("Extra
    inputs are not permitted"), which breaks every turn after the first tool
    call. _normalize_message must strip it (both the model_dump path and an
    already-dict block reloaded from JSONL)."""
    import api.agent.runtime_direct as rt

    class _ParsedBlock:
        def model_dump(self, mode: str = "json") -> dict:
            return {"type": "text", "text": "hi", "citations": None, "parsed_output": None}

    out = rt._normalize_message({"role": "assistant", "content": [_ParsedBlock()]})
    block = out["content"][0]
    assert "parsed_output" not in block, "parsed_output must be stripped from streamed blocks"
    assert block["text"] == "hi"

    # already-a-dict path (e.g. reloaded from a JSONL mirror).
    out2 = rt._normalize_message(
        {"role": "assistant", "content": [{"type": "text", "text": "x", "parsed_output": None}]}
    )
    assert "parsed_output" not in out2["content"][0]


async def test_emits_turn_complete_when_agent_finishes_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Parity with runtime_managed: when the agent ends its tech-turn
    (stop_reason != tool_use), emit a turn_complete frame so WS clients (bench
    scripts, the reused engine UI) know it's safe to send the next input.
    Without it the spinner hangs forever at end of turn — direct mode never
    signalled turn end at all."""
    _stub_session(monkeypatch)
    _patch_settings(monkeypatch, memory_root=tmp_path)
    _install_stream_recorder(monkeypatch, [_stream_text("Mesure F1 et donne-moi les valeurs.")])

    import api.agent.runtime_direct as rt
    ws = FakeWS(["carte morte"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    tc = [m for m in ws.sent if m.get("type") == "turn_complete"]
    assert tc, "no turn_complete frame emitted at end of turn"
    assert tc[0].get("stop_reason") == "end_turn"


async def test_metering_report_forwards_cache_tokens_from_usage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The per-call metering report must carry the response's cache_read /
    cache_creation token counts so the cloud prices them at their own tiers.
    Dropping them billed hot turns (mostly cache_read) as full input."""
    _stub_session(monkeypatch)
    _patch_settings(monkeypatch, memory_root=tmp_path)

    import api.agent.runtime_direct as rt

    usage = SimpleNamespace(
        input_tokens=10, output_tokens=5,
        cache_read_input_tokens=4096, cache_creation_input_tokens=2048,
    )
    block = MagicMock(type="text", text="ok")
    stop = MagicMock()
    stop.type = "content_block_stop"
    stop.index = 0
    final = MagicMock(content=[block], stop_reason="end_turn", usage=usage, id="msg_x")
    _install_stream_recorder(monkeypatch, [([(stop, [block])], final)])

    calls: list[dict] = []
    import api.agent.cloud_metering as cm
    monkeypatch.setattr(cm, "fire_and_forget_report", lambda **kw: calls.append(kw))

    ws = FakeWS(["go"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    assert calls, "metering report was never fired"
    assert calls[0]["cache_read_input_tokens"] == 4096
    assert calls[0]["cache_creation_input_tokens"] == 2048


class _RaisingStreamCM:
    """Stream context manager whose __aenter__ raises — mirrors the anthropic
    SDK surfacing a RateLimitError/APIError when the request is made."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc_info) -> bool:
        return False


async def test_api_error_mid_stream_emits_error_and_does_not_propagate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When messages.stream() raises an anthropic.APIError (overload >retry
    budget, a 400, a mid-stream disconnect), the turn must end cleanly: emit a
    `stream_error` frame so the tech sees a signal, and DO NOT let the
    exception bubble up and kill the WS handler silently (quota already
    consumed, no UI signal)."""
    import anthropic
    import httpx

    _stub_session(monkeypatch)
    _patch_settings(monkeypatch, memory_root=tmp_path)

    import api.agent.runtime_direct as rt

    err = anthropic.APIError(
        "upstream overloaded",
        httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        body=None,
    )

    def _stream_factory(**_kwargs):
        return _RaisingStreamCM(err)

    client = MagicMock()
    client.messages.stream = _stream_factory
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda **_kw: client)

    ws = FakeWS(["diagnose this board"])
    # Must NOT raise — today the APIError propagates through to the caller.
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    stream_errors = [m for m in ws.sent if m.get("type") == "stream_error"]
    assert stream_errors, "no stream_error frame emitted on APIError"
    assert stream_errors[0].get("error") == "api_error"


class _StallingStream:
    """Stream whose first event read times out — simulates the inactivity
    watchdog firing (asyncio.wait_for surfaces TimeoutError)."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise TimeoutError

    @property
    def current_message_snapshot(self) -> SimpleNamespace:
        return SimpleNamespace(content=[])

    async def get_final_message(self):  # pragma: no cover - must not be reached
        raise AssertionError("get_final_message must not run on a stalled stream")


def _seq_stream_factory(monkeypatch, streams: list) -> dict:
    """Install an AsyncAnthropic whose messages.stream() returns each entry of
    `streams` in order. Returns a dict whose 'i' counts how many were drawn."""
    import api.agent.runtime_direct as rt
    n = {"i": 0}

    def _factory(**_kwargs):
        s = streams[n["i"]]
        n["i"] += 1
        return s

    client = MagicMock()
    client.messages.stream = _factory
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda **_kw: client)
    return n


async def test_stream_stall_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A single inactivity stall must NOT kill the turn: the runtime re-streams
    the same request (messages list untouched, no partial state committed) and
    recovers. The recovered text reaches the WS and no terminal stream_error is
    emitted."""
    _stub_session(monkeypatch)
    _patch_settings(monkeypatch, memory_root=tmp_path)

    import api.agent.runtime_direct as rt
    monkeypatch.setattr(rt, "_STREAM_STALL_MAX_RETRIES", 1)

    events_ok, final_ok = _stream_text("recovered after stall")
    n = _seq_stream_factory(monkeypatch, [_StallingStream(), _FakeStream(events_ok, final_ok)])

    ws = FakeWS(["go"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    assert n["i"] == 2, "expected one stall then exactly one retry attempt"
    msgs = [m for m in ws.sent if m.get("type") == "message" and m.get("role") == "assistant"]
    assert any("recovered after stall" in m["text"] for m in msgs)
    assert not [m for m in ws.sent if m.get("type") == "stream_error"], (
        "retry recovered — no terminal stream_error should be emitted"
    )


async def test_stream_stall_exhausts_retries_then_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When the stall persists past the retry budget, the turn ends with a
    terminal stream_timeout frame (WS stays alive) rather than spinning
    forever or silently dying."""
    _stub_session(monkeypatch)
    _patch_settings(monkeypatch, memory_root=tmp_path)

    import api.agent.runtime_direct as rt
    monkeypatch.setattr(rt, "_STREAM_STALL_MAX_RETRIES", 1)

    n = _seq_stream_factory(monkeypatch, [_StallingStream(), _StallingStream()])

    ws = FakeWS(["go"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    assert n["i"] == 2, "initial attempt + 1 retry, then give up"
    errs = [
        m for m in ws.sent
        if m.get("type") == "stream_error" and m.get("error") == "stream_timeout"
    ]
    assert errs, "terminal stream_timeout must be emitted after retries exhausted"


class _HangingOpenStream:
    """Stream whose __aenter__ (the HTTP POST that opens the SSE stream) never
    returns — reproduces the PRODUCTION FREEZE.

    In the field, the *next* `async with client.messages.stream(...)` after a
    bv_scene dispatch awaited forever. The Anthropic SDK performs the actual
    network request inside `AsyncMessageStreamManager.__aenter__`
    (`await self.__api_request`), and the runtime opened the stream OUTSIDE its
    per-event watchdog (`asyncio.wait_for` only wrapped `__anext__`). When a
    concurrent CPU-bound expand starved the event loop for minutes, this pending
    connect/read got no service (and the server may have dropped the idle
    socket), so the await on `__aenter__` hung with no timeout to break it — the
    diagnostic never made another API call until the client's own WS timeout
    ~5 min later. This double makes `__aenter__` hang to assert the runtime now
    bounds the OPEN too, not just per-event reads.
    """

    async def __aenter__(self):
        # Sleep far longer than the watchdog timeout the test installs. With the
        # fix in place, asyncio.wait_for cancels this await and raises
        # TimeoutError into the runtime before this ever returns.
        await asyncio.sleep(3600)
        return self  # pragma: no cover - never reached

    async def __aexit__(self, *exc_info) -> bool:
        return False

    def __aiter__(self):  # pragma: no cover - open never completes
        return self

    async def __anext__(self):  # pragma: no cover - open never completes
        raise StopAsyncIteration

    @property
    def current_message_snapshot(self) -> SimpleNamespace:  # pragma: no cover
        return SimpleNamespace(content=[])

    async def get_final_message(self):  # pragma: no cover - never reached
        raise AssertionError("get_final_message must not run on a hung-open stream")


async def test_stream_open_hang_is_bounded_and_recovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """REGRESSION (production freeze): a hang while OPENING the next stream must
    be bounded by the same inactivity watchdog as a mid-stream stall, then
    retried — not awaited forever.

    The stream-open (`async with client.messages.stream(...)`) was outside the
    `asyncio.wait_for` watchdog, so a starved/dead connection there hung the turn
    indefinitely. We script: open #1 hangs forever, open #2 succeeds. The whole
    session is itself wrapped in asyncio.wait_for(5s): if the runtime still hangs
    on the open (the bug), this raises TimeoutError and the test fails RED. With
    the fix, the runtime times the open out, re-streams, and recovers.
    """
    _stub_session(monkeypatch)
    # Tiny per-event/open timeout so the hung open trips the watchdog fast.
    _patch_settings(monkeypatch, memory_root=tmp_path)
    import api.agent.runtime_direct as rt

    settings = rt.get_settings()
    settings.ma_stream_event_timeout_seconds = 0.05
    monkeypatch.setattr(rt, "_STREAM_STALL_MAX_RETRIES", 1)

    events_ok, final_ok = _stream_text("recovered after open hang")
    n = _seq_stream_factory(
        monkeypatch, [_HangingOpenStream(), _FakeStream(events_ok, final_ok)]
    )

    ws = FakeWS(["go"])
    # Hard outer bound: if the open hang is NOT bounded by the runtime (the bug),
    # the session never returns and this raises TimeoutError → RED.
    await asyncio.wait_for(
        rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast"),
        timeout=5.0,
    )

    assert n["i"] == 2, "expected one hung open then exactly one retry attempt"
    msgs = [m for m in ws.sent if m.get("type") == "message" and m.get("role") == "assistant"]
    assert any("recovered after open hang" in m["text"] for m in msgs)
    assert not [m for m in ws.sent if m.get("type") == "stream_error"], (
        "retry recovered — no terminal stream_error should be emitted"
    )


async def test_sanitizer_logs_unknown_refdes_and_wraps_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog,
) -> None:
    """The runtime calls sanitize_agent_text on each text block before WS send.
    When the agent surfaces an unknown refdes, the wrapped form ⟨?…⟩ hits the
    wire AND the runtime logs the offence at WARNING — both are required for
    the anti-hallucination defense in depth."""
    import logging

    _stub_session(monkeypatch, _board_with_u7())
    _patch_settings(monkeypatch, memory_root=tmp_path)
    _install_stream_recorder(
        monkeypatch, [_stream_text("U999 looks bad and U7 is fine.")]
    )

    import api.agent.runtime_direct as rt
    ws = FakeWS(["diagnose"])
    with caplog.at_level(logging.WARNING, logger="wrench_board.agent.direct"):
        await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    agent_msg = next(
        m for m in ws.sent
        if m.get("type") == "message" and m.get("role") == "assistant"
    )
    assert "⟨?U999⟩" in agent_msg["text"]
    assert "U7 is fine" in agent_msg["text"]
    # The warning is structural — without it, silent corruption would be
    # invisible in production logs.
    assert any("sanitizer wrapped" in rec.getMessage() for rec in caplog.records)
