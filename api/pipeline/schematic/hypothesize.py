# SPDX-License-Identifier: Apache-2.0
"""Reverse-diagnostic hypothesis engine — inverse of the behavioral simulator.

Given a partial observation of the board (dead / alive components and rails,
four classes), enumerate refdes-kill candidates that explain the observation,
score them with an F1-style soft-penalty function, and return the top-N
ranked hypotheses with a structured diff + a deterministic French narrative.

Single-fault exhaustive + 2-fault pruned (seed from top-K single survivors,
pair only with components whose cascade intersects the residual unexplained
observations). Pure sync, no LLM, no IO — depends only on the existing
ElectricalGraph + SimulationEngine.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.pipeline.schematic.passive_classifier import _BAT_FAMILY_PATTERN
from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph
from api.pipeline.schematic.simulator import SimulationEngine

if TYPE_CHECKING:
    from api.pipeline.schematic.schemas import ComponentNode as _CompNode

CascadeFn = Callable[["ElectricalGraph", "_CompNode"], dict]

# ---------------------------------------------------------------------------
# Tunable constants — exported so tests and scripts can override without
# monkey-patching. `tune_hypothesize_weights.py` rewrites PENALTY_WEIGHTS
# based on benchmark accuracy.
# ---------------------------------------------------------------------------

PENALTY_WEIGHTS: tuple[int, int] = (10, 2)  # (fp_weight, fn_weight)
TOP_K_SINGLE: int = 20  # how many single-fault survivors seed 2-fault
MAX_RESULTS_DEFAULT: int = 5
TWO_FAULT_ENABLED: bool = True
MAX_PAIRS: int = 100  # 2-fault pair cap (safety net, rarely hit)

# ---------------------------------------------------------------------------
# Phase 4: visibility multiplier — dampens topologically weak passive cascades.
# Key is (kind, role, mode). Missing entries default to 1.0 (no dampening).
# Applied to `tp_comps` only; FP/FN weights are unchanged.
# ---------------------------------------------------------------------------

_SCORE_VISIBILITY: dict[tuple[str, str, str], float] = {
    ("passive_c", "decoupling", "open"): 0.5,
    ("passive_c", "bulk", "open"): 0.5,
    ("passive_c", "filter", "open"): 0.5,
    ("passive_r", "pull_up", "open"): 0.5,
    ("passive_r", "pull_down", "open"): 0.5,
    # shorts are visible at rail level → no multiplier.
}

# ---------------------------------------------------------------------------
# Mode vocabulary — imported by tools, HTTP, tests, UI JSON.
# ---------------------------------------------------------------------------

ComponentMode = Literal[
    "dead",
    "alive",
    "anomalous",
    "hot",
    "open",
    "short",
    "stuck_on",
    "stuck_off",
]
RailMode = Literal[
    "dead",
    "alive",
    "shorted",  # to GND OR overvolt (Phase 1 semantics)
    "stuck_on",  # Phase 4.5 — rail alive when it should be off
]

# Failure modes that can be attributed to a component as the root-cause kill.
# `alive` omitted (a live component is not a failure). `shorted` is rail-side
# but produced by a component that shorts its input rail to GND. `open` /
# `short` are passive Phase 4 modes. `stuck_on` / `stuck_off` are Phase 4.5 Q
# modes — stuck_on = conducts permanently (rail stays on), stuck_off =
# never conducts (rail stays off).
FailureMode = Literal[
    "dead",
    "anomalous",
    "hot",
    "shorted",
    "open",
    "short",
    "stuck_on",
    "stuck_off",
]

_IC_MODES: frozenset[str] = frozenset({"dead", "alive", "anomalous", "hot"})
_PASSIVE_MODES: frozenset[str] = frozenset({"open", "short", "alive", "stuck_on", "stuck_off"})


class ObservedMetric(BaseModel):
    """Numeric measurement attached to an observation. Optional in Phase 1 —
    stored for UI and FR narrative enrichment, not used by the discrete
    scoring (deferred to Phase 5)."""

    model_config = ConfigDict(extra="forbid")

    measured: float
    unit: Literal["V", "A", "W", "°C", "Ω", "mV"]
    nominal: float | None = None
    tolerance_percent: float = 10.0


class Observations(BaseModel):
    """Structured per-target observation map (schema B).

    Each refdes / rail label maps to exactly one mode. Numeric metrics
    parallel the state dicts and carry the raw measurements the tech
    probed, used for FR narrative and UI timeline — NOT for scoring.
    """

    model_config = ConfigDict(extra="forbid")

    state_comps: dict[str, ComponentMode] = Field(default_factory=dict)
    state_rails: dict[str, RailMode] = Field(default_factory=dict)
    metrics_comps: dict[str, ObservedMetric] = Field(default_factory=dict)
    metrics_rails: dict[str, ObservedMetric] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_cross_bucket_alias(self):
        overlap = set(self.state_comps) & set(self.state_rails)
        if overlap:
            raise ValueError(f"target appears as both component and rail: {sorted(overlap)}")
        return self

    def is_empty(self) -> bool:
        return not (self.state_comps or self.state_rails)


class HypothesisMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tp_comps: int
    tp_rails: int
    fp_comps: int
    fp_rails: int
    fn_comps: int
    fn_rails: int


class HypothesisDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # (target, observed_mode, predicted_mode)
    contradictions: list[tuple[str, str, str]] = Field(default_factory=list)
    # targets observed non-alive but the hypothesis leaves them alive
    under_explained: list[str] = Field(default_factory=list)
    # (target, predicted_mode) pairs not in any observation
    over_predicted: list[tuple[str, str]] = Field(default_factory=list)


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # parallel lists — kill_refdes[i] fails in mode kill_modes[i]
    kill_refdes: list[str]
    kill_modes: list[FailureMode]
    score: float
    metrics: HypothesisMetrics
    diff: HypothesisDiff
    narrative: str
    cascade_preview: (
        dict  # {dead_rails, shorted_rails, dead_comps_count, anomalous_count, hot_count}
    )


class PruningStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    single_candidates_tested: int
    two_fault_pairs_tested: int
    wall_ms: float


class HypothesizeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_slug: str
    observations_echo: Observations
    hypotheses: list[Hypothesis]
    pruning: PruningStats
    # Phase 4.2 — when the top-N hypotheses tie at the same score, these
    # are targets whose measurement would best partition the candidate
    # set. Empty when scores are well-separated or there's only one
    # hypothesis. Callers (UI, agent) can suggest "mesure X ou Y pour
    # trancher".
    discriminating_targets: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Forward simulation — mode-aware dispatcher
# ---------------------------------------------------------------------------


def _empty_cascade() -> dict:
    return {
        "dead_comps": frozenset(),
        "dead_rails": frozenset(),
        "shorted_rails": frozenset(),
        "always_on_rails": frozenset(),  # Phase 4.5 — Q stuck_on cascades
        "anomalous_comps": frozenset(),
        "hot_comps": frozenset(),
        "degraded_rails": frozenset(),  # Phase 4.7 — continuous (leaky/regulating_low) modes
        "final_verdict": "",
        "blocked_at_phase": None,
    }


def _simulate_dead(
    electrical: ElectricalGraph,
    analyzed_boot: AnalyzedBootSequence | None,
    killed: list[str],
) -> dict:
    """Forward cascade when one or more refdes are fully dead (power-off)."""
    tl = SimulationEngine(
        electrical,
        analyzed_boot=analyzed_boot,
        killed_refdes=killed,
    ).run()
    c = _empty_cascade()
    c["dead_comps"] = frozenset(set(tl.cascade_dead_components) | set(killed))
    c["dead_rails"] = frozenset(tl.cascade_dead_rails)
    c["final_verdict"] = tl.final_verdict
    c["blocked_at_phase"] = tl.blocked_at_phase
    return c


SIGNAL_EDGE_KINDS: frozenset[str] = frozenset(
    {"produces_signal", "consumes_signal", "clocks", "depends_on"}
)


def _propagate_signal_downstream(
    electrical: ElectricalGraph,
    origin_refdes: str,
) -> set[str]:
    """BFS downstream on signal-typed edges, returning reachable REFDES.

    Uses an intermediate net layer: a refdes produces a signal onto a net;
    the net's consumers (refdes that consume that signal) become anomalous.
    The allow-set (`SIGNAL_EDGE_KINDS`) intentionally excludes `powered_by`,
    `enables`, `decouples`, `filters`, and `feedback_in` — those represent
    power topology or decoupling passives, both out of scope for anomalous
    propagation.
    """
    # Build a net → consumers map once (refdes that consume a signal on a net).
    net_consumers: dict[str, set[str]] = {}
    # Build a refdes → produced nets map (signals the refdes drives).
    produces_by: dict[str, set[str]] = {}
    for edge in electrical.typed_edges:
        if edge.kind not in SIGNAL_EDGE_KINDS:
            continue
        if edge.kind in ("consumes_signal", "depends_on"):
            # refdes consumes a signal on net `dst`
            net_consumers.setdefault(edge.dst, set()).add(edge.src)
        elif edge.kind in ("produces_signal", "clocks"):
            produces_by.setdefault(edge.src, set()).add(edge.dst)

    # BFS: starting from origin's produced signals, fan out via consumers.
    reached: set[str] = set()
    frontier: list[str] = sorted(produces_by.get(origin_refdes, set()))
    while frontier:
        net = frontier.pop()
        for consumer in sorted(net_consumers.get(net, set())):
            if consumer == origin_refdes or consumer in reached:
                continue
            reached.add(consumer)
            # Chain: the consumer may produce further signals downstream.
            for next_net in sorted(produces_by.get(consumer, set())):
                if next_net not in frontier:
                    frontier.append(next_net)
    return reached


def _find_powered_rail(
    electrical: ElectricalGraph,
    refdes: str,
) -> str | None:
    """Return the (first) rail label whose consumers list contains `refdes`."""
    for label, rail in electrical.power_rails.items():
        if refdes in (rail.consumers or []):
            return label
    return None


def _simulate_failure(
    electrical: ElectricalGraph,
    analyzed_boot: AnalyzedBootSequence | None,
    refdes: str,
    mode: str,
) -> dict:
    """Run the forward cascade of a single failed (refdes, mode) pair.

    Dispatches by mode. `anomalous`, `hot`, `shorted` are implemented in
    Tasks 3-5. Phase 2+ modes should extend this dispatcher.

    Cross-scenario memoization: the cascade is a pure function of
    (graph, analyzed_boot, refdes, mode) and the returned dict is treated
    as immutable by every caller (score / narrate / preview / two-fault
    union all read-only). We cache on the per-graph memo so that a bench
    running N scenarios against the same pack pays the simulation cost
    once instead of N times.
    """
    memo = _memo_for(electrical)
    key = (id(analyzed_boot), refdes, mode)
    cached = memo.cascades.get(key)
    if cached is not None:
        return cached
    result = _simulate_failure_uncached(electrical, analyzed_boot, refdes, mode)
    memo.cascades[key] = result
    return result


def _simulate_failure_uncached(
    electrical: ElectricalGraph,
    analyzed_boot: AnalyzedBootSequence | None,
    refdes: str,
    mode: str,
) -> dict:
    if mode == "dead":
        return _simulate_dead(electrical, analyzed_boot, [refdes])
    if mode == "anomalous":
        downstream = _propagate_signal_downstream(electrical, refdes)
        c = _empty_cascade()
        c["anomalous_comps"] = frozenset({refdes} | downstream)
        return c
    if mode == "hot":
        c = _empty_cascade()
        c["hot_comps"] = frozenset({refdes})
        return c
    if mode == "shorted":
        rail = _find_powered_rail(electrical, refdes)
        if rail is None:
            c = _empty_cascade()
            c["dead_comps"] = frozenset({refdes})
            return c
        source = electrical.power_rails[rail].source_refdes
        downstream = (
            _simulate_dead(electrical, analyzed_boot, [source]) if source else _empty_cascade()
        )
        # SimulationEngine now handles transitive rail death internally — no
        # second-pass patch needed.
        c = _empty_cascade()
        c["shorted_rails"] = frozenset({rail})
        c["dead_rails"] = downstream["dead_rails"] - {rail}
        c["dead_comps"] = downstream["dead_comps"]
        c["hot_comps"] = frozenset({source}) if source else frozenset()
        c["final_verdict"] = downstream["final_verdict"]
        c["blocked_at_phase"] = downstream["blocked_at_phase"]
        return c
    if mode == "regulating_low":
        sourced = [
            label for label, rail in electrical.power_rails.items() if rail.source_refdes == refdes
        ]
        c = _empty_cascade()
        c["degraded_rails"] = frozenset(sourced)
        return c
    # Phase 4 passive + Phase 4.5 Q modes + Phase 4.7 continuous (leaky_short).
    if mode in {"open", "short", "stuck_on", "stuck_off", "leaky_short"}:
        comp = electrical.components.get(refdes)
        if comp is None:
            return _empty_cascade()
        kind = getattr(comp, "kind", "ic")
        role = getattr(comp, "role", None)
        if kind == "ic" or role is None:
            return _empty_cascade()
        handler = _PASSIVE_CASCADE_TABLE.get((kind, role, mode))
        if handler is None:
            return _empty_cascade()
        return handler(electrical, comp)
    raise ValueError(f"unknown failure mode: {mode!r}")


# ---------------------------------------------------------------------------
# Scoring — mode-aware F1-style soft-penalty function
# ---------------------------------------------------------------------------


def _score_candidate(
    cascade: dict,
    obs: Observations,
    *,
    tp_mult: float = 1.0,
) -> tuple[float, HypothesisMetrics, HypothesisDiff]:
    """Score a candidate cascade against observations.

    Works off the 5-bucket cascade returned by _simulate_failure. Unlike
    the v1 engine this one matches PER MODE:

    - Each observation target has an expected mode.
    - Each cascade bucket implies a predicted mode for some refdes/rail.
    - TP = same mode observed AND predicted.
    - FP = predicted non-alive but observed alive OR mode mismatch between
           two non-alive modes.
    - FN = observed non-alive but predicted alive (target not in any cascade
           bucket).
    - Over-predicted = predicted non-alive but no observation exists.
    """
    fp_w, fn_w = PENALTY_WEIGHTS

    # Build per-target predicted mode maps.
    predicted_comps: dict[str, str] = {}
    for r in cascade["dead_comps"]:
        predicted_comps[r] = "dead"
    for r in cascade["anomalous_comps"]:
        predicted_comps[r] = "anomalous"
    for r in cascade["hot_comps"]:
        # hot wins over anomalous if both (unusual, keep for safety)
        predicted_comps[r] = "hot"
    predicted_rails: dict[str, str] = {}
    for rail in cascade["dead_rails"]:
        predicted_rails[rail] = "dead"
    for rail in cascade["shorted_rails"]:
        predicted_rails[rail] = "shorted"  # shorted wins over dead
    for rail in cascade["always_on_rails"]:
        predicted_rails[rail] = "stuck_on"  # Phase 4.5 — disjoint from shorted

    contradictions: list[tuple[str, str, str]] = []
    under_explained: list[str] = []
    tp_c = fp_c = fn_c = 0
    tp_r = fp_r = fn_r = 0

    # Components
    for refdes, obs_mode in obs.state_comps.items():
        pred_mode = predicted_comps.get(refdes, "alive")
        if pred_mode == obs_mode:
            tp_c += 1
        elif obs_mode == "alive" and pred_mode != "alive":
            fp_c += 1
            contradictions.append((refdes, obs_mode, pred_mode))
        elif obs_mode != "alive" and pred_mode == "alive":
            fn_c += 1
            under_explained.append(refdes)
        else:
            # Both non-alive, different modes — soft mismatch counted as FP.
            fp_c += 1
            contradictions.append((refdes, obs_mode, pred_mode))

    # Rails
    for rail, obs_mode in obs.state_rails.items():
        pred_mode = predicted_rails.get(rail, "alive")
        if pred_mode == obs_mode:
            tp_r += 1
        elif obs_mode == "alive" and pred_mode != "alive":
            fp_r += 1
            contradictions.append((rail, obs_mode, pred_mode))
        elif obs_mode != "alive" and pred_mode == "alive":
            fn_r += 1
            under_explained.append(rail)
        else:
            fp_r += 1
            contradictions.append((rail, obs_mode, pred_mode))

    # Over-predicted: non-alive predicted for targets not in any observation.
    observed_keys = set(obs.state_comps) | set(obs.state_rails)
    over_predicted: list[tuple[str, str]] = []
    for refdes, mode in predicted_comps.items():
        if refdes not in observed_keys:
            over_predicted.append((refdes, mode))
    for rail, mode in predicted_rails.items():
        if rail not in observed_keys:
            over_predicted.append((rail, mode))
    over_predicted.sort()

    metrics = HypothesisMetrics(
        tp_comps=tp_c,
        tp_rails=tp_r,
        fp_comps=fp_c,
        fp_rails=fp_r,
        fn_comps=fn_c,
        fn_rails=fn_r,
    )
    tp = (tp_c * tp_mult) + tp_r
    fp = fp_c + fp_r
    fn = fn_c + fn_r
    score = float(tp - fp_w * fp - fn_w * fn)
    diff = HypothesisDiff(
        contradictions=sorted(contradictions),
        under_explained=sorted(under_explained),
        over_predicted=over_predicted,
    )
    return score, metrics, diff


# ---------------------------------------------------------------------------
# Phase 4: passive cascade dispatch
# ---------------------------------------------------------------------------


# Phase 4.6 — suffixes that unambiguously mark the protected-side rail
# on a cell_protection Q. BMS nomenclature varies across vendors; this
# covers MNT Reform (BAT1FUSED) and common alternatives.
_CELL_PROT_DOWNSTREAM_SUFFIXES = ("FUSED", "PROT", "OUT", "PACK")


def _find_cell_protection_downstream(
    electrical: ElectricalGraph,
    q: _CompNode,
) -> str | None:
    """Return the protected-side BAT-family rail for a cell_protection Q.

    Heuristic, in priority order:

    1. Collect the Q's BAT-family rail pins (registered in
       `electrical.power_rails`).
    2. If fewer than two distinct rails: None — insufficient topology.
    3. If exactly one of them carries a `FUSED|PROT|OUT|PACK` suffix:
       return that one — asymmetric naming unambiguously marks the
       protected side.
    4. Fallback to `_find_downstream_rail` (source_refdes / consumer-
       count heuristic).

    Uses `_BAT_FAMILY_PATTERN` from `passive_classifier`.
    """
    pin_rails = [
        p.net_label for p in q.pins if p.net_label and p.net_label in electrical.power_rails
    ]
    bat_rails = sorted({r for r in pin_rails if _BAT_FAMILY_PATTERN.match(r)})
    if len(bat_rails) < 2:
        return None
    suffixed = [r for r in bat_rails if any(r.endswith(s) for s in _CELL_PROT_DOWNSTREAM_SUFFIXES)]
    if len(suffixed) == 1:
        return suffixed[0]
    return _find_downstream_rail(electrical, q)


def _find_downstream_rail(
    electrical: ElectricalGraph,
    passive: _CompNode,
) -> str | None:
    """Return the rail sourced on one side of a series passive (R/FB/D/C).

    Heuristic: both pin nets must be power rails. The "downstream" rail
    is the one with a consumer list (fed by nothing else) — the other is
    the upstream source. Ambiguous returns None.
    """
    nets = [p.net_label for p in passive.pins if p.net_label]
    if len(nets) < 2:
        return None
    rail_labels = [n for n in nets if n in electrical.power_rails]
    if len(rail_labels) < 2:
        return None
    # Primary — rail whose source IS this passive (compiler marks the
    # passive as `source_refdes` on the downstream rail when it sees a
    # `powers` edge from the passive). Unambiguous.
    for label in rail_labels:
        if electrical.power_rails[label].source_refdes == passive.refdes:
            return label
    # Secondary — rail with source_refdes null (passive-driven, source
    # not annotated by the compiler).
    candidates = [
        label for label in rail_labels if electrical.power_rails[label].source_refdes is None
    ]
    if len(candidates) == 1:
        return candidates[0]
    # Tertiary — pick the rail with FEWER consumers. The downstream
    # rail of a series passive is typically more specific (fewer
    # downstream loads) than the upstream bus rail.
    rail_labels.sort(
        key=lambda r: len(electrical.power_rails[r].consumers or []),
    )
    return rail_labels[0]


def _find_decoupled_rail(
    electrical: ElectricalGraph,
    passive: _CompNode,
) -> str | None:
    """A decoupling cap has one pin on a rail and one on GND. Return the rail.

    Fallback: when the cap's own pins don't reference any known rail (typical
    for SoC-internal rails like VDD_GPU sourced inside the package — the
    schematic surface-net is named differently from the rail label), look up
    membership in `rail.decoupling` lists set by the classifier. The
    classifier sees evidence the human reader sees (placement near IC,
    annotated decoupling group) that the pin-net string alone misses.
    """
    nets = [p.net_label for p in passive.pins if p.net_label]
    for n in nets:
        if n in electrical.power_rails:
            return n
    for label, rail in electrical.power_rails.items():
        if passive.refdes in (rail.decoupling or []):
            return label
    return None


def _find_decoupled_ic(
    electrical: ElectricalGraph,
    passive: _CompNode,
) -> str | None:
    """The IC most likely decoupled by this cap — explicit `decouples` edge
    target, or the first consumer IC on the decoupled rail."""
    for edge in electrical.typed_edges:
        if edge.kind == "decouples" and edge.src == passive.refdes:
            if edge.dst in electrical.components:
                return edge.dst
        if edge.kind == "decouples" and edge.dst == passive.refdes:
            if edge.src in electrical.components:
                return edge.src
    rail = _find_decoupled_rail(electrical, passive)
    if rail is None:
        return None
    consumers = electrical.power_rails[rail].consumers or []
    return consumers[0] if consumers else None


def _find_regulated_rail_of_feedback(
    electrical: ElectricalGraph,
    passive: _CompNode,
) -> str | None:
    """Walk a `feedback_in` edge from the divider's signal pin back to the
    regulator that drives the rail being regulated."""
    # Find the non-GND, non-rail net — that's the feedback signal net.
    fb_net: str | None = None
    for pin in passive.pins:
        n = pin.net_label
        if not n:
            continue
        if n in electrical.power_rails:
            continue
        up = n.upper()
        if up in {"GND", "AGND", "DGND", "PGND"}:
            continue
        fb_net = n
        break
    if fb_net is None:
        return None
    # Find the IC with a pin named `feedback_in` on `fb_net`; then find
    # its power_out rail.
    for ic in electrical.components.values():
        if ic.kind != "ic":
            continue
        has_fb = any(p.role == "feedback_in" and p.net_label == fb_net for p in ic.pins)
        if not has_fb:
            continue
        for p in ic.pins:
            if p.role == "power_out" and p.net_label in electrical.power_rails:
                return p.net_label
    return None


def _simulate_rail_loss(
    electrical: ElectricalGraph,
    rail_label: str,
) -> dict:
    """Mark a rail dead and propagate through SimulationEngine by killing
    its source. If the rail has no source (passive-driven rail), fall
    back to a local cascade: the rail + every consumer of it dead."""
    rail = electrical.power_rails.get(rail_label)
    if rail is None:
        return _empty_cascade()
    if rail.source_refdes:
        return _simulate_dead(electrical, None, [rail.source_refdes])
    # Passive-driven rail — no upstream IC to kill. Build the cascade
    # directly.
    c = _empty_cascade()
    c["dead_rails"] = frozenset({rail_label})
    c["dead_comps"] = frozenset(rail.consumers or [])
    return c


# --- Cascade handlers (one per (kind, role, mode) family) ---


def _cascade_passive_alive(electrical: ElectricalGraph, passive: _CompNode) -> dict:
    """Physically plausible but no observable cascade. Empty → pruned."""
    return _empty_cascade()


def _cascade_series_open(electrical: ElectricalGraph, passive: _CompNode) -> dict:
    downstream = _find_downstream_rail(electrical, passive)
    if downstream is None:
        return _empty_cascade()
    return _simulate_rail_loss(electrical, downstream)


def _cascade_filter_open(electrical: ElectricalGraph, passive: _CompNode) -> dict:
    # FB filter open is functionally identical to a series element open.
    return _cascade_series_open(electrical, passive)


def _cascade_decoupling_open(electrical: ElectricalGraph, passive: _CompNode) -> dict:
    ic = _find_decoupled_ic(electrical, passive)
    if ic is None:
        return _empty_cascade()
    c = _empty_cascade()
    c["anomalous_comps"] = frozenset({ic})
    return c


def _cascade_decoupling_short(electrical: ElectricalGraph, passive: _CompNode) -> dict:
    rail = _find_decoupled_rail(electrical, passive)
    if rail is None:
        return _empty_cascade()
    source = electrical.power_rails[rail].source_refdes
    downstream = _simulate_dead(electrical, None, [source]) if source else _empty_cascade()
    c = _empty_cascade()
    c["shorted_rails"] = frozenset({rail})
    c["dead_rails"] = downstream["dead_rails"] - {rail}
    c["dead_comps"] = downstream["dead_comps"]
    c["hot_comps"] = frozenset({source}) if source else frozenset()
    return c


def _cascade_decoupling_leaky(electrical: ElectricalGraph, comp: _CompNode) -> dict:
    """passive_c.leaky_short on decoupling/bulk cap — decoupled rail collapses
    toward GND. Encoded as `shorted_rails` (not `degraded_rails`) so it's
    observable through the same axis a tech reports: 'rail PP1V8 shorted'.
    The simulator marks the rail `degraded` because it models a finite ESR
    leak, but for the diagnostic round-trip the symptom is rail-low/short.
    """
    target_rail: str | None = None
    for label, rail in electrical.power_rails.items():
        if comp.refdes in rail.decoupling:
            target_rail = label
            break
    c = _empty_cascade()
    if target_rail is not None:
        c["shorted_rails"] = frozenset({target_rail})
    return c


def _cascade_feedback_open_overvolt(electrical: ElectricalGraph, passive: _CompNode) -> dict:
    rail = _find_regulated_rail_of_feedback(electrical, passive)
    if rail is None:
        return _empty_cascade()
    c = _empty_cascade()
    c["shorted_rails"] = frozenset({rail})  # Phase 1 encoding for overvoltage
    consumers = electrical.power_rails[rail].consumers or []
    c["anomalous_comps"] = frozenset(consumers)
    return c


def _cascade_feedback_short_undervolt(electrical: ElectricalGraph, passive: _CompNode) -> dict:
    """R feedback short → divider collapses → regulator shuts output → rail dead."""
    rail = _find_regulated_rail_of_feedback(electrical, passive)
    if rail is None:
        return _empty_cascade()
    return _simulate_rail_loss(electrical, rail)


def _cascade_pull_up_open(electrical: ElectricalGraph, passive: _CompNode) -> dict:
    """Pull-up/pull-down open → signal floats → consumers anomalous."""
    # Identify the signal net (the non-rail, non-GND pin).
    sig_net: str | None = None
    for pin in passive.pins:
        n = pin.net_label
        if not n or n in electrical.power_rails:
            continue
        up = n.upper()
        if up in {"GND", "AGND", "DGND", "PGND"} or up.startswith("GND_"):
            continue
        sig_net = n
        break
    if sig_net is None:
        return _empty_cascade()
    anomalous: set[str] = set()
    for edge in electrical.typed_edges:
        if edge.kind in {"consumes_signal", "depends_on"} and edge.dst == sig_net:
            if edge.src in electrical.components:
                anomalous.add(edge.src)
    c = _empty_cascade()
    c["anomalous_comps"] = frozenset(anomalous)
    return c


def _cascade_pull_up_short(electrical: ElectricalGraph, passive: _CompNode) -> dict:
    """Pull-up short → rail shorted to signal (or bus stuck) → rail dead.
    Using rail-loss primitive on the rail-side pin."""
    for pin in passive.pins:
        n = pin.net_label
        if n and n in electrical.power_rails:
            return _simulate_rail_loss(electrical, n)
    return _empty_cascade()


def _cascade_filter_cap_open(electrical: ElectricalGraph, passive: _CompNode) -> dict:
    """Filter cap open on a regulated rail → ripple → upstream IC anomalous.
    Same topological signature as decoupling_open."""
    return _cascade_decoupling_open(electrical, passive)


def _cascade_signal_path_open(electrical: ElectricalGraph, passive: _CompNode) -> dict:
    """AC-coupling cap open → signal broken downstream of the cap → consumers
    of the output net anomalous."""
    nets = [p.net_label for p in passive.pins if p.net_label]
    if len(nets) < 2:
        return _empty_cascade()
    # Pick the net with the most downstream consumers as the "output" side.
    consumer_counts = {
        n: sum(
            1
            for e in electrical.typed_edges
            if e.kind in {"consumes_signal", "depends_on"} and e.dst == n
        )
        for n in nets
    }
    output = max(nets, key=lambda n: consumer_counts[n])
    c = _empty_cascade()
    consumers: set[str] = set()
    for e in electrical.typed_edges:
        if e.kind in {"consumes_signal", "depends_on"} and e.dst == output:
            if e.src in electrical.components:
                consumers.add(e.src)
    c["anomalous_comps"] = frozenset(consumers)
    return c


def _cascade_signal_path_dc(electrical: ElectricalGraph, passive: _CompNode) -> dict:
    """AC-coupling cap short → DC offset propagates → downstream anomalous."""
    return _cascade_signal_path_open(electrical, passive)


def _cascade_tank_open(electrical: ElectricalGraph, passive: _CompNode) -> dict:
    """Tank cap open near oscillator → clock dead → clock consumers anomalous.
    Tank has 1 pin on GND, 1 on the oscillator output. Treat oscillator as
    anomalous."""
    for pin in passive.pins:
        n = pin.net_label
        if not n:
            continue
        # Find an IC (oscillator) with clock_out on this net.
        for ic in electrical.components.values():
            if ic.kind != "ic":
                continue
            for p in ic.pins:
                if p.role == "clock_out" and p.net_label == n:
                    c = _empty_cascade()
                    c["anomalous_comps"] = frozenset({ic.refdes})
                    return c
    return _empty_cascade()


def _cascade_tank_short(electrical: ElectricalGraph, passive: _CompNode) -> dict:
    """Tank cap short → oscillator dead."""
    # Same lookup as tank_open but tag oscillator dead instead of anomalous.
    for pin in passive.pins:
        n = pin.net_label
        if not n:
            continue
        for ic in electrical.components.values():
            if ic.kind != "ic":
                continue
            for p in ic.pins:
                if p.role == "clock_out" and p.net_label == n:
                    return _simulate_dead(electrical, None, [ic.refdes])
    return _empty_cascade()


def _cascade_rectifier_short(electrical: ElectricalGraph, passive) -> dict:
    """Shorted rectifier → its upstream rail shorted (input pulled to output)."""
    nets = [p.net_label for p in passive.pins if p.net_label]
    rails = [n for n in nets if n in electrical.power_rails]
    if not rails:
        return _empty_cascade()
    # Pick the input-side rail — heuristic: the one with a source_refdes.
    rails_with_source = [r for r in rails if electrical.power_rails[r].source_refdes is not None]
    target = rails_with_source[0] if rails_with_source else rails[0]
    source = electrical.power_rails[target].source_refdes
    downstream = _simulate_dead(electrical, None, [source]) if source else _empty_cascade()
    c = _empty_cascade()
    c["shorted_rails"] = frozenset({target})
    c["dead_rails"] = downstream["dead_rails"] - {target}
    c["dead_comps"] = downstream["dead_comps"]
    c["hot_comps"] = frozenset({source}) if source else frozenset()
    return c


def _cascade_rectifier_open(electrical: ElectricalGraph, passive) -> dict:
    """Open rectifier → output rail dead."""
    nets = [p.net_label for p in passive.pins if p.net_label]
    rails = [n for n in nets if n in electrical.power_rails]
    if not rails:
        return _empty_cascade()
    # Pick the output-side rail (no source_refdes — the diode is the source).
    rails_without_source = [r for r in rails if electrical.power_rails[r].source_refdes is None]
    target = rails_without_source[0] if rails_without_source else rails[0]
    return _simulate_rail_loss(electrical, target)


def _cascade_flyback_open(electrical: ElectricalGraph, passive) -> dict:
    """Flyback diode open → inductor kickback damages downstream → anomalous."""
    nets = set(p.net_label for p in passive.pins if p.net_label)
    consumers: set[str] = set()
    for ic in electrical.components.values():
        if ic.kind != "ic":
            continue
        for p in ic.pins:
            if p.role == "power_in" and p.net_label in nets:
                consumers.add(ic.refdes)
    c = _empty_cascade()
    c["anomalous_comps"] = frozenset(consumers)
    return c


def _cascade_flyback_short(electrical: ElectricalGraph, passive) -> dict:
    """Flyback short → continuous current path → source hot + rail shorted."""
    nets = [p.net_label for p in passive.pins if p.net_label]
    rails = [n for n in nets if n in electrical.power_rails]
    if not rails:
        return _empty_cascade()
    target = rails[0]
    source = electrical.power_rails[target].source_refdes
    downstream = _simulate_dead(electrical, None, [source]) if source else _empty_cascade()
    c = _empty_cascade()
    c["shorted_rails"] = frozenset({target})
    c["dead_rails"] = downstream["dead_rails"] - {target}
    c["dead_comps"] = downstream["dead_comps"]
    c["hot_comps"] = frozenset({source}) if source else frozenset()
    return c


def _cascade_signal_to_ground(electrical: ElectricalGraph, passive) -> dict:
    """ESD clamp short / signal clamp short → signal stuck → consumers anomalous.
    Uses the signal-net side (non-GND pin)."""
    sig_net: str | None = None
    for pin in passive.pins:
        n = pin.net_label
        if not n:
            continue
        if n in electrical.power_rails:
            continue
        up = n.upper()
        if up in {"GND", "AGND", "DGND", "PGND"} or up.startswith("GND_"):
            continue
        sig_net = n
        break
    if sig_net is None:
        return _empty_cascade()
    consumers: set[str] = set()
    for edge in electrical.typed_edges:
        if edge.kind in {"consumes_signal", "depends_on"} and edge.dst == sig_net:
            if edge.src in electrical.components:
                consumers.add(edge.src)
    c = _empty_cascade()
    c["anomalous_comps"] = frozenset(consumers)
    return c


# ---------------------------------------------------------------------------
# Phase 4.5 — Q transistor cascade handlers
# ---------------------------------------------------------------------------


def _is_ground_net_label(label: str | None) -> bool:
    if not label:
        return False
    up = label.upper()
    return up in {"GND", "AGND", "DGND", "PGND"} or up.startswith("GND_")


def _cascade_q_load_dead(electrical: ElectricalGraph, q) -> dict:
    """Load switch open or stuck_off → downstream rail dead + consumers dead."""
    downstream = _find_downstream_rail(electrical, q)
    if downstream is None:
        return _empty_cascade()
    return _simulate_rail_loss(electrical, downstream)


def _cascade_q_load_stuck_on(electrical: ElectricalGraph, q) -> dict:
    """Load switch short / stuck_on → downstream rail permanently on.
    Consumers become anomalous (active when they should be off in standby)."""
    downstream = _find_downstream_rail(electrical, q)
    if downstream is None:
        return _empty_cascade()
    c = _empty_cascade()
    c["always_on_rails"] = frozenset({downstream})
    consumers = electrical.power_rails[downstream].consumers or []
    # Consumers are anomalous: they're being powered when the sequencer
    # expected them off. Exclude the Q itself if it appears in consumers.
    c["anomalous_comps"] = frozenset(r for r in consumers if r != q.refdes)
    return c


def _cascade_q_shifter_signal_broken(
    electrical: ElectricalGraph,
    q,
) -> dict:
    """Level shifter open / stuck_off → signal not propagating → consumers
    anomalous. Treats both signal nets as potentially affected."""
    nets = [p.net_label for p in q.pins if p.net_label]
    sig_nets = [n for n in nets if n not in electrical.power_rails and not _is_ground_net_label(n)]
    anomalous: set[str] = set()
    for edge in electrical.typed_edges:
        if edge.kind in {"consumes_signal", "depends_on"} and edge.dst in sig_nets:
            if edge.src in electrical.components:
                anomalous.add(edge.src)
    c = _empty_cascade()
    c["anomalous_comps"] = frozenset(anomalous)
    return c


def _cascade_q_shifter_signal_stuck(
    electrical: ElectricalGraph,
    q,
) -> dict:
    """Level shifter short / stuck_on → signal stuck at one rail level →
    consumers anomalous. Cascade topologically identical to _broken; the
    distinction is in the narrative/mode, not the cascade bucket."""
    return _cascade_q_shifter_signal_broken(electrical, q)


def _cascade_q_inrush_rail_dead(
    electrical: ElectricalGraph,
    q,
) -> dict:
    """Inrush limiter open / stuck_off → downstream regulator never powers up."""
    return _cascade_q_load_dead(electrical, q)


def _cascade_q_flyback_switch_dead(
    electrical: ElectricalGraph,
    q,
) -> dict:
    """Flyback switch open / stuck_off → SMPS doesn't switch → output rail
    dead. Finds the output rail by inspecting the inductor that spans the
    Q's SW node; the inductor's other pin is the output rail."""
    nets = [p.net_label for p in q.pins if p.net_label]
    # Identify the SW net — where we also find an inductor pin.
    for sw_net in nets:
        for other in electrical.components.values():
            if other.refdes == q.refdes or other.type != "inductor":
                continue
            other_nets = [p.net_label for p in other.pins if p.net_label]
            if sw_net not in other_nets:
                continue
            # The inductor's OTHER pin is the output rail (or a net that
            # might be promoted to rail).
            for out in other_nets:
                if out == sw_net:
                    continue
                if out in electrical.power_rails:
                    return _simulate_rail_loss(electrical, out)
    # Fallback — if no inductor found, mark any downstream rail dead.
    downstream = _find_downstream_rail(electrical, q)
    if downstream is not None:
        return _simulate_rail_loss(electrical, downstream)
    return _empty_cascade()


def _cascade_q_flyback_switch_short(
    electrical: ElectricalGraph,
    q,
) -> dict:
    """Flyback D-S short / stuck_on → continuous current through inductor →
    input rail (PVIN / VIN) stressed, source IC hot, downstream of source
    dead."""
    nets = [p.net_label for p in q.pins if p.net_label]
    # Find the input rail (PVIN / VIN / +12V / BAT — the SOURCE-side rail).
    input_rail: str | None = None
    for n in nets:
        if n not in electrical.power_rails:
            continue
        up = n.upper()
        if any(tok in up for tok in ("VIN", "PVIN", "+12V", "BAT")):
            input_rail = n
            break
    if input_rail is None:
        # Fall back to any rail pin as the source.
        for n in nets:
            if n in electrical.power_rails:
                input_rail = n
                break
    if input_rail is None:
        return _empty_cascade()
    source = electrical.power_rails[input_rail].source_refdes
    downstream = _simulate_dead(electrical, None, [source]) if source else _empty_cascade()
    c = _empty_cascade()
    c["shorted_rails"] = frozenset({input_rail})
    c["dead_rails"] = downstream["dead_rails"] - {input_rail}
    c["dead_comps"] = downstream["dead_comps"]
    c["hot_comps"] = frozenset({source}) if source else frozenset()
    return c


def _cascade_q_cell_protection_dead(
    electrical: ElectricalGraph,
    q,
) -> dict:
    """Cell-protection series FET open / stuck_off → protected-side rail
    loses power. Consumers of that rail become dead.

    Upstream cell tap stays alive (it's still electrically connected to
    its cell). Uses the suffix-aware downstream helper so we pick the
    protected side even when the compiler didn't annotate a source_refdes
    on the rail (common on BMS Qs where vision misses the `powers` edge)."""
    downstream = _find_cell_protection_downstream(electrical, q)
    if downstream is None:
        return _empty_cascade()
    return _simulate_rail_loss(electrical, downstream)


_PASSIVE_CASCADE_TABLE: dict[tuple[str, str, str], CascadeFn] = {
    # ========================= RESISTORS =========================
    ("passive_r", "series", "open"): _cascade_series_open,
    ("passive_r", "series", "short"): _cascade_passive_alive,
    ("passive_r", "feedback", "open"): _cascade_feedback_open_overvolt,
    ("passive_r", "feedback", "short"): _cascade_feedback_short_undervolt,
    ("passive_r", "pull_up", "open"): _cascade_pull_up_open,
    ("passive_r", "pull_up", "short"): _cascade_pull_up_short,
    ("passive_r", "pull_down", "open"): _cascade_pull_up_open,
    ("passive_r", "pull_down", "short"): _cascade_passive_alive,
    ("passive_r", "current_sense", "open"): _cascade_series_open,
    ("passive_r", "current_sense", "short"): _cascade_passive_alive,
    ("passive_r", "damping", "open"): _cascade_passive_alive,
    ("passive_r", "damping", "short"): _cascade_passive_alive,
    # ========================= CAPACITORS ========================
    ("passive_c", "decoupling", "open"): _cascade_decoupling_open,
    ("passive_c", "decoupling", "short"): _cascade_decoupling_short,
    ("passive_c", "decoupling", "leaky_short"): _cascade_decoupling_leaky,
    ("passive_c", "bulk", "open"): _cascade_decoupling_open,
    ("passive_c", "bulk", "short"): _cascade_decoupling_short,
    ("passive_c", "bulk", "leaky_short"): _cascade_decoupling_leaky,
    ("passive_c", "filter", "open"): _cascade_filter_cap_open,
    ("passive_c", "filter", "short"): _cascade_decoupling_short,
    ("passive_c", "ac_coupling", "open"): _cascade_signal_path_open,
    ("passive_c", "ac_coupling", "short"): _cascade_signal_path_dc,
    ("passive_c", "tank", "open"): _cascade_tank_open,
    ("passive_c", "tank", "short"): _cascade_tank_short,
    ("passive_c", "bypass", "open"): _cascade_decoupling_open,
    ("passive_c", "bypass", "short"): _cascade_decoupling_short,
    # (ferrite entries added in T7)
    ("passive_fb", "filter", "open"): _cascade_filter_open,
    ("passive_fb", "filter", "short"): _cascade_passive_alive,
    # ========================= DIODES ===========================
    ("passive_d", "flyback", "open"): _cascade_flyback_open,
    ("passive_d", "flyback", "short"): _cascade_flyback_short,
    ("passive_d", "rectifier", "open"): _cascade_rectifier_open,
    ("passive_d", "rectifier", "short"): _cascade_rectifier_short,
    ("passive_d", "esd", "open"): _cascade_passive_alive,
    ("passive_d", "esd", "short"): _cascade_signal_to_ground,
    ("passive_d", "reverse_protection", "open"): _cascade_series_open,
    ("passive_d", "reverse_protection", "short"): _cascade_passive_alive,
    ("passive_d", "signal_clamp", "open"): _cascade_passive_alive,
    ("passive_d", "signal_clamp", "short"): _cascade_signal_to_ground,
    # ========================= TRANSISTORS (Phase 4.5) ===========================
    ("passive_q", "load_switch", "open"): _cascade_q_load_dead,
    ("passive_q", "load_switch", "short"): _cascade_q_load_stuck_on,
    ("passive_q", "load_switch", "stuck_on"): _cascade_q_load_stuck_on,
    ("passive_q", "load_switch", "stuck_off"): _cascade_q_load_dead,
    ("passive_q", "level_shifter", "open"): _cascade_q_shifter_signal_broken,
    ("passive_q", "level_shifter", "short"): _cascade_q_shifter_signal_stuck,
    ("passive_q", "level_shifter", "stuck_on"): _cascade_q_shifter_signal_stuck,
    ("passive_q", "level_shifter", "stuck_off"): _cascade_q_shifter_signal_broken,
    ("passive_q", "inrush_limiter", "open"): _cascade_q_inrush_rail_dead,
    ("passive_q", "inrush_limiter", "short"): _cascade_passive_alive,
    ("passive_q", "inrush_limiter", "stuck_on"): _cascade_passive_alive,
    ("passive_q", "inrush_limiter", "stuck_off"): _cascade_q_inrush_rail_dead,
    ("passive_q", "flyback_switch", "open"): _cascade_q_flyback_switch_dead,
    ("passive_q", "flyback_switch", "short"): _cascade_q_flyback_switch_short,
    ("passive_q", "flyback_switch", "stuck_on"): _cascade_q_flyback_switch_short,
    ("passive_q", "flyback_switch", "stuck_off"): _cascade_q_flyback_switch_dead,
    ("passive_q", "cell_protection", "open"): _cascade_q_cell_protection_dead,
    ("passive_q", "cell_protection", "short"): _cascade_passive_alive,
    ("passive_q", "cell_protection", "stuck_on"): _cascade_passive_alive,
    ("passive_q", "cell_protection", "stuck_off"): _cascade_q_cell_protection_dead,
    ("passive_q", "cell_balancer", "open"): _cascade_passive_alive,
    ("passive_q", "cell_balancer", "short"): _cascade_passive_alive,
    ("passive_q", "cell_balancer", "stuck_on"): _cascade_passive_alive,
    ("passive_q", "cell_balancer", "stuck_off"): _cascade_passive_alive,
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _narrate(
    kill_refdes: list[str],
    kill_modes: list[str],
    cascade: dict,
    metrics: HypothesisMetrics,
    diff: HypothesisDiff,
    observations: Observations,
) -> str:
    """Deterministic FR narrative — no LLM."""
    obs_total = len(observations.state_comps) + len(observations.state_rails)
    tp = metrics.tp_comps + metrics.tp_rails
    fp = metrics.fp_comps + metrics.fp_rails

    # Pick a rails preview — shorted takes precedence visually.
    shorted_preview = ", ".join(sorted(cascade["shorted_rails"])[:2])
    dead_preview = ", ".join(sorted(cascade["dead_rails"])[:3]) or "aucun rail"
    rails_preview = shorted_preview or dead_preview
    dead_count = max(0, len(cascade["dead_comps"]) - len(kill_refdes))
    anom_count = len(cascade["anomalous_comps"])

    if len(kill_refdes) == 1:
        verb = {
            "dead": "meurt",
            "anomalous": "dysfonctionne (output faux)",
            "hot": "chauffe anormalement",
            "shorted": "court vers GND",
        }.get(kill_modes[0], "échoue")
        head = f"Si {kill_refdes[0]} {verb} : {rails_preview}"
        if dead_count > 0:
            head += f" → {dead_count} composant(s) downstream morts"
        if anom_count > 1:
            head += f", {anom_count} composant(s) aval anormaux"
        head += "."
    else:
        parts = [f"{r} ({m})" for r, m in zip(kill_refdes, kill_modes, strict=True)]
        head = (
            f"Si {' ET '.join(parts)} échouent simultanément : "
            f"{rails_preview} → {dead_count} composant(s) downstream morts."
        )

    coverage = f" Explique {tp}/{obs_total} observations, {fp} contradiction(s)."

    # Cite up to 2 measurements.
    metric_snippets: list[str] = []
    for target, metric in list(observations.metrics_comps.items())[:2]:
        unit = metric.unit
        metric_snippets.append(f"{target} à {metric.measured}{unit}")
    for target, metric in list(observations.metrics_rails.items())[:2]:
        unit = metric.unit
        metric_snippets.append(f"{target} à {metric.measured}{unit}")
    metrics_tail = " Mesures : " + ", ".join(metric_snippets) + "." if metric_snippets else ""

    tail = ""
    if diff.contradictions:
        contras = ", ".join(f"{t} observé {o}, prédit {p}" for t, o, p in diff.contradictions[:3])
        tail += f" Contredit : {contras}."
    if diff.under_explained:
        tail += f" Ne couvre pas : {', '.join(diff.under_explained[:4])}."

    return head + coverage + metrics_tail + tail


def _cascade_preview(cascade: dict) -> dict:
    return {
        "dead_rails": sorted(cascade["dead_rails"]),
        "shorted_rails": sorted(cascade["shorted_rails"]),
        "always_on_rails": sorted(cascade["always_on_rails"]),  # Phase 4.5 — Q stuck_on
        "dead_comps_count": len(cascade["dead_comps"]),
        "anomalous_count": len(cascade["anomalous_comps"]),
        "hot_count": len(cascade["hot_comps"]),
    }


def _compute_discriminators(
    hypotheses: list[Hypothesis],
    *,
    score_tolerance: float = 0.01,
    top_n: int = 5,
    max_results: int = 3,
) -> list[str]:
    """Return targets that would best discriminate between tied top-N hypotheses.

    A target is "discriminating" if it appears as non-alive in SOME but
    not all of the top-N tied hypotheses. The best discriminator is the
    one whose split is closest to 50/50 — measuring it halves the
    candidate set.

    Returns an empty list when:
      - Fewer than 2 hypotheses
      - Top hypothesis scores clearly higher than #2 (> score_tolerance gap)
      - No target appears in >=1 but <top_n of the tied cascades
    """
    if len(hypotheses) < 2:
        return []
    # Top-N are "tied" if they all sit within score_tolerance of the best.
    best_score = hypotheses[0].score
    tied = [h for h in hypotheses[:top_n] if abs(h.score - best_score) <= score_tolerance]
    if len(tied) < 2:
        return []
    # Build a target → {indices of tied hypotheses that predict it} map.
    # Sources: cascade_preview rails + kill_refdes entries.
    target_signatures: dict[str, set[int]] = {}
    for idx, h in enumerate(tied):
        predicted: set[str] = set()
        # Rails from cascade_preview
        predicted.update(h.cascade_preview.get("dead_rails", []) or [])
        predicted.update(h.cascade_preview.get("shorted_rails", []) or [])
        predicted.update(h.cascade_preview.get("always_on_rails", []) or [])  # Phase 4.5
        # Also include the kill_refdes themselves — if H1 kills U1 and H2
        # kills U7, measuring U1 (is it dead/alive?) discriminates H1 vs H2.
        predicted.update(h.kill_refdes)
        for target in predicted:
            target_signatures.setdefault(target, set()).add(idx)

    # Score each target by how close its hit count is to N/2.
    n = len(tied)
    half = n / 2.0
    candidates = []
    for target, indices in target_signatures.items():
        hits = len(indices)
        if hits == 0 or hits == n:
            continue  # appears in none or all — not discriminating
        # Lower distance-from-half = better discriminator.
        distance = abs(hits - half)
        candidates.append((distance, -hits, target))
    # Sort: smallest distance first, then prefer targets with MORE hits
    # (ties broken toward the larger partition, so tech's measurement is
    # more likely to be informative on the first try).
    candidates.sort()
    return [t for _, _, t in candidates[:max_results]]


_GRAPH_MEMOS: dict[tuple[int, str], _GraphMemo] = {}


class _GraphMemo:
    """Per-graph lazy cache for the reverse-diagnostic engine.

    The accuracy bench runs ~200 scenarios against a single ElectricalGraph.
    Both `_applicable_modes` and `_simulate_failure` are pure functions of
    the graph topology — their outputs don't depend on the observations
    being scored. This memo computes them once per (graph, analyzed_boot)
    pair and shares the results across scenarios.

    Fields:
      - `applicable_modes[refdes] → tuple[str, ...]`: eagerly precomputed
        from typed_edges + power_rails in one linear pass per graph.
      - `cascades[(id(analyzed_boot), refdes, mode)] → dict`: lazily
        populated on first `_simulate_failure` call.

    Cascade dicts are treated as immutable by every caller (score,
    narrate, preview, two-fault union all read-only), so sharing is
    safe without defensive copies.
    """

    __slots__ = ("applicable_modes", "cascades")

    def __init__(self, graph: ElectricalGraph) -> None:
        signal_sources: set[str] = set()
        for edge in graph.typed_edges:
            if edge.kind in SIGNAL_EDGE_KINDS:
                signal_sources.add(edge.src)
        rail_consumers: set[str] = set()
        for rail in graph.power_rails.values():
            rail_consumers.update(rail.consumers or ())
        modes_by_refdes: dict[str, tuple[str, ...]] = {}
        for refdes, comp in graph.components.items():
            kind = getattr(comp, "kind", "ic")
            role = getattr(comp, "role", None)
            if kind == "ic":
                modes: list[str] = ["dead", "hot"]
                if refdes in signal_sources:
                    modes.append("anomalous")
                if refdes in rail_consumers:
                    modes.append("shorted")
                modes_by_refdes[refdes] = tuple(modes)
                continue
            if role is None:
                modes_by_refdes[refdes] = ()
                continue
            candidates = (
                ("open", "short", "stuck_on", "stuck_off")
                if kind == "passive_q"
                else ("open", "short")
            )
            applicable: list[str] = []
            for mode in candidates:
                handler = _PASSIVE_CASCADE_TABLE.get((kind, role, mode))
                if handler is not None and handler is not _cascade_passive_alive:
                    applicable.append(mode)
            modes_by_refdes[refdes] = tuple(applicable)
        self.applicable_modes: dict[str, tuple[str, ...]] = modes_by_refdes
        self.cascades: dict[tuple[int, str, str], dict] = {}


def _memo_for(graph: ElectricalGraph) -> _GraphMemo:
    """Return the memo attached to `graph`, building it on first access.

    Key: (id(graph), graph.device_slug). The slug guards against id() reuse
    after GC of a distinct-slug graph — the colliding id is rejected because
    the slug differs. Same-slug id collisions are harmless: a fresh-slug
    graph is semantically identical to the one it replaced.
    """
    key = (id(graph), graph.device_slug)
    memo = _GRAPH_MEMOS.get(key)
    if memo is None:
        memo = _GraphMemo(graph)
        _GRAPH_MEMOS[key] = memo
    return memo


def _applicable_modes(
    electrical: ElectricalGraph,
    refdes: str,
) -> list[str]:
    """Return the list of modes worth simulating for a given refdes.

    - ICs: `dead`, `hot` always; `anomalous` when the IC has an outgoing
      signal edge; `shorted` when the IC is a rail consumer.
    - Passives with a known role: `open` and/or `short` when the dispatch
      table has a non-alive handler for the (kind, role, mode) triple.
    - Passives without a role: no applicable mode (returns [])."""
    return list(_memo_for(electrical).applicable_modes.get(refdes, ()))


def _relevant_to_observations(cascade: dict, obs: Observations) -> bool:
    """Pruning gate — cascade touches at least one observation target."""
    obs_comps = set(obs.state_comps)
    obs_rails = set(obs.state_rails)
    any_pred = cascade["dead_comps"] | cascade["anomalous_comps"] | cascade["hot_comps"]
    any_rail = cascade["dead_rails"] | cascade["shorted_rails"] | cascade["always_on_rails"]
    if any_pred & obs_comps:
        return True
    if any_rail & obs_rails:
        return True
    return False


def _enumerate_single_fault(
    electrical: ElectricalGraph,
    analyzed_boot: AnalyzedBootSequence | None,
    observations: Observations,
) -> tuple[
    dict[tuple[str, str], dict],  # cascades by (refdes, mode)
    list[tuple[str, str, float, HypothesisMetrics, HypothesisDiff]],  # ranked survivors
]:
    cascades_cache: dict[tuple[str, str], dict] = {}
    ranked: list[tuple[str, str, float, HypothesisMetrics, HypothesisDiff]] = []
    for refdes in electrical.components:
        comp = electrical.components[refdes]
        kind = getattr(comp, "kind", "ic")
        role = getattr(comp, "role", None)
        for mode in _applicable_modes(electrical, refdes):
            cascade = _simulate_failure(electrical, analyzed_boot, refdes, mode)
            cascades_cache[(refdes, mode)] = cascade
            if not _relevant_to_observations(cascade, observations):
                continue
            tp_mult = _SCORE_VISIBILITY.get((kind, role, mode), 1.0) if role else 1.0
            score, metrics, diff = _score_candidate(
                cascade,
                observations,
                tp_mult=tp_mult,
            )
            ranked.append((refdes, mode, score, metrics, diff))
    ranked.sort(key=lambda t: -t[2])
    return cascades_cache, ranked


def _enumerate_two_fault(
    electrical: ElectricalGraph,
    analyzed_boot: AnalyzedBootSequence | None,
    observations: Observations,
    cascades_cache: dict[tuple[str, str], dict],
    single_ranked: list[tuple[str, str, float, HypothesisMetrics, HypothesisDiff]],
) -> tuple[
    int,
    list[
        tuple[
            tuple[tuple[str, str], tuple[str, str]], float, HypothesisMetrics, HypothesisDiff, dict
        ]
    ],
]:
    """2-fault pass seeded by top-K single-fault survivors.

    Each kill element is a (refdes, mode) pair. Pairs are deduplicated
    as sorted tuples. Capped at MAX_PAIRS.
    """
    if not TWO_FAULT_ENABLED:
        return 0, []

    top_k = [(r, m) for r, m, *_ in single_ranked[:TOP_K_SINGLE]]
    seen: set[tuple[tuple[str, str], tuple[str, str]]] = set()
    pairs_tested = 0
    ranked: list[
        tuple[
            tuple[tuple[str, str], tuple[str, str]], float, HypothesisMetrics, HypothesisDiff, dict
        ]
    ] = []

    for r1, m1 in top_k:
        c1 = cascades_cache[(r1, m1)]
        residual_comps = set(observations.state_comps) - (
            c1["dead_comps"] | c1["anomalous_comps"] | c1["hot_comps"]
        )
        residual_rails = set(observations.state_rails) - (
            c1["dead_rails"] | c1["shorted_rails"] | c1["always_on_rails"]
        )
        if not residual_comps and not residual_rails:
            continue
        for (r2, m2), c2 in cascades_cache.items():
            if (r2, m2) == (r1, m1) or r2 == r1:
                continue
            key = tuple(sorted(((r1, m1), (r2, m2))))
            if key in seen:
                continue
            # c2 must touch at least one residual target.
            c2_all_comps = c2["dead_comps"] | c2["anomalous_comps"] | c2["hot_comps"]
            c2_all_rails = c2["dead_rails"] | c2["shorted_rails"] | c2["always_on_rails"]
            if not (c2_all_comps & residual_comps) and not (c2_all_rails & residual_rails):
                continue
            seen.add(key)
            # Union cascades: we don't re-simulate the combined pair (the
            # forward simulator doesn't compose modes cleanly). Take the
            # element-wise union of buckets — this is an approximation but
            # it's cheap and matches observation semantics.
            combined = {
                "dead_comps": c1["dead_comps"] | c2["dead_comps"],
                "dead_rails": c1["dead_rails"] | c2["dead_rails"],
                "shorted_rails": c1["shorted_rails"] | c2["shorted_rails"],
                "always_on_rails": c1["always_on_rails"] | c2["always_on_rails"],  # Phase 4.5
                "anomalous_comps": c1["anomalous_comps"] | c2["anomalous_comps"],
                "hot_comps": c1["hot_comps"] | c2["hot_comps"],
                "final_verdict": c1.get("final_verdict") or c2.get("final_verdict") or "",
                "blocked_at_phase": None,
            }
            pairs_tested += 1
            score, metrics, diff = _score_candidate(combined, observations)
            ranked.append((key, score, metrics, diff, combined))
            if pairs_tested >= MAX_PAIRS:
                break
        if pairs_tested >= MAX_PAIRS:
            break
    ranked.sort(key=lambda t: -t[1])
    return pairs_tested, ranked


def _validate_obs_against_graph(
    electrical: ElectricalGraph,
    observations: Observations,
) -> None:
    """Cross-check each observation's mode against the target's ComponentKind.

    Raises ValueError with a specific target-and-mode message. The Pydantic
    shape accepts any value in the unified ComponentMode Literal; this
    function is the source of truth for `(kind, mode)` coherence.
    """
    for refdes, mode in observations.state_comps.items():
        comp = electrical.components.get(refdes)
        if comp is None:
            # Unknown refdes — no kind info; allow and let scoring drop it.
            continue
        kind = getattr(comp, "kind", "ic")
        if kind == "ic" and mode not in _IC_MODES:
            raise ValueError(
                f"Observation for {refdes!r} uses {mode!r} — not a valid IC mode "
                f"(expected one of {sorted(_IC_MODES)})."
            )
        if kind != "ic" and mode not in _PASSIVE_MODES:
            raise ValueError(
                f"Observation for {refdes!r} (kind={kind}) uses {mode!r} — "
                f"not a passive mode (expected one of {sorted(_PASSIVE_MODES)})."
            )


def hypothesize(
    electrical: ElectricalGraph,
    *,
    analyzed_boot: AnalyzedBootSequence | None = None,
    observations: Observations,
    max_results: int = MAX_RESULTS_DEFAULT,
) -> HypothesizeResult:
    """Rank candidate (refdes, mode) kills that explain `observations`."""
    t0 = time.perf_counter()
    _validate_obs_against_graph(electrical, observations)
    if observations.is_empty():
        return HypothesizeResult(
            device_slug=electrical.device_slug,
            observations_echo=observations,
            hypotheses=[],
            pruning=PruningStats(
                single_candidates_tested=0,
                two_fault_pairs_tested=0,
                wall_ms=(time.perf_counter() - t0) * 1000,
            ),
        )

    cascades_cache, single_ranked = _enumerate_single_fault(
        electrical,
        analyzed_boot,
        observations,
    )
    pairs_tested, two_ranked = _enumerate_two_fault(
        electrical,
        analyzed_boot,
        observations,
        cascades_cache,
        single_ranked,
    )

    hypotheses: list[Hypothesis] = []
    for refdes, mode, score, metrics, diff in single_ranked:
        cascade = cascades_cache[(refdes, mode)]
        hypotheses.append(
            Hypothesis(
                kill_refdes=[refdes],
                kill_modes=[mode],
                score=score,
                metrics=metrics,
                diff=diff,
                narrative=_narrate([refdes], [mode], cascade, metrics, diff, observations),
                cascade_preview=_cascade_preview(cascade),
            )
        )
    for key, score, metrics, diff, combined in two_ranked:
        (r1, m1), (r2, m2) = key
        hypotheses.append(
            Hypothesis(
                kill_refdes=[r1, r2],
                kill_modes=[m1, m2],
                score=score,
                metrics=metrics,
                diff=diff,
                narrative=_narrate([r1, r2], [m1, m2], combined, metrics, diff, observations),
                cascade_preview=_cascade_preview(combined),
            )
        )

    hypotheses.sort(
        key=lambda h: (
            -h.score,
            len(h.kill_refdes),
            h.cascade_preview["dead_comps_count"] + h.cascade_preview["anomalous_count"],
        )
    )
    hypotheses = hypotheses[:max_results]

    # Find discriminators (empty when scores are well-separated).
    discriminating_targets = _compute_discriminators(hypotheses)

    return HypothesizeResult(
        device_slug=electrical.device_slug,
        observations_echo=observations,
        hypotheses=hypotheses,
        pruning=PruningStats(
            single_candidates_tested=len(cascades_cache),
            two_fault_pairs_tested=pairs_tested,
            wall_ms=(time.perf_counter() - t0) * 1000,
        ),
        discriminating_targets=discriminating_targets,
    )
