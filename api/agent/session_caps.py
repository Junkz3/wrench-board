"""Per-session plan capabilities for the diagnostic agent.

The multi-tenant cloud front-door is the only gatekeeper for money/plan logic
(the public engine holds no such logic). For capabilities that depend on the
tenant's PLAN — today only ``can_expand`` (may the agent trigger a paid
``mb_expand_knowledge`` pack enrichment?) — the cloud signals its verdict on the
diagnostic WS handshake via the ``X-Wb-Can-Expand`` header, and the engine reads
it here.

Mirrors ``owner_ref`` exactly (a ContextVar, not a module global, so concurrent
WS sessions stay isolated — ``asyncio.create_task`` copies the context). The
engine treats the value as opaque policy decided by the cloud.

Default ``True`` so STANDALONE / SELF-HOST (no cloud, header absent) keeps full
capability — a self-hoster can always enrich their own pack, today's behaviour.
The cloud always sends an explicit ``true``/``false``; a missing header therefore
means "not behind the cloud" → unrestricted.
"""

from __future__ import annotations

from contextvars import ContextVar

_can_expand: ContextVar[bool] = ContextVar("agent_can_expand", default=True)


def set_can_expand(value: bool) -> None:
    """Bind the current diagnostic session's expand capability (call once, top)."""
    _can_expand.set(bool(value))


def current_can_expand() -> bool:
    """True when the current session's plan may trigger pack enrichment."""
    return _can_expand.get()
