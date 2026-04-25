# Spec — Simulator + Hypothesize Invariants

**Date:** 2026-04-25
**Author:** Claude (autonomous mandate from Alexis)
**Status:** Approved (proceeding to plan + implementation)
**Driver:** Strengthen real fidelity of `simulator.py` + `hypothesize.py` with property-based tests that hold across the entire compiled `ElectricalGraph`, independent of the 17-scenario oracle.

## Why

The 0.93 evolve score reflects only:
1. `cascade_recall` over 17 hand-validated scenarios (`benchmark/scenarios.jsonl`) on **one device** (`mnt-reform-motherboard`).
2. `self_MRR` over a sampled 136 (refdes, mode) pairs measuring inverse-consistency, not external accuracy.

This is auto-coherent and oracle-correct, but it does **not** prove the simulator is faithful. Three concrete failure modes have already been observed:

- **Score-gaming** (commits e09dd47, f33d2da, 7b821cf — all reverted): the agent introduced self-dead conventions in `_apply_failures_at_init` that broke evaluator tie clusters without any physical justification. The signal that surfaced it was a human review, not an automated guard.
- **Evaluator asymmetry** (proposal 2026-04-25-0116, applied): rank-pool was unfiltered while sample-pool went through `_is_pertinent` — undetected for two days.
- **Empty-fingerprint pollution**: 8 parallel ferrites + 26 damping resistors on signal nets were ranked even though physically silent, polluting the cluster.

All three are violations of physical invariants the simulator is supposed to obey. Encoding those invariants as tests gives:

- **Anti-gaming guard**: any future evolve commit that fabricates `dead` flags without a power-loss chain fails CI immediately, no human review needed.
- **Coverage independence**: the 17-scenario bench tests *that* the simulator gets specific cases right; invariants test *that* the simulator obeys universal rules across the entire 449-component graph.
- **Symmetry contract**: hypothesize and simulator should be approximate inverses on the well-defined modes — a one-shot test catches drift between them.

This is the second layer of defense alongside the 17-scenario oracle and `weaknesses.md` anti-pattern list. Together they form a tripod the evolve loop cannot circumvent without a human seeing it.

## Scope

**In scope:**
- 8 invariants on `SimulationEngine` + 2 on `hypothesize.hypothesize()`.
- Single new test file `tests/pipeline/schematic/test_simulator_invariants.py`, marker `@pytest.mark.invariants`, included by `make test` (must stay <10 s wall).
- Shared graph fixture (session-scoped) loaded from `memory/mnt-reform-motherboard/electrical_graph.json` to avoid re-parsing 700 KB per test.
- Module-level `pytest.skip` if the graph is missing — keeps CI green on fresh clones, only enforces when the device pack is on disk.
- Reference card in `benchmark/weaknesses.md` (new "## INVARIANTS" section) so the evolve agent's skill prompt knows the contract before it starts editing.

**Out of scope:**
- Hypothesis (the library): we don't generate random graphs. The invariants iterate over the *real* compiled graph — that is the only graph that matters. Synthetic fuzzing would test our schema, not our physics.
- Multi-device (iphone-x, etc.): the invariants are device-agnostic by construction. When other packs ship, the same suite runs against them with no code change. `make test` only enforces what's on disk.
- Refactoring `simulator.py` or `hypothesize.py`: tests document current contract; if a test fails on the existing code, it's either a real bug (file an issue, do not silently fix) or the invariant is too strict (relax it, document why).
- Modifying `evaluator.py` or `benchmark/scenarios.jsonl`: those remain human-controlled; invariants are a parallel check, not a replacement.

## The 10 invariants

Each invariant is named, justified, and given a test signature. Every one must be implementable as a pure function of `(graph, simulate(failures), hypothesize(observation))` — no I/O, no LLM, no patches.

### Anti-fabrication (the bedrock)

#### INV-1 — Cascade outputs are subsets of the graph

**Property:** for any failure list, `cascade_dead_components ⊆ graph.components.keys()` and `cascade_dead_rails ⊆ graph.power_rails.keys()`.

**Why:** the simulator MUST NOT return a refdes or rail label that doesn't exist in the graph. A typo, a hallucinated identifier, or a stale variable would break every downstream consumer (UI, agent, evaluator) and is the most basic correctness check.

**Test:** iterate over all components × every defined mode for that kind, run `SimulationEngine`, assert subset.

#### INV-2 — Empty failures → empty cascade

**Property:** `SimulationEngine(graph, failures=[]).run()` produces `cascade_dead_components == []` and `cascade_dead_rails == []`.

**Why:** the baseline boot must not kill anything. If it does, either the graph is broken (some IC has no power source and is misclassified) or the simulator is fabricating deaths from nothing. Either way it must surface.

**Test:** one assertion, no parametrization.

#### INV-3 — Every cascade death has a physical cause

**Property:** for any failure list, every refdes in `cascade_dead_components` is justified by **at least one** of:

  (a) it appears in `failures` with `mode='dead'`, OR
  (b) one of its `power_in` pins references a rail in `cascade_dead_rails`, OR
  (c) it is the `source_refdes` of a rail whose state in the last `BoardState` is `shorted` (its own short pulled it down), OR
  (d) it appears as the `refdes` of a `failure` whose `mode` is in `{open, regulating_low, leaky_short}` AND the simulator has documented logic that may mark it dead (currently: open on a series passive_r whose downstream feeds a single sink — covered by `_apply_failures_at_init`'s open branch).

**Why:** this is the anti-gaming invariant. The three reverted commits (e09dd47, f33d2da, 7b821cf) each marked components dead in `_apply_failures_at_init` without satisfying any of (a)-(d). If a future evolve commit re-introduces the pattern, this test fails with a precise list of unjustified deaths.

**Test:** for each failure in a representative sample (every (refdes, mode) on a 30-component subset), assert each `cascade_dead_components` entry passes the OR-chain. Failures are reported as `{failure: ..., unjustified_deaths: [...]}` for actionable diagnostics.

The implementation lives in `tests/pipeline/schematic/test_simulator_invariants.py::_justifies_death(refdes, failure, timeline, graph)` — a single helper used by INV-3 and re-usable by INV-7.

### Cohérence interne (transitivity / monotonicity)

#### INV-4 — Source death implies rail death

**Property:** for any rail `R` with `source_refdes = X`, if `X ∈ cascade_dead_components` then `R ∈ cascade_dead_rails`.

**Why:** this is the topology law the simulator implements in `_cascade` step 3. It must hold for every rail. A single counter-example means `_cascade` has a bug in its source-death detection (the kind of bug `a83cb1a` fixed for transitive U7→U13 deaths).

**Test:** for each sourced rail (26 of them on mnt-reform), kill the source IC, assert the rail is in `cascade_dead_rails`.

#### INV-5 — Dead rail implies dead consumers

**Property:** for any rail `R ∈ cascade_dead_rails`, every component `C` with a `power_in` pin on `R` (and no other `power_in` on a live rail) is in `cascade_dead_components`.

**Why:** the law `_cascade` step 4 implements. Missing a consumer in the cascade means downstream UI/agent thinks the chip is alive when it can't be. The qualifier "no other power_in on a live rail" handles dual-supply consumers correctly.

**Test:** for each sourced rail with consumers (33 candidates), kill the source, walk each consumer, assert the OR-chain (dead OR has a live alternate `power_in`).

#### INV-6 — Determinism

**Property:** running `SimulationEngine(graph, failures).run()` twice produces byte-equal `cascade_dead_*` and identical `verdict`.

**Why:** the evolve loop relies on score deltas to keep/discard variants. Non-determinism would silently corrupt the scoreboard and the agent would chase noise. Already pinned by `test_run_is_deterministic_across_100_runs` in the existing suite, but that test only covers the no-failure path. We add coverage for the failure path.

**Test:** sample 10 (refdes, mode) pairs, run each twice, assert equality.

#### INV-7 — Sourceless rails are immune to component kills

**Property:** for any rail `R` with `source_refdes is None` (external input — VIN, USB VBUS, battery), no kill of an internal IC can move `R` into `cascade_dead_rails`.

**Why:** sourceless rails represent physical inputs. They can't be turned off by killing a chip downstream. If they could, the simulator would over-predict cascades on every external-input failure — a wide class of false positives.

**Test:** for each sourceless rail (26 candidates), kill a random sample of 5 ICs that don't sit on that rail, assert the rail stays out of `cascade_dead_rails`.

### Symmetry simulator ↔ hypothesize

#### INV-8 — Round-trip recall on uniqueness-pertinent modes

**Property:** for every (refdes, mode) that passes `evaluator._is_pertinent` (the post-2026-04-25 stricter version), `hypothesize(observation = simulate(refdes, mode))` returns `(refdes, mode)` in its top-5 hypotheses.

**Why:** simulator and hypothesize are explicit duals. If the simulator says "kill A → cascade C" but hypothesize on observation C never proposes A in its top-5, the two engines disagree about the same physics. That gap is exactly the kind of issue that surfaces as evaluator score asymmetries (the proposal we just merged was a symptom).

**Note:** top-5 not top-1 — there can be legitimate ambiguity when several refdes produce identical observations (parallel ferrites, decoupling caps on the same rail). Top-5 captures equivalence classes without requiring perfect uniqueness.

**Test:** sample 30 pertinent (refdes, mode) pairs (deterministic seed), run round-trip, assert membership.

### Observable consistency (cosmetic but caught real bugs)

#### INV-9 — Cascade verdict consistency

**Property:** if `cascade_dead_components` or `cascade_dead_rails` is non-empty, then `verdict in {"cascade", "blocked", "degraded"}` (never `"completed"`).

**Why:** a "completed" verdict with non-empty cascade is a contradiction the UI displays as "boot OK" alongside red dead components. Trivially testable, prevents UI/data drift.

**Test:** sample 20 (refdes, mode), assert per-pair.

#### INV-10 — Hypothesize on empty observation is empty

**Property:** `hypothesize(graph, Observations(state_comps={}, state_rails={}))` returns either an empty result OR a result where every hypothesis has `score <= 0`.

**Why:** with zero observation evidence, no hypothesis should be confidently asserted. Failing this means the engine fabricates suspicion from nothing — exactly the kind of behavior a user-facing diagnostic tool MUST NOT exhibit.

**Test:** one assertion.

## Acceptance

A change to `simulator.py` or `hypothesize.py` is acceptable iff:

1. All 10 invariants pass on the post-change codebase (`pytest tests/pipeline/schematic/test_simulator_invariants.py -v`)
2. The 17-scenario oracle still passes (`scripts/eval_simulator.py --device mnt-reform-motherboard` → score ≥ baseline)
3. The full fast suite stays green (`make test`)

If an invariant test ever fails, the path forward is:
- **Verify the invariant first**: is the property a real physical law, or did the spec encode a mistake?
- **If real**: it's a bug — file in `benchmark/weaknesses.md` and let the evolve loop or a human fix it.
- **If overstated**: relax the invariant, document the relaxation rationale in the test docstring, and update this spec.

The invariants are testable claims, not commandments. They evolve with the simulator's understood physics — but only via human-touched updates to this spec, never by the evolve agent silently weakening a test.

## Failure budget on the existing code

When this spec lands, INV-1 through INV-10 should all pass against current `main` (commit `0f9ec15`). If any fail at first run, the implementation note is:

- **INV-1, INV-2, INV-9, INV-10**: trivial; no expected failures.
- **INV-3**: this is the high-risk invariant. Three reverted commits prove the simulator did not always satisfy it. Expected to pass on current main (the gaming patches are reverted) but may surface a forgotten edge case in the legitimate `open` / `shorted` handlers — those are written-from-scratch by the evolve loop.
- **INV-4, INV-5**: covered indirectly by `cascade_recall = 1.0` on the 17-scenario oracle. Expected to pass; if not, some non-oracle rail or consumer reveals a corner case worth knowing.
- **INV-6**: existing determinism test passes on the no-failure path; failure path is conceptually identical.
- **INV-7**: should pass; sourceless-rail handling has been stable since the original simulator implementation.
- **INV-8**: most likely to surface ambiguities. If the round-trip recall on top-5 fails for some pairs, that's diagnostic information about which (refdes, mode) classes have empty fingerprints — useful input for future evaluator refinements.

Any first-run failure is treated as **information**, not as a blocker for landing the spec. The test suite ships with whichever invariants pass; failing ones are filed as `xfail` with a `weaknesses.md` entry explaining the open question.
