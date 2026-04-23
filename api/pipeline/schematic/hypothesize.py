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
from typing import TYPE_CHECKING, Callable, Literal

if TYPE_CHECKING:
    from api.pipeline.schematic.schemas import ComponentNode as _CompNode

CascadeFn = Callable[["ElectricalGraph", "_CompNode"], dict]

from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph
from api.pipeline.schematic.simulator import SimulationEngine

# ---------------------------------------------------------------------------
# Tunable constants — exported so tests and scripts can override without
# monkey-patching. `tune_hypothesize_weights.py` rewrites PENALTY_WEIGHTS
# based on benchmark accuracy.
# ---------------------------------------------------------------------------

PENALTY_WEIGHTS: tuple[int, int] = (10, 2)   # (fp_weight, fn_weight)
TOP_K_SINGLE: int = 20                        # how many single-fault survivors seed 2-fault
MAX_RESULTS_DEFAULT: int = 5
TWO_FAULT_ENABLED: bool = True
MAX_PAIRS: int = 100                          # 2-fault pair cap (safety net, rarely hit)

# ---------------------------------------------------------------------------
# Mode vocabulary — imported by tools, HTTP, tests, UI JSON.
# ---------------------------------------------------------------------------

ComponentMode = Literal[
    "dead", "alive", "anomalous", "hot",
    "open", "short",
]
RailMode = Literal["dead", "alive", "shorted"]

# Failure modes that can be attributed to a component as the root-cause kill.
# `alive` is omitted (a live component is not a failure). `shorted` is a rail
# observation but it's produced by a shorted component pulling its input rail
# to GND, so it's a legitimate component-level failure mode in this engine.
# `open` / `short` are the Phase 4 additions for passives.
FailureMode = Literal[
    "dead", "anomalous", "hot", "shorted",
    "open", "short",
]

_IC_MODES: frozenset[str] = frozenset({"dead", "alive", "anomalous", "hot"})
_PASSIVE_MODES: frozenset[str] = frozenset({"open", "short", "alive"})


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
            raise ValueError(
                f"target appears as both component and rail: {sorted(overlap)}"
            )
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
    cascade_preview: dict  # {dead_rails, shorted_rails, dead_comps_count, anomalous_count, hot_count}


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


# ---------------------------------------------------------------------------
# Forward simulation — mode-aware dispatcher
# ---------------------------------------------------------------------------


def _empty_cascade() -> dict:
    return {
        "dead_comps": frozenset(),
        "dead_rails": frozenset(),
        "shorted_rails": frozenset(),
        "anomalous_comps": frozenset(),
        "hot_comps": frozenset(),
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
        electrical, analyzed_boot=analyzed_boot, killed_refdes=killed,
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
    electrical: ElectricalGraph, origin_refdes: str,
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
    electrical: ElectricalGraph, refdes: str,
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
    """
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
            _simulate_dead(electrical, analyzed_boot, [source])
            if source else _empty_cascade()
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
    raise ValueError(f"unknown failure mode: {mode!r}")


# ---------------------------------------------------------------------------
# Scoring — mode-aware F1-style soft-penalty function
# ---------------------------------------------------------------------------


def _score_candidate(
    cascade: dict,
    obs: Observations,
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
        tp_comps=tp_c, tp_rails=tp_r,
        fp_comps=fp_c, fp_rails=fp_r,
        fn_comps=fn_c, fn_rails=fn_r,
    )
    tp = tp_c + tp_r
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


def _find_downstream_rail(
    electrical: ElectricalGraph, passive: "_CompNode",
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
    # Downstream = the one whose source_refdes is null (no IC drives it)
    # OR whose consumers list is non-empty.
    candidates = []
    for label in rail_labels:
        rail = electrical.power_rails[label]
        # A downstream-of-passive rail typically has source_refdes=None
        # because the passive is the implicit source.
        if rail.source_refdes is None:
            candidates.append(label)
    if len(candidates) == 1:
        return candidates[0]
    # Fall back: pick the rail with more consumers.
    rail_labels.sort(
        key=lambda r: len(electrical.power_rails[r].consumers or []),
        reverse=True,
    )
    return rail_labels[0]


def _find_decoupled_rail(
    electrical: ElectricalGraph, passive: "_CompNode",
) -> str | None:
    """A decoupling cap has one pin on a rail and one on GND. Return the rail."""
    nets = [p.net_label for p in passive.pins if p.net_label]
    for n in nets:
        if n in electrical.power_rails:
            return n
    return None


def _find_decoupled_ic(
    electrical: ElectricalGraph, passive: "_CompNode",
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
    electrical: ElectricalGraph, passive: "_CompNode",
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
    electrical: ElectricalGraph, rail_label: str,
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

def _cascade_passive_alive(electrical: ElectricalGraph, passive: "_CompNode") -> dict:
    """Physically plausible but no observable cascade. Empty → pruned."""
    return _empty_cascade()


def _cascade_series_open(electrical: ElectricalGraph, passive: "_CompNode") -> dict:
    downstream = _find_downstream_rail(electrical, passive)
    if downstream is None:
        return _empty_cascade()
    return _simulate_rail_loss(electrical, downstream)


def _cascade_filter_open(electrical: ElectricalGraph, passive: "_CompNode") -> dict:
    # FB filter open is functionally identical to a series element open.
    return _cascade_series_open(electrical, passive)


def _cascade_decoupling_open(electrical: ElectricalGraph, passive: "_CompNode") -> dict:
    ic = _find_decoupled_ic(electrical, passive)
    if ic is None:
        return _empty_cascade()
    c = _empty_cascade()
    c["anomalous_comps"] = frozenset({ic})
    return c


def _cascade_decoupling_short(electrical: ElectricalGraph, passive: "_CompNode") -> dict:
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


def _cascade_feedback_open_overvolt(electrical: ElectricalGraph, passive: "_CompNode") -> dict:
    rail = _find_regulated_rail_of_feedback(electrical, passive)
    if rail is None:
        return _empty_cascade()
    c = _empty_cascade()
    c["shorted_rails"] = frozenset({rail})  # Phase 1 encoding for overvoltage
    consumers = electrical.power_rails[rail].consumers or []
    c["anomalous_comps"] = frozenset(consumers)
    return c


# The dispatch table is filled in T7 (C), T8 (D/FB). For T6 we register
# just the primitives so the three unit tests pass.
_PASSIVE_CASCADE_TABLE: dict[tuple[str, str, str], CascadeFn] = {
    ("passive_r",  "series", "open"):  _cascade_series_open,
    ("passive_fb", "filter", "open"):  _cascade_filter_open,
    # (rest added in T7/T8)
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
    metrics_tail = (
        " Mesures : " + ", ".join(metric_snippets) + "."
        if metric_snippets else ""
    )

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
        "dead_comps_count": len(cascade["dead_comps"]),
        "anomalous_count": len(cascade["anomalous_comps"]),
        "hot_count": len(cascade["hot_comps"]),
    }


def _applicable_modes(
    electrical: ElectricalGraph, refdes: str,
) -> list[str]:
    """Return the list of modes worth trying for a given refdes.

    - `dead` always.
    - `anomalous` if the refdes has at least one outgoing signal-typed edge.
    - `hot` always (cheap, self-only).
    - `shorted` if the refdes is listed as a consumer of any power rail.
    """
    modes = ["dead", "hot"]
    has_signal = any(
        e.src == refdes and e.kind in SIGNAL_EDGE_KINDS
        for e in electrical.typed_edges
    )
    if has_signal:
        modes.append("anomalous")
    is_consumer = any(
        refdes in (r.consumers or [])
        for r in electrical.power_rails.values()
    )
    if is_consumer:
        modes.append("shorted")
    return modes


def _relevant_to_observations(cascade: dict, obs: Observations) -> bool:
    """Pruning gate — cascade touches at least one observation target."""
    obs_comps = set(obs.state_comps)
    obs_rails = set(obs.state_rails)
    any_pred = (
        cascade["dead_comps"] | cascade["anomalous_comps"] | cascade["hot_comps"]
    )
    any_rail = cascade["dead_rails"] | cascade["shorted_rails"]
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
        for mode in _applicable_modes(electrical, refdes):
            cascade = _simulate_failure(electrical, analyzed_boot, refdes, mode)
            cascades_cache[(refdes, mode)] = cascade
            if not _relevant_to_observations(cascade, observations):
                continue
            score, metrics, diff = _score_candidate(cascade, observations)
            ranked.append((refdes, mode, score, metrics, diff))
    ranked.sort(key=lambda t: -t[2])
    return cascades_cache, ranked


def _enumerate_two_fault(
    electrical: ElectricalGraph,
    analyzed_boot: AnalyzedBootSequence | None,
    observations: Observations,
    cascades_cache: dict[tuple[str, str], dict],
    single_ranked: list[tuple[str, str, float, HypothesisMetrics, HypothesisDiff]],
) -> tuple[int, list[tuple[tuple[tuple[str, str], tuple[str, str]], float, HypothesisMetrics, HypothesisDiff, dict]]]:
    """2-fault pass seeded by top-K single-fault survivors.

    Each kill element is a (refdes, mode) pair. Pairs are deduplicated
    as sorted tuples. Capped at MAX_PAIRS.
    """
    if not TWO_FAULT_ENABLED:
        return 0, []

    top_k = [(r, m) for r, m, *_ in single_ranked[:TOP_K_SINGLE]]
    seen: set[tuple[tuple[str, str], tuple[str, str]]] = set()
    pairs_tested = 0
    ranked: list[tuple[tuple[tuple[str, str], tuple[str, str]], float, HypothesisMetrics, HypothesisDiff, dict]] = []

    for (r1, m1) in top_k:
        c1 = cascades_cache[(r1, m1)]
        residual_comps = (
            set(observations.state_comps) - (c1["dead_comps"] | c1["anomalous_comps"] | c1["hot_comps"])
        )
        residual_rails = (
            set(observations.state_rails) - (c1["dead_rails"] | c1["shorted_rails"])
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
            c2_all_rails = c2["dead_rails"] | c2["shorted_rails"]
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
    electrical: ElectricalGraph, observations: Observations,
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
        electrical, analyzed_boot, observations,
    )
    pairs_tested, two_ranked = _enumerate_two_fault(
        electrical, analyzed_boot, observations,
        cascades_cache, single_ranked,
    )

    hypotheses: list[Hypothesis] = []
    for refdes, mode, score, metrics, diff in single_ranked:
        cascade = cascades_cache[(refdes, mode)]
        hypotheses.append(Hypothesis(
            kill_refdes=[refdes],
            kill_modes=[mode],
            score=score,
            metrics=metrics,
            diff=diff,
            narrative=_narrate([refdes], [mode], cascade, metrics, diff, observations),
            cascade_preview=_cascade_preview(cascade),
        ))
    for key, score, metrics, diff, combined in two_ranked:
        (r1, m1), (r2, m2) = key
        hypotheses.append(Hypothesis(
            kill_refdes=[r1, r2],
            kill_modes=[m1, m2],
            score=score,
            metrics=metrics,
            diff=diff,
            narrative=_narrate([r1, r2], [m1, m2], combined, metrics, diff, observations),
            cascade_preview=_cascade_preview(combined),
        ))

    hypotheses.sort(key=lambda h: (
        -h.score,
        len(h.kill_refdes),
        h.cascade_preview["dead_comps_count"] + h.cascade_preview["anomalous_count"],
    ))
    hypotheses = hypotheses[:max_results]

    return HypothesizeResult(
        device_slug=electrical.device_slug,
        observations_echo=observations,
        hypotheses=hypotheses,
        pruning=PruningStats(
            single_candidates_tested=len(cascades_cache),
            two_fault_pairs_tested=pairs_tested,
            wall_ms=(time.perf_counter() - t0) * 1000,
        ),
    )
