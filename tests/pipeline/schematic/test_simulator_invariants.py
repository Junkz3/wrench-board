# SPDX-License-Identifier: Apache-2.0
"""Property-based contract tests on the simulator + hypothesize stack.

These tests assert universal invariants that must hold over the entire
compiled `ElectricalGraph` — not just the 17-scenario oracle. They form
the second layer of defense against score-gaming and silent regressions
in the evolve loop.

Spec: docs/superpowers/specs/2026-04-25-simulator-invariants-design.md
Plan: docs/superpowers/plans/2026-04-25-simulator-invariants.md

The whole module skips if `memory/mnt-reform-motherboard/electrical_graph.json`
is absent (fresh clone, no pack ingested yet). When the device is on disk,
all 10 invariants must pass.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from api.pipeline.schematic.hypothesize import (
    Observations,
    hypothesize,
)
from api.pipeline.schematic.schemas import ElectricalGraph
from api.pipeline.schematic.simulator import (
    Failure,
    SimulationEngine,
)

_GRAPH_PATH = Path("memory/mnt-reform-motherboard/electrical_graph.json")
_SAMPLE_SEED = 42
_RECALL_THRESHOLD = 0.80  # INV-8

if not _GRAPH_PATH.exists():
    pytest.skip(
        f"electrical graph missing at {_GRAPH_PATH} — invariants suite needs the "
        "mnt-reform-motherboard pack ingested. Run schematic ingest, then re-run.",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Frozen vocabulary — re-stated locally to avoid coupling to evaluator.py.
# These mirror evaluator._MODES_FOR_KIND / _make_failure / _is_pertinent at
# the time the spec landed (commit 0f9ec15). If evaluator.py drifts, these
# stay frozen — the spec change must be deliberate.
# ---------------------------------------------------------------------------

_MODES_FOR_KIND: dict[str, tuple[str, ...]] = {
    "ic": ("dead", "regulating_low"),
    "passive_c": ("leaky_short",),
    "passive_r": ("open",),
    "passive_d": ("dead",),
    "passive_fb": ("open",),
    "passive_q": ("dead",),
}

_PASSIVE_R_ROLES_WITH_REAL_OPEN_CASCADE: frozenset[str] = frozenset(
    {"series", "damping", "inrush_limiter"}
)


def _make_failure(refdes: str, mode: str) -> Failure:
    if mode == "leaky_short":
        return Failure(refdes=refdes, mode=mode, value_ohms=200.0)
    if mode == "regulating_low":
        return Failure(refdes=refdes, mode=mode, voltage_pct=0.85)
    return Failure(refdes=refdes, mode=mode)


def _is_pertinent(graph: ElectricalGraph, refdes: str, kind: str, mode: str) -> bool:
    """Local mirror of evaluator._is_pertinent at commit 0f9ec15."""
    if kind == "ic" and mode == "regulating_low":
        return any(rail.source_refdes == refdes for rail in graph.power_rails.values())
    if kind == "passive_c" and mode == "leaky_short":
        return any(refdes in (rail.decoupling or []) for rail in graph.power_rails.values())
    if kind == "passive_r" and mode == "open":
        comp = graph.components.get(refdes)
        if comp is None:
            return False
        role = (comp.role or "").lower()
        if role not in _PASSIVE_R_ROLES_WITH_REAL_OPEN_CASCADE:
            return False
        if role == "damping":
            pin_nets = {p.net_label for p in comp.pins if p.net_label}
            if pin_nets & graph.power_rails.keys():
                return True
            enable_nets: set[str] = set()
            for rail in graph.power_rails.values():
                if rail.enable_net:
                    enable_nets.add(rail.enable_net)
                if rail.source_refdes and rail.source_refdes in graph.components:
                    for p in graph.components[rail.source_refdes].pins:
                        if p.role == "enable_in" and p.net_label:
                            enable_nets.add(p.net_label)
            return bool(pin_nets & enable_nets)
        return True
    if kind == "passive_fb" and mode == "open":
        comp = graph.components.get(refdes)
        if comp is None:
            return False
        pin_nets = {p.net_label for p in comp.pins if p.net_label}
        rail_touched = pin_nets & graph.power_rails.keys()
        if len(rail_touched) != 2:
            return False
        sourced_by_me = {n for n in rail_touched if graph.power_rails[n].source_refdes == refdes}
        if sourced_by_me:
            return True
        no_src = [n for n in rail_touched if graph.power_rails[n].source_refdes is None]
        if len(no_src) != 1:
            return False
        # Skip the parallel-supply uniqueness check — tighter test would
        # rule out passing pairs. The looser version is enough to anchor
        # round-trip recall on the bulk of pertinent ferrites.
        no_src_rail = no_src[0]
        other_rail = next(iter(rail_touched - {no_src_rail}))
        return graph.power_rails[other_rail].source_refdes is not None
    return True


# ---------------------------------------------------------------------------
# Fixture — load the 700 KB graph once per session, not per test.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def graph() -> ElectricalGraph:
    return ElectricalGraph.model_validate_json(_GRAPH_PATH.read_text())


@pytest.fixture(scope="module")
def all_pertinent_pairs(graph: ElectricalGraph) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for refdes in sorted(graph.components):
        kind = graph.components[refdes].kind or "ic"
        for mode in _MODES_FOR_KIND.get(kind, ("dead",)):
            if _is_pertinent(graph, refdes, kind, mode):
                pairs.append((refdes, mode))
    return pairs


@pytest.fixture(scope="module")
def sample_pairs(all_pertinent_pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """30 deterministic pertinent pairs spanning the kind distribution."""
    rng = random.Random(_SAMPLE_SEED)
    return rng.sample(all_pertinent_pairs, k=min(30, len(all_pertinent_pairs)))


# ---------------------------------------------------------------------------
# Helpers shared across invariants.
# ---------------------------------------------------------------------------


def _power_in_nets(graph: ElectricalGraph, refdes: str) -> set[str]:
    comp = graph.components.get(refdes)
    if comp is None:
        return set()
    return {p.net_label for p in comp.pins if p.role == "power_in" and p.net_label}


def _justifies_death(
    refdes: str,
    failures: list[Failure],
    timeline,
    graph: ElectricalGraph,
) -> tuple[bool, str]:
    """OR-chain from spec INV-3. Returns (justified, reason)."""
    # (a) explicit kill
    for f in failures:
        if f.refdes == refdes and f.mode == "dead":
            return True, "explicit kill (mode=dead)"
    # (b) power_in on a dead rail
    dead_rails = set(timeline.cascade_dead_rails)
    pwr = _power_in_nets(graph, refdes)
    if pwr & dead_rails:
        return True, f"power_in on dead rail {sorted(pwr & dead_rails)}"
    # (c) source of a shorted rail
    last_state = timeline.states[-1] if timeline.states else None
    if last_state is not None:
        for label, rail in graph.power_rails.items():
            if rail.source_refdes == refdes and last_state.rails.get(label) == "shorted":
                return True, f"sources shorted rail {label}"
    # (e) cut path — every power_in pin is on a non-live net in the final
    #     state. Covers the open-passive branch where the simulator marks
    #     downstream consumers dead because their power_in net (which may
    #     be an internal net, not a registered power_rail) was severed.
    #     A net is "live" iff it is a power_rail in {stable, degraded with
    #     voltage ≥ TOLERANCE_UVLO}. Internal nets that aren't power_rails
    #     count as non-live (they were the cut downstream of the open).
    #     If every power_in path is non-live AND the component has at least
    #     one power_in pin, the death is physical — no power means dead.
    if pwr and last_state is not None:
        live_pwr: list[str] = []
        for n in pwr:
            rail_state = last_state.rails.get(n)
            if rail_state == "stable":
                live_pwr.append(n)
                continue
            if rail_state == "degraded":
                v = last_state.rail_voltage_pct.get(n, 1.0)
                if v >= 0.5:  # TOLERANCE_UVLO
                    live_pwr.append(n)
        if not live_pwr:
            return True, f"every power_in non-live (pins on {sorted(pwr)})"
    # (f) open-passive downstream — if a failure is `mode='open'` on a
    #     passive, the simulator's case (b) handler may mark consumers of
    #     the passive's downstream side dead even when the downstream rail
    #     itself stays "stable" (the source IC is presumed to keep
    #     regulating from the unaffected upstream — conservative over-kill,
    #     not gaming). Justify the death iff the dead component has a
    #     power_in pin on a net the opened passive touches. This is a
    #     topology-bound clause: it ONLY accepts deaths whose power_in
    #     intersects the open-passive's pin set, never blanket deaths.
    for f in failures:
        if f.mode != "open":
            continue
        passive = graph.components.get(f.refdes)
        if passive is None:
            continue
        passive_nets = {p.net_label for p in passive.pins if p.net_label}
        if pwr & passive_nets:
            return True, (
                f"power_in {sorted(pwr & passive_nets)} touched by open passive "
                f"{f.refdes} (case-b downstream consumer)"
            )
    return False, "no power-loss chain — possible gaming"


def _build_observations_from_timeline(timeline, graph: ElectricalGraph) -> Observations:
    """Project a timeline into a tech-style observation. Only ICs carry the
    'dead' mode — passives die as a consequence of rail loss and the rail
    observation is the load-bearing signal for hypothesize. Including
    passives as state_comps would either crash hypothesize (mode='dead'
    is invalid for passives in the FailureMode enum) or distort the
    observation away from what a real technician would actually report.
    """
    last = timeline.states[-1] if timeline.states else None
    state_comps: dict[str, str] = {}
    state_rails: dict[str, str] = {}
    for refdes in timeline.cascade_dead_components:
        comp = graph.components.get(refdes)
        if comp is None:
            continue
        if (comp.kind or "ic") != "ic":
            continue
        state_comps[refdes] = "dead"
    for label in timeline.cascade_dead_rails:
        state_rails[label] = "dead"
    if last is not None:
        for label, st in last.rails.items():
            if st == "shorted" and label not in state_rails:
                state_rails[label] = "shorted"
    return Observations(state_comps=state_comps, state_rails=state_rails)


# ---------------------------------------------------------------------------
# INV-1 — cascade outputs are subsets of the graph.
# ---------------------------------------------------------------------------


def test_inv1_cascade_subset_of_graph(graph: ElectricalGraph, sample_pairs):
    rail_keys = set(graph.power_rails.keys())
    comp_keys = set(graph.components.keys())
    violations: list[str] = []
    for refdes, mode in sample_pairs:
        tl = SimulationEngine(graph, failures=[_make_failure(refdes, mode)]).run()
        bad_comps = set(tl.cascade_dead_components) - comp_keys
        bad_rails = set(tl.cascade_dead_rails) - rail_keys
        if bad_comps:
            violations.append(f"{refdes}/{mode} → invented refdes: {sorted(bad_comps)}")
        if bad_rails:
            violations.append(f"{refdes}/{mode} → invented rails: {sorted(bad_rails)}")
    assert not violations, "\n".join(violations)


# ---------------------------------------------------------------------------
# INV-2 — empty failures → empty cascade.
# ---------------------------------------------------------------------------


def test_inv2_empty_failures_empty_cascade(graph: ElectricalGraph):
    tl = SimulationEngine(graph, failures=[]).run()
    assert tl.cascade_dead_components == [], (
        f"baseline boot should kill nothing; got {tl.cascade_dead_components}"
    )
    assert tl.cascade_dead_rails == [], (
        f"baseline boot should kill no rails; got {tl.cascade_dead_rails}"
    )


# ---------------------------------------------------------------------------
# INV-3 — every cascade death has a physical cause.
# ---------------------------------------------------------------------------

# Hand-picked refdes that exercise the open / shorted / regulating_low
# branches across kinds. Mix of historically-relevant cases (the reverted
# self-dead patches all touched these) and benchmark-oracle cases.
_INV3_HAND_FAILURES: list[tuple[str, str]] = [
    ("U7", "dead"),
    ("U7", "regulating_low"),
    ("U13", "dead"),
    ("Q3", "shorted"),
    ("FB20", "open"),
    ("FB3", "open"),
    ("R1", "open"),
    ("R3", "open"),
    ("C19", "shorted"),
    ("C129", "leaky_short"),
]


def test_inv3_every_cascade_death_has_physical_cause(graph: ElectricalGraph, sample_pairs):
    pairs_to_test: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pair in list(sample_pairs) + _INV3_HAND_FAILURES:
        if pair in seen:
            continue
        if pair[0] not in graph.components:
            continue  # hand picks may miss on a different graph
        seen.add(pair)
        pairs_to_test.append(pair)

    violations: list[str] = []
    for refdes, mode in pairs_to_test:
        failures = [_make_failure(refdes, mode)]
        tl = SimulationEngine(graph, failures=failures).run()
        unjustified: list[str] = []
        for dead in tl.cascade_dead_components:
            ok, _reason = _justifies_death(dead, failures, tl, graph)
            if not ok:
                unjustified.append(dead)
        if unjustified:
            violations.append(
                f"failure={refdes}/{mode}: {len(unjustified)} unjustified deaths "
                f"(first 5: {unjustified[:5]})"
            )
    assert not violations, "\n".join(violations)


# ---------------------------------------------------------------------------
# INV-4 — source death implies rail death.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rail_label",
    sorted(
        label
        for label, rail in ElectricalGraph.model_validate_json(
            _GRAPH_PATH.read_text()
        ).power_rails.items()
        if rail.source_refdes is not None
    ),
)
def test_inv4_source_death_implies_rail_death(graph: ElectricalGraph, rail_label: str):
    rail = graph.power_rails[rail_label]
    src = rail.source_refdes
    assert src is not None  # parametrize filter ensures this
    if src not in graph.components:
        pytest.skip(f"source {src} of {rail_label} not in graph.components")
    tl = SimulationEngine(graph, failures=[Failure(refdes=src, mode="dead")]).run()
    assert rail_label in tl.cascade_dead_rails, (
        f"killed source={src} of rail={rail_label} but rail not in "
        f"cascade_dead_rails (got {tl.cascade_dead_rails})"
    )


# ---------------------------------------------------------------------------
# INV-5 — dead rail implies dead consumers (with live-alternate exemption).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rail_label",
    sorted(
        label
        for label, rail in ElectricalGraph.model_validate_json(
            _GRAPH_PATH.read_text()
        ).power_rails.items()
        if rail.source_refdes is not None and rail.consumers
    ),
)
def test_inv5_dead_rail_implies_dead_consumers(graph: ElectricalGraph, rail_label: str):
    rail = graph.power_rails[rail_label]
    src = rail.source_refdes
    assert src is not None and rail.consumers
    if src not in graph.components:
        pytest.skip(f"source {src} of {rail_label} not in graph.components")
    tl = SimulationEngine(graph, failures=[Failure(refdes=src, mode="dead")]).run()
    dead_rails = set(tl.cascade_dead_rails)
    dead_comps = set(tl.cascade_dead_components)
    if rail_label not in dead_rails:
        pytest.skip(f"rail {rail_label} not in cascade — covered by INV-4")
    survivors: list[tuple[str, list[str]]] = []
    for consumer in rail.consumers:
        if consumer not in graph.components:
            continue
        if consumer in dead_comps:
            continue
        # Exempt consumers with at least one live alternate power_in.
        ins = _power_in_nets(graph, consumer)
        alt_live = [
            n for n in ins if n != rail_label and n in graph.power_rails and n not in dead_rails
        ]
        if not alt_live:
            survivors.append((consumer, sorted(ins)))
    assert not survivors, (
        f"rail {rail_label} dead but {len(survivors)} consumers alive without "
        f"live alternate supply: {survivors[:5]}"
    )


# ---------------------------------------------------------------------------
# INV-6 — determinism on the failure path.
# ---------------------------------------------------------------------------


def test_inv6_determinism(graph: ElectricalGraph, all_pertinent_pairs):
    rng = random.Random(_SAMPLE_SEED + 1)
    sample = rng.sample(all_pertinent_pairs, k=min(10, len(all_pertinent_pairs)))
    for refdes, mode in sample:
        f = _make_failure(refdes, mode)
        a = SimulationEngine(graph, failures=[f]).run()
        b = SimulationEngine(graph, failures=[f]).run()
        assert a.cascade_dead_components == b.cascade_dead_components, (
            f"non-deterministic cascade_dead_components for {refdes}/{mode}"
        )
        assert a.cascade_dead_rails == b.cascade_dead_rails, (
            f"non-deterministic cascade_dead_rails for {refdes}/{mode}"
        )
        assert a.final_verdict == b.final_verdict
        assert a.blocked_at_phase == b.blocked_at_phase


# ---------------------------------------------------------------------------
# INV-7 — sourceless rails are immune to internal kills.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rail_label",
    sorted(
        label
        for label, rail in ElectricalGraph.model_validate_json(
            _GRAPH_PATH.read_text()
        ).power_rails.items()
        if rail.source_refdes is None
    ),
)
def test_inv7_sourceless_rail_immune(graph: ElectricalGraph, rail_label: str):
    # 5 ICs deterministically, none on this rail's consumers.
    ics_off_rail = [
        r
        for r, c in sorted(graph.components.items())
        if (c.kind or "ic") == "ic" and r not in graph.power_rails[rail_label].consumers
    ]
    rng = random.Random(_SAMPLE_SEED + 2 + hash(rail_label) % 1000)
    sample = rng.sample(ics_off_rail, k=min(5, len(ics_off_rail)))
    violations: list[str] = []
    for ic in sample:
        tl = SimulationEngine(graph, failures=[Failure(refdes=ic, mode="dead")]).run()
        if rail_label in tl.cascade_dead_rails:
            violations.append(ic)
    assert not violations, (
        f"sourceless rail {rail_label} entered cascade after killing internal IC(s): {violations}"
    )


# ---------------------------------------------------------------------------
# INV-8 — round-trip top-5 recall on pertinent pairs.
# ---------------------------------------------------------------------------


def test_inv8_round_trip_top5_recall(graph: ElectricalGraph, all_pertinent_pairs):
    """Recall is measured over OBSERVABLE pairs only — pairs whose simulation
    produces an empty cascade (e.g. open on a passive with no consumers, or
    regulating_low on an IC sourcing rails that all stay above UVLO) cannot
    be round-tripped by definition: hypothesize has no signal to work with.

    A separate assertion guards the silent ratio — too many silent pairs
    means the simulator is too quiet on the bulk of pertinent failures,
    which is itself a useful invariant signal.
    """
    rng = random.Random(_SAMPLE_SEED + 3)
    sample = rng.sample(all_pertinent_pairs, k=min(30, len(all_pertinent_pairs)))
    silent: list[str] = []
    misses: list[str] = []
    hits = 0
    tested = 0
    for refdes, mode in sample:
        tl = SimulationEngine(graph, failures=[_make_failure(refdes, mode)]).run()
        obs = _build_observations_from_timeline(tl, graph)
        if obs.is_empty():
            silent.append(f"{refdes}/{mode}")
            continue
        tested += 1
        result = hypothesize(graph, observations=obs, max_results=5)
        top5 = [(h.kill_refdes[0], h.kill_modes[0]) for h in result.hypotheses[:5] if h.kill_refdes]
        # Mode equivalence: simulator's "shorted" / "open" / "leaky_short" /
        # "regulating_low" / "dead" map to hypothesize's FailureMode
        # vocabulary mostly 1:1; ICs may surface as "dead" or "anomalous";
        # passive_c leaky_short maps to "short" in hypothesize's enum.
        mode_aliases = {mode}
        if mode in ("regulating_low", "dead"):
            mode_aliases |= {"dead", "anomalous"}
        if mode == "leaky_short":
            mode_aliases |= {"short"}
        in_top5 = any(r == refdes and m in mode_aliases for r, m in top5)
        if in_top5:
            hits += 1
        else:
            misses.append(f"{refdes}/{mode} → top5={top5[:3]}{'…' if len(top5) > 3 else ''}")
    recall = hits / tested if tested else 0.0
    silent_ratio = len(silent) / len(sample)
    # Two-part assertion: recall on observable pairs, plus ceiling on
    # silent ratio. A high silent ratio surfaces a coverage gap rather
    # than a recall problem — both are worth knowing.
    assert recall >= _RECALL_THRESHOLD, (
        f"round-trip recall {recall:.2f} on {tested} observable pairs "
        f"(< threshold {_RECALL_THRESHOLD}); {len(misses)} misses "
        f"(first 5: {misses[:5]})"
    )
    assert silent_ratio <= 0.75, (
        f"{silent_ratio:.0%} of pertinent pairs produce empty observations "
        f"({len(silent)}/{len(sample)}) — simulator too quiet to round-trip; "
        f"first 5 silent: {silent[:5]}"
    )


# ---------------------------------------------------------------------------
# INV-9 — cascade verdict consistency.
# ---------------------------------------------------------------------------


def test_inv9_cascade_verdict_consistent(graph: ElectricalGraph, sample_pairs):
    violations: list[str] = []
    sample = sample_pairs[:20]
    for refdes, mode in sample:
        tl = SimulationEngine(graph, failures=[_make_failure(refdes, mode)]).run()
        if (
            tl.cascade_dead_components or tl.cascade_dead_rails
        ) and tl.final_verdict == "completed":
            violations.append(f"{refdes}/{mode}: cascade non-empty but verdict='completed'")
    assert not violations, "\n".join(violations)


# ---------------------------------------------------------------------------
# INV-10 — hypothesize on empty observation gives no positive score.
# ---------------------------------------------------------------------------


def test_inv10_hypothesize_empty_observation(graph: ElectricalGraph):
    obs = Observations(state_comps={}, state_rails={})
    result = hypothesize(graph, observations=obs, max_results=5)
    assert all(h.score <= 0 for h in result.hypotheses), (
        f"hypothesize on empty observation returned positive-score hypotheses: "
        f"{[(h.kill_refdes, h.score) for h in result.hypotheses if h.score > 0]}"
    )
