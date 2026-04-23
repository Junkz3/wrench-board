# SPDX-License-Identifier: Apache-2.0
"""Passive role classifier — deterministic heuristic + optional Opus pass.

Same architecture as `net_classifier.py`:
- `classify_passives_heuristic(graph)` — rule-driven, no LLM, always available.
- `classify_passives_llm(graph, client, model)` — optional Opus enrichment (T18).
- `classify_passives(graph, client=None)` — public entry point with graceful
  fallback.

Output shape: `dict[str, tuple[ComponentKind, str | None, float]]` mapping
refdes → (kind, role, confidence). Confidence is 0.6 for heuristic hits,
0.9+ for LLM-confirmed, 0.0 when unclassifiable.

Only passive refdes (R / C / D / FB) are emitted; ICs / connectors / modules
are absent from the result (classifier is a no-op for them).
"""

from __future__ import annotations

import logging

from api.pipeline.schematic.schemas import (
    ComponentKind,
    ComponentNode,
    ElectricalGraph,
)

logger = logging.getLogger("microsolder.pipeline.schematic.passive_classifier")

# Map schema `ComponentType` → `ComponentKind` for passives we handle.
_TYPE_TO_KIND: dict[str, str] = {
    "resistor":  "passive_r",
    "capacitor": "passive_c",
    "diode":     "passive_d",
    "ferrite":   "passive_fb",
}

_GND_TOKENS = frozenset({"GND", "AGND", "DGND", "PGND", "SGND"})


def _is_ground_net(label: str | None) -> bool:
    if not label:
        return False
    up = label.upper()
    return up in _GND_TOKENS or up.startswith("GND_")


def _is_power_rail(graph: ElectricalGraph, label: str | None) -> bool:
    if not label:
        return False
    return label in graph.power_rails


def _pin_nets(component: ComponentNode) -> list[str]:
    """Return the 2 (or more) net labels attached to this passive's pins."""
    return [p.net_label for p in component.pins if p.net_label]


# ---------------------------------------------------------------------------
# Resistors
# ---------------------------------------------------------------------------

def _classify_resistor(
    graph: ElectricalGraph, comp: ComponentNode,
) -> tuple[str | None, float]:
    """Return (role, confidence) for a resistor. Role is None if
    unclassifiable; the dispatcher downstream silently drops such cases."""
    # Evidence 1 — explicit `feedback_in` typed edge pointing at us.
    for edge in graph.typed_edges:
        if edge.kind == "feedback_in" and edge.dst == comp.refdes:
            return "feedback", 0.85

    nets = _pin_nets(comp)
    if len(nets) < 2:
        return None, 0.0

    n1, n2 = nets[0], nets[1]
    rail1 = _is_power_rail(graph, n1)
    rail2 = _is_power_rail(graph, n2)
    gnd1 = _is_ground_net(n1)
    gnd2 = _is_ground_net(n2)

    # Evidence 2 — pull-up / pull-down.
    if rail1 and not rail2 and not gnd2:
        return "pull_up", 0.65
    if rail2 and not rail1 and not gnd1:
        return "pull_up", 0.65

    # Evidence 3 — pull-down (rail/signal + GND).
    if (rail1 or rail2) and (gnd1 or gnd2):
        # Ambiguous without a value — classify as pull_down with warn-level conf.
        return "pull_down", 0.5

    # Evidence 4 — series: rail on one side, the other pin feeds a consumer
    # of a (possibly different) rail.
    if rail1 or rail2:
        other = n2 if rail1 else n1
        # Any IC's power_in pin sits on `other` → this resistor is in series
        # between two rail domains (typical VIN → regulator_in path).
        for ic in graph.components.values():
            if ic.kind != "ic":
                continue
            for pin in ic.pins:
                if pin.role == "power_in" and pin.net_label == other:
                    return "series", 0.6

    # Evidence 5 — damping (two signals, no rails, no GND).
    if not rail1 and not rail2 and not gnd1 and not gnd2:
        return "damping", 0.4

    return None, 0.0


# ---------------------------------------------------------------------------
# Capacitors / Diodes / Ferrites — stubs filled in T3
# ---------------------------------------------------------------------------

def _classify_capacitor(
    graph: ElectricalGraph, comp: ComponentNode,
) -> tuple[str | None, float]:
    # Filled in T3.
    return None, 0.0


def _classify_diode(
    graph: ElectricalGraph, comp: ComponentNode,
) -> tuple[str | None, float]:
    # Filled in T3.
    return None, 0.0


def _classify_ferrite(
    graph: ElectricalGraph, comp: ComponentNode,
) -> tuple[str | None, float]:
    # Filled in T3.
    return None, 0.0


# ---------------------------------------------------------------------------
# Public dispatchers
# ---------------------------------------------------------------------------

def classify_passive_refdes(
    graph: ElectricalGraph, comp: ComponentNode,
) -> tuple[ComponentKind, str | None, float]:
    """Classify a single component. Returns ("ic", None, 0.0) if not passive."""
    kind = _TYPE_TO_KIND.get(comp.type)
    if kind is None:
        return "ic", None, 0.0
    if comp.type == "resistor":
        role, conf = _classify_resistor(graph, comp)
    elif comp.type == "capacitor":
        role, conf = _classify_capacitor(graph, comp)
    elif comp.type == "diode":
        role, conf = _classify_diode(graph, comp)
    elif comp.type == "ferrite":
        role, conf = _classify_ferrite(graph, comp)
    else:
        role, conf = None, 0.0
    return kind, role, conf


def classify_passives_heuristic(
    graph: ElectricalGraph,
) -> dict[str, tuple[str, str | None, float]]:
    """Whole-graph pass. Emits one entry per passive refdes only."""
    out: dict[str, tuple[str, str | None, float]] = {}
    for refdes, comp in graph.components.items():
        if comp.type not in _TYPE_TO_KIND:
            continue
        kind, role, conf = classify_passive_refdes(graph, comp)
        out[refdes] = (kind, role, conf)
    logger.info(
        "passive_classifier(heuristic): slug=%s classified=%d",
        graph.device_slug, len(out),
    )
    return out
