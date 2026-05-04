"""Helper for tracking fire-and-forget mirror tasks spawned from a managed
diagnostic session.

Lifted out of ``runtime_managed.py`` so the dispatch surface
(``tool_dispatch.py``) and the runtime can both reference it without forming
an import cycle. Behaviour is unchanged from the original in-line definition.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from api.config import get_settings

logger = logging.getLogger("wrench_board.agent.managed")


class SessionMirrors:
    """Tracks fire-and-forget mirror tasks and awaits them on session close.

    Used by the managed runtime to make sure ``mb_validate_finding``'s
    fire-and-forget MA-store mirror, the ``cam_capture`` round-trip, and the
    auto-seed re-upload are not orphaned by a fast WebSocket disconnect. The
    drain timeout is read from ``settings.ma_session_drain_timeout_seconds``.
    """

    def __init__(self) -> None:
        self._pending: set[asyncio.Task] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return task

    async def wait_drain(self, timeout: float | None = None) -> None:
        if not self._pending:
            return
        if timeout is None:
            timeout = get_settings().ma_session_drain_timeout_seconds
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._pending, return_exceptions=True),
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning(
                "[Diag-MA] %d mirror tasks still pending after %.1fs — cancelling",
                len(self._pending),
                timeout,
            )
            for task in list(self._pending):
                task.cancel()
