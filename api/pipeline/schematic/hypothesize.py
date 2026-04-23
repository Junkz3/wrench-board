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

from typing import Literal

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

ComponentMode = Literal["dead", "alive", "anomalous", "hot"]
RailMode = Literal["dead", "alive", "shorted"]


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
    kill_modes: list[ComponentMode]
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
        raise NotImplementedError("shorted lands in Task 5")
    raise ValueError(f"unknown failure mode: {mode!r}")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def hypothesize(
    electrical: ElectricalGraph,
    *,
    analyzed_boot: AnalyzedBootSequence | None = None,
    observations: Observations,
    max_results: int = MAX_RESULTS_DEFAULT,
) -> HypothesizeResult:
    raise NotImplementedError  # lands in Task 6
