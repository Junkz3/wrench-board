"""End-to-end tests for the direct diagnostic runtime over a fake WebSocket."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.board.model import Board, Layer, Part, Pin, Point
from api.session.state import SessionState


class FakeWS:
    """Minimal WebSocket double that captures send_json calls."""

    def __init__(self, user_messages: list[str]) -> None:
        self.sent: list[dict] = []
        self._inbox: asyncio.Queue[str] = asyncio.Queue()
        for m in user_messages:
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


def _stub_session(monkeypatch: pytest.MonkeyPatch, board: Board | None) -> None:
    """Force SessionState.from_device to return a pre-built session."""
    def _from_device(_slug: str) -> SessionState:
        s = SessionState()
        if board is not None:
            s.set_board(board)
        return s
    monkeypatch.setattr(
        "api.agent.runtime_direct.SessionState.from_device",
        staticmethod(_from_device),
    )


def _board_with_u7() -> Board:
    return Board(
        board_id="t", file_hash="sha256:x", source_format="t",
        outline=[],
        parts=[Part(refdes="U7", layer=Layer.TOP, is_smd=True,
                    bbox=(Point(x=0, y=0), Point(x=10, y=10)), pin_refs=[0, 1])],
        pins=[
            Pin(part_refdes="U7", index=1, pos=Point(x=2, y=2), layer=Layer.TOP),
            Pin(part_refdes="U7", index=2, pos=Point(x=8, y=8), layer=Layer.TOP),
        ],
        nets=[], nails=[],
    )


class _FakeStream:
    """Async context manager that doubles as an async iterator — mirrors the
    shape of `client.messages.stream(...)` just enough for the direct runtime.

    Events are scripted as (event, snapshot_content) pairs: the snapshot
    accumulates completed blocks so the runtime can read
    `stream.current_message_snapshot.content[idx]` after each
    `content_block_stop`.
    """

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


def _stream_text(text: str) -> tuple[list[tuple], MagicMock]:
    """Scripted stream producing one text block then ending."""
    block = MagicMock(type="text", text=text)
    stop_ev = MagicMock()
    stop_ev.type = "content_block_stop"
    stop_ev.index = 0
    events = [(stop_ev, [block])]
    final = MagicMock(content=[block], stop_reason="end_turn")
    return events, final


def _stream_tool_use(
    name: str, tool_input: dict, tool_id: str = "toolu_1"
) -> tuple[list[tuple], MagicMock]:
    """Scripted stream producing one tool_use block.

    Tool-use blocks don't trigger WS emission in the runtime (it only emits
    at content_block_stop for *text* blocks), so we yield no events and let
    `get_final_message` deliver the tool_use for dispatch.
    """
    block = MagicMock(type="tool_use", input=tool_input, id=tool_id)
    block.name = name
    final = MagicMock(content=[block], stop_reason="tool_use")
    return [], final


def _mock_anthropic(scripted: list[tuple[list[tuple], MagicMock]]) -> MagicMock:
    """Build an AsyncAnthropic whose messages.stream yields scripted responses."""
    iterator = iter(scripted)
    client = MagicMock()

    def _stream_factory(**_kwargs):
        events, final = next(iterator)
        return _FakeStream(events, final)

    client.messages.stream = _stream_factory
    return client


def _patch_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    import api.agent.runtime_direct as rt
    monkeypatch.setattr(rt, "get_settings", lambda: MagicMock(
        anthropic_api_key="sk-fake",
        memory_root=Path("/tmp/nope"),
        anthropic_model_main="claude-opus-4-8",
        ma_stream_event_timeout_seconds=600.0,
        ma_protocol_confirmation_timeout_seconds=300.0,
    ))


def _push_frame(ws: FakeWS, frame: dict) -> None:
    """Inject a raw JSON frame (e.g. a confirmation reply) into the WS inbox."""
    ws._inbox.put_nowait(json.dumps(frame))


def _protocol_payload_u7() -> dict:
    """A minimal, valid bv_propose_protocol payload targeting U7."""
    return {
        "title": "Vérifier VCC U7",
        "rationale": "Suspicion d'absence d'alimentation.",
        "steps": [{
            "type": "numeric",
            "target": "U7",
            "instruction": "Mesurer VCC sur U7.",
            "unit": "V",
            "nominal": 3.3,
            "pass_range": [3.0, 3.6],
        }],
    }


class _BlockingFakeWS(FakeWS):
    """Like FakeWS but receive_text blocks on an empty inbox instead of
    raising WebSocketDisconnect — lets the protocol-confirmation parking reach
    its inactivity timeout in tests."""

    async def receive_text(self) -> str:
        if self._closed:
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect
        return await self._inbox.get()


@pytest.mark.asyncio
async def test_bv_highlight_emits_tool_use_then_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent calls bv_highlight(U7) → WS sees tool_use, then boardview.highlight, then final message."""
    _stub_session(monkeypatch, _board_with_u7())
    import api.agent.runtime_direct as rt
    fake_client = _mock_anthropic([
        _stream_tool_use("bv_highlight", {"refdes": "U7"}),
        _stream_text("Done."),
    ])
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda **_kw: fake_client)
    _patch_settings(monkeypatch)

    ws = FakeWS(["show U7"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    types = [m.get("type") for m in ws.sent]
    assert "session_ready" in types
    tu_idx = types.index("tool_use")
    bv_idx = next(i for i, t in enumerate(types) if t == "boardview.highlight")
    assert tu_idx < bv_idx
    assert any(m.get("type") == "message" and m.get("role") == "assistant" for m in ws.sent)


@pytest.mark.asyncio
async def test_bv_highlight_unknown_emits_no_boardview_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """bv_highlight(U999) → tool_use, NO boardview.* event, final message present."""
    _stub_session(monkeypatch, _board_with_u7())
    import api.agent.runtime_direct as rt
    fake_client = _mock_anthropic([
        _stream_tool_use("bv_highlight", {"refdes": "U999"}),
        _stream_text("Couldn't find that one."),
    ])
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda **_kw: fake_client)
    _patch_settings(monkeypatch)

    ws = FakeWS(["show U999"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    types = [m.get("type", "") for m in ws.sent]
    assert "tool_use" in types
    assert not any(t.startswith("boardview.") for t in types)


@pytest.mark.asyncio
async def test_tool_result_never_contains_event_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core design invariant: the tool_result sent back to the agent has no 'event' key."""
    _stub_session(monkeypatch, _board_with_u7())
    import api.agent.runtime_direct as rt

    captured_messages: list[list[dict]] = []

    def recording_stream(**kwargs):
        captured_messages.append(list(kwargs["messages"]))
        if len(captured_messages) == 1:
            events, final = _stream_tool_use("bv_highlight", {"refdes": "U7"})
        else:
            events, final = _stream_text("ok")
        return _FakeStream(events, final)

    fake_client = MagicMock()
    fake_client.messages.stream = recording_stream
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda **_kw: fake_client)
    _patch_settings(monkeypatch)

    ws = FakeWS(["show U7"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    second_call_messages = captured_messages[1]
    tool_result_blocks = [
        b for m in second_call_messages
        for b in (m.get("content") if isinstance(m.get("content"), list) else [])
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert tool_result_blocks, "expected at least one tool_result block"
    decoded = json.loads(tool_result_blocks[0]["content"])
    assert "event" not in decoded
    assert decoded.get("ok") is True


@pytest.mark.asyncio
async def test_sanitizer_wraps_unknown_refdes_in_final_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent text 'U999 is suspect' gets wrapped to '⟨?U999⟩ is suspect' before WS send."""
    _stub_session(monkeypatch, _board_with_u7())
    import api.agent.runtime_direct as rt
    fake_client = _mock_anthropic([
        _stream_text("U999 is suspect"),
    ])
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda **_kw: fake_client)
    _patch_settings(monkeypatch)

    ws = FakeWS(["what's wrong?"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    agent_msgs = [m for m in ws.sent if m.get("type") == "message" and m.get("role") == "assistant"]
    assert agent_msgs
    assert "⟨?U999⟩" in agent_msgs[0]["text"]
    assert "U999 is suspect" not in agent_msgs[0]["text"]


@pytest.mark.asyncio
async def test_direct_cam_capture_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """T16: cam_capture requests a frame, awaits it, uploads to Files API, and
    feeds an image tool_result back to the agent (parity with runtime/camera.py).

    The disk write (persist_macro) and Files API are stubbed; the behavior under
    test is the round-trip + the image-shaped tool_result content.
    """
    _stub_session(monkeypatch, _board_with_u7())
    import api.agent.runtime_direct as rt

    monkeypatch.setattr(rt, "persist_macro", lambda **_k: Path("/tmp/cap.jpg"))

    captured_msgs: list[list[dict]] = []

    def recording_stream(**kwargs):
        captured_msgs.append(list(kwargs["messages"]))
        if len(captured_msgs) == 1:
            events, final = _stream_tool_use(
                "cam_capture", {"reason": "voir U7"}, tool_id="toolu_cam"
            )
        else:
            events, final = _stream_text("Je vois U7 sur la photo.")
        return _FakeStream(events, final)

    fake_client = MagicMock()
    fake_client.messages.stream = recording_stream
    fake_client.beta.files.upload = AsyncMock(
        return_value=SimpleNamespace(id="file_xyz")
    )
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda **_kw: fake_client)
    monkeypatch.setattr(rt, "get_settings", lambda: MagicMock(
        anthropic_api_key="sk-fake",
        memory_root=Path("/tmp/nope"),
        anthropic_model_main="claude-opus-4-8",
        ma_stream_event_timeout_seconds=600.0,
        ma_protocol_confirmation_timeout_seconds=300.0,
        ma_camera_capture_timeout_seconds=5.0,
    ))

    img_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfakeimagedata").decode()
    ws = _BlockingFakeWS(["prends une photo de U7"])
    task = asyncio.create_task(
        rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast", repair_id="rep-1")
    )

    req = None
    for _ in range(200):
        req = next((m for m in ws.sent if m.get("type") == "server.capture_request"), None)
        if req:
            break
        await asyncio.sleep(0.01)
    assert req is not None, "expected a server.capture_request frame"
    assert req.get("tool_use_id") == "toolu_cam"

    ws._inbox.put_nowait(json.dumps({
        "type": "client.capture_response",
        "request_id": req["request_id"],
        "base64": img_b64,
        "mime": "image/png",
        "device_label": "USB Cam",
    }))
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    fake_client.beta.files.upload.assert_awaited_once()
    assert len(captured_msgs) >= 2, "expected a follow-up turn carrying the image"
    tool_results = [
        b for m in captured_msgs[1]
        for b in (m.get("content") if isinstance(m.get("content"), list) else [])
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert tool_results, "expected an image tool_result fed back to the agent"
    content = tool_results[0]["content"]
    assert isinstance(content, list), "cam_capture tool_result must be a content list"
    img_blocks = [c for c in content if isinstance(c, dict) and c.get("type") == "image"]
    assert img_blocks, "expected an image block in the tool_result"
    assert img_blocks[0]["source"]["file_id"] == "file_xyz"


@pytest.mark.asyncio
async def test_direct_capabilities_rebuilds_manifest_with_cam_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T16: a client.capabilities{camera_available:true} frame rebuilds the
    tools manifest so cam_capture is offered on subsequent turns.

    The frontend sends capabilities AFTER session_ready (after the initial
    manifest snapshot), so without a rebuild cam_capture would never appear in
    direct mode and the agent could never request a photo.
    """
    _stub_session(monkeypatch, _board_with_u7())
    import api.agent.runtime_direct as rt

    captured_tools: list[list] = []

    def recording_stream(**kwargs):
        captured_tools.append(kwargs.get("tools") or [])
        events, final = _stream_text("ok")
        return _FakeStream(events, final)

    fake_client = MagicMock()
    fake_client.messages.stream = recording_stream
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda **_kw: fake_client)
    _patch_settings(monkeypatch)

    ws = FakeWS([])
    _push_frame(ws, {"type": "client.capabilities", "camera_available": True})
    _push_frame(ws, {"type": "message", "text": "prends une photo de U7"})
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    assert captured_tools, "expected at least one stream call"
    names = [t.get("name") for t in captured_tools[-1]]
    assert "cam_capture" in names, "cam_capture must be offered after capabilities"


@pytest.mark.asyncio
async def test_direct_protocol_accept_dispatches_after_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T16: bv_propose_protocol parks for tech confirmation; accept → dispatch.

    Parity with runtime_managed Pattern-4: the runtime emits
    protocol_pending_confirmation and waits; only on an accept frame does the
    real dispatch run (materialize + protocol_proposed). The disk dispatch is
    tested elsewhere — here it's stubbed to assert the confirmation gate.
    """
    _stub_session(monkeypatch, _board_with_u7())
    import api.agent.runtime_direct as rt

    calls: list[str] = []

    async def _stub_dispatch(name, *_a, **_k):
        calls.append(name)
        return {"ok": True, "event": {"type": "protocol_proposed", "protocol_id": "p1"}}

    monkeypatch.setattr(rt, "_dispatch_protocol_tool", _stub_dispatch)
    fake_client = _mock_anthropic([
        _stream_tool_use("bv_propose_protocol", _protocol_payload_u7(), tool_id="toolu_p1"),
        _stream_text("Protocole lancé."),
    ])
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda **_kw: fake_client)
    _patch_settings(monkeypatch)

    ws = FakeWS(["propose un protocole"])
    _push_frame(ws, {
        "type": "client.protocol_confirmation",
        "tool_use_id": "toolu_p1",
        "decision": "accept",
    })
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast", repair_id="rep-1")

    types = [m.get("type") for m in ws.sent]
    assert "protocol_pending_confirmation" in types
    assert calls == ["bv_propose_protocol"], "accept must trigger the real dispatch"
    assert "protocol_proposed" in types
    assert types.index("protocol_pending_confirmation") < types.index("protocol_proposed")


@pytest.mark.asyncio
async def test_direct_protocol_reject_skips_dispatch_and_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T16: a reject confirmation must NOT materialize the protocol; the agent
    gets an error tool_result carrying the tech's reason."""
    _stub_session(monkeypatch, _board_with_u7())
    import api.agent.runtime_direct as rt

    calls: list[str] = []

    async def _stub_dispatch(name, *_a, **_k):
        calls.append(name)
        return {"ok": True}

    monkeypatch.setattr(rt, "_dispatch_protocol_tool", _stub_dispatch)

    captured: list[list[dict]] = []

    def recording_stream(**kwargs):
        captured.append(list(kwargs["messages"]))
        if len(captured) == 1:
            events, final = _stream_tool_use(
                "bv_propose_protocol", _protocol_payload_u7(), tool_id="toolu_p2"
            )
        else:
            events, final = _stream_text("Compris, j'abandonne ce protocole.")
        return _FakeStream(events, final)

    fake_client = MagicMock()
    fake_client.messages.stream = recording_stream
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda **_kw: fake_client)
    _patch_settings(monkeypatch)

    ws = FakeWS(["propose un protocole"])
    _push_frame(ws, {
        "type": "client.protocol_confirmation",
        "tool_use_id": "toolu_p2",
        "decision": "reject",
        "reason": "trop risqué sans isoler la batterie",
    })
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast", repair_id="rep-1")

    types = [m.get("type") for m in ws.sent]
    assert "protocol_pending_confirmation" in types
    assert "protocol_proposed" not in types
    assert calls == [], "reject must NOT dispatch the protocol"
    # The tool_result fed back to the agent on the 2nd turn carries the reason.
    second_turn_msgs = captured[1]
    tool_results = [
        b for m in second_turn_msgs
        for b in (m.get("content") if isinstance(m.get("content"), list) else [])
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert tool_results, "expected a tool_result for the rejected protocol"
    decoded = json.loads(tool_results[0]["content"])
    assert decoded["reason"] == "rejected"
    assert "trop risqué" in decoded["error"]


@pytest.mark.asyncio
async def test_direct_protocol_confirmation_timeout_emits_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T16: if the tech never answers, the parking times out → a
    protocol_confirmation_timeout frame is emitted and no dispatch runs."""
    _stub_session(monkeypatch, _board_with_u7())
    import api.agent.runtime_direct as rt

    calls: list[str] = []

    async def _stub_dispatch(name, *_a, **_k):
        calls.append(name)
        return {"ok": True}

    monkeypatch.setattr(rt, "_dispatch_protocol_tool", _stub_dispatch)
    fake_client = _mock_anthropic([
        _stream_tool_use("bv_propose_protocol", _protocol_payload_u7(), tool_id="toolu_p3"),
        _stream_text("D'accord."),
    ])
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda **_kw: fake_client)
    monkeypatch.setattr(rt, "get_settings", lambda: MagicMock(
        anthropic_api_key="sk-fake",
        memory_root=Path("/tmp/nope"),
        anthropic_model_main="claude-opus-4-8",
        ma_stream_event_timeout_seconds=600.0,
        ma_protocol_confirmation_timeout_seconds=0.05,
    ))

    ws = _BlockingFakeWS(["propose un protocole"])
    task = asyncio.create_task(
        rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast", repair_id="rep-1")
    )
    await asyncio.sleep(0.4)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    types = [m.get("type") for m in ws.sent]
    assert "protocol_pending_confirmation" in types
    assert "protocol_confirmation_timeout" in types
    assert calls == [], "timeout must NOT dispatch the protocol"


@pytest.mark.asyncio
async def test_direct_stream_watchdog_trips_on_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    """T16: a stalled model stream trips the per-event watchdog → stream_error.

    Parity with runtime_managed's stream watchdog: if no stream event arrives
    within ma_stream_event_timeout_seconds, the turn must not hang forever (an
    infinite spinner for the tech). Direct mode has no server-side replay, so a
    stall is terminal: emit a stream_error frame and return cleanly (the WS
    stays alive for the next user message / close).
    """
    _stub_session(monkeypatch, _board_with_u7())
    import api.agent.runtime_direct as rt

    class _StallingStream:
        """Stream whose first event takes 0.3s — longer than the watchdog window."""

        async def __aenter__(self) -> _StallingStream:
            return self

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

        def __aiter__(self) -> _StallingStream:
            return self

        async def __anext__(self) -> object:
            await asyncio.sleep(0.3)
            raise StopAsyncIteration

        async def get_final_message(self) -> MagicMock:
            m = MagicMock(content=[], stop_reason="end_turn")
            m.id = "msg_stall"
            m.usage = SimpleNamespace(
                input_tokens=0, output_tokens=0,
                cache_read_input_tokens=0, cache_creation_input_tokens=0,
            )
            return m

    fake_client = MagicMock()
    fake_client.messages.stream = lambda **_kw: _StallingStream()
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda **_kw: fake_client)
    monkeypatch.setattr(rt, "get_settings", lambda: MagicMock(
        anthropic_api_key="sk-fake",
        memory_root=Path("/tmp/nope"),
        anthropic_model_main="claude-opus-4-8",
        ma_stream_event_timeout_seconds=0.05,
    ))

    ws = FakeWS(["diagnose"])
    await asyncio.wait_for(
        rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast"),
        timeout=5.0,
    )

    errs = [m for m in ws.sent if m.get("type") == "stream_error"]
    assert errs, "expected a stream_error frame when the watchdog trips"
    assert errs[0].get("error") == "stream_timeout"


@pytest.mark.asyncio
async def test_direct_turn_reports_token_usage_to_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    """T13/T16: a completed direct-mode turn fires cloud token-usage metering.

    Parity with runtime_managed's span.model_request_end hook: each LLM call's
    raw input/output tokens are reported to the cloud (the tenant-private
    billing unit), carrying the session owner_ref + engine repair_id, keyed on
    the Anthropic message id for cloud-side idempotency. Without this, running
    the engine in DIAGNOSTIC_MODE=direct would bill nothing.
    """
    _stub_session(monkeypatch, _board_with_u7())
    import api.agent.cloud_metering as cm
    import api.agent.runtime_direct as rt

    spy = MagicMock()
    monkeypatch.setattr(cm, "fire_and_forget_report", spy)

    block = MagicMock(type="text", text="Checked.")
    stop_ev = MagicMock()
    stop_ev.type = "content_block_stop"
    stop_ev.index = 0
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    final = MagicMock(content=[block], stop_reason="end_turn")
    final.id = "msg_abc123"
    final.usage = usage

    def _stream_factory(**_kw):
        return _FakeStream([(stop_ev, [block])], final)

    fake_client = MagicMock()
    fake_client.messages.stream = _stream_factory
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda **_kw: fake_client)
    _patch_settings(monkeypatch)

    ws = FakeWS(["check U7"])
    await rt.run_diagnostic_session_direct(
        ws, "demo-pi", tier="fast", repair_id="rep-1", owner_ref="tenant-xyz"
    )

    assert spy.call_count == 1, "expected exactly one cloud metering report for the turn"
    kwargs = spy.call_args.kwargs
    assert kwargs["owner_ref"] == "tenant-xyz"
    assert kwargs["model"] == "claude-haiku-4-5"
    assert kwargs["input_tokens"] == 100
    assert kwargs["output_tokens"] == 50
    assert kwargs["engine_repair_id"] == "rep-1"
    assert "msg_abc123" in kwargs["event_id"]


@pytest.mark.asyncio
async def test_stream_emits_each_text_block_at_its_stop_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two text blocks in one response → two separate WS `message` events.

    Proves we emit at each content_block_stop rather than batching the whole
    response: each stop event carries its own snapshot slice, and the runtime
    flushes to the WS as soon as a block closes.
    """
    _stub_session(monkeypatch, _board_with_u7())
    import api.agent.runtime_direct as rt

    block_a = MagicMock(type="text", text="first")
    block_b = MagicMock(type="text", text="second")
    stop_a = MagicMock()
    stop_a.type = "content_block_stop"
    stop_a.index = 0
    stop_b = MagicMock()
    stop_b.type = "content_block_stop"
    stop_b.index = 1
    events = [
        (stop_a, [block_a]),
        (stop_b, [block_a, block_b]),
    ]
    final = MagicMock(content=[block_a, block_b], stop_reason="end_turn")

    def _stream_factory(**_kw):
        return _FakeStream(events, final)

    fake_client = MagicMock()
    fake_client.messages.stream = _stream_factory
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda **_kw: fake_client)
    _patch_settings(monkeypatch)

    ws = FakeWS(["tell me a story"])
    await rt.run_diagnostic_session_direct(ws, "demo-pi", tier="fast")

    agent_msgs = [
        m for m in ws.sent
        if m.get("type") == "message" and m.get("role") == "assistant"
    ]
    assert len(agent_msgs) == 2
    assert agent_msgs[0]["text"] == "first"
    assert agent_msgs[1]["text"] == "second"
