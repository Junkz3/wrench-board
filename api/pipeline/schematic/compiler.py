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
#
# Token list covers the universal CMOS / ARM / Apple SoC ground conventions:
#   - GND family: GND, AGND (analog), DGND (digital), PGND (power),
#     SGND (signal), GNDA / GNDD (suffix-after-prefix variants used by
#     TI / ON Semi, present on Apple SoC pin-list pages).
#   - VSS family: VSS (universal CMOS substrate ground used by Apple,
#     Arm, Intel), AVSS / DVSS (analog/digital substrate), VSSA / VSSD
#     (alt spellings — same physical net, different style guide).
# All accept an optional `_<SUFFIX>` qualifier to catch domain-tagged
# ground nets (e.g. `AGND_RF`, `VSSA_PLL`). Standalone `_PMU_VSS_RTC`
# style names with VSS in the MIDDLE are NOT matched here on purpose —
# that token list is anchored at the start so we only catch labels that
# *begin* with a ground keyword, never substrings buried in a rail name.
_GROUND_LABEL = re.compile(
    r"^(?:GND|AGND|DGND|PGND|SGND|GNDA|GNDD|VSS|AVSS|DVSS|VSSA|VSSD)(?:_[A-Z0-9]+)?$"
)


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
    _augment_sources_from_producer_pins(rails, graph)
    _propagate_sources_through_passive_bridges(rails, graph)
    _promote_ic_owning_switch_node_over_inductor(rails, graph)
    _propagate_sources_through_rail_aliases(rails, graph)

    # Final scrub: a regulator never consumes its own output. The vision pass
    # occasionally emits a `powered_by(regulator, rail)` edge alongside the
    # `powers(regulator, rail)` edge for the same regulator (or a `powered_by`
    # edge whose direction we interpret as making the producer also a
    # consumer). The pin-augmentation path already enforces
    # `component != rail.source_refdes`; this enforces the same invariant for
    # the edge-driven population path. Producer-pin and passive-bridge
    # augmentations may also have raised `source_refdes` to a refdes that was
    # earlier added to `consumers` by an unrelated rule (e.g. a buck IC's
    # feedback pin mis-classified as `power_in`); the same scrub applies.
    for rail in rails.values():
        if rail.source_refdes is not None and rail.source_refdes in rail.consumers:
            rail.consumers.remove(rail.source_refdes)
    return rails


_CONSUMER_COMPONENT_TYPES = frozenset(
    {"ic", "module", "transistor", "connector", "led", "crystal", "oscillator"}
)
_PRODUCER_COMPONENT_TYPES = frozenset(
    {"ic", "module", "transistor", "connector"}
)
_POWER_PIN_ROLES = frozenset({"power_in"})
_PRODUCER_PIN_ROLES = frozenset({"power_out", "switch_node"})


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


def _augment_sources_from_producer_pins(
    rails: dict[str, PowerRail], graph: SchematicGraph
) -> None:
    """Mirror of `_augment_consumers_from_pins` for the producer side.

    Vision models emit `powers` edges sparsely. The pin data is more reliable:
    any IC / module / transistor / connector with a `power_out` (or, for
    switching regulators, `switch_node`) pin on a rail label is the producer
    of that rail. Passives (R, L, FL, C, D) are excluded — they don't
    generate power. Only fills `source_refdes` when it is currently None
    (additive, never overrides an existing producer) and only when exactly
    one candidate exists, to avoid mis-attributing a multi-output PMIC pin
    that was vision-misclassified.
    """
    candidates: dict[str, set[str]] = {}
    for component in graph.components.values():
        if component.type not in _PRODUCER_COMPONENT_TYPES:
            continue
        for pin in component.pins:
            if pin.role not in _PRODUCER_PIN_ROLES or not pin.net_label:
                continue
            rail = rails.get(pin.net_label)
            if rail is None or rail.source_refdes is not None:
                continue
            candidates.setdefault(pin.net_label, set()).add(component.refdes)

    for label, refs in candidates.items():
        if len(refs) != 1:
            # Ambiguous — multiple ICs claim producer pins on this rail.
            # Leave unsourced rather than guess; the diagnostic agent prefers
            # an honest null over a wrong producer.
            continue
        rail = rails[label]
        rail.source_refdes = next(iter(refs))
        rail.source_type = _infer_source_type(graph, rail.source_refdes)


def _propagate_sources_through_passive_bridges(
    rails: dict[str, PowerRail], graph: SchematicGraph
) -> None:
    """Forward-propagate `source_refdes` across 2-pin ferrite / inductor bridges.

    Apple-style schematics route an IC's clean output rail (e.g. PP1V8_AON)
    through a ferrite to a downstream filtered sub-rail (e.g.
    PP1V8_AON_CAM_CONN). Vision sometimes emits the `powers` edge on the
    upstream rail only, leaving the downstream sub-rail unsourced — but
    physically a ferrite or air-core inductor doesn't generate power, it
    just filters / smooths it. Both rails share the same upstream producer.

    This is intentionally restricted to inductor / ferrite bridges with
    exactly two pins, both labelled with rails. Resistors and capacitors
    are excluded — a cap is a decoupler, not a power path; a resistor on a
    power path is a sense / bleed component, not a clean filter. Iterating
    to a fixed point handles chains FL_a -> FL_b -> FL_c.
    """
    changed = True
    while changed:
        changed = False
        for component in graph.components.values():
            if component.type not in {"inductor", "ferrite"}:
                continue
            if len(component.pins) != 2:
                continue
            n1 = component.pins[0].net_label
            n2 = component.pins[1].net_label
            if not n1 or not n2:
                continue
            r1 = rails.get(n1)
            r2 = rails.get(n2)
            if r1 is None or r2 is None:
                continue
            if r1.source_refdes and not r2.source_refdes:
                r2.source_refdes = r1.source_refdes
                if r1.source_type:
                    r2.source_type = r1.source_type
                changed = True
            elif r2.source_refdes and not r1.source_refdes:
                r1.source_refdes = r2.source_refdes
                if r2.source_type:
                    r1.source_type = r2.source_type
                changed = True


_PASSIVE_TYPES = frozenset(
    {"resistor", "capacitor", "inductor", "ferrite", "diode"}
)


def _promote_ic_owning_switch_node_over_inductor(
    rails: dict[str, PowerRail], graph: SchematicGraph
) -> None:
    """Buck-topology source recovery.

    A 2-pin inductor sitting between a regulator's switch_node pin and a
    rail label is the buck OUTPUT FILTER, not the regulator itself.
    Physically the inductor stores energy and smooths the chopped switch
    waveform — it does not generate power. The actual producer is the IC
    whose `switch_node` pin shares a net with the inductor's switch_node
    side pin.

    The vision pass occasionally emits `inductor powers RAIL` edges
    (mistaking the buck output filter for the regulator), and the strict
    `powers` rule lets these through because it only checks
    `edge.src in graph.components`, not the producer-physics constraint
    that R / L / C / FL / D cannot generate power.

    Strategy: for each rail whose current source is a passive (R/L/C/FL/D),
    look up the topology — if a 2-pin inductor sits between the rail and a
    switch_node net OWNED by an IC (i.e. an IC has a `switch_node`-role
    pin on that same net), promote the IC as the rail's true producer.
    Additive: only fires when the current source is a passive (never
    overrides an IC source). When no IC owner exists (e.g. the regulator
    sits on an un-captured page), the passive stays as the fallback so we
    don't lose a sourced rail.

    Runs BEFORE rail-alias propagation so downstream alias rails inherit
    the corrected source.
    """
    # Index switch_node nets owned by ICs (first-IC-wins for stability).
    sw_owner: dict[str, str] = {}
    for ref, comp in graph.components.items():
        if comp.type not in _PRODUCER_COMPONENT_TYPES:
            continue
        for pin in comp.pins:
            if pin.role == "switch_node" and pin.net_label:
                sw_owner.setdefault(pin.net_label, ref)
    if not sw_owner:
        return

    # Walk every 2-pin inductor; if it sits between a switch_node net (IC-owned)
    # and a rail currently sourced by a passive, schedule a promotion.
    promotions: list[tuple[str, str]] = []
    for comp in graph.components.values():
        if comp.type != "inductor" or len(comp.pins) != 2:
            continue
        sw_net: str | None = None
        rail_label: str | None = None
        for pin in comp.pins:
            if not pin.net_label:
                continue
            if pin.role == "switch_node":
                sw_net = pin.net_label
            elif pin.net_label in rails:
                rail_label = pin.net_label
        if sw_net is None or rail_label is None:
            continue
        ic = sw_owner.get(sw_net)
        if ic is None:
            continue
        rail = rails[rail_label]
        if rail.source_refdes is None:
            # No current source — fill with the IC (buck pattern detected).
            promotions.append((rail_label, ic))
            continue
        # Existing source must be a passive to be overridden.
        current = graph.components.get(rail.source_refdes)
        if current is None or current.type not in _PASSIVE_TYPES:
            continue
        promotions.append((rail_label, ic))

    for label, ic in promotions:
        rail = rails[label]
        rail.source_refdes = ic
        rail.source_type = _infer_source_type(graph, ic)


def _propagate_sources_through_rail_aliases(
    rails: dict[str, PowerRail], graph: SchematicGraph
) -> None:
    """Forward-propagate `source_refdes` across rail-to-rail `powers` edges.

    Apple-style SoC schematics emit `powers` edges between two rail labels
    (e.g. `PP_CPU_PCORE -> VDD_CPU`, `PP1V2_SOC -> VDD12_PLL_CPU`) on the
    chip pin-list page. Physically these are the SAME net under two names:
    the package label (PP_*) and the die-side internal label (VDD_*). The
    `powers` edge between them is a label rename, not a component
    producer claim — and the existing strict `powers` rule above skips it
    because neither endpoint is in `graph.components`.

    Treat such an edge as an alias and let the downstream rail inherit its
    upstream rail's source. Additive: only fills `source_refdes` when None
    (never overrides an existing producer). Iterates to a fixed point so
    chains `A -> B -> C` propagate cleanly. Pure no-op on schematics with
    no rail-to-rail edges (e.g. mnt-reform-motherboard).
    """
    aliases: list[tuple[str, str]] = []
    for edge in graph.typed_edges:
        if edge.kind != "powers":
            continue
        if edge.src in rails and edge.dst in rails:
            aliases.append((edge.src, edge.dst))
    if not aliases:
        return

    changed = True
    while changed:
        changed = False
        for src_label, dst_label in aliases:
            src_rail = rails[src_label]
            dst_rail = rails[dst_label]
            if src_rail.source_refdes and not dst_rail.source_refdes:
                dst_rail.source_refdes = src_rail.source_refdes
                if src_rail.source_type:
                    dst_rail.source_type = src_rail.source_type
                changed = True


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
