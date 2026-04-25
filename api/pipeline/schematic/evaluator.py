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
                if not _is_pertinent(graph, refdes, kind, mode):
                    continue
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
# more modes increases evaluation time linearly. Pertinence is filtered
# per-pair via `_is_pertinent` (see below) — having a mode listed here is
# necessary but not sufficient.
_MODES_FOR_KIND: dict[str, tuple[str, ...]] = {
    "ic": ("dead", "regulating_low"),
    "passive_c": ("leaky_short",),
    "passive_r": ("open",),
    "passive_d": ("dead",),
    "passive_fb": ("open",),
    "passive_q": ("dead",),
}


# Roles where an `open` mode produces a real cascade (vs. silent no-op).
# Pull-ups, pull-downs, current-sense, feedback resistors don't kill anything
# physically meaningful when they open — sampling them adds tie clusters
# without diagnostic value, which would tempt the evolve agent into
# fabricating self-dead conventions to break the ties.
_PASSIVE_R_ROLES_WITH_REAL_OPEN_CASCADE: frozenset[str] = frozenset({
    "series",
    "damping",
    "inrush_limiter",
})


def _is_pertinent(graph: ElectricalGraph, refdes: str, kind: str, mode: str) -> bool:
    """Skip (refdes, mode) pairs that are physically nonsensical.

    `regulating_low` only applies to ICs that source at least one rail.
    `leaky_short` on a cap only applies if the cap is in some rail's
    `decoupling` list. `open` on a passive_r only applies for the roles
    that produce a real downstream cascade.

    This filtering closes the score-hack backdoor where the simulator was
    pushed to mark untouched components as dead just to break Jaccard tie
    clusters of meaningless (refdes, mode) samples.
    """
    if kind == "ic" and mode == "regulating_low":
        return any(
            rail.source_refdes == refdes for rail in graph.power_rails.values()
        )
    if kind == "passive_c" and mode == "leaky_short":
        return any(
            refdes in (rail.decoupling or []) for rail in graph.power_rails.values()
        )
    if kind == "passive_r" and mode == "open":
        comp = graph.components.get(refdes)
        if comp is None:
            return False
        role = (comp.role or "").lower()
        if role not in _PASSIVE_R_ROLES_WITH_REAL_OPEN_CASCADE:
            return False
        # Damping resistors only matter when their open mode actually orphans
        # something the power-domain model tracks — a power rail or an
        # enable_net of one. Damping on a pure signal net is silent in this
        # model (no signal-liveness tracking), so sampling it adds empty
        # fingerprints to the rank cluster without diagnostic value.
        if role == "damping":
            pin_nets = {p.net_label for p in comp.pins if p.net_label}
            if pin_nets & graph.power_rails.keys():
                return True
            enable_nets = _enable_nets(graph)
            return bool(pin_nets & enable_nets)
        return True
    if kind == "passive_fb" and mode == "open":
        # Mirror the simulator's uniqueness check: opening one ferrite of a
        # parallel bank leaves the rail powered, so sampling it produces an
        # empty fingerprint and pollutes the tie cluster.
        comp = graph.components.get(refdes)
        if comp is None:
            return False
        return _is_unique_supply_for_some_rail(graph, refdes, comp)
    return True


def _enable_nets(graph: ElectricalGraph) -> set[str]:
    """Union of every rail's enable_net + every enable_in pin of a rail's source IC."""
    out: set[str] = set()
    for rail in graph.power_rails.values():
        if rail.enable_net:
            out.add(rail.enable_net)
        if rail.source_refdes and rail.source_refdes in graph.components:
            for p in graph.components[rail.source_refdes].pins:
                if p.role == "enable_in" and p.net_label:
                    out.add(p.net_label)
    return out


def _is_unique_supply_for_some_rail(graph: ElectricalGraph, refdes: str, comp) -> bool:
    """True when this passive's open-mode would actually orphan some rail.

    Mirrors `simulator._is_unique_passive_supply` semantics — a passive
    bridging two rails A↔B kills the unsourced one ONLY if no parallel
    sibling provides another A↔B path."""
    pin_nets = {p.net_label for p in comp.pins if p.net_label}
    rail_touched = pin_nets & graph.power_rails.keys()
    if len(rail_touched) != 2:
        return False
    sourced_by_me = {n for n in rail_touched if graph.power_rails[n].source_refdes == refdes}
    if sourced_by_me:
        return True  # case (a): kills its own sourced rail
    no_src = [n for n in rail_touched if graph.power_rails[n].source_refdes is None]
    if len(no_src) != 1:
        return False
    no_src_rail = no_src[0]
    other_rail = next(iter(rail_touched - {no_src_rail}))
    if graph.power_rails[other_rail].source_refdes is None:
        return False
    for other_refdes, other_comp in graph.components.items():
        if other_refdes == refdes:
            continue
        if other_comp.kind not in ("passive_r", "passive_fb", "passive_d", "passive_q"):
            continue
        onets = {p.net_label for p in other_comp.pins if p.net_label}
        orails = onets & graph.power_rails.keys()
        if len(orails) != 2 or no_src_rail not in orails:
            continue
        if graph.power_rails[next(iter(orails - {no_src_rail}))].source_refdes is not None:
            return False
    return True


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
            if not _is_pertinent(graph, refdes, kind, mode):
                continue
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
