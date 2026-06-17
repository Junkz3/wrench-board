"""T13 expand metering — the knowledge-expansion's per-phase token spend is
reported to the cloud ledger as kind='expand' (separate from agent chat and
the build pipeline).

Invariants:
  - hard no-op when cloud metering is unconfigured (self-host never phones home),
  - one report per phase that actually called the model (empty phases skipped),
  - event_id is keyed on the expansion_id (slug-scoped, no repair backs it) so
    a re-fired expansion can't be deduped against an earlier one,
  - kind='expand' on every report so the cloud buckets it apart from the chat
    budget and the build slot system.
"""
from __future__ import annotations

from api.pipeline import expand_metering
from api.pipeline.telemetry.token_stats import PhaseTokenStats


def _stats() -> list[PhaseTokenStats]:
    scout = PhaseTokenStats(phase="scout")
    scout.record(input_tokens=1000, output_tokens=200, cache_read=50, model="claude-sonnet-4-6")
    empty = PhaseTokenStats(phase="registry")  # never called the model
    clinicien = PhaseTokenStats(phase="clinicien")
    clinicien.record(input_tokens=20, output_tokens=80, model="claude-opus-4-8")
    return [scout, empty, clinicien]


def test_noop_when_cloud_metering_unconfigured(monkeypatch):
    calls = []
    monkeypatch.setattr(expand_metering, "cloud_metering_enabled", lambda: False)
    monkeypatch.setattr(expand_metering, "fire_and_forget_report", lambda **kw: calls.append(kw))

    expand_metering.report_expand_phases(
        owner_ref="t1", device_slug="iphone-x", stats=_stats(), expansion_id="E-abc",
    )

    assert calls == []


def test_reports_each_non_empty_phase_with_kind_expand(monkeypatch):
    calls = []
    monkeypatch.setattr(expand_metering, "cloud_metering_enabled", lambda: True)
    monkeypatch.setattr(expand_metering, "fire_and_forget_report", lambda **kw: calls.append(kw))

    expand_metering.report_expand_phases(
        owner_ref="tenant-1", device_slug="iphone-x", stats=_stats(), expansion_id="E-abc",
    )

    assert len(calls) == 2  # the empty 'registry' phase is skipped
    by_phase = {c["event_id"].rsplit(":", 1)[-1]: c for c in calls}
    assert set(by_phase) == {"scout", "clinicien"}

    scout = by_phase["scout"]
    assert scout["kind"] == "expand"
    assert scout["owner_ref"] == "tenant-1"
    assert scout["engine_repair_id"] is None  # an expansion is pack-level
    assert scout["model"] == "claude-sonnet-4-6"
    assert scout["input_tokens"] == 1000
    assert scout["cache_read_input_tokens"] == 50
    assert scout["event_id"] == "iphone-x:expand:E-abc:scout"

    assert by_phase["clinicien"]["model"] == "claude-opus-4-8"


def test_event_ids_differ_across_expansions(monkeypatch):
    # Two expansions on the same device are distinct spend — distinct expansion_id
    # keeps their event_ids from colliding (and being deduped) in the ledger.
    monkeypatch.setattr(expand_metering, "cloud_metering_enabled", lambda: True)
    seen: list[str] = []
    monkeypatch.setattr(
        expand_metering, "fire_and_forget_report", lambda **kw: seen.append(kw["event_id"])
    )

    expand_metering.report_expand_phases(
        owner_ref="t", device_slug="s", stats=_stats(), expansion_id="E-111",
    )
    expand_metering.report_expand_phases(
        owner_ref="t", device_slug="s", stats=_stats(), expansion_id="E-222",
    )

    assert len(seen) == 4
    assert len(set(seen)) == 4  # all unique


def test_mints_expansion_id_when_absent(monkeypatch):
    monkeypatch.setattr(expand_metering, "cloud_metering_enabled", lambda: True)
    calls = []
    monkeypatch.setattr(expand_metering, "fire_and_forget_report", lambda **kw: calls.append(kw))

    legacy = PhaseTokenStats(phase="registry")
    legacy.record(input_tokens=7, output_tokens=3)  # no model
    expand_metering.report_expand_phases(owner_ref="t1", device_slug="s", stats=[legacy])

    assert calls[0]["model"] == "unknown"
    assert calls[0]["event_id"].startswith("s:expand:E-")
    assert calls[0]["event_id"].endswith(":registry")
