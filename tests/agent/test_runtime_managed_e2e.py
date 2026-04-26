# SPDX-License-Identifier: Apache-2.0
"""End-to-end tests for the Managed Agents stream loop in `_forward_session_to_ws`.

These tests focus on edge cases that previously slipped through:

* Stream iterator raising a non-`TimeoutError` (e.g. SSL reset, connection
  drop, mid-stream APIStatusError) used to bubble silently — the WS client
  saw no signal, the technician saw a frozen UI. Now the loop catches it,
  logs, emits `stream_error` on the WS, and breaks cleanly.
* Stream iterator stalling beyond `ma_stream_event_timeout_seconds` — should
  emit `stream_timeout` and break. Already worked; locked in here so a
  refactor can't regress it.
* Managed Agents re-emitting `session.status_idle` with the same `event_ids`
  after we've already responded — the dedupe set must skip the second pass
  (responding twice is a 400 from MA that tears down the stream).
* `processed_at` round-trip telemetry on `user.custom_tool_result` echoes —
  must log the agent's consumption delay without raising on missing fields.

The test mocks the SDK's `AsyncStream` and the WS so the suite stays
fast (sub-second) and offline.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


class _FakeStream:
    """Async-iterable + async-context-manager mimicking AsyncAnthropic's stream.

    `events` is the queue to yield; `raise_after` (optional) is an exception
    to raise on the next `__anext__()` once the queue is drained, simulating
    a transport-level failure mid-stream.
    """

    def __init__(self, events, *, raise_after: Exception | None = None):
        self._events = list(events)
        self._raise_after = raise_after

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._events:
            return self._events.pop(0)
        if self._raise_after is not None:
            exc, self._raise_after = self._raise_after, None
            raise exc
        raise StopAsyncIteration


def _make_client(stream: _FakeStream) -> MagicMock:
    """Build a fake AsyncAnthropic exposing only what the loop touches."""
    client = MagicMock()
    client.beta = MagicMock()
    client.beta.sessions = MagicMock()
    client.beta.sessions.events = MagicMock()
    # `.stream(session_id)` is awaited then used as `async with` — return the
    # FakeStream wrapped in an awaitable.
    client.beta.sessions.events.stream = AsyncMock(return_value=stream)
    client.beta.sessions.events.send = AsyncMock()
    client.beta.sessions.events.list = MagicMock()
    return client


def _make_ws() -> MagicMock:
    ws = MagicMock()
    ws.send_json = AsyncMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    return ws


def _stale_settings(monkeypatch, rm, *, timeout: float = 600.0) -> None:
    """Patch get_settings so the watchdog window is controllable in tests."""
    class _Settings:
        ma_stream_event_timeout_seconds = timeout
        memory_root = "/tmp"
        ma_memory_store_enabled = False
    monkeypatch.setattr(rm, "get_settings", lambda: _Settings())


@pytest.mark.asyncio
async def test_stream_emits_stream_error_on_transport_failure(
    monkeypatch, tmp_path,
):
    """Non-timeout exceptions in the stream iterator must surface on the WS.

    Prior to the fix, an SSL reset or ConnectionError raised inside
    `__anext__()` propagated past the `except TimeoutError` and the task
    ended without telling the client. The WS stayed open, the UI hung.
    Now the loop catches `Exception`, sends `stream_error`, and breaks.
    """
    from api.agent import runtime_managed as rm
    from api.session.state import SessionState

    _stale_settings(monkeypatch, rm)

    boom = ConnectionError("simulated TLS reset")
    stream = _FakeStream(events=[], raise_after=boom)
    client = _make_client(stream)
    ws = _make_ws()
    session_state = SessionState.from_device("nonexistent-slug")

    await rm._forward_session_to_ws(
        ws=ws,
        client=client,
        session_id="sesn_test",
        device_slug="demo",
        memory_root=tmp_path,
        events_by_id={},
        session_state=session_state,
        agent_model="claude-haiku-4-5",
        tier="fast",
        environment_id="env_test",
        repair_id=None,
        conv_id=None,
    )

    payloads = [call.args[0] for call in ws.send_json.await_args_list]
    error_frames = [p for p in payloads if p.get("type") == "stream_error"]
    assert error_frames, (
        f"expected a stream_error frame on transport failure, got {payloads!r}"
    )
    err = error_frames[0]
    assert err["error"] == "ConnectionError"
    assert "simulated TLS reset" in err["message"]
    assert err["session_id"] == "sesn_test"


@pytest.mark.asyncio
async def test_stream_emits_stream_timeout_on_inactive_iterator(
    monkeypatch, tmp_path,
):
    """A stalled SSE iterator beyond the watchdog window emits stream_timeout."""
    from api.agent import runtime_managed as rm
    from api.session.state import SessionState

    # 0.05 s window — short enough for a fast unit test.
    _stale_settings(monkeypatch, rm, timeout=0.05)

    class _NeverEmits:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        def __aiter__(self): return self
        async def __anext__(self):
            await asyncio.sleep(10)  # longer than the watchdog
            raise StopAsyncIteration

    client = _make_client(_NeverEmits())
    ws = _make_ws()
    session_state = SessionState.from_device("nonexistent-slug")

    await rm._forward_session_to_ws(
        ws=ws, client=client, session_id="sesn_test",
        device_slug="demo", memory_root=tmp_path,
        events_by_id={}, session_state=session_state,
        agent_model="claude-haiku-4-5", tier="fast",
        environment_id="env_test", repair_id=None, conv_id=None,
    )

    payloads = [call.args[0] for call in ws.send_json.await_args_list]
    timeout_frames = [p for p in payloads if p.get("type") == "stream_timeout"]
    assert timeout_frames, (
        f"expected a stream_timeout frame on inactive iterator, got {payloads!r}"
    )
    assert timeout_frames[0]["timeout_seconds"] == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_requires_action_dedup_skips_second_dispatch(
    monkeypatch, tmp_path,
):
    """Re-emitted requires_action with same event_ids must NOT re-dispatch.

    MA occasionally re-emits `session.status_idle` with stop_reason=
    requires_action carrying event_ids we've already responded to. Sending
    a second user.custom_tool_result for the same id returns HTTP 400 and
    tears down the stream. The dedupe set short-circuits the second pass.
    """
    from api.agent import runtime_managed as rm
    from api.session.state import SessionState

    _stale_settings(monkeypatch, rm)

    # Stub the dispatcher so we can count invocations without touching the
    # full bv_* / mb_* tool surface.
    dispatch_calls: list[tuple[str, dict]] = []

    async def fake_dispatch(name, payload, *_a, **_kw):
        dispatch_calls.append((name, payload))
        return {"ok": True, "echo": payload}

    monkeypatch.setattr(rm, "_dispatch_tool", fake_dispatch)

    # Build a sequence: a custom_tool_use, then status_idle requires_action,
    # then a SECOND status_idle with the same event_ids (the re-emit).
    tool_use = SimpleNamespace(
        type="agent.custom_tool_use",
        id="sevt_tool_001",
        name="bv_highlight_component",
        input={"refdes": "U7"},
    )
    requires_action_1 = SimpleNamespace(
        type="session.status_idle",
        stop_reason=SimpleNamespace(
            type="requires_action",
            event_ids=["sevt_tool_001"],
        ),
    )
    requires_action_2 = SimpleNamespace(
        type="session.status_idle",
        stop_reason=SimpleNamespace(
            type="requires_action",
            event_ids=["sevt_tool_001"],  # same id — must be skipped
        ),
    )
    end_turn = SimpleNamespace(
        type="session.status_idle",
        stop_reason=SimpleNamespace(type="end_turn", event_ids=[]),
    )

    stream = _FakeStream(events=[
        tool_use, requires_action_1, requires_action_2, end_turn,
    ])
    client = _make_client(stream)
    ws = _make_ws()
    session_state = SessionState.from_device("nonexistent-slug")

    await rm._forward_session_to_ws(
        ws=ws, client=client, session_id="sesn_test",
        device_slug="demo", memory_root=tmp_path,
        events_by_id={}, session_state=session_state,
        agent_model="claude-haiku-4-5", tier="fast",
        environment_id="env_test", repair_id=None, conv_id=None,
    )

    assert len(dispatch_calls) == 1, (
        f"dispatcher must run exactly once for a deduped tool use, got "
        f"{len(dispatch_calls)} calls: {dispatch_calls!r}"
    )
    # Exactly one user.custom_tool_result must hit the wire.
    sent_events = client.beta.sessions.events.send.await_args_list
    tool_results = [
        ev for call in sent_events
        for ev in call.kwargs.get("events", [])
        if ev.get("type") == "user.custom_tool_result"
    ]
    assert len(tool_results) == 1, (
        f"exactly one user.custom_tool_result expected, got {tool_results!r}"
    )
    assert tool_results[0]["custom_tool_use_id"] == "sevt_tool_001"


@pytest.mark.asyncio
async def test_processed_at_logs_consumption_delay(
    monkeypatch, tmp_path, caplog,
):
    """tool_result echo with processed_at populated must log the round-trip."""
    import logging

    from api.agent import runtime_managed as rm
    from api.session.state import SessionState

    _stale_settings(monkeypatch, rm)
    caplog.set_level(logging.INFO, logger=rm.logger.name)

    async def fake_dispatch(name, payload, *_a, **_kw):
        return {"ok": True}
    monkeypatch.setattr(rm, "_dispatch_tool", fake_dispatch)

    tool_use = SimpleNamespace(
        type="agent.custom_tool_use",
        id="sevt_pat_42",
        name="bv_focus_component",
        input={"refdes": "C12"},
    )
    requires_action = SimpleNamespace(
        type="session.status_idle",
        stop_reason=SimpleNamespace(
            type="requires_action", event_ids=["sevt_pat_42"],
        ),
    )
    # Echo back the user.custom_tool_result with processed_at populated —
    # this is what MA would send on the second pass after the agent
    # consumed our response.
    echo = SimpleNamespace(
        type="user.custom_tool_result",
        custom_tool_use_id="sevt_pat_42",
        processed_at="2026-04-26T12:00:00Z",
    )
    end_turn = SimpleNamespace(
        type="session.status_idle",
        stop_reason=SimpleNamespace(type="end_turn", event_ids=[]),
    )

    stream = _FakeStream(events=[tool_use, requires_action, echo, end_turn])
    client = _make_client(stream)
    ws = _make_ws()
    session_state = SessionState.from_device("nonexistent-slug")

    await rm._forward_session_to_ws(
        ws=ws, client=client, session_id="sesn_test",
        device_slug="demo", memory_root=tmp_path,
        events_by_id={}, session_state=session_state,
        agent_model="claude-haiku-4-5", tier="fast",
        environment_id="env_test", repair_id=None, conv_id=None,
    )

    # Expect an INFO log carrying the eid + a delay= value.
    relevant = [
        r for r in caplog.records
        if "tool_result consumed" in r.getMessage()
        and "sevt_pat_42" in r.getMessage()
    ]
    assert relevant, (
        f"expected a tool_result consumption log, got "
        f"{[r.getMessage() for r in caplog.records]!r}"
    )


@pytest.mark.asyncio
async def test_processed_at_null_echo_is_ignored(
    monkeypatch, tmp_path, caplog,
):
    """The first echo (queued, processed_at=None) must NOT log a delay.

    MA echoes our user-sent events twice: once with processed_at=null
    (queued), once with a timestamp (processed). Logging on the queued
    pass would emit a misleading delay measurement before the agent
    has even seen the response.
    """
    import logging

    from api.agent import runtime_managed as rm
    from api.session.state import SessionState

    _stale_settings(monkeypatch, rm)
    caplog.set_level(logging.INFO, logger=rm.logger.name)

    queued_echo = SimpleNamespace(
        type="user.custom_tool_result",
        custom_tool_use_id="sevt_unknown_99",
        processed_at=None,
    )
    end_turn = SimpleNamespace(
        type="session.status_idle",
        stop_reason=SimpleNamespace(type="end_turn", event_ids=[]),
    )

    stream = _FakeStream(events=[queued_echo, end_turn])
    client = _make_client(stream)
    ws = _make_ws()
    session_state = SessionState.from_device("nonexistent-slug")

    await rm._forward_session_to_ws(
        ws=ws, client=client, session_id="sesn_test",
        device_slug="demo", memory_root=tmp_path,
        events_by_id={}, session_state=session_state,
        agent_model="claude-haiku-4-5", tier="fast",
        environment_id="env_test", repair_id=None, conv_id=None,
    )

    delay_logs = [
        r for r in caplog.records
        if "tool_result consumed" in r.getMessage()
    ]
    assert delay_logs == [], (
        f"queued (processed_at=None) echo must not produce a delay log, "
        f"got {[r.getMessage() for r in delay_logs]!r}"
    )


@pytest.mark.asyncio
async def test_full_turn_flow_message_tool_result_complete(
    monkeypatch, tmp_path,
):
    """End-to-end: agent.message → custom_tool_use → requires_action →
    dispatch → user.custom_tool_result → agent.message → end_turn.

    Replays the most common turn shape (tool-using assistant) through the
    full stream loop and asserts:

    * Both `agent.message` chunks reach the WS as `message` frames in
      order, sanitized.
    * The dispatcher runs once with the right name + payload.
    * `user.custom_tool_result` is sent back to MA with the dispatcher's
      result serialized as JSON, keyed by the original tool_use eid.
    * The closing `end_turn` lands as a `turn_complete` WS frame.

    This is the "happy path" the previous suite never covered — every
    other test exercised an edge (error, timeout, dedupe). A regression
    in any of the four steps above would have shipped silently before.
    """
    import json

    from api.agent import runtime_managed as rm
    from api.session.state import SessionState

    _stale_settings(monkeypatch, rm)

    # Capture the dispatcher invocation so we can assert on its inputs.
    dispatch_calls: list[tuple[str, dict]] = []

    async def fake_dispatch(name, payload, *_a, **_kw):
        dispatch_calls.append((name, payload))
        # The runtime strips `event` / `events` keys from the result
        # before serializing into user.custom_tool_result, so include
        # one to verify the strip behavior end-to-end.
        return {
            "ok": True,
            "highlighted": payload.get("refdes"),
            "event": {"type": "bv_highlight", "refdes": payload.get("refdes")},
        }

    monkeypatch.setattr(rm, "_dispatch_tool", fake_dispatch)

    # Full turn sequence, in order MA would emit it.
    intro_message = SimpleNamespace(
        type="agent.message",
        content=[
            SimpleNamespace(type="text", text="Je vais surligner U7 pour vérifier."),
        ],
    )
    tool_use = SimpleNamespace(
        type="agent.custom_tool_use",
        id="sevt_full_001",
        name="bv_highlight_component",
        input={"refdes": "U7"},
    )
    requires_action = SimpleNamespace(
        type="session.status_idle",
        stop_reason=SimpleNamespace(
            type="requires_action", event_ids=["sevt_full_001"],
        ),
    )
    closing_message = SimpleNamespace(
        type="agent.message",
        content=[
            SimpleNamespace(type="text", text="U7 est surligné. Que mesures-tu ?"),
        ],
    )
    end_turn = SimpleNamespace(
        type="session.status_idle",
        stop_reason=SimpleNamespace(type="end_turn", event_ids=[]),
    )

    stream = _FakeStream(events=[
        intro_message, tool_use, requires_action, closing_message, end_turn,
    ])
    client = _make_client(stream)
    ws = _make_ws()
    session_state = SessionState.from_device("nonexistent-slug")

    await rm._forward_session_to_ws(
        ws=ws, client=client, session_id="sesn_test",
        device_slug="demo", memory_root=tmp_path,
        events_by_id={}, session_state=session_state,
        agent_model="claude-haiku-4-5", tier="fast",
        environment_id="env_test", repair_id=None, conv_id=None,
    )

    # ---- Assertions ----------------------------------------------------

    # 1. Both agent.message texts reached the WS as `message` frames, in order.
    payloads = [call.args[0] for call in ws.send_json.await_args_list]
    message_frames = [p for p in payloads if p.get("type") == "message"]
    assert len(message_frames) == 2, (
        f"expected 2 message frames (intro + closing), got {len(message_frames)}: "
        f"{message_frames!r}"
    )
    assert message_frames[0]["role"] == "assistant"
    assert "U7" in message_frames[0]["text"]
    assert "mesures" in message_frames[1]["text"]

    # 2. tool_use frame announced to the WS so the UI chat can show it.
    tool_use_frames = [p for p in payloads if p.get("type") == "tool_use"]
    assert len(tool_use_frames) == 1
    assert tool_use_frames[0]["name"] == "bv_highlight_component"
    assert tool_use_frames[0]["input"] == {"refdes": "U7"}

    # 3. Dispatcher ran exactly once with the right inputs.
    assert dispatch_calls == [("bv_highlight_component", {"refdes": "U7"})], (
        f"dispatcher invocation mismatch: {dispatch_calls!r}"
    )

    # 4. user.custom_tool_result was posted back to MA with the eid + JSON
    #    body, and the `event` key was stripped from the agent-facing payload.
    sent_events = client.beta.sessions.events.send.await_args_list
    tool_results = [
        ev for call in sent_events
        for ev in call.kwargs.get("events", [])
        if ev.get("type") == "user.custom_tool_result"
    ]
    assert len(tool_results) == 1
    tr = tool_results[0]
    assert tr["custom_tool_use_id"] == "sevt_full_001"
    body = json.loads(tr["content"][0]["text"])
    assert body == {"ok": True, "highlighted": "U7"}, (
        f"event/events keys must be stripped from agent-facing tool_result, "
        f"got {body!r}"
    )

    # 5. turn_complete WS frame was emitted at end_turn.
    turn_complete_frames = [p for p in payloads if p.get("type") == "turn_complete"]
    assert len(turn_complete_frames) == 1, (
        f"expected exactly one turn_complete frame, got {len(turn_complete_frames)}"
    )
    assert turn_complete_frames[0]["stop_reason"] == "end_turn"
