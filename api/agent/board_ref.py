"""Per-session board-number context for the diagnostic agent.

The board number (e.g. "820-02016") identifies the PCB revision being
diagnosed and determines which board-delta pack the agent loads. It is
bound once at the top of a diagnostic session from the ``?board=`` query
parameter on the WS handshake, and read by board-delta-aware tools to
inject the revision-specific delta into the session context.

A ContextVar (not a module global) so concurrent sessions — each a
separate asyncio task per WS connection — stay isolated: ``asyncio.create_task``
copies the context, so child tasks of a session inherit the board_ref set at
its top, while a different session's task carries its own value. The engine
treats board_ref as an opaque string; no trust or gating logic lives here.

Default ``None`` so STANDALONE / SELF-HOST (no ``?board=`` param) is fully
functional — the delta is simply not injected, preserving today's behaviour.
"""

from __future__ import annotations

from contextvars import ContextVar

_board_ref: ContextVar[str | None] = ContextVar("agent_board_ref", default=None)


def set_board_ref(value: str | None) -> None:
    """Bind the current diagnostic session to a board number (call once, at the top)."""
    _board_ref.set(value or None)


def current_board_ref() -> str | None:
    """The board number for the current session, or None (not supplied / standalone)."""
    return _board_ref.get()
