# Richer Fault Modes + Measurement Memory Design

## Context

The reverse-diagnostic engine landed on 2026-04-23 (commits `d026065 → a2a2662`,
15 commits). It ranks refdes-kills that explain a tech's observations with
95% top-1 accuracy on an auto-generated MNT corpus in ~215 ms. Live
verification on a field report (U13 buck dead, `+1V2 = 0.025V`) returns U13
top-1.

Crawling 9 documented MNT Reform community repair cases revealed the hard
limit of the current model: **only 1 of 9 is encodable** with the binary
`{dead, alive}` observation schema. The 8 others ask for failure modes the
system cannot represent today — IC-active-but-bad-output (U10 eDP bridge,
LTC6803 bad sense), thermal anomalies (Q17 overheating), rail-level shorts,
and passive component failures (R29 exploded, D5 removed).

Parallel to this limit, the current Observations schema is a snapshot. There
is no recording of *when* a measurement was taken, no way to compare before
vs. after a rework, and no shared memory between the tech (clicking toggles)
and Claude (reading free-form « j'ai mesuré +3V3 à 2.87V »).

This spec closes both gaps in one coherent feature. It extends the observation
vocabulary to four failure modes and introduces a per-repair **measurement
memory** that tracks every value the tech probes, timestamped, replayable,
and bidirectionally writable by Claude via WS-bridged tools.

## Goal

Ship a **Phase 1 unified** feature that:

1. Replaces the 4-set `Observations` with a structured `{refdes: mode}` schema
   supporting 4 component modes (`dead`, `alive`, `anomalous`, `hot`) and 3
   rail modes (`dead`, `alive`, `shorted`).
2. Implements the `anomalous` propagation rule in `_simulate_failure`: an IC
   that receives power normally but whose signal output is wrong — its
   downstream on typed signal edges (`produces_signal`, `consumes_signal`,
   `clocks`, `depends_on`) is marked anomalous.
3. Introduces a per-repair, append-only measurement journal that both the
   frontend UI and Claude write to and read from.
4. Surfaces 4 new `mb_*` tools for Claude to record / list / compare / materialise
   measurements into hypothesize observations.
5. Adds 2 new `mb_*` tools to set/clear observations over the WebSocket so
   Claude and the UI share the same visual state.
6. Extends the frontend inspector with a mode-picker contextualised by node
   kind, a free-form metric input, a per-target mini-timeline of recorded
   measurements, and a node-classify-from-value helper.
7. Upgrades the benchmark generator and CI accuracy gate to report top-1 /
   top-3 / MRR **per mode** instead of aggregate only.

The demo flow the feature unlocks:

> Tech to Claude: « +3V3 mesurée à 2.87V, U7 chauffe à 72°C ».
> Claude → `mb_record_measurement` twice → WS events fire → UI toggles light
> up in amber (rail auto-classified anomalous, U7 badged hot) → Claude →
> `mb_hypothesize` → results panel shows U7 as top-1 with FR narrative citing
> the measurements. Tech reflows U7 → « remesuré, +3V3 = 3.29V » → Claude →
> `mb_record_measurement` (with `note="après reflow"`) → toggle turns
> emerald → Claude: « la mesure confirme la réparation ».

## Non-goals

- **Analog / SPICE-style simulation.** Rails remain binary-ish. The simulator
  is topological-logical, not continuous. Numeric measurements are stored
  and auto-classified to a mode; they do not participate in the discrete
  scoring function in Phase 1. (A follow-up Phase 5 may add a soft proximity
  term to the score — deferred to backlog.)
- **Richer modes beyond `anomalous`.** `shorted`, `hot`, and the passive
  retro-injection are **Phases 2-4** of this same design family, shipped as
  separate specs/plans once Phase 1 is on the bench. This spec defines the
  data shapes to accept them from day one (so nothing is thrown away when
  they land) but implements only `anomalous` propagation + `hot` as
  self-only + `shorted` as a stub.
- **Cross-session measurement persistence** at device level. The measurement
  memory is scoped to a `repair_id`; two repairs on the same MNT will not
  share a measurement history. A device-level field-report aggregate exists
  already via `findings.json`; duplicating it is out of scope.
- **Automatic multimeter capture.** Inputs are manual (tech types the value)
  or via Claude extracting from free-form chat. No USB-HID multimeter
  bridge.

## Architecture

Five layers, each with one responsibility:

1. **Core engine** (`api/pipeline/schematic/hypothesize.py`, sync pure Python,
   ~200 LOC net delta)
   - New Pydantic shapes: `Observations` (schema B with two `dict[str, mode]`),
     `Hypothesis` (with `kill_modes` parallel list), `HypothesisDiff` (typed
     contradictions), `ObservedMetric`.
   - New helper `_simulate_failure(electrical, analyzed_boot, refdes, mode)`
     that dispatches by mode: `dead` → existing `SimulationEngine`, `anomalous`
     → BFS on signal-typed edges, `hot` → self-only, `shorted` → rail-via-source
     propagation.
   - New helper `_propagate_signal_downstream(electrical, refdes)` doing the
     anomalous BFS on `typed_edges[kind in {produces_signal, consumes_signal, clocks, depends_on}]`.
   - Updated `_score_candidate` that works off `state_comps` / `state_rails`
     dicts, with mode-aware TP/FP/FN counting.
   - Updated FR narrative template mentioning per-mode wording and citing
     numeric measurements when available.

2. **Measurement memory store** (`api/agent/measurement_memory.py`, new file,
   ~120 LOC)
   - `MeasurementEvent` Pydantic model + append-only JSONL file at
     `memory/{slug}/repairs/{repair_id}/measurements.jsonl`, same pattern as
     `chat_history.jsonl`.
   - Functions: `record_measurement()`, `list_measurements()`,
     `compare_measurements()`, `synthesise_observations()`.
   - Auto-classify rules: central table mapping (value, nominal, unit) to a
     component or rail mode. Thresholds tunable, committed defaults based on
     standard PSU tolerances:
     - Rail voltage ±10% of nominal → `alive`
     - Rail voltage 50–90% of nominal → `anomalous` (sagging / regulation loss)
     - Rail voltage <50% of nominal AND non-zero → `anomalous` (heavy sag)
     - Rail voltage ≈0 V (< 50 mV) → `dead` by default. Promoted to `shorted`
       if a separate observation notes the upstream regulator / source drawing
       current above its spec, or the tech explicitly passes `note="short"`.
       Pure voltage alone cannot distinguish dead from shorted — the
       distinction lives in current draw or upstream source stress.
     - Rail voltage >110% of nominal → `overvoltage` (**new tag, not
       `shorted`** — a short collapses the rail, an open feedback divider or
       blown regulator drives it high). Overvoltage maps to `shorted` in the
       `state_rails` dict for Phase 1 (same downstream effect: damaged
       consumers, source stress) with a note flagged for Phase 5 to split it
       into its own mode if needed.
     - IC temperature >65 °C → `hot`. Threshold configurable per IC class.

3. **Agent tools** (`api/tools/hypothesize.py` + new
   `api/tools/measurements.py`, ~250 LOC net delta)
   - Extended `mb_hypothesize`: accepts `state_comps`, `state_rails` (the
     new schema B shapes).
   - New tools:
     - `mb_record_measurement(target, value, unit, nominal?, note?)` — append
       to the repair's journal, emit WS event `simulation.observation_set`
       with the auto-classified mode (envelope subclasses a new `_SimEvent`
       base in `api/tools/ws_events.py`, mirroring the existing `_BVEvent`
       / `boardview.<verb>` pattern).
     - `mb_list_measurements(target?, since?)` — filtered read of the journal.
     - `mb_compare_measurements(target, before_ts, after_ts)` — explicit
       before/after diff with delta + delta_percent.
     - `mb_observations_from_measurements()` — walk the journal, keep the
       latest measurement per target, materialise a full `Observations`
       payload that `mb_hypothesize` can consume directly.
     - `mb_set_observation(target, mode)` — direct state flip without a
       measurement (for when the tech already knows it's dead).
     - `mb_clear_observations()` — wipe all state, emit WS `simulation.observation_clear`.
   - Total: 5 new tools + 1 extended. All added to manifest, dispatched in
     both `runtime_direct` and `runtime_managed`.

4. **HTTP endpoint** (`api/pipeline/__init__.py` extension)
   - `POST /pipeline/packs/{slug}/schematic/hypothesize` — request body migrates
     from 4 lists to `{state_comps, state_rails, metrics_comps, metrics_rails}`.
     **Breaking change** (no dual-support). Sole caller is `web/js/schematic.js`
     (grep: single `fetch(/pipeline/packs/.../hypothesize)` site), own codebase,
     no external consumer to coordinate with. All fixtures regenerated.
   - No new endpoint for measurements — the frontend talks to the backend
     via the existing WS (agent tool) or a new lightweight HTTP route
     `POST /pipeline/packs/{slug}/repairs/{repair_id}/measurements` for
     direct UI record without an agent turn. Same validator as the tool.
   - `GET /pipeline/packs/{slug}/repairs/{repair_id}/measurements?target=...`
     for the UI to fetch history on inspector open.

5. **Frontend** (`web/js/schematic.js`, `web/styles/schematic.css`)
   - `SimulationController.observations` migrates from 4 Sets to
     `{state_comps: Map, state_rails: Map, metrics_comps: Map, metrics_rails: Map}`.
   - Inspector row replaces the 3-state toggle with a **contextual segmented
     control** per node kind:
     - Component (IC/active) → `[⚪] [✅] [❌] [⚠] [🔥]`
     - Rail → `[⚪] [✅] [❌] [⚡]`
     - (Phase 4) Passive → `[⚪] [✅] [❌]`
   - New metric input row: `Mesuré: [____] V  (nominal: 3.30 V)`, with
     auto-classify on blur/enter. Non-rail targets (`°C` for thermal, `A`
     for current, `Ω` for resistance, `mV` for ripple) supported; UI
     adapts unit field to node kind.
   - New mini-timeline section per target: the N most recent measurements
     for the currently-selected node, with timestamp, value, and `note`
     rendered as a compact vertical list with delta hint vs. the previous
     entry (« reflow U7 → +3V3: 2.87 V → 3.29 V »).
   - WS handler for `observation.set` / `observation.clear` events to mirror
     Claude's tool calls in real time. Reuses the existing
     `brd_viewer.js`-style event consumption pattern.
   - « Enregistrer mesure » action appending to the journal, either via the
     new HTTP route (direct UI) or (cleaner) via the WS so Claude sees the
     tech's manual entries too.

## Data shapes (Pydantic v2, all `ConfigDict(extra="forbid")`)

```python
# api/pipeline/schematic/hypothesize.py

ComponentMode = Literal["dead", "alive", "anomalous", "hot"]
RailMode = Literal["dead", "alive", "shorted"]

class ObservedMetric(BaseModel):
    measured: float
    nominal: float | None = None
    unit: Literal["V", "A", "W", "°C", "Ω", "mV"]
    tolerance_percent: float = 10.0

class Observations(BaseModel):
    state_comps: dict[str, ComponentMode] = Field(default_factory=dict)
    state_rails: dict[str, RailMode] = Field(default_factory=dict)
    metrics_comps: dict[str, ObservedMetric] = Field(default_factory=dict)
    metrics_rails: dict[str, ObservedMetric] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_conflicting_alias(self):
        # Enforce per-refdes single-mode property built-in by schema B.
        # Cross-check that a refdes is not both in state_comps and state_rails.
        overlap = set(self.state_comps) & set(self.state_rails)
        if overlap:
            raise ValueError(f"refdes appears as both component and rail: {overlap}")
        return self

class HypothesisMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Unchanged from the reverse-diagnostic v1 spec — re-stated here so the
    # shape is self-contained for the new reviewer.
    tp_comps: int
    tp_rails: int
    fp_comps: int   # predicted anomalous/dead/hot but observed alive
    fp_rails: int
    fn_comps: int   # observed non-alive but predicted alive
    fn_rails: int

class HypothesisDiff(BaseModel):
    contradictions: list[tuple[str, str, str]] = Field(default_factory=list)
    # each tuple: (target, observed_mode, predicted_mode)
    under_explained: list[str] = Field(default_factory=list)
    over_predicted: list[tuple[str, str]] = Field(default_factory=list)
    # each tuple: (target, predicted_mode)

class Hypothesis(BaseModel):
    kill_refdes: list[str]
    kill_modes: list[ComponentMode]    # parallel list, same length
    score: float
    metrics: HypothesisMetrics
    diff: HypothesisDiff
    narrative: str
    cascade_preview: dict
    # {dead_rails: [...], dead_comps_count: int, anomalous_count: int,
    #  hot_count: int, shorted_rails: [...]}
```

```python
# api/agent/measurement_memory.py

class MeasurementEvent(BaseModel):
    timestamp: str                       # ISO 8601 UTC
    target: str                          # see Target string grammar below
    value: float
    unit: Literal["V", "A", "W", "°C", "Ω", "mV"]
    nominal: float | None = None
    note: str | None = None
    source: Literal["ui", "agent"]       # who wrote it
    auto_classified_mode: str | None     # e.g. "anomalous" — cache of classify rule result

# Target string grammar
#   target := "<kind>:<name>"
#   kind   ∈ {"rail", "comp", "pin"}
#   name   := anything after the first colon, for "rail" it's the rail label
#            (e.g. "+3V3", "LPC_VCC"), for "comp" it's the refdes (e.g. "U7"),
#            for "pin" it's "{refdes}:{pin_number}" (e.g. "U7:3").
#   Parsing: split(":", 1) separates kind from name. For "pin", further
#   split name on the first colon to get (refdes, pin_number).
#   Refdes containing ":" are rejected at parse time. Verified against the
#   MNT corpus: no refdes in board_assets/ contains a colon.

class MeasurementJournal(BaseModel):
    repair_id: str
    device_slug: str
    events: list[MeasurementEvent]
```

## Algorithm

### `_simulate_failure(electrical, analyzed_boot, refdes, mode)`

Returns a cascade dict with 5 frozensets: `dead_comps`, `dead_rails`,
`anomalous_comps`, `hot_comps`, `shorted_rails`, plus `final_verdict` and
`blocked_at_phase`. Keeping `shorted_rails` separate from `dead_rails` is
needed so the scoring function can match an observed `state_rails={"+3V3":
"shorted"}` against a prediction that also tagged the rail `shorted` rather
than just `dead` — otherwise a correctly-identified short would score as a
soft mismatch.

- `mode="dead"` → existing `SimulationEngine`, output unchanged but wrapped
  with empty `anomalous_comps` / `hot_comps` for shape uniformity.
- `mode="anomalous"` → BFS on `typed_edges` whose `kind ∈ {produces_signal,
  consumes_signal, clocks, depends_on}`. `anomalous_comps` = `{refdes} ∪
  reachable`. Power rails sourced by `refdes` remain alive
  (`dead_rails = ∅`). Consumers on power edges remain alive
  (`dead_comps = ∅`). Self-observation is implicit (the refdes is in its
  own anomalous set).
  The kinds `powered_by`, `enables`, `decouples`, `filters`, and
  `feedback_in` are **intentionally excluded** from the anomalous BFS:
  `powered_by` / `enables` are power/control topology already handled by
  the `dead` mode, and `decouples` / `filters` point to passives (Phase 4
  scope). Kinds not matching the allow-set are skipped by simple set
  membership — no special-casing needed.
- `mode="hot"` → degenerate, `hot_comps = {refdes}` only, zero propagation.
  Useful as corroboration for multi-obs scenarios.
- `mode="shorted"` → `refdes` is a consumer that shorts its input rail to
  GND. Identify the rail via `electrical.power_rails` lookup for any rail
  that lists `refdes` in `consumers`. Kill the source of that rail (reuse
  `SimulationEngine` with `killed_refdes=[source_of_rail]`). The source
  also goes into `hot_comps` (current-limit / thermal stress), **and the
  rail itself is tagged in `shorted_rails` (not `dead_rails`)** so observer
  semantics match. Downstream rails + components from the source kill go
  into `dead_rails` / `dead_comps` normally. If no rail found for `refdes`,
  fall back to marking `refdes` alone dead.

### Pruning (same spirit as the dead-only engine)

For each `(refdes, mode)` pair applicable to the refdes type (ICs can be
dead / anomalous / hot / shorted; rails only shorted), compute the cascade
and keep if the cascade intersects any observation. Top-K single-mode
candidates (K=20) seed the 2-fault pass.

Candidate set would nominally multiply by up to 4 in the worst case
(449 refdes × 4 modes ≈ 1796). In practice the per-mode **applicability
gate** eliminates most of that multiplier before simulation even runs:

1. **`hot` mode** — applies to every IC but the cascade is a trivial
   self-only frozenset; no propagation sim. ~449 × ~0.05 ms = ~25 ms.
2. **`anomalous` mode** — only applies to ICs that have at least one
   outgoing `produces_signal` / `clocks` / `depends_on` edge in
   `typed_edges`. On the MNT graph that's roughly 40–60 ICs (verified
   subset), not 449.
3. **`shorted` mode** — only applies to refdes that are listed as
   consumers of at least one rail in `electrical.power_rails`. On MNT
   that's roughly 80 consumers, not 449.
4. **Cascade-intersection pruning** (existing from v1) continues to keep
   only candidates whose cascade touches an observed target.

Effective multiplier is therefore ~2× over the single-mode baseline
(449 dead + 25 ms hot + ~50 anomalous + ~80 shorted ≈ 600 sims). At the
~0.5 ms/sim rate measured by `bench_hypothesize.py`, that's a **~300 ms
p95** expected — within the 500 ms budget with margin. If a real run
breaches, cap `MAX_PAIRS = 100` on the 2-fault pass and emit a warning.

### Scoring

The scoring function works off the new schema. Pseudo:

```python
def _score_candidate(cascade: dict, obs: Observations):
    predicted = {}                             # refdes -> predicted mode string
    for r in cascade["dead_comps"]:       predicted[r] = "dead"
    for r in cascade["anomalous_comps"]:  predicted[r] = "anomalous"
    for r in cascade["hot_comps"]:        predicted[r] = "hot"
    predicted_rails = {}
    for rail in cascade["dead_rails"]:    predicted_rails[rail] = "dead"
    for rail in cascade["shorted_rails"]: predicted_rails[rail] = "shorted"
    # shorted takes precedence if a rail appears in both sets (shouldn't
    # happen by construction but enforced to be safe).

    tp_c = fp_c = fn_c = 0
    contradictions, under_explained, over_predicted = [], [], []

    for target, obs_mode in obs.state_comps.items():
        pred = predicted.get(target, "alive")
        if pred == obs_mode:
            tp_c += 1
        elif obs_mode == "alive" and pred != "alive":
            fp_c += 1
            contradictions.append((target, obs_mode, pred))
        elif obs_mode != "alive" and pred == "alive":
            fn_c += 1
            under_explained.append(target)
        else:
            fp_c += 1   # soft mismatch (e.g. obs=hot, pred=anomalous)
            contradictions.append((target, obs_mode, pred))

    # Same block mirrored for rails

    for target, mode in {**predicted, **predicted_rails}.items():
        if mode != "alive" and target not in (obs.state_comps.keys() | obs.state_rails.keys()):
            over_predicted.append((target, mode))

    tp = tp_c + tp_r
    fp = fp_c + fp_r
    fn = fn_c + fn_r
    score = float(tp - PENALTY_WEIGHTS[0] * fp - PENALTY_WEIGHTS[1] * fn)
    return score, metrics, HypothesisDiff(
        contradictions=contradictions,
        under_explained=under_explained,
        over_predicted=over_predicted,
    )
```

`PENALTY_WEIGHTS` re-tuned after the schema migration — the tuner script
re-runs on the regenerated fixture corpus. Expected current (10, 2) still
wins given the 95% baseline.

### FR narrative enrichment

The narrative cites measured values when present in `obs.metrics_*`:

> `Si U7 meurt : +3V3 jamais stable(s) → 20 composant(s) downstream morts.
> Mesures confirment : +3V3 à 2.87 V (87% du nominal, classé anomalous),
> U7 à 72°C (hot). Explique 2/2 observations, 0 contradiction.`

Template stays a pure f-string. No LLM in the hot path.

## Agent tools — public API

```python
# api/tools/hypothesize.py (extended)

def mb_hypothesize(
    *,
    device_slug: str,
    repair_id: str | None,                 # NEW — if provided, synthesises obs from journal
    memory_root: Path,
    state_comps: dict[str, str] | None = None,
    state_rails: dict[str, str] | None = None,
    metrics_comps: dict[str, dict] | None = None,
    metrics_rails: dict[str, dict] | None = None,
    max_results: int = 5,
) -> dict[str, Any]:
    ...

# api/tools/measurements.py (new)

def mb_record_measurement(
    *,
    device_slug: str,
    repair_id: str,
    memory_root: Path,
    target: str,             # "rail:+3V3" | "comp:U7" | "pin:U7:3"
    value: float,
    unit: str,
    nominal: float | None = None,
    note: str | None = None,
    source: str = "agent",
) -> dict[str, Any]:
    """Append a MeasurementEvent to the journal, auto-classify to a mode,
    return {recorded: True, auto_classified_mode, timestamp}."""

def mb_list_measurements(...): ...
def mb_compare_measurements(...): ...
def mb_observations_from_measurements(...): ...

def mb_set_observation(
    *, device_slug, repair_id, memory_root, target, mode,
) -> dict[str, Any]: ...

def mb_clear_observations(
    *, device_slug, repair_id, memory_root,
) -> dict[str, Any]: ...
```

Each write-tool (`record`, `set`, `clear`) emits a WS
`simulation.observation_set` or `simulation.observation_clear` event so the
frontend stays in sync. The envelope mirrors the existing `_BVEvent` /
`boardview.<verb>` pattern — a new `_SimEvent(BaseModel)` base class with a
`type: Literal[...]` discriminator, plus two concrete subclasses
`SimulationObservationSet` and `SimulationObservationClear` colocated in
`api/tools/ws_events.py`. Payload of `SimulationObservationSet`:
`{type, target, mode, measurement: {measured, unit, nominal?, note?} | null}`.

## HTTP surface

```http
POST /pipeline/packs/{slug}/schematic/hypothesize
Body: {
  "state_comps": {"U7": "anomalous"},
  "state_rails": {"+3V3": "dead", "+5V": "alive"},
  "metrics_comps": {"U7": {"measured": 72.3, "unit": "°C", "nominal": null}},
  "metrics_rails": {"+3V3": {"measured": 0.02, "unit": "V", "nominal": 3.3}}
}
→ 200 HypothesizeResult

POST /pipeline/packs/{slug}/repairs/{repair_id}/measurements
Body: MeasurementEvent minus timestamp (server-stamped)
→ 201 with the stored event + auto_classified_mode

GET /pipeline/packs/{slug}/repairs/{repair_id}/measurements?target=...&since=...
→ 200 list[MeasurementEvent]
```

All other routes (`/simulate`, etc.) untouched.

## Frontend integration

### `SimulationController.observations` (new shape)

```javascript
observations: {
  state_comps: new Map(),     // refdes → mode
  state_rails: new Map(),     // rail → mode
  metrics_comps: new Map(),   // refdes → {measured, unit, nominal?, ts}
  metrics_rails: new Map(),   // rail → {measured, unit, nominal?, ts}
}
```

### Inspector — contextual mode picker

`updateInspector(node)` branches on `node.kind`:

```html
<!-- Component (IC/active) -->
<div class="sim-obs-row" data-kind="comp">
  <span class="sim-obs-label">Observation</span>
  <div class="sim-mode-picker">
    <button data-mode="unknown"   class="active">⚪ inconnu</button>
    <button data-mode="alive">✅ vivant</button>
    <button data-mode="dead">❌ mort</button>
    <button data-mode="anomalous">⚠ anomalous</button>
    <button data-mode="hot">🔥 chaud</button>
  </div>
</div>

<!-- Rail -->
<div class="sim-obs-row" data-kind="rail">
  <div class="sim-mode-picker">
    <button data-mode="unknown">⚪</button>
    <button data-mode="alive">✅</button>
    <button data-mode="dead">❌</button>
    <button data-mode="shorted">⚡ shorté</button>
  </div>
</div>
```

### Inspector — metric input

```html
<div class="sim-metric-row">
  <span class="sim-obs-label">Mesuré</span>
  <input type="number" class="sim-metric-input" step="0.01">
  <select class="sim-metric-unit">
    <option>V</option><option>A</option><option>°C</option>
    <option>Ω</option><option>mV</option>
  </select>
  <span class="sim-metric-nominal">(nominal: 3.30 V)</span>
  <button class="sim-metric-record">Enregistrer</button>
</div>
```

`blur` / `Enter` on the input → auto-classify via client-side rules (mirror of
server-side auto-classify), update the mode picker, record to journal via
`POST /pipeline/packs/{slug}/repairs/{repair_id}/measurements`.

### Inspector — mini-timeline per target

Below the metric row, a 6-max recent-measurement list for the selected target:

```
─ Historique ─────────────────────────────────
 18:45:12   +3V3  2.87 V  (87% nominal)   anomalous   "avant reflow U7"
 18:52:03   +3V3  0.98 V  (30% nominal)   anomalous   —
 19:02:44   +3V3  3.29 V  (99% nominal)   alive       "après reflow"
```

Each entry clickable to re-highlight the moment in the session. Clean glass
card design, JetBrains Mono font, OKLCH amber/emerald for mode swatches.

### WS handler — agent mirror

```javascript
ws.addEventListener("message", (e) => {
  const msg = JSON.parse(e.data);
  if (msg.type === "simulation.observation_set") {
    SimulationController.setObservation(msg.target, msg.mode, msg.measurement);
  } else if (msg.type === "simulation.observation_clear") {
    SimulationController.clearObservations();
  }
  // ...existing handlers
});
```

## Benchmark suite — extensions

### Fixture regeneration

`scripts/gen_hypothesize_benchmarks.py` is extended with `--modes all` (default
all applicable to node kind). For each `(refdes, applicable_mode)` pair:

- `anomalous`: only for ICs that have at least one outgoing signal edge in
  `typed_edges`. Sample 2-4 `anomalous_comps` and 1-2 non-affected comps as
  `alive`.
- `hot`: for every IC. Self-observation — the scenario just places `{refdes:
  "hot"}` in obs and optional corroborating measurements.
- `shorted`: for every rail that has at least one consumer; pick a consumer
  c, kill its rail, sample the cascade.
- `dead`: identical to current behaviour.

Expected corpus on MNT: ~60 (dead, existing) + ~40 (anomalous, subset with
signal edges) + ~30 (hot, ICs) + ~25 (shorted, rails) ≈ **155 scenarios**.
Fixture file grows to ~20 KB.

### CI gates per mode

`tests/pipeline/schematic/test_hypothesize_accuracy.py` parametrises on
`mode`:

```python
@pytest.mark.parametrize("mode", ["dead", "anomalous", "hot", "shorted"])
def test_top1_accuracy_per_mode(mode):
    records = _run_scenarios(filter_mode=mode)
    top1 = ...
    assert top1 >= THRESHOLDS[mode]["top1"]
```

Conservative starting thresholds (will adjust after the first full run):

| Mode | top-1 | top-3 | MRR |
|---|---|---|---|
| dead | 80% | 90% | 0.85 (established: 95% on current corpus) |
| anomalous | 50% | 70% | 0.65 |
| hot | 70% | 90% | 0.80 (self-observation helps) |
| shorted | 55% | 75% | 0.65 |

Aggregate p95 latency gate stays at 500 ms.

### Weight tuning

`scripts/tune_hypothesize_weights.py` adapted to the new schema. Sweeps the
same 5×4 grid but scores on a weighted average across modes (`dead` weight =
0.4, `anomalous` = 0.3, `shorted` = 0.2, `hot` = 0.1 — reflecting frequency
on the field corpus). Commits the tuned `PENALTY_WEIGHTS` if it improves the
weighted top-3.

## Files impacted

| File | Action | Est. delta |
|---|---|---|
| `api/pipeline/schematic/hypothesize.py` | modify — schema B shapes + `_simulate_failure` + propagation helpers + scoring update + narrative update | ~250 LOC net |
| `api/agent/measurement_memory.py` | **create** — JSONL journal + auto-classify rules + compare/synthesise | ~180 LOC |
| `api/tools/hypothesize.py` | modify — new input fields, pass-through `repair_id` for journal synthesis | +40 LOC |
| `api/tools/measurements.py` | **create** — 6 new tool functions | ~200 LOC |
| `api/tools/ws_events.py` | modify — new event envelopes `observation.set/clear` | +30 LOC |
| `api/agent/manifest.py` | modify — register 6 new tools + update `mb_hypothesize` schema | +120 LOC |
| `api/agent/runtime_direct.py` | modify — dispatch branches for 6 new tools (stash-dance aware) | +60 LOC |
| `api/agent/runtime_managed.py` | modify — same | +60 LOC |
| `api/pipeline/__init__.py` | modify — new measurements routes + updated hypothesize request shape | +80 LOC |
| `web/js/schematic.js` | modify — observations Map migration + contextual picker + metric input + timeline + WS handler | +400 LOC |
| `web/styles/schematic.css` | modify — mode picker, metric row, timeline card | +120 LOC |
| `tests/pipeline/schematic/test_hypothesize.py` | modify — schema B migration + new modes tests | +200 LOC |
| `tests/pipeline/schematic/test_hypothesize_accuracy.py` | modify — per-mode gates | +80 LOC |
| `tests/pipeline/schematic/fixtures/hypothesize_scenarios.json` | regenerate | ~20 KB |
| `tests/agent/test_measurement_memory.py` | **create** | ~150 LOC |
| `tests/tools/test_measurements.py` | **create** | ~180 LOC |
| `tests/tools/test_hypothesize.py` | modify — schema B + new fields | +60 LOC |
| `tests/pipeline/test_hypothesize_endpoint.py` | modify — schema B + measurements routes | +80 LOC |
| `scripts/gen_hypothesize_benchmarks.py` | modify — per-mode scenario generation | +100 LOC |
| `scripts/bench_hypothesize.py` | modify — report per-mode p95 | +40 LOC |
| `scripts/tune_hypothesize_weights.py` | modify — weighted-aggregate accuracy | +40 LOC |

Grand total: ~1900 LOC new/changed + ~700 LOC test/infra. Reviewer-adjusted
estimate once the schema-B migration overhead on already-shipped fixtures,
tests, narrative template, and endpoint is priced in: **~2400–2600 LOC
realistic**, 20–25 tasks on the implementation plan (vs. v1's 16 tasks /
~1800 LOC). 1.3–1.5× the size of the reverse-diagnostic Phase 1 because
this one touches shapes of an already-shipped feature, regenerates
fixtures, re-tunes penalty weights, AND introduces three new frontend
interaction surfaces plus the WS mirror.

## Rollout plan (high level)

The implementation plan will split this into ~20 tasks. Structural arc:

1. **Core migration** (tasks 1-5) — schema B shapes, `_simulate_failure`
   refactor, scoring update, `anomalous` propagation, regenerate fixtures,
   update accuracy gates (per-mode). Breaking change commit.
2. **Measurement memory** (tasks 6-9) — journal model, store module,
   auto-classify table, unit tests.
3. **Agent tools** (tasks 10-13) — `measurements.py` wrappers, manifest
   updates, dispatch in direct + managed. `runtime_direct.py` currently
   has one uncommitted line of Alexis's WIP (`domain=payload.get("domain")`
   in the `mb_schematic_graph` dispatch, well away from where the new
   branches go). A trivial cherry-pick / stash check, not a real conflict
   risk. Integration tests.
4. **HTTP routes** (tasks 14-15) — measurements routes, hypothesize body
   migration, endpoint tests.
5. **Frontend** (tasks 16-18) — contextual mode picker, metric input +
   auto-classify, WS handler, mini-timeline. Browser-verify before commit
   (T16 toggles + metric input, T17 timeline + WS handler).
6. **Bench re-tune + final verify** (tasks 19-20) — run generator, run
   accuracy suite, tune if needed, run perf bench, hero demo with Alexis.

Each group lands in a single commit. Tests pass at every commit.

## Phased follow-ups (out of this spec, same design family)

- **Phase 2 — shorted on rails with unknown culprit**: enumerate consumers
  of the shorted rail as candidates. ~500 LOC.
- **Phase 3 — thermal multi-observation corroboration**: use `hot` obs to
  boost scores of candidates whose `hot_comps` cascade matches. ~200 LOC.
- **Phase 4 — passive component injection**: extend the compiler
  (`net_classifier.py`, `boot_analyzer.py`) to include R/C/D in
  `electrical_graph`. ~1500 LOC, non-trivial since it touches the upstream
  schematic compiler. Reserved for after Phase 1 ships on the bench.
- **Phase 5 — numeric metric scoring**: add a soft proximity term
  `|measured - predicted| / nominal` to the discrete score for tie-breaking.
  Gated on having field data to calibrate the weight. ~300 LOC.

## Open questions

All resolved after the spec review pass. Two were surfaced and locked:

- **Auto-classify of `shorted` rails from voltage alone** — impossible
  purely from a voltage reading (a shorted rail and a dead rail both read
  near-zero). Resolved: voltage ≈ 0 V classifies to `dead` by default;
  promoted to `shorted` only when the tech adds a `note="short"`, a
  concurrent measurement of the upstream source shows over-current stress,
  or the chat agent hints at a short through context. Overvoltage (>110%)
  is surfaced as a distinct `overvoltage` semantic but maps to `shorted`
  in `state_rails` for Phase 1 — the two share downstream effects.
- **WS event naming** — adopted the existing `boardview.<verb>` convention
  with a new `simulation.<verb>` prefix (`simulation.observation_set` /
  `simulation.observation_clear`) and a `_SimEvent` base class mirroring
  `_BVEvent` in `api/tools/ws_events.py`.

## Dette backlog (out of scope entirely)

- Rust port of the simulator + hypothesize for >3-fault or batched use
  cases. p95 230 ms leaves plenty of headroom.
- Cross-session measurement memory at device level (aggregate across
  repair_ids). Could be a `findings.json` enrichment.
- USB-HID multimeter auto-capture. Would replace manual input with a
  stream, significant UX rework.
- SPICE-lite analog simulation. Different product.
