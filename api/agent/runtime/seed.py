"""Auto-seed background task for stale pack files.

Spawned at WS-open time so the device-mounted memory store reflects any
pack edits since the last seed without blocking the session start.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from anthropic import AsyncAnthropic

from api.agent import runtime_managed as _rm
from api.agent._session_mirrors import SessionMirrors as _SessionMirrors
from api.agent.runtime._aux import logger


async def maybe_auto_seed(
    *,
    client: AsyncAnthropic,
    device_slug: str,
    memory_root: Path,
    session_mirrors: _SessionMirrors | None = None,
) -> asyncio.Task | None:
    """Launch a background re-seed of pack files that drifted since last seed.

    Returns the spawned task so callers can optionally await it (e.g. in tests).
    In the normal session path the task is fire-and-forget; its failure is
    logged and the next session open will retry.
    """
    from api.agent.memory_seed import (
        seed_memory_store_from_pack,
        stale_files_for_pack,
    )

    settings = _rm.get_settings()
    if not settings.ma_memory_store_enabled:
        return None
    pack_dir = memory_root / device_slug
    if not pack_dir.exists():
        return None
    stale = stale_files_for_pack(pack_dir)
    if not stale:
        return None

    async def _run():
        try:
            await seed_memory_store_from_pack(
                client=client,
                device_slug=device_slug,
                pack_dir=pack_dir,
                only_files=stale,
            )
            logger.info(
                "[Diag-MA] auto-seeded slug=%s files=%s",
                device_slug,
                stale,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Diag-MA] auto-seed failed slug=%s files=%s: %s",
                device_slug,
                stale,
                exc,
            )

    if session_mirrors is not None:
        return session_mirrors.spawn(_run())
    return asyncio.create_task(_run())
