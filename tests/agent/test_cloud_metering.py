"""T13 — the engine reports per-LLM-call agent token usage back to the cloud.

The diagnostic agent's token cost is the tenant-private billing unit (the
shared pipeline pack build is the amortized moat, not metered here). At each
`span.model_request_end` the live forwarder fires a best-effort POST to the
cloud's `/internal/metering/diagnostic` endpoint. It must:

  - be a no-op when the cloud target is unconfigured (self-host / dev),
  - POST the exact snake_case contract with a Bearer service token,
  - never raise on a network/HTTP failure (fire-and-forget).
"""
from __future__ import annotations

import pytest


class _FakeResp:
    status_code = 202
    text = ""

    def json(self):  # pragma: no cover - trivial
        return {"recorded": True, "cost_cents": 7, "period": "2026-05"}


def _fake_client_factory(captured: dict, *, raise_on_post: bool = False):
    class _FakeClient:
        def __init__(self, *args, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            if raise_on_post:
                raise RuntimeError("connection refused")
            return _FakeResp()

    return _FakeClient


class _Settings:
    def __init__(self, url="", token=""):
        self.cloud_metering_url = url
        self.cloud_metering_token = token


def test_cloud_metering_enabled_requires_both_url_and_token(monkeypatch):
    from api.agent import cloud_metering as cm

    monkeypatch.setattr(cm, "get_settings", lambda: _Settings("", ""))
    assert cm.cloud_metering_enabled() is False

    monkeypatch.setattr(cm, "get_settings", lambda: _Settings("https://c", ""))
    assert cm.cloud_metering_enabled() is False

    monkeypatch.setattr(cm, "get_settings", lambda: _Settings("", "tok"))
    assert cm.cloud_metering_enabled() is False

    monkeypatch.setattr(cm, "get_settings", lambda: _Settings("https://c", "tok"))
    assert cm.cloud_metering_enabled() is True


@pytest.mark.asyncio
async def test_report_turn_usage_noop_when_unconfigured(monkeypatch):
    from api.agent import cloud_metering as cm

    monkeypatch.setattr(cm, "get_settings", lambda: _Settings("", ""))
    captured: dict = {}
    monkeypatch.setattr(cm.httpx, "AsyncClient", _fake_client_factory(captured))

    await cm.report_turn_usage(
        owner_ref="tenant-1",
        model="claude-opus-4-8",
        input_tokens=1000,
        output_tokens=200,
        engine_repair_id="rep-1",
        event_id="sesn:evt-1",
    )

    assert captured == {}  # no HTTP call attempted


@pytest.mark.asyncio
async def test_report_turn_usage_posts_contract_with_bearer(monkeypatch):
    from api.agent import cloud_metering as cm

    monkeypatch.setattr(
        cm, "get_settings", lambda: _Settings("https://cloud.example/", "tok_test")
    )
    captured: dict = {}
    monkeypatch.setattr(cm.httpx, "AsyncClient", _fake_client_factory(captured))

    await cm.report_turn_usage(
        owner_ref="tenant-1",
        model="claude-opus-4-8",
        input_tokens=1000,
        output_tokens=200,
        engine_repair_id="rep-1",
        event_id="sesn:evt-1",
    )

    # trailing slash on the configured base must not double up
    assert captured["url"] == "https://cloud.example/internal/metering/diagnostic"
    assert captured["headers"]["Authorization"] == "Bearer tok_test"
    assert captured["json"] == {
        "owner_ref": "tenant-1",
        "model": "claude-opus-4-8",
        "kind": "agent",  # default — build reports pass kind='build' explicitly
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "engine_repair_id": "rep-1",
        "event_id": "sesn:evt-1",
    }


@pytest.mark.asyncio
async def test_report_turn_usage_includes_cache_tokens(monkeypatch):
    """Cache tokens must ride the report so the cloud prices them at their own
    (much cheaper) tiers. Without them a hot turn — mostly cache_read — is
    billed as full input (~10x overcharge to the tenant)."""
    from api.agent import cloud_metering as cm

    monkeypatch.setattr(
        cm, "get_settings", lambda: _Settings("https://cloud.example", "tok_test")
    )
    captured: dict = {}
    monkeypatch.setattr(cm.httpx, "AsyncClient", _fake_client_factory(captured))

    await cm.report_turn_usage(
        owner_ref="tenant-1",
        model="claude-opus-4-8",
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=4096,
        cache_creation_input_tokens=2048,
        engine_repair_id="rep-1",
        event_id="sesn:evt-9",
    )

    assert captured["json"]["cache_read_input_tokens"] == 4096
    assert captured["json"]["cache_creation_input_tokens"] == 2048


@pytest.mark.asyncio
async def test_report_turn_usage_swallows_errors(monkeypatch):
    from api.agent import cloud_metering as cm

    monkeypatch.setattr(
        cm, "get_settings", lambda: _Settings("https://cloud.example", "tok_test")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cm.httpx, "AsyncClient", _fake_client_factory(captured, raise_on_post=True)
    )

    # Must NOT raise — fire-and-forget best-effort.
    await cm.report_turn_usage(
        owner_ref="tenant-1",
        model="claude-opus-4-8",
        input_tokens=1,
        output_tokens=1,
        engine_repair_id=None,
        event_id="sesn:evt-2",
    )


@pytest.mark.asyncio
async def test_fire_and_forget_does_not_schedule_when_disabled(monkeypatch):
    """When unconfigured, fire_and_forget must NOT even spawn a task."""
    import asyncio
    from unittest.mock import AsyncMock

    from api.agent import cloud_metering as cm

    monkeypatch.setattr(cm, "get_settings", lambda: _Settings("", ""))
    spy = AsyncMock()
    monkeypatch.setattr(cm, "report_turn_usage", spy)

    cm.fire_and_forget_report(
        owner_ref="t", model="m", input_tokens=1, output_tokens=1,
        engine_repair_id=None, event_id="e",
    )
    await asyncio.sleep(0)
    spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_fire_and_forget_schedules_report_when_enabled(monkeypatch):
    """When configured, fire_and_forget schedules report_turn_usage with the args."""
    import asyncio
    from unittest.mock import AsyncMock

    from api.agent import cloud_metering as cm

    monkeypatch.setattr(
        cm, "get_settings", lambda: _Settings("https://c", "tok")
    )
    spy = AsyncMock()
    monkeypatch.setattr(cm, "report_turn_usage", spy)

    cm.fire_and_forget_report(
        owner_ref="tenant-1", model="claude-opus-4-8",
        input_tokens=1000, output_tokens=200,
        engine_repair_id="rep-1", event_id="sesn:evt-1",
    )
    # Let the scheduled task run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    spy.assert_awaited_once_with(
        owner_ref="tenant-1", model="claude-opus-4-8",
        input_tokens=1000, output_tokens=200,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
        engine_repair_id="rep-1", event_id="sesn:evt-1", kind="agent",
    )
