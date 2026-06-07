"""Unit tests for the per-repair diagnosis log store."""

from __future__ import annotations

from pathlib import Path

from api.agent.diagnosis_log import (
    DiagnosisLogEntry,
    append_diagnosis,
    load_diagnosis_log,
)


def test_diagnosis_log_entry_shape():
    entry = DiagnosisLogEntry(
        timestamp="2026-04-23T19:00:00Z",
        observations={"state_comps": {}, "state_rails": {"+3V3": "dead"}, "metrics_comps": {}, "metrics_rails": {}},
        hypotheses_top5=[{"kill_refdes": ["U12"], "kill_modes": ["dead"], "score": 1.0, "narrative": "..."}],
        pruning_stats={"single_candidates_tested": 400, "two_fault_pairs_tested": 12, "wall_ms": 251.3},
    )
    assert entry.observations["state_rails"]["+3V3"] == "dead"
    assert entry.hypotheses_top5[0]["kill_refdes"] == ["U12"]


def test_append_and_load_roundtrip(tmp_path: Path):
    mr = tmp_path / "memory"
    append_diagnosis(
        memory_root=mr, device_slug="demo", repair_id="r1",
        observations={"state_comps": {}, "state_rails": {"+3V3": "dead"}, "metrics_comps": {}, "metrics_rails": {}},
        hypotheses_top5=[{"kill_refdes": ["U12"], "kill_modes": ["dead"], "score": 1.0, "narrative": "U12 meurt"}],
        pruning_stats={"single_candidates_tested": 400, "two_fault_pairs_tested": 0, "wall_ms": 120.0},
    )
    entries = load_diagnosis_log(memory_root=mr, device_slug="demo", repair_id="r1")
    assert len(entries) == 1
    assert entries[0].hypotheses_top5[0]["kill_refdes"] == ["U12"]


def test_append_multiple_entries_preserves_order(tmp_path: Path):
    mr = tmp_path / "memory"
    for ranks in [[["U7"]], [["U12"]], [["U19"]]]:
        append_diagnosis(
            memory_root=mr, device_slug="d", repair_id="r",
            observations={"state_comps": {}, "state_rails": {}, "metrics_comps": {}, "metrics_rails": {}},
            hypotheses_top5=[{"kill_refdes": ranks[0], "kill_modes": ["dead"], "score": 1.0, "narrative": ""}],
            pruning_stats={"single_candidates_tested": 0, "two_fault_pairs_tested": 0, "wall_ms": 0.0},
        )
    entries = load_diagnosis_log(memory_root=mr, device_slug="d", repair_id="r")
    assert [e.hypotheses_top5[0]["kill_refdes"] for e in entries] == [["U7"], ["U12"], ["U19"]]


def test_load_missing_returns_empty(tmp_path: Path):
    assert load_diagnosis_log(memory_root=tmp_path, device_slug="d", repair_id="r") == []


def test_append_swallows_missing_dir_errors(tmp_path: Path, monkeypatch):
    # Force the write to a read-only location — should not raise.
    mr = tmp_path / "memory"
    mr.mkdir()
    # Simulate a permission error on parent mkdir by pre-creating a file where a dir should go.
    conflict = mr / "d"
    conflict.write_text("block")
    # Should not raise — best-effort write.
    append_diagnosis(
        memory_root=mr, device_slug="d", repair_id="r",
        observations={"state_comps": {}, "state_rails": {}, "metrics_comps": {}, "metrics_rails": {}},
        hypotheses_top5=[],
        pruning_stats={"single_candidates_tested": 0, "two_fault_pairs_tested": 0, "wall_ms": 0.0},
    )
