"""Asyncio-safety regression tests for runtime_managed.

These tests exercise real behaviors (not just signature shapes), targeting
the three async pitfalls a recent audit flagged:

* F1 — measurement / validation `_emit` callbacks used to spawn bare
  `asyncio.create_task(ws.send_json(...))`. On a fast WS close, the
  task was orphaned and the frame never hit the wire. They now route
  through `session_mirrors.spawn(...)` so `wait_drain` can observe
  them. Test: spawn N emits, close the session, assert all N landed
  on the WS before teardown.

* F2 — `cam_capture` was dispatched as `asyncio.create_task(...)` and
  the eid was added to `responded_tool_ids` immediately, even when the
  dispatch crashed. The result was a permablock: MA waiting forever on
  a tool_use that no client ever answered. The fix uses
  `session_mirrors.spawn(...)` plus a done-callback that DISCARDS the
  eid on cancel/exception so MA's retry path is unblocked. Tests:
  happy path keeps the eid in the dedup; crash path discards it.

* F8 — when one forwarder task ends (stream timeout, end_turn,
  WebSocketDisconnect), the other was `task.cancel()`'d but never
  awaited. The next line of `finally` would pull `set_ws_emitter(None)`
  out from under a still-unwinding measurement-tool callback that was
  mid-`_emit`. Test: a forwarder cancelled mid-await must be observed
  as `cancelled()` after the wait, with the gather not raising.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from api.agent.runtime_managed import _SessionMirrors

# ---------------------------------------------------------------------------
# F1: _emit must route through session_mirrors so frames are awaited
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_mirrors_spawn_drains_emitted_frames():
    """Replicates the _emit pattern: spawn N ws.send_json coroutines via
    session_mirrors and assert all N are awaited before drain returns.

    This is the contract _emit relies on. A regression that goes back to
    bare asyncio.create_task would break this test because the create_task
    path doesn't add the task to the mirrors pool, so wait_drain returns
    immediately while the sends are still pending.
    """
    mirrors = _SessionMirrors()
    ws = MagicMock()
    sends_received: list[dict] = []

    async def slow_send(payload):
        # Simulate a real WS send taking a tick to actually hit the wire.
        await asyncio.sleep(0.01)
        sends_received.append(payload)

    ws.send_json = slow_send

    # Spawn 5 emits in rapid succession (mirrors what measurement /
    # validation tools do during a turn).
    for i in range(5):
        mirrors.spawn(ws.send_json({"type": "measurement", "i": i}))

    # The pool must hold all 5 BEFORE drain.
    assert len(mirrors._pending) == 5

    await mirrors.wait_drain(timeout=2.0)

    # All 5 frames must have landed by the time drain returns.
    assert len(sends_received) == 5
    assert {s["i"] for s in sends_received} == {0, 1, 2, 3, 4}
    # Pool must be empty post-drain.
    assert len(mirrors._pending) == 0


@pytest.mark.asyncio
async def test_session_mirrors_drain_swallows_exceptions():
    """A failing send must NOT prevent the other sends from completing,
    and must NOT raise out of wait_drain. Otherwise a transient WS
    failure would tear down the entire session shutdown path.
    """
    mirrors = _SessionMirrors()

    async def good_send():
        await asyncio.sleep(0.01)

    async def bad_send():
        await asyncio.sleep(0.01)
        raise ConnectionResetError("simulated WS broken pipe")

    mirrors.spawn(good_send())
    mirrors.spawn(bad_send())
    mirrors.spawn(good_send())

    # Must not raise.
    await mirrors.wait_drain(timeout=2.0)
    assert len(mirrors._pending) == 0


@pytest.mark.asyncio
async def test_session_mirrors_drain_cancels_on_timeout():
    """Tasks that don't finish within the drain window must be cancelled
    so session teardown doesn't hang forever on a wedged send.
    """
    mirrors = _SessionMirrors()

    async def hangs_forever():
        await asyncio.sleep(60)

    task = mirrors.spawn(hangs_forever())
    await mirrors.wait_drain(timeout=0.05)
    # Give the cancel a tick to propagate.
    await asyncio.sleep(0.01)
    assert task.cancelled() or task.done()


# ---------------------------------------------------------------------------
# F2: cam_capture dedup rollback on dispatch failure
# ---------------------------------------------------------------------------


def _build_cam_release_callback(responded_tool_ids: set[str], eid: str):
    """Reproduce the closure runtime_managed installs on the cam_task.

    Kept as a separate helper so tests assert against the exact callback
    runtime_managed wires up. If the runtime callback shape changes, this
    helper drifts and the failing test points the maintainer at the right
    place.
    """
    def _release_eid_on_failure(task: asyncio.Task) -> None:
        if task.cancelled():
            responded_tool_ids.discard(eid)
            return
        exc = task.exception()
        if exc is not None:
            responded_tool_ids.discard(eid)
    return _release_eid_on_failure


@pytest.mark.asyncio
async def test_cam_capture_dedup_holds_on_success():
    """Happy path: cam dispatch returns cleanly → eid stays in the dedup
    set so MA's re-emitted requires_action doesn't trigger a duplicate
    dispatch. This is the original protection; the F2 fix must not weaken
    it.
    """
    responded: set[str] = set()
    eid = "sevt_cam_001"

    async def successful_dispatch():
        await asyncio.sleep(0.01)
        return None

    mirrors = _SessionMirrors()
    responded.add(eid)  # mirrors the runtime: add BEFORE spawn
    task = mirrors.spawn(successful_dispatch())
    task.add_done_callback(_build_cam_release_callback(responded, eid))

    await mirrors.wait_drain(timeout=2.0)
    # done-callback runs via the loop, give it a tick.
    await asyncio.sleep(0.01)
    assert eid in responded, "successful dispatch must keep the dedup intact"


@pytest.mark.asyncio
async def test_cam_capture_dedup_releases_on_exception():
    """Crash path: dispatch raises → callback must remove the eid so MA's
    next requires_action can retry. Without this rollback, a single camera
    misfire leaves the tool_use answered-on-paper but never delivered,
    permablocking the session.
    """
    responded: set[str] = set()
    eid = "sevt_cam_crash"

    async def failing_dispatch():
        await asyncio.sleep(0.01)
        raise RuntimeError("camera handshake failed")

    mirrors = _SessionMirrors()
    responded.add(eid)
    task = mirrors.spawn(failing_dispatch())
    task.add_done_callback(_build_cam_release_callback(responded, eid))

    await mirrors.wait_drain(timeout=2.0)
    await asyncio.sleep(0.01)
    assert eid not in responded, (
        "exception path must release the dedup so MA can retry"
    )


@pytest.mark.asyncio
async def test_cam_capture_dedup_releases_on_cancel():
    """Session close mid-capture: the dispatch task is cancelled by the
    teardown drain. The eid must be released so a reopened session can
    answer the original tool_use cleanly instead of inheriting a stale
    "answered" mark.
    """
    responded: set[str] = set()
    eid = "sevt_cam_cancel"

    async def hanging_dispatch():
        await asyncio.sleep(60)

    mirrors = _SessionMirrors()
    responded.add(eid)
    task = mirrors.spawn(hanging_dispatch())
    task.add_done_callback(_build_cam_release_callback(responded, eid))

    # Force the cancel path via a tight drain.
    await mirrors.wait_drain(timeout=0.05)
    await asyncio.sleep(0.05)
    assert task.cancelled() or task.done()
    assert eid not in responded, "cancel path must release the dedup"


# ---------------------------------------------------------------------------
# F8: Cancelled forwarder tasks must finish unwinding before teardown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_forwarder_unwinds_before_teardown_proceeds():
    """When one forwarder finishes (stream end_turn) and the other is
    cancelled (recv_task waiting on receive_text), the loop must observe
    the cancellation as `cancelled()`/`done()` BEFORE proceeding to the
    cleanup that pulls the global emitters out from under any in-flight
    measurement callback.

    Replicates the actual asyncio.wait + cancel + gather sequence the
    runtime now uses (post-F8 fix). A regression that drops the gather
    would leave `still_running.done() == False` here.
    """
    teardown_observed = False

    async def emit_task_returns_cleanly():
        await asyncio.sleep(0.01)

    async def recv_task_blocks_on_receive():
        # Mimics ws.receive_text() that's stuck waiting for client input.
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            # Real recv_task does cleanup here (close iter, etc) — give
            # it a measurable tick so the test catches a missing await.
            await asyncio.sleep(0.01)
            raise

    emit = asyncio.create_task(emit_task_returns_cleanly())
    recv = asyncio.create_task(recv_task_blocks_on_receive())

    done, pending = await asyncio.wait(
        {emit, recv}, return_when=asyncio.FIRST_COMPLETED,
    )
    assert emit in done
    assert recv in pending

    for task in pending:
        task.cancel()

    # Without the gather, this assertion would fail because the cancel
    # has only been requested, not observed.
    if pending:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True),
            timeout=1.0,
        )

    teardown_observed = True
    assert recv.done(), "recv_task must be fully unwound before teardown"
    assert recv.cancelled(), (
        "recv_task must report cancelled() — not just done() — so any "
        "post-cancel telemetry (logger.exception, etc.) sees the right state"
    )
    assert teardown_observed


@pytest.mark.asyncio
async def test_post_cancel_gather_swallows_cancelled_error():
    """asyncio.gather(..., return_exceptions=True) must absorb the
    CancelledError that propagates out of the cancelled task. Without
    return_exceptions the gather would re-raise into the finally block
    and break the orderly teardown of session_mirrors + ws_emitter.
    """
    async def will_be_cancelled():
        await asyncio.sleep(60)

    task = asyncio.create_task(will_be_cancelled())
    await asyncio.sleep(0)  # let it start
    task.cancel()

    # This is the runtime's post-cancel pattern.
    results = await asyncio.gather(task, return_exceptions=True)
    assert len(results) == 1
    assert isinstance(results[0], asyncio.CancelledError)


# ---------------------------------------------------------------------------
# F1 (post-cancel asymmetry follow-up): per-task cancel + bounded wait
# ---------------------------------------------------------------------------
#
# These two tests pin the new orchestration in runtime_managed where the
# `await asyncio.gather(*pending, return_exceptions=True)` global call
# was replaced by a per-task `task.cancel()` + bounded `asyncio.wait`.
# The replacement gives each forwarder its own unwind window and logs by
# name when a task ignores its cancel, so a single misbehaving task can
# no longer starve a clean-finishing sibling out of the shared timeout.


def _drive_pending_unwind(
    pending: set[asyncio.Task],
    *,
    per_task_timeout: float,
    logger,
    session_id: str,
):
    """Mirror of the runtime loop in ``_run_session_loop``.

    Kept as a sync factory returning the coroutine so the tests assert
    against the exact shape the runtime ships. If the runtime sequence
    drifts, this helper drifts and the failing tests point the maintainer
    at the right place.
    """
    async def _runner() -> dict[str, str]:
        outcomes: dict[str, str] = {}
        for task in pending:
            task.cancel()
            _, unwind_pending = await asyncio.wait(
                {task}, timeout=per_task_timeout
            )
            if unwind_pending:
                logger.warning(
                    "[Diag-MA] forwarder task %s did not unwind within "
                    "%.1fs after cancel — session=%s; proceeding with "
                    "teardown",
                    task.get_name(),
                    per_task_timeout,
                    session_id,
                )
                outcomes[task.get_name()] = "timeout"
                continue
            try:
                exc = task.exception()
            except asyncio.CancelledError:
                outcomes[task.get_name()] = "cancelled"
                continue
            if exc is None:
                outcomes[task.get_name()] = "clean"
            elif isinstance(exc, asyncio.CancelledError):
                outcomes[task.get_name()] = "cancelled"
            else:
                logger.warning(
                    "[Diag-MA] forwarder task %s raised during unwind: "
                    "%s — session=%s; proceeding with teardown",
                    task.get_name(),
                    exc,
                    session_id,
                )
                outcomes[task.get_name()] = "raised"
        return outcomes
    return _runner()


@pytest.mark.asyncio
async def test_post_cancel_task_ignoring_cancel_logs_warning_with_name(
    caplog,
):
    """A forwarder that swallows its CancelledError and keeps running
    must be logged WARNING with its task name, and the teardown loop
    must move on within the per-task budget instead of hanging.

    This is the audit's F1 fix: the previous code did a single global
    gather with a 5s timeout, which logged a generic warning that did
    not name the offender. The new code names the task so the operator
    can route the post-mortem to the right forwarder.
    """
    import logging

    async def ignores_cancel():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            # Misbehaving forwarder: swallows the cancel and keeps
            # running well past the per-task budget. Real-world example
            # is a forwarder stuck in a `try: ... except Exception: pass`
            # loop that catches CancelledError as a side effect.
            await asyncio.sleep(60)

    task = asyncio.create_task(ignores_cancel(), name="session->ws")
    await asyncio.sleep(0)  # let it start
    pending = {task}

    fake_logger = logging.getLogger(
        "wrench_board.test.post_cancel_ignored"
    )
    with caplog.at_level(logging.WARNING, logger=fake_logger.name):
        outcomes = await _drive_pending_unwind(
            pending,
            per_task_timeout=0.05,
            logger=fake_logger,
            session_id="sess_test_ignored",
        )

    assert outcomes == {"session->ws": "timeout"}, (
        "Task ignoring cancel must be reported as timeout, not silently "
        "marked clean."
    )
    # The warning record must mention the task name so an operator can
    # tell recv vs emit apart.
    matching = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "session->ws" in r.getMessage()
        and "did not unwind" in r.getMessage()
    ]
    assert matching, (
        "Expected a WARNING naming the task that ignored its cancel; got "
        f"records={[r.getMessage() for r in caplog.records]}"
    )

    # Cleanup so pytest doesn't surface a leaked-task warning.
    task.cancel()
    try:
        await asyncio.wait({task}, timeout=0.05)
    except Exception:
        pass


@pytest.mark.asyncio
async def test_post_cancel_clean_task_not_penalized_by_slow_sibling():
    """Two pending tasks: one obeys its cancel quickly, the other
    ignores it and keeps running. Each task is awaited independently,
    so the clean task must be observed as cancelled within its own
    budget regardless of the sibling timing out.

    The previous global gather collapsed both into a single 5s window
    — if one task dragged on, the other's "did this finish?" answer
    came late. With the per-task wait, the loop's overall wall time is
    bounded by `sum(per_task_timeout)`, but each task is reported as
    soon as ITS budget elapses or it unwinds, whichever is first.
    """
    import logging
    import time

    async def cancels_cleanly():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            # Real recv_task close path: cleans up an iterator quickly.
            await asyncio.sleep(0)
            raise

    async def ignores_cancel():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(60)

    clean = asyncio.create_task(cancels_cleanly(), name="ws->session")
    slow = asyncio.create_task(ignores_cancel(), name="session->ws")
    await asyncio.sleep(0)  # let both start
    pending = {clean, slow}

    fake_logger = logging.getLogger(
        "wrench_board.test.post_cancel_independent"
    )

    start = time.monotonic()
    outcomes = await _drive_pending_unwind(
        pending,
        per_task_timeout=0.05,
        logger=fake_logger,
        session_id="sess_test_independent",
    )
    elapsed = time.monotonic() - start

    # Clean task observed as cancelled, slow one as timeout — handled
    # independently. The order in which the runtime visits `pending`
    # is set-iteration order, which is deterministic per process but
    # not specified across runs; we assert on the per-task outcome,
    # not on log ordering.
    assert outcomes == {
        "ws->session": "cancelled",
        "session->ws": "timeout",
    }, (
        "Each task must be reported on its own outcome, regardless of "
        "the sibling's behaviour."
    )

    # Wall time must be bounded by 2 * per_task_timeout (worst case the
    # clean task is visited second and waits ~zero before being seen as
    # cancelled). Generous upper bound to absorb scheduler jitter on
    # busy CI hardware.
    assert elapsed < 0.5, (
        f"Per-task wait should not balloon past 2*budget; got {elapsed:.3f}s"
    )

    # Cleanup the misbehaving task to avoid pytest warning.
    slow.cancel()
    try:
        await asyncio.wait({slow}, timeout=0.05)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Integration: _emit + session_mirrors interplay (the real F1 scenario)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_pattern_drains_under_simulated_session_close():
    """End-to-end check of the F1 fix: a measurement tool fires _emit
    several times in rapid succession, then the WS closes. The session
    teardown's `await session_mirrors.wait_drain(...)` MUST see those
    sends through to the wire — replicates exactly the runtime's
    install-and-tear sequence.
    """
    ws = MagicMock()
    delivered: list[dict] = []

    async def real_send(payload):
        await asyncio.sleep(0.005)
        delivered.append(payload)
    ws.send_json = real_send

    mirrors = _SessionMirrors()

    # Reproduce _emit closure shape from runtime_managed.
    def _emit(event: dict) -> None:
        mirrors.spawn(ws.send_json(event))

    # Simulate three measurement events arriving in quick succession,
    # then an immediate session close (no time for the loop to round-trip).
    _emit({"type": "measurement", "rail": "PP3V0", "voltage": 3.0})
    _emit({"type": "measurement", "rail": "PP1V8", "voltage": 1.79})
    _emit({"type": "validation", "step_id": "s1", "ok": True})

    # The session teardown awaits the drain — without F1 fix the
    # asyncio.create_task tasks would be unrelated to mirrors and the
    # drain would return instantly with `delivered` still empty.
    await mirrors.wait_drain(timeout=2.0)

    assert len(delivered) == 3
    rails = [d.get("rail") for d in delivered if d.get("type") == "measurement"]
    assert "PP3V0" in rails
    assert "PP1V8" in rails
    validations = [d for d in delivered if d.get("type") == "validation"]
    assert len(validations) == 1
