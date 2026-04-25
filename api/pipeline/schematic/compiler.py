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
    NetNode,
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
    power_rails, rail_alias_map = _derive_power_rails(graph)
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
            if not pin.net_label:
                continue
            # Resolve through the rail alias map so caps whose `pin.net_label`
            # is one of two vision-aliased labels (e.g. `PP_CPU_PCORE` and
            # `VDD_CPU`) still find the merged canonical rail when their
            # original label was the dropped non-canonical entry.
            target_label = rail_alias_map.get(pin.net_label, pin.net_label)
            if target_label in power_rails:
                rail = power_rails[target_label]
                if refdes not in rail.decoupling:
                    rail.decoupling.append(refdes)
                break

    enriched_nets = _alias_nets_from_power_pin_names(graph)

    return ElectricalGraph(
        device_slug=graph.device_slug,
        components=enriched,
        nets=enriched_nets,
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
#     TI / ON Semi, present on Apple SoC pin-list pages). Also `GROUND`
#     spelled out (some block diagrams) and compact `GND<letter>` like
#     `GNDP` (Apple BBPMU pages — power ground without underscore).
#   - VSS family: VSS (universal CMOS substrate ground used by Apple,
#     Arm, Intel), AVSS / DVSS (analog/digital substrate), VSSA / VSSD
#     (alt spellings — same physical net, different style guide).
# Two anchor styles:
#   1. start-anchored — labels that BEGIN with a ground keyword (e.g.
#      `AGND_RF`, `VSSA_PLL`, `GROUND`, `GNDP`).
#   2. domain-prefixed (`<DOMAIN>_<ground-token>(_<SUFFIX>)?`) — Apple SoC
#      pin-list pages and codec subblocks emit `CODEC_AGND`, `BBPMU_AGND_K`,
#      `PMU_VSS_RTC` etc. where the ground keyword sits AFTER a domain
#      qualifier. This style only matches when the underscore-separated
#      tail token IS one of the ground keywords; arbitrary substrings
#      buried in a rail name (`PP1V8_VSSADC_SENSE` would not match — VSS
#      is not the head of a `_`-separated trailing segment) stay rails.
_GROUND_LABEL_START = re.compile(
    r"^(?:"
    r"GROUND"                     # spelled-out
    r"|GND[A-Z0-9]?"              # GND, GNDP, GNDA, GND0 — compact (no separator)
    r"|[ADPS]?GND"                # GND, AGND, DGND, PGND, SGND
    r"|VSS[AD]?"                  # VSS, VSSA, VSSD
    r"|[AD]VSS"                   # AVSS, DVSS
    r")(?:_[A-Z0-9]+)*$"
)
# Domain-prefixed: <DOMAIN>(_<MORE>)*_<ground-token>(_<SUFFIX>)?
# Examples: CODEC_AGND, BBPMU_AGND_K, PMU_VSS_RTC.
_GROUND_LABEL_DOMAIN = re.compile(
    r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*"
    r"_(?:GND|AGND|DGND|PGND|SGND|GNDA|GNDD|VSS|AVSS|DVSS|VSSA|VSSD)"
    r"(?:_[A-Z0-9]+)?$"
)


def _is_noise_rail_label(label: str) -> bool:
    if label in _RAIL_LABEL_NOISE:
        return True
    if _GROUND_LABEL_START.match(label):
        return True
    if _GROUND_LABEL_DOMAIN.match(label):
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


def _derive_power_rails(
    graph: SchematicGraph,
) -> tuple[dict[str, PowerRail], dict[str, str]]:
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
    _recognize_buck_self_sense_outputs(rails, graph)
    _propagate_sources_through_rail_aliases(rails, graph)
    _propagate_sources_through_consumer_topology(rails, graph)
    _augment_sources_from_external_connectors(rails, graph)
    rail_alias_map = _coalesce_rails_via_shared_cap_pins(rails, graph)

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
    # Also covers the merged-rail case where the cap-pin coalesce step
    # unioned two rails whose source on the canonical side was on the other
    # side's consumer list.
    for rail in rails.values():
        if rail.source_refdes is not None and rail.source_refdes in rail.consumers:
            rail.consumers.remove(rail.source_refdes)
    return rails, rail_alias_map


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

    Two paths, run sequentially, ambiguity gate (single candidate) enforced
    on each independently:

    1. **Pin-label keyed.** Direct match: `pin.net_label == rail.label`.
       Covers the canonical case where the IC pin's net_label IS the rail
       name (mnt-reform-style boards, KiCad schematics, mid-tier vendor
       PMICs).

    2. **Net-connects walk.** Apple-style PMICs double-label power outputs:
       the IC pin's `pin.net_label` is the die-side name (`VLDO10`) but the
       merger placed the pin under the package-side rail name
       (`PP1V1_FCAM_DVDD`) via `net.connects`. Path 1 misses these because
       it looks up `pin.net_label` against `rails`. Path 2 walks each
       unsourced rail's `net.connects` and resolves the pin object back to
       its component; if it's a producer-type component with a producer-
       role pin, it qualifies. Pure no-op on schematics that don't use
       die-side aliasing (verified empirically on mnt-reform).
    """
    # Path 1 — pin.net_label keyed.
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

    # Path 2 — net-connects walk for rails still unsourced after Path 1.
    # Same single-candidate ambiguity gate. The pin must belong to a
    # producer-type component AND carry a producer-role tag from vision —
    # otherwise we'd fall back to topology guessing, which we don't do.
    connect_candidates: dict[str, set[str]] = {}
    for label, rail in rails.items():
        if rail.source_refdes is not None:
            continue
        net = graph.nets.get(label)
        if net is None:
            continue
        for ref_pin in net.connects:
            if "." not in ref_pin:
                continue
            ref, pin_num = ref_pin.split(".", 1)
            comp = graph.components.get(ref)
            if comp is None or comp.type not in _PRODUCER_COMPONENT_TYPES:
                continue
            for pin in comp.pins:
                if pin.number != pin_num:
                    continue
                if pin.role in _PRODUCER_PIN_ROLES:
                    connect_candidates.setdefault(label, set()).add(ref)
                break

    for label, refs in connect_candidates.items():
        if len(refs) != 1:
            continue
        rail = rails[label]
        if rail.source_refdes is not None:
            continue
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


# ----------------------------------------------------------------------
# PMU buck-output self-sense recognition
# ----------------------------------------------------------------------

# Pin-name pattern for a switching regulator's regulated-rail self-sense
# input. Apple Tigris (D2422), Qualcomm PM660-class PMICs, MediaTek MT63xx
# and most SoC PMUs share this convention: the chip's integrated buck cell
# drives an LX switch node out to the external L+C filter, and the
# filtered output comes BACK INTO the chip on a `VDD_BUCK<n>*` pin (a
# `V`-prefixed alternative — `VVDD_BUCK<n>` — appears on Samsung S2MPS
# PMICs). At that pin the IC senses the regulated voltage to close the
# control loop. Vision tags the pin as `power_in` because at the bond
# wire the node receives the post-inductor voltage, even though the IC
# itself is the rail's only producer.
#
# Anchored at start of pin name with `\d+` enforcing a numeric index
# (single, double, or triple digits). Ground-family names (VSS / AVSS)
# are not at risk — they don't carry `_BUCK<n>` substrings.
_BUCK_SELF_SENSE_PIN = re.compile(r"^V?VDD_BUCK\d+")

# Rail-label pattern for charge-pump / boost-cell output rails on multi-cell
# PMICs that name their cells in the rail label rather than at the pin.
# Apple's ACORN charge pump (iPhone X U5600) is the canonical example: a
# single IC carries multiple boost cells, each cell drives an external L+C
# filter, the filtered output loops back into the chip on a `power_in` pin
# for closed-loop control — exactly the buck self-sense topology, but the
# chip designer named the pins generically (`VB`, `VG`) and pushed the cell
# identifier into the rail label instead (`PP_BOOST1_ACORN`,
# `PP5V45_BOOST2_ACORN`). The existing `_BUCK_SELF_SENSE_PIN` pin-name gate
# misses these.
#
# Anchored start+end with explicit `_<NICK>` tail so we never partial-match
# generic names like `PWR_BOOST` or `BOOST_5V` from KiCad / MCU schematics
# that don't follow Apple's cell-named convention. Requires:
#   - leading `PP` (Apple package-side rail prefix)
#   - optional voltage segment (`<n>V<m>` like `5V45`)
#   - mandatory `_BOOST<n>_` cell identifier
#   - mandatory `<NICK>` tail (the IC's nickname — ACORN, RACER, …)
_BOOST_OUTPUT_RAIL_LABEL = re.compile(
    r"^PP(?:\d+V\d*)?_BOOST\d+_[A-Z][A-Z0-9_]*$"
)


def _recognize_buck_self_sense_outputs(
    rails: dict[str, PowerRail], graph: SchematicGraph
) -> None:
    """Attribute PMU buck-output rails sensed back through chip pins.

    PMU ICs route their integrated buck regulator output through an
    external L+C filter and BACK INTO the chip on a self-sense pin
    matching `^V?VDD_BUCK\\d+` (role `power_in`). When the external
    filter inductor lives on the SAME page as the PMU pin list, the
    existing `_promote_ic_owning_switch_node_over_inductor` pass picks
    it up via the inductor bridge. When the inductor is on a separate
    schematic page that wasn't captured (common on Apple's segregated
    "power tree" pages — the regulator IC pin list is on one page,
    the buck output filters on another), the rail stays unsourced
    despite physically being a known IC output.

    This pass closes that gap with four conservative gates:

      1. Rail R is currently unsourced — additive only, never overrides.
      2. R's pin connections include exactly one IC X plus only passives
         (cap / resistor / inductor / ferrite / diode). A buck rail
         shared between a primary and secondary PMU (e.g. iPhone X
         `VDD_BUCK9` connecting both Tigris and the camera PMU) is
         genuinely ambiguous about which side produces vs. consumes,
         so we leave it unsourced.
      3. IC X has at least one `switch_node`-role pin — proves IC X is
         a switching regulator, not a consumer SoC. Without this gate,
         a consumer SoC like Apple A11 (U1000) with `VDD_CPU` /
         `VDD_GPU` `power_in` pins on its rails would be mis-attributed
         as the rails' producer.
      4. IC X's pin connecting to R has role `power_in` AND
         (a) pin.name matches `_BUCK_SELF_SENSE_PIN` — the universal SoC
             PMU buck self-sense convention, OR
         (b) the rail label matches `_BOOST_OUTPUT_RAIL_LABEL` — Apple
             ACORN-style charge-pump / boost-cell convention where the
             cell identifier lives in the rail label (`PP_BOOST1_ACORN`)
             rather than the pin name.

    Pure no-op on schematics that don't use this convention (mnt-reform
    KiCad-style boards, MAX*/LTC* point regulators with `VOUT` / `OUT`
    pin names instead). Empirically verified: zero `VDD_BUCK*` pins and
    zero `PP*_BOOST<n>_*` rails on mnt-reform-motherboard.

    Runs AFTER `_promote_ic_owning_switch_node_over_inductor` so the
    inductor-bridge path is preferred when both paths are available
    (cleaner topology — explicit external filter wins over a self-sense
    fallback). Runs BEFORE `_propagate_sources_through_rail_aliases` so
    any `VDD_BUCK<n>` rail that aliases to a die-side rail (none observed
    today, but conventions evolve) inherits the corrected source.
    """
    sw_ics: set[str] = set()
    for ref, comp in graph.components.items():
        if comp.type != "ic":
            continue
        for pin in comp.pins:
            if pin.role == "switch_node":
                sw_ics.add(ref)
                break
    if not sw_ics:
        return

    for label, rail in rails.items():
        if rail.source_refdes is not None:
            continue
        net = graph.nets.get(label)
        if net is None:
            continue

        # Walk the rail's connections: enforce single-IC + all-passives.
        ic_refs: set[str] = set()
        bail = False
        for ref_pin in net.connects:
            if "." not in ref_pin:
                continue
            ref = ref_pin.split(".", 1)[0]
            comp = graph.components.get(ref)
            if comp is None:
                continue
            if comp.type == "ic":
                ic_refs.add(ref)
            elif comp.type not in _PASSIVE_TYPES:
                # Non-passive non-IC (connector / transistor / module /
                # oscillator / led) — not a clean self-sense topology.
                bail = True
                break
        if bail or len(ic_refs) != 1:
            continue
        (ic_ref,) = ic_refs
        if ic_ref not in sw_ics:
            continue

        ic = graph.components[ic_ref]
        for pin in ic.pins:
            if pin.net_label != label:
                continue
            if pin.role != "power_in":
                continue
            pin_name_matches = bool(
                pin.name and _BUCK_SELF_SENSE_PIN.match(pin.name)
            )
            label_matches = bool(_BOOST_OUTPUT_RAIL_LABEL.match(label))
            if not (pin_name_matches or label_matches):
                continue
            rail.source_refdes = ic_ref
            rail.source_type = _infer_source_type(graph, ic_ref)
            break


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


# ----------------------------------------------------------------------
# Consumer-topology source inference
# ----------------------------------------------------------------------


def _propagate_sources_through_consumer_topology(
    rails: dict[str, PowerRail], graph: SchematicGraph
) -> None:
    """Infer a missing rail source from the lone consumer's other supplies.

    On modern boards a single chip — SoC, baseband, camera-module bus front-end —
    is fed by *one* upstream PMU. The chip pin-list page enumerates 10–15
    die-side `power_in` pins (`VDD_CPU`, `VDDQL_DDR<n>`, `VDDIO18_GRP<n>` …)
    but the LLM rarely captures the producer-side `powers` edge for every
    one of them; the producer is implied by the conventional fanout from the
    same PMU instance. The existing rail-alias propagation
    (`_propagate_sources_through_rail_aliases`) catches die-side rails where
    the LLM did capture an explicit `powers` edge between two rail labels;
    this pass is the consumer-side topology fallback for the rest.

    Heuristic, per unsourced rail R:
      1. R must have exactly ONE consumer C — multi-consumer rails could mix
         domains (e.g. a 1.8 V I/O rail spanning the SoC and a discrete IC may
         not share a PMU).
      2. Gather the family F = sourced rails that C also consumes.
      3. F must be ≥ 3 large (noise filter against accidental 1- or 2-rail
         families, common when only a handful of supplies were captured).
      4. F must agree unanimously on a single source S — any disagreement
         (e.g. two PMUs feeding different banks of the same chip) bails.
      5. S must NOT equal C — a regulator never self-sources, and rules out
         the pathological case where C produces one of its own die-side
         rails (covered separately by the buck-self-sense pass).
      6. S must exist in `graph.components` — defense in depth against an
         upstream pass that left a stale source string. (In practice every
         family entry was source-set by an earlier pass that already
         enforces this invariant; we re-check to keep the pass independent.)

    Side properties preserved:
      - `voltage_nominal` untouched (no I3/I6 risk).
      - `consumers` untouched (no I4 risk; the final scrub still runs).
      - `decoupling` untouched.
      - `enable_net` untouched.

    Cycle (I1) safety: setting source=S on a rail consumed by C only adds
    `depends_on(C, S)` edges. The board-level invariant — PMU never depends
    on chips it powers — already holds in the captured graph (else the
    existing producer-pin / rail-alias passes would have triggered I1). We
    do not introduce a new dependency direction; we only thicken existing
    edges.

    No-op on schematics where every chip already has unanimous source
    coverage (mnt-reform-motherboard) or where families never reach size 3
    (small boards with one IC).
    """
    rail_list = list(rails.values())

    # Pre-index: for each chip, the list of source_refdes it already gets.
    consumer_to_sourced_supplies: dict[str, list[str]] = {}
    for r in rail_list:
        if r.source_refdes is None:
            continue
        for cons in r.consumers:
            consumer_to_sourced_supplies.setdefault(cons, []).append(r.source_refdes)

    for r in rail_list:
        if r.source_refdes is not None:
            continue
        if len(r.consumers) != 1:
            continue
        cons = r.consumers[0]
        family = consumer_to_sourced_supplies.get(cons, [])
        if len(family) < 3:
            continue
        unique_sources = set(family)
        if len(unique_sources) != 1:
            continue
        source = next(iter(unique_sources))
        if source == cons:
            continue
        if source not in graph.components:
            continue
        r.source_refdes = source
        r.source_type = _infer_source_type(graph, source)


# ----------------------------------------------------------------------
# External-input connector source recovery
# ----------------------------------------------------------------------

# Top-level external-input rail naming convention shared by phone, laptop,
# SBC and DIY board schematics. A rail whose label matches one of these
# patterns is, by convention, the FIRST rail on the board fed by an
# external connector — barrel jack, USB cable, battery pack, mains brick,
# discrete power header. Pattern is anchored at start AND end so we never
# partial-match a longer rail name (e.g. `USB_PWR_TIMER` does NOT match
# `USB_PWR`, `PP1V8_VBUS_SENSE` does NOT match `VBUS`).
#
# Families covered:
#   - VBUS / USB_VBUS / USB_PWR — USB bus power (USB-A, USB-C, microUSB)
#   - VBAT / VBATT / BAT / BATT — battery pack input (phones, laptops)
#   - VAC / AC_IN / MAINS — AC mains input (PSU primary side)
#   - VDC / DC_IN — generic DC input
#   - VIN — generic regulator input header / bare wire input
#   - +24V_IN / 5V_IN / +3V3_IN / 12V_IN — explicit voltage-bearing input
#     names (barrel jacks with hardwired voltage labels, screw terminals)
#   - +5V_SUPPLY / 12V_SUPPLY — explicit "supply" suffix variant
#
# Names like `PP1V8_CAM_WIDE_VDDIO_CONN` (internal supply going OUT through
# a connector to a peripheral module) deliberately do NOT match — the
# connector there is downstream, not source.
_EXTERNAL_INPUT_RAIL = re.compile(
    r"^(?:"
    r"VBUS"
    r"|VBAT|VBATT|BAT|BATT"
    r"|VAC|AC_IN|MAINS"
    r"|VDC|DC_IN"
    r"|VIN"
    r"|USB_PWR|USB_VBUS"
    r"|\+?\d+V\d*_IN"
    r"|\+?\d+V_SUPPLY"
    r")$"
)


def _augment_sources_from_external_connectors(
    rails: dict[str, PowerRail], graph: SchematicGraph
) -> None:
    """Promote a connector as the source of an external-input rail.

    Boards always have one or more rails fed externally — a barrel jack, a
    USB cable, a battery pack, a power header. Topology-wise the connector
    IS the producer of those rails as far as the on-board graph is
    concerned: no upstream regulator exists *on the board* to claim the
    source. The connector pins for these rails are vision-labelled
    `power_in` (power flowing INTO the pin from the external source),
    which is why `_augment_sources_from_producer_pins` (looking for
    `power_out` / `switch_node`) misses them.

    Three guardrails keep this from misfiring on internal supply rails
    delivered TO peripheral modules through a connector (camera, flash,
    display — common on phone schematics):

      1. **Label gate.** Only rail labels matching `_EXTERNAL_INPUT_RAIL`
         qualify (anchored start+end). Internal-supply rails like
         `PP1V8_CAM_WIDE_VDDIO_CONN` or `PP_STROBE_WARM_WIDE_LED` do not
         match the pattern and are skipped.

      2. **No-producer gate.** If ANY component on the rail (including
         passives like ferrites or inductors) has a `power_out` or
         `switch_node` pin, we skip — that means there's an on-board
         producer (or a passive bridge delivering an upstream rail) and
         the connector is downstream, not source.

      3. **Connector-with-power_in gate.** At least one connector must
         have a `power_in` pin on the rail. Without it there's no
         candidate.

    Picks the lowest refdes deterministically when multiple connectors
    qualify (e.g. multiple USB ports sharing VBUS via the system charger,
    common on multi-port hubs and laptop motherboards).

    Runs LAST in the source-augmentation chain so on-board producers
    (regulators, passive bridges, rail aliases) always win — this is the
    fallback for the small set of rails that genuinely have no on-board
    producer.
    """
    for label, rail in rails.items():
        if rail.source_refdes is not None:
            continue
        if not _EXTERNAL_INPUT_RAIL.match(label):
            continue
        has_producer = False
        connector_candidates: set[str] = set()
        for ref, comp in graph.components.items():
            for pin in comp.pins:
                if pin.net_label != label:
                    continue
                if pin.role in _PRODUCER_PIN_ROLES:
                    has_producer = True
                    break
                if comp.type == "connector" and pin.role == "power_in":
                    connector_candidates.add(ref)
            if has_producer:
                break
        if has_producer or not connector_candidates:
            continue
        chosen = sorted(connector_candidates)[0]
        rail.source_refdes = chosen
        rail.source_type = _infer_source_type(graph, chosen)


# ----------------------------------------------------------------------
# Rail coalescing — cap-pin shared between two power nets = aliasing
# ----------------------------------------------------------------------


def _coalesce_rails_via_shared_cap_pins(
    rails: dict[str, PowerRail],
    graph: SchematicGraph,
) -> dict[str, str]:
    """Merge rails whose underlying is_power nets share a capacitor pin.

    Vision occasionally places the same physical pin on TWO power-net labels
    (the package-side label and the die-side label, e.g. `PP_CPU_PCORE` and
    `VDD_CPU` on iPhone X p4 — pin `C1703.1` is on both). Capacitor terminals
    are electrically isolated (an insulator separates them), so a single cap
    pin can physically belong to ONLY ONE net. When vision says a cap pin is
    on two power nets, those nets are aliases of the same physical rail —
    the dual-label is a documentation convention (Apple's `PP_*` package /
    `VDD_*` die-side is the dominant case, but the signal is universal).

    Restricting the support evidence to capacitor pins is what makes this
    safe across schematic styles. IC pins on multiple nets ARE often valid
    aliases too (the SoC pin's name matches the die-side label and the trace
    carries a package-side label) but they're also more vulnerable to vision
    misreads of adjacent labels — caps give a topology-grounded signal:
    physical-cap-terminal isolation. Resistors and inductors are excluded
    too: each terminal is electrically distinct (resistor between two nets
    is the canonical case, not an alias).

    Implementation:
    - Build pin → set of rail labels (only labels in `rails` — ground/noise
      already filtered, both labels guaranteed `is_power=True`, no need to
      re-check).
    - Union-find on rail labels supported by capacitor pins on ≥ 2 of them.
    - Voltage coherence guard: if both sub-rails parse to incompatible
      voltages (Δ > 0.05V), skip the cluster (mismatch is more likely a
      vision OCR error than a real alias). When only one side parses, the
      other is treated as compatible.
    - Canonical preference per cluster (deterministic):
        1. has `source_refdes` (so source-attribution work isn't lost),
        2. higher `len(net.connects)` (richer underlying capture),
        3. alphabetically smallest label (tie-breaker).
    - Merge into canonical: union consumers + decoupling (preserve order,
      dedup); inherit `source_refdes` / `source_type` / `voltage_nominal`
      / `enable_net` from the first sub-rail with a non-null value
      (canonical wins ties because preference put it first). Drop the
      non-canonical entry from `rails`.
    - Return alias_map `{dropped_label: canonical_label}` so the caller can
      route decoupling-cap assignments (in `compile_electrical_graph`)
      through the canonical rail when a cap's `pin.net_label` is the
      dropped alias.

    Pure no-op when no cap pin spans ≥ 2 power rails (mnt-reform-style
    boards that don't dual-label rails). Empirically verified: zero merges
    on mnt-reform-motherboard, ~7 merges on iPhone X (covering the
    PP_CPU_PCORE/VDD_CPU, PP_GPU/VDD_GPU, PP_SOC_S1/VDD_SOC and similar
    SoC/PMIC dual-label families).

    Anti-collapse safety: typical merge count is 5-10% of total rails;
    well within I5's 30% floor.

    Runs LAST in the rail derivation chain so source-attribution work from
    earlier passes (`_recognize_buck_self_sense_outputs`,
    `_propagate_sources_through_rail_aliases`,
    `_augment_sources_from_external_connectors`) flows into the canonical
    rail when present on either sub-rail.
    """
    # Collect cap refdes (no caps -> nothing to do).
    cap_refdes = {
        ref for ref, c in graph.components.items() if c.type == "capacitor"
    }
    if not cap_refdes:
        return {}

    # Pin -> set of rail labels supported by cap pins.
    cap_pin_to_rails: dict[str, set[str]] = {}
    for label, _rail in rails.items():
        net = graph.nets.get(label)
        if net is None:
            continue
        for ref_pin in net.connects:
            if "." not in ref_pin:
                continue
            ref = ref_pin.split(".", 1)[0]
            if ref in cap_refdes:
                cap_pin_to_rails.setdefault(ref_pin, set()).add(label)

    # Union-find on rail labels.
    parent: dict[str, str] = {}

    def _find(x: str) -> str:
        # Path-compression find with iterative climb.
        root = x
        while parent.get(root, root) != root:
            root = parent[root]
        # Compress.
        cur = x
        while parent.get(cur, cur) != root:
            nxt = parent[cur]
            parent[cur] = root
            cur = nxt
        return root

    def _union(a: str, b: str) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    for _pin, labels in cap_pin_to_rails.items():
        if len(labels) < 2:
            continue
        # Voltage coherence: skip the cluster when two sub-rails parse to
        # incompatible voltages. Treat null voltage as compatible with any.
        voltages = [
            rails[lbl].voltage_nominal
            for lbl in labels
            if rails[lbl].voltage_nominal is not None
        ]
        if voltages and (max(voltages) - min(voltages) > 0.05):
            continue
        sorted_labels = sorted(labels)
        for lbl in sorted_labels:
            if lbl not in parent:
                parent[lbl] = lbl
        for lbl in sorted_labels[1:]:
            _union(sorted_labels[0], lbl)

    # Group by union-find root.
    groups: dict[str, set[str]] = {}
    for lbl in parent:
        groups.setdefault(_find(lbl), set()).add(lbl)

    alias_map: dict[str, str] = {}

    def _canonical_key(lbl: str) -> tuple[int, int, str]:
        rail = rails[lbl]
        net = graph.nets.get(lbl)
        n_connects = len(net.connects) if net is not None else 0
        return (
            0 if rail.source_refdes else 1,  # sourced first
            -n_connects,                      # richer net first
            lbl,                              # alpha tie-breaker
        )

    for cluster in groups.values():
        if len(cluster) < 2:
            continue
        canonical = min(cluster, key=_canonical_key)
        canonical_rail = rails[canonical]
        for other in sorted(cluster):
            if other == canonical:
                continue
            other_rail = rails[other]
            for c in other_rail.consumers:
                if c not in canonical_rail.consumers:
                    canonical_rail.consumers.append(c)
            for d in other_rail.decoupling:
                if d not in canonical_rail.decoupling:
                    canonical_rail.decoupling.append(d)
            if (
                canonical_rail.source_refdes is None
                and other_rail.source_refdes is not None
            ):
                canonical_rail.source_refdes = other_rail.source_refdes
                if other_rail.source_type and not canonical_rail.source_type:
                    canonical_rail.source_type = other_rail.source_type
            if (
                canonical_rail.voltage_nominal is None
                and other_rail.voltage_nominal is not None
            ):
                canonical_rail.voltage_nominal = other_rail.voltage_nominal
            if (
                canonical_rail.enable_net is None
                and other_rail.enable_net is not None
            ):
                canonical_rail.enable_net = other_rail.enable_net
            del rails[other]
            alias_map[other] = canonical

    return alias_map


# ----------------------------------------------------------------------
# Net aliasing — die-side power pin names
# ----------------------------------------------------------------------

# Power-supply naming convention shared by all major SoCs (Apple, Qualcomm,
# MediaTek, Samsung, Intel, ARM partners). Anchored at start so we never
# match substrings buried in a longer pin name.
#
#   - V family: VDD/VCC/VEE/VREG/VREF/VBAT/VBUS — positive supplies.
#     Ground variants (VSS / AVSS / DVSS) are deliberately EXCLUDED — they
#     are filtered out as ground in `_is_noise_rail_label` and must not be promoted
#     to nets.
#   - A/D-prefixed: AVDD / AVCC / DVDD / DVCC — analog/digital domains.
#   - Apple PP family: PP / VPP — package-side rail names.
#
# An optional `[A-Z0-9_]*` tail captures the domain qualifier (e.g.
# `VDD18_TSADC_CPU0`, `VDD_FIXED_PCIE_REFBUF`). Only matched against pin.name
# — never against arbitrary text or labels — and only when the pin's role is
# `power_in` / `power_out`, so signal pin names like `RXD` or coordinates
# like `K1` are never promoted.
_POWER_PIN_NAME_ALIAS = re.compile(
    r"^(?:V(?:DD|CC|EE|REG|REF|BAT|BUS)|AVDD|AVCC|DVDD|DVCC|PP|VPP)[A-Z0-9_]*$"
)
_POWER_PIN_ALIAS_ROLES = frozenset({"power_in", "power_out"})


def _alias_nets_from_power_pin_names(
    graph: SchematicGraph,
) -> dict[str, NetNode]:
    """Promote die-side power pin names to NetNode aliases.

    SoC schematics double-label power nets: the SAME wire carries the
    package-side name (e.g. `PP1V1_S2`) on its trace and the die-side name
    (e.g. `VDD_BYPASS`) at the IC pin. The vision pass captures the
    package-side label as `pin.net_label` and the die-side label as
    `pin.name` — but only the former enters `graph.nets`. The die-side name
    is the canonical reference in datasheets and PMIC traces (« the
    VDD_BYPASS rail »); a diagnostic agent should be able to resolve it
    just like the package-side name.

    For each component pin where:
      - pin.role ∈ {power_in, power_out}, AND
      - pin.name matches the universal power-net pattern, AND
      - pin.name is not already a known net,
    add a NetNode aliasing the connected net (same connects + page set,
    is_power=True). Pure no-op when no such pins exist (mnt-reform-style
    boards that don't emit `pin.name` for power pins).

    The added entry is a real NetNode — `connects` is inherited from the
    underlying physical net so downstream consumers (UI, diagnostic agent)
    can walk it.

    Two materialization paths, attempted in order per pin:

    1. **Aliased path** (default, original behaviour). When
       `pin.net_label` resolves to an existing `graph.nets` entry, the new
       NetNode inherits its `connects` / `pages` / `is_global` — the
       die-side name and the package-side name share one physical wire.

    2. **Solitary die-side path** (additive). When `pin.name == pin.net_label`
       and that label is NOT in `graph.nets`, the merger has dropped the
       net because only one component pin in the captured schematic carries
       it (single-pin nets aren't materialized as NetNodes during
       `merge_pages`). On Apple SoC pin-list pages this is the dominant
       pattern for the analog auxiliary supplies (`VDD18_TSADC_*`,
       `VDD18_EFUSE*`, `VDD_FIXED_PLL_DDR<n>`) — each die-side rail enters
       the SoC on a single pin from a PMU on a separate (often un-captured)
       page. The label is real and the diagnostic agent / UI / eval should
       see it. Synthesize a minimal NetNode anchored on the only known pin;
       pages/is_global default conservatively because the underlying wire
       was never materialized.

    Ground-family names (VSS / AVSS / DVSS) are excluded by the regex.
    Power rails are NOT touched — `eg.power_rails` keeps its original keys
    so sourced/voltage invariants and rails counts are unchanged.
    """
    aliases: dict[str, NetNode] = dict(graph.nets)
    for component in graph.components.values():
        for pin in component.pins:
            if pin.role not in _POWER_PIN_ALIAS_ROLES:
                continue
            name = pin.name
            if not name or name in aliases:
                continue
            if not _POWER_PIN_NAME_ALIAS.match(name):
                continue
            net_label = pin.net_label
            if not net_label:
                continue
            underlying = graph.nets.get(net_label)
            if underlying is not None:
                aliases[name] = NetNode(
                    label=name,
                    is_power=True,
                    is_global=underlying.is_global,
                    pages=list(underlying.pages),
                    connects=list(underlying.connects),
                )
            elif net_label == name:
                aliases[name] = NetNode(
                    label=name,
                    is_power=True,
                    is_global=False,
                    pages=[],
                    connects=[f"{component.refdes}.{pin.number}"],
                )
    return aliases


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
