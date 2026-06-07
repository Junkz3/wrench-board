"""T13 — the live forwarder reports per-LLM-call token usage to the cloud.

At each `span.model_request_end`, `_forward_session_to_ws` must fire
`cloud_metering.fire_and_forget_report` with the tenant (owner_ref), the model,
the raw token counts, the engine repair id, and a stable per-call event_id
(`{session_id}:{event.id}`). The catch-up/replay dedup gate (`_already_seen`)
must make it fire AT MOST ONCE per event id — re-seeing the same span (lossless
reconnect catch-up) must not double-report.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.agent import runtime_managed as rm
from api.agent.owner_ref import set_owner_ref
from api.agent.runtime import forwarders
from api.session.state import SessionState


class _FakeStream:
    def __init__(self, events):
        self._events = list(events)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._events:
            return self._events.pop(0)
        raise StopAsyncIteration


class _FakeEventsList:
    def __init__(self, events=()):
        self._events = list(events)

    def __aiter__(self):
        async def _gen():
            for ev in self._events:
                yield ev
        return _gen()


def _make_client(stream):
    client = MagicMock()
    client.beta = MagicMock()
    client.beta.sessions = MagicMock()
    client.beta.sessions.events = MagicMock()
    client.beta.sessions.events.stream = AsyncMock(return_value=stream)
    client.beta.sessions.events.send = AsyncMock()
    client.beta.sessions.events.list = lambda _sid: _FakeEventsList()
    return client


def _make_ws():
    ws = MagicMock()
    ws.send_json = AsyncMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    return ws


class _Settings:
    # max_reconnects=0 → the stream ends fast after our events are processed.
    ma_stream_event_timeout_seconds = 600.0
    ma_stream_max_reconnects = 0
    ma_session_drain_timeout_seconds = 5.0
    ma_forwarder_unwind_timeout_seconds = 2.0
    ma_subagent_consultation_timeout_seconds = 120.0
    ma_curator_timeout_seconds = 180.0
    ma_camera_capture_timeout_seconds = 30.0
    ma_memory_store_http_timeout_seconds = 30.0
    memory_root = "/tmp"
    ma_memory_store_enabled = False


def _model_request_end(event_id="evt-1"):
    usage = SimpleNamespace(
        model="claude-opus-4-8",
        input_tokens=1000,
        output_tokens=200,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(
        type="span.model_request_end", id=event_id, model_usage=usage
    )


@pytest.mark.asyncio
async def test_forwarder_reports_usage_once_per_event(monkeypatch, tmp_path):
    monkeypatch.setattr(rm, "get_settings", lambda: _Settings())
    recorder = MagicMock()
    monkeypatch.setattr(forwarders.cloud_metering, "fire_and_forget_report", recorder)

    # Same event id delivered twice — the second hit is a catch-up replay and
    # must be deduped by `_already_seen`, so only ONE report fires.
    stream = _FakeStream([_model_request_end("evt-1"), _model_request_end("evt-1")])
    client = _make_client(stream)
    ws = _make_ws()
    session_state = SessionState.from_device("demo")

    set_owner_ref("tenant-xyz")
    try:
        await forwarders._forward_session_to_ws(
            ws=ws,
            client=client,
            session_id="sesn_1",
            device_slug="demo",
            memory_root=tmp_path,
            events_by_id={},
            session_state=session_state,
            agent_model="claude-opus-4-8",
            tier="deep",
            environment_id="env_test",
            repair_id="rep-1",
            conv_id=None,
        )
    finally:
        set_owner_ref(None)

    assert recorder.call_count == 1, (
        f"expected exactly one metering report, got {recorder.call_count}"
    )
    assert recorder.call_args.kwargs == {
        "owner_ref": "tenant-xyz",
        "model": "claude-opus-4-8",
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "engine_repair_id": "rep-1",
        "event_id": "sesn_1:evt-1",
    }


@pytest.mark.asyncio
async def test_forwarder_report_includes_cache_tokens(monkeypatch, tmp_path):
    """The managed forwarder already computes cache_read/creation for the local
    turn cost + hit-rate log — it must also forward them in the metering report
    so the cloud prices them at their own tiers (hot turns aren't overcharged)."""
    monkeypatch.setattr(rm, "get_settings", lambda: _Settings())
    recorder = MagicMock()
    monkeypatch.setattr(forwarders.cloud_metering, "fire_and_forget_report", recorder)

    usage = SimpleNamespace(
        model="claude-opus-4-8", input_tokens=10, output_tokens=5,
        cache_read_input_tokens=4096, cache_creation_input_tokens=2048,
    )
    span = SimpleNamespace(type="span.model_request_end", id="evt-c", model_usage=usage)
    client = _make_client(_FakeStream([span]))
    ws = _make_ws()
    session_state = SessionState.from_device("demo")

    set_owner_ref("tenant-xyz")
    try:
        await forwarders._forward_session_to_ws(
            ws=ws, client=client, session_id="sesn_1", device_slug="demo",
            memory_root=tmp_path, events_by_id={}, session_state=session_state,
            agent_model="claude-opus-4-8", tier="deep", environment_id="env_test",
            repair_id="rep-1", conv_id=None,
        )
    finally:
        set_owner_ref(None)

    assert recorder.call_args.kwargs["cache_read_input_tokens"] == 4096
    assert recorder.call_args.kwargs["cache_creation_input_tokens"] == 2048


@pytest.mark.asyncio
async def test_forwarder_skips_report_for_id_less_span(monkeypatch, tmp_path):
    """A span without an id has no stable idempotency key — don't report it
    (else every id-less span collapses to {session}:None and the cloud dedups
    them to one ledger row → silent undercount)."""
    monkeypatch.setattr(rm, "get_settings", lambda: _Settings())
    recorder = MagicMock()
    monkeypatch.setattr(forwarders.cloud_metering, "fire_and_forget_report", recorder)

    usage = SimpleNamespace(
        model="claude-opus-4-8", input_tokens=10, output_tokens=2,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    id_less = SimpleNamespace(type="span.model_request_end", model_usage=usage)  # no id
    client = _make_client(_FakeStream([id_less]))
    ws = _make_ws()
    session_state = SessionState.from_device("demo")

    await forwarders._forward_session_to_ws(
        ws=ws, client=client, session_id="sesn_1", device_slug="demo",
        memory_root=tmp_path, events_by_id={}, session_state=session_state,
        agent_model="claude-opus-4-8", tier="deep", environment_id="env_test",
        repair_id="rep-1", conv_id=None,
    )

    recorder.assert_not_called()


@pytest.mark.asyncio
async def test_forwarder_still_emits_turn_cost_frame(monkeypatch, tmp_path):
    """The metering hook is additive — the turn_cost WS frame must still go out."""
    monkeypatch.setattr(rm, "get_settings", lambda: _Settings())
    monkeypatch.setattr(forwarders.cloud_metering, "fire_and_forget_report", MagicMock())

    stream = _FakeStream([_model_request_end("evt-1")])
    client = _make_client(stream)
    ws = _make_ws()
    session_state = SessionState.from_device("demo")

    await forwarders._forward_session_to_ws(
        ws=ws,
        client=client,
        session_id="sesn_1",
        device_slug="demo",
        memory_root=tmp_path,
        events_by_id={},
        session_state=session_state,
        agent_model="claude-opus-4-8",
        tier="deep",
        environment_id="env_test",
        repair_id="rep-1",
        conv_id=None,
    )

    sent_types = [c.args[0].get("type") for c in ws.send_json.await_args_list]
    assert "turn_cost" in sent_types
