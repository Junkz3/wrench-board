# Reverse Diagnostic — Symptom → Hypothesis Design

## Context

The behavioral simulator landed in April 2026 (commits 5ae5963 → 30b7192). It answers the forward question: *given a kill, what cascades?* This spec closes the **inverse** direction: *given observed symptoms, which refdes deaths explain them?*

This is the killer feature that turns the simulator from a cascade viewer into a diagnostic oracle — no SPICE, no schematic grep, a tech describes what they see cold on the bench and gets back a ranked list of fault hypotheses.

## Goal

Ship a synchronous pure-Python hypothesis engine that, given a partial observation of the board state, returns the top-N (refdes-kill-set, score, diff, narrative) tuples that best explain the observation. Ship it with a benchmark suite as first-class citizen — top-3 accuracy ≥ 80% CI-gated.

## Non-goals

- **3-fault exhaustive**: real microsolder pannes are ≤ 2 independent events; 3+ simultaneous independent deaths are out of scope (carte brûlée / inondée).
- **Fault types beyond "refdes is dead"**: no short-to-GND, no stuck-EN, no leaky-cap modelling yet. Single death per component, cascades derived by the existing forward simulator. Richer fault modes are a separate spec.
- **Intra-phase dynamics**: the forward simulator stays phase-atomic; hypothesize reuses it as-is.
- **Real-time streaming**: the tool is synchronous JSON-in / JSON-out. No WebSocket progress.
- **Observation persistence across sessions**: the tech's dead/alive toggles live in the browser for the current diagnostic session. Per-repair persistence is a follow-up.

## Architecture

Three layers, each with one responsibility and a well-defined boundary:

1. **Core engine** (`api/pipeline/schematic/hypothesize.py`, sync, pure, ~400 lines)
   - Input: `ElectricalGraph`, optional `AnalyzedBootSequence`, `Observations` (4 sets).
   - Output: `HypothesizeResult` — up to N ranked `Hypothesis` objects.
   - Depends on the existing `SimulationEngine`. No LLM, no IO, no session state.

2. **Agent tool** (`api/tools/hypothesize.py`, ~100 lines)
   - Thin wrapper around the engine, validates `killed_refdes` and observation refdes against the graph, returns `{found: false, ...}` on missing pack, same contract as `mb_schematic_graph`.
   - Exposed to Claude as `mb_hypothesize`. Added to the agent's tool manifest.

3. **HTTP endpoint** (`api/pipeline/__init__.py` extension)
   - `POST /pipeline/packs/{device_slug}/schematic/hypothesize`
   - Body: `Observations` JSON. Response: `HypothesizeResult` inline (< 500 ms p95 on MNT).
   - Validates refdes against graph (400 on unknown).

4. **Frontend** (`web/js/schematic.js`, `web/styles/schematic.css`)
   - Inspector gets 3-state toggles (❌ mort / ⚪ inconnu / ✅ vivant) for the selected node, stored in a new `SimulationController.observations` object.
   - A « Diagnostiquer » button in the inspector calls the endpoint with the current observations and shows the top-N hypotheses as cards with the diff coloured in the graph.
   - Claude also gets the tool via `mb_hypothesize` and can set observations or read them automatically when the user types « +3V3 dead, U1 froid » in the LLM panel.

## Data shapes (Pydantic v2, `api/pipeline/schematic/hypothesize.py`)

All shapes `model_config = ConfigDict(extra="forbid")`, closed semantics.

```python
class Observations(BaseModel):
    dead_comps: frozenset[str] = frozenset()
    alive_comps: frozenset[str] = frozenset()
    dead_rails: frozenset[str] = frozenset()
    alive_rails: frozenset[str] = frozenset()

class HypothesisMetrics(BaseModel):
    tp_comps: int
    tp_rails: int
    fp_comps: int      # predicted dead, observed alive (contradiction)
    fp_rails: int
    fn_comps: int      # observed dead, predicted alive (under-explain)
    fn_rails: int

class HypothesisDiff(BaseModel):
    contradictions: list[str]     # refdes / rail the hypothesis kills but observation says alive
    under_explained: list[str]    # refdes / rail observed dead but hypothesis leaves alive
    over_predicted: list[str]     # refdes / rail hypothesis kills that wasn't observed either way

class Hypothesis(BaseModel):
    kill_refdes: list[str]                  # 1 or 2 items
    score: float
    metrics: HypothesisMetrics
    diff: HypothesisDiff
    narrative: str                          # FR, deterministic template, no LLM
    cascade_preview: dict                   # {dead_rails: [...], dead_comps_count: int}

class PruningStats(BaseModel):
    single_candidates_tested: int
    two_fault_pairs_tested: int
    wall_ms: float

class HypothesizeResult(BaseModel):
    device_slug: str
    observations_echo: Observations
    hypotheses: list[Hypothesis]
    pruning: PruningStats
```

## Algorithm

### Pruning rules (the whole game is here)

**Single-fault candidate set**: for each `refdes` in `electrical.components`, memoize `sim(killed=[refdes]).cascade` exactly once. Keep a candidate only if its cascade explains at least one observation:

```
keep refdes iff (cascade.dead_comps ∪ {refdes}) ∩ observations.dead_comps ≠ ∅
            OR   cascade.dead_rails ∩ observations.dead_rails ≠ ∅
```

Expected reduction on MNT: 449 → ~30-80 candidates.

**2-fault candidate set**: bounded by top-K single-fault survivors (K=20). For each `c1` in top-K:

```
residual_dead_comps = observations.dead_comps − cascade(c1).dead_comps
residual_dead_rails = observations.dead_rails − cascade(c1).dead_rails
if residual is empty: skip 2-fault for c1 (it's already sufficient)
for each c2 ≠ c1:
    if cascade(c2).dead_comps ∩ residual_dead_comps = ∅
    AND cascade(c2).dead_rails ∩ residual_dead_rails = ∅:
        skip — c2 can't help explain the residual
    simulate combined killed=[c1, c2] and score
```

Expected 2-fault pairs tested on MNT: ~200-500. Combined wall budget: **< 500 ms p95**.

### Scoring function

For each candidate hypothesis with simulated cascade `S`:

```
tp = |S.dead_comps ∩ obs.dead_comps| + |S.dead_rails ∩ obs.dead_rails|
fp = |S.dead_comps ∩ obs.alive_comps| + |S.dead_rails ∩ obs.alive_rails|
fn = |obs.dead_comps − S.dead_comps| + |obs.dead_rails − S.dead_rails|

score = tp − 10·fp − 2·fn
```

Weights `10` (FP) and `2` (FN) are **constants tuned by the benchmark suite**. They live in a module-level tuple `PENALTY_WEIGHTS = (10, 2)` so the bench script can sweep them and pick the pair that maximises top-3 accuracy on the fixture corpus.

### Output ranking

Merge single-fault and 2-fault candidates; sort by score descending; keep top-N (default N=5, tunable via argument). Tie-break: fewer refdes in kill_refdes (prefer single-fault), then lower sum of blast_radius (prefer less-cascading explanation).

### Narrative template (FR, deterministic)

For each hypothesis, compose a 2-3 sentence French narrative:

- Single-fault: `"Si {refdes} meurt : {dead_rails_list} jamais stables → {dead_count} composant(s) downstream morts. Explique {tp}/{obs_total} observations, {fp} contradiction(s)."`
- 2-fault: `"Si {refdes1} ET {refdes2} meurent simultanément : {combined_effect}. Explique {tp}/{obs_total}, {fp} contradiction(s)."`
- If `contradictions` non-empty: append `" Contredit : {contradictions}."`
- If `under_explained` non-empty: append `" Ne couvre pas : {under_explained}."`

Where `obs_total = |obs.dead_comps| + |obs.dead_rails| + |obs.alive_comps| + |obs.alive_rails|` (we report coverage across ALL observation classes, not just the dead ones). `tp` counts true-positive matches on both the dead AND the alive sides — a hypothesis that correctly predicts an observed-alive component as alive scores a point.

Template implemented as a Python f-string in `_narrate(hypothesis, observations)`. No LLM in the hot path — explainability is first-class.

### Tunable constants (module-level in `hypothesize.py`)

```python
PENALTY_WEIGHTS = (10, 2)            # (fp_weight, fn_weight) — tuned by scripts/tune_hypothesize_weights.py
TOP_K_SINGLE = 20                    # how many single-fault survivors seed 2-fault exploration
MAX_RESULTS_DEFAULT = 5              # default top-N returned to caller
TWO_FAULT_ENABLED = True             # kill-switch if 2-fault budget is ever blown
```

All four are exported so tests and the weight-tuner can parameterise them without monkey-patching.

## Public API

### Python tool (for `runtime_direct.py` and `runtime_managed.py`)

```python
# api/tools/hypothesize.py

def mb_hypothesize(
    *,
    device_slug: str,
    memory_root: Path,
    dead_comps: list[str] | None = None,
    alive_comps: list[str] | None = None,
    dead_rails: list[str] | None = None,
    alive_rails: list[str] | None = None,
    max_results: int = 5,
) -> dict[str, Any]:
    """Rank candidate refdes-kills that explain the observations.

    Returns {found: false, reason, invalid_refdes, closest_matches} on any
    unknown refdes or rail label. Observation lists default to empty.
    """
```

The tool is added to the agent's `manifest.py` alongside `mb_schematic_graph` / `mb_get_component` etc., with a JSON schema describing the 4 observation arrays. Claude gets a system-prompt paragraph explaining *when* to call it (the user describes a symptom), so it doesn't call it on every message.

### HTTP endpoint

```http
POST /pipeline/packs/{device_slug}/schematic/hypothesize
Content-Type: application/json

{
  "dead_comps": ["U1", "U9"],
  "alive_comps": ["U7"],
  "dead_rails": ["+3V3"],
  "alive_rails": ["+5V", "VIN"],
  "max_results": 5
}
```

Responses:
- **200** — `HypothesizeResult` JSON.
- **400** — unknown refdes or rail in any of the 4 lists, with `detail` listing the offenders.
- **404** — pack has no `electrical_graph.json`.
- **422** — malformed graph JSON on disk.

## Frontend integration

### `SimulationController.observations`

New field on the existing controller:
```javascript
observations: { dead_comps: new Set(), alive_comps: new Set(),
                dead_rails: new Set(), alive_rails: new Set() }
```

### 3-state toggles in inspector

In `updateInspector(node)`, append a compact row between the existing criticality block and the Simuler-panne button:

```
Observation :  [❌ mort]  [⚪ inconnu]  [✅ vivant]
```

Clicking a state:
- Updates the corresponding `observations.*` set.
- Annotates the node in the graph with `.obs-dead` / `.obs-alive` CSS classes (amber border vs. emerald border — **new classes**, not reusing `sim-*`). The « inconnu » state intentionally has no class — implicit default.
- Removes the opposite observation from the same node if present (a component can't be simultaneously observed dead and alive).
- Does NOT trigger a re-simulation automatically.

### « Diagnostiquer » button

Appears at the bottom of the inspector when `observations` has at least one entry (any of the 4 sets). Click → fetch `POST /schematic/hypothesize`, render results in a new right-side panel `.sim-hypotheses-panel`:

- Top-5 hypothesis cards, each with:
  - kill_refdes chip (clickable → triggers Simuler-panne with that kill set for visual verification)
  - score as a bar
  - TP/FP/FN summary
  - narrative FR sentence
  - diff breakdown (contradictions in red, under_explained in amber, over_predicted in grey)
- A « Réinitialiser observations » button clears all toggles.

### Chat-side integration

Claude gets `mb_hypothesize` in its tool manifest. The system-prompt paragraph is short: *« Quand le tech décrit un symptôme (« X est mort », « Y est OK »), extrais les observations des 4 catégories et appelle mb_hypothesize. Puis présente les top-3 hypothèses avec les contradictions. »* No prompt-engineering magic — the narrative field is already FR-ready.

## Benchmark suite

### Fixtures

`tests/pipeline/schematic/fixtures/hypothesize_scenarios.json` — auto-generated by `scripts/gen_hypothesize_benchmarks.py --slug mnt-reform-motherboard`. Each scenario:

```json
{
  "id": "mnt-kill-u7-partial",
  "slug": "mnt-reform-motherboard",
  "ground_truth_kill": ["U7"],
  "observations": {
    "dead_comps": ["U1", "U13"],         // sampled from full cascade
    "alive_comps": ["U14", "U18"],        // sampled from unaffected set
    "dead_rails": ["+5V"],
    "alive_rails": ["LPC_VCC", "VIN"]
  },
  "sample_strategy": "3-rails-observed + 4-comps-observed"
}
```

**Generation logic**:
- Pick 27 rail sources + top-20 components by blast_radius → ~45 candidates.
- For each candidate, simulate `killed=[candidate]` on MNT.
- Create 3 scenario variants per kill: partial-rail-only, partial-comp-only, mixed — by sampling 2-5 items from dead and alive sets at random (seeded for reproducibility).
- Yields ~135 scenarios (45 × 3). Stored as single JSON array in the fixture file.

Plus 10 manually-curated scenarios covering:
- iPhone-X common pannes (Tristar short, U2 audio codec dead)
- MNT field reports (when we have them)
- Edge cases: observations that match multiple hypotheses equally

### Tests

`tests/pipeline/schematic/test_hypothesize.py` — unit tests on the core engine:
- Single-fault scoring (TP / FP / FN counted correctly)
- Pruning leaves no false-negative candidate that would have scored > 0
- 2-fault kicks in only when residual is non-empty
- Determinism across 100 runs
- Empty observations → empty result (no spurious hypotheses)
- Unknown refdes in `alive_comps` → invalid-input contract

`tests/pipeline/schematic/test_hypothesize_accuracy.py` — parametrised over all scenarios in the fixture file:
- `top1_accuracy` ≥ **60%** (CI gate)
- `top3_accuracy` ≥ **80%** (CI gate)
- `mrr` ≥ **0.75** (CI gate)
- Emits a summary CSV on failure for triage.

`tests/tools/test_hypothesize.py` — tool wrapper contract tests (invalid refdes → structured error, slug-not-found path, etc.).

`tests/pipeline/test_hypothesize_endpoint.py` — HTTP TestClient coverage (happy path, 400, 404).

### Perf bench

`scripts/bench_hypothesize.py --slug mnt-reform-motherboard --iterations 100`:
- Picks random scenarios from the fixture, calls the engine, emits p50/p95/p99 in ms + mean single_candidates_tested + mean two_fault_pairs_tested.
- CI gate in `test_hypothesize_accuracy.py`: `p95 < 500 ms` on MNT.

### Weight-tuning script

`scripts/tune_hypothesize_weights.py` — sweeps `fp_weight ∈ [5, 10, 15, 20]` × `fn_weight ∈ [1, 2, 5]` over the fixture corpus, reports the top-3 accuracy for each pair, picks the best and writes it back to `PENALTY_WEIGHTS` in `hypothesize.py`. Runs once manually, committed result.

## Files impacted

| File | Action | Est. size |
|---|---|---|
| `api/pipeline/schematic/hypothesize.py` | **create** — engine + shapes + narrative | ~400 lines |
| `api/tools/hypothesize.py` | **create** — `mb_hypothesize` tool | ~100 lines |
| `api/pipeline/__init__.py` | modify — POST /hypothesize endpoint | +80 lines |
| `api/agent/manifest.py` | modify — register `mb_hypothesize` | +20 lines |
| `web/js/schematic.js` | modify — `observations`, toggles, results panel | +250 lines |
| `web/styles/schematic.css` | modify — `.obs-*` + `.sim-hypotheses-panel` | +120 lines |
| `tests/pipeline/schematic/test_hypothesize.py` | **create** — unit tests | ~300 lines |
| `tests/pipeline/schematic/test_hypothesize_accuracy.py` | **create** — accuracy + perf CI gate | ~150 lines |
| `tests/pipeline/schematic/fixtures/hypothesize_scenarios.json` | **create (generated)** | ~6 KB |
| `tests/tools/test_hypothesize.py` | **create** — tool contract | ~120 lines |
| `tests/pipeline/test_hypothesize_endpoint.py` | **create** — HTTP | ~100 lines |
| `scripts/gen_hypothesize_benchmarks.py` | **create** | ~80 lines |
| `scripts/bench_hypothesize.py` | **create** | ~80 lines |
| `scripts/tune_hypothesize_weights.py` | **create** | ~60 lines |

Grand total: ~1800 lines of new code + ~450 lines of test/infra.

## Rollout plan (high level, the detailed plan follows in writing-plans)

1. **Core engine** (shapes + scoring + single-fault only) with unit tests — first commit, CI green.
2. **2-fault pruning** with unit tests — second commit, same CI.
3. **Benchmark generator** + fixture JSON — third commit, generates but doesn't yet gate.
4. **Accuracy test file + CI thresholds** — fourth commit, turns on the CI gate. Tune weights if thresholds fail.
5. **Agent tool `mb_hypothesize`** with wrapper tests — fifth commit.
6. **HTTP endpoint** — sixth commit.
7. **Frontend toggles + results panel** — three commits (toggles, fetch, results rendering) with browser-verif checkpoints with Alexis.
8. **Perf bench** + final CI check. Port to Rust deferred as backlog if p95 > 500 ms.

## Open questions (to settle before the plan)

None — every decision above has been locked through brainstorming. The only knobs the benchmark will tune post-hoc are `PENALTY_WEIGHTS`.

## Dette backlog (hors scope)

- Port `simulator.py` + `hypothesize.py` core to Rust via PyO3 if 3-fault or batched use cases emerge.
- Richer fault modes (short-to-GND, stuck-EN, leaky-cap) — would extend `Observations` with a new per-refdes mode field and require simulator changes.
- Compiler alias pass (3V3 ↔ +3V3, 5V_SUPPLY ↔ +5V) and ferrite-as-source rule — improves graph quality upstream, benefits all downstream tools.
- Per-repair persistence of observations (currently browser session only).
- Chat symptom parser: a Claude pre-pass that extracts the 4 observation lists from free-text BEFORE calling `mb_hypothesize`. Trivial prompt, not needed for MVP because Claude can do it inline.
