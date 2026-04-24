# SPDX-License-Identifier: Apache-2.0
"""Tiny per-slug async pubsub for pipeline progress events.

Used by the orchestrator to broadcast phase transitions, and by the
`/pipeline/progress/{slug}` WebSocket to relay them to the browser.

The bus is process-local (asyncio.Queue-backed): several WebSocket clients
can subscribe to the same device_slug (fan-out), and a publish to a slug
with no subscribers is a silent no-op — the pipeline never blocks waiting
for a listener.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

logger = logging.getLogger("microsolder.pipeline.events")

# slug -> list of subscriber queues. Plain dict of lists is enough — we don't
# expect contention here, and the queues themselves are the async primitive.
_subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)


def subscribe(slug: str) -> asyncio.Queue[dict[str, Any]]:
    """Register a new listener for this slug. Returns the queue to `.get()` from."""
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
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


async def publish(slug: str, event: dict[str, Any]) -> None:
    """Broadcast an event to every subscriber of this slug.

    No-op when nobody listens — the pipeline must not stall when the UI is
    disconnected. Delivery is best-effort per-subscriber: if a queue raises,
    we log and continue (the other subscribers still get the event).
    """
    listeners = list(_subscribers.get(slug, ()))
    if not listeners:
        return
    for q in listeners:
        try:
            await q.put(event)
        except Exception:  # pragma: no cover — asyncio.Queue.put shouldn't fail
            logger.warning("events.publish: queue.put failed for slug=%r", slug)


def subscribers_count(slug: str) -> int:
    return len(_subscribers.get(slug, ()))


def reset() -> None:
    """Clear all subscribers — test-only helper."""
    _subscribers.clear()
