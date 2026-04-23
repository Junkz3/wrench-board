# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the repair-outcome shape + IO."""

from __future__ import annotations

from pathlib import Path

import pytest

from api.agent.validation import (
    RepairOutcome,
    ValidatedFix,
    load_outcome,
    write_outcome,
)


def test_validated_fix_accepts_all_modes():
    for mode in ("dead", "alive", "anomalous", "hot", "shorted", "passive_swap"):
        fix = ValidatedFix(refdes="U7", mode=mode, rationale="test")
        assert fix.mode == mode


def test_validated_fix_rejects_unknown_mode():
    with pytest.raises(ValueError):
        ValidatedFix(refdes="U7", mode="bogus", rationale="test")


def test_repair_outcome_shape():
    oc = RepairOutcome(
        validated_at="2026-04-23T19:45:12Z",
        repair_id="0f8ab295e689",
        device_slug="mnt-reform-motherboard",
        fixes=[ValidatedFix(refdes="U12", mode="dead", rationale="+3V3 absent, remplacé")],
    )
    assert oc.tech_note is None
    assert oc.agent_confidence == "high"
    assert len(oc.fixes) == 1


def test_write_and_load_outcome(tmp_path: Path):
    mr = tmp_path / "memory"
    oc = RepairOutcome(
        validated_at="2026-04-23T19:45:12Z",
        repair_id="r1",
        device_slug="demo",
        fixes=[ValidatedFix(refdes="U12", mode="dead", rationale="replaced")],
        tech_note="reflow + replace",
    )
    write_outcome(memory_root=mr, outcome=oc)
    loaded = load_outcome(memory_root=mr, device_slug="demo", repair_id="r1")
    assert loaded is not None
    assert loaded.fixes[0].refdes == "U12"
    assert loaded.tech_note == "reflow + replace"


def test_load_outcome_missing_returns_none(tmp_path: Path):
    assert load_outcome(memory_root=tmp_path, device_slug="d", repair_id="r") is None


def test_write_outcome_is_idempotent(tmp_path: Path):
    mr = tmp_path / "memory"
    oc1 = RepairOutcome(
        validated_at="2026-04-23T19:00:00Z",
        repair_id="r", device_slug="d",
        fixes=[ValidatedFix(refdes="U7", mode="dead", rationale="v1")],
    )
    oc2 = RepairOutcome(
        validated_at="2026-04-23T19:05:00Z",
        repair_id="r", device_slug="d",
        fixes=[ValidatedFix(refdes="U12", mode="dead", rationale="v2")],
    )
    write_outcome(memory_root=mr, outcome=oc1)
    write_outcome(memory_root=mr, outcome=oc2)
    loaded = load_outcome(memory_root=mr, device_slug="d", repair_id="r")
    # The second write overwrites — outcome.json is single-valued per repair.
    assert loaded is not None
    assert loaded.fixes[0].refdes == "U12"
