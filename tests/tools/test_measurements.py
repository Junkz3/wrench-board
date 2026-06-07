"""Contract tests for the measurement-memory agent tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from api.tools.measurements import (
    mb_compare_measurements,
    mb_list_measurements,
    mb_observations_from_measurements,
    mb_record_measurement,
)


@pytest.fixture
def mr(tmp_path: Path) -> Path:
    return tmp_path / "memory"


SLUG = "demo"
REPAIR = "r1"


def test_record_measurement_returns_mode_and_timestamp(mr: Path):
    result = mb_record_measurement(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
        target="rail:+3V3", value=2.87, unit="V", nominal=3.3,
    )
    assert result["recorded"] is True
    assert result["auto_classified_mode"] == "anomalous"
    assert "timestamp" in result


def test_record_measurement_rejects_unknown_target_kind(mr: Path):
    result = mb_record_measurement(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
        target="bogus:X", value=1.0, unit="V",
    )
    assert result["recorded"] is False
    assert result["reason"] == "invalid_target"


def test_list_measurements_returns_all(mr: Path):
    for v in (2.87, 3.29):
        mb_record_measurement(
            memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
            target="rail:+3V3", value=v, unit="V", nominal=3.3,
        )
    result = mb_list_measurements(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
    )
    assert result["found"] is True
    assert len(result["measurements"]) == 2
    assert result["measurements"][0]["value"] == 2.87


def test_list_measurements_filter_target(mr: Path):
    mb_record_measurement(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
        target="rail:+3V3", value=2.87, unit="V", nominal=3.3,
    )
    mb_record_measurement(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
        target="comp:U7", value=65.0, unit="°C",
    )
    rail = mb_list_measurements(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR, target="rail:+3V3",
    )
    assert len(rail["measurements"]) == 1


def test_compare_measurements_happy(mr: Path):
    for v, note in ((2.87, "avant"), (3.29, "après")):
        mb_record_measurement(
            memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
            target="rail:+3V3", value=v, unit="V", nominal=3.3, note=note,
        )
    diff = mb_compare_measurements(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
        target="rail:+3V3",
    )
    assert diff["found"] is True
    assert round(diff["delta"], 2) == 0.42


def test_compare_measurements_insufficient(mr: Path):
    mb_record_measurement(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
        target="rail:+3V3", value=2.87, unit="V", nominal=3.3,
    )
    diff = mb_compare_measurements(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
        target="rail:+3V3",
    )
    assert diff["found"] is False
    assert diff["reason"] == "insufficient_measurements"


def test_observations_from_measurements(mr: Path):
    mb_record_measurement(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
        target="rail:+3V3", value=0.02, unit="V", nominal=3.3,
    )
    result = mb_observations_from_measurements(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
    )
    assert result["state_rails"]["+3V3"] == "dead"
    assert result["metrics_rails"]["+3V3"]["measured"] == 0.02
