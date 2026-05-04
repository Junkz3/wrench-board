"""Tiny per-slug async pubsub for pipeline progress events.

Used by the orchestrator to broadcast phase transitions, and by the
`/pipeline/progress/{slug}` WebSocket to relay them to the browser.

The bus is process-local (asyncio.Queue-backed): several WebSocket clients
can subscribe to the same device_slug (fan-out), and a publish to a slug
with no subscribers is no longer silent — we keep a small ring buffer of
the most recent events per slug so a late subscriber gets a replay. This
fixes the race where the client opens the WS *just after* the orchestrator
has already emitted `pipeline_started` and `phase_started: scout`, leaving
the UI stuck on the initial status text until the first phase finishes.

The replay buffer is also why a page-reload mid-pipeline now picks up the
already-completed phases instead of staring at a blank timeline until the
next phase boundary.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from typing import Any

logger = logging.getLogger("wrench_board.pipeline.events")

# slug -> list of subscriber queues. Plain dict of lists is enough — we don't
# expect contention here, and the queues themselves are the async primitive.
_subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)

# slug -> ring buffer of recent events. Capped per slug so the bus can never
# leak unbounded memory even if a pipeline runs forever or no one ever
# subscribes. 64 events is enough to cover a full pipeline + narrations.
_HISTORY_MAX = 64
_history: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=_HISTORY_MAX))


def subscribe(slug: str) -> asyncio.Queue[dict[str, Any]]:
    """Register a new listener for this slug.

    Returns a queue pre-populated with the slug's recent event history (so a
    late subscriber catches up to the current pipeline state). The caller
    `.get()`s from the queue normally.
    """
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    # Replay history first — order preserved.
    for event in _history.get(slug, ()):
        queue.put_nowait(event)
    _subscribers[slug].append(queue)
    return queue


def unsubscribe(slug: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
    """Drop a listener. Safe to call twice — missing queues are ignored."""
    try:
        _subscribers[slug].remove(queue)
    except ValueError:
        pass
    # Drop the slug entry entirely when empty to avoid leaking keys.
    if not _subscribers[slug]:
        _subscribers.pop(slug, None)


_TERMINAL_TYPES = frozenset({"pipeline_finished", "pipeline_failed"})


async def publish(slug: str, event: dict[str, Any]) -> None:
    """Broadcast an event to every subscriber of this slug AND store it in history.

    The history is what lets a late subscriber catch up — see `subscribe`. We
    also clear history on terminal events so a brand-new pipeline run on the
    same slug doesn't replay yesterday's ghosts.
    """
    _history[slug].append(event)

    listeners = list(_subscribers.get(slug, ()))
    for q in listeners:
        try:
            await q.put(event)
        except Exception:  # pragma: no cover — asyncio.Queue.put shouldn't fail
            logger.warning("events.publish: queue.put failed for slug=%r", slug)

    # Terminal events: keep them in history briefly (so a subscriber that
    # connects right after `pipeline_finished` still sees the verdict), but
    # schedule a cleanup so the next pipeline run starts with a clean history.
    if event.get("type") in _TERMINAL_TYPES:
        asyncio.create_task(_clear_history_after(slug, delay_s=10.0))


async def _clear_history_after(slug: str, *, delay_s: float) -> None:
    """Drop a slug's history after a grace period — runs as a fire-and-forget task."""
    try:
        await asyncio.sleep(delay_s)
    except asyncio.CancelledError:  # pragma: no cover
        return
    _history.pop(slug, None)


def subscribers_count(slug: str) -> int:
    return len(_subscribers.get(slug, ()))


def history_count(slug: str) -> int:
    """Test/debug helper — number of events buffered for this slug."""
    return len(_history.get(slug, ()))


def reset() -> None:
    """Clear all subscribers and history — test-only helper."""
    _subscribers.clear()
    _history.clear()
