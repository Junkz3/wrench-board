# SPDX-License-Identifier: Apache-2.0
"""Compiler — SchematicGraph → ElectricalGraph.

Derives the final interrogeable artefact:

- `power_rails`   from nets marked `is_power` and their `powers` / `powered_by` /
                  `enables` / `decouples` edges produced by the vision pass
- `depends_on`    edges added globally (component → component) whenever a consumer
                  is powered by a rail whose producer is known
- `boot_sequence` phases built via Kahn topological sort on those deps
- `voltage_nominal` parsed from net labels ('+3V3' → 3.3, '+5V' → 5.0, …)
- `quality`       report — counts of orphan refs, missing values, global confidence

No LLM call. Pure function of its `SchematicGraph` input (plus optional
per-page confidences for the quality report).
"""

from __future__ import annotations

import re

from api.pipeline.schematic.passive_classifier import classify_passives_heuristic
from api.pipeline.schematic.schemas import (
    Ambiguity,
    BootPhase,
    ElectricalGraph,
    PowerRail,
    SchematicGraph,
    SchematicQualityReport,
    TypedEdge,
)

# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------


def compile_electrical_graph(
    graph: SchematicGraph,
    *,
    page_confidences: dict[int, float] | None = None,
) -> ElectricalGraph:
    power_rails = _derive_power_rails(graph)
    depends_on = _derive_depends_on_edges(graph, power_rails)
    boot_sequence, cycle_refs = _compute_boot_sequence(
        graph, power_rails, depends_on
    )

    ambiguities = list(graph.ambiguities)
    if cycle_refs:
        ambiguities.append(
            Ambiguity(
                description=(
                    "Cycle in boot-power dependencies; the following components could "
                    f"not be scheduled: {', '.join(sorted(cycle_refs))}"
                ),
                page=0,
                related_refdes=sorted(cycle_refs),
            )
        )

    quality = _build_quality_report(
        graph=graph,
        ambiguities=ambiguities,
        page_confidences=page_confidences or {},
    )

    # --- Phase 4: passive role classifier ---
    # Run heuristic classifier against the pre-compiled graph + rails.
    # We build a minimal ElectricalGraph view so the classifier can use
    # `power_rails`. Then copy `kind`/`role` onto each passive and
    # populate `PowerRail.decoupling` for decoupling/bulk/filter caps.
    proxy = ElectricalGraph(
        device_slug=graph.device_slug,
        components=graph.components,
        nets=graph.nets,
        power_rails=power_rails,
        typed_edges=graph.typed_edges + depends_on,
        quality=quality,
    )
    assignments = classify_passives_heuristic(proxy)
    enriched = dict(graph.components)
    for refdes, (kind, role, _conf) in assignments.items():
        node = enriched.get(refdes)
        if node is None:
            continue
        enriched[refdes] = node.model_copy(update={"kind": kind, "role": role})
    # Populate PowerRail.decoupling from classifier output (cap-on-rail roles).
    for refdes, (kind, role, _) in assignments.items():
        if kind != "passive_c":
            continue
        if role not in {"decoupling", "bulk", "bypass"}:
            continue
        # Find the rail this cap sits on (any non-GND pin).
        comp = enriched.get(refdes)
        if comp is None:
            continue
        for pin in comp.pins:
            if pin.net_label and pin.net_label in power_rails:
                rail = power_rails[pin.net_label]
                if refdes not in rail.decoupling:
                    rail.decoupling.append(refdes)
                break

    return ElectricalGraph(
        device_slug=graph.device_slug,
        components=enriched,
        nets=graph.nets,
        power_rails=power_rails,
        typed_edges=graph.typed_edges + depends_on,
        boot_sequence=boot_sequence,
        designer_notes=graph.designer_notes,
        ambiguities=ambiguities,
        quality=quality,
        hierarchy=graph.hierarchy,
    )


# ----------------------------------------------------------------------
# Power rails
# ----------------------------------------------------------------------


_RAIL_LABEL_NOISE = {
    "PWR_FLAG",       # KiCad symbol indicating "this is a power net", not a rail
    "NC",             # No-connect
    "DNC",            # Do-not-connect
}

# Ground nets are incorrectly tagged `is_power=True` by the vision pass because
# the power-symbol heuristic doesn't distinguish VCC from GND. Ground is NOT
# a rail to sequence or visualise — it has hundreds of pin connections that
# would drown every other rail in the downstream UI.
_GROUND_LABEL = re.compile(r"^(?:GND|AGND|DGND|PGND|SGND)(?:_[A-Z0-9]+)?$")


def _is_noise_rail_label(label: str) -> bool:
    if label in _RAIL_LABEL_NOISE:
        return True
    if _GROUND_LABEL.match(label):
        return True
    # OCR glitch — text overlapping wires makes pdfplumber double every letter
    # ('GND' -> 'GGNNDD'). Heuristic: run-length compression halves the length
    # or more AND the label is ≥ 4 chars. This catches doubled/tripled letter
    # artefacts without flagging legitimate all-caps names like 'VCCIO'.
    compressed: list[str] = []
    for c in label:
        if not compressed or compressed[-1] != c:
            compressed.append(c)
    if len(label) >= 4 and len(compressed) * 2 <= len(label):
        return True
    return False


def _derive_power_rails(graph: SchematicGraph) -> dict[str, PowerRail]:
    rails: dict[str, PowerRail] = {}

    for label, net in graph.nets.items():
        if not net.is_power:
            continue
        if _is_noise_rail_label(label):
            continue
        rails[label] = PowerRail(
            label=label,
            voltage_nominal=_parse_voltage_from_label(label),
        )

    # Pre-compute producer refdes per rail so `enables` edges can link the
    # right rail even when `powers` edges were emitted with reversed direction.
    producer_by_rail: dict[str, str] = {}

    for edge in graph.typed_edges:
        if edge.kind == "powers":
            # `powers` is kept STRICT: src MUST be a real component (producer),
            # dst MUST be a rail. A reversed `rail powers component` edge is a
            # vision-pass mistake — we refuse to interpret it as a producer
            # claim because that propagates to wrong enable/consumer wiring.
            rail = rails.get(edge.dst)
            if rail is None or edge.src not in graph.components:
                continue
            if rail.source_refdes is None:
                rail.source_refdes = edge.src
                rail.source_type = _infer_source_type(graph, edge.src)
            producer_by_rail[rail.label] = edge.src
        elif edge.kind == "powered_by":
            rail, component = _classify_rail_component(edge, rails, graph)
            if rail is not None and component is not None:
                if component not in rail.consumers:
                    rail.consumers.append(component)
        elif edge.kind == "decouples":
            rail, component = _classify_rail_component(edge, rails, graph)
            if rail is not None and component is not None:
                if component not in rail.decoupling:
                    rail.decoupling.append(component)

    for edge in graph.typed_edges:
        if edge.kind != "enables":
            continue
        # `enables` convention — src is the enable signal (net), dst is the
        # component being enabled. We attach the enable net to whichever rail
        # the dst component produces.
        for label, producer in producer_by_rail.items():
            if producer == edge.dst and rails[label].enable_net is None:
                rails[label].enable_net = edge.src

    _augment_consumers_from_pins(rails, graph)
    return rails


_CONSUMER_COMPONENT_TYPES = frozenset(
    {"ic", "module", "transistor", "connector", "led", "crystal", "oscillator"}
)
_POWER_PIN_ROLES = frozenset({"power_in"})


def _augment_consumers_from_pins(
    rails: dict[str, PowerRail], graph: SchematicGraph
) -> None:
    """Populate rail.consumers from component pin data.

    Vision models emit typed_edges sparsely — a few `powered_by` edges per page,
    not one per IC pin. The pin data in `SchematicGraph.components` is richer
    and more reliable for this derivation: any component with a `power_in` pin
    on a rail label IS a consumer of that rail. Passives (caps / inductors /
    resistors / diodes) are deliberately excluded — their role is decoupling /
    filtering / biasing, not consumption from a diagnostic standpoint.
    """
    for component in graph.components.values():
        if component.type not in _CONSUMER_COMPONENT_TYPES:
            continue
        for pin in component.pins:
            if pin.role not in _POWER_PIN_ROLES or not pin.net_label:
                continue
            rail = rails.get(pin.net_label)
            if rail is None:
                continue
            if (
                component.refdes not in rail.consumers
                and component.refdes != rail.source_refdes
            ):
                rail.consumers.append(component.refdes)


def _classify_rail_component(
    edge: TypedEdge,
    rails: dict[str, PowerRail],
    graph: SchematicGraph,
) -> tuple[PowerRail | None, str | None]:
    """Given an edge, figure out which end is a rail vs a component.

    Vision models emit `powered_by` / `powers` / `decouples` edges with
    inconsistent direction conventions (e.g. Sonnet writes
    `+5V powered_by U19` while the schema doc describes the opposite). We
    accept both by looking up each end against `rails` and `graph.components`
    and picking the coherent interpretation.
    """
    src_rail = rails.get(edge.src)
    dst_rail = rails.get(edge.dst)
    src_is_component = edge.src in graph.components
    dst_is_component = edge.dst in graph.components

    if dst_rail is not None and src_is_component:
        return dst_rail, edge.src
    if src_rail is not None and dst_is_component:
        return src_rail, edge.dst
    if dst_rail is not None and not src_rail:
        return dst_rail, edge.src
    if src_rail is not None and not dst_rail:
        return src_rail, edge.dst
    return None, None


_VOLTAGE_NVN = re.compile(r"(\d+)V(\d+)")
_VOLTAGE_DOT = re.compile(r"(\d+\.\d+)V")
_VOLTAGE_INT = re.compile(r"(?<!\d)(\d+)V(?!\d)")


def _parse_voltage_from_label(label: str) -> float | None:
    s = label.upper().lstrip("+")
    if (m := _VOLTAGE_NVN.search(s)) is not None:
        return float(f"{m.group(1)}.{m.group(2)}")
    if (m := _VOLTAGE_DOT.search(s)) is not None:
        return float(m.group(1))
    if (m := _VOLTAGE_INT.search(s)) is not None:
        return float(m.group(1))
    return None


def _infer_source_type(graph: SchematicGraph, refdes: str) -> str | None:
    comp = graph.components.get(refdes)
    if comp is None or comp.value is None:
        return None
    blob = " ".join(
        s
        for s in (comp.value.primary, comp.value.description, comp.value.mpn)
        if s
    ).lower()
    if not blob:
        return None
    if any(k in blob for k in ("buck", "switching", "smps", "dc-dc", "dc/dc")):
        return "buck"
    if any(k in blob for k in ("ldo", "linear regulator")):
        return "ldo"
    if "charger" in blob or "battery" in blob:
        return "battery"
    return None


# ----------------------------------------------------------------------
# Dependency edges
# ----------------------------------------------------------------------


def _derive_depends_on_edges(
    graph: SchematicGraph, power_rails: dict[str, PowerRail]
) -> list[TypedEdge]:
    edges: list[TypedEdge] = []
    seen: set[tuple[str, str]] = set()

    def _add(src: str, dst: str) -> None:
        if src == dst:
            return
        key = (src, dst)
        if key in seen:
            return
        seen.add(key)
        edges.append(TypedEdge(src=src, dst=dst, kind="depends_on"))

    for edge in graph.typed_edges:
        if edge.kind != "powered_by":
            continue
        rail, consumer = _classify_rail_component(edge, power_rails, graph)
        if rail is None or consumer is None or rail.source_refdes is None:
            continue
        _add(consumer, rail.source_refdes)

    # Augment from pin data — every consumer on a rail depends on that rail's
    # producer. This catches ICs whose `powered_by` edge was never emitted by
    # the vision pass but whose VIN/VDD pin is correctly classified.
    for rail in power_rails.values():
        if rail.source_refdes is None:
            continue
        for consumer in rail.consumers:
            _add(consumer, rail.source_refdes)

    return edges


# ----------------------------------------------------------------------
# Boot sequence (Kahn's topological sort, levelised)
# ----------------------------------------------------------------------


def _compute_boot_sequence(
    graph: SchematicGraph,
    power_rails: dict[str, PowerRail],
    depends_on: list[TypedEdge],
) -> tuple[list[BootPhase], set[str]]:
    # Node set = every real component that either produces a rail or consumes
    # one. Strings that happen to appear as an edge endpoint but aren't in
    # `graph.components` (net labels leaking from reversed-direction edges)
    # are filtered out so phases only ever contain actual refdes.
    involved: set[str] = set()
    for rail in power_rails.values():
        if rail.source_refdes and rail.source_refdes in graph.components:
            involved.add(rail.source_refdes)
        for consumer in rail.consumers:
            if consumer in graph.components:
                involved.add(consumer)
    for e in depends_on:
        if e.src in graph.components:
            involved.add(e.src)
        if e.dst in graph.components:
            involved.add(e.dst)

    if not involved:
        return [], set()

    deps: dict[str, set[str]] = {c: set() for c in involved}
    for e in depends_on:
        if e.src in deps and e.dst in involved:
            deps[e.src].add(e.dst)

    phases: list[BootPhase] = []
    placed: set[str] = set()
    phase_index = 1

    while len(placed) < len(involved):
        ready = {
            c
            for c in involved
            if c not in placed and deps[c].issubset(placed)
        }
        if not ready:
            # Cycle — remaining nodes can't be scheduled.
            return phases, involved - placed

        rails_stable = [
            e.dst
            for e in graph.typed_edges
            if e.kind == "powers" and e.src in ready
        ]
        phases.append(
            BootPhase(
                index=phase_index,
                name=_phase_name(phase_index),
                rails_stable=sorted(set(rails_stable)),
                components_entering=sorted(ready),
            )
        )
        placed.update(ready)
        phase_index += 1

    return phases, set()


def _phase_name(index: int) -> str:
    if index == 1:
        return "PHASE 1 — cold plug / always-on"
    return f"PHASE {index}"


# ----------------------------------------------------------------------
# Quality report
# ----------------------------------------------------------------------


def _build_quality_report(
    *,
    graph: SchematicGraph,
    ambiguities: list[Ambiguity],
    page_confidences: dict[int, float],
) -> SchematicQualityReport:
    orphan_cross_page = sum(
        1
        for a in ambiguities
        if "cross-page" in a.description.lower() or a.related_nets
    )
    nets_unresolved = sum(1 for n in graph.nets.values() if not n.connects)
    comps_without_value = sum(
        1 for c in graph.components.values() if c.value is None
    )
    comps_without_mpn = sum(
        1
        for c in graph.components.values()
        if c.value is None or c.value.mpn is None
    )

    if page_confidences:
        confidence_global = sum(page_confidences.values()) / len(page_confidences)
    else:
        confidence_global = 1.0

    degraded = confidence_global < 0.7 or orphan_cross_page > 5

    return SchematicQualityReport(
        total_pages=graph.page_count,
        pages_parsed=graph.page_count,
        orphan_cross_page_refs=orphan_cross_page,
        nets_unresolved=nets_unresolved,
        components_without_value=comps_without_value,
        components_without_mpn=comps_without_mpn,
        confidence_global=confidence_global,
        degraded_mode=degraded,
    )
