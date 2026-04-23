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

import asyncio
import logging
from collections import Counter

from anthropic import AsyncAnthropic
from pydantic import BaseModel, ConfigDict, Field

from api.pipeline.schematic.schemas import (
    ComponentKind,
    ComponentNode,
    ElectricalGraph,
)
from api.pipeline.tool_call import call_with_forced_tool

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
    # Evidence 1 — explicit `decouples` edge pointing at this cap.
    for edge in graph.typed_edges:
        if edge.kind == "decouples" and edge.dst == comp.refdes:
            return "decoupling", 0.85

    nets = _pin_nets(comp)
    if len(nets) < 2:
        return None, 0.0
    n1, n2 = nets[0], nets[1]
    rail1 = _is_power_rail(graph, n1)
    rail2 = _is_power_rail(graph, n2)
    gnd1 = _is_ground_net(n1)
    gnd2 = _is_ground_net(n2)

    # Evidence 2 — rail-to-GND near a consumer IC on the same rail.
    if (rail1 and gnd2) or (rail2 and gnd1):
        rail_label = n1 if rail1 else n2
        rail = graph.power_rails.get(rail_label)
        if rail and rail.consumers:
            # Large-value caps classify as bulk; without value info we default
            # to decoupling. `value.primary` parsing left to the LLM pass.
            return "decoupling", 0.65
        # Rail with no consumers found — fall back to filter.
        return "filter", 0.45

    # Evidence 3 — signal-to-signal (both non-power, non-GND) = AC coupling.
    if not rail1 and not rail2 and not gnd1 and not gnd2:
        return "ac_coupling", 0.55

    return None, 0.0


def _classify_diode(
    graph: ElectricalGraph, comp: ComponentNode,
) -> tuple[str | None, float]:
    nets = _pin_nets(comp)
    if len(nets) < 2:
        return None, 0.0
    # For >2 nets, take first two sorted (unusual but can happen with multi-pin models)
    n1, n2 = sorted(nets)[:2]
    gnd1 = _is_ground_net(n1)
    gnd2 = _is_ground_net(n2)
    rail1 = _is_power_rail(graph, n1)
    rail2 = _is_power_rail(graph, n2)

    # Evidence 1 — flyback: an inductor spans the same two nets.
    my_nets = set(nets)
    for other in graph.components.values():
        if other.refdes == comp.refdes or other.type != "inductor":
            continue
        other_nets = set(_pin_nets(other))
        if my_nets == other_nets:
            return "flyback", 0.75

    # Evidence 2 — signal to GND = ESD clamp.
    if gnd1 or gnd2:
        # One end GND, other end a non-rail net → ESD.
        other = n2 if gnd1 else n1
        if not _is_power_rail(graph, other):
            return "esd", 0.6

    # Evidence 3 — rail to rail = rectifier-ish.
    if rail1 and rail2:
        return "rectifier", 0.5

    return None, 0.0


def _classify_ferrite(
    graph: ElectricalGraph, comp: ComponentNode,
) -> tuple[str | None, float]:
    """A ferrite bead's only practical role is `filter` — between a
    rail and a filtered variant of it (`+3V3` → `+3V3_AUDIO`)."""
    nets = _pin_nets(comp)
    if len(nets) < 2:
        return None, 0.0
    n1, n2 = nets[0], nets[1]
    rail1 = _is_power_rail(graph, n1)
    rail2 = _is_power_rail(graph, n2)
    if rail1 and rail2:
        return "filter", 0.85
    # One side rail, other side a net-not-yet-promoted-to-rail is still filter.
    if rail1 or rail2:
        return "filter", 0.65
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


# ---------------------------------------------------------------------------
# Opus/Sonnet enrichment pass — mirrors net_classifier.classify_nets_llm.
# Fills the 30% of passives the heuristic leaves as role=None, using
# full context (designer notes + connected refdes + pin labels).
# ---------------------------------------------------------------------------

_BATCH_SIZE = 150  # see spec — larger than net_classifier's 100, passives
                    # carry less context per entry so batches can be deeper
_DEFAULT_CLASSIFIER_MODEL = "claude-sonnet-4-6"

SUBMIT_PASSIVE_TOOL_NAME = "submit_passive_classification"


class PassiveAssignment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    refdes: str = Field(description="Exact refdes (copy verbatim from input list).")
    kind: str = Field(
        description=(
            "ComponentKind — one of: passive_r · passive_c · passive_d · passive_fb. "
            "Must match the component's physical type (resistor → passive_r, "
            "capacitor → passive_c, diode → passive_d, ferrite → passive_fb)."
        ),
    )
    role: str | None = Field(
        default=None,
        description=(
            "Canonical role for this kind. passive_r: series · feedback · pull_up · "
            "pull_down · current_sense · damping. passive_c: decoupling · bulk · "
            "filter · ac_coupling · tank · bypass. passive_d: flyback · rectifier · "
            "esd · reverse_protection · signal_clamp. passive_fb: filter. Use null "
            "when topology + notes genuinely don't narrow it down — never guess."
        ),
    )
    confidence: float = Field(
        ge=0.0, le=1.0, default=0.7,
        description="Confidence in the role assignment, 0..1. Lower when evidence "
                    "is indirect or when falling back between two equally plausible roles.",
    )
    rationale: str = Field(
        default="",
        description="ONE SHORT sentence citing the topology / note evidence. "
                    "Example: 'Between +3V3 and GND near U7 (LPC consumer) → decoupling'.",
    )


class PassiveClassification(BaseModel):
    """LLM-produced classification of passives flagged `role=None` by the heuristic.

    Persisted alongside the electrical graph as
    `passive_classification_llm.json`. Merged with the heuristic
    assignments by `classify_passives()` — heuristic wins when it
    already has a confident role; LLM fills the holes.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    device_slug: str
    assignments: list[PassiveAssignment] = Field(
        default_factory=list,
        description="One entry per input passive. Every passive in the input list "
                    "MUST appear exactly once — never skip.",
    )
    ambiguities: list[str] = Field(
        default_factory=list,
        description="Short sentences for passives where the role genuinely couldn't "
                    "be decided (model emits role=null and lists them here).",
    )
    model_used: str = Field(description="Anthropic model id that produced this output.")


_SYSTEM_PROMPT = """You are an expert in board-level electronics. For every
passive in the input list, assign a functional `role` from the canonical set
for that passive's kind. The deterministic heuristic upstream already
classified most passives; the ones reaching you are the ambiguous cases
where pure topology was not enough.

Canonical roles (use exactly these strings, or null if genuinely undecidable):

  passive_r (resistors):
    - series          — in the power path between two rail domains
    - feedback        — in a buck/boost regulator feedback divider
    - pull_up         — between a signal net and a rail
    - pull_down       — between a signal net and GND
    - current_sense   — low-value (< 1 Ω) in series on a power rail for current measurement
    - damping         — in a signal path, between two signal nets, usually low value

  passive_c (capacitors):
    - decoupling      — between a rail and GND, near a specific IC's VCC pin
    - bulk            — between a rail and GND, large value (>10 µF), broad decoupling
    - filter          — between a rail and GND on a regulated rail (post-regulator)
    - ac_coupling     — between two signal nets, blocks DC
    - tank            — near an oscillator or crystal
    - bypass          — general bypass on a rail (similar to decoupling, specific to
                         a voltage reference / analog block)

  passive_d (diodes):
    - flyback         — across an inductor in a buck/boost SMPS
    - rectifier       — between two power rails, converting direction
    - esd             — from a signal net to GND, protection
    - reverse_protection — in series with a power input, polarity protection
    - signal_clamp    — between a signal net and a rail (or GND), clamping

  passive_fb (ferrite beads):
    - filter          — the only canonical role; between a rail and a filtered
                         variant of it (e.g. +3V3 → +3V3_AUDIO)

Use the input context for each passive:
  - `refdes`      — identifier, kept verbatim
  - `type`        — physical kind (resistor/capacitor/diode/ferrite) — determines `kind`
  - `value`       — printed value if any (100nF, 4.7k, etc.)
  - `pins`        — pin numbers + net labels
  - `nearby_refdes` — refdes of components sharing one of this passive's nets
  - `nearby_notes` — designer notes attached to this passive or its nets

Rules:
  - Output `kind` matching the physical `type` (resistor → passive_r, etc.)
  - If the component type is anything else (ic/transistor/module), it should NOT
    be in the input — but if it appears, emit kind="ic" with role=null.
  - Use role=null only when nothing in the context narrows it. Lower confidence
    when doing so. Prefer a best-guess role with confidence 0.5-0.6 over null
    when ONE role is more plausible than others.
  - EVERY refdes from the input list MUST appear in the output `assignments`.
    Never skip.
  - Quote evidence in `rationale` when relevant — reference a specific net label,
    designer note, or neighbour refdes.
"""


def _format_passive_for_prompt(
    graph: ElectricalGraph, passive: ComponentNode,
) -> str:
    """Render one passive's context as a compact block."""
    value_str = ""
    if passive.value and passive.value.primary:
        value_str = f" value={passive.value.primary}"
        if passive.value.package:
            value_str += f" [{passive.value.package}]"
    pins_str = ", ".join(
        f"pin{p.number}={p.net_label}" if p.net_label else f"pin{p.number}=?"
        for p in passive.pins[:4]
    )
    # Nearby refdes: any IC or rail source sharing a net with this passive.
    my_nets = {p.net_label for p in passive.pins if p.net_label}
    nearby = set()
    for refdes, comp in graph.components.items():
        if refdes == passive.refdes or comp.kind != "ic":
            continue
        for pin in comp.pins:
            if pin.net_label in my_nets:
                nearby.add(refdes)
                break
        if len(nearby) >= 6:
            break
    nearby_str = ", ".join(sorted(nearby)) or "(none)"
    # Nearby notes: any designer note whose attached_to_refdes is us OR
    # attached_to_net is one of our nets.
    notes = []
    for note in graph.designer_notes:
        if note.attached_to_refdes == passive.refdes or note.attached_to_net in my_nets:
            notes.append(f"  - p{note.page}: {note.text[:160]}")
            if len(notes) >= 3:
                break
    notes_str = "\n".join(notes) or "  (none)"
    return (
        f"- {passive.refdes}  type={passive.type}{value_str}\n"
        f"  pins: {pins_str}\n"
        f"  nearby_refdes: {nearby_str}\n"
        f"  nearby_notes:\n{notes_str}"
    )


def _build_llm_context(
    graph: ElectricalGraph, refdes_list: list[str],
) -> str:
    """Assemble the user-content payload for one batch."""
    lines = [
        f"DEVICE: {graph.device_slug}",
        f"PASSIVES TO CLASSIFY ({len(refdes_list)} in this batch):",
    ]
    for refdes in refdes_list:
        comp = graph.components.get(refdes)
        if comp is None:
            continue
        lines.append(_format_passive_for_prompt(graph, comp))
    lines.append(
        f"\nEmit `assignments` for ALL {len(refdes_list)} passives above — "
        f"never skip. Use role=null only when truly undecidable."
    )
    return "\n".join(lines)


def _passive_tool_definition() -> dict:
    return {
        "name": SUBMIT_PASSIVE_TOOL_NAME,
        "description": (
            "Submit the passive classification. Every passive from the input list "
            "MUST appear in `assignments`. Never invent a passive that wasn't listed."
        ),
        "input_schema": PassiveClassification.model_json_schema(),
    }


async def _classify_passive_batch(
    graph: ElectricalGraph,
    refdes_list: list[str],
    *,
    client: AsyncAnthropic,
    model: str,
    batch_idx: int,
) -> PassiveClassification:
    """Single LLM call for one batch of passives."""
    return await call_with_forced_tool(
        client=client,
        model=model,
        system=_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": _build_llm_context(graph, refdes_list),
        }],
        tools=[_passive_tool_definition()],
        forced_tool_name=SUBMIT_PASSIVE_TOOL_NAME,
        output_schema=PassiveClassification,
        max_attempts=2,
        max_tokens=16000,
        log_label=f"passive_classifier({graph.device_slug})[batch{batch_idx}]",
    )


async def classify_passives_llm(
    graph: ElectricalGraph,
    *,
    client: AsyncAnthropic,
    model: str | None = None,
) -> dict[str, tuple[str, str | None, float]]:
    """Classify every passive in `graph` via Opus/Sonnet. Returns the same
    shape as `classify_passives_heuristic`: refdes → (kind, role, confidence).

    Strategy: take the heuristic baseline first. For passives the heuristic
    left as `role=None`, batch them into groups of ``_BATCH_SIZE`` and
    dispatch in parallel via ``asyncio.gather``. Merge the LLM output on
    top: heuristic wins where it has a role; LLM fills the holes.

    On any exception during the LLM fanout, logs a warning and returns
    the heuristic baseline alone — never raises on transient errors.
    """
    model = model or _DEFAULT_CLASSIFIER_MODEL
    heuristic = classify_passives_heuristic(graph)

    # Select the passives the heuristic couldn't role.
    unclassified = [
        refdes for refdes, (_kind, role, _conf) in heuristic.items() if role is None
    ]
    if not unclassified:
        logger.info(
            "passive_classifier(llm): no unclassified passives on slug=%s — heuristic covered %d/%d",
            graph.device_slug, len(heuristic), len(heuristic),
        )
        return heuristic

    batches = [
        unclassified[i : i + _BATCH_SIZE]
        for i in range(0, len(unclassified), _BATCH_SIZE)
    ]
    logger.info(
        "passive_classifier(llm) starting (model=%s slug=%s unclassified=%d batches=%d)",
        model, graph.device_slug, len(unclassified), len(batches),
    )

    try:
        tasks = [
            _classify_passive_batch(
                graph, batch, client=client, model=model, batch_idx=i,
            )
            for i, batch in enumerate(batches)
        ]
        partial = await asyncio.gather(*tasks)
    except Exception:
        logger.warning(
            "passive_classifier(llm) failed on slug=%s — returning heuristic baseline",
            graph.device_slug, exc_info=True,
        )
        return heuristic

    # Merge: heuristic wins where it has a role, LLM fills the rest.
    merged = dict(heuristic)
    filled = 0
    for part in partial:
        for a in part.assignments:
            current = merged.get(a.refdes)
            if current is None or current[1] is not None:
                continue
            if a.role is None:
                continue  # LLM also gave up on this one
            merged[a.refdes] = (current[0], a.role, a.confidence)
            filled += 1
    logger.info(
        "passive_classifier(llm) done (slug=%s filled=%d/%d unclassified passives)",
        graph.device_slug, filled, len(unclassified),
    )
    return merged


async def classify_passives(
    graph: ElectricalGraph,
    *,
    client: AsyncAnthropic | None = None,
    model: str | None = None,
) -> dict[str, tuple[str, str | None, float]]:
    """Public entry point — prefers LLM when a client is provided, falls
    back to heuristic otherwise or on LLM failure."""
    if client is None:
        return classify_passives_heuristic(graph)
    return await classify_passives_llm(graph, client=client, model=model)
