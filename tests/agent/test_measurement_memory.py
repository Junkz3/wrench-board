# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the per-repair measurement journal."""

from __future__ import annotations

import pytest

from api.agent.measurement_memory import (
    MeasurementEvent,
    auto_classify,
    parse_target,
)


def test_measurement_event_shape():
    ev = MeasurementEvent(
        timestamp="2026-04-23T18:45:12Z",
        target="rail:+3V3",
        value=2.87,
        unit="V",
        nominal=3.3,
        source="ui",
    )
    assert ev.target == "rail:+3V3"
    assert ev.auto_classified_mode is None  # defaults to None


def test_parse_target_rail():
    assert parse_target("rail:+3V3") == ("rail", "+3V3")
    assert parse_target("rail:LPC_VCC") == ("rail", "LPC_VCC")


def test_parse_target_comp():
    assert parse_target("comp:U7") == ("comp", "U7")


def test_parse_target_pin():
    assert parse_target("pin:U7:3") == ("pin", "U7:3")
    assert parse_target("pin:U18:A7") == ("pin", "U18:A7")


def test_parse_target_invalid_kind():
    with pytest.raises(ValueError, match="unknown target kind"):
        parse_target("foo:bar")


def test_parse_target_missing_colon():
    with pytest.raises(ValueError, match="expected '<kind>:<name>'"):
        parse_target("U7")


def test_auto_classify_rail_alive():
    assert auto_classify(target="rail:+3V3", value=3.29, unit="V", nominal=3.3) == "alive"
    assert auto_classify(target="rail:+3V3", value=3.0, unit="V", nominal=3.3) == "alive"  # 90.9%


def test_auto_classify_rail_anomalous_sag():
    assert auto_classify(target="rail:+3V3", value=2.8, unit="V", nominal=3.3) == "anomalous"
    assert auto_classify(target="rail:+3V3", value=1.65, unit="V", nominal=3.3) == "anomalous"  # 50%


def test_auto_classify_rail_dead():
    assert auto_classify(target="rail:+3V3", value=0.02, unit="V", nominal=3.3) == "dead"


def test_auto_classify_rail_overvoltage_as_shorted():
    assert auto_classify(target="rail:+3V3", value=4.0, unit="V", nominal=3.3) == "shorted"


def test_auto_classify_rail_explicit_short_note():
    # near-zero voltage + explicit note='short' promotes dead → shorted.
    assert auto_classify(
        target="rail:+3V3", value=0.0, unit="V", nominal=3.3, note="short"
    ) == "shorted"


def test_auto_classify_ic_hot():
    assert auto_classify(target="comp:Q17", value=72.3, unit="°C") == "hot"
    assert auto_classify(target="comp:Q17", value=55.0, unit="°C") == "alive"


def test_auto_classify_rail_missing_nominal_returns_none():
    # Can't classify without knowing the expected value.
    assert auto_classify(target="rail:+3V3", value=2.8, unit="V", nominal=None) is None


def test_auto_classify_unknown_target_kind_returns_none():
    # Pin-level measurements don't auto-classify to component modes.
    assert auto_classify(target="pin:U7:3", value=0.8, unit="V", nominal=3.3) is None


from pathlib import Path

from api.agent.measurement_memory import (
    append_measurement,
    compare_measurements,
    load_measurements,
    synthesise_observations,
)


def test_append_and_load_roundtrip(tmp_path: Path):
    mr = tmp_path / "memory"
    append_measurement(
        memory_root=mr, device_slug="demo", repair_id="r1",
        target="rail:+3V3", value=2.87, unit="V", nominal=3.3, source="ui",
    )
    events = load_measurements(
        memory_root=mr, device_slug="demo", repair_id="r1",
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.target == "rail:+3V3"
    assert ev.value == 2.87
    assert ev.auto_classified_mode == "anomalous"
    assert ev.timestamp.endswith("Z") or "+" in ev.timestamp


def test_append_auto_classify_writes_mode(tmp_path: Path):
    mr = tmp_path / "memory"
    append_measurement(
        memory_root=mr, device_slug="d", repair_id="r",
        target="rail:+5V", value=0.01, unit="V", nominal=5.0, source="agent",
    )
    events = load_measurements(memory_root=mr, device_slug="d", repair_id="r")
    assert events[0].auto_classified_mode == "dead"


def test_load_measurements_filter_target(tmp_path: Path):
    mr = tmp_path / "memory"
    for target, value in (("rail:+3V3", 2.87), ("rail:+5V", 5.01), ("rail:+3V3", 3.29)):
        append_measurement(
            memory_root=mr, device_slug="d", repair_id="r",
            target=target, value=value, unit="V", nominal=3.3 if "3V3" in target else 5.0,
            source="ui",
        )
    rail3 = load_measurements(memory_root=mr, device_slug="d", repair_id="r", target="rail:+3V3")
    assert [e.value for e in rail3] == [2.87, 3.29]
    all_ = load_measurements(memory_root=mr, device_slug="d", repair_id="r")
    assert len(all_) == 3


def test_compare_measurements(tmp_path: Path):
    mr = tmp_path / "memory"
    append_measurement(
        memory_root=mr, device_slug="d", repair_id="r",
        target="rail:+3V3", value=2.87, unit="V", nominal=3.3, source="ui",
        note="avant reflow",
    )
    append_measurement(
        memory_root=mr, device_slug="d", repair_id="r",
        target="rail:+3V3", value=3.29, unit="V", nominal=3.3, source="ui",
        note="après reflow",
    )
    diff = compare_measurements(
        memory_root=mr, device_slug="d", repair_id="r",
        target="rail:+3V3",
    )
    assert diff["before"]["value"] == 2.87
    assert diff["after"]["value"] == 3.29
    assert round(diff["delta"], 2) == 0.42
    assert diff["delta_percent"] is not None


def test_synthesise_observations_dedup_latest(tmp_path: Path):
    mr = tmp_path / "memory"
    # Same target measured twice — latest wins.
    append_measurement(
        memory_root=mr, device_slug="d", repair_id="r",
        target="rail:+3V3", value=2.87, unit="V", nominal=3.3, source="ui",
    )
    append_measurement(
        memory_root=mr, device_slug="d", repair_id="r",
        target="rail:+3V3", value=3.29, unit="V", nominal=3.3, source="ui",
    )
    append_measurement(
        memory_root=mr, device_slug="d", repair_id="r",
        target="comp:Q17", value=72.3, unit="°C", source="agent",
    )
    obs = synthesise_observations(
        memory_root=mr, device_slug="d", repair_id="r",
    )
    # Latest rail mode = alive (3.29V ≈ 3.3V).
    assert obs.state_rails.get("+3V3") == "alive"
    assert obs.state_comps.get("Q17") == "hot"
    assert obs.metrics_rails["+3V3"].measured == 3.29


def test_load_measurements_missing_returns_empty(tmp_path: Path):
    assert load_measurements(memory_root=tmp_path, device_slug="d", repair_id="r") == []


def test_compare_measurements_insufficient_returns_none(tmp_path: Path):
    mr = tmp_path / "memory"
    append_measurement(
        memory_root=mr, device_slug="d", repair_id="r",
        target="rail:+3V3", value=2.87, unit="V", nominal=3.3, source="ui",
    )
    diff = compare_measurements(
        memory_root=mr, device_slug="d", repair_id="r",
        target="rail:+3V3",
    )
    assert diff is None  # only one measurement — no before/after
