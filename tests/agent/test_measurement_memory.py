"""Unit tests for the per-repair measurement journal."""

from __future__ import annotations

from pathlib import Path

import pytest

from api.agent.measurement_memory import (
    MeasurementEvent,
    append_measurement,
    auto_classify,
    compare_measurements,
    load_measurements,
    parse_target,
    synthesise_observations,
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


def test_rail_nominal_voltage_with_standby_note_classifies_stuck_on():
    """+3V3 at 3.28V with note='en veille' → stuck_on (rail alimenté quand
    il devrait être off)."""
    assert auto_classify(
        target="rail:+3V3", value=3.28, unit="V", nominal=3.3,
        note="tech en veille, board éteint",
    ) == "stuck_on"


def test_rail_nominal_voltage_with_standby_keywords():
    """Test various standby-like keywords trigger stuck_on classification."""
    # English variants
    assert auto_classify(
        target="rail:+3V3", value=3.3, unit="V", nominal=3.3, note="standby"
    ) == "stuck_on"
    assert auto_classify(
        target="rail:+3V3", value=3.3, unit="V", nominal=3.3, note="off"
    ) == "stuck_on"
    assert auto_classify(
        target="rail:+3V3", value=3.3, unit="V", nominal=3.3, note="sleep"
    ) == "stuck_on"
    # French variants
    assert auto_classify(
        target="rail:+3V3", value=3.3, unit="V", nominal=3.3, note="veille"
    ) == "stuck_on"
    assert auto_classify(
        target="rail:+3V3", value=3.3, unit="V", nominal=3.3, note="éteint"
    ) == "stuck_on"
    assert auto_classify(
        target="rail:+3V3", value=3.3, unit="V", nominal=3.3, note="eteint"
    ) == "stuck_on"


def test_rail_nominal_voltage_without_standby_note_classifies_alive():
    """Sanity — no standby hint, nominal voltage stays alive."""
    assert auto_classify(
        target="rail:+3V3", value=3.28, unit="V", nominal=3.3, note=None
    ) == "alive"
    assert auto_classify(
        target="rail:+3V3", value=3.28, unit="V", nominal=3.3, note="after reflow"
    ) == "alive"


def test_end_to_end_journal_drives_hypothesize(tmp_path: Path):
    """
    Tech records +3V3 dead (0.02V) + +5V alive (5.0V).
    synthesise_observations must produce Observations with:
      - state_rails={'+3V3': 'dead', '+5V': 'alive'}
      - metrics_rails populated with both.
    hypothesize() on a mini_graph then returns U12 (source of +3V3) top-1.
    """
    from api.pipeline.schematic.hypothesize import hypothesize
    from api.pipeline.schematic.schemas import (
        ComponentNode,
        ElectricalGraph,
        NetNode,
        PagePin,
        PowerRail,
        SchematicQualityReport,
    )

    mr = tmp_path / "memory"
    append_measurement(
        memory_root=mr, device_slug="demo", repair_id="r",
        target="rail:+3V3", value=0.02, unit="V", nominal=3.3, source="agent",
    )
    append_measurement(
        memory_root=mr, device_slug="demo", repair_id="r",
        target="rail:+5V", value=5.0, unit="V", nominal=5.0, source="agent",
    )

    obs = synthesise_observations(
        memory_root=mr, device_slug="demo", repair_id="r",
    )
    assert obs.state_rails == {"+3V3": "dead", "+5V": "alive"}
    assert obs.metrics_rails["+3V3"].measured == 0.02

    # Minimal graph where U12 sources +3V3 and consumes +5V (like MNT).
    eg = ElectricalGraph(
        device_slug="demo",
        components={
            "U12": ComponentNode(refdes="U12", type="ic", pins=[
                PagePin(number="1", name="VIN", role="power_in", net_label="+5V"),
                PagePin(number="2", name="VOUT", role="power_out", net_label="+3V3"),
            ]),
        },
        nets={"+5V": NetNode(label="+5V", is_power=True, is_global=True),
              "+3V3": NetNode(label="+3V3", is_power=True, is_global=True)},
        power_rails={
            "+5V": PowerRail(label="+5V", source_refdes="U7", consumers=["U12"]),
            "+3V3": PowerRail(label="+3V3", source_refdes="U12"),
        },
        typed_edges=[], boot_sequence=[], designer_notes=[], ambiguities=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )
    r = hypothesize(eg, observations=obs)
    assert r.hypotheses[0].kill_refdes == ["U12"]
    assert r.hypotheses[0].kill_modes == ["dead"]
