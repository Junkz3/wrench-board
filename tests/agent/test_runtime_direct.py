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
        anthropic_model_main="claude-opus-4-7",
        anthropic_max_retries=max_retries,
        chat_history_backend="jsonl",
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
        ("deep", "claude-opus-4-7"),
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
    settings.anthropic_model_main = "claude-opus-4-7"
    _client, kwargs_log = _install_stream_recorder(
        monkeypatch, [_stream_text("ok")]
    )

    import api.agent.runtime_direct as rt
    ws = FakeWS(["go"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="bogus-tier")

    assert kwargs_log[0]["model"] == "claude-opus-4-7"


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
    """Opus 4.7 only: adaptive thinking + xhigh effort are added to the call
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
