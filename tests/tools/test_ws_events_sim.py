"""WS event envelopes for the simulation / observation layer."""

from __future__ import annotations

from api.tools.ws_events import (
    SimulationObservationClear,
    SimulationObservationSet,
)


def test_observation_set_envelope():
    ev = SimulationObservationSet(
        target="rail:+3V3",
        mode="dead",
        measurement={"measured": 0.02, "unit": "V", "nominal": 3.3, "note": None},
    )
    assert ev.type == "simulation.observation_set"
    payload = ev.model_dump()
    assert payload["type"] == "simulation.observation_set"
    assert payload["target"] == "rail:+3V3"
    assert payload["mode"] == "dead"


def test_observation_set_without_measurement():
    ev = SimulationObservationSet(target="comp:U7", mode="anomalous")
    payload = ev.model_dump()
    assert payload["measurement"] is None


def test_observation_clear_envelope():
    ev = SimulationObservationClear()
    assert ev.type == "simulation.observation_clear"
    assert ev.model_dump() == {"type": "simulation.observation_clear"}
