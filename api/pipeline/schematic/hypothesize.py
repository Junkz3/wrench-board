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

from pydantic import BaseModel, ConfigDict, Field

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
# Forward-simulation helpers
# ---------------------------------------------------------------------------


def _simulate_kill(
    electrical: ElectricalGraph,
    analyzed_boot: AnalyzedBootSequence | None,
    killed: list[str],
) -> dict:
    """Run the forward simulator and return the compact cascade dict used here."""
    tl = SimulationEngine(
        electrical, analyzed_boot=analyzed_boot, killed_refdes=killed,
    ).run()
    return {
        "dead_comps": frozenset(set(tl.cascade_dead_components) | set(killed)),
        "dead_rails": frozenset(tl.cascade_dead_rails),
        "final_verdict": tl.final_verdict,
        "blocked_at_phase": tl.blocked_at_phase,
    }


def _relevant_to_observations(
    cascade: dict, observations: Observations
) -> bool:
    """Pruning gate — keep the candidate only if its cascade touches an obs."""
    if cascade["dead_comps"] & observations.dead_comps:
        return True
    if cascade["dead_rails"] & observations.dead_rails:
        return True
    return False


def _narrate(
    kill_refdes: list[str],
    cascade: dict,
    metrics: HypothesisMetrics,
    diff: HypothesisDiff,
    observations: Observations,
) -> str:
    """Deterministic FR narrative for one hypothesis. No LLM."""
    obs_total = (
        len(observations.dead_comps) + len(observations.alive_comps)
        + len(observations.dead_rails) + len(observations.alive_rails)
    )
    tp = metrics.tp_comps + metrics.tp_rails
    fp = metrics.fp_comps + metrics.fp_rails
    dead_rails_preview = ", ".join(sorted(cascade["dead_rails"])[:3]) or "aucun rail"
    dead_count = max(0, len(cascade["dead_comps"]) - len(kill_refdes))

    if len(kill_refdes) == 1:
        head = (
            f"Si {kill_refdes[0]} meurt : {dead_rails_preview} jamais stable(s) "
            f"→ {dead_count} composant(s) downstream morts."
        )
    else:
        joined = " ET ".join(kill_refdes)
        head = (
            f"Si {joined} meurent simultanément : {dead_rails_preview} jamais "
            f"stable(s) → {dead_count} composant(s) downstream morts."
        )

    coverage = f" Explique {tp}/{obs_total} observations, {fp} contradiction(s)."

    tail = ""
    if diff.contradictions:
        tail += f" Contredit : {', '.join(diff.contradictions[:4])}."
    if diff.under_explained:
        tail += f" Ne couvre pas : {', '.join(diff.under_explained[:4])}."

    return head + coverage + tail


def _enumerate_single_fault(
    electrical: ElectricalGraph,
    analyzed_boot: AnalyzedBootSequence | None,
    observations: Observations,
) -> tuple[dict[str, dict], list[tuple[str, float, HypothesisMetrics, HypothesisDiff]]]:
    """Run single-fault enumeration with pruning.

    Returns:
      - cascades_cache: {refdes: cascade_dict}  — ALL tested cascades, even
        those that scored < 0. Reused by 2-fault as the "c1" candidate pool.
      - ranked: list of (refdes, score, metrics, diff) for candidates that
        passed the relevance gate, score-sorted descending.
    """
    cascades_cache: dict[str, dict] = {}
    ranked: list[tuple[str, float, HypothesisMetrics, HypothesisDiff]] = []
    for refdes in electrical.components:
        cascade = _simulate_kill(electrical, analyzed_boot, [refdes])
        cascades_cache[refdes] = cascade
        if not _relevant_to_observations(cascade, observations):
            continue
        score, metrics, diff = _score_candidate(cascade, observations)
        ranked.append((refdes, score, metrics, diff))
    ranked.sort(key=lambda t: -t[1])
    return cascades_cache, ranked


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
    """Rank candidate refdes-kills that explain `observations`."""
    t0 = time.perf_counter()
    has_any = bool(
        observations.dead_comps
        or observations.alive_comps
        or observations.dead_rails
        or observations.alive_rails
    )
    if not has_any:
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

    # Assemble Hypothesis objects from the ranked list.
    hypotheses: list[Hypothesis] = []
    for refdes, score, metrics, diff in single_ranked:
        cascade = cascades_cache[refdes]
        hypotheses.append(Hypothesis(
            kill_refdes=[refdes],
            score=score,
            metrics=metrics,
            diff=diff,
            narrative=_narrate(
                kill_refdes=[refdes],
                cascade=cascade,
                metrics=metrics,
                diff=diff,
                observations=observations,
            ),
            cascade_preview={
                "dead_rails": sorted(cascade["dead_rails"]),
                "dead_comps_count": len(cascade["dead_comps"]),
            },
        ))

    # Top-N slicing.
    hypotheses = hypotheses[:max_results]
    return HypothesizeResult(
        device_slug=electrical.device_slug,
        observations_echo=observations,
        hypotheses=hypotheses,
        pruning=PruningStats(
            single_candidates_tested=len(cascades_cache),
            two_fault_pairs_tested=0,
            wall_ms=(time.perf_counter() - t0) * 1000,
        ),
    )
