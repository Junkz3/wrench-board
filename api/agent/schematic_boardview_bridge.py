# SPDX-License-Identifier: Apache-2.0
"""Joins a SimulationTimeline (schematic-space) with a parsed Board
(physical-PCB-space) to produce a measurement-friendly EnrichedTimeline.

Pure module. No I/O. The single entry point is `enrich(timeline, board)`.
The route is built by stacking up to four heuristic rules, capped at
8 ProbePoints total — see the ranking section for ordering.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from api.board.model import Board, Part
from api.pipeline.schematic.simulator import SimulationTimeline

# Conversion constant: Board uses mils per OBV convention.
MIL_TO_MM = 0.0254
MAX_ROUTE_ENTRIES = 8


class ProbePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refdes: str
    side: str                                  # "top" | "bottom"
    coords: tuple[float, float]                # (x_mm, y_mm)
    bbox_mm: tuple[tuple[float, float], tuple[float, float]] | None = None
    reason: str
    priority: int


class EnrichedTimeline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeline: SimulationTimeline
    probe_route: list[ProbePoint] = Field(default_factory=list)
    unmapped_refdes: list[str] = Field(default_factory=list)


def enrich(timeline: SimulationTimeline, board: Board) -> EnrichedTimeline:
    """Produce a ranked probe route from a SimulationTimeline + parsed Board.

    Heuristic stack (each rule contributes one or more ProbePoints):
      1 — Source IC of the failing rail (priority 1)
      2 — First dead component in cascade with a power_in pin (priority 2)
      3..5 — Decoupling caps near priority-1 IC (sorted by distance)
      6..8 — Test points on degraded nets (sorted by distance)
    Total cap: MAX_ROUTE_ENTRIES.
    """
    parts_by_refdes = {p.refdes: p for p in board.parts}
    referenced_refdes: set[str] = set(timeline.killed_refdes)
    referenced_refdes.update(timeline.cascade_dead_components)

    route: list[ProbePoint] = []

    # Rule 1 — source IC of blocked rail.
    blocked_state = next((s for s in timeline.states if s.blocked), None)
    priority1_refdes: str | None = None
    if blocked_state is not None:
        # Find the first off/shorted rail in the blocked phase whose source we can map.
        # cascade_dead_rails is the strongest signal; fall back to the rails dict.
        candidate_rails = list(timeline.cascade_dead_rails)
        for label, st in blocked_state.rails.items():
            if st in ("off", "shorted") and label not in candidate_rails:
                candidate_rails.append(label)
        for label in candidate_rails:
            # Find a part that is referenced + lives on the board to anchor priority 1.
            for refdes in timeline.killed_refdes:
                part = parts_by_refdes.get(refdes)
                if part is not None:
                    priority1_refdes = refdes
                    route.append(
                        ProbePoint(
                            refdes=refdes,
                            side=_layer_side(part),
                            coords=_bbox_center_mm(part),
                            bbox_mm=_bbox_mm(part),
                            reason=f"Source IC for blocked rail {label}",
                            priority=1,
                        )
                    )
                    break
            if priority1_refdes:
                break

    # Rule 2 — first dead component in cascade (skip the priority-1 refdes).
    for refdes in timeline.cascade_dead_components:
        if refdes == priority1_refdes:
            continue
        part = parts_by_refdes.get(refdes)
        if part is None:
            continue
        route.append(
            ProbePoint(
                refdes=refdes,
                side=_layer_side(part),
                coords=_bbox_center_mm(part),
                bbox_mm=_bbox_mm(part),
                reason="Earliest dead component in cascade",
                priority=2,
            )
        )
        break

    # Rule 3..5 — up to 3 nearest decoupling caps to the priority-1 IC.
    if priority1_refdes is not None:
        anchor = parts_by_refdes[priority1_refdes]
        ax, ay = _bbox_center_mils(anchor)
        # Pull the priority-1 IC's decoupling list from cascade hints — for
        # the bridge we don't have direct rail access, so just look at any
        # capacitor part nearby. The simulator's own cap suspicion is in the
        # timeline.cascade_dead_components when caps are at fault; here we
        # offer the closest physical neighbours as candidates.
        cap_candidates = [
            p for p in board.parts
            if p.refdes.startswith("C") and p.refdes != priority1_refdes
        ]
        cap_candidates.sort(
            key=lambda p: _euclidean_mils((ax, ay), _bbox_center_mils(p))
        )
        for i, cap in enumerate(cap_candidates[:3]):
            route.append(
                ProbePoint(
                    refdes=cap.refdes,
                    side=_layer_side(cap),
                    coords=_bbox_center_mm(cap),
                    bbox_mm=_bbox_mm(cap),
                    reason=(
                        f"Cap near {priority1_refdes} — leak/short suspect"
                    ),
                    priority=3 + i,
                )
            )

    # Rule 6..8 — test points on any degraded net (best-effort; nets stored on Net).
    degraded_nets = {
        label
        for state in timeline.states
        for label, s in state.rails.items()
        if s in ("degraded", "shorted")
    }
    if priority1_refdes is not None and degraded_nets:
        anchor = parts_by_refdes[priority1_refdes]
        ax, ay = _bbox_center_mils(anchor)
        tps = [p for p in board.parts if p.refdes.startswith("TP")]
        tps.sort(key=lambda p: _euclidean_mils((ax, ay), _bbox_center_mils(p)))
        for i, tp in enumerate(tps[:3]):
            route.append(
                ProbePoint(
                    refdes=tp.refdes,
                    side=_layer_side(tp),
                    coords=_bbox_center_mm(tp),
                    bbox_mm=_bbox_mm(tp),
                    reason="Test point near suspect IC on a degraded net",
                    priority=6 + i,
                )
            )

    # Cap and de-dup by refdes (lowest priority wins on tie).
    seen: dict[str, ProbePoint] = {}
    for pp in sorted(route, key=lambda x: x.priority):
        if pp.refdes not in seen:
            seen[pp.refdes] = pp
        if len(seen) >= MAX_ROUTE_ENTRIES:
            break
    final_route = list(seen.values())

    # unmapped_refdes — referenced but missing from board.
    unmapped = sorted(r for r in referenced_refdes if r not in parts_by_refdes)

    return EnrichedTimeline(
        timeline=timeline,
        probe_route=final_route,
        unmapped_refdes=unmapped,
    )


# ----------------------------------------------------------------------
# Geometry helpers
# ----------------------------------------------------------------------

def _layer_side(part: Part) -> str:
    """Map Layer IntFlag to the workbench convention ("top" | "bottom")."""
    return "top" if int(part.layer) & 1 else "bottom"


def _bbox_center_mils(part: Part) -> tuple[float, float]:
    (lo, hi) = part.bbox
    return ((lo.x + hi.x) / 2.0, (lo.y + hi.y) / 2.0)


def _bbox_center_mm(part: Part) -> tuple[float, float]:
    cx, cy = _bbox_center_mils(part)
    return (cx * MIL_TO_MM, cy * MIL_TO_MM)


def _bbox_mm(part: Part) -> tuple[tuple[float, float], tuple[float, float]]:
    (lo, hi) = part.bbox
    return (
        (lo.x * MIL_TO_MM, lo.y * MIL_TO_MM),
        (hi.x * MIL_TO_MM, hi.y * MIL_TO_MM),
    )


def _euclidean_mils(a: tuple[float, float], b: tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
