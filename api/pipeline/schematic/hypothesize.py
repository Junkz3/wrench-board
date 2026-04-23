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
