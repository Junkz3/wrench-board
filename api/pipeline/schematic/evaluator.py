# SPDX-License-Identifier: Apache-2.0
"""Scalar evaluation of the simulator + hypothesize stack.

Pure functions. Caller loads the graph and the bench from disk and
passes them in. The CLI in scripts/eval_simulator.py wires the I/O.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from api.pipeline.schematic.schemas import ElectricalGraph
from api.pipeline.schematic.simulator import Failure, SimulationEngine

# Per-spec weighting — kept as constants so future re-weighting is one diff.
WEIGHT_SELF_MRR = 0.6
WEIGHT_CASCADE_RECALL = 0.4
DEFAULT_MAX_PER_KIND = 50


class ScenarioResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    self_mrr_contribution: float = 0.0
    cascade_recall: float | None = None


class Scorecard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float
    self_mrr: float
    cascade_recall: float
    n_scenarios: int
    per_scenario: list[ScenarioResult] = Field(default_factory=list)


def compute_self_mrr(graph: ElectricalGraph, *, max_per_kind: int = DEFAULT_MAX_PER_KIND) -> float:
    """For every (refdes, mode) pair sampled from the graph, forward-simulate
    then check whether the cause is recoverable from the resulting state.
    Returns mean reciprocal rank ∈ [0, 1]. Returns 0.0 on empty graphs.

    Sampling is deterministic (sorted refdes), reproducible across runs.
    """
    candidates: list[tuple[str, str]] = []
    by_kind: dict[str, list[str]] = {}
    for refdes in sorted(graph.components):
        kind = graph.components[refdes].kind or "ic"
        by_kind.setdefault(kind, []).append(refdes)
    for kind, refdes_list in by_kind.items():
        for refdes in refdes_list[:max_per_kind]:
            for mode in _MODES_FOR_KIND.get(kind, ("dead",)):
                candidates.append((refdes, mode))
    if not candidates:
        return 0.0

    rrs: list[float] = []
    for refdes, mode in candidates:
        timeline = SimulationEngine(graph, failures=[_make_failure(refdes, mode)]).run()
        symptoms = _symptoms_from_timeline(timeline)
        ranked = _rank_candidates_for_symptoms(graph, symptoms)
        rank = next(
            (i + 1 for i, (r, m) in enumerate(ranked) if r == refdes and m == mode),
            None,
        )
        rrs.append(1.0 / rank if rank else 0.0)

    return sum(rrs) / len(rrs)


def compute_cascade_recall(
    graph: ElectricalGraph, scenarios: list[dict]
) -> tuple[float, list[ScenarioResult]]:
    """For each scenario in the bench, forward-simulate the cause and compare
    predicted dead rails / components to the expected set. Recall is averaged
    across rails+components for that scenario; the macro-mean is returned.
    Returns 0.0 with an empty breakdown when scenarios is empty."""
    if not scenarios:
        return 0.0, []

    # Skip scenarios for other devices silently — keeps the bench file
    # device-agnostic without forcing the caller to filter.
    relevant = [s for s in scenarios if s.get("device_slug") == graph.device_slug]
    if not relevant:
        return 0.0, []

    breakdown: list[ScenarioResult] = []
    recalls: list[float] = []
    for s in relevant:
        cause = s["cause"]
        expected_rails = set(s.get("expected_dead_rails") or [])
        expected_comps = set(s.get("expected_dead_components") or [])
        f_kwargs = {k: v for k, v in cause.items() if k != "refdes" and k != "mode"}
        timeline = SimulationEngine(
            graph,
            failures=[Failure(refdes=cause["refdes"], mode=cause["mode"], **f_kwargs)],
        ).run()
        predicted_rails = set(timeline.cascade_dead_rails)
        predicted_comps = set(timeline.cascade_dead_components)
        rec_rails = (
            len(predicted_rails & expected_rails) / len(expected_rails) if expected_rails else None
        )
        rec_comps = (
            len(predicted_comps & expected_comps) / len(expected_comps) if expected_comps else None
        )
        parts = [r for r in (rec_rails, rec_comps) if r is not None]
        # Scenarios with empty expected sets (degraded-only cases: leaky_short,
        # regulating_low above UVLO, etc.) test that the simulator does NOT
        # over-predict a cascade. If the predicted cascade is also empty, the
        # simulator got it right → recall = 1.0. If it over-predicted, penalise.
        if parts:
            recall = sum(parts) / len(parts)
        elif not predicted_rails and not predicted_comps:
            recall = 1.0  # correct "no cascade" prediction
        else:
            recall = 0.0  # false-positive cascade
        breakdown.append(
            ScenarioResult(
                scenario_id=s["id"],
                cascade_recall=recall,
            )
        )
        recalls.append(recall)
    return (sum(recalls) / len(recalls)), breakdown


def compute_score(graph: ElectricalGraph, scenarios: list[dict]) -> Scorecard:
    """Weighted scalar: WEIGHT_SELF_MRR × self_mrr + WEIGHT_CASCADE_RECALL × cascade_recall."""
    self_mrr = compute_self_mrr(graph)
    cascade_recall, breakdown = compute_cascade_recall(graph, scenarios)
    score = WEIGHT_SELF_MRR * self_mrr + WEIGHT_CASCADE_RECALL * cascade_recall
    return Scorecard(
        score=score,
        self_mrr=self_mrr,
        cascade_recall=cascade_recall,
        n_scenarios=len(breakdown),
        per_scenario=breakdown,
    )


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------

# Modes considered per kind during self_mrr sampling. Kept short — adding
# more modes increases evaluation time linearly.
_MODES_FOR_KIND: dict[str, tuple[str, ...]] = {
    "ic": ("dead", "regulating_low"),
    "passive_c": ("leaky_short",),
    "passive_r": ("open",),
    "passive_d": ("dead",),
    "passive_fb": ("open",),
    "passive_q": ("dead",),
}


def _make_failure(refdes: str, mode: str) -> Failure:
    """Construct a `Failure` honouring the mode-specific required fields.

    `leaky_short` requires `value_ohms`, `regulating_low` requires
    `voltage_pct`; the model validator raises otherwise. The evaluator
    samples one representative value per mode — tests pin behaviour,
    not the exact number, so defaults are deliberately simple.
    """
    if mode == "leaky_short":
        return Failure(refdes=refdes, mode=mode, value_ohms=200.0)
    if mode == "regulating_low":
        return Failure(refdes=refdes, mode=mode, voltage_pct=0.85)
    return Failure(refdes=refdes, mode=mode)


def _symptoms_from_timeline(timeline) -> dict:
    """Project a SimulationTimeline into the observation shape hypothesize
    consumes — dead rails + dead components + degraded rails."""
    last = timeline.states[-1] if timeline.states else None
    if last is None:
        return {"dead_rails": [], "dead_components": [], "degraded_rails": []}
    return {
        "dead_rails": [r for r, s in last.rails.items() if s in ("off", "shorted")],
        "dead_components": [c for c, s in last.components.items() if s == "dead"],
        "degraded_rails": [r for r, s in last.rails.items() if s == "degraded"],
    }


def _rank_candidates_for_symptoms(graph: ElectricalGraph, symptoms: dict) -> list[tuple[str, str]]:
    """Brute-force inverse — try every (refdes, mode), score by overlap of
    predicted vs observed symptoms, return ranked descending."""
    pairs: list[tuple[str, str]] = []
    for refdes in sorted(graph.components):
        kind = graph.components[refdes].kind or "ic"
        for mode in _MODES_FOR_KIND.get(kind, ("dead",)):
            pairs.append((refdes, mode))

    obs_dead_rails = set(symptoms.get("dead_rails") or [])
    obs_dead_comps = set(symptoms.get("dead_components") or [])
    obs_degraded_rails = set(symptoms.get("degraded_rails") or [])

    scored: list[tuple[float, tuple[str, str]]] = []
    for refdes, mode in pairs:
        try:
            tl = SimulationEngine(graph, failures=[_make_failure(refdes, mode)]).run()
        except Exception:
            continue
        last = tl.states[-1] if tl.states else None
        if last is None:
            scored.append((0.0, (refdes, mode)))
            continue
        pred_dead_rails = {r for r, s in last.rails.items() if s in ("off", "shorted")}
        pred_dead_comps = {c for c, s in last.components.items() if s == "dead"}
        pred_degraded_rails = {r for r, s in last.rails.items() if s == "degraded"}
        # Simple Jaccard sum across the three observation sets.
        s = (
            _jaccard(pred_dead_rails, obs_dead_rails)
            + _jaccard(pred_dead_comps, obs_dead_comps)
            + _jaccard(pred_degraded_rails, obs_degraded_rails)
        )
        scored.append((s, (refdes, mode)))

    scored.sort(key=lambda x: (-x[0], x[1]))
    return [pair for _, pair in scored]


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
