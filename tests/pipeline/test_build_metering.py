"""T13 build metering — the pipeline's per-phase token spend is reported to the
cloud ledger as kind='build' (the agent's chat reports stay kind='agent').

Invariants:
  - hard no-op when cloud metering is unconfigured (self-host never phones home),
  - one report per phase that actually called the model (empty phases skipped),
  - event_id is unique per RUN (a « relancer » re-run of the same repair is new
    spend — it must not be deduped against the first build's events),
  - kind='build' on every report so the cloud excludes it from the chat budget.
"""
from __future__ import annotations

import pytest

from api.pipeline import build_metering
from api.pipeline.telemetry.token_stats import PhaseTokenStats


def _stats() -> list[PhaseTokenStats]:
    scout = PhaseTokenStats(phase="scout")
    scout.record(input_tokens=1000, output_tokens=200, cache_read=50, model="claude-opus-4-8")
    empty = PhaseTokenStats(phase="mapper")  # never called the model
    writer = PhaseTokenStats(phase="writer_cartographe")
    writer.record(input_tokens=10, output_tokens=5, model="claude-sonnet-4-6")
    return [scout, empty, writer]


def test_noop_when_cloud_metering_unconfigured(monkeypatch):
    calls = []
    monkeypatch.setattr(build_metering, "cloud_metering_enabled", lambda: False)
    monkeypatch.setattr(build_metering, "fire_and_forget_report", lambda **kw: calls.append(kw))

    build_metering.report_build_phases(owner_ref="t1", engine_repair_id="r1", stats=_stats())

    assert calls == []


def test_reports_each_non_empty_phase_with_kind_build(monkeypatch):
    calls = []
    monkeypatch.setattr(build_metering, "cloud_metering_enabled", lambda: True)
    monkeypatch.setattr(build_metering, "fire_and_forget_report", lambda **kw: calls.append(kw))

    build_metering.report_build_phases(
        owner_ref="tenant-1", engine_repair_id="rep-9", stats=_stats(), run_id="runA",
    )

    assert len(calls) == 2  # the empty 'mapper' phase is skipped
    by_phase = {c["event_id"].rsplit(":", 1)[-1]: c for c in calls}
    assert set(by_phase) == {"scout", "writer_cartographe"}

    scout = by_phase["scout"]
    assert scout["kind"] == "build"
    assert scout["owner_ref"] == "tenant-1"
    assert scout["engine_repair_id"] == "rep-9"
    assert scout["model"] == "claude-opus-4-8"
    assert scout["input_tokens"] == 1000
    assert scout["output_tokens"] == 200
    assert scout["cache_read_input_tokens"] == 50
    assert scout["event_id"] == "rep-9:build:runA:scout"

    assert by_phase["writer_cartographe"]["model"] == "claude-sonnet-4-6"


def test_event_ids_differ_across_runs_of_the_same_repair(monkeypatch):
    # The « relancer » flow re-fires the pipeline on the SAME repair_id; the
    # second build is real spend — its events must not collide with the first.
    monkeypatch.setattr(build_metering, "cloud_metering_enabled", lambda: True)
    seen: list[str] = []
    monkeypatch.setattr(
        build_metering, "fire_and_forget_report", lambda **kw: seen.append(kw["event_id"])
    )

    stats = _stats()
    build_metering.report_build_phases(owner_ref="t", engine_repair_id="r", stats=stats)
    build_metering.report_build_phases(owner_ref="t", engine_repair_id="r", stats=stats)

    assert len(seen) == 4
    assert len(set(seen)) == 4  # all unique — fresh run_id per call


def test_model_fallback_and_anonymous_pack_rebuild(monkeypatch):
    # A legacy stats entry with no model prices at the cloud's default tier; a
    # confirm-kind rebuild has no repair_id → the event key falls back to 'pack'.
    monkeypatch.setattr(build_metering, "cloud_metering_enabled", lambda: True)
    calls = []
    monkeypatch.setattr(build_metering, "fire_and_forget_report", lambda **kw: calls.append(kw))

    legacy = PhaseTokenStats(phase="registry")
    legacy.record(input_tokens=7, output_tokens=3)  # no model
    build_metering.report_build_phases(
        owner_ref="t1", engine_repair_id=None, stats=[legacy], run_id="runB",
    )

    assert calls[0]["model"] == "unknown"
    assert calls[0]["event_id"] == "pack:build:runB:registry"


@pytest.mark.asyncio
async def test_kind_rides_the_cloud_metering_payload(monkeypatch):
    # End-to-end through report_turn_usage: kind lands in the POSTed JSON.
    from api.agent import cloud_metering

    captured: dict = {}

    class _FakeResp:
        status_code = 202
        text = ""

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, headers=None, json=None):
            captured["json"] = json
            return _FakeResp()

    monkeypatch.setattr(cloud_metering.httpx, "AsyncClient", _FakeClient)
    settings = cloud_metering.get_settings()
    monkeypatch.setattr(settings, "cloud_metering_url", "http://cloud:8080")
    monkeypatch.setattr(settings, "cloud_metering_token", "svc")

    await cloud_metering.report_turn_usage(
        owner_ref="t1", model="claude-opus-4-8", input_tokens=1, output_tokens=1,
        engine_repair_id="r1", event_id="e1", kind="build",
    )
    assert captured["json"]["kind"] == "build"

    await cloud_metering.report_turn_usage(
        owner_ref="t1", model="claude-opus-4-8", input_tokens=1, output_tokens=1,
        engine_repair_id="r1", event_id="e2",
    )
    assert captured["json"]["kind"] == "agent"  # default: agent (back-compat)
