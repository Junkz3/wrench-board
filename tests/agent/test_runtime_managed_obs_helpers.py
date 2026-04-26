# SPDX-License-Identifier: Apache-2.0
"""Observability + safety helpers in runtime_managed.

Two pure helpers added in the post-audit hardening pass:

* `_safe_tool_result_text(result)` — JSON-serializes a tool result with
  graceful fallback on non-JSON-clean payloads. Replaces three call
  sites that did `json.dumps(result, default=str)`, which silently
  coerced custom objects via `__str__` (potentially producing opaque
  `<Foo at 0x…>` strings the agent then sees as garbage).

* `_build_log_id(repair_id, conv_id, tier)` — compact correlation id
  for log lines: `repair:conv:tier`. Lets a post-mortem pivot between
  `session_id` (used in every log line) and the human-grep-friendly
  triplet without rebuilding the link by hand.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_safe_tool_result_text_handles_plain_json():
    from api.agent.runtime_managed import _safe_tool_result_text

    out = _safe_tool_result_text({"ok": True, "value": 42, "items": ["a", "b"]})
    assert json.loads(out) == {"ok": True, "value": 42, "items": ["a", "b"]}


def test_safe_tool_result_text_falls_back_to_str_for_pathlib():
    """Path is the canonical Pydantic-roundtrip case: not JSON-serializable
    natively, but `str()` produces a stable, lossless representation that
    the agent can still reason about."""
    from api.agent.runtime_managed import _safe_tool_result_text

    out = _safe_tool_result_text({"ok": True, "path": Path("/tmp/x.json")})
    decoded = json.loads(out)
    assert decoded["ok"] is True
    assert decoded["path"] == "/tmp/x.json"


def test_safe_tool_result_text_returns_structured_error_on_total_failure():
    """A genuinely non-stringifiable payload (object whose __str__ raises)
    must produce a structured `serialization_failed` JSON instead of
    propagating the exception or returning the bytes 'None'.
    """
    from api.agent.runtime_managed import _safe_tool_result_text

    class Hostile:
        def __str__(self):
            raise RuntimeError("__str__ refused")
        def __repr__(self):
            raise RuntimeError("__repr__ refused too")

    out = _safe_tool_result_text({"ok": True, "weird": Hostile()})
    decoded = json.loads(out)
    assert decoded == {
        "ok": False,
        "reason": "serialization_failed",
        "error": pytest.approx(decoded["error"], abs=0),  # any non-empty str
    }
    assert decoded["error"]


def test_safe_tool_result_text_does_not_raise_on_empty_dict():
    from api.agent.runtime_managed import _safe_tool_result_text

    assert _safe_tool_result_text({}) == "{}"


def test_build_log_id_renders_full_triplet():
    from api.agent.runtime_managed import _build_log_id

    assert _build_log_id("rep_001", "conv_abc", "deep") == "rep_001:conv_abc:deep"


def test_build_log_id_sentinel_for_missing_repair():
    """Anonymous WS (no repair_id) must still produce a parseable id —
    `anon:new:fast` reads cleanly and groups all dev-mode traffic
    together when grep'ing a log file."""
    from api.agent.runtime_managed import _build_log_id

    assert _build_log_id(None, None, "fast") == "anon:new:fast"
    assert _build_log_id(None, "conv_abc", "fast") == "anon:conv_abc:fast"
    assert _build_log_id("rep_001", None, "fast") == "rep_001:new:fast"


def test_build_log_id_tier_at_end_so_partial_grep_narrows_naturally():
    """The triplet order is repair → conv → tier so that
    `grep "rep_001:" log` matches all activity on a repair across convs
    AND tiers, while `grep "rep_001:conv_abc:" log` narrows to one conv.
    """
    from api.agent.runtime_managed import _build_log_id

    log_id = _build_log_id("rep_42", "conv_xyz", "normal")
    assert log_id.startswith("rep_42:conv_xyz:")
    # Tier is the third segment, not somewhere in the middle.
    parts = log_id.split(":")
    assert parts == ["rep_42", "conv_xyz", "normal"]


def test_session_start_log_emits_log_id_anchor_line(monkeypatch, caplog):
    """End-to-end check: the runtime emits a `session_start log_id=…
    session=…` line that anchors the correlation. Without this, the
    operator can't pivot from a Sentry trace_id to the session_id and
    back to the device/repair grouping."""
    import logging
    import re
    from unittest.mock import AsyncMock, MagicMock

    from api.agent import runtime_managed as rm

    caplog.set_level(logging.INFO, logger=rm.logger.name)
    rm._active_diagnostic_keys.clear()

    class _Settings:
        anthropic_api_key = "sk-test"
        anthropic_max_retries = 5
        ma_stream_event_timeout_seconds = 600.0
        ma_session_drain_timeout_seconds = 5.0
        ma_forwarder_unwind_timeout_seconds = 2.0
        ma_subagent_consultation_timeout_seconds = 120.0
        ma_curator_timeout_seconds = 180.0
        ma_camera_capture_timeout_seconds = 30.0
        ma_memory_store_http_timeout_seconds = 30.0
        memory_root = "/tmp"
        ma_memory_store_enabled = False
    monkeypatch.setattr(rm, "get_settings", lambda: _Settings())
    monkeypatch.setattr(rm, "load_managed_ids", lambda: {"environment_id": "env_x"})
    monkeypatch.setattr(
        rm, "get_agent",
        lambda ids, tier: {"id": "agent_x", "version": 1, "model": "claude-haiku-4-5"},
    )
    monkeypatch.setattr(
        rm, "ensure_conversation",
        lambda **kw: ("conv_anchor_001", False),
    )
    monkeypatch.setattr(rm, "list_conversations", lambda **kw: ["conv_anchor_001"])
    monkeypatch.setattr(
        rm, "get_conversation_tier", lambda **kw: kw.get("tier", "fast"),
    )

    async def _noop(*_a, **_kw):
        return None
    monkeypatch.setattr(rm, "_forward_ws_to_session", _noop)
    monkeypatch.setattr(rm, "_forward_session_to_ws", _noop)

    fake_session = MagicMock()
    fake_session.id = "sesn_anchor_999"
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

    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()

    import asyncio
    asyncio.run(
        rm.run_diagnostic_session_managed(
            ws, "demo", tier="fast",
            repair_id="rep_anchor", conv_id="conv_anchor_001",
        )
    )

    rm._active_diagnostic_keys.clear()

    anchor_lines = [
        r.getMessage() for r in caplog.records
        if "session_start log_id=" in r.getMessage()
    ]
    assert anchor_lines, (
        f"expected exactly one session_start anchor log, got "
        f"{[r.getMessage() for r in caplog.records]!r}"
    )
    anchor = anchor_lines[0]
    # Must carry both the human triplet AND the MA session id so an
    # operator can pivot between them.
    assert "rep_anchor:conv_anchor_001:fast" in anchor, (
        f"anchor must contain the log_id triplet, got {anchor!r}"
    )
    assert "sesn_anchor_999" in anchor, (
        f"anchor must contain the MA session_id, got {anchor!r}"
    )
    # Sanity: the format is grepable as `log_id=X session=Y` (not nested
    # in a JSON blob or formatted across multiple lines).
    m = re.search(r"log_id=(\S+)\s+session=(\S+)", anchor)
    assert m, f"anchor must match `log_id=X session=Y` pattern, got {anchor!r}"
    assert m.group(1) == "rep_anchor:conv_anchor_001:fast"
    assert m.group(2) == "sesn_anchor_999"
