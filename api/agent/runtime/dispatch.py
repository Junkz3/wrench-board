"""Custom-tool dispatch shim.

Bundles the ten-positional-arg legacy signature into a ``ToolContext``
and forwards to :func:`api.agent.tool_dispatch.dispatch_tool`. Kept as a
runtime-level helper so existing callers (and the in-flight
``mb_expand_knowledge`` interceptor at ``_forward_session_to_ws``) need
no rewiring.
"""

from __future__ import annotations

from pathlib import Path

from anthropic import AsyncAnthropic

from api.agent._session_mirrors import SessionMirrors as _SessionMirrors
from api.agent.tool_dispatch import ToolContext, dispatch_tool
from api.session.state import SessionState


async def _dispatch_tool(
    name: str,
    payload: dict,
    device_slug: str,
    memory_root: Path,
    client: AsyncAnthropic,
    session: SessionState,
    session_id: str | None = None,
    repair_id: str | None = None,
    session_mirrors: _SessionMirrors | None = None,
    conv_id: str | None = None,
) -> dict:
    """Thin shim around :func:`api.agent.tool_dispatch.dispatch_tool`.

    Bundles the legacy ten-positional-arg signature into a ``ToolContext``
    and forwards to the dispatch table. Kept as a module-level symbol so
    existing callers (and the in-flight ``mb_expand_knowledge`` interceptor
    at ``_forward_session_to_ws``) need no rewiring. Behaviour is byte-for-
    byte equivalent to the pre-refactor waterfall — see ``tool_dispatch.py``
    for the per-tool handlers.
    """
    ctx = ToolContext(
        device_slug=device_slug,
        memory_root=memory_root,
        client=client,
        session=session,
        session_id=session_id,
        repair_id=repair_id,
        session_mirrors=session_mirrors,
        conv_id=conv_id,
    )
    return await dispatch_tool(name, payload, ctx)
