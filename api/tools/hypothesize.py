# SPDX-License-Identifier: Apache-2.0
"""mb_hypothesize — reverse diagnostic tool (schema B)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from api.pipeline.schematic.hypothesize import (
    ObservedMetric,
    Observations,
    hypothesize,
)
from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph


def _closest_matches(candidates: list[str], needle: str, k: int = 5) -> list[str]:
    needle_u = needle.upper()
    prefix = needle_u[:1] if needle_u else ""
    substr = sorted(c for c in candidates if needle_u and needle_u in c.upper())
    pfx = sorted(c for c in candidates if prefix and c.upper().startswith(prefix))
    merged = list(dict.fromkeys(substr + pfx))
    return merged[:k]


def _coerce_metric(raw: Any) -> ObservedMetric:
    if isinstance(raw, ObservedMetric):
        return raw
    if isinstance(raw, dict):
        return ObservedMetric.model_validate(raw)
    raise ValueError(f"unsupported metric payload: {raw!r}")


def mb_hypothesize(
    *,
    device_slug: str,
    memory_root: Path,
    state_comps: dict[str, str] | None = None,
    state_rails: dict[str, str] | None = None,
    metrics_comps: dict[str, dict] | None = None,
    metrics_rails: dict[str, dict] | None = None,
    max_results: int = 5,
    repair_id: str | None = None,
) -> dict[str, Any]:
    """Rank candidate (refdes, mode) kills that explain the observations.

    Input routes:
      - explicit state/metrics dicts from the caller (frontend, agent, HTTP),
      - OR `repair_id` set and all state dicts empty → synthesise from the
        repair's measurement journal.

    Returns `HypothesizeResult.model_dump() + {"found": True}` on success,
    or `{"found": False, "reason", ...}` on any validation failure.
    """
    pack = memory_root / device_slug
    graph_path = pack / "electrical_graph.json"
    if not graph_path.exists():
        return {"found": False, "reason": "no_schematic_graph", "device_slug": device_slug}
    try:
        eg = ElectricalGraph.model_validate_json(graph_path.read_text())
    except (OSError, ValueError):
        return {"found": False, "reason": "malformed_graph", "device_slug": device_slug}

    # Journal-based auto-synthesis.
    if repair_id and not (state_comps or state_rails or metrics_comps or metrics_rails):
        from api.agent.measurement_memory import synthesise_observations
        observations = synthesise_observations(
            memory_root=memory_root, device_slug=device_slug, repair_id=repair_id,
        )
    else:
        known_comps = set(eg.components.keys())
        known_rails = set(eg.power_rails.keys())

        comps_in = state_comps or {}
        rails_in = state_rails or {}
        metrics_c_in = metrics_comps or {}
        metrics_r_in = metrics_rails or {}

        invalid_refdes = sorted(
            r for r in set(comps_in) | set(metrics_c_in) if r not in known_comps
        )
        if invalid_refdes:
            return {
                "found": False,
                "reason": "unknown_refdes",
                "invalid_refdes": invalid_refdes,
                "closest_matches": {
                    r: _closest_matches(list(known_comps), r) for r in invalid_refdes
                },
            }
        invalid_rails = sorted(
            r for r in set(rails_in) | set(metrics_r_in) if r not in known_rails
        )
        if invalid_rails:
            return {
                "found": False,
                "reason": "unknown_rail",
                "invalid_rails": invalid_rails,
                "closest_matches": {
                    r: _closest_matches(list(known_rails), r) for r in invalid_rails
                },
            }
        try:
            observations = Observations(
                state_comps=comps_in,
                state_rails=rails_in,
                metrics_comps={k: _coerce_metric(v) for k, v in metrics_c_in.items()},
                metrics_rails={k: _coerce_metric(v) for k, v in metrics_r_in.items()},
            )
        except ValueError as exc:
            return {"found": False, "reason": "invalid_observations", "detail": str(exc)}

    ab: AnalyzedBootSequence | None = None
    ab_path = pack / "boot_sequence_analyzed.json"
    if ab_path.exists():
        try:
            ab = AnalyzedBootSequence.model_validate_json(ab_path.read_text())
        except ValueError:
            ab = None

    result = hypothesize(
        eg, analyzed_boot=ab, observations=observations, max_results=max_results,
    )
    payload = result.model_dump()
    payload["found"] = True
    return payload
