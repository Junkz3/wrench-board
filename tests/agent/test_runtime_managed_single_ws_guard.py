# SPDX-License-Identifier: Apache-2.0
"""Single-WS guard tests for the diagnostic runtime.

The audit-revealed concurrency bug: `responded_tool_ids` is per-forwarder,
not per-MA-session. Two browser tabs on the same `(device_slug, repair_id,
conv_id)` would each dispatch the same `agent.custom_tool_use` and both
POST `user.custom_tool_result`; MA rejects the second with HTTP 400 and
tears down the stream. The fix rejects the second WS at handshake time.

These tests pin that contract:
* claim/release lifecycle (the module-level set stays consistent)
* second WS on the same triplet is rejected at accept() with code 1008
* anonymous WS (no repair_id, no conv_id) bypass the guard
* a session-create failure releases the claim so the next tab can open
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _clear_active_keys():
    """Reset the module-level guard between tests so order doesn't leak."""
    from api.agent import runtime_managed as rm
    rm._active_diagnostic_keys.clear()
    yield
    rm._active_diagnostic_keys.clear()


def _stale_settings(monkeypatch, rm, *, timeout: float = 600.0) -> None:
    class _Settings:
        anthropic_api_key = "sk-test"
        anthropic_max_retries = 5
        ma_stream_event_timeout_seconds = timeout
        memory_root = "/tmp"
        ma_memory_store_enabled = False
    monkeypatch.setattr(rm, "get_settings", lambda: _Settings())


def _ma_bootstrap_mocks(monkeypatch, rm) -> None:
    """Wire the absolute minimum of MA bootstrap so the guard runs.

    The runtime needs load_managed_ids + get_agent + ensure_conversation +
    AsyncAnthropic. Anything past the guard (forwarders) is stubbed to no-op
    so the test focuses on the claim/release behavior.
    """
    monkeypatch.setattr(rm, "load_managed_ids", lambda: {"environment_id": "env_x"})
    monkeypatch.setattr(
        rm, "get_agent",
        lambda ids, tier: {
            "id": "agent_x", "version": 1, "model": "claude-haiku-4-5",
        },
    )
    monkeypatch.setattr(
        rm, "ensure_conversation",
        lambda **kw: ("conv_test_001", False),
    )
    monkeypatch.setattr(rm, "list_conversations", lambda **kw: ["conv_test_001"])
    monkeypatch.setattr(
        rm, "get_conversation_tier",
        lambda **kw: kw.get("tier", "fast"),
    )

    # Forwarders no-op so the test never opens a real stream.
    async def _noop(*_a, **_kw):
        return None
    monkeypatch.setattr(rm, "_forward_ws_to_session", _noop)
    monkeypatch.setattr(rm, "_forward_session_to_ws", _noop)

    # Sessions.create returns a fake session so the runtime threads through
    # to the orchestration block + finally cleanup (which releases the key).
    fake_session = MagicMock()
    fake_session.id = "sesn_test_001"

    class FakeSessions:
        async def create(self, **_kw):
            return fake_session
        async def retrieve(self, _sid):
            raise Exception("none")
    class FakeBeta:
        sessions = FakeSessions()
    class FakeClient:
        beta = FakeBeta()
    monkeypatch.setattr(rm, "AsyncAnthropic", lambda **_kw: FakeClient())


@pytest.mark.asyncio
async def test_first_ws_claims_diagnostic_key_and_releases_on_close(
    monkeypatch, tmp_path,
):
    """Happy path: a single WS on (slug, repair, conv) claims the key,
    runs (no-op forwarders), then releases on `finally`. Pool is empty
    after the call returns."""
    from api.agent import runtime_managed as rm

    _stale_settings(monkeypatch, rm)
    _ma_bootstrap_mocks(monkeypatch, rm)

    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()

    # Pre-condition: no claim yet.
    assert (
        ("demo", "rep_001", "conv_test_001") not in rm._active_diagnostic_keys
    )

    await rm.run_diagnostic_session_managed(
        ws, "demo", tier="fast", repair_id="rep_001", conv_id="conv_test_001",
    )

    # Post-condition: key released by the function-final finally.
    assert (
        ("demo", "rep_001", "conv_test_001") not in rm._active_diagnostic_keys
    )


@pytest.mark.asyncio
async def test_second_ws_on_same_triplet_rejected_with_1008(
    monkeypatch, tmp_path,
):
    """Pre-claim the key (simulating a still-open WS), then attempt to
    open a second one. Must be rejected at handshake with close code
    1008 (Policy Violation) and an explicit `session_already_open`
    error frame so the frontend can surface a friendly message."""
    from api.agent import runtime_managed as rm

    _stale_settings(monkeypatch, rm)
    _ma_bootstrap_mocks(monkeypatch, rm)

    # Simulate a sibling WS already holding the slot.
    rm._active_diagnostic_keys.add(("demo", "rep_001", "conv_test_001"))

    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()

    await rm.run_diagnostic_session_managed(
        ws, "demo", tier="fast", repair_id="rep_001", conv_id="conv_test_001",
    )

    # The rejection path: accept → send error frame → close 1008 → return.
    ws.accept.assert_awaited_once()
    sent = [c.args[0] for c in ws.send_json.await_args_list]
    err_frames = [p for p in sent if p.get("type") == "error"]
    assert err_frames, f"expected an error frame, got {sent!r}"
    assert err_frames[0]["code"] == "session_already_open"
    ws.close.assert_awaited_once()
    close_kwargs = ws.close.await_args.kwargs
    assert close_kwargs.get("code") == 1008, (
        f"expected RFC 6455 code 1008 (Policy Violation), got "
        f"{close_kwargs.get('code')}"
    )
    # The pre-existing claim must NOT be released by the rejection path —
    # only the WS that successfully claimed releases on its own teardown.
    assert (
        ("demo", "rep_001", "conv_test_001") in rm._active_diagnostic_keys
    )


@pytest.mark.asyncio
async def test_anonymous_ws_no_repair_id_skips_guard(
    monkeypatch, tmp_path,
):
    """A WS opened without a repair_id (smoke / dev session) must NOT
    interact with the guard set — repair_id is the only identity
    anchor that lets us dedup, and an anonymous WS can't collide with
    a different anonymous WS by definition."""
    from api.agent import runtime_managed as rm

    _stale_settings(monkeypatch, rm)
    _ma_bootstrap_mocks(monkeypatch, rm)

    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()

    await rm.run_diagnostic_session_managed(
        ws, "demo", tier="fast", repair_id=None, conv_id=None,
    )

    # Pool stayed empty throughout — the anonymous path never touched it.
    assert rm._active_diagnostic_keys == set()


@pytest.mark.asyncio
async def test_session_create_failure_releases_claim(
    monkeypatch, tmp_path,
):
    """If MA `sessions.create` raises (transient quota burst, etc.),
    the early-return path must release the claim — otherwise the next
    tab attempt is permablocked by a stale entry in the guard set."""
    from api.agent import runtime_managed as rm

    _stale_settings(monkeypatch, rm)
    _ma_bootstrap_mocks(monkeypatch, rm)

    # Override sessions.create to raise so we hit the early-return path.
    class FailingSessions:
        async def create(self, **_kw):
            raise RuntimeError("simulated 429 burst")
        async def retrieve(self, _sid):
            raise Exception("none")
    class FakeBeta:
        sessions = FailingSessions()
    class FakeClient:
        beta = FakeBeta()
    monkeypatch.setattr(rm, "AsyncAnthropic", lambda **_kw: FakeClient())

    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()

    await rm.run_diagnostic_session_managed(
        ws, "demo", tier="fast", repair_id="rep_001", conv_id="conv_test_001",
    )

    # The early-return release path must clear the claim.
    assert (
        ("demo", "rep_001", "conv_test_001") not in rm._active_diagnostic_keys
    ), (
        "session.create failure must release the guard claim — otherwise "
        "the user is permablocked from reopening the conversation"
    )
    # Error frame was emitted and WS closed (existing behavior preserved).
    sent = [c.args[0] for c in ws.send_json.await_args_list]
    err = [p for p in sent if p.get("type") == "error"]
    assert err, f"expected an error frame on session create failure, got {sent!r}"


@pytest.mark.asyncio
async def test_different_conv_ids_dont_collide(
    monkeypatch, tmp_path,
):
    """Two WS on the SAME repair but DIFFERENT conv_ids must both be
    allowed — they're separate conversation threads (e.g. tier switch
    creates a new conv per CLAUDE.md). The guard keys on the full
    triplet, not just (slug, repair)."""
    from api.agent import runtime_managed as rm

    _stale_settings(monkeypatch, rm)
    _ma_bootstrap_mocks(monkeypatch, rm)

    # Sibling on a different conv_id of the same repair.
    rm._active_diagnostic_keys.add(("demo", "rep_001", "conv_other_999"))

    # First conv: ensure_conversation returns conv_test_001 (mocked above).
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()

    await rm.run_diagnostic_session_managed(
        ws, "demo", tier="fast", repair_id="rep_001", conv_id="conv_test_001",
    )

    # The new WS opened cleanly (no rejection frame).
    sent = [c.args[0] for c in ws.send_json.await_args_list]
    rejected = [p for p in sent if p.get("code") == "session_already_open"]
    assert not rejected, (
        "WS on a different conv_id must not be rejected by the sibling's "
        f"claim. Frames: {sent!r}"
    )
    # The sibling's claim is untouched.
    assert ("demo", "rep_001", "conv_other_999") in rm._active_diagnostic_keys
