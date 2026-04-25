# Simulator weaknesses — priorities for the evolve loop

Live list of known simulator / hypothesize gaps, sorted by impact on the
scoring oracle. Updated by the human (morning review) and by the evolve
agent (moves resolved items to RESOLVED after a keep-commit).

Format per item: **[Pn] refdes/mode — short diagnosis** · *file:line pointer to where the fix likely lives*

---

## INVARIANTS — universal properties the simulator MUST satisfy

The 10 invariants below are enforced by `tests/pipeline/schematic/test_simulator_invariants.py` and run in `make test` (≈1.3 s wall). Every keep-commit MUST pass them. CI rejects regressions automatically — no human review needed to catch a gaming pattern that violates any of these.

Spec: `docs/superpowers/specs/2026-04-25-simulator-invariants-design.md`

| ID | Property | Catches |
|----|----------|---------|
| INV-1 | `cascade_dead_*` is always a subset of the graph | Hallucinated refdes / rail labels |
| INV-2 | `failures=[]` produces empty cascade | Spurious deaths from baseline |
| INV-3 | Every cascade death has a physical cause (kill / dead rail / shorted source / open-passive downstream) | The gaming pattern that produced commits e09dd47 / f33d2da / 7b821cf — the OR-chain in `_justifies_death()` is the formal anti-gaming contract |
| INV-4 | Killing a rail's source IC → rail enters `cascade_dead_rails` | Broken transitivity in `_cascade` step 3 |
| INV-5 | Dead rail → its consumers die (unless they have a live alternate `power_in`) | Broken transitivity in `_cascade` step 4 |
| INV-6 | Determinism on the failure path | Non-determinism corrupting evolve scoring |
| INV-7 | Sourceless rails (external inputs: VIN, USB, battery) immune to internal IC kills | Over-prediction on external-input failures |
| INV-8 | Round-trip: `hypothesize(simulate(refdes, mode))` returns `(refdes, mode)` in top-5 — measured on observable pairs only, ≥80% recall, ≤75% silent ratio | Asymmetry between simulator and hypothesize physics |
| INV-9 | Non-empty cascade → verdict ∈ {cascade, blocked, degraded} (never "completed") | UI/data drift |
| INV-10 | `hypothesize(empty_observation)` returns no positive-score hypotheses | Fabrication of suspicion from no evidence |

If an invariant fails on a future change, the path is:
1. Verify the invariant property is the right physical law (vs. mistakenly encoded).
2. If the property is right → it's a bug; file here as a P-level entry, do NOT silently weaken the test.
3. If the property is overstated → relax it via human edit to the spec + test, document why in the test docstring.

The evolve agent CANNOT modify the invariant file. Only humans update it. This is the closure of the score-gaming backdoor, complementing the hand-curated `benchmark/scenarios.jsonl` and the human-only control of `evaluator.py`.

---

## P1 — High-impact gaps (move the needle on cascade_recall or self_mrr)

### Open on passive_fb filter — downstream rail not marked dead

Scenarios: `mnt-reform-fb20-filter-open-dbvdd`, `mnt-reform-fb3-filter-open-pcie`

**Symptom:** expected `expected_dead_rails=["DBVDD"]` / `["PCIE1_PWR_FILT"]` with the downstream IC also dead. Simulator currently kills only the downstream IC via the "open passive" handler in `_apply_failures_at_init`; the filter-output rail isn't registered as dead in `cascade_dead_rails`.

**Hypothesis direction:** when `_apply_failures_at_init` handles `mode="open"` on a `passive_fb role=filter`, identify the downstream net (the touched net that is NOT a sourced power rail) and if that net is itself registered in `power_rails`, add it to the rails-dead set for `_cascade` to pick up. Or extend `_cascade` to treat any rail that *only* reaches consumers via an opened passive as effectively dead. Pointer: `api/pipeline/schematic/simulator.py:_apply_failures_at_init` and `_cascade`.

### Q3 shorted — over-prediction on load-switch hard short

Scenario: `mnt-reform-q3-load-switch-stuck-on-pvin`

**Symptom:** expected empty cascade (PVIN stays powered when Q3 shorts D-S, it's just always-on instead of being gated). Simulator probably populates something and fails the `false-positive cascade` check.

**Hypothesis direction:** `passive_q role=load_switch` with `mode=shorted` shouldn't zero the downstream rail — it makes it always-on (ungated). Current `_apply_failures_at_init` `shorted` branch likely treats it generically as "rail to GND short". Needs a per-role branch that reads the role and picks the right state. Pointer: `api/pipeline/schematic/simulator.py:_apply_failures_at_init` shorted branch.

### U7 dead — transitive rail cascade incomplete

Scenario: `mnt-reform-u7-dead-5v-buck` (cascade_recall 0.75)

**Symptom:** killing U7 (main +5V source) should zero +5V and then +1V2 (sourced by U13 which consumes +5V). Simulator currently marks +5V dead but may miss +1V2 in `cascade_dead_rails` — it kills U13 (component), but not the rails U13 sources.

**Hypothesis direction:** `_cascade` already does two passes; check whether the second pass correctly picks up the rails whose source ended up in `dead_components` after pass 1. May need a third pass (fixpoint iteration) OR a single pre-computed "dead source set" that includes transitively-powered-off ICs. Pointer: `api/pipeline/schematic/simulator.py:_cascade`.

---

## P2 — Medium-impact (move self_mrr, not cascade_recall directly)

### Undersampled failure modes for passive_r roles

`_MODES_FOR_KIND["passive_r"]` in evaluator only samples `("open",)`. Real roles include `damping`, `feedback`, `pull_up`, `pull_down`, `series`, `current_sense` — each with its own `short` / `open` cascade in `_PASSIVE_CASCADE_TABLE`. The self_MRR pass doesn't exercise short on feedback resistors, for instance. **NOT a simulator bug — this is the evaluator sampling strategy, which is READ-ONLY for the evolve agent**. Flag for human revision only.

### passive_d rectifier handlers

`_PASSIVE_CASCADE_TABLE` has open/short for rectifier but no leaky/stuck-forward equivalent. If field findings show diode leakage is common, add a mode. Blocked on benchmark evidence.

---

## P3 — Low-impact / exploratory

### passive_q cell_balancer ambiguity

`passive_q role=cell_balancer` modes currently all return "alive" (observation-cell only, no cascade). This is correct for BMS topology but means these components never contribute to self_MRR (every (refdes, mode) pair produces identical empty cascade → rank ambiguous). Trade-off: documenting is fine, no change needed unless field findings surface a real BMS failure cascade.

### `_phase_blocked` message quality for shorted rails

Current message "Rail X never stabilised" for a shorted rail reads oddly — it did stabilise, just at 0V. Cosmetic. `api/pipeline/schematic/simulator.py:_phase_blocked`.

---

## ANTI-PATTERNS — interdits explicites (l'agent NE DOIT PAS faire ça)

### ❌ Self-dead conventions

**Règle :** un composant ne doit JAMAIS être marqué `dead` dans `_apply_failures_at_init` quand sa branche de failure ne produit aucun effet observable downstream (rail dead, consumer dead, signal cascade).

**Pourquoi :** marquer self-dead casse les ties dans le ranking Jaccard de `evaluator.py` et fait monter `self_MRR` artificiellement, mais fabrique de l'information non-physique. Concrètement :
- Un IC qui n'est PAS source d'un rail n'a PAS de mode `regulating_low` réaliste
- Un cap hors `decoupling` list ne devient PAS dead quand il leak
- Une R pull-up / current-sense / feedback ne devient PAS dead quand elle s'ouvre

**Pollution downstream :** `cascade_dead_components` est consommé par hypothesize.py, mb_schematic_graph(query=simulate), et l'UI Boardview. Marquer un composant dead → hallucination diagnostique propagée jusqu'au technicien.

**Si tu identifies que tu as besoin d'un effet observable pour casser un tie cluster** → c'est un bug de l'évaluateur (sampling absurde dans `_MODES_FOR_KIND`), pas du simulateur. Utilise le canal `propose-evaluator-fix` (cf. SKILL §Cas spécial).

**Précédent :** commits e09dd47, f33d2da, 7b821cf (reverted 2026-04-24 après code review automatique). Trois patterns identiques pour les modes regulating_low, open, leaky_short. Tous gaming, tous reverted.

## RESOLVED (evolve agent: move items here as "keep" commits land them)

- **P1 #1 (passive_fb open disambiguation)** — résolu par `e29f3f3`, garde-fou pin la sémantique
- **P1 #2 (load_switch shorted = stuck-on)** — résolu par `a673123`, filtré par role
- **P1 #3 (transitive cascade U7)** — résolu par `a83cb1a`, ordre des passes corrigé
