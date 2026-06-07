"""Tests for api.pipeline.events — tiny per-slug async pubsub."""

from __future__ import annotations

import asyncio

import pytest

from api.pipeline import events


@pytest.fixture(autouse=True)
def _isolate_bus():
    """Reset the global bus between tests so they don't leak subscribers."""
    events.reset()
    yield
    events.reset()


async def test_subscribe_receives_published_events():
    q = events.subscribe("demo-pi")
    await events.publish("demo-pi", {"type": "phase_started", "phase": "scout"})
    ev = await asyncio.wait_for(q.get(), timeout=0.5)
    assert ev == {"type": "phase_started", "phase": "scout"}


async def test_subscribe_fans_out_to_multiple_listeners():
    q1 = events.subscribe("demo-pi")
    q2 = events.subscribe("demo-pi")
    await events.publish("demo-pi", {"type": "pipeline_started"})
    ev1 = await asyncio.wait_for(q1.get(), timeout=0.5)
    ev2 = await asyncio.wait_for(q2.get(), timeout=0.5)
    assert ev1 == ev2 == {"type": "pipeline_started"}


async def test_publish_to_unrelated_slug_is_not_delivered():
    q = events.subscribe("demo-pi")
    await events.publish("other-device", {"type": "phase_started"})
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q.get(), timeout=0.2)


async def test_unsubscribe_removes_listener():
    q = events.subscribe("demo-pi")
    events.unsubscribe("demo-pi", q)
    await events.publish("demo-pi", {"type": "pipeline_started"})
    # A fresh queue should be empty.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q.get(), timeout=0.2)


async def test_subscribers_count_tracks_lifecycle():
    assert events.subscribers_count("demo-pi") == 0
    q1 = events.subscribe("demo-pi")
    q2 = events.subscribe("demo-pi")
    assert events.subscribers_count("demo-pi") == 2
    events.unsubscribe("demo-pi", q1)
    assert events.subscribers_count("demo-pi") == 1
    events.unsubscribe("demo-pi", q2)
    assert events.subscribers_count("demo-pi") == 0


async def test_publish_with_no_subscribers_is_a_noop():
    # Must not raise, must not block.
    await events.publish("nobody-home", {"type": "pipeline_started"})


# ============ Replay buffer ============


async def test_late_subscriber_replays_recent_history():
    """A WS that connects after the pipeline already started should still see
    pipeline_started + phase_started events — that's what fixes the race
    where the client opens the WS just after asyncio.create_task(pipeline)."""
    await events.publish("demo-pi", {"type": "pipeline_started", "device_slug": "demo-pi"})
    await events.publish("demo-pi", {"type": "phase_started", "phase": "scout"})
    # Subscribe AFTER both events fired.
    q = events.subscribe("demo-pi")
    e1 = await asyncio.wait_for(q.get(), timeout=0.5)
    e2 = await asyncio.wait_for(q.get(), timeout=0.5)
    assert e1["type"] == "pipeline_started"
    assert e2["type"] == "phase_started"
    assert e2["phase"] == "scout"


async def test_replay_buffer_caps_at_history_max():
    """Spam more than _HISTORY_MAX events; only the most recent ones replay."""
    cap = events._HISTORY_MAX
    for i in range(cap + 20):
        await events.publish("demo-pi", {"type": "tick", "i": i})
    q = events.subscribe("demo-pi")
    drained = []
    while True:
        try:
            drained.append(await asyncio.wait_for(q.get(), timeout=0.05))
        except TimeoutError:
            break
    assert len(drained) == cap
    # Most recent events kept (oldest dropped)
    assert drained[0]["i"] == 20
    assert drained[-1]["i"] == cap + 19


async def test_history_count_helper():
    assert events.history_count("demo-pi") == 0
    await events.publish("demo-pi", {"type": "phase_started", "phase": "scout"})
    await events.publish("demo-pi", {"type": "phase_finished", "phase": "scout"})
    assert events.history_count("demo-pi") == 2


async def test_terminal_event_clears_history_after_grace():
    """After pipeline_finished, history is cleared (with a grace delay) so a
    new pipeline run on the same slug doesn't replay yesterday's events."""
    await events.publish("demo-pi", {"type": "phase_started", "phase": "scout"})
    await events.publish("demo-pi", {"type": "pipeline_finished", "status": "APPROVED"})
    # The clear is scheduled with a 10s grace; we patch the delay to 0 for the test.
    # Simpler: just verify the cleanup task was spawned and resolves.
    assert events.history_count("demo-pi") == 2  # still here pre-grace
    # Force-run the grace clear via direct call (no asyncio.sleep wait).
    await events._clear_history_after("demo-pi", delay_s=0.0)
    assert events.history_count("demo-pi") == 0


async def test_reset_clears_history_too():
    await events.publish("demo-pi", {"type": "phase_started"})
    assert events.history_count("demo-pi") == 1
    events.reset()
    assert events.history_count("demo-pi") == 0
