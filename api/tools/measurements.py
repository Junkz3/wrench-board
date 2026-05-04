"""Agent tools for the measurement journal.

Every write tool emits a `simulation.observation_set` WS event through a
pluggable emitter (set by the runtime at session open) so the frontend
UI mirrors the agent's measurements live.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from api.agent.measurement_memory import (
    append_measurement,
    compare_measurements,
    load_measurements,
    parse_target,
    synthesise_observations,
)

# Per-async-context emitter: each WS session sets its own via set_ws_emitter,
# and concurrent sessions no longer overwrite each other's emitter. Child
# tasks spawned inside a session inherit the session's context, so tool
# dispatch reads the right emitter without threading it through every call.
_ws_emitter: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "wrench_board_measurements_ws_emitter", default=None,
)


def set_ws_emitter(emitter: Callable[[dict[str, Any]], None] | None) -> None:
    _ws_emitter.set(emitter)


def _emit(event: dict[str, Any]) -> None:
    emitter = _ws_emitter.get()
    if emitter is not None:
        try:
            emitter(event)
        except Exception:   # noqa: BLE001 — best-effort broadcast
            pass


_RAIL_MODES: frozenset[str] = frozenset({"dead", "alive", "shorted", "stuck_on"})
_IC_MODES: frozenset[str] = frozenset({"dead", "alive", "anomalous", "hot"})
_PASSIVE_MODES: frozenset[str] = frozenset(
    {"open", "short", "alive", "stuck_on", "stuck_off"}
)
_ALL_MODES: frozenset[str] = _RAIL_MODES | _IC_MODES | _PASSIVE_MODES


def _lookup_comp_kind(memory_root: Path, device_slug: str, refdes: str) -> str | None:
    """Return the ComponentKind for `refdes` from the device's electrical
    graph, or None when the graph is missing / unreadable / the refdes
    unknown. Best-effort — any error path yields None, which means the
    caller skips kind-specific mode validation."""
    graph_path = memory_root / device_slug / "electrical_graph.json"
    if not graph_path.exists():
        return None
    try:
        from api.pipeline.schematic.schemas import ElectricalGraph
        eg = ElectricalGraph.model_validate_json(graph_path.read_text())
    except (OSError, ValueError):
        return None
    comp = eg.components.get(refdes)
    if comp is None:
        return None
    return getattr(comp, "kind", None)


def mb_record_measurement(
    *,
    device_slug: str,
    repair_id: str,
    memory_root: Path,
    target: str,
    value: float,
    unit: str,
    nominal: float | None = None,
    note: str | None = None,
    source: str = "agent",
) -> dict[str, Any]:
    """Append a MeasurementEvent and emit the WS observation_set event."""
    try:
        parse_target(target)
    except ValueError as exc:
        return {"recorded": False, "reason": "invalid_target", "detail": str(exc)}
    ev = append_measurement(
        memory_root=memory_root, device_slug=device_slug, repair_id=repair_id,
        target=target, value=value, unit=unit, nominal=nominal, note=note,
        source=source,
    )
    if ev.auto_classified_mode:
        _emit({
            "type": "simulation.observation_set",
            "target": target,
            "mode": ev.auto_classified_mode,
            "measurement": {
                "measured": value,
                "unit": unit,
                "nominal": nominal,
                "note": note,
            },
        })
    return {
        "recorded": True,
        "timestamp": ev.timestamp,
        "auto_classified_mode": ev.auto_classified_mode,
    }


def mb_list_measurements(
    *,
    device_slug: str,
    repair_id: str,
    memory_root: Path,
    target: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    events = load_measurements(
        memory_root=memory_root, device_slug=device_slug, repair_id=repair_id,
        target=target, since=since,
    )
    return {
        "found": True,
        "measurements": [e.model_dump() for e in events],
    }


def mb_compare_measurements(
    *,
    device_slug: str,
    repair_id: str,
    memory_root: Path,
    target: str,
    before_ts: str | None = None,
    after_ts: str | None = None,
) -> dict[str, Any]:
    diff = compare_measurements(
        memory_root=memory_root, device_slug=device_slug, repair_id=repair_id,
        target=target, before_ts=before_ts, after_ts=after_ts,
    )
    if diff is None:
        return {"found": False, "reason": "insufficient_measurements", "target": target}
    return {"found": True, **diff}


def mb_observations_from_measurements(
    *,
    device_slug: str,
    repair_id: str,
    memory_root: Path,
) -> dict[str, Any]:
    obs = synthesise_observations(
        memory_root=memory_root, device_slug=device_slug, repair_id=repair_id,
    )
    return obs.model_dump()


def mb_set_observation(
    *,
    device_slug: str,
    repair_id: str,
    memory_root: Path,
    target: str,
    mode: str,
) -> dict[str, Any]:
    """Force an observation mode (no measurement), emit WS event.

    Useful when the tech tells the agent « U7 est mort » without a value.
    We record a placeholder MeasurementEvent with value=None and
    the given mode pre-set so synthesise_observations picks it up.
    """
    try:
        kind, name = parse_target(target)
    except ValueError as exc:
        return {"recorded": False, "reason": "invalid_target", "detail": str(exc)}

    if mode not in _ALL_MODES:
        return {
            "recorded": False, "reason": "invalid_mode",
            "mode": mode, "valid_modes": sorted(_ALL_MODES),
        }
    if kind == "rail" and mode not in _RAIL_MODES:
        return {
            "recorded": False, "reason": "mode_not_valid_for_rail",
            "mode": mode, "valid_modes": sorted(_RAIL_MODES),
        }
    if kind == "comp":
        comp_kind = _lookup_comp_kind(memory_root, device_slug, name)
        if comp_kind == "ic" and mode not in _IC_MODES:
            return {
                "recorded": False, "reason": "mode_not_valid_for_ic",
                "refdes": name, "mode": mode, "valid_modes": sorted(_IC_MODES),
            }
        if comp_kind is not None and comp_kind != "ic" and mode not in _PASSIVE_MODES:
            return {
                "recorded": False, "reason": "mode_not_valid_for_passive",
                "refdes": name, "kind": comp_kind, "mode": mode,
                "valid_modes": sorted(_PASSIVE_MODES),
            }

    import json
    from datetime import UTC, datetime

    from api.agent.measurement_memory import MeasurementEvent, _journal_path

    ev = MeasurementEvent(
        timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        target=target,
        value=None,
        unit="V",  # arbitrary — placeholder event, value is not used
        nominal=None,
        note=f"agent-declared mode={mode}",
        source="agent",
        auto_classified_mode=mode,
    )
    path = _journal_path(memory_root, device_slug, repair_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = ev.model_dump()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        return {"recorded": False, "reason": "io_error"}
    _emit({
        "type": "simulation.observation_set",
        "target": target,
        "mode": mode,
        "measurement": None,
    })
    return {"recorded": True, "timestamp": ev.timestamp, "mode": mode}


def mb_clear_observations(
    *,
    device_slug: str,
    repair_id: str,
    memory_root: Path,
) -> dict[str, Any]:
    """Emit the WS clear event. Does NOT delete the journal — clearing the
    journal on disk would lose history; we only tell the UI to reset its
    visible state."""
    _emit({"type": "simulation.observation_clear"})
    return {"cleared": True}
