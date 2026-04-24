# Schematic simulator — axes 2 & 3 (continuous failure modes + boardview bridge)

**Status:** draft
**Author:** Alexis Chapellier
**Date:** 2026-04-24
**Companion plan (Tasks 9–10):** `docs/superpowers/plans/2026-04-23-schematic-simulator.md`

## 1. Motivation

The current simulator (`api/pipeline/schematic/simulator.py`) is a binary
event-driven engine: rails are `off | rising | stable`, components are
`off | on | dead`, signals are `low | high | floating`. It tells us
*« kill U7 → +5V dies → U12 doesn't power on »* and that's it.

Real board-level diagnosis is rarely binary. The technician measures
*« +5V at 4.7V »* (sagging — leaky decoupling cap, drifting LDO), or
*« short to GND with 200 Ω »* (resistive short, not a hard short).
And once a candidate is identified, the question becomes *« where do I
probe first ? »* — which the simulator can't answer because it lives in
schematic-space and ignores the physical PCB.

This spec covers two complementary upgrades:

- **Axe 2 — Continuous failure modes.** Extend the state space to
  represent degraded rails, degraded components, resistive shorts, and
  voltage drift; extend the failure-mode catalog in `hypothesize.py` so
  these states can also be **caused** (not only **observed**).
- **Axe 3 — Boardview bridge.** Introduce a dedicated layer that joins
  a `SimulationTimeline` with a parsed `Board` to produce a ranked
  *« probe route »* — the technician's measurement plan in priority
  order with side / coordinates / rationale.

A third transverse concern is added: a scalar **evaluation metric**
(`self_MRR + cascade_recall`) that lets a future autoloop skill decide
whether a simulator mutation is genuinely better than its predecessor.

## 2. Scope

### In scope

- New `RailState` enum value `degraded` and `shorted`; new
  `ComponentState` enum value `degraded`.
- Optional `voltage_pct` per rail in `BoardState` (`1.0` = nominal,
  `0.94` = sagging, `0.0` = dead).
- Two new failure modes in `_PASSIVE_CASCADE_TABLE`:
  - `passive_C.leaky_short` (decoupling cap with finite leakage R)
  - `ic.regulating_low` (LDO/buck whose output drifts low)
- `SimulationEngine` accepts both **causes** (`failures`) and
  **observations** (`rail_overrides`) as inputs.
- New module `api/agent/schematic_boardview_bridge.py` that turns a
  `SimulationTimeline` + a parsed `Board` into an `EnrichedTimeline`
  with a ranked `probe_route`.
- `mb_schematic_graph(query="simulate")` and the HTTP endpoint accept
  `failures` and `rail_overrides`. The tool path (which has access to
  the session board) returns enriched output; the HTTP path returns
  the raw timeline.
- New module `api/pipeline/schematic/evaluator.py` exposing
  `compute_self_mrr`, `compute_cascade_recall`, `compute_score`.
- New CLI `scripts/eval_simulator.py` that prints a one-line JSON
  scorecard.
- New benchmark format `benchmark/scenarios.jsonl` (frozen oracle).

### Out of scope

- SPICE-grade analog modelling (transients, RC time constants,
  inductive ringing). Continuous voltages live as a single
  `voltage_pct` per rail per phase, not a waveform.
- Timing of phases in milliseconds. Phases stay logical, not chronometric
  — datasheet timing windows are tracked in axe 1 (separate future spec).
- Frontend scrubber UI (covered by the existing plan, Tasks 11–14).
- Auto-generation of benchmark scenarios overnight. The bench is created
  once with a human in the loop and frozen; refreshing it is a
  conscious manual decision.
- Mutation engine itself (the autoloop skill consumes the evaluation
  surface this spec defines but is not part of this spec).
- New `ComponentState` values beyond `degraded` (no `under_voltage` /
  `over_voltage` granularity — captured implicitly via `voltage_pct`).

### Hard rules respected

- All code Apache-2.0, no copy from external sources (rule #1, #2).
- Permissive deps only — no new third-party dependency introduced.
- No proprietary hardware artefact lands in the repo. Benchmark
  scenarios cite **public** sources only (forum threads, manufacturer
  datasheets, public repair videos).
- No hallucinated component IDs. Every refdes the simulator emits is
  drawn from the loaded `ElectricalGraph.components` (validated at the
  tool / endpoint boundary, same pattern as Tasks 9–10).

## 3. Architecture

```
api/pipeline/schematic/
  simulator.py              ← MODIFIED — RailState/ComponentState enriched,
                              SimulationEngine accepts `failures` + `rail_overrides`
  hypothesize.py            ← MODIFIED — +2 entries in _PASSIVE_CASCADE_TABLE
                              (passive_C.leaky_short, ic.regulating_low)
  evaluator.py              ← NEW — self_MRR, cascade_recall, scalar score
                              Pure functions, no I/O at call site

api/agent/
  schematic_boardview_bridge.py   ← NEW — pure : (timeline, board) → enriched
                                    Heuristic ranking for probe_route

api/tools/
  schematic.py              ← MODIFIED — query=simulate accepts failures +
                              rail_overrides ; calls bridge when session.board
                              is present, returns raw timeline otherwise

api/pipeline/__init__.py    ← MODIFIED — POST .../simulate accepts failures +
                              rail_overrides ; returns raw timeline only
                              (no session board available in HTTP context)

scripts/
  eval_simulator.py         ← NEW — CLI : prints {score, self_mrr, cascade_recall,
                              per_scenario_breakdown} as one-line JSON

benchmark/
  scenarios.jsonl           ← NEW — frozen oracle, ~10–20 sourced scenarios
  sources/                  ← NEW — local cache of source quotes (URL rot insurance)

tests/
  pipeline/schematic/test_simulator.py     ← +tests for continuous modes,
                                              rail_overrides, failures
  pipeline/schematic/test_hypothesize.py   ← +tests for leaky_short,
                                              regulating_low
  pipeline/schematic/test_evaluator.py     ← NEW
  agent/test_schematic_boardview_bridge.py ← NEW
  tools/test_schematic.py                  ← +tests for enriched output path
```

### Separation of concerns

- `simulator.py` stays **board-agnostic**. It knows about rails,
  components, signals — never about coordinates, layers, or bboxes.
  Testable in isolation against synthetic graphs.
- `bridge.py` is the **only** module that knows about both worlds.
  Pure function `enrich(timeline, board) → EnrichedTimeline`. Easily
  testable with fixture pairs.
- `evaluator.py` only consumes the engine's outputs. Knows nothing of
  the bridge. Decoupled lifetimes.
- The HTTP endpoint **never** returns enriched output, by design — the
  HTTP context has no session, hence no loaded board. Raw timeline only.
  Clients that want enrichment go through the agent (which has session
  board access), or compose `bv_*` tool calls themselves.

## 4. Axe 2 — Continuous failure modes

### 4.1 New state space

In `api/pipeline/schematic/simulator.py`:

```python
RailState = Literal["off", "rising", "stable", "degraded", "shorted"]
ComponentState = Literal["off", "on", "degraded", "dead"]
SignalState = Literal["low", "high", "floating"]   # unchanged
FinalVerdict = Literal["completed", "blocked", "cascade", "degraded"]   # +"degraded"
```

`BoardState` gains one optional field:

```python
class BoardState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase_index: int
    phase_name: str
    rails: dict[str, RailState] = Field(default_factory=dict)
    rail_voltage_pct: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Optional per-rail voltage as a fraction of nominal "
            "(1.0 = nominal, 0.94 = sagging, 0.0 = dead). Only present "
            "when the rail is `degraded` or `shorted` (with finite R), "
            "or when the rail was explicitly observed via rail_overrides. "
            "Absent for plain `stable` / `off`."
        ),
    )
    components: dict[str, ComponentState] = Field(default_factory=dict)
    signals: dict[str, SignalState] = Field(default_factory=dict)
    blocked: bool = False
    blocked_reason: str | None = None
```

`SimulationTimeline` is unchanged structurally — only the inner
`BoardState` shapes evolve. Existing JSON snapshots stay readable
because new fields have defaults.

### 4.2 Inputs — both causes and observations

Two new types, both optional, both passed to `SimulationEngine`:

```python
class Failure(BaseModel):
    """A cause prescribed by the caller — the simulator computes the
    consequences (which rails sag, which components degrade)."""

    model_config = ConfigDict(extra="forbid")

    refdes: str
    mode: Literal[
        "dead",                # baseline mode (existing kill semantics)
        "shorted",             # hard short to GND
        "leaky_short",         # passive_C only — finite R to GND
        "regulating_low",      # ic only — output sags
        "open",                # passive_R / passive_FB — series open
    ]
    value_ohms: float | None = None
    """Required for `leaky_short`. The leakage path resistance (Ω)."""

    voltage_pct: float | None = None
    """Required for `regulating_low`. Target output as a fraction of
    nominal (e.g. 0.94 for an LDO that sags from 5.0V to 4.7V)."""


class RailOverride(BaseModel):
    """An observation supplied by the caller — the simulator forces the
    rail to that state and propagates downstream consequences."""

    model_config = ConfigDict(extra="forbid")

    label: str
    state: RailState
    voltage_pct: float | None = None
    """Required when state is `degraded` (e.g. 0.94 for 4.7V/5V)."""
```

The `SimulationEngine` constructor signature evolves additively:

```python
class SimulationEngine:
    def __init__(
        self,
        electrical: ElectricalGraph,
        *,
        analyzed_boot: AnalyzedBootSequence | None = None,
        killed_refdes: list[str] | None = None,            # backward-compat
        failures: list[Failure] | None = None,             # new
        rail_overrides: list[RailOverride] | None = None,  # new
    ) -> None:
        ...
```

`killed_refdes` is kept and treated as sugar for
`failures=[Failure(refdes=r, mode="dead") for r in killed_refdes]` so
every existing caller (Task 9, Task 10, all current tests) keeps working
without change.

### 4.3 Propagation rules

Rules are linear-time, ordered, and deterministic:

1. **Apply failures first.** Each `Failure` mutates the initial state:
   - `mode=dead` → `components[refdes] = "dead"` (existing semantics).
   - `mode=shorted` → if refdes is a passive_C / passive_R between rail and
     GND, mark the rail `shorted` with `voltage_pct=0`. If the refdes is
     an IC, mark it `dead` and let cascade compute downstream consequences.
   - `mode=leaky_short` (passive_C only) → mark the rail the cap decouples
     as `degraded` with `voltage_pct = max(0.0, 1.0 - leakage_factor)`.
     The exact formula for `leakage_factor` from `value_ohms` is left to
     the implementation plan; the spec only requires it be deterministic,
     monotone (lower R = lower voltage_pct), and saturate at 0.0 for
     hard shorts. A reasonable starting point is
     `leakage_factor = clamp(I_nom × R_pull / V_nom, 0, 1)` where
     `I_nom` is estimated from consumer count × a module-level constant
     (default 50 mA per consumer), but the plan may pick anything
     equivalent. Tests pin behaviour, not the exact curve.
   - `mode=regulating_low` (ic only) → mark every rail this IC sources
     as `degraded` with the requested `voltage_pct`.
   - `mode=open` (passive_R / passive_FB only) → mark the IC powered
     through this resistor as `dead` (rail can't reach it).

2. **Apply rail_overrides second.** Force each rail to the requested
   state and `voltage_pct`. Overrides win over computed cause states
   (a manual observation is more authoritative than an inferred cause).

3. **Run the existing phase walk.** `_stabilise_rails`,
   `_activate_components`, `_assert_triggers`, `_phase_blocked` are
   updated to recognise `degraded` rails:
   - A `degraded` rail with `voltage_pct >= 0.9` is treated as `stable`
     for downstream component activation (within tolerance).
   - A `degraded` rail with `voltage_pct < 0.9` activates its consumers
     as `degraded` rather than `on`.
   - A `degraded` rail with `voltage_pct < 0.5` activates its consumers
     as `dead` (UVLO — under-voltage lockout, a real chip behaviour).
   - A `shorted` rail is functionally equivalent to `off` for activation
     (the IC sees no voltage), but the cascade reason changes.

4. **Cascade pass** is extended:
   - Components with degraded `power_in` propagate `degraded` to any
     rail they themselves source (drift compounds downstream).
   - Tolerance thresholds (0.9 / 0.5) are module-level constants. No
     per-component override in this spec — when needed later, they
     migrate to `ComponentNode.tolerance_v` extracted from datasheets.

5. **Final verdict** gains a `degraded` value: the boot completed with
   no blockage and no dead components, but at least one rail or
   component ended up in a degraded state. This is the *« it boots but
   USB drops every 30 seconds »* class.

### 4.4 New entries in `_PASSIVE_CASCADE_TABLE`

In `api/pipeline/schematic/hypothesize.py`:

```python
_PASSIVE_CASCADE_TABLE: dict[tuple[str, str | None, str], Handler] = {
    ...                                          # existing entries
    ("passive_c", "decoupling", "leaky_short"):  _handle_decoup_leaky,
    ("passive_c", "bulk",       "leaky_short"):  _handle_decoup_leaky,
    ("ic",        None,          "regulating_low"): _handle_ic_reg_low,
}
```

Where:

- `_handle_decoup_leaky(refdes, mode, value_ohms, graph)` returns the
  rail this cap decouples in `degraded` state, with `voltage_pct`
  derived from `value_ohms` via the same simple model the simulator
  uses.
- `_handle_ic_reg_low(refdes, voltage_pct, graph)` returns every rail
  this IC sources in `degraded` state at the requested `voltage_pct`.

Both handlers are pure, deterministic, and < 30 lines each.

`hypothesize` itself learns to **invert** these modes: given an
observed `degraded` rail with measured `voltage_pct`, candidate causes
include `(refdes_decoupling_cap, leaky_short)` and
`(refdes_source_ic, regulating_low)` ranked by F1 against the full
observation set.

## 5. Axe 3 — The boardview bridge

### 5.1 Module shape

`api/agent/schematic_boardview_bridge.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Joins a SimulationTimeline (schematic-space) with a parsed Board
(physical-PCB-space) to produce a measurement-friendly EnrichedTimeline.

Pure module. No I/O. The single entry point is `enrich(timeline, board)`.
"""

from pydantic import BaseModel, ConfigDict, Field

from api.board.model import Board
from api.pipeline.schematic.simulator import SimulationTimeline


class ProbePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refdes: str
    side: str                      # "top" | "bot"
    coords: tuple[float, float]    # (x_mm, y_mm) — converted from mils
    bbox_mm: tuple[tuple[float, float], tuple[float, float]] | None
    reason: str                    # one short sentence
    priority: int                  # 1 = probe first


class EnrichedTimeline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeline: SimulationTimeline
    probe_route: list[ProbePoint] = Field(default_factory=list)
    unmapped_refdes: list[str] = Field(default_factory=list)
    """Refdes that appear in the timeline but have no match in the
    parsed Board (different naming convention, missing from boardview).
    Surfaced so the agent can fall back to schematic-only guidance."""


def enrich(timeline: SimulationTimeline, board: Board) -> EnrichedTimeline:
    ...
```

### 5.2 Probe route ranking heuristic

Each rule contributes one or more `ProbePoint` entries with a fixed
priority value. The route is the sorted concatenation, deduplicated
by refdes (lowest priority wins on tie):

1. **Source rail IC of the failure** — priority `1`. If `timeline` has
   a `blocked_at_phase` with a rail that never stabilised, the source
   IC of that rail emits one ProbePoint. *« Mesure VOUT de U7 — rail
   de sortie principal du régulateur boot. »*
2. **First dead component in cascade** — priority `2`. The earliest
   `dead` component in `cascade_dead_components` that has a `power_in`
   pin emits one ProbePoint. *« Si VOUT de U7 OK, alors U12 mort
   indique un défaut entre les deux. »*
3. **Decoupling caps near the priority-1 IC** — priorities `3..5`.
   Up to three caps from the IC's `decoupling` list, sorted ascending
   by Euclidean distance between the cap's bbox center and the IC's
   bbox center (both converted to mm). *« C42 à 0.8 mm de U7,
   decoupling : suspect leaky_short. »*
4. **Strategic test points on degraded nets** — priorities `6..8`.
   Up to three `test_point` parts on the same net as any degraded /
   shorted rail, sorted ascending by distance to the priority-1 IC.

Total route caps at 8 entries. Any rule whose preconditions don't
hold (no source IC found, no decoupling list, no test points) is
silently skipped — the route just gets shorter.

The heuristic is fully deterministic and unit-testable. When the
boardview lacks a refdes that the timeline references, that refdes is
appended to `unmapped_refdes` so the agent can downgrade gracefully.

### 5.3 Unit conversion

`api/board/model.py:Point` stores `x, y` in mils (1 mil = 0.0254 mm)
per OBV convention. The bridge converts to mm at its boundary so the
agent and frontend always speak millimetres. The constant lives in
`bridge.py` only — `Board` and `Part` keep their native units.

## 6. API surface

### 6.1 Tool — `mb_schematic_graph(query="simulate")`

The tool defined by Task 9 evolves additively:

```python
def mb_schematic_graph(
    *,
    device_slug: str,
    memory_root: Path,
    query: str,
    label: str | None = None,
    refdes: str | None = None,
    index: int | None = None,
    domain: str | None = None,
    killed_refdes: list[str] | None = None,
    failures: list[dict] | None = None,        # new
    rail_overrides: list[dict] | None = None,  # new
    session: SessionState | None = None,        # new — for board access
) -> dict[str, Any]:
    ...
```

When `query == "simulate"`:

1. Validate every refdes in `killed_refdes`, `failures[*].refdes` and
   every label in `rail_overrides[*].label` against the loaded graph.
   Hard rule #5 — unknown identifiers return
   `{found: False, reason: "unknown_refdes"|"unknown_rail",
   invalid: [...], closest_matches: {...}}`.
2. Coerce dicts to `Failure` / `RailOverride` via Pydantic.
3. Run `SimulationEngine(electrical, ..., failures=..., rail_overrides=...).run()`.
4. **If `session is not None and session.board is not None`** :
   - Call `enrich(timeline, session.board)` and return the **compact**
     enriched payload: `{found, query, final_verdict, blocked_at_phase,
     phase_count, cascade_dead_components, cascade_dead_rails,
     probe_route, unmapped_refdes}`. The full per-state dump stays
     server-side as in Task 9 — only the route is added.
5. **Else** : return the same compact payload as Task 9 with no
   `probe_route` field.

The new `session` parameter requires a one-line update at each call
site (`api/agent/runtime_managed.py`, `api/agent/runtime_direct.py`,
`api/agent/dispatch_bv.py`'s sibling tool dispatcher) to pass the
session through. Backward-compat is preserved by the default
`session=None`, so existing direct callers (tests, scripts) keep
working without change.

### 6.2 Endpoint — `POST /pipeline/packs/{slug}/schematic/simulate`

The Task 10 endpoint accepts the new optional fields:

```python
class SimulateRequest(BaseModel):
    killed_refdes: list[str] = Field(default_factory=list)
    failures: list[Failure] = Field(default_factory=list)
    rail_overrides: list[RailOverride] = Field(default_factory=list)
```

Behaviour identical to Task 10 otherwise. The endpoint has **no access
to a session board** by design (HTTP is stateless), so the response is
the raw `SimulationTimeline.model_dump()` — no `probe_route`, no
enrichment. Clients that need the route either go through the agent
WS or compose with `POST /api/board/parse` themselves.

## 7. Evaluation surface

### 7.1 `evaluator.py`

```python
# api/pipeline/schematic/evaluator.py
"""Scalar evaluation of the simulator + hypothesize stack.

Pure functions. Caller loads the graph and the bench from disk and
passes them in. The CLI in scripts/eval_simulator.py wires the I/O.
"""

from pydantic import BaseModel, ConfigDict, Field

from api.pipeline.schematic.schemas import ElectricalGraph
from api.pipeline.schematic.simulator import SimulationEngine


class ScenarioResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    self_mrr_contribution: float
    cascade_recall: float | None    # None when scenario lacks expected cascade


class Scorecard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float                    # 0.6 × self_mrr + 0.4 × cascade_recall
    self_mrr: float
    cascade_recall: float
    n_scenarios: int
    per_scenario: list[ScenarioResult] = Field(default_factory=list)


def compute_self_mrr(graph: ElectricalGraph, *, max_per_kind: int = 50) -> float:
    """For every (refdes, mode) pair, forward-simulate then check
    that hypothesize ranks (refdes, mode) in the top-K candidates from
    the resulting symptoms. Returns mean reciprocal rank ∈ [0, 1].
    Returns 0.0 when no valid (refdes, mode) pairs can be sampled.

    `max_per_kind` caps the number of components per kind sampled to
    keep evaluation O(seconds), not O(minutes), on large graphs.
    Sampling is deterministic (sorted refdes), reproducible across runs.
    """


def compute_cascade_recall(
    graph: ElectricalGraph, scenarios: list[dict]
) -> tuple[float, list[ScenarioResult]]:
    """For each scenario in the bench, forward-simulate the cause and
    compare predicted dead rails / components to the expected set.
    Returns (mean_recall, per_scenario_breakdown).
    """


def compute_score(
    graph: ElectricalGraph, scenarios: list[dict]
) -> Scorecard:
    """Weighted scalar: 0.6 × self_mrr + 0.4 × cascade_recall.

    When `scenarios` is empty, `cascade_recall` is 0.0 and
    `score = 0.6 × self_mrr`. The autoloop should refuse to run with
    an empty bench (single-metric optimization is exactly the gaming
    failure mode this spec calls out)."""
```

The 60/40 weighting is a module-level constant. Adjustable later (the
plan for the autoloop skill calls for re-weighting as real findings
accumulate — see "Future" below).

### 7.2 Benchmark format

`benchmark/scenarios.jsonl` — one JSON object per line:

```json
{
  "id": "iphone-x-u2-dead",
  "device_slug": "iphone-x",
  "cause": {"refdes": "U2", "mode": "dead"},
  "expected_dead_rails": ["PP_VDD_MAIN", "PP1V8", "PP1V05"],
  "expected_dead_components": ["U0202", "U0700"],
  "source_url": "https://example-pro-repair-forum.com/thread/12345",
  "source_quote": "When U2 (Tristar) shorts internally, the entire VDD_MAIN rail collapses, taking down all sequenced rails the PMU drives.",
  "source_archive": "benchmark/sources/a3f9b2c.txt",
  "confidence": 0.85,
  "generated_by": "claude-sonnet-4-6",
  "generated_at": "2026-04-24T22:00:00Z",
  "validated_by_human": true
}
```

**Provenance is mandatory.** Any scenario without a non-empty
`source_url` AND `source_quote` AND `source_archive` is rejected at load
time. This forces benchmark authoring to be *« structuring real human
knowledge »*, not *« inventing plausible-sounding scenarios »*.

`benchmark/sources/` stores a verbatim copy of each quote, hashed by
content. Archives never get deleted, so URL rot doesn't break the
oracle.

### 7.3 CLI

`scripts/eval_simulator.py`:

```bash
python -m scripts.eval_simulator --device iphone-x
# {"score": 0.74, "self_mrr": 0.81, "cascade_recall": 0.63, "n_scenarios": 12}

python -m scripts.eval_simulator --device iphone-x --verbose
# Same payload + per_scenario breakdown
```

Single-line JSON to stdout by default — designed to be consumed by an
autoloop skill that does `score = json.loads(subprocess.check_output(...))["score"]`.

## 8. Tests

### 8.1 Unit

- `tests/pipeline/schematic/test_simulator.py` (extended)
  - Continuous propagation: leaky_short → degraded rail → degraded IC → cascade.
  - rail_overrides win over computed states.
  - UVLO threshold: `voltage_pct < 0.5` activates consumer as `dead`.
  - Tolerance threshold: `voltage_pct >= 0.9` keeps consumer `on`.
  - Backward-compat: `killed_refdes=[X]` produces identical timeline as
    before for binary cases (regression guard).
- `tests/pipeline/schematic/test_hypothesize.py` (extended)
  - `passive_C.leaky_short` produces the right cascade.
  - `ic.regulating_low` propagates `voltage_pct` to all sourced rails.
  - Inverse: given degraded rail observation, both candidates appear
    in top-K hypotheses.
- `tests/pipeline/schematic/test_evaluator.py` (new)
  - `compute_self_mrr` returns 1.0 on a trivial 1-cause graph.
  - `compute_cascade_recall` returns 1.0 when scenario expectations
    match simulator output; 0 when they fully diverge.
  - `compute_score` honours the 0.6 / 0.4 weighting.
  - Sampling is deterministic across runs.
- `tests/agent/test_schematic_boardview_bridge.py` (new)
  - `enrich` produces `probe_route` with priority 1 = source rail IC.
  - Decoupling caps within physical proximity of priority-1 IC are
    correctly ranked.
  - Refdes present in timeline but missing from board land in
    `unmapped_refdes`.
  - Mil → mm conversion is correct (1 mil = 0.0254 mm; 1000 mils = 25.4 mm).
- `tests/tools/test_schematic.py` (extended)
  - `query=simulate` with `failures=[...]` returns valid Pydantic-shaped
    response.
  - `query=simulate` with session and board returns `probe_route` in
    output.
  - `query=simulate` with session but no board returns no `probe_route`
    (downgrade path).
- `tests/pipeline/test_simulate_endpoint.py` (extended)
  - Endpoint accepts `failures` and `rail_overrides` payloads.
  - Endpoint never returns `probe_route` (HTTP path always raw).

### 8.2 Sémantique end-to-end

One additional test against a real device fixture (MNT Reform — already
parsed for boot_analyzer tests):

- Simulate `regulating_low` on a known LDO at `voltage_pct=0.85` →
  asserts that downstream consumers end up `degraded`, not `dead`.
- Simulate `leaky_short` on a known decoupling cap → asserts the rail
  it decouples ends up `degraded`.

### 8.3 Bench-driven (`make test-eval`)

A new make target runs `eval_simulator.py` against the frozen bench
and **fails the build** if `score < 0.5`. This gives CI a hard floor
to catch regressions even before the autoloop ships.

## 9. Implementation order

A separate plan file (`docs/superpowers/plans/2026-04-24-schematic-simulator-axes-2-3.md`)
will break this down into TDD-discrete tasks. The intended order:

1. **Tasks 9–10 from the existing plan** — *prerequisite*. The
   `query=simulate` and `POST .../simulate` endpoints must exist before
   axes 2 and 3 plug into them.
2. **Axe 2 — schemas + engine.** `RailState` / `ComponentState`
   extension, `Failure` / `RailOverride` types, propagation rules.
   Tests first (red), engine after (green).
3. **Axe 2 — hypothesize catalog.** `leaky_short` and `regulating_low`
   handlers. Tests first.
4. **Evaluator + bench.** `evaluator.py`, frozen bench (5 scenarios at
   first — Sonnet-assisted, human-validated), CLI, `make test-eval`.
5. **Axe 3 — bridge module.** `enrich()`, `probe_route` ranking, tests
   with synthetic Board + Timeline fixtures.
6. **API plumbing.** Tool path enriches when session board present;
   HTTP path stays raw. End-to-end tests through the WS for the
   enriched path.
7. **Bench expansion.** Grow to 10–20 scenarios as device coverage
   matures (one scenario per major device family the workshop sees).

## 10. Future (out of this spec)

- **Axe 1 — Datasheet timing windows.** When axes 2 and 3 land, the
  next gap is timing. Bring a separate spec when Sonnet can extract
  PMIC EN→PG windows from datasheets reliably.
- **Per-component tolerance.** Today 0.9 / 0.5 thresholds are global.
  Real ICs have per-pin Vmin/Vmax. Plug datasheet-extracted tolerances
  into `ComponentNode.tolerance_v` when available.
- **Real-finding MRR.** Replace 60% of `self_MRR` weight with
  `repair_MRR` once the workshop has 30+ structured findings.
- **Phase 4 `behavior_analyzer`.** Still deferred per the original
  plan. Re-evaluate after axes 2 and 3 generate enough field data to
  show whether richer behavioural modelling actually moves the
  diagnostic needle.
- **Mutation engine / autoloop skill.** Lives outside `api/`. Consumes
  `eval_simulator.py` and the frozen bench. Subject of its own skill
  spec, not this one.

## 11. Open questions resolved during brainstorming

| Question | Decision |
|---|---|
| Cause-driven vs observation-driven inputs ? | Both, via `failures` + `rail_overrides` |
| State space richness ? | 5 rail states + `voltage_pct` optional |
| `ComponentState` evolution ? | +`degraded` only |
| Failure mode catalog scope ? | 2 new modes : `passive_C.leaky_short`, `ic.regulating_low` |
| Bridge architecture ? | Dedicated `bridge.py` module |
| Bridge output shape ? | Ranked `probe_route` (heuristic) |
| Bench generation strategy ? | Created once, frozen, sourced quotes mandatory |
| Eval metric ? | `0.6 × self_MRR + 0.4 × cascade_recall` |
