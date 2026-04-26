# SPDX-License-Identifier: Apache-2.0
"""Per-repair append-only journal of tech measurements.

Same JSONL pattern as `api/agent/chat_history.py` — one `{ts, event}`
record per line at `memory/{slug}/repairs/{repair_id}/measurements.jsonl`.

Public surface:
- MeasurementEvent (Pydantic shape)
- append_measurement / load_measurements / compare_measurements
- synthesise_observations (derive Observations from the latest-per-target
  state in the journal)
- auto_classify (pure function — map a value + nominal + unit to a
  ComponentMode / RailMode, or None if it can't decide)
- parse_target (parser for "kind:name" strings)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("wrench_board.agent.measurement_memory")


Source = Literal["ui", "agent"]
Unit = Literal["V", "A", "W", "°C", "Ω", "mV"]


class MeasurementEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: str
    target: str
    value: float | None = None   # None = placeholder event from mb_set_observation
    unit: Unit
    nominal: float | None = None
    note: str | None = None
    source: Source
    auto_classified_mode: str | None = None


# ---------------------------------------------------------------------------
# Target grammar
# ---------------------------------------------------------------------------

TargetKind = Literal["rail", "comp", "pin"]
_KNOWN_KINDS: frozenset[str] = frozenset({"rail", "comp", "pin"})


def parse_target(target: str) -> tuple[str, str]:
    """Split a target string into (kind, name).

    Examples:
      "rail:+3V3"  → ("rail", "+3V3")
      "comp:U7"    → ("comp", "U7")
      "pin:U7:3"   → ("pin", "U7:3")

    Raises ValueError for unknown kinds or malformed input.
    """
    if ":" not in target:
        raise ValueError(f"expected '<kind>:<name>', got {target!r}")
    kind, _, name = target.partition(":")
    if kind not in _KNOWN_KINDS:
        raise ValueError(f"unknown target kind {kind!r}; expected one of {sorted(_KNOWN_KINDS)}")
    if not name:
        raise ValueError(f"empty name in target {target!r}")
    return kind, name


# ---------------------------------------------------------------------------
# Auto-classify rules
# ---------------------------------------------------------------------------

# Central, tunable. Values are ratios of nominal unless otherwise stated.
CLASSIFY_RAIL_ALIVE_LOW = 0.90         # ≥ 90% of nominal
CLASSIFY_RAIL_ALIVE_HIGH = 1.10        # ≤ 110% of nominal
CLASSIFY_RAIL_DEAD_THRESHOLD_V = 0.05  # absolute volts, < this → dead
CLASSIFY_RAIL_ANOMALOUS_LOW = 0.50     # 50-90% of nominal → anomalous
CLASSIFY_IC_HOT_CELSIUS = 65.0         # IC temperature threshold


def auto_classify(
    *, target: str, value: float, unit: str,
    nominal: float | None = None, note: str | None = None,
) -> str | None:
    """Map a (target, value, unit, nominal?) to a mode string.

    Returns None when we can't decide (missing nominal, unsupported
    kind, etc.) — the caller keeps the measurement in storage but
    leaves the mode unset.
    """
    try:
        kind, name = parse_target(target)
    except ValueError:
        return None

    if kind == "rail" and unit in ("V", "mV"):
        if nominal is None:
            return None
        # Normalise the reading to V. `nominal` is the rail's SI target
        # (stored in V across the codebase — see tests + schematic_graph
        # inference), so it is NEVER divided by 1000 even when the reading
        # is submitted in mV.
        v = value / 1000.0 if unit == "mV" else value
        nom = nominal
        # Explicit short note dominates.
        if note and "short" in note.lower() and abs(v) < CLASSIFY_RAIL_DEAD_THRESHOLD_V:
            return "shorted"
        if v < CLASSIFY_RAIL_DEAD_THRESHOLD_V:
            return "dead"
        ratio = v / nom if nom != 0 else 0.0
        if ratio > CLASSIFY_RAIL_ALIVE_HIGH:
            return "shorted"   # overvoltage folded into shorted for Phase 1
        if ratio >= CLASSIFY_RAIL_ALIVE_LOW:
            # Phase 4.5: if voltage is nominal but the tech's note implies the rail
            # SHOULD be off (standby/veille/sleep), promote to stuck_on.
            if note:
                note_lower = note.lower()
                STANDBY_TOKENS = ("veille", "standby", "off", "power_off", "sleep",
                                   "éteint", "eteint", "capot fermé", "lid closed")
                if any(tok in note_lower for tok in STANDBY_TOKENS):
                    return "stuck_on"
            return "alive"
        if ratio >= CLASSIFY_RAIL_ANOMALOUS_LOW:
            return "anomalous"
        return "anomalous"   # any non-zero sag below 50% is still anomalous

    if kind == "comp" and unit == "°C":
        return "hot" if value >= CLASSIFY_IC_HOT_CELSIUS else "alive"

    # Unsupported combinations — we store the measurement but leave the
    # mode empty for the tech to decide manually.
    return None


# ---------------------------------------------------------------------------
# Journal helpers
# ---------------------------------------------------------------------------


def _journal_path(memory_root: Path, device_slug: str, repair_id: str) -> Path:
    return (
        memory_root / device_slug / "repairs" / repair_id / "measurements.jsonl"
    )


def append_measurement(
    *,
    memory_root: Path,
    device_slug: str,
    repair_id: str,
    target: str,
    value: float,
    unit: str,
    nominal: float | None = None,
    note: str | None = None,
    source: str = "agent",
) -> MeasurementEvent:
    """Append one MeasurementEvent to the journal, return it.

    Auto-classify is computed synchronously and cached on the event so
    replay and filtering don't need to re-run the rules.
    """
    mode = auto_classify(target=target, value=value, unit=unit, nominal=nominal, note=note)
    ev = MeasurementEvent(
        timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        target=target,
        value=value,
        unit=unit,  # type: ignore[arg-type]
        nominal=nominal,
        note=note,
        source=source,  # type: ignore[arg-type]
        auto_classified_mode=mode,
    )
    path = _journal_path(memory_root, device_slug, repair_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(ev.model_dump_json() + "\n")
    except OSError as exc:
        logger.warning("append_measurement failed for %s / %s: %s", device_slug, repair_id, exc)
    return ev


def load_measurements(
    *,
    memory_root: Path,
    device_slug: str,
    repair_id: str,
    target: str | None = None,
    since: str | None = None,
) -> list[MeasurementEvent]:
    """Return the ordered list of MeasurementEvents, optionally filtered."""
    path = _journal_path(memory_root, device_slug, repair_id)
    if not path.exists():
        return []
    events: list[MeasurementEvent] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = MeasurementEvent.model_validate_json(line)
            except ValueError:
                logger.warning("skipping malformed measurement line in %s", path)
                continue
            if target and ev.target != target:
                continue
            if since and ev.timestamp < since:
                continue
            events.append(ev)
    except OSError as exc:
        logger.warning("load_measurements failed for %s / %s: %s", device_slug, repair_id, exc)
    return events


def compare_measurements(
    *,
    memory_root: Path,
    device_slug: str,
    repair_id: str,
    target: str,
    before_ts: str | None = None,
    after_ts: str | None = None,
) -> dict[str, Any] | None:
    """Return {before, after, delta, delta_percent} for a target's journal.

    Without explicit timestamps, uses the first and last events for the
    target. Returns None if fewer than 2 events match.
    """
    events = load_measurements(
        memory_root=memory_root, device_slug=device_slug, repair_id=repair_id,
        target=target,
    )
    if len(events) < 2:
        return None
    if before_ts:
        candidates = [e for e in events if e.timestamp <= before_ts]
        before = candidates[-1] if candidates else events[0]
    else:
        before = events[0]
    if after_ts:
        candidates = [e for e in events if e.timestamp >= after_ts]
        after = candidates[0] if candidates else events[-1]
    else:
        after = events[-1]
    if before.timestamp == after.timestamp:
        return None
    # Skip placeholder events (value=None) for numeric diff.
    if before.value is None or after.value is None:
        return None
    delta = after.value - before.value
    delta_pct = None
    if before.value:
        delta_pct = round((delta / before.value) * 100, 2)
    return {
        "target": target,
        "before": {"timestamp": before.timestamp, "value": before.value, "mode": before.auto_classified_mode, "note": before.note},
        "after": {"timestamp": after.timestamp, "value": after.value, "mode": after.auto_classified_mode, "note": after.note},
        "delta": round(delta, 6),
        "delta_percent": delta_pct,
    }


def synthesise_observations(
    *,
    memory_root: Path,
    device_slug: str,
    repair_id: str,
) -> Any:
    """Walk the journal, keep the latest event per target, materialise
    an `Observations` shape suitable for hypothesize().

    Imports Observations / ObservedMetric lazily to avoid a circular
    dependency with api.pipeline.schematic.
    """
    from api.pipeline.schematic.hypothesize import Observations, ObservedMetric

    events = load_measurements(
        memory_root=memory_root, device_slug=device_slug, repair_id=repair_id,
    )
    latest: dict[str, MeasurementEvent] = {}
    for ev in events:
        latest[ev.target] = ev

    state_comps: dict[str, str] = {}
    state_rails: dict[str, str] = {}
    metrics_comps: dict[str, ObservedMetric] = {}
    metrics_rails: dict[str, ObservedMetric] = {}

    for target, ev in latest.items():
        try:
            kind, name = parse_target(target)
        except ValueError:
            continue
        if kind == "comp":
            if ev.auto_classified_mode in ("dead", "alive", "anomalous", "hot"):
                state_comps[name] = ev.auto_classified_mode
            if ev.value is not None:
                metrics_comps[name] = ObservedMetric(
                    measured=ev.value,
                    unit=ev.unit,  # type: ignore[arg-type]
                    nominal=ev.nominal,
                )
        elif kind == "rail":
            if ev.auto_classified_mode in ("dead", "alive", "shorted", "stuck_on"):
                state_rails[name] = ev.auto_classified_mode
            if ev.value is not None:
                metrics_rails[name] = ObservedMetric(
                    measured=ev.value,
                    unit=ev.unit,  # type: ignore[arg-type]
                    nominal=ev.nominal,
                )
        # pin-level: store nothing — pin measurements don't map to refdes modes.
    return Observations(
        state_comps=state_comps,
        state_rails=state_rails,
        metrics_comps=metrics_comps,
        metrics_rails=metrics_rails,
    )
