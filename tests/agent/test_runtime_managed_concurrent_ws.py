# SPDX-License-Identifier: Apache-2.0
"""Concurrent-WebSocket tests for the Managed Agents stream loop.

These tests exercise scenarios where two `_forward_session_to_ws` calls
overlap in time, on the same device or across tiers, and assert the
runtime's current behaviour around four axes flagged by an architectural
audit:

* dedup of `agent.custom_tool_use` ids when two forwarders share state
  (`events_by_id`) but each maintain a private `responded_tool_ids` set;
* cleanup when a WebSocket close fires while a custom tool dispatch is
  in flight — the stream task must drain through cancellation and not
  orphan its `session_mirrors` children;
* tier isolation when two forwarders run on different tiers
  (`fast` vs `deep`) for the same device with independent
  `SessionState` instances — no crosstalk on `message` / `turn_complete`
  frames;
* concurrent mutation of a shared `SessionState.highlights` set when
  two forwarders accidentally share the same session — documents the
  current last-write-wins / set-merge behaviour without prescribing a
  Lock that does not exist in production code.

Some assertions DOCUMENT a real bug (see `# CURRENT BEHAVIOR — see audit`
markers). Those tests pin the present behaviour so a future fix breaks
them and forces the fixer to update the assertion intentionally — they
do NOT prescribe the buggy behaviour as desirable.

The mocks mimic the SDK's `AsyncStream` and the WS so the suite stays
fast (sub-second) and offline. Setup helpers are intentionally local
copies of the ones in `test_runtime_managed_e2e.py` to keep each test
file self-contained — diverging shapes between concurrent and serial
tests would obscure the contract under test here.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


class _FakeStream:
    """Async-iterable + async-context-manager mimicking AsyncAnthropic's stream.

    `events` is the queue to yield; `raise_after` (optional) is an exception
    to raise on the next `__anext__()` once the queue is drained.
    `gate` (optional asyncio.Event) blocks `__anext__()` until set —
    useful for coordinating two forwarders so they reach the same
    requires_action point at the same time.
    """

    def __init__(
        self,
        events,
        *,
        raise_after: Exception | None = None,
        gate: asyncio.Event | None = None,
    ):
        self._events = list(events)
        self._raise_after = raise_after
        self._gate = gate

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._gate is not None and not self._events:
            # Once the queue is drained, hold open until the test releases
            # — mimics MA keeping the SSE connection alive between turns.
            await self._gate.wait()
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


# ---------------------------------------------------------------------------
# Test 1 — shared events_by_id but per-task responded_tool_ids: BUG REVEALED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_ws_same_conv_dedup_tool_ids(monkeypatch, tmp_path):
    """Two forwarders share `events_by_id` but each carry their own
    `responded_tool_ids` set — so a tool_use that lands in both streams
    gets dispatched TWICE.

    Setup mirrors what would happen if two WebSockets opened on the same
    `(device_slug, repair_id, conv_id)` and were wired into the same
    in-memory event index (a future "sticky session" pool). MA echoes
    the same `agent.custom_tool_use` event on both streams (it's the same
    underlying session); each forwarder reads it and both reach the
    `requires_action` `event_ids=[eid]` pause independently.

    # CURRENT BEHAVIOR — see audit F1 (concurrent dedup)
    The dispatcher runs **once per forwarder** because `responded_tool_ids`
    is created locally in each `_forward_session_to_ws` call (see
    `runtime_managed.py:2533`). The intended dedup contract — "MA must
    never see two `user.custom_tool_result` for the same tool_use id" —
    is broken under concurrent forwarders sharing a session: MA will
    receive two responses and reject the second with HTTP 400.

    This test pins the bug. A fix that hoists `responded_tool_ids` to a
    shared (per-session) set will make this test fail with
    `len(dispatch_calls) == 1`; update the assertion at that point.
    """
    from api.agent import runtime_managed as rm
    from api.session.state import SessionState

    _stale_settings(monkeypatch, rm)

    # Count dispatch invocations across both forwarders.
    dispatch_calls: list[tuple[str, dict]] = []
    dispatch_lock = asyncio.Lock()  # serializes append; not on the SUT

    async def fake_dispatch(name, payload, *_a, **_kw):
        async with dispatch_lock:
            dispatch_calls.append((name, payload))
        return {"ok": True, "echo": payload}

    monkeypatch.setattr(rm, "_dispatch_tool", fake_dispatch)

    # Same eid across both streams — the entire point of the test. A
    # shared events_by_id dict is what would simulate "both forwarders
    # see the same upstream tool_use".
    eid = "sevt_shared_001"

    def _build_events():
        return [
            SimpleNamespace(
                type="agent.custom_tool_use",
                id=eid,
                name="bv_highlight_component",
                input={"refdes": "U7"},
            ),
            SimpleNamespace(
                type="session.status_idle",
                stop_reason=SimpleNamespace(
                    type="requires_action", event_ids=[eid],
                ),
            ),
            SimpleNamespace(
                type="session.status_idle",
                stop_reason=SimpleNamespace(type="end_turn", event_ids=[]),
            ),
        ]

    stream_a = _FakeStream(events=_build_events())
    stream_b = _FakeStream(events=_build_events())
    client_a = _make_client(stream_a)
    client_b = _make_client(stream_b)
    ws_a = _make_ws()
    ws_b = _make_ws()

    # CRITICAL: shared events_by_id between the two forwarders (the only
    # state that, by signature, COULD be shared today).
    shared_events_by_id: dict = {}

    session_state_a = SessionState.from_device("nonexistent-slug")
    session_state_b = SessionState.from_device("nonexistent-slug")

    await asyncio.gather(
        rm._forward_session_to_ws(
            ws=ws_a, client=client_a, session_id="sesn_shared",
            device_slug="demo", memory_root=tmp_path,
            events_by_id=shared_events_by_id,
            session_state=session_state_a,
            agent_model="claude-haiku-4-5", tier="fast",
            environment_id="env_test", repair_id=None, conv_id=None,
        ),
        rm._forward_session_to_ws(
            ws=ws_b, client=client_b, session_id="sesn_shared",
            device_slug="demo", memory_root=tmp_path,
            events_by_id=shared_events_by_id,
            session_state=session_state_b,
            agent_model="claude-haiku-4-5", tier="fast",
            environment_id="env_test", repair_id=None, conv_id=None,
        ),
    )

    # CURRENT BEHAVIOR — see audit: both forwarders dispatch the same
    # tool independently because responded_tool_ids is per-call local
    # state, not shared across forwarders. The "ideal" outcome would
    # be exactly 1, but the runtime does not coordinate across
    # concurrent forwarders today.
    assert len(dispatch_calls) == 2, (
        "Expected the documented bug: two concurrent forwarders sharing a "
        "session each run their own dispatch for the same tool_use eid. "
        f"Got {len(dispatch_calls)} dispatches: {dispatch_calls!r}"
    )
    assert all(
        call == ("bv_highlight_component", {"refdes": "U7"})
        for call in dispatch_calls
    )
    # Each forwarder also posts its own user.custom_tool_result back to
    # MA — the second one is what MA would reject with 400 in production.
    sent_a = [
        ev for call in client_a.beta.sessions.events.send.await_args_list
        for ev in call.kwargs.get("events", [])
        if ev.get("type") == "user.custom_tool_result"
    ]
    sent_b = [
        ev for call in client_b.beta.sessions.events.send.await_args_list
        for ev in call.kwargs.get("events", [])
        if ev.get("type") == "user.custom_tool_result"
    ]
    assert len(sent_a) == 1
    assert len(sent_b) == 1
    assert sent_a[0]["custom_tool_use_id"] == eid
    assert sent_b[0]["custom_tool_use_id"] == eid


# ---------------------------------------------------------------------------
# Test 2 — WS close mid-tool-dispatch must drain mirror tasks cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_close_mid_tool_dispatch_cleanup(monkeypatch, tmp_path):
    """WS closes (cancellation) while `_dispatch_tool` is awaiting — the
    forwarder task must observe its own cancel within a bounded window,
    no orphan mirror tasks may linger past `wait_drain`, and any task
    pushed onto `session_mirrors` before/around the cancel must drain
    or be cancelled (not stay forever-pending).

    Replicates the production sequence:
      * `_forward_ws_to_session` raises `WebSocketDisconnect` on
        `ws.receive_text()` because the browser tab closed;
      * the orchestrator (`_run_session_loop`) cancels the sibling
        `_forward_session_to_ws` task;
      * `wait_drain(timeout=5.0)` then runs in the orchestrator's
        finally block.
    """
    from api.agent import runtime_managed as rm
    from api.session.state import SessionState

    _stale_settings(monkeypatch, rm)

    dispatch_started = asyncio.Event()
    dispatch_release = asyncio.Event()  # never set in this test

    async def hanging_dispatch(name, payload, *_a, **_kw):
        dispatch_started.set()
        # Hang as if the tool were waiting on a slow downstream call.
        try:
            await dispatch_release.wait()
        except asyncio.CancelledError:
            # Real dispatch tools must cooperate with cancel — propagate.
            raise
        return {"ok": True}

    monkeypatch.setattr(rm, "_dispatch_tool", hanging_dispatch)

    eid = "sevt_hang_001"
    tool_use = SimpleNamespace(
        type="agent.custom_tool_use",
        id=eid,
        name="bv_highlight_component",
        input={"refdes": "U7"},
    )
    requires_action = SimpleNamespace(
        type="session.status_idle",
        stop_reason=SimpleNamespace(type="requires_action", event_ids=[eid]),
    )
    # gate keeps the stream open after the queue drains so the forwarder
    # is genuinely stuck inside _dispatch_tool when we cancel.
    keep_open = asyncio.Event()
    stream = _FakeStream(events=[tool_use, requires_action], gate=keep_open)
    client = _make_client(stream)
    ws = _make_ws()
    session_state = SessionState.from_device("nonexistent-slug")
    session_mirrors = rm._SessionMirrors()

    forwarder = asyncio.create_task(
        rm._forward_session_to_ws(
            ws=ws, client=client, session_id="sesn_hang",
            device_slug="demo", memory_root=tmp_path,
            events_by_id={}, session_state=session_state,
            agent_model="claude-haiku-4-5", tier="fast",
            environment_id="env_test", repair_id=None, conv_id=None,
            session_mirrors=session_mirrors,
        ),
        name="session->ws",
    )

    # Wait until the dispatch is in-flight, then trigger the WS-close path
    # by cancelling the forwarder (this is what _run_session_loop does
    # when its sibling task raises WebSocketDisconnect).
    await asyncio.wait_for(dispatch_started.wait(), timeout=1.0)
    forwarder.cancel()

    # Drain the forwarder, mirror the orchestrator's per-task wait pattern.
    await asyncio.wait({forwarder}, timeout=1.0)
    assert forwarder.done(), "forwarder must observe its cancel within the budget"
    assert forwarder.cancelled(), (
        "forwarder must report cancelled() — not just done() — so the "
        "orchestrator's post-cancel telemetry sees the right state"
    )

    # No orphan tasks may linger in session_mirrors past wait_drain.
    # In this test we never spawned via mirrors, but wait_drain must still
    # return promptly even with an empty pool — it's the contract the
    # orchestrator's finally block relies on.
    await asyncio.wait_for(session_mirrors.wait_drain(timeout=1.0), timeout=2.0)
    assert len(session_mirrors._pending) == 0, (
        "session_mirrors pool must be empty after WS-close cleanup; got "
        f"{len(session_mirrors._pending)} orphan tasks"
    )

    # Allow the gate to release any held tasks so pytest doesn't surface
    # a "task pending" warning at session teardown.
    keep_open.set()


@pytest.mark.asyncio
async def test_ws_close_mid_dispatch_drains_mirror_spawned_tasks(
    monkeypatch, tmp_path,
):
    """Companion to the test above: when a tool dispatch spawns a
    mirror task (e.g. the cam_capture / mb_validate_finding pattern) and
    the WS closes mid-dispatch, `wait_drain` must finish those mirror
    tasks (or cancel them) instead of returning while they're still
    pending.

    This is the F2 scenario seen from the orchestrator angle, not the
    `_forward_session_to_ws` angle: the mirror task does NOT live on
    the cancelled forwarder — it was created via `mirrors.spawn(...)`
    and survives the cancel. `wait_drain` is the safety net.
    """
    from api.agent import runtime_managed as rm

    _stale_settings(monkeypatch, rm)

    mirrors = rm._SessionMirrors()
    delivered: list[str] = []

    async def slow_mirror_send(label: str):
        await asyncio.sleep(0.02)
        delivered.append(label)

    # Simulate a dispatch that fires-and-forgets several mirror sends
    # before returning, then the WS closes.
    mirrors.spawn(slow_mirror_send("frame_a"))
    mirrors.spawn(slow_mirror_send("frame_b"))
    mirrors.spawn(slow_mirror_send("frame_c"))

    # The orchestrator's finally block runs wait_drain BEFORE cleaning
    # up the WS / global emitters; assert the mirrored sends survive
    # the cancellation of the forwarder above.
    await mirrors.wait_drain(timeout=2.0)
    assert sorted(delivered) == ["frame_a", "frame_b", "frame_c"], (
        f"all mirror sends must drain before teardown; got {delivered!r}"
    )
    assert len(mirrors._pending) == 0


# ---------------------------------------------------------------------------
# Test 3 — two forwarders on the same device, different tiers, isolated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_ws_different_tiers_isolated_state(monkeypatch, tmp_path):
    """Two forwarders run in parallel on the same device but on different
    tiers (`fast` / Haiku and `deep` / Opus) with independent
    `SessionState` and independent `events_by_id` dicts. Each must
    deliver ITS OWN `message` and `turn_complete` frames to ITS OWN WS
    — no crosstalk on either direction.
    """
    from api.agent import runtime_managed as rm
    from api.session.state import SessionState

    _stale_settings(monkeypatch, rm)

    monkeypatch.setattr(
        rm, "_dispatch_tool",
        AsyncMock(return_value={"ok": True}),
    )

    def _build_events(tag: str):
        # Each forwarder sees a unique text payload tagged with its tier
        # so we can assert non-crossover at the WS layer.
        return [
            SimpleNamespace(
                type="agent.message",
                content=[
                    SimpleNamespace(type="text", text=f"hello from {tag}"),
                ],
            ),
            SimpleNamespace(
                type="session.status_idle",
                stop_reason=SimpleNamespace(type="end_turn", event_ids=[]),
            ),
        ]

    stream_fast = _FakeStream(events=_build_events("FAST"))
    stream_deep = _FakeStream(events=_build_events("DEEP"))
    client_fast = _make_client(stream_fast)
    client_deep = _make_client(stream_deep)
    ws_fast = _make_ws()
    ws_deep = _make_ws()

    session_state_fast = SessionState.from_device("nonexistent-slug-a")
    session_state_deep = SessionState.from_device("nonexistent-slug-b")

    await asyncio.gather(
        rm._forward_session_to_ws(
            ws=ws_fast, client=client_fast, session_id="sesn_fast",
            device_slug="demo", memory_root=tmp_path,
            events_by_id={}, session_state=session_state_fast,
            agent_model="claude-haiku-4-5", tier="fast",
            environment_id="env_test", repair_id=None, conv_id=None,
        ),
        rm._forward_session_to_ws(
            ws=ws_deep, client=client_deep, session_id="sesn_deep",
            device_slug="demo", memory_root=tmp_path,
            events_by_id={}, session_state=session_state_deep,
            agent_model="claude-opus-4-7", tier="deep",
            environment_id="env_test", repair_id=None, conv_id=None,
        ),
    )

    payloads_fast = [c.args[0] for c in ws_fast.send_json.await_args_list]
    payloads_deep = [c.args[0] for c in ws_deep.send_json.await_args_list]

    msgs_fast = [p for p in payloads_fast if p.get("type") == "message"]
    msgs_deep = [p for p in payloads_deep if p.get("type") == "message"]
    assert len(msgs_fast) == 1
    assert len(msgs_deep) == 1
    assert msgs_fast[0]["text"] == "hello from FAST", (
        f"fast WS got crosstalk: {msgs_fast!r}"
    )
    assert msgs_deep[0]["text"] == "hello from DEEP", (
        f"deep WS got crosstalk: {msgs_deep!r}"
    )

    turn_fast = [p for p in payloads_fast if p.get("type") == "turn_complete"]
    turn_deep = [p for p in payloads_deep if p.get("type") == "turn_complete"]
    assert len(turn_fast) == 1
    assert len(turn_deep) == 1

    # Strict no-crosstalk check: the FAST text never landed on the deep WS
    # and vice versa.
    deep_texts = {p.get("text") for p in payloads_deep if "text" in p}
    fast_texts = {p.get("text") for p in payloads_fast if "text" in p}
    assert "hello from FAST" not in deep_texts
    assert "hello from DEEP" not in fast_texts


# ---------------------------------------------------------------------------
# Test 4 — concurrent mutation of a shared SessionState.highlights set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_ws_concurrent_session_state_board_mutation(
    monkeypatch, tmp_path,
):
    """Two forwarders accidentally share the same `SessionState` (would
    happen if a per-device session pool returned the same instance for
    two concurrent WS) and each receive a `bv_highlight_component`
    tool_use for a distinct refdes. The dispatcher mutates
    `session_state.highlights` directly.

    # CURRENT BEHAVIOR — see audit F4 (no SessionState locking)
    `SessionState.highlights` is a plain `set[str]` with no lock and
    `dispatch_bv → highlight_component` does a non-atomic
    `session.highlights = set(); session.highlights.update(targets)`
    when `additive=False` (the default). On two concurrent calls with
    different refdes, the final state can be:
      * {"U7", "R5"} when both updates land after both resets, or
      * {"U7"} or {"R5"} when one reset clobbers the other's update.

    Asyncio without explicit yields inside the dispatch makes the race
    rare on a single thread, so this test asserts the loose invariant
    "the final set is non-empty and is a subset of the requested
    refdes" without prescribing a Lock. Pin the behaviour as documented
    so a future fix that introduces ordering or merging changes the
    invariant intentionally.
    """
    from api.agent import runtime_managed as rm
    from api.session.state import SessionState

    _stale_settings(monkeypatch, rm)

    # We need a real bv_highlight_component dispatch so `session.highlights`
    # actually mutates. Build a minimal Board-shaped object whose
    # `validator.is_valid_refdes` accepts U7 and R5.
    class _StubBoard:
        def __init__(self, refdes_set: set[str]):
            self._refdes = refdes_set
            # The validator does `not is_valid_refdes(session.board, ...)`,
            # which calls `board.part_by_refdes(r)` — provide a dict.
            self._part_by_refdes_cache = {r: SimpleNamespace(refdes=r) for r in refdes_set}

        def part_by_refdes(self, refdes: str):
            return self._part_by_refdes_cache.get(refdes)

        @property
        def parts(self):
            return [SimpleNamespace(refdes=r) for r in self._refdes]

    # Patch the validator's `is_valid_refdes` so our stub board is accepted
    # without going through the real `Board` model_post_init machinery.
    from api.board import validator as _v

    real_is_valid = _v.is_valid_refdes

    def stub_is_valid(board, refdes):
        if isinstance(board, _StubBoard):
            return refdes in board._refdes
        return real_is_valid(board, refdes)

    monkeypatch.setattr(_v, "is_valid_refdes", stub_is_valid)
    monkeypatch.setattr(
        "api.tools.boardview.is_valid_refdes", stub_is_valid,
    )

    shared_session = SessionState()
    shared_session.board = _StubBoard({"U7", "R5"})

    eid_a = "sevt_mut_001"
    eid_b = "sevt_mut_002"

    def _build_stream(eid: str, refdes: str):
        return _FakeStream(events=[
            SimpleNamespace(
                type="agent.custom_tool_use",
                id=eid,
                name="bv_highlight",
                input={"refdes": refdes, "additive": True},
            ),
            SimpleNamespace(
                type="session.status_idle",
                stop_reason=SimpleNamespace(
                    type="requires_action", event_ids=[eid],
                ),
            ),
            SimpleNamespace(
                type="session.status_idle",
                stop_reason=SimpleNamespace(type="end_turn", event_ids=[]),
            ),
        ])

    stream_a = _build_stream(eid_a, "U7")
    stream_b = _build_stream(eid_b, "R5")
    client_a = _make_client(stream_a)
    client_b = _make_client(stream_b)
    ws_a = _make_ws()
    ws_b = _make_ws()

    await asyncio.gather(
        rm._forward_session_to_ws(
            ws=ws_a, client=client_a, session_id="sesn_mut_a",
            device_slug="demo", memory_root=tmp_path,
            events_by_id={}, session_state=shared_session,
            agent_model="claude-haiku-4-5", tier="fast",
            environment_id="env_test", repair_id=None, conv_id=None,
        ),
        rm._forward_session_to_ws(
            ws=ws_b, client=client_b, session_id="sesn_mut_b",
            device_slug="demo", memory_root=tmp_path,
            events_by_id={}, session_state=shared_session,
            agent_model="claude-haiku-4-5", tier="fast",
            environment_id="env_test", repair_id=None, conv_id=None,
        ),
    )

    # CURRENT BEHAVIOR — see audit F4: with `additive=True` both writes
    # merge into the shared set without any data loss because each call
    # only does a `session.highlights.update([refdes])`. Both refdes must
    # be present.
    assert shared_session.highlights == {"U7", "R5"}, (
        "additive bv_highlight calls on a shared session must merge into "
        f"a {{U7, R5}} set; got {shared_session.highlights!r}. "
        "If this assertion fails with a strict subset, the runtime is "
        "racing on the highlights set even in additive mode and a Lock "
        "or per-WS SessionState is required."
    )


@pytest.mark.asyncio
async def test_two_ws_concurrent_non_additive_highlight_documents_clobber(
    monkeypatch, tmp_path,
):
    """Companion to the above: two `bv_highlight` calls with the default
    `additive=False` on a shared `SessionState` clobber each other's
    set because each call resets `session.highlights = set()` before
    inserting its own refdes.

    # CURRENT BEHAVIOR — see audit F4 (non-additive write clobbers peer)
    The final set contains exactly ONE of the two refdes, not both. On
    a single asyncio thread with no awaits inside the synchronous
    `highlight_component` body, the runtime serializes the two calls and
    the second one wins. Documents the expectation that without a per-WS
    `SessionState`, concurrent highlights destroy each other's view
    state. Fix path: per-WS `SessionState`, NOT a Lock — locks would
    serialize but the user-facing semantics would still be "last call
    wins on the rendered overlay".
    """
    from api.agent import runtime_managed as rm
    from api.session.state import SessionState

    _stale_settings(monkeypatch, rm)

    class _StubBoard:
        def __init__(self, refdes_set: set[str]):
            self._refdes = refdes_set
            self._part_by_refdes_cache = {
                r: SimpleNamespace(refdes=r) for r in refdes_set
            }

        def part_by_refdes(self, refdes: str):
            return self._part_by_refdes_cache.get(refdes)

        @property
        def parts(self):
            return [SimpleNamespace(refdes=r) for r in self._refdes]

    from api.board import validator as _v
    real_is_valid = _v.is_valid_refdes

    def stub_is_valid(board, refdes):
        if isinstance(board, _StubBoard):
            return refdes in board._refdes
        return real_is_valid(board, refdes)

    monkeypatch.setattr(_v, "is_valid_refdes", stub_is_valid)
    monkeypatch.setattr(
        "api.tools.boardview.is_valid_refdes", stub_is_valid,
    )

    shared_session = SessionState()
    shared_session.board = _StubBoard({"U7", "R5"})

    eid_a = "sevt_clobber_001"
    eid_b = "sevt_clobber_002"

    def _build_stream(eid: str, refdes: str):
        return _FakeStream(events=[
            SimpleNamespace(
                type="agent.custom_tool_use",
                id=eid,
                name="bv_highlight",
                input={"refdes": refdes},  # additive defaults to False
            ),
            SimpleNamespace(
                type="session.status_idle",
                stop_reason=SimpleNamespace(
                    type="requires_action", event_ids=[eid],
                ),
            ),
            SimpleNamespace(
                type="session.status_idle",
                stop_reason=SimpleNamespace(type="end_turn", event_ids=[]),
            ),
        ])

    stream_a = _build_stream(eid_a, "U7")
    stream_b = _build_stream(eid_b, "R5")
    client_a = _make_client(stream_a)
    client_b = _make_client(stream_b)
    ws_a = _make_ws()
    ws_b = _make_ws()

    await asyncio.gather(
        rm._forward_session_to_ws(
            ws=ws_a, client=client_a, session_id="sesn_cl_a",
            device_slug="demo", memory_root=tmp_path,
            events_by_id={}, session_state=shared_session,
            agent_model="claude-haiku-4-5", tier="fast",
            environment_id="env_test", repair_id=None, conv_id=None,
        ),
        rm._forward_session_to_ws(
            ws=ws_b, client=client_b, session_id="sesn_cl_b",
            device_slug="demo", memory_root=tmp_path,
            events_by_id={}, session_state=shared_session,
            agent_model="claude-haiku-4-5", tier="fast",
            environment_id="env_test", repair_id=None, conv_id=None,
        ),
    )

    # CURRENT BEHAVIOR — see audit F4: only one refdes survives. Which
    # one depends on scheduler ordering — both are valid outcomes today.
    final = shared_session.highlights
    assert final in ({"U7"}, {"R5"}), (
        "with additive=False on a shared session, the final highlights set "
        f"must contain exactly one of the two refdes (clobber); got {final!r}. "
        "If this assertion fails with {U7, R5}, the runtime acquired "
        "ordering protection — update the test to match."
    )
