"""Mid-session boardview import — the agent must be TOLD, not just served.

Defect: in managed mode a boardview imported after WS open was only picked
up by the pre-dispatch refresh in `dispatch_tool` — but an agent told
"no board" at session start never calls a bv_* tool, so it never learned a
board had arrived. Fix: both runtimes re-resolve the active boardview on
every user turn and, when it actually changed, prepend a ctx-style
`board_status` line to that turn's user message. The line starts with
CTX_TAG_PREFIX so `strip_ctx_tag` drops it from chat replays exactly like
the per-turn ctx tag.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocketDisconnect

from api.agent.chat_history import (
    CTX_TAG_PREFIX,
    build_board_refresh_note,
    strip_ctx_tag,
)
from api.session.state import SessionState

FAKE_BOARD = SimpleNamespace(
    board_id="demo", parts=[1, 2, 3], nets=[1, 2],
)


# --------------------------------------------------------------------------- #
# The note itself
# --------------------------------------------------------------------------- #

def test_note_is_ctx_prefixed_and_carries_board_identity():
    note = build_board_refresh_note(FAKE_BOARD, Path("/x/uploads/iphone13.brd"))
    assert note.startswith(CTX_TAG_PREFIX)
    assert "board_status" in note
    assert "iphone13.brd" in note
    assert "3 parts" in note
    assert "2 nets" in note


def test_note_falls_back_to_board_id_without_source():
    note = build_board_refresh_note(FAKE_BOARD, None)
    assert '"demo"' in note


def test_strip_ctx_tag_drops_note_alone():
    text = build_board_refresh_note(FAKE_BOARD, None) + "\n\nhello"
    assert strip_ctx_tag(text) == "hello"


def test_strip_ctx_tag_drops_ctx_tag_and_note_as_one_block():
    ctx_tag = f"{CTX_TAG_PREFIX} device=demo (demo)]"
    note = build_board_refresh_note(FAKE_BOARD, None)
    text = f"{ctx_tag}\n{note}\n\nhello"
    assert strip_ctx_tag(text) == "hello"


# --------------------------------------------------------------------------- #
# Managed forwarder — note injected on the turn after the board appears
# --------------------------------------------------------------------------- #

def _ws_with_one_message(text: str) -> MagicMock:
    ws = MagicMock()
    ws.receive_text = AsyncMock(
        side_effect=[json.dumps({"text": text}), WebSocketDisconnect()]
    )
    ws.send_json = AsyncMock()
    return ws


def _capturing_client() -> tuple[MagicMock, list]:
    sent: list = []
    client = MagicMock()

    async def _send(session_id, *, events):
        sent.append(events)

    client.beta.sessions.events.send = AsyncMock(side_effect=_send)
    return client, sent


@pytest.mark.asyncio
async def test_managed_forwarder_injects_note_when_board_appears(tmp_path):
    # Import via the shim — importing the sub-module directly triggers the
    # runtime_managed <-> forwarders import cycle in a cold process.
    from api.agent.runtime_managed import _forward_ws_to_session

    session_state = SessionState()
    session_state.board = FAKE_BOARD  # type: ignore[assignment]
    session_state.board_source = tmp_path / "iphone13.brd"
    session_state.refresh_board_if_changed = lambda: True  # type: ignore[method-assign]

    ws = _ws_with_one_message("le board est importé")
    client, sent = _capturing_client()

    with pytest.raises(WebSocketDisconnect):
        await _forward_ws_to_session(
            ws, client, "sesn_x",
            ctx_tag=f"{CTX_TAG_PREFIX} device=demo (demo)]",
            session_state=session_state,
        )

    assert sent, "no user.message reached the MA session"
    text = sent[0][0]["content"][0]["text"]
    assert "board_status" in text
    assert "iphone13.brd" in text
    # Replay paths must reconstruct the bare user message.
    assert strip_ctx_tag(text) == "le board est importé"


@pytest.mark.asyncio
async def test_managed_forwarder_no_note_when_board_unchanged():
    # Import via the shim — importing the sub-module directly triggers the
    # runtime_managed <-> forwarders import cycle in a cold process.
    from api.agent.runtime_managed import _forward_ws_to_session

    session_state = SessionState()
    session_state.refresh_board_if_changed = lambda: False  # type: ignore[method-assign]

    ws = _ws_with_one_message("salut")
    client, sent = _capturing_client()

    with pytest.raises(WebSocketDisconnect):
        await _forward_ws_to_session(
            ws, client, "sesn_x",
            ctx_tag=f"{CTX_TAG_PREFIX} device=demo (demo)]",
            session_state=session_state,
        )

    text = sent[0][0]["content"][0]["text"]
    assert "board_status" not in text
    assert strip_ctx_tag(text) == "salut"


# --------------------------------------------------------------------------- #
# Direct runtime — source-level locks (the loop is too big to harness here;
# same discipline as test_runtime_conv_id_dispatch.py)
# --------------------------------------------------------------------------- #

def test_direct_loop_refreshes_board_and_recomputes_snapshot():
    from api.agent import runtime_direct

    src = inspect.getsource(runtime_direct.run_diagnostic_session_direct)
    assert "refresh_board_if_changed" in src, (
        "the direct loop no longer re-resolves the active boardview per "
        "user turn — a mid-session import becomes invisible again"
    )
    assert "build_tools_manifest(session)" in src
    assert "render_system_prompt(" in src
    assert "build_board_refresh_note(" in src


def test_no_board_result_carries_retry_hint():
    from api.tools.boardview import highlight_component

    result = highlight_component(SessionState(), refdes="U1")
    assert result["reason"] == "no-board-loaded"
    assert "hint" in result
    assert "import" in result["hint"]
