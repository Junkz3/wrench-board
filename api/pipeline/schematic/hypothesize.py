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

from pydantic import BaseModel, ConfigDict, Field

from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph

# ---------------------------------------------------------------------------
# Tunable constants — exported so tests and scripts can override without
# monkey-patching. `tune_hypothesize_weights.py` rewrites PENALTY_WEIGHTS
# based on benchmark accuracy.
# ---------------------------------------------------------------------------

PENALTY_WEIGHTS: tuple[int, int] = (10, 2)   # (fp_weight, fn_weight)
TOP_K_SINGLE: int = 20                        # how many single-fault survivors seed 2-fault
MAX_RESULTS_DEFAULT: int = 5
TWO_FAULT_ENABLED: bool = True


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


class Observations(BaseModel):
    """Partial board observation provided by the tech (or Claude on their behalf).

    Every set is a frozenset of exact refdes / rail labels. Empty sets are fine —
    the tech may observe only rails, or only components. Dead and alive sets
    for the same class are expected to be disjoint (enforced at construction).
    """

    model_config = ConfigDict(extra="forbid")

    dead_comps: frozenset[str] = Field(default_factory=frozenset)
    alive_comps: frozenset[str] = Field(default_factory=frozenset)
    dead_rails: frozenset[str] = Field(default_factory=frozenset)
    alive_rails: frozenset[str] = Field(default_factory=frozenset)


class HypothesisMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tp_comps: int
    tp_rails: int
    fp_comps: int   # predicted dead, observed alive (contradiction)
    fp_rails: int
    fn_comps: int   # observed dead, predicted alive (under-explain)
    fn_rails: int


class HypothesisDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contradictions: list[str] = Field(default_factory=list)
    under_explained: list[str] = Field(default_factory=list)
    over_predicted: list[str] = Field(default_factory=list)


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kill_refdes: list[str]
    score: float
    metrics: HypothesisMetrics
    diff: HypothesisDiff
    narrative: str
    cascade_preview: dict


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
# Scoring
# ---------------------------------------------------------------------------


def _score_candidate(
    predicted: dict,
    observations: Observations,
) -> tuple[float, HypothesisMetrics, HypothesisDiff]:
    """Score one hypothesis against the observations.

    `predicted` is a dict `{"dead_comps": frozenset, "dead_rails": frozenset}`
    produced by simulating the candidate kill. Returns:

    - score = TP − fp_weight·FP − fn_weight·FN
    - metrics = per-class TP / FP / FN counts
    - diff    = structured breakdown (contradictions / under_explained /
                over_predicted) for UI rendering

    TP counts both dead-correctly-predicted-dead AND alive-correctly-predicted-alive
    (when the tech provides alive-side evidence, matching it is also a positive
    signal, not just the absence of a contradiction).
    """
    fp_w, fn_w = PENALTY_WEIGHTS

    pred_dead_comps: frozenset[str] = predicted.get("dead_comps", frozenset())
    pred_dead_rails: frozenset[str] = predicted.get("dead_rails", frozenset())

    # Dead-side matches
    tp_dc = len(pred_dead_comps & observations.dead_comps)
    tp_dr = len(pred_dead_rails & observations.dead_rails)
    # Alive-side matches (predicted alive = complement of predicted dead within
    # the observed alive sets — we only credit elements the tech positively
    # said are alive).
    tp_ac = len(observations.alive_comps - pred_dead_comps)
    tp_ar = len(observations.alive_rails - pred_dead_rails)

    # Contradictions: predicted dead BUT observed alive
    fp_c_set = pred_dead_comps & observations.alive_comps
    fp_r_set = pred_dead_rails & observations.alive_rails
    # Under-explanations: observed dead BUT predicted alive
    fn_c_set = observations.dead_comps - pred_dead_comps
    fn_r_set = observations.dead_rails - pred_dead_rails
    # Over-predicted (informational): predicted dead, not in any obs set
    observed_either_comps = (
        observations.dead_comps | observations.alive_comps
    )
    observed_either_rails = (
        observations.dead_rails | observations.alive_rails
    )
    over_comps = pred_dead_comps - observed_either_comps
    over_rails = pred_dead_rails - observed_either_rails

    metrics = HypothesisMetrics(
        tp_comps=tp_dc + tp_ac,
        tp_rails=tp_dr + tp_ar,
        fp_comps=len(fp_c_set),
        fp_rails=len(fp_r_set),
        fn_comps=len(fn_c_set),
        fn_rails=len(fn_r_set),
    )
    tp = metrics.tp_comps + metrics.tp_rails
    fp = metrics.fp_comps + metrics.fp_rails
    fn = metrics.fn_comps + metrics.fn_rails
    score = float(tp - fp_w * fp - fn_w * fn)

    diff = HypothesisDiff(
        contradictions=sorted(fp_c_set | fp_r_set),
        under_explained=sorted(fn_c_set | fn_r_set),
        over_predicted=sorted(over_comps | over_rails),
    )
    return score, metrics, diff


# ---------------------------------------------------------------------------
# Public entry point — stub until Task 3
# ---------------------------------------------------------------------------


def hypothesize(
    electrical: ElectricalGraph,
    *,
    analyzed_boot: AnalyzedBootSequence | None = None,
    observations: Observations,
    max_results: int = MAX_RESULTS_DEFAULT,
) -> HypothesizeResult:
    """Rank candidate refdes-kills that explain `observations`."""
    raise NotImplementedError
