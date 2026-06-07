"""Per-session tenant/owner context for the diagnostic agent.

The multi-tenant cloud front-door supplies an opaque ``owner_ref`` (the tenant id)
on the diagnostic WS handshake via the ``X-Owner-Ref`` header. It is set once at
the top of a diagnostic session and read by owner-sensitive tools (stock) so the
agent's writes land in the RIGHT tenant's private store rather than a shared
global one. Unset (standalone / self-host) → ``None`` → the global/shared store,
preserving single-tenant behaviour.

A ContextVar (not a module global) so concurrent sessions — each a separate
asyncio task per WS connection — stay isolated: ``asyncio.create_task`` copies the
context, so child tasks of a session inherit the owner set at its top, while a
different session's task carries its own value. The engine treats owner_ref as an
opaque key; the cloud is the gatekeeper that authenticated it.
"""

from __future__ import annotations

from contextvars import ContextVar

_owner_ref: ContextVar[str | None] = ContextVar("agent_owner_ref", default=None)


def set_owner_ref(ref: str | None) -> None:
    """Bind the current diagnostic session to its tenant (call once, at the top)."""
    _owner_ref.set(ref or None)


def current_owner_ref() -> str | None:
    """The tenant owning the current session, or None (standalone / self-host)."""
    return _owner_ref.get()
