# SPDX-License-Identifier: Apache-2.0
"""End-to-end test for /ws/diagnostic/{slug} over a real TestClient WebSocket.

Unlike the existing tests in test_ws_flow.py — which drive the runtime loop
with a `FakeWS` double and skip routing entirely — this file opens the WS
through FastAPI's TestClient (`client.websocket_connect(...)`). That means
every layer between the browser and the runtime is exercised: URL routing,
query-param parsing (`tier`, `repair`, `conv`), the `DIAGNOSTIC_MODE`
dispatch in api.main, the WS accept handshake, and the exact JSON frame
protocol the frontend sees on the wire.

Anthropic is mocked at the `AsyncAnthropic` import boundary of
`api.agent.runtime_direct`, so no network is touched and `ANTHROPIC_API_KEY`
doesn't need to be set.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from api.agent.chat_history import append_event, ensure_conversation
from api.board.model import Board, Layer, Part, Pin, Point
from api.main import app
from api.session.state import SessionState

# ----------------------------------------------------------------------------
# Fake stream helpers — mirror the shape of client.messages.stream(...) just
# enough for the direct runtime. Kept local to avoid cross-importing private
# helpers from test_ws_flow.py.
# ----------------------------------------------------------------------------


class _FakeStream:
    """Async context manager + iterator that yields scripted stream events."""

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
    stop_ev = MagicMock()
    stop_ev.type = "content_block_stop"
    stop_ev.index = 0
    events = [(stop_ev, [block])]
    # usage has to be real ints — cost_from_response runs getattr on this and
    # the result ends up inside a JSON-encoded turn_cost frame.
    final = MagicMock(content=[block], stop_reason="end_turn", usage=_FAKE_USAGE)
    return events, final


def _stream_tool_use(
    name: str, tool_input: dict, tool_id: str = "toolu_1"
) -> tuple[list[tuple], MagicMock]:
    block = MagicMock(type="tool_use", input=tool_input, id=tool_id)
    block.name = name
    final = MagicMock(content=[block], stop_reason="tool_use", usage=_FAKE_USAGE)
    return [], final


def _mock_anthropic(scripted: list[tuple[list[tuple], MagicMock]]) -> MagicMock:
    iterator = iter(scripted)
    client = MagicMock()

    def _stream_factory(**_kwargs):
        events, final = next(iterator)
        return _FakeStream(events, final)

    client.messages.stream = _stream_factory
    return client


def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    api_key: str = "sk-fake",
    memory_root: str = "/tmp/ws-e2e",
    board: Board | None = None,
    scripted: list | None = None,
) -> MagicMock:
    """Patch api.agent.runtime_direct so the real WS endpoint hits a mocked
    Anthropic client, a stub SessionState, and a fake settings object.

    Returns the mocked AsyncAnthropic client for further assertions.
    """
    import api.agent.runtime_direct as rt
    monkeypatch.setenv("DIAGNOSTIC_MODE", "direct")

    def _from_device(_slug: str) -> SessionState:
        s = SessionState()
        if board is not None:
            s.set_board(board)
        return s

    monkeypatch.setattr(
        "api.agent.runtime_direct.SessionState.from_device",
        staticmethod(_from_device),
    )
    monkeypatch.setattr(rt, "get_settings", lambda: MagicMock(
        anthropic_api_key=api_key,
        memory_root=memory_root,
        anthropic_model_main="claude-opus-4-7",
        anthropic_max_retries=5,
    ))
    fake_client = _mock_anthropic(scripted or [_stream_text("hello")])
    monkeypatch.setattr(rt, "AsyncAnthropic", lambda **_kw: fake_client)
    return fake_client


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------


def test_ws_diagnostic_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open /ws/diagnostic/demo-pi?tier=fast, send one message, read back the
    session_ready ack, an assistant text frame and the turn_cost marker."""
    _patch_runtime(monkeypatch, scripted=[_stream_text("Hello tech.")])

    with TestClient(app) as client, client.websocket_connect(
        "/ws/diagnostic/demo-pi?tier=fast"
    ) as ws:
        ready = ws.receive_json()
        assert ready["type"] == "session_ready"
        assert ready["mode"] == "direct"
        assert ready["device_slug"] == "demo-pi"
        assert ready["tier"] == "fast"
        assert ready["board_loaded"] is False
        assert ready["repair_id"] is None

        ws.send_json({"type": "message", "text": "what's up"})

        frames: list[dict] = []
        for _ in range(10):
            frame = ws.receive_json()
            frames.append(frame)
            if frame.get("type") == "turn_cost":
                break

        types = [f.get("type") for f in frames]
        assert "message" in types, frames
        assert "turn_cost" in types, frames
        assistant = next(
            f for f in frames
            if f.get("type") == "message" and f.get("role") == "assistant"
        )
        assert assistant["text"] == "Hello tech."


def test_ws_diagnostic_rejects_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an API key, the server emits an error frame and closes — the
    runtime refuses to spin up an Anthropic client."""
    _patch_runtime(monkeypatch, api_key="", scripted=[_stream_text("unused")])

    with TestClient(app) as client, client.websocket_connect(
        "/ws/diagnostic/demo-pi?tier=fast"
    ) as ws:
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert "ANTHROPIC_API_KEY" in frame["text"]


def test_ws_diagnostic_invalid_tier_falls_back_to_deep(monkeypatch: pytest.MonkeyPatch) -> None:
    """A garbage `tier` query param is silently downgraded to the default
    (`deep`, Opus) so the session always opens rather than 400ing the
    browser."""
    _patch_runtime(monkeypatch, scripted=[_stream_text("ok")])

    with TestClient(app) as client, client.websocket_connect(
        "/ws/diagnostic/demo-pi?tier=bogus"
    ) as ws:
        ready = ws.receive_json()
        assert ready["type"] == "session_ready"
        assert ready["tier"] == "deep"


def test_ws_diagnostic_sanitizes_unknown_refdes_over_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sanitizer must wrap an unknown refdes in the outbound `message`
    frame — anti-hallucination guarantee, measured through the real WS."""
    board = Board(
        board_id="t", file_hash="sha256:x", source_format="t",
        outline=[],
        parts=[Part(
            refdes="U7", layer=Layer.TOP, is_smd=True,
            bbox=(Point(x=0, y=0), Point(x=10, y=10)), pin_refs=[0, 1],
        )],
        pins=[
            Pin(part_refdes="U7", index=1, pos=Point(x=2, y=2), layer=Layer.TOP),
            Pin(part_refdes="U7", index=2, pos=Point(x=8, y=8), layer=Layer.TOP),
        ],
        nets=[], nails=[],
    )
    _patch_runtime(
        monkeypatch, board=board,
        scripted=[_stream_text("U999 is suspect, U7 is fine.")],
    )

    with TestClient(app) as client, client.websocket_connect(
        "/ws/diagnostic/demo-pi?tier=fast"
    ) as ws:
        ws.receive_json()  # session_ready
        ws.send_json({"type": "message", "text": "diagnose"})

        # First frame that is an assistant message.
        assistant_text = None
        for _ in range(10):
            frame = ws.receive_json()
            if frame.get("type") == "message" and frame.get("role") == "assistant":
                assistant_text = frame["text"]
                break
        assert assistant_text is not None
        assert "⟨?U999⟩" in assistant_text
        assert "U7 is fine" in assistant_text


def test_ws_diagnostic_bv_tool_dispatch_emits_board_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bv_* tool use must surface as both a `tool_use` frame and the
    corresponding `boardview.*` event in the wire order the frontend
    expects (tool_use precedes the board mutation)."""
    board = Board(
        board_id="t", file_hash="sha256:x", source_format="t",
        outline=[],
        parts=[Part(
            refdes="U7", layer=Layer.TOP, is_smd=True,
            bbox=(Point(x=0, y=0), Point(x=10, y=10)), pin_refs=[0, 1],
        )],
        pins=[
            Pin(part_refdes="U7", index=1, pos=Point(x=2, y=2), layer=Layer.TOP),
            Pin(part_refdes="U7", index=2, pos=Point(x=8, y=8), layer=Layer.TOP),
        ],
        nets=[], nails=[],
    )
    _patch_runtime(
        monkeypatch, board=board,
        scripted=[
            _stream_tool_use("bv_highlight", {"refdes": "U7"}),
            _stream_text("Mis en évidence."),
        ],
    )

    with TestClient(app) as client, client.websocket_connect(
        "/ws/diagnostic/demo-pi?tier=fast"
    ) as ws:
        ready = ws.receive_json()
        assert ready["board_loaded"] is True

        ws.send_json({"type": "message", "text": "show U7"})

        # Scripted flow emits exactly 5 frames:
        # turn_cost (stream 1) / tool_use / boardview.highlight /
        # message (stream 2) / turn_cost (stream 2). Pull them explicitly so
        # a missing boardview event fails the test fast instead of blocking
        # forever on receive_json.
        frames = [ws.receive_json() for _ in range(5)]

        types = [f.get("type", "") for f in frames]
        tu_idx = types.index("tool_use")
        bv_idx = next(i for i, t in enumerate(types) if t.startswith("boardview."))
        assert tu_idx < bv_idx, (
            "tool_use must come before its boardview side-effect event"
        )
        tool_use = frames[tu_idx]
        assert tool_use["name"] == "bv_highlight"
        assert tool_use["input"] == {"refdes": "U7"}


# ----------------------------------------------------------------------------
# Managed Agents runtime — isolate the session_ready + memory-store attach
# behavior. Mocks stop at _forward_*_to_session so we don't have to fake the
# full MA event stream; those two tasks are proven out elsewhere.
# ----------------------------------------------------------------------------


def _patch_managed_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    memory_root: str,
    api_key: str = "sk-fake",
    memory_store_id: str | None = None,
    board: Board | None = None,
    created_session_id: str = "sess_test",
) -> MagicMock:
    """Wire api.agent.runtime_managed's dependencies to in-memory fakes.

    Patches just enough that run_diagnostic_session_managed can reach its
    session_ready emission and the intro / replay path. The two forward tasks
    (ws↔session) are stubbed to async no-ops so the runtime unblocks and the
    WS close handshake runs immediately.
    """
    import api.agent.runtime_managed as rm

    monkeypatch.setenv("DIAGNOSTIC_MODE", "managed")
    monkeypatch.setattr(rm, "get_settings", lambda: MagicMock(
        anthropic_api_key=api_key,
        memory_root=memory_root,
        anthropic_max_retries=5,
        ma_memory_store_enabled=False,
        chat_history_backend="jsonl",
    ))
    monkeypatch.setattr(rm, "load_managed_ids", lambda: {
        "environment_id": "env_test", "agents": {},
    })
    monkeypatch.setattr(rm, "get_agent", lambda ids, tier: {
        "id": "ag_fast", "version": 1, "model": "claude-haiku-4-5",
    })

    async def _fake_ensure(_client, _slug):
        return memory_store_id
    monkeypatch.setattr(rm, "ensure_memory_store", _fake_ensure)

    async def _fake_auto_seed(**_kw):
        return None
    monkeypatch.setattr(rm, "maybe_auto_seed", _fake_auto_seed)

    def _from_device(_slug: str) -> SessionState:
        s = SessionState()
        if board is not None:
            s.set_board(board)
        return s
    monkeypatch.setattr(
        "api.agent.runtime_managed.SessionState.from_device",
        staticmethod(_from_device),
    )

    # Capture the kwargs passed to sessions.create so tests can assert the
    # memory_store resource attachment without needing deep stream mocking.
    created_kwargs: dict = {}

    class _FakeSessions:
        async def create(self, **kwargs):
            created_kwargs.update(kwargs)
            sess = MagicMock()
            sess.id = created_session_id
            agent = MagicMock()
            agent.id = "ag_fast"
            sess.agent = agent
            return sess

        async def retrieve(self, _sid):
            raise RuntimeError("fresh session path")

    class _FakeBeta:
        sessions = _FakeSessions()

    class _FakeClient:
        beta = _FakeBeta()

    monkeypatch.setattr(rm, "AsyncAnthropic", lambda **_kw: _FakeClient())

    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr(rm, "_forward_ws_to_session", _noop)
    monkeypatch.setattr(rm, "_forward_session_to_ws", _noop)

    captured = MagicMock()
    captured.session_create_kwargs = created_kwargs
    return captured


def test_ws_diagnostic_managed_session_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Managed mode: WS open yields a session_ready frame tagged with the
    agent's model, the fresh session id, and a None memory_store_id when
    the store flag is off."""
    _patch_managed_runtime(monkeypatch, memory_root=str(tmp_path))

    with TestClient(app) as client, client.websocket_connect(
        "/ws/diagnostic/demo?tier=fast"
    ) as ws:
        ready = ws.receive_json()
        assert ready["type"] == "session_ready"
        assert ready["mode"] == "managed"
        assert ready["session_id"] == "sess_test"
        assert ready["memory_store_id"] is None
        assert ready["device_slug"] == "demo"
        assert ready["tier"] == "fast"
        assert ready["model"] == "claude-haiku-4-5"
        assert ready["board_loaded"] is False
        assert ready["repair_id"] is None


def test_ws_diagnostic_managed_attaches_memory_store_readonly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When ensure_memory_store resolves a store id, the session create
    payload must include that store as a read_only resource — the device
    history is a read mount for the agent, never a write path."""
    captured = _patch_managed_runtime(
        monkeypatch, memory_root=str(tmp_path),
        memory_store_id="memstore_abc",
    )

    with TestClient(app) as client, client.websocket_connect(
        "/ws/diagnostic/demo?tier=fast"
    ) as ws:
        ready = ws.receive_json()
        assert ready["memory_store_id"] == "memstore_abc"

    kwargs = captured.session_create_kwargs
    resources = kwargs.get("resources") or []
    assert resources, "managed session must attach memory_store as a resource"
    resource = resources[0]
    assert resource["type"] == "memory_store"
    assert resource["memory_store_id"] == "memstore_abc"
    assert resource["access"] == "read_only"


# ----------------------------------------------------------------------------
# Repair resumption — direct mode scoped to an existing repair_id. Covers
# both the fresh-repair path (context_loaded frame emitted once) and the
# resume path (history_replay_start / per-event frames / history_replay_end).
# ----------------------------------------------------------------------------


def _write_repair_meta(memory_root: Path, slug: str, repair_id: str, *, symptom: str) -> None:
    repairs = memory_root / slug / "repairs"
    repairs.mkdir(parents=True, exist_ok=True)
    (repairs / f"{repair_id}.json").write_text(json.dumps({
        "device_label": "Demo Device",
        "symptom": symptom,
    }))


def test_ws_diagnostic_direct_fresh_repair_emits_context_loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Opening ?repair=R1 on a repair with no prior conversation should:
      - create a fresh conversation (session_ready.conv_id is populated),
      - NOT replay any history,
      - emit a single context_loaded frame so the client can stamp the
        device context in its chat panel before the tech types anything.
    """
    slug, repair_id = "demo-pi", "R1"
    _write_repair_meta(tmp_path, slug, repair_id, symptom="no boot 3V3 missing")
    _patch_runtime(
        monkeypatch, memory_root=str(tmp_path),
        scripted=[_stream_text("unused — tech never sends")],
    )

    with TestClient(app) as client, client.websocket_connect(
        f"/ws/diagnostic/{slug}?tier=fast&repair={repair_id}"
    ) as ws:
        ready = ws.receive_json()
        assert ready["type"] == "session_ready"
        assert ready["repair_id"] == repair_id
        assert ready["conv_id"], "a fresh conversation must be minted"

        # The fresh-repair branch in run_diagnostic_session_direct emits a
        # `boardview.reset_view` (per-conv canvas wipe — keeps the renderer
        # from inheriting the previous conv's overlay) followed by the
        # `context_loaded` signal that gates user input. Drain frames
        # until we hit context_loaded so this test stays robust to other
        # bootstrap events the runtime may insert in front later.
        ctx = None
        for _ in range(8):
            frame = ws.receive_json()
            if frame.get("type") == "context_loaded":
                ctx = frame
                break
        assert ctx is not None, "context_loaded never arrived after session_ready"
        assert ctx["device_slug"] == slug
        assert ctx["repair_id"] == repair_id


def test_ws_diagnostic_direct_replays_prior_conversation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When a repair already has a persisted conversation, the WS open must
    emit history_replay_start → past events (user + assistant) → history
    replay_end before the live receive loop begins. Each replayed frame is
    stamped replay:true so the UI can render it differently from live text."""
    slug, repair_id = "demo-pi", "R1"
    memory_root = tmp_path
    _write_repair_meta(memory_root, slug, repair_id, symptom="capture")

    # Seed a conversation via the real helpers so the on-disk layout matches
    # exactly what the runtime reads back.
    conv_id, _created = ensure_conversation(
        device_slug=slug, repair_id=repair_id, conv_id="new",
        tier="fast", memory_root=memory_root,
    )
    append_event(
        device_slug=slug, repair_id=repair_id, conv_id=conv_id,
        memory_root=memory_root,
        event={"role": "user", "content": "what's wrong?"},
    )
    append_event(
        device_slug=slug, repair_id=repair_id, conv_id=conv_id,
        memory_root=memory_root,
        event={
            "role": "assistant",
            "content": [{"type": "text", "text": "Probably U7."}],
        },
    )

    _patch_runtime(
        monkeypatch, memory_root=str(memory_root),
        scripted=[_stream_text("unused")],
    )

    with TestClient(app) as client, client.websocket_connect(
        f"/ws/diagnostic/{slug}?tier=fast&repair={repair_id}&conv={conv_id}"
    ) as ws:
        ready = ws.receive_json()
        assert ready["type"] == "session_ready"
        assert ready["repair_id"] == repair_id
        assert ready["conv_id"] == conv_id

        # Expected replay sequence (with the per-conv canvas wipe in front):
        #   boardview.reset_view, history_replay_start, message(user, replay),
        #   message(assistant, replay), history_replay_end. Filter on the
        #   chat-history bracket so this stays robust to other bootstrap
        #   events. Read until the history_replay_end sentinel rather than a
        #   hardcoded count — the bootstrap event mix evolves (board_state,
        #   recovery_state, etc.) and a fixed range() hangs on `receive_json()`
        #   when fewer events arrive.
        all_frames: list[dict] = []
        for _ in range(20):  # safety cap
            f = ws.receive_json()
            all_frames.append(f)
            if f.get("type") == "history_replay_end":
                break
        frames = [
            f for f in all_frames
            if f.get("type") in (
                "history_replay_start", "history_replay_end", "message",
            )
        ]
        types = [f.get("type") for f in frames]
        assert types[0] == "history_replay_start"
        assert types[-1] == "history_replay_end"
        assert frames[0]["count"] == 2

        user_replay = next(
            f for f in frames
            if f.get("type") == "message" and f.get("role") == "user"
        )
        assert user_replay["text"] == "what's wrong?"
        # User messages aren't flagged `replay`: the UI differentiates them
        # from live input by being inside the replay window (between start
        # and end frames), not by a per-message flag. Only assistant text
        # carries replay:true so the streaming renderer doesn't re-animate.

        asst_replay = next(
            f for f in frames
            if f.get("type") == "message" and f.get("role") == "assistant"
        )
        assert asst_replay["text"] == "Probably U7."
        assert asst_replay["replay"] is True
