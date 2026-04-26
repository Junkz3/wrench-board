# Plan — Simulator Invariants Implementation

**Date:** 2026-04-25
**Spec:** `docs/superpowers/specs/2026-04-25-simulator-invariants-design.md`
**Status:** Executing autonomously
**Branch:** main (worktree principal)

## Context

The evolve runner is stopped. We're committing on `main` directly — these are tests, not changes to the simulator's logic, and the simulator is already at the post-evaluator-fix state (commit `0f9ec15`).

The plan respects the spec's 10 invariants and ships a single test file plus one documentation update. No simulator/hypothesize code change. No new dependencies.

## Tasks

### T1 — Test infrastructure (graph fixture, helpers)

**File:** `tests/pipeline/schematic/test_simulator_invariants.py` (new)

- Module-level `pytest.skip(...)` if `memory/mnt-reform-motherboard/electrical_graph.json` is absent. Keeps fresh-clone CI green.
- Session-scoped fixture `graph()` that loads + parses the electrical graph **once** for the whole module.
- Helper `_modes_for_kind(kind: str) -> tuple[str, ...]` — same vocabulary as `evaluator._MODES_FOR_KIND` (cannot import directly without coupling — re-state as the spec freezes it).
- Helper `_make_failure(refdes, mode) -> Failure` — same logic as `evaluator._make_failure` (defaults: leaky_short → 200 Ω, regulating_low → 0.85). Re-stated for the same reason.
- Helper `_justifies_death(refdes, failures, timeline, graph) -> tuple[bool, str]` — the OR-chain from INV-3. Returns (justified, reason) so the test can report which clause matched.
- Constant `_SAMPLE_SEED = 42` for any random sampling — determinism is a hard requirement.
- Marker registration in `pyproject.toml`: `invariants` (added if missing).

**Acceptance:** module imports without error; `pytest --collect-only tests/pipeline/schematic/test_simulator_invariants.py` lists 10 tests; first invocation of `graph()` takes <1 s.

### T2 — INV-1 cascade subset of graph

Iterate `(refdes, mode)` over a deterministic 30-component sample (sorted refdes, take every Nth so kinds are mixed). For each, run `SimulationEngine(graph, failures=[failure]).run()`, assert subset.

Report violations as `f"failure={refdes}/{mode} produced {orphans} not in graph"`.

**Acceptance:** test passes on `0f9ec15`. Wall <1.5 s.

### T3 — INV-2 empty failures empty cascade

One assertion: `SimulationEngine(graph, failures=[]).run().cascade_dead_components == []` and same for rails.

**Acceptance:** test passes. Wall <100 ms.

### T4 — INV-3 every cascade death has a physical cause (THE bedrock)

For each `(refdes, mode)` in the same 30-component sample as T2, plus 10 hand-picked failures known to exercise the open / shorted / regulating_low branches (using the reverted-commit refdes — `U7`, `Q3`, `FB20`, `R1`, `C19`, `R3`, `U13`, `R34`, `R113`, `C7`):

- run the simulator
- for each refdes in `cascade_dead_components`, call `_justifies_death(refdes, failures, timeline, graph)`
- collect all unjustified refdes with their failure context
- assert the collection is empty, with a message listing every violation in priority order

The OR-chain in `_justifies_death`:

```
(a) refdes appears in failures with mode='dead'
(b) refdes has a power_in pin on a rail in cascade_dead_rails
(c) refdes is the source_refdes of a rail with last_state.rails[label] == 'shorted'
    (own rail-to-GND pulled it down)
(d) refdes appears in failures with mode in {'open', 'regulating_low', 'leaky_short'}
    AND simulator's documented branch for that mode marks the component dead
    (currently: open on a passive whose downstream nets carry no other supply)
```

Clauses are tried in order; first match wins; reason is recorded.

**Acceptance:** test passes on `0f9ec15`. If it fails, the violation list IS the bug report — keep it as `xfail` and file an entry in `weaknesses.md` with the offending (refdes, mode, unjustified_dead). Wall <2 s.

### T5 — INV-4 source death implies rail death

For each rail with `source_refdes is not None` (26 rails on mnt-reform):

- run `SimulationEngine(graph, failures=[Failure(source_refdes, 'dead')])`
- assert the rail label is in `cascade_dead_rails`

If a rail fails, the failure message includes `source_refdes`, the cascade output, and the rail's `consumers` list — enough to debug.

**Acceptance:** test passes. Wall <2 s.

### T6 — INV-5 dead rail implies dead consumers

For each sourced rail with consumers (~33 candidates):

- kill the source IC
- for each consumer `C` of the rail:
  - if `C ∈ cascade_dead_components`, OK
  - else assert `C` has a `power_in` on **another** live rail (post-cascade)

Failure surfaces the consumer refdes + its `power_in` pin set + which rails are live.

**Acceptance:** test passes. Wall <2 s.

### T7 — INV-6 determinism

Sample 10 (refdes, mode) pairs (deterministic). For each, run twice. Assert `cascade_dead_components`, `cascade_dead_rails`, `verdict`, `blocked_at_phase` all equal across runs.

**Acceptance:** test passes. Wall <1 s.

### T8 — INV-7 sourceless rails immune to internal kills

For each sourceless rail (26 candidates):

- pick 5 ICs deterministically (sorted refdes, every Nth) that are NOT consumers of this rail and are NOT the rail itself
- for each IC, kill it, assert the rail is NOT in `cascade_dead_rails`

If the rail enters cascade_dead_rails, log which IC kill caused it and which rail consumers / source structure made the simulator think the input died.

**Acceptance:** test passes. Wall <2 s.

### T9 — INV-8 round-trip top-5 recall

Sample 30 (refdes, mode) pairs that pass `evaluator._is_pertinent` (use a local copy of the predicate to avoid coupling). For each:

- simulate to get a `SimulationTimeline`
- build `Observations` from the timeline's last state:
  - `state_rails[label] = "dead"` for each rail in `cascade_dead_rails`
  - `state_comps[refdes] = "dead"` for each refdes in `cascade_dead_components`
- call `hypothesize(graph, observations)`
- collect top-5 hypotheses by score
- assert the (refdes, mode) tuple appears in top-5 hypothesized

Recall <100% is OK if it's a small fraction; test asserts ≥ 80% (i.e. ≥ 24/30). If recall is below 80%, surface the missed pairs as a list — that's the diagnostic signal.

**Acceptance:** ≥ 80% recall on `0f9ec15`. If lower, file the missed pairs in `weaknesses.md` and relax the bound. Wall <30 s (most expensive test — `hypothesize` runs many sub-simulations).

### T10 — INV-9 cascade verdict consistency

Sample 20 (refdes, mode) pairs. For each, assert `not (cascade_dead_* and verdict == 'completed')`.

**Acceptance:** test passes. Wall <2 s.

### T11 — INV-10 hypothesize empty observation gives no positive score

`hypothesize(graph, Observations(state_comps={}, state_rails={}))` → assert either result is empty OR every hypothesis has score ≤ 0.

**Acceptance:** test passes. Wall <5 s (one big call to hypothesize).

### T12 — Pytest marker registration

If `pyproject.toml` has a `[tool.pytest.ini_options].markers` section, append `"invariants: simulator/hypothesize property-based contract tests"`. Otherwise leave default `make test` behaviour (no marker filter on this file).

**Acceptance:** `pytest --markers` lists `invariants`.

### T13 — `benchmark/weaknesses.md` reference card

Append a new section at the top:

```markdown
## INVARIANTS — universal properties the simulator MUST satisfy

The 10 invariants below are enforced by `tests/pipeline/schematic/test_simulator_invariants.py`. They run in `make test` (≈10 s wall). The evolve loop MUST pass them on every keep-commit; CI rejects otherwise.

(brief one-line summary per INV-N + pointer to the spec)
```

This puts the contract under the agent's nose every time the skill loads `weaknesses.md`. The full reference is the spec.

**Acceptance:** new section is the first major heading after the intro paragraph.

### T14 — End-to-end verification

```bash
.venv/bin/pytest tests/pipeline/schematic/test_simulator_invariants.py -v
.venv/bin/pytest tests/ -m "not slow"
.venv/bin/python scripts/eval_simulator.py --device mnt-reform-motherboard --verbose | head -1
```

All three must succeed. The eval score must remain `0.9321130952380953` exactly (we made no logic change).

**Acceptance:** all green.

### T15 — Single commit

Conventional-commits message: `test(invariants): 10 property-based contract tests on simulator + hypothesize`.

Body: short rationale (~5 lines), pointer to spec + plan, summary of any `xfail` invariants surfaced, runtime budget achieved.

Files committed:
- `tests/pipeline/schematic/test_simulator_invariants.py` (new)
- `pyproject.toml` (marker registration only, optional)
- `benchmark/weaknesses.md` (INVARIANTS section)
- `docs/superpowers/specs/2026-04-25-simulator-invariants-design.md`
- `docs/superpowers/plans/2026-04-25-simulator-invariants.md`

**No** `git push`. Local commit only. Co-authored trailer per repo convention.

## Risk register

- **Round-trip INV-8 may fail at high recall threshold.** Mitigation: ship at 80% with documented missed pairs; if real recall is below 50%, file as `xfail` and surface as a `weaknesses.md` P-something to the evolve loop.
- **INV-3 may surface a legitimate but undocumented branch in `_apply_failures_at_init`** (an open-handler edge case). Mitigation: if so, add the branch to the OR-chain (case (d)) with a comment citing the simulator code line, not silently weaken the invariant.
- **Slow runtime risk.** Hypothesize is the one expensive call (single-fault enumerates ~280 candidates × ~10 ms each = 2.8 s per call). T9 + T11 = 31 calls = ~90 s if naive. Mitigation: T9 runs at most 30 round-trips (≈ 1.5 min worst case), and the spec's <10 s budget for the full module is achieved by limiting T9's sample size.

  Actually: re-reading the budget, T9 alone could blow the <10 s budget. Resolution: the per-test wall budgets in T9/T11 are **soft** — if the suite truly takes 90 s, mark T9 as `@pytest.mark.slow` so it only runs in `make test-all`. The other 9 tests stay in `make test` and complete in <10 s. Will be decided at implementation time based on measured wall.

## Out of plan

- No simulator/hypothesize code change.
- No evolve worktree fast-forward (Alexis will decide whether to relaunch the runner; if he does, the worktree fast-forwards as before).
- No state.json / results.tsv mutation. Monitor cursor stays where it is.
