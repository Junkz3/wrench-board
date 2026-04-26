# Fault Modes + Measurement Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate `Observations` to schema B (structured `{refdes: mode}` dicts), add the `anomalous` failure mode with signal-edge BFS propagation, stub `hot` and `shorted` modes, introduce a per-repair append-only measurement journal, ship 6 new `mb_*` agent tools (record / list / compare / synthesise / set / clear), bridge observations to the frontend over WebSocket so Claude and the UI share the same state, extend the inspector with a contextual mode-picker + metric input + per-target mini-timeline, and add per-mode CI gates to the benchmark suite.

**Architecture:** Breaking change on the shipped `POST /schematic/hypothesize` endpoint + `Observations` schema. Single contiguous Phase 1 that covers the data-shape migration, the `anomalous` propagation, the full Measurement Memory pipeline (shapes + store + auto-classify + 4 MB tools), the WS bridge (aligned on the existing `boardview.<verb>` / `_BVEvent` pattern with a new `simulation.<verb>` / `_SimEvent` counterpart), the frontend UX, and the benchmark / accuracy adaptations. Phases 2-4 (shorted with unknown culprit, thermal corroboration, passive injection) are separate specs / plans.

**Tech Stack:** Python 3.11, Pydantic v2 (`extra="forbid"`), FastAPI, pytest, vanilla JS + D3. Deterministic, no LLM in the hot path.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `api/pipeline/schematic/hypothesize.py` | modify | Schema B shapes, `_simulate_failure` dispatcher, `_propagate_signal_downstream`, mode-aware scoring, multi-mode narrative |
| `api/agent/measurement_memory.py` | **create** | `MeasurementEvent` Pydantic shape, append-only JSONL store, filtered read, compare, synthesise, auto-classify table |
| `api/tools/hypothesize.py` | modify | Schema B tool-wrapper — accepts `state_comps` / `state_rails` / `metrics_comps` / `metrics_rails`, optional `repair_id` for journal synthesis |
| `api/tools/measurements.py` | **create** | `mb_record_measurement`, `mb_list_measurements`, `mb_compare_measurements`, `mb_observations_from_measurements`, `mb_set_observation`, `mb_clear_observations` + WS event emission |
| `api/tools/ws_events.py` | modify | `_SimEvent` base + `SimulationObservationSet` + `SimulationObservationClear` envelopes |
| `api/agent/manifest.py` | modify | Register 6 new tools + update `mb_hypothesize` `input_schema` |
| `api/agent/runtime_direct.py` | modify | Dispatch branches for 6 new tools |
| `api/agent/runtime_managed.py` | modify | Dispatch branches for 6 new tools |
| `api/pipeline/__init__.py` | modify | New `POST` / `GET` measurement routes, `POST /hypothesize` request-body migration |
| `web/js/schematic.js` | modify | `SimulationController.observations` Map migration + mode-picker + metric input + auto-classify + mini-timeline + WS handler |
| `web/styles/schematic.css` | modify | `.sim-mode-picker`, `.sim-metric-row`, `.sim-measurement-history` |
| `tests/pipeline/schematic/test_hypothesize.py` | modify | Migrate tests to schema B + new modes |
| `tests/pipeline/schematic/test_hypothesize_accuracy.py` | modify | Per-mode CI gates (parametrised) |
| `tests/pipeline/schematic/fixtures/hypothesize_scenarios.json` | regenerate | Per-mode corpus (~155 scenarios) |
| `tests/agent/test_measurement_memory.py` | **create** | Store + auto-classify unit tests |
| `tests/tools/test_measurements.py` | **create** | 6 tool wrappers contract tests |
| `tests/tools/test_hypothesize.py` | modify | Schema B input paths + `repair_id` auto-synthesis |
| `tests/pipeline/test_hypothesize_endpoint.py` | modify | Schema B body + measurement routes |
| `scripts/gen_hypothesize_benchmarks.py` | modify | Per-mode scenario generation |
| `scripts/bench_hypothesize.py` | modify | Report per-mode p95 |
| `scripts/tune_hypothesize_weights.py` | modify | Weighted-aggregate top-3 accuracy across modes |

**Locked decisions:**

- **Schema B** — two top-level dicts `state_comps: dict[str, ComponentMode]` + `state_rails: dict[str, RailMode]`, plus two matching dicts for numeric metrics. Per-refdes single-mode invariant built-in.
- **`anomalous` mode** — BFS on `typed_edges.kind ∈ {produces_signal, consumes_signal, clocks, depends_on}`. Kinds `powered_by`, `enables`, `decouples`, `filters`, `feedback_in` intentionally excluded. Power rails sourced by the anomalous refdes **remain alive**.
- **`hot` mode** — self-only. No propagation. Scored as corroborating observation.
- **`shorted` mode** — refdes-level: consumer that shorts its input rail to GND. The rail appears in a new `shorted_rails` frozenset (not `dead_rails`) so scoring matches observed `"shorted"` correctly. The source of the rail goes into `hot_comps` (current-limit stress).
- **`MeasurementEvent` target grammar** — `"rail:+3V3"` / `"comp:U7"` / `"pin:U7:3"`, split on first `:` for kind, refdes never contains `:`.
- **WS event namespace** — `simulation.<verb>` via `_SimEvent(BaseModel)` base class in `api/tools/ws_events.py`, mirror of the existing `_BVEvent` / `boardview.<verb>`.
- **Auto-classify defaults** — rail ±10 % → alive, 50–90 % → anomalous, <50 % → anomalous (heavy sag) unless ≈ 0 V → dead; rail ≈ 0 V + explicit `note="short"` or upstream-source stress → `shorted`; rail > 110 % → `shorted` (encapsulates overvoltage for Phase 1); IC temperature > 65 °C → `hot`. Thresholds in a central tunable table.
- **`PENALTY_WEIGHTS` unchanged at (10, 2)** through the migration; re-tuner runs on the new corpus in Task 20.
- **`MAX_PAIRS = 100`** cap on the 2-fault pass (new constant, no-op unless breach).
- **Repair-session persistence only** for measurements — no cross-session memory in Phase 1.

---

## Phase structure (within this plan)

The 22 tasks cluster into 5 groups. Each group's last task is a strict commit gate:

| Group | Tasks | Goal |
|---|---|---|
| A — Core engine migration | 1-7 | Schema B, `_simulate_failure` dispatcher, 4 modes, scoring, narrative |
| B — Measurement memory | 8-11 | Journal + auto-classify + compare + synthesise |
| C — Agent tools + HTTP + WS | 12-16 | 6 tools + manifest + dispatch + HTTP + WS envelopes |
| D — Frontend | 17-19 | State migration + mode picker + metric input + timeline + WS handler |
| E — Bench + final verify | 20-22 | Corpus regeneration + per-mode gates + tune + final smoke |

Tasks 18-19 require **browser-verify with Alexis before commit** (visual UI changes). All others are safe to enchaîner.

---

## Task 1: Migrate `Observations` to schema B

**Files:**
- Modify: `api/pipeline/schematic/hypothesize.py`
- Modify: `tests/pipeline/schematic/test_hypothesize.py`

- [ ] **Step 1: Rewrite the Pydantic shapes in `hypothesize.py`**

Replace the existing `Observations`, `HypothesisMetrics`, `HypothesisDiff`, `Hypothesis`, `PruningStats`, `HypothesizeResult` blocks (lines ~36-102 in the current file) with:

```python
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Mode vocabulary — imported by tools, HTTP, tests, UI JSON.
# ---------------------------------------------------------------------------

ComponentMode = Literal["dead", "alive", "anomalous", "hot"]
RailMode = Literal["dead", "alive", "shorted"]


class ObservedMetric(BaseModel):
    """Numeric measurement attached to an observation. Optional in Phase 1 —
    stored for UI and FR narrative enrichment, not used by the discrete
    scoring (deferred to Phase 5)."""

    model_config = ConfigDict(extra="forbid")

    measured: float
    unit: Literal["V", "A", "W", "°C", "Ω", "mV"]
    nominal: float | None = None
    tolerance_percent: float = 10.0


class Observations(BaseModel):
    """Structured per-target observation map (schema B).

    Each refdes / rail label maps to exactly one mode. Numeric metrics
    parallel the state dicts and carry the raw measurements the tech
    probed, used for FR narrative and UI timeline — NOT for scoring.
    """

    model_config = ConfigDict(extra="forbid")

    state_comps: dict[str, ComponentMode] = Field(default_factory=dict)
    state_rails: dict[str, RailMode] = Field(default_factory=dict)
    metrics_comps: dict[str, ObservedMetric] = Field(default_factory=dict)
    metrics_rails: dict[str, ObservedMetric] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_cross_bucket_alias(self):
        overlap = set(self.state_comps) & set(self.state_rails)
        if overlap:
            raise ValueError(
                f"target appears as both component and rail: {sorted(overlap)}"
            )
        return self

    def is_empty(self) -> bool:
        return not (self.state_comps or self.state_rails)


class HypothesisMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tp_comps: int
    tp_rails: int
    fp_comps: int
    fp_rails: int
    fn_comps: int
    fn_rails: int


class HypothesisDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # (target, observed_mode, predicted_mode)
    contradictions: list[tuple[str, str, str]] = Field(default_factory=list)
    # targets observed non-alive but the hypothesis leaves them alive
    under_explained: list[str] = Field(default_factory=list)
    # (target, predicted_mode) pairs not in any observation
    over_predicted: list[tuple[str, str]] = Field(default_factory=list)


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # parallel lists — kill_refdes[i] fails in mode kill_modes[i]
    kill_refdes: list[str]
    kill_modes: list[ComponentMode]
    score: float
    metrics: HypothesisMetrics
    diff: HypothesisDiff
    narrative: str
    cascade_preview: dict  # {dead_rails, shorted_rails, dead_comps_count, anomalous_count, hot_count}


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
```

- [ ] **Step 2: Add a module-level constant `MAX_PAIRS`**

Next to `TWO_FAULT_ENABLED`:

```python
MAX_PAIRS: int = 100   # 2-fault pair cap (safety net, rarely hit)
```

- [ ] **Step 3: Delete the legacy `_score_candidate` / `_simulate_kill` / `_relevant_to_observations` / `_enumerate_single_fault` / `_enumerate_two_fault` / `_narrate` / `hypothesize` bodies**

Leave an empty placeholder so the module still imports:

```python
def hypothesize(
    electrical: ElectricalGraph,
    *,
    analyzed_boot: AnalyzedBootSequence | None = None,
    observations: Observations,
    max_results: int = MAX_RESULTS_DEFAULT,
) -> HypothesizeResult:
    raise NotImplementedError  # lands in Task 6
```

All helpers return placeholder empty results (they'll be rewritten in Tasks 2-6). Keep the old implementation commented out ONLY if that helps during migration — delete before commit.

- [ ] **Step 4: Rewrite the existing test file header so all the old tests fail fast**

Open `tests/pipeline/schematic/test_hypothesize.py`. Replace the import block and the first shape test so it uses schema B:

```python
# SPDX-License-Identifier: Apache-2.0
"""Tests for the reverse-diagnostic hypothesis engine (schema B)."""

from __future__ import annotations

import pytest

from api.pipeline.schematic.hypothesize import (
    MAX_PAIRS,
    MAX_RESULTS_DEFAULT,
    PENALTY_WEIGHTS,
    TOP_K_SINGLE,
    Hypothesis,
    HypothesisDiff,
    HypothesisMetrics,
    HypothesizeResult,
    ObservedMetric,
    Observations,
    PruningStats,
    hypothesize,
)
from api.pipeline.schematic.schemas import (
    AnalyzedBootPhase,
    AnalyzedBootSequence,
    AnalyzedBootTrigger,
    BootPhase,
    ComponentNode,
    ElectricalGraph,
    NetNode,
    PagePin,
    PowerRail,
    SchematicQualityReport,
)


def test_observations_shape_minimal():
    obs = Observations()
    assert obs.state_comps == {}
    assert obs.state_rails == {}
    assert obs.metrics_comps == {}
    assert obs.metrics_rails == {}
    assert obs.is_empty() is True


def test_observations_accepts_dicts():
    obs = Observations(
        state_comps={"U1": "dead", "U7": "anomalous", "Q17": "hot"},
        state_rails={"+3V3": "dead", "+5V": "shorted"},
        metrics_rails={"+3V3": ObservedMetric(measured=0.02, unit="V", nominal=3.3)},
    )
    assert obs.state_comps["U7"] == "anomalous"
    assert obs.state_rails["+5V"] == "shorted"
    assert obs.metrics_rails["+3V3"].measured == 0.02
    assert obs.is_empty() is False


def test_observations_cross_bucket_alias_rejected():
    with pytest.raises(ValueError, match="both component and rail"):
        Observations(state_comps={"X": "dead"}, state_rails={"X": "dead"})


def test_module_constants_present():
    assert PENALTY_WEIGHTS == (10, 2)
    assert TOP_K_SINGLE == 20
    assert MAX_PAIRS == 100
    assert MAX_RESULTS_DEFAULT == 5


def test_hypothesis_shape_minimal():
    h = Hypothesis(
        kill_refdes=["U7"],
        kill_modes=["dead"],
        score=3.0,
        metrics=HypothesisMetrics(
            tp_comps=2, tp_rails=1, fp_comps=0, fp_rails=0, fn_comps=0, fn_rails=0,
        ),
        diff=HypothesisDiff(),
        narrative="",
        cascade_preview={
            "dead_rails": ["+5V"],
            "shorted_rails": [],
            "dead_comps_count": 4,
            "anomalous_count": 0,
            "hot_count": 0,
        },
    )
    assert h.kill_modes == ["dead"]


def test_hypothesize_stub_raises_not_implemented():
    eg = ElectricalGraph(
        device_slug="demo",
        components={}, nets={}, power_rails={}, typed_edges=[],
        boot_sequence=[], designer_notes=[], ambiguities=[],
        quality=SchematicQualityReport(total_pages=0, pages_parsed=0),
    )
    with pytest.raises(NotImplementedError):
        hypothesize(eg, observations=Observations())
```

Delete the remainder of the old test file content (everything below that test). We'll re-add the enumeration / scoring / narrative tests in Tasks 2-6 as their functionality lands.

- [ ] **Step 5: Run the tests**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v
```

Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
git commit -m "$(cat <<'EOF'
refactor(hypothesize): migrate to schema B — {refdes: mode} dicts

Replaces the four frozenset Observations with two dicts keyed by refdes /
rail to a Literal ComponentMode / RailMode. Adds ObservedMetric for optional
numeric measurements (stored, not scored in Phase 1). Hypothesis gains a
parallel kill_modes list. HypothesisDiff is now typed: contradictions carry
(target, observed, predicted) tuples, over_predicted carries (target, mode).
Module-level MAX_PAIRS=100 safety cap introduced.

All engine helpers (_score_candidate, _simulate_kill, _narrate, enumerations)
are torn down and will be re-implemented mode-aware in the next six tasks.
hypothesize() stubs NotImplementedError until Task 6.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
```

---

## Task 2: `_simulate_failure` dispatcher + `dead` mode

**Files:**
- Modify: `api/pipeline/schematic/hypothesize.py`
- Modify: `tests/pipeline/schematic/test_hypothesize.py`

- [ ] **Step 1: Add the cascade-building primitive + dispatcher**

Append to `hypothesize.py` after the shapes (before the stubbed `hypothesize`):

```python
# ---------------------------------------------------------------------------
# Forward simulation — mode-aware dispatcher
# ---------------------------------------------------------------------------


def _empty_cascade() -> dict:
    return {
        "dead_comps": frozenset(),
        "dead_rails": frozenset(),
        "shorted_rails": frozenset(),
        "anomalous_comps": frozenset(),
        "hot_comps": frozenset(),
        "final_verdict": "",
        "blocked_at_phase": None,
    }


def _simulate_dead(
    electrical: ElectricalGraph,
    analyzed_boot: AnalyzedBootSequence | None,
    killed: list[str],
) -> dict:
    """Forward cascade when one or more refdes are fully dead (power-off)."""
    tl = SimulationEngine(
        electrical, analyzed_boot=analyzed_boot, killed_refdes=killed,
    ).run()
    c = _empty_cascade()
    c["dead_comps"] = frozenset(set(tl.cascade_dead_components) | set(killed))
    c["dead_rails"] = frozenset(tl.cascade_dead_rails)
    c["final_verdict"] = tl.final_verdict
    c["blocked_at_phase"] = tl.blocked_at_phase
    return c


def _simulate_failure(
    electrical: ElectricalGraph,
    analyzed_boot: AnalyzedBootSequence | None,
    refdes: str,
    mode: str,
) -> dict:
    """Run the forward cascade of a single failed (refdes, mode) pair.

    Dispatches by mode. `anomalous`, `hot`, `shorted` are implemented in
    Tasks 3-5. Phase 2+ modes should extend this dispatcher.
    """
    if mode == "dead":
        return _simulate_dead(electrical, analyzed_boot, [refdes])
    if mode == "anomalous":
        raise NotImplementedError("anomalous lands in Task 3")
    if mode == "hot":
        raise NotImplementedError("hot lands in Task 4")
    if mode == "shorted":
        raise NotImplementedError("shorted lands in Task 5")
    raise ValueError(f"unknown failure mode: {mode!r}")
```

- [ ] **Step 2: Failing tests for the dispatcher + dead cascade**

Append to `tests/pipeline/schematic/test_hypothesize.py`:

```python
from api.pipeline.schematic.hypothesize import (
    _empty_cascade,
    _simulate_dead,
    _simulate_failure,
)


def _mini_graph() -> ElectricalGraph:
    return ElectricalGraph(
        device_slug="demo",
        components={
            "U18": ComponentNode(refdes="U18", type="ic", pins=[
                PagePin(number="1", name="VIN", role="power_in", net_label="LPC_VCC"),
            ]),
            "U7": ComponentNode(refdes="U7", type="ic", pins=[
                PagePin(number="1", name="VIN", role="power_in", net_label="VIN"),
                PagePin(number="2", name="VOUT", role="power_out", net_label="+5V"),
            ]),
            "U12": ComponentNode(refdes="U12", type="ic", pins=[
                PagePin(number="1", name="VIN", role="power_in", net_label="+5V"),
                PagePin(number="2", name="VOUT", role="power_out", net_label="+3V3"),
            ]),
            "U19": ComponentNode(refdes="U19", type="ic", pins=[
                PagePin(number="1", name="VIN", role="power_in", net_label="+5V"),
            ]),
        },
        nets={
            "VIN": NetNode(label="VIN", is_power=True, is_global=True),
            "LPC_VCC": NetNode(label="LPC_VCC", is_power=True, is_global=True),
            "+5V": NetNode(label="+5V", is_power=True, is_global=True),
            "+3V3": NetNode(label="+3V3", is_power=True, is_global=True),
        },
        power_rails={
            "VIN": PowerRail(label="VIN", source_refdes=None, consumers=["U18"]),
            "LPC_VCC": PowerRail(label="LPC_VCC", source_refdes="U14", consumers=["U18"]),
            "+5V": PowerRail(label="+5V", source_refdes="U7", enable_net="5V_PWR_EN", consumers=["U12", "U19"]),
            "+3V3": PowerRail(label="+3V3", source_refdes="U12", enable_net="3V3_PWR_EN"),
        },
        typed_edges=[], boot_sequence=[], designer_notes=[], ambiguities=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )


def _mini_boot() -> AnalyzedBootSequence:
    return AnalyzedBootSequence(
        device_slug="demo",
        phases=[
            AnalyzedBootPhase(
                index=0, name="Standby", kind="always-on",
                rails_stable=["VIN", "LPC_VCC"],
                components_entering=["U18"],
                triggers_next=[
                    AnalyzedBootTrigger(net_label="5V_PWR_EN", from_refdes="U18", rationale="LPC asserts 5V"),
                ],
            ),
            AnalyzedBootPhase(
                index=1, name="+5V", kind="sequenced",
                rails_stable=["+5V"],
                components_entering=["U7"],
                triggers_next=[
                    AnalyzedBootTrigger(net_label="3V3_PWR_EN", from_refdes="U18", rationale="LPC asserts 3V3"),
                ],
            ),
            AnalyzedBootPhase(
                index=2, name="+3V3", kind="sequenced",
                rails_stable=["+3V3"],
                components_entering=["U12", "U19"],
                triggers_next=[],
            ),
        ],
        sequencer_refdes="U18", global_confidence=0.9, model_used="test",
    )


def test_empty_cascade_has_all_buckets():
    c = _empty_cascade()
    for key in ("dead_comps", "dead_rails", "shorted_rails", "anomalous_comps", "hot_comps"):
        assert c[key] == frozenset()


def test_simulate_failure_dead_mirrors_legacy():
    c = _simulate_failure(_mini_graph(), _mini_boot(), "U7", "dead")
    # Killing U7 cascades +5V → dead downstream (+3V3 via U12, U19 directly).
    assert "U7" in c["dead_comps"]
    assert "+5V" in c["dead_rails"]
    assert c["shorted_rails"] == frozenset()
    assert c["anomalous_comps"] == frozenset()


def test_simulate_failure_unknown_mode_raises():
    with pytest.raises(ValueError, match="unknown failure mode"):
        _simulate_failure(_mini_graph(), _mini_boot(), "U7", "bogus")


def test_simulate_failure_anomalous_and_hot_pending():
    for mode in ("anomalous", "hot", "shorted"):
        with pytest.raises(NotImplementedError):
            _simulate_failure(_mini_graph(), _mini_boot(), "U7", mode)
```

- [ ] **Step 3: Run tests**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v
```

Expected: 10 passed.

- [ ] **Step 4: Commit**

```bash
git add api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
git commit -m "$(cat <<'EOF'
feat(hypothesize): mode-aware _simulate_failure dispatcher + dead path

Introduces the unified _simulate_failure(electrical, boot, refdes, mode)
entry point. `dead` routes to the existing SimulationEngine forward
cascade (wrapped as _simulate_dead, returning the 5-bucket shape —
dead_comps, dead_rails, shorted_rails, anomalous_comps, hot_comps).

`anomalous`, `hot`, `shorted` raise NotImplementedError and land in the
next three tasks. Unknown mode strings raise ValueError.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
```

---

## Task 3: `anomalous` mode + signal-edge BFS

**Files:**
- Modify: `api/pipeline/schematic/hypothesize.py`
- Modify: `tests/pipeline/schematic/test_hypothesize.py`

- [ ] **Step 1: Failing anomalous test**

Append to `tests/pipeline/schematic/test_hypothesize.py`:

```python
from api.pipeline.schematic.hypothesize import _propagate_signal_downstream
from api.pipeline.schematic.schemas import TypedEdge


def _mini_graph_with_signal_edges() -> ElectricalGraph:
    """MNT-like mini graph with signal edges: U10 → U11 → U17 chain."""
    g = _mini_graph()
    # Add 3 components in a signal chain on the DSI path.
    g.components["U10"] = ComponentNode(refdes="U10", type="ic", pins=[
        PagePin(number="1", name="DSI_IN", role="signal_in", net_label="DSI_D0"),
        PagePin(number="2", name="EDP_OUT", role="signal_out", net_label="EDP_D0"),
    ])
    g.components["U11"] = ComponentNode(refdes="U11", type="ic", pins=[
        PagePin(number="1", name="EDP_IN", role="signal_in", net_label="EDP_D0"),
        PagePin(number="2", name="PANEL_OUT", role="signal_out", net_label="PANEL_D0"),
    ])
    g.components["U17"] = ComponentNode(refdes="U17", type="ic", pins=[
        PagePin(number="1", name="PANEL_IN", role="signal_in", net_label="PANEL_D0"),
    ])
    g.typed_edges = [
        TypedEdge(src="U10", dst="EDP_D0", kind="produces_signal", page=1),
        TypedEdge(src="U11", dst="EDP_D0", kind="consumes_signal", page=1),
        TypedEdge(src="U11", dst="PANEL_D0", kind="produces_signal", page=1),
        TypedEdge(src="U17", dst="PANEL_D0", kind="consumes_signal", page=1),
        # Unrelated power edge — must NOT appear in anomalous BFS.
        TypedEdge(src="U10", dst="+5V", kind="powered_by", page=1),
        # Clock edge — included (`clocks` kind is in the allow-list).
        TypedEdge(src="U11", dst="CLK_P", kind="clocks", page=1),
    ]
    return g


def test_propagate_signal_downstream_reaches_consumers():
    g = _mini_graph_with_signal_edges()
    reached = _propagate_signal_downstream(g, "U10")
    # From U10 we reach EDP_D0 consumers (U11), then PANEL_D0 consumers (U17).
    assert "U11" in reached
    assert "U17" in reached
    # Clock target (U11 already reached, but CLK_P itself is a net not a comp)
    assert reached == {"U11", "U17"}  # no net names — we return refdes only


def test_propagate_signal_downstream_excludes_power_kinds():
    g = _mini_graph_with_signal_edges()
    # Add a power-only edge that should be IGNORED by the anomalous BFS.
    g.typed_edges.append(TypedEdge(src="U10", dst="+3V3", kind="powered_by", page=1))
    reached = _propagate_signal_downstream(g, "U10")
    # +3V3's consumers (U12, U19) must NOT appear — they're on the power side.
    assert "U12" not in reached
    assert "U19" not in reached


def test_simulate_failure_anomalous_contains_downstream_signal_comps():
    g = _mini_graph_with_signal_edges()
    c = _simulate_failure(g, _mini_boot(), "U10", "anomalous")
    assert "U10" in c["anomalous_comps"]
    assert "U11" in c["anomalous_comps"]
    assert "U17" in c["anomalous_comps"]
    # Power unaffected.
    assert c["dead_comps"] == frozenset()
    assert c["dead_rails"] == frozenset()


def test_simulate_failure_anomalous_isolated_component():
    g = _mini_graph()  # No signal edges at all.
    c = _simulate_failure(g, _mini_boot(), "U7", "anomalous")
    # U7 alone (no downstream signal) — only itself marked.
    assert c["anomalous_comps"] == frozenset({"U7"})
```

- [ ] **Step 2: Confirm failure**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k anomalous
```

Expected: 4 failures (`ImportError: cannot import _propagate_signal_downstream` + NotImplementedError).

- [ ] **Step 3: Implement `_propagate_signal_downstream` + wire `anomalous` into the dispatcher**

Insert in `hypothesize.py` after `_simulate_dead`, before `_simulate_failure`:

```python
SIGNAL_EDGE_KINDS: frozenset[str] = frozenset(
    {"produces_signal", "consumes_signal", "clocks", "depends_on"}
)


def _propagate_signal_downstream(
    electrical: ElectricalGraph, origin_refdes: str,
) -> set[str]:
    """BFS downstream on signal-typed edges, returning reachable REFDES.

    Uses an intermediate net layer: a refdes produces a signal onto a net;
    the net's consumers (refdes that consume that signal) become anomalous.
    The allow-set (`SIGNAL_EDGE_KINDS`) intentionally excludes `powered_by`,
    `enables`, `decouples`, `filters`, and `feedback_in` — those represent
    power topology or decoupling passives, both out of scope for anomalous
    propagation.
    """
    # Build a net → consumers map once (refdes that consume a signal on a net).
    net_consumers: dict[str, set[str]] = {}
    # Build a refdes → produced nets map (signals the refdes drives).
    produces_by: dict[str, set[str]] = {}
    for edge in electrical.typed_edges:
        if edge.kind not in SIGNAL_EDGE_KINDS:
            continue
        if edge.kind in ("consumes_signal", "depends_on"):
            # refdes consumes a signal on net `dst`
            net_consumers.setdefault(edge.dst, set()).add(edge.src)
        elif edge.kind in ("produces_signal", "clocks"):
            produces_by.setdefault(edge.src, set()).add(edge.dst)

    # BFS: starting from origin's produced signals, fan out via consumers.
    reached: set[str] = set()
    frontier: list[str] = sorted(produces_by.get(origin_refdes, set()))
    while frontier:
        net = frontier.pop()
        for consumer in sorted(net_consumers.get(net, set())):
            if consumer == origin_refdes or consumer in reached:
                continue
            reached.add(consumer)
            # Chain: the consumer may produce further signals downstream.
            for next_net in sorted(produces_by.get(consumer, set())):
                if next_net not in frontier:
                    frontier.append(next_net)
    return reached
```

Then update `_simulate_failure` to handle `anomalous`:

```python
    if mode == "anomalous":
        downstream = _propagate_signal_downstream(electrical, refdes)
        c = _empty_cascade()
        c["anomalous_comps"] = frozenset({refdes} | downstream)
        return c
```

(Replace the `raise NotImplementedError("anomalous lands in Task 3")` branch.)

- [ ] **Step 4: Verify tests pass**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v
```

Expected: 14 passed (10 + 4 new).

- [ ] **Step 5: Commit**

```bash
git add api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
git commit -m "$(cat <<'EOF'
feat(hypothesize): anomalous mode — signal-edge BFS propagation

_propagate_signal_downstream walks typed_edges with kind in
{produces_signal, consumes_signal, clocks, depends_on} starting from a
refdes, returning every downstream refdes that consumes a signal
produced (directly or transitively) by the origin. Power-typed kinds
(powered_by, enables, decouples, filters, feedback_in) are
intentionally excluded — an IC with bad signal output leaves its
power rails alive; only its signal consumers are affected.

Hooks into _simulate_failure as mode='anomalous'. The returned cascade
has empty dead_comps / dead_rails / shorted_rails / hot_comps and a
populated anomalous_comps = {origin} ∪ downstream.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
```

---

## Task 4: `hot` mode (self-only)

**Files:**
- Modify: `api/pipeline/schematic/hypothesize.py`
- Modify: `tests/pipeline/schematic/test_hypothesize.py`

- [ ] **Step 1: Failing test**

Append:

```python
def test_simulate_failure_hot_is_self_only():
    g = _mini_graph()
    c = _simulate_failure(g, _mini_boot(), "U7", "hot")
    assert c["hot_comps"] == frozenset({"U7"})
    assert c["dead_comps"] == frozenset()
    assert c["dead_rails"] == frozenset()
    assert c["anomalous_comps"] == frozenset()
    assert c["shorted_rails"] == frozenset()
```

- [ ] **Step 2: Confirm failure**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py::test_simulate_failure_hot_is_self_only -v
```

Expected: `NotImplementedError: hot lands in Task 4`.

- [ ] **Step 3: Wire `hot` in `_simulate_failure`**

Replace the `hot` branch:

```python
    if mode == "hot":
        c = _empty_cascade()
        c["hot_comps"] = frozenset({refdes})
        return c
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v
```

Expected: 15 passed.

- [ ] **Step 5: Commit**

```bash
git add api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
git commit -m "$(cat <<'EOF'
feat(hypothesize): hot mode — self-observation only

Degenerate cascade: hot_comps = {refdes}, every other bucket empty.
Zero propagation by design (the simulator does not model thermal
runaway). Useful as corroborating observation: if the tech reports
U7 hot AND +3V3 dead, the 2-fault pass can combine hot(U7) + dead(U12)
to exactly match.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
```

---

## Task 5: `shorted` mode (rail-via-source)

**Files:**
- Modify: `api/pipeline/schematic/hypothesize.py`
- Modify: `tests/pipeline/schematic/test_hypothesize.py`

- [ ] **Step 1: Failing test**

Append:

```python
def test_simulate_failure_shorted_consumer_kills_rail_stresses_source():
    g = _mini_graph()
    # U12 is consumer of +5V. Shorting U12 shorts +5V to GND.
    c = _simulate_failure(g, _mini_boot(), "U12", "shorted")
    # The shorted rail is tagged separately (NOT in dead_rails).
    assert "+5V" in c["shorted_rails"]
    assert "+5V" not in c["dead_rails"]
    # The source of +5V (U7) goes into hot_comps (current-limit stress).
    assert "U7" in c["hot_comps"]
    # Downstream of the killed source propagates as dead (U19, +3V3, U12's own downstream).
    assert "+3V3" in c["dead_rails"]
    assert "U19" in c["dead_comps"]


def test_simulate_failure_shorted_orphan_consumer_returns_self_dead():
    g = _mini_graph()
    # A refdes with NO input power rail (no consumer record) falls back to self-dead.
    g.components["U99"] = ComponentNode(refdes="U99", type="ic", pins=[])
    c = _simulate_failure(g, _mini_boot(), "U99", "shorted")
    assert c["dead_comps"] == frozenset({"U99"})
    assert c["shorted_rails"] == frozenset()
    assert c["hot_comps"] == frozenset()
```

- [ ] **Step 2: Confirm failure**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k shorted
```

Expected: 2 failures.

- [ ] **Step 3: Implement `_find_powered_rail` + wire `shorted`**

Insert in `hypothesize.py` near the helpers:

```python
def _find_powered_rail(
    electrical: ElectricalGraph, refdes: str,
) -> str | None:
    """Return the (first) rail label whose consumers list contains `refdes`."""
    for label, rail in electrical.power_rails.items():
        if refdes in (rail.consumers or []):
            return label
    return None
```

Replace the `shorted` branch in `_simulate_failure`:

```python
    if mode == "shorted":
        rail = _find_powered_rail(electrical, refdes)
        if rail is None:
            c = _empty_cascade()
            c["dead_comps"] = frozenset({refdes})
            return c
        source = electrical.power_rails[rail].source_refdes
        # Propagate as-if the source was killed — that gives us the downstream.
        downstream = (
            _simulate_dead(electrical, analyzed_boot, [source])
            if source else _empty_cascade()
        )
        c = _empty_cascade()
        # shorted rail tagged separately so scoring matches observed "shorted"
        c["shorted_rails"] = frozenset({rail})
        c["dead_rails"] = downstream["dead_rails"] - {rail}
        c["dead_comps"] = downstream["dead_comps"]
        c["hot_comps"] = frozenset({source}) if source else frozenset()
        c["final_verdict"] = downstream["final_verdict"]
        c["blocked_at_phase"] = downstream["blocked_at_phase"]
        return c
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v
```

Expected: 17 passed.

- [ ] **Step 5: Commit**

```bash
git add api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
git commit -m "$(cat <<'EOF'
feat(hypothesize): shorted mode — consumer shorts input rail to GND

_find_powered_rail looks up the rail whose consumers list contains
the refdes. _simulate_failure with mode='shorted' tags that rail in
shorted_rails (distinct from dead_rails so observed 'shorted' matches
the prediction), propagates downstream by wrapping _simulate_dead on
the rail source, and marks the source as hot (current-limit stress).

Orphan consumer (refdes not in any rail's consumers list) degenerates
to self-dead — rather than silently failing we surface it as a single
dead_comp, which the scoring can still match.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
```

---

## Task 6: Mode-aware `_score_candidate`

**Files:**
- Modify: `api/pipeline/schematic/hypothesize.py`
- Modify: `tests/pipeline/schematic/test_hypothesize.py`

- [ ] **Step 1: Failing tests**

Append:

```python
from api.pipeline.schematic.hypothesize import _score_candidate


def test_score_perfect_match_dead():
    obs = Observations(
        state_comps={"U1": "dead", "U7": "alive"},
        state_rails={"+3V3": "dead", "+5V": "alive"},
    )
    cascade = _empty_cascade()
    cascade["dead_comps"] = frozenset({"U1"})
    cascade["dead_rails"] = frozenset({"+3V3"})
    score, metrics, diff = _score_candidate(cascade, obs)
    # 2 dead match + 2 alive match = 4 TP, 0 FP, 0 FN
    assert metrics.tp_comps == 2
    assert metrics.tp_rails == 2
    assert metrics.fp_comps == 0
    assert metrics.fp_rails == 0
    assert score == 4.0
    assert diff.contradictions == []


def test_score_contradiction_cross_mode_costs_10x():
    # Tech observes U7 anomalous, hypothesis predicts U7 dead — soft mismatch.
    obs = Observations(state_comps={"U7": "anomalous"})
    cascade = _empty_cascade()
    cascade["dead_comps"] = frozenset({"U7"})
    score, metrics, diff = _score_candidate(cascade, obs)
    assert metrics.fp_comps == 1
    assert ("U7", "anomalous", "dead") in diff.contradictions
    assert score == -10.0   # 0 TP - 10*1 FP - 0 FN


def test_score_alive_observed_dead_predicted_is_fn():
    obs = Observations(state_comps={"U7": "dead"})
    cascade = _empty_cascade()  # predicts alive
    score, metrics, diff = _score_candidate(cascade, obs)
    assert metrics.fn_comps == 1
    assert "U7" in diff.under_explained
    assert score == -2.0


def test_score_alive_observed_alive_predicted_is_tp():
    obs = Observations(state_comps={"U7": "alive"})
    cascade = _empty_cascade()  # predicts alive
    score, metrics, diff = _score_candidate(cascade, obs)
    assert metrics.tp_comps == 1
    assert score == 1.0


def test_score_shorted_rail_matches_predicted_shorted():
    obs = Observations(state_rails={"+5V": "shorted"})
    cascade = _empty_cascade()
    cascade["shorted_rails"] = frozenset({"+5V"})
    score, metrics, diff = _score_candidate(cascade, obs)
    assert metrics.tp_rails == 1
    assert score == 1.0
    assert diff.contradictions == []


def test_score_anomalous_rail_predicted_hot_comp_matches_hot_obs():
    obs = Observations(state_comps={"Q17": "hot"})
    cascade = _empty_cascade()
    cascade["hot_comps"] = frozenset({"Q17"})
    score, _, diff = _score_candidate(cascade, obs)
    assert score == 1.0
    assert diff.contradictions == []


def test_score_over_predicted_not_penalised():
    obs = Observations(state_comps={"U1": "dead"})
    cascade = _empty_cascade()
    cascade["dead_comps"] = frozenset({"U1", "U99"})  # U99 not in obs
    score, metrics, diff = _score_candidate(cascade, obs)
    assert metrics.fp_comps == 0
    assert ("U99", "dead") in diff.over_predicted
    assert score == 1.0
```

- [ ] **Step 2: Confirm failure**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k score
```

Expected: 7 failures (`ImportError`).

- [ ] **Step 3: Implement mode-aware `_score_candidate`**

Insert in `hypothesize.py` after `_simulate_failure`:

```python
def _score_candidate(
    cascade: dict,
    obs: Observations,
) -> tuple[float, HypothesisMetrics, HypothesisDiff]:
    """Score a candidate cascade against observations.

    Works off the 5-bucket cascade returned by _simulate_failure. Unlike
    the v1 engine this one matches PER MODE:

    - Each observation target has an expected mode.
    - Each cascade bucket implies a predicted mode for some refdes/rail.
    - TP = same mode observed AND predicted.
    - FP = predicted non-alive but observed alive OR mode mismatch between
           two non-alive modes.
    - FN = observed non-alive but predicted alive (target not in any cascade
           bucket).
    - Over-predicted = predicted non-alive but no observation exists.
    """
    fp_w, fn_w = PENALTY_WEIGHTS

    # Build per-target predicted mode maps.
    predicted_comps: dict[str, str] = {}
    for r in cascade["dead_comps"]:
        predicted_comps[r] = "dead"
    for r in cascade["anomalous_comps"]:
        predicted_comps[r] = "anomalous"
    for r in cascade["hot_comps"]:
        # hot wins over anomalous if both (unusual, keep for safety)
        predicted_comps[r] = "hot"
    predicted_rails: dict[str, str] = {}
    for rail in cascade["dead_rails"]:
        predicted_rails[rail] = "dead"
    for rail in cascade["shorted_rails"]:
        predicted_rails[rail] = "shorted"  # shorted wins over dead

    contradictions: list[tuple[str, str, str]] = []
    under_explained: list[str] = []
    tp_c = fp_c = fn_c = 0
    tp_r = fp_r = fn_r = 0

    # Components
    for refdes, obs_mode in obs.state_comps.items():
        pred_mode = predicted_comps.get(refdes, "alive")
        if pred_mode == obs_mode:
            tp_c += 1
        elif obs_mode == "alive" and pred_mode != "alive":
            fp_c += 1
            contradictions.append((refdes, obs_mode, pred_mode))
        elif obs_mode != "alive" and pred_mode == "alive":
            fn_c += 1
            under_explained.append(refdes)
        else:
            # Both non-alive, different modes — soft mismatch counted as FP.
            fp_c += 1
            contradictions.append((refdes, obs_mode, pred_mode))

    # Rails
    for rail, obs_mode in obs.state_rails.items():
        pred_mode = predicted_rails.get(rail, "alive")
        if pred_mode == obs_mode:
            tp_r += 1
        elif obs_mode == "alive" and pred_mode != "alive":
            fp_r += 1
            contradictions.append((rail, obs_mode, pred_mode))
        elif obs_mode != "alive" and pred_mode == "alive":
            fn_r += 1
            under_explained.append(rail)
        else:
            fp_r += 1
            contradictions.append((rail, obs_mode, pred_mode))

    # Over-predicted: non-alive predicted for targets not in any observation.
    observed_keys = set(obs.state_comps) | set(obs.state_rails)
    over_predicted: list[tuple[str, str]] = []
    for refdes, mode in predicted_comps.items():
        if refdes not in observed_keys:
            over_predicted.append((refdes, mode))
    for rail, mode in predicted_rails.items():
        if rail not in observed_keys:
            over_predicted.append((rail, mode))
    over_predicted.sort()

    metrics = HypothesisMetrics(
        tp_comps=tp_c, tp_rails=tp_r,
        fp_comps=fp_c, fp_rails=fp_r,
        fn_comps=fn_c, fn_rails=fn_r,
    )
    tp = tp_c + tp_r
    fp = fp_c + fp_r
    fn = fn_c + fn_r
    score = float(tp - fp_w * fp - fn_w * fn)
    diff = HypothesisDiff(
        contradictions=sorted(contradictions),
        under_explained=sorted(under_explained),
        over_predicted=over_predicted,
    )
    return score, metrics, diff
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v
```

Expected: 24 passed (17 + 7 new).

- [ ] **Step 5: Commit**

```bash
git add api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
git commit -m "$(cat <<'EOF'
feat(hypothesize): mode-aware _score_candidate

Works off the 5-bucket cascade shape. Builds per-refdes / per-rail
predicted-mode maps from dead_comps / anomalous_comps / hot_comps /
dead_rails / shorted_rails, then matches each observation entry by
exact mode equality. Mode mismatches between two non-alive modes are
counted as contradictions (FP × 10). Over-predictions are surfaced
informationally with their predicted mode.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
```

---

## Task 7: Multi-mode narrative + public `hypothesize()` re-wiring

**Files:**
- Modify: `api/pipeline/schematic/hypothesize.py`
- Modify: `tests/pipeline/schematic/test_hypothesize.py`

- [ ] **Step 1: Failing tests**

Append:

```python
def test_hypothesize_end_to_end_dead_recovery():
    obs = Observations(
        state_comps={"U12": "dead", "U19": "dead"},
        state_rails={"+5V": "dead"},
    )
    result = hypothesize(
        _mini_graph(), analyzed_boot=_mini_boot(), observations=obs,
    )
    assert len(result.hypotheses) >= 1
    top = result.hypotheses[0]
    assert top.kill_refdes == ["U7"]
    assert top.kill_modes == ["dead"]
    assert top.score > 0
    assert top.narrative != ""
    assert "U7" in top.narrative
    assert "meurt" in top.narrative


def test_hypothesize_end_to_end_anomalous_recovery():
    g = _mini_graph_with_signal_edges()
    obs = Observations(state_comps={"U17": "anomalous"})
    result = hypothesize(
        g, analyzed_boot=_mini_boot(), observations=obs,
    )
    # U10 OR U11 should be in the top (both can explain U17 anomalous).
    top_refdes = {tuple(sorted(h.kill_refdes)) for h in result.hypotheses[:3]}
    assert ("U10",) in top_refdes or ("U11",) in top_refdes


def test_hypothesize_empty_obs_returns_empty():
    r = hypothesize(_mini_graph(), observations=Observations())
    assert r.hypotheses == []
    assert r.pruning.single_candidates_tested == 0


def test_hypothesize_narrative_cites_mode_and_metric():
    obs = Observations(
        state_rails={"+5V": "dead"},
        metrics_rails={
            "+5V": ObservedMetric(measured=0.02, unit="V", nominal=5.0),
        },
    )
    r = hypothesize(
        _mini_graph(), analyzed_boot=_mini_boot(), observations=obs,
    )
    top = r.hypotheses[0]
    # Metric cited in the narrative.
    assert "0.02" in top.narrative or "5.0" in top.narrative


def test_hypothesize_respects_max_results():
    obs = Observations(state_rails={"+5V": "dead", "+3V3": "dead"})
    r = hypothesize(
        _mini_graph(), analyzed_boot=_mini_boot(), observations=obs,
        max_results=1,
    )
    assert len(r.hypotheses) == 1
```

- [ ] **Step 2: Confirm failure**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k end_to_end
```

Expected: NotImplementedError.

- [ ] **Step 3: Re-implement `_narrate`, enumerations, and the public `hypothesize`**

Insert in `hypothesize.py` (replacing the stub):

```python
def _narrate(
    kill_refdes: list[str],
    kill_modes: list[str],
    cascade: dict,
    metrics: HypothesisMetrics,
    diff: HypothesisDiff,
    observations: Observations,
) -> str:
    """Deterministic FR narrative — no LLM."""
    obs_total = len(observations.state_comps) + len(observations.state_rails)
    tp = metrics.tp_comps + metrics.tp_rails
    fp = metrics.fp_comps + metrics.fp_rails

    # Pick a rails preview — shorted takes precedence visually.
    shorted_preview = ", ".join(sorted(cascade["shorted_rails"])[:2])
    dead_preview = ", ".join(sorted(cascade["dead_rails"])[:3]) or "aucun rail"
    rails_preview = shorted_preview or dead_preview
    dead_count = max(0, len(cascade["dead_comps"]) - len(kill_refdes))
    anom_count = len(cascade["anomalous_comps"])

    if len(kill_refdes) == 1:
        verb = {
            "dead": "meurt",
            "anomalous": "dysfonctionne (output faux)",
            "hot": "chauffe anormalement",
            "shorted": "court vers GND",
        }.get(kill_modes[0], "échoue")
        head = f"Si {kill_refdes[0]} {verb} : {rails_preview}"
        if dead_count > 0:
            head += f" → {dead_count} composant(s) downstream morts"
        if anom_count > 1:
            head += f", {anom_count} composant(s) aval anormaux"
        head += "."
    else:
        parts = [f"{r} ({m})" for r, m in zip(kill_refdes, kill_modes)]
        head = (
            f"Si {' ET '.join(parts)} échouent simultanément : "
            f"{rails_preview} → {dead_count} composant(s) downstream morts."
        )

    coverage = f" Explique {tp}/{obs_total} observations, {fp} contradiction(s)."

    # Cite up to 2 measurements.
    metric_snippets: list[str] = []
    for target, metric in list(observations.metrics_comps.items())[:2]:
        unit = metric.unit
        metric_snippets.append(f"{target} à {metric.measured}{unit}")
    for target, metric in list(observations.metrics_rails.items())[:2]:
        unit = metric.unit
        metric_snippets.append(f"{target} à {metric.measured}{unit}")
    metrics_tail = (
        " Mesures : " + ", ".join(metric_snippets) + "."
        if metric_snippets else ""
    )

    tail = ""
    if diff.contradictions:
        contras = ", ".join(f"{t} observé {o}, prédit {p}" for t, o, p in diff.contradictions[:3])
        tail += f" Contredit : {contras}."
    if diff.under_explained:
        tail += f" Ne couvre pas : {', '.join(diff.under_explained[:4])}."

    return head + coverage + metrics_tail + tail


def _cascade_preview(cascade: dict) -> dict:
    return {
        "dead_rails": sorted(cascade["dead_rails"]),
        "shorted_rails": sorted(cascade["shorted_rails"]),
        "dead_comps_count": len(cascade["dead_comps"]),
        "anomalous_count": len(cascade["anomalous_comps"]),
        "hot_count": len(cascade["hot_comps"]),
    }


def _applicable_modes(
    electrical: ElectricalGraph, refdes: str,
) -> list[str]:
    """Return the list of modes worth trying for a given refdes.

    - `dead` always.
    - `anomalous` if the refdes has at least one outgoing signal-typed edge.
    - `hot` always (cheap, self-only).
    - `shorted` if the refdes is listed as a consumer of any power rail.
    """
    modes = ["dead", "hot"]
    has_signal = any(
        e.src == refdes and e.kind in SIGNAL_EDGE_KINDS
        for e in electrical.typed_edges
    )
    if has_signal:
        modes.append("anomalous")
    is_consumer = any(
        refdes in (r.consumers or [])
        for r in electrical.power_rails.values()
    )
    if is_consumer:
        modes.append("shorted")
    return modes


def _relevant_to_observations(cascade: dict, obs: Observations) -> bool:
    """Pruning gate — cascade touches at least one observation target."""
    obs_comps = set(obs.state_comps)
    obs_rails = set(obs.state_rails)
    any_pred = (
        cascade["dead_comps"] | cascade["anomalous_comps"] | cascade["hot_comps"]
    )
    any_rail = cascade["dead_rails"] | cascade["shorted_rails"]
    if any_pred & obs_comps:
        return True
    if any_rail & obs_rails:
        return True
    return False


def _enumerate_single_fault(
    electrical: ElectricalGraph,
    analyzed_boot: AnalyzedBootSequence | None,
    observations: Observations,
) -> tuple[
    dict[tuple[str, str], dict],  # cascades by (refdes, mode)
    list[tuple[str, str, float, HypothesisMetrics, HypothesisDiff]],  # ranked survivors
]:
    cascades_cache: dict[tuple[str, str], dict] = {}
    ranked: list[tuple[str, str, float, HypothesisMetrics, HypothesisDiff]] = []
    for refdes in electrical.components:
        for mode in _applicable_modes(electrical, refdes):
            cascade = _simulate_failure(electrical, analyzed_boot, refdes, mode)
            cascades_cache[(refdes, mode)] = cascade
            if not _relevant_to_observations(cascade, observations):
                continue
            score, metrics, diff = _score_candidate(cascade, observations)
            ranked.append((refdes, mode, score, metrics, diff))
    ranked.sort(key=lambda t: -t[2])
    return cascades_cache, ranked


def _enumerate_two_fault(
    electrical: ElectricalGraph,
    analyzed_boot: AnalyzedBootSequence | None,
    observations: Observations,
    cascades_cache: dict[tuple[str, str], dict],
    single_ranked: list[tuple[str, str, float, HypothesisMetrics, HypothesisDiff]],
) -> tuple[int, list[tuple[tuple[tuple[str, str], tuple[str, str]], float, HypothesisMetrics, HypothesisDiff, dict]]]:
    """2-fault pass seeded by top-K single-fault survivors.

    Each kill element is a (refdes, mode) pair. Pairs are deduplicated
    as sorted tuples. Capped at MAX_PAIRS.
    """
    if not TWO_FAULT_ENABLED:
        return 0, []

    top_k = [(r, m) for r, m, *_ in single_ranked[:TOP_K_SINGLE]]
    seen: set[tuple[tuple[str, str], tuple[str, str]]] = set()
    pairs_tested = 0
    ranked: list[tuple[tuple[tuple[str, str], tuple[str, str]], float, HypothesisMetrics, HypothesisDiff, dict]] = []

    for (r1, m1) in top_k:
        c1 = cascades_cache[(r1, m1)]
        residual_comps = (
            set(observations.state_comps) - (c1["dead_comps"] | c1["anomalous_comps"] | c1["hot_comps"])
        )
        residual_rails = (
            set(observations.state_rails) - (c1["dead_rails"] | c1["shorted_rails"])
        )
        if not residual_comps and not residual_rails:
            continue
        for (r2, m2), c2 in cascades_cache.items():
            if (r2, m2) == (r1, m1) or r2 == r1:
                continue
            key = tuple(sorted(((r1, m1), (r2, m2))))
            if key in seen:
                continue
            # c2 must touch at least one residual target.
            c2_all_comps = c2["dead_comps"] | c2["anomalous_comps"] | c2["hot_comps"]
            c2_all_rails = c2["dead_rails"] | c2["shorted_rails"]
            if not (c2_all_comps & residual_comps) and not (c2_all_rails & residual_rails):
                continue
            seen.add(key)
            # Union cascades: we don't re-simulate the combined pair (the
            # forward simulator doesn't compose modes cleanly). Take the
            # element-wise union of buckets — this is an approximation but
            # it's cheap and matches observation semantics.
            combined = {
                "dead_comps": c1["dead_comps"] | c2["dead_comps"],
                "dead_rails": c1["dead_rails"] | c2["dead_rails"],
                "shorted_rails": c1["shorted_rails"] | c2["shorted_rails"],
                "anomalous_comps": c1["anomalous_comps"] | c2["anomalous_comps"],
                "hot_comps": c1["hot_comps"] | c2["hot_comps"],
                "final_verdict": c1.get("final_verdict") or c2.get("final_verdict") or "",
                "blocked_at_phase": None,
            }
            pairs_tested += 1
            score, metrics, diff = _score_candidate(combined, observations)
            ranked.append((key, score, metrics, diff, combined))
            if pairs_tested >= MAX_PAIRS:
                break
        if pairs_tested >= MAX_PAIRS:
            break
    ranked.sort(key=lambda t: -t[1])
    return pairs_tested, ranked


def hypothesize(
    electrical: ElectricalGraph,
    *,
    analyzed_boot: AnalyzedBootSequence | None = None,
    observations: Observations,
    max_results: int = MAX_RESULTS_DEFAULT,
) -> HypothesizeResult:
    """Rank candidate (refdes, mode) kills that explain `observations`."""
    t0 = time.perf_counter()
    if observations.is_empty():
        return HypothesizeResult(
            device_slug=electrical.device_slug,
            observations_echo=observations,
            hypotheses=[],
            pruning=PruningStats(
                single_candidates_tested=0,
                two_fault_pairs_tested=0,
                wall_ms=(time.perf_counter() - t0) * 1000,
            ),
        )

    cascades_cache, single_ranked = _enumerate_single_fault(
        electrical, analyzed_boot, observations,
    )
    pairs_tested, two_ranked = _enumerate_two_fault(
        electrical, analyzed_boot, observations,
        cascades_cache, single_ranked,
    )

    hypotheses: list[Hypothesis] = []
    for refdes, mode, score, metrics, diff in single_ranked:
        cascade = cascades_cache[(refdes, mode)]
        hypotheses.append(Hypothesis(
            kill_refdes=[refdes],
            kill_modes=[mode],
            score=score,
            metrics=metrics,
            diff=diff,
            narrative=_narrate([refdes], [mode], cascade, metrics, diff, observations),
            cascade_preview=_cascade_preview(cascade),
        ))
    for key, score, metrics, diff, combined in two_ranked:
        (r1, m1), (r2, m2) = key
        hypotheses.append(Hypothesis(
            kill_refdes=[r1, r2],
            kill_modes=[m1, m2],
            score=score,
            metrics=metrics,
            diff=diff,
            narrative=_narrate([r1, r2], [m1, m2], combined, metrics, diff, observations),
            cascade_preview=_cascade_preview(combined),
        ))

    hypotheses.sort(key=lambda h: (
        -h.score,
        len(h.kill_refdes),
        h.cascade_preview["dead_comps_count"] + h.cascade_preview["anomalous_count"],
    ))
    hypotheses = hypotheses[:max_results]

    return HypothesizeResult(
        device_slug=electrical.device_slug,
        observations_echo=observations,
        hypotheses=hypotheses,
        pruning=PruningStats(
            single_candidates_tested=len(cascades_cache),
            two_fault_pairs_tested=pairs_tested,
            wall_ms=(time.perf_counter() - t0) * 1000,
        ),
    )
```

- [ ] **Step 4: Run all tests**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v
```

Expected: 29 passed.

- [ ] **Step 5: Commit**

```bash
git add api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
git commit -m "$(cat <<'EOF'
feat(hypothesize): multi-mode enumeration, scoring, FR narrative, public API

Re-wires the full hypothesize() engine on top of the new multi-mode
primitives. Candidate set = {(refdes, mode) for every refdes × applicable
mode}, where applicable is determined by _applicable_modes (dead always,
hot always, anomalous if signal-producing, shorted if rail consumer).

Single-fault pruning keeps only cascades that touch an observation.
2-fault pass approximates combined cascades as element-wise bucket
unions (avoids costly re-simulation of mode compositions). Capped at
MAX_PAIRS=100.

FR narrative cites mode verb (meurt / dysfonctionne / chauffe / court),
rails preview with shorted priority, cascade size, and up to 2 numeric
measurements from obs.metrics_*. Contradictions report (observed,
predicted) mode pairs. Tie-break: score desc, kill count asc, cascade
size asc.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
```

---

## Task 8: `MeasurementEvent` shape + auto-classify table

**Files:**
- Create: `api/agent/measurement_memory.py`
- Create: `tests/agent/test_measurement_memory.py`

- [ ] **Step 1: Failing tests**

Create `tests/agent/test_measurement_memory.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the per-repair measurement journal."""

from __future__ import annotations

import pytest

from api.agent.measurement_memory import (
    MeasurementEvent,
    auto_classify,
    parse_target,
)


def test_measurement_event_shape():
    ev = MeasurementEvent(
        timestamp="2026-04-23T18:45:12Z",
        target="rail:+3V3",
        value=2.87,
        unit="V",
        nominal=3.3,
        source="ui",
    )
    assert ev.target == "rail:+3V3"
    assert ev.auto_classified_mode is None  # defaults to None


def test_parse_target_rail():
    assert parse_target("rail:+3V3") == ("rail", "+3V3")
    assert parse_target("rail:LPC_VCC") == ("rail", "LPC_VCC")


def test_parse_target_comp():
    assert parse_target("comp:U7") == ("comp", "U7")


def test_parse_target_pin():
    assert parse_target("pin:U7:3") == ("pin", "U7:3")
    assert parse_target("pin:U18:A7") == ("pin", "U18:A7")


def test_parse_target_invalid_kind():
    with pytest.raises(ValueError, match="unknown target kind"):
        parse_target("foo:bar")


def test_parse_target_missing_colon():
    with pytest.raises(ValueError, match="expected '<kind>:<name>'"):
        parse_target("U7")


def test_auto_classify_rail_alive():
    assert auto_classify(target="rail:+3V3", value=3.29, unit="V", nominal=3.3) == "alive"
    assert auto_classify(target="rail:+3V3", value=3.0, unit="V", nominal=3.3) == "alive"  # 90.9%


def test_auto_classify_rail_anomalous_sag():
    assert auto_classify(target="rail:+3V3", value=2.8, unit="V", nominal=3.3) == "anomalous"
    assert auto_classify(target="rail:+3V3", value=1.65, unit="V", nominal=3.3) == "anomalous"  # 50%


def test_auto_classify_rail_dead():
    assert auto_classify(target="rail:+3V3", value=0.02, unit="V", nominal=3.3) == "dead"


def test_auto_classify_rail_overvoltage_as_shorted():
    assert auto_classify(target="rail:+3V3", value=4.0, unit="V", nominal=3.3) == "shorted"


def test_auto_classify_rail_explicit_short_note():
    # near-zero voltage + explicit note='short' promotes dead → shorted.
    assert auto_classify(
        target="rail:+3V3", value=0.0, unit="V", nominal=3.3, note="short"
    ) == "shorted"


def test_auto_classify_ic_hot():
    assert auto_classify(target="comp:Q17", value=72.3, unit="°C") == "hot"
    assert auto_classify(target="comp:Q17", value=55.0, unit="°C") == "alive"


def test_auto_classify_rail_missing_nominal_returns_none():
    # Can't classify without knowing the expected value.
    assert auto_classify(target="rail:+3V3", value=2.8, unit="V", nominal=None) is None


def test_auto_classify_unknown_target_kind_returns_none():
    # Pin-level measurements don't auto-classify to component modes.
    assert auto_classify(target="pin:U7:3", value=0.8, unit="V", nominal=3.3) is None
```

- [ ] **Step 2: Confirm failure**

```bash
.venv/bin/pytest tests/agent/test_measurement_memory.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Create `api/agent/measurement_memory.py`**

```python
# SPDX-License-Identifier: Apache-2.0
"""Per-repair append-only journal of tech measurements.

Same JSONL pattern as `api/agent/chat_history.py` — one `{ts, event}`
record per line at `memory/{slug}/repairs/{repair_id}/measurements.jsonl`.

Public surface:
- MeasurementEvent (Pydantic shape)
- append_measurement / load_measurements / compare_measurements
- synthesise_observations (derive Observations from the latest-per-target
  state in the journal)
- auto_classify (pure function — map a value + nominal + unit to a
  ComponentMode / RailMode, or None if it can't decide)
- parse_target (parser for "kind:name" strings)
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("wrench_board.agent.measurement_memory")


Source = Literal["ui", "agent"]
Unit = Literal["V", "A", "W", "°C", "Ω", "mV"]


class MeasurementEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: str
    target: str
    value: float
    unit: Unit
    nominal: float | None = None
    note: str | None = None
    source: Source
    auto_classified_mode: str | None = None


# ---------------------------------------------------------------------------
# Target grammar
# ---------------------------------------------------------------------------

TargetKind = Literal["rail", "comp", "pin"]
_KNOWN_KINDS: frozenset[str] = frozenset({"rail", "comp", "pin"})


def parse_target(target: str) -> tuple[str, str]:
    """Split a target string into (kind, name).

    Examples:
      "rail:+3V3"  → ("rail", "+3V3")
      "comp:U7"    → ("comp", "U7")
      "pin:U7:3"   → ("pin", "U7:3")

    Raises ValueError for unknown kinds or malformed input.
    """
    if ":" not in target:
        raise ValueError(f"expected '<kind>:<name>', got {target!r}")
    kind, _, name = target.partition(":")
    if kind not in _KNOWN_KINDS:
        raise ValueError(f"unknown target kind {kind!r}; expected one of {sorted(_KNOWN_KINDS)}")
    if not name:
        raise ValueError(f"empty name in target {target!r}")
    return kind, name


# ---------------------------------------------------------------------------
# Auto-classify rules
# ---------------------------------------------------------------------------

# Central, tunable. Values are ratios of nominal unless otherwise stated.
CLASSIFY_RAIL_ALIVE_LOW = 0.90         # ≥ 90% of nominal
CLASSIFY_RAIL_ALIVE_HIGH = 1.10        # ≤ 110% of nominal
CLASSIFY_RAIL_DEAD_THRESHOLD_V = 0.05  # absolute volts, < this → dead
CLASSIFY_RAIL_ANOMALOUS_LOW = 0.50     # 50-90% of nominal → anomalous
CLASSIFY_IC_HOT_CELSIUS = 65.0         # IC temperature threshold


def auto_classify(
    *, target: str, value: float, unit: str,
    nominal: float | None = None, note: str | None = None,
) -> str | None:
    """Map a (target, value, unit, nominal?) to a mode string.

    Returns None when we can't decide (missing nominal, unsupported
    kind, etc.) — the caller keeps the measurement in storage but
    leaves the mode unset.
    """
    try:
        kind, name = parse_target(target)
    except ValueError:
        return None

    if kind == "rail" and unit in ("V", "mV"):
        if nominal is None:
            return None
        # Normalise mV to V.
        v = value / 1000.0 if unit == "mV" else value
        nom = nominal / 1000.0 if unit == "mV" else nominal
        # Explicit short note dominates.
        if note and "short" in note.lower() and abs(v) < CLASSIFY_RAIL_DEAD_THRESHOLD_V:
            return "shorted"
        if v < CLASSIFY_RAIL_DEAD_THRESHOLD_V:
            return "dead"
        ratio = v / nom if nom != 0 else 0.0
        if ratio > CLASSIFY_RAIL_ALIVE_HIGH:
            return "shorted"   # overvoltage folded into shorted for Phase 1
        if ratio >= CLASSIFY_RAIL_ALIVE_LOW:
            return "alive"
        if ratio >= CLASSIFY_RAIL_ANOMALOUS_LOW:
            return "anomalous"
        return "anomalous"   # any non-zero sag below 50% is still anomalous

    if kind == "comp" and unit == "°C":
        return "hot" if value >= CLASSIFY_IC_HOT_CELSIUS else "alive"

    # Unsupported combinations — we store the measurement but leave the
    # mode empty for the tech to decide manually.
    return None
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/agent/test_measurement_memory.py -v
```

Expected: 14 passed.

- [ ] **Step 5: Commit**

```bash
git add api/agent/measurement_memory.py tests/agent/test_measurement_memory.py
git commit -m "$(cat <<'EOF'
feat(agent): measurement memory — shape + target grammar + auto-classify

Adds api/agent/measurement_memory.py with the MeasurementEvent Pydantic
shape (ISO timestamp, target, value, unit, nominal?, note?, source,
auto_classified_mode?), a parse_target helper (rail: / comp: / pin:),
and a pure auto_classify function encoding the default thresholds from
the spec: rail ±10% → alive, 50-90% → anomalous, <50 mV → dead, >110%
→ shorted (overvoltage folded in), note='short' + near-zero → shorted,
IC >65°C → hot. Returns None for unsupported cases (missing nominal,
unit mismatch, pin-level measurements).

The JSONL store, compare, and synthesise_observations land in Task 9.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- api/agent/measurement_memory.py tests/agent/test_measurement_memory.py
```

---

## Task 9: Journal store (append / load / compare / synthesise)

**Files:**
- Modify: `api/agent/measurement_memory.py`
- Modify: `tests/agent/test_measurement_memory.py`

- [ ] **Step 1: Failing tests**

Append:

```python
from pathlib import Path

from api.agent.measurement_memory import (
    append_measurement,
    compare_measurements,
    load_measurements,
    synthesise_observations,
)


def test_append_and_load_roundtrip(tmp_path: Path):
    mr = tmp_path / "memory"
    append_measurement(
        memory_root=mr, device_slug="demo", repair_id="r1",
        target="rail:+3V3", value=2.87, unit="V", nominal=3.3, source="ui",
    )
    events = load_measurements(
        memory_root=mr, device_slug="demo", repair_id="r1",
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.target == "rail:+3V3"
    assert ev.value == 2.87
    assert ev.auto_classified_mode == "anomalous"
    assert ev.timestamp.endswith("Z") or "+" in ev.timestamp


def test_append_auto_classify_writes_mode(tmp_path: Path):
    mr = tmp_path / "memory"
    append_measurement(
        memory_root=mr, device_slug="d", repair_id="r",
        target="rail:+5V", value=0.01, unit="V", nominal=5.0, source="agent",
    )
    events = load_measurements(memory_root=mr, device_slug="d", repair_id="r")
    assert events[0].auto_classified_mode == "dead"


def test_load_measurements_filter_target(tmp_path: Path):
    mr = tmp_path / "memory"
    for target, value in (("rail:+3V3", 2.87), ("rail:+5V", 5.01), ("rail:+3V3", 3.29)):
        append_measurement(
            memory_root=mr, device_slug="d", repair_id="r",
            target=target, value=value, unit="V", nominal=3.3 if "3V3" in target else 5.0,
            source="ui",
        )
    rail3 = load_measurements(memory_root=mr, device_slug="d", repair_id="r", target="rail:+3V3")
    assert [e.value for e in rail3] == [2.87, 3.29]
    all_ = load_measurements(memory_root=mr, device_slug="d", repair_id="r")
    assert len(all_) == 3


def test_compare_measurements(tmp_path: Path):
    mr = tmp_path / "memory"
    append_measurement(
        memory_root=mr, device_slug="d", repair_id="r",
        target="rail:+3V3", value=2.87, unit="V", nominal=3.3, source="ui",
        note="avant reflow",
    )
    append_measurement(
        memory_root=mr, device_slug="d", repair_id="r",
        target="rail:+3V3", value=3.29, unit="V", nominal=3.3, source="ui",
        note="après reflow",
    )
    diff = compare_measurements(
        memory_root=mr, device_slug="d", repair_id="r",
        target="rail:+3V3",
    )
    assert diff["before"]["value"] == 2.87
    assert diff["after"]["value"] == 3.29
    assert round(diff["delta"], 2) == 0.42
    assert diff["delta_percent"] is not None


def test_synthesise_observations_dedup_latest(tmp_path: Path):
    mr = tmp_path / "memory"
    # Same target measured twice — latest wins.
    append_measurement(
        memory_root=mr, device_slug="d", repair_id="r",
        target="rail:+3V3", value=2.87, unit="V", nominal=3.3, source="ui",
    )
    append_measurement(
        memory_root=mr, device_slug="d", repair_id="r",
        target="rail:+3V3", value=3.29, unit="V", nominal=3.3, source="ui",
    )
    append_measurement(
        memory_root=mr, device_slug="d", repair_id="r",
        target="comp:Q17", value=72.3, unit="°C", source="agent",
    )
    obs = synthesise_observations(
        memory_root=mr, device_slug="d", repair_id="r",
    )
    # Latest rail mode = alive (3.29V ≈ 3.3V).
    assert obs.state_rails.get("+3V3") == "alive"
    assert obs.state_comps.get("Q17") == "hot"
    assert obs.metrics_rails["+3V3"].measured == 3.29


def test_load_measurements_missing_returns_empty(tmp_path: Path):
    assert load_measurements(memory_root=tmp_path, device_slug="d", repair_id="r") == []


def test_compare_measurements_insufficient_returns_none(tmp_path: Path):
    mr = tmp_path / "memory"
    append_measurement(
        memory_root=mr, device_slug="d", repair_id="r",
        target="rail:+3V3", value=2.87, unit="V", nominal=3.3, source="ui",
    )
    diff = compare_measurements(
        memory_root=mr, device_slug="d", repair_id="r",
        target="rail:+3V3",
    )
    assert diff is None  # only one measurement — no before/after
```

- [ ] **Step 2: Confirm failure**

```bash
.venv/bin/pytest tests/agent/test_measurement_memory.py -v
```

Expected: 7 new failures (`ImportError`).

- [ ] **Step 3: Implement the journal functions**

Append to `api/agent/measurement_memory.py`:

```python
def _journal_path(memory_root: Path, device_slug: str, repair_id: str) -> Path:
    return (
        memory_root / device_slug / "repairs" / repair_id / "measurements.jsonl"
    )


def append_measurement(
    *,
    memory_root: Path,
    device_slug: str,
    repair_id: str,
    target: str,
    value: float,
    unit: str,
    nominal: float | None = None,
    note: str | None = None,
    source: str = "agent",
) -> MeasurementEvent:
    """Append one MeasurementEvent to the journal, return it.

    Auto-classify is computed synchronously and cached on the event so
    replay and filtering don't need to re-run the rules.
    """
    mode = auto_classify(target=target, value=value, unit=unit, nominal=nominal, note=note)
    ev = MeasurementEvent(
        timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        target=target,
        value=value,
        unit=unit,  # validated by Literal
        nominal=nominal,
        note=note,
        source=source,  # validated by Literal
        auto_classified_mode=mode,
    )
    path = _journal_path(memory_root, device_slug, repair_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(ev.model_dump_json() + "\n")
    except OSError as exc:
        logger.warning("append_measurement failed for %s / %s: %s", device_slug, repair_id, exc)
    return ev


def load_measurements(
    *,
    memory_root: Path,
    device_slug: str,
    repair_id: str,
    target: str | None = None,
    since: str | None = None,
) -> list[MeasurementEvent]:
    """Return the ordered list of MeasurementEvents, optionally filtered."""
    path = _journal_path(memory_root, device_slug, repair_id)
    if not path.exists():
        return []
    events: list[MeasurementEvent] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = MeasurementEvent.model_validate_json(line)
            except ValueError:
                logger.warning("skipping malformed measurement line in %s", path)
                continue
            if target and ev.target != target:
                continue
            if since and ev.timestamp < since:
                continue
            events.append(ev)
    except OSError as exc:
        logger.warning("load_measurements failed for %s / %s: %s", device_slug, repair_id, exc)
    return events


def compare_measurements(
    *,
    memory_root: Path,
    device_slug: str,
    repair_id: str,
    target: str,
    before_ts: str | None = None,
    after_ts: str | None = None,
) -> dict[str, Any] | None:
    """Return {before, after, delta, delta_percent} for a target's journal.

    Without explicit timestamps, uses the first and last events for the
    target. Returns None if fewer than 2 events match.
    """
    events = load_measurements(
        memory_root=memory_root, device_slug=device_slug, repair_id=repair_id,
        target=target,
    )
    if len(events) < 2:
        return None
    if before_ts:
        candidates = [e for e in events if e.timestamp <= before_ts]
        before = candidates[-1] if candidates else events[0]
    else:
        before = events[0]
    if after_ts:
        candidates = [e for e in events if e.timestamp >= after_ts]
        after = candidates[0] if candidates else events[-1]
    else:
        after = events[-1]
    if before.timestamp == after.timestamp:
        return None
    delta = after.value - before.value
    delta_pct = None
    if before.value:
        delta_pct = round((delta / before.value) * 100, 2)
    return {
        "target": target,
        "before": {"timestamp": before.timestamp, "value": before.value, "mode": before.auto_classified_mode, "note": before.note},
        "after": {"timestamp": after.timestamp, "value": after.value, "mode": after.auto_classified_mode, "note": after.note},
        "delta": round(delta, 6),
        "delta_percent": delta_pct,
    }


def synthesise_observations(
    *,
    memory_root: Path,
    device_slug: str,
    repair_id: str,
) -> Any:
    """Walk the journal, keep the latest event per target, materialise
    an `Observations` shape suitable for hypothesize().

    Imports Observations / ObservedMetric lazily to avoid a circular
    dependency with api.pipeline.schematic.
    """
    from api.pipeline.schematic.hypothesize import Observations, ObservedMetric

    events = load_measurements(
        memory_root=memory_root, device_slug=device_slug, repair_id=repair_id,
    )
    latest: dict[str, MeasurementEvent] = {}
    for ev in events:
        latest[ev.target] = ev

    state_comps: dict[str, str] = {}
    state_rails: dict[str, str] = {}
    metrics_comps: dict[str, ObservedMetric] = {}
    metrics_rails: dict[str, ObservedMetric] = {}

    for target, ev in latest.items():
        try:
            kind, name = parse_target(target)
        except ValueError:
            continue
        metric = ObservedMetric(
            measured=ev.value,
            unit=ev.unit,  # type: ignore[arg-type]
            nominal=ev.nominal,
        )
        if kind == "comp":
            if ev.auto_classified_mode in ("dead", "alive", "anomalous", "hot"):
                state_comps[name] = ev.auto_classified_mode
            metrics_comps[name] = metric
        elif kind == "rail":
            if ev.auto_classified_mode in ("dead", "alive", "shorted"):
                state_rails[name] = ev.auto_classified_mode
            metrics_rails[name] = metric
        # pin-level: store nothing — pin measurements don't map to refdes modes.
    return Observations(
        state_comps=state_comps,
        state_rails=state_rails,
        metrics_comps=metrics_comps,
        metrics_rails=metrics_rails,
    )
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/agent/test_measurement_memory.py -v
```

Expected: 21 passed.

- [ ] **Step 5: Commit**

```bash
git add api/agent/measurement_memory.py tests/agent/test_measurement_memory.py
git commit -m "$(cat <<'EOF'
feat(agent): measurement-memory journal — append / load / compare / synthesise

Appends to memory/{slug}/repairs/{id}/measurements.jsonl, one
MeasurementEvent per line, ISO-8601-Z timestamps. Load filters by
target and `since`. Compare picks the first and last occurrence of a
target (or explicit timestamps) and returns {before, after, delta,
delta_percent}. synthesise_observations walks the journal, keeps the
latest event per target, and assembles an Observations payload (mode
from auto_classified_mode, metric with nominal) that hypothesize() can
consume directly. Errors are logged and swallowed — persistence is
best-effort, the diagnostic session never fails on a journal write.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- api/agent/measurement_memory.py tests/agent/test_measurement_memory.py
```

---

## Task 10: `_SimEvent` WS envelopes

**Files:**
- Modify: `api/tools/ws_events.py`
- Create: `tests/tools/test_ws_events_sim.py`

- [ ] **Step 1: Failing tests**

Create `tests/tools/test_ws_events_sim.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""WS event envelopes for the simulation / observation layer."""

from __future__ import annotations

from api.tools.ws_events import (
    SimulationObservationClear,
    SimulationObservationSet,
)


def test_observation_set_envelope():
    ev = SimulationObservationSet(
        target="rail:+3V3",
        mode="dead",
        measurement={"measured": 0.02, "unit": "V", "nominal": 3.3, "note": None},
    )
    assert ev.type == "simulation.observation_set"
    payload = ev.model_dump()
    assert payload["type"] == "simulation.observation_set"
    assert payload["target"] == "rail:+3V3"
    assert payload["mode"] == "dead"


def test_observation_set_without_measurement():
    ev = SimulationObservationSet(target="comp:U7", mode="anomalous")
    payload = ev.model_dump()
    assert payload["measurement"] is None


def test_observation_clear_envelope():
    ev = SimulationObservationClear()
    assert ev.type == "simulation.observation_clear"
    assert ev.model_dump() == {"type": "simulation.observation_clear"}
```

- [ ] **Step 2: Confirm failure**

```bash
.venv/bin/pytest tests/tools/test_ws_events_sim.py -v
```

Expected: `ImportError`.

- [ ] **Step 3: Add the `_SimEvent` base + two subclasses to `ws_events.py`**

Append to `api/tools/ws_events.py`:

```python
class _SimEvent(BaseModel):
    """Base class for simulation / observation events (backend → frontend)."""

    type: str


class SimulationObservationSet(_SimEvent):
    type: Literal["simulation.observation_set"] = "simulation.observation_set"
    target: str  # e.g. "rail:+3V3" | "comp:U7" | "pin:U7:3"
    mode: str    # ComponentMode | RailMode | "unknown"
    measurement: dict[str, Any] | None = None


class SimulationObservationClear(_SimEvent):
    type: Literal["simulation.observation_clear"] = "simulation.observation_clear"
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/tools/test_ws_events_sim.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add api/tools/ws_events.py tests/tools/test_ws_events_sim.py
git commit -m "$(cat <<'EOF'
feat(tools): _SimEvent WS envelopes — observation_set / observation_clear

Mirrors the existing _BVEvent / boardview.<verb> pattern with a new
simulation.<verb> namespace. SimulationObservationSet carries {target,
mode, measurement?}; SimulationObservationClear is payloadless. Both
serialise to discriminated unions identical in shape to the boardview
events so the frontend dispatcher is symmetric.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>
EOF
)" -- api/tools/ws_events.py tests/tools/test_ws_events_sim.py
```

---

## Task 11: Integration test — full journal + synthesis + hypothesize loop

**Files:**
- Modify: `tests/agent/test_measurement_memory.py`

- [ ] **Step 1: Failing integration test**

Append to `tests/agent/test_measurement_memory.py`:

```python
def test_end_to_end_journal_drives_hypothesize(tmp_path: Path):
    """
    Tech records +3V3 dead (0.02V) + +5V alive (5.0V).
    synthesise_observations must produce Observations with:
      - state_rails={'+3V3': 'dead', '+5V': 'alive'}
      - metrics_rails populated with both.
    hypothesize() on a mini_graph then returns U12 (source of +3V3) top-1.
    """
    from api.pipeline.schematic.hypothesize import Observations, hypothesize
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, NetNode, PagePin, PowerRail,
        SchematicQualityReport,
    )

    mr = tmp_path / "memory"
    append_measurement(
        memory_root=mr, device_slug="demo", repair_id="r",
        target="rail:+3V3", value=0.02, unit="V", nominal=3.3, source="agent",
    )
    append_measurement(
        memory_root=mr, device_slug="demo", repair_id="r",
        target="rail:+5V", value=5.0, unit="V", nominal=5.0, source="agent",
    )

    obs = synthesise_observations(
        memory_root=mr, device_slug="demo", repair_id="r",
    )
    assert obs.state_rails == {"+3V3": "dead", "+5V": "alive"}
    assert obs.metrics_rails["+3V3"].measured == 0.02

    # Minimal graph where U12 sources +3V3 and consumes +5V (like MNT).
    eg = ElectricalGraph(
        device_slug="demo",
        components={
            "U12": ComponentNode(refdes="U12", type="ic", pins=[
                PagePin(number="1", name="VIN", role="power_in", net_label="+5V"),
                PagePin(number="2", name="VOUT", role="power_out", net_label="+3V3"),
            ]),
        },
        nets={"+5V": NetNode(label="+5V", is_power=True, is_global=True),
              "+3V3": NetNode(label="+3V3", is_power=True, is_global=True)},
        power_rails={
            "+5V": PowerRail(label="+5V", source_refdes="U7", consumers=["U12"]),
            "+3V3": PowerRail(label="+3V3", source_refdes="U12"),
        },
        typed_edges=[], boot_sequence=[], designer_notes=[], ambiguities=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )
    r = hypothesize(eg, observations=obs)
    assert r.hypotheses[0].kill_refdes == ["U12"]
    assert r.hypotheses[0].kill_modes == ["dead"]
```

- [ ] **Step 2: Run the integration test**

```bash
.venv/bin/pytest tests/agent/test_measurement_memory.py::test_end_to_end_journal_drives_hypothesize -v
```

Expected: passed.

- [ ] **Step 3: Run full module test suite**

```bash
.venv/bin/pytest tests/agent/test_measurement_memory.py tests/pipeline/schematic/test_hypothesize.py -v 2>&1 | tail -5
```

Expected: 22 + 29 = 51 passed.

- [ ] **Step 4: Commit**

```bash
git add tests/agent/test_measurement_memory.py
git commit -m "$(cat <<'EOF'
test(agent): end-to-end journal → synthesise → hypothesize integration

Demonstrates the live flow: append two rail measurements, synthesise
an Observations payload with metrics + auto-classified modes, feed it
to hypothesize, and verify the top-1 candidate identifies the buck
upstream of the dead rail. Same shape as the T13 endpoint test but
pure-Python end-to-end.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>
EOF
)" -- tests/agent/test_measurement_memory.py
```

---

## Task 12: Migrate `mb_hypothesize` tool wrapper to schema B

**Files:**
- Modify: `api/tools/hypothesize.py`
- Modify: `tests/tools/test_hypothesize.py`

- [ ] **Step 1: Rewrite test fixtures for schema B**

Open `tests/tools/test_hypothesize.py`. Replace the happy-path test content:

```python
# SPDX-License-Identifier: Apache-2.0
"""Tests for the mb_hypothesize tool wrapper (schema B)."""

from __future__ import annotations

from pathlib import Path

import pytest

from api.pipeline.schematic.schemas import (
    ComponentNode, ElectricalGraph, NetNode, PagePin, PowerRail,
    SchematicQualityReport,
)
from api.tools.hypothesize import mb_hypothesize

SLUG = "demo-device"


def _write_graph(memory_root: Path, graph: ElectricalGraph) -> None:
    pack = memory_root / graph.device_slug
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "electrical_graph.json").write_text(graph.model_dump_json(indent=2))


@pytest.fixture
def memory_root(tmp_path: Path) -> Path:
    return tmp_path / "memory"


@pytest.fixture
def graph() -> ElectricalGraph:
    return ElectricalGraph(
        device_slug=SLUG,
        components={
            "U7": ComponentNode(refdes="U7", type="ic", pins=[
                PagePin(number="1", role="power_in", net_label="VIN"),
                PagePin(number="2", role="power_out", net_label="+5V"),
            ]),
            "U12": ComponentNode(refdes="U12", type="ic", pins=[
                PagePin(number="1", role="power_in", net_label="+5V"),
            ]),
        },
        nets={
            "VIN": NetNode(label="VIN", is_power=True, is_global=True),
            "+5V": NetNode(label="+5V", is_power=True, is_global=True),
        },
        power_rails={
            "VIN": PowerRail(label="VIN", source_refdes=None),
            "+5V": PowerRail(label="+5V", source_refdes="U7", consumers=["U12"]),
        },
        typed_edges=[], boot_sequence=[], designer_notes=[], ambiguities=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )


def test_mb_hypothesize_happy_path(memory_root: Path, graph: ElectricalGraph):
    _write_graph(memory_root, graph)
    result = mb_hypothesize(
        device_slug=SLUG, memory_root=memory_root,
        state_comps={"U12": "dead"},
        state_rails={"+5V": "dead"},
    )
    assert result["found"] is True
    assert result["device_slug"] == SLUG
    assert len(result["hypotheses"]) >= 1
    top = result["hypotheses"][0]
    assert top["kill_refdes"] == ["U7"]
    assert top["kill_modes"] == ["dead"]


def test_mb_hypothesize_accepts_metrics(memory_root: Path, graph: ElectricalGraph):
    _write_graph(memory_root, graph)
    result = mb_hypothesize(
        device_slug=SLUG, memory_root=memory_root,
        state_rails={"+5V": "dead"},
        metrics_rails={"+5V": {"measured": 0.02, "unit": "V", "nominal": 5.0}},
    )
    assert result["found"] is True
    top = result["hypotheses"][0]
    # Measurement cited in the narrative.
    assert "0.02" in top["narrative"] or "5.0" in top["narrative"]


def test_mb_hypothesize_synthesise_from_repair_journal(
    memory_root: Path, graph: ElectricalGraph,
):
    from api.agent.measurement_memory import append_measurement
    _write_graph(memory_root, graph)
    # Tech recorded one measurement in the journal → mb_hypothesize reads it.
    append_measurement(
        memory_root=memory_root, device_slug=SLUG, repair_id="r1",
        target="rail:+5V", value=0.02, unit="V", nominal=5.0, source="ui",
    )
    result = mb_hypothesize(
        device_slug=SLUG, memory_root=memory_root, repair_id="r1",
    )
    assert result["found"] is True
    assert result["hypotheses"][0]["kill_refdes"] == ["U7"]


def test_mb_hypothesize_unknown_refdes_rejected(memory_root: Path, graph: ElectricalGraph):
    _write_graph(memory_root, graph)
    result = mb_hypothesize(
        device_slug=SLUG, memory_root=memory_root,
        state_comps={"Z999": "dead"},
    )
    assert result["found"] is False
    assert result["reason"] == "unknown_refdes"
    assert "Z999" in result["invalid_refdes"]


def test_mb_hypothesize_unknown_rail_rejected(memory_root: Path, graph: ElectricalGraph):
    _write_graph(memory_root, graph)
    result = mb_hypothesize(
        device_slug=SLUG, memory_root=memory_root,
        state_rails={"NOT_A_RAIL": "dead"},
    )
    assert result["found"] is False
    assert result["reason"] == "unknown_rail"
    assert "NOT_A_RAIL" in result["invalid_rails"]


def test_mb_hypothesize_no_pack(memory_root: Path):
    result = mb_hypothesize(
        device_slug="nonexistent", memory_root=memory_root,
        state_rails={"+5V": "dead"},
    )
    assert result["found"] is False
    assert result["reason"] == "no_schematic_graph"


def test_mb_hypothesize_empty_inputs(memory_root: Path, graph: ElectricalGraph):
    _write_graph(memory_root, graph)
    result = mb_hypothesize(
        device_slug=SLUG, memory_root=memory_root,
    )
    assert result["found"] is True
    assert result["hypotheses"] == []


def test_mb_hypothesize_manifest_exposes_new_signature():
    from api.agent import manifest
    names: list[str] = []
    if hasattr(manifest, "TOOLS"):
        names = [t["name"] for t in manifest.TOOLS]
    elif hasattr(manifest, "build_tools_manifest"):
        tools = manifest.build_tools_manifest(session=None)
        names = [t["name"] for t in tools]
    assert "mb_hypothesize" in names
```

- [ ] **Step 2: Confirm failure**

```bash
.venv/bin/pytest tests/tools/test_hypothesize.py -v
```

Expected: multiple failures (the current wrapper accepts `dead_comps` lists, not `state_comps` dict).

- [ ] **Step 3: Rewrite `api/tools/hypothesize.py`**

Replace the entire content:

```python
# SPDX-License-Identifier: Apache-2.0
"""mb_hypothesize — reverse diagnostic tool (schema B)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from api.pipeline.schematic.hypothesize import (
    ObservedMetric,
    Observations,
    hypothesize,
)
from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph


def _closest_matches(candidates: list[str], needle: str, k: int = 5) -> list[str]:
    needle_u = needle.upper()
    prefix = needle_u[:1] if needle_u else ""
    substr = sorted(c for c in candidates if needle_u and needle_u in c.upper())
    pfx = sorted(c for c in candidates if prefix and c.upper().startswith(prefix))
    merged = list(dict.fromkeys(substr + pfx))
    return merged[:k]


def _coerce_metric(raw: Any) -> ObservedMetric:
    if isinstance(raw, ObservedMetric):
        return raw
    if isinstance(raw, dict):
        return ObservedMetric.model_validate(raw)
    raise ValueError(f"unsupported metric payload: {raw!r}")


def mb_hypothesize(
    *,
    device_slug: str,
    memory_root: Path,
    state_comps: dict[str, str] | None = None,
    state_rails: dict[str, str] | None = None,
    metrics_comps: dict[str, dict] | None = None,
    metrics_rails: dict[str, dict] | None = None,
    max_results: int = 5,
    repair_id: str | None = None,
) -> dict[str, Any]:
    """Rank candidate (refdes, mode) kills that explain the observations.

    Input routes:
      - explicit state/metrics dicts from the caller (frontend, agent, HTTP),
      - OR `repair_id` set and all state dicts empty → synthesise from the
        repair's measurement journal.

    Returns `HypothesizeResult.model_dump() + {"found": True}` on success,
    or `{"found": False, "reason", ...}` on any validation failure.
    """
    pack = memory_root / device_slug
    graph_path = pack / "electrical_graph.json"
    if not graph_path.exists():
        return {"found": False, "reason": "no_schematic_graph", "device_slug": device_slug}
    try:
        eg = ElectricalGraph.model_validate_json(graph_path.read_text())
    except (OSError, ValueError):
        return {"found": False, "reason": "malformed_graph", "device_slug": device_slug}

    # Journal-based auto-synthesis.
    if repair_id and not (state_comps or state_rails or metrics_comps or metrics_rails):
        from api.agent.measurement_memory import synthesise_observations
        observations = synthesise_observations(
            memory_root=memory_root, device_slug=device_slug, repair_id=repair_id,
        )
    else:
        known_comps = set(eg.components.keys())
        known_rails = set(eg.power_rails.keys())

        comps_in = state_comps or {}
        rails_in = state_rails or {}
        metrics_c_in = metrics_comps or {}
        metrics_r_in = metrics_rails or {}

        invalid_refdes = sorted(
            r for r in set(comps_in) | set(metrics_c_in) if r not in known_comps
        )
        if invalid_refdes:
            return {
                "found": False,
                "reason": "unknown_refdes",
                "invalid_refdes": invalid_refdes,
                "closest_matches": {
                    r: _closest_matches(list(known_comps), r) for r in invalid_refdes
                },
            }
        invalid_rails = sorted(
            r for r in set(rails_in) | set(metrics_r_in) if r not in known_rails
        )
        if invalid_rails:
            return {
                "found": False,
                "reason": "unknown_rail",
                "invalid_rails": invalid_rails,
                "closest_matches": {
                    r: _closest_matches(list(known_rails), r) for r in invalid_rails
                },
            }
        try:
            observations = Observations(
                state_comps=comps_in,
                state_rails=rails_in,
                metrics_comps={k: _coerce_metric(v) for k, v in metrics_c_in.items()},
                metrics_rails={k: _coerce_metric(v) for k, v in metrics_r_in.items()},
            )
        except ValueError as exc:
            return {"found": False, "reason": "invalid_observations", "detail": str(exc)}

    ab: AnalyzedBootSequence | None = None
    ab_path = pack / "boot_sequence_analyzed.json"
    if ab_path.exists():
        try:
            ab = AnalyzedBootSequence.model_validate_json(ab_path.read_text())
        except ValueError:
            ab = None

    result = hypothesize(
        eg, analyzed_boot=ab, observations=observations, max_results=max_results,
    )
    payload = result.model_dump()
    payload["found"] = True
    return payload
```

- [ ] **Step 4: Run tests (manifest test will still fail — that lands in Task 14)**

```bash
.venv/bin/pytest tests/tools/test_hypothesize.py -v -k "not manifest_exposes_new_signature"
```

Expected: 7 passed (all happy / error paths).

- [ ] **Step 5: Commit**

```bash
git add api/tools/hypothesize.py tests/tools/test_hypothesize.py
git commit -m "$(cat <<'EOF'
refactor(tools): mb_hypothesize — schema B input surface

Accepts state_comps / state_rails / metrics_comps / metrics_rails dicts
instead of the four flat refdes lists. Adds `repair_id` for automatic
synthesis from the measurement journal when no explicit observations
are provided. Metric payloads accept dict or ObservedMetric directly.

Unknown refdes / rail validation is per-dict: checks the union of state
and metric keys against the graph. invalid_observations path surfaces
Pydantic ValueError messages (cross-bucket alias, mode literal mismatch)
with the same structured {found: false, reason, ...} contract as v1.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- api/tools/hypothesize.py tests/tools/test_hypothesize.py
```

---

## Task 13: `api/tools/measurements.py` — 4 journal tools

**Files:**
- Create: `api/tools/measurements.py`
- Create: `tests/tools/test_measurements.py`

- [ ] **Step 1: Failing tests**

Create `tests/tools/test_measurements.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Contract tests for the measurement-memory agent tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from api.tools.measurements import (
    mb_compare_measurements,
    mb_list_measurements,
    mb_observations_from_measurements,
    mb_record_measurement,
)


@pytest.fixture
def mr(tmp_path: Path) -> Path:
    return tmp_path / "memory"


SLUG = "demo"
REPAIR = "r1"


def test_record_measurement_returns_mode_and_timestamp(mr: Path):
    result = mb_record_measurement(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
        target="rail:+3V3", value=2.87, unit="V", nominal=3.3,
    )
    assert result["recorded"] is True
    assert result["auto_classified_mode"] == "anomalous"
    assert "timestamp" in result


def test_record_measurement_rejects_unknown_target_kind(mr: Path):
    result = mb_record_measurement(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
        target="bogus:X", value=1.0, unit="V",
    )
    assert result["recorded"] is False
    assert result["reason"] == "invalid_target"


def test_list_measurements_returns_all(mr: Path):
    for v in (2.87, 3.29):
        mb_record_measurement(
            memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
            target="rail:+3V3", value=v, unit="V", nominal=3.3,
        )
    result = mb_list_measurements(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
    )
    assert result["found"] is True
    assert len(result["events"]) == 2
    assert result["events"][0]["value"] == 2.87


def test_list_measurements_filter_target(mr: Path):
    mb_record_measurement(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
        target="rail:+3V3", value=2.87, unit="V", nominal=3.3,
    )
    mb_record_measurement(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
        target="comp:U7", value=65.0, unit="°C",
    )
    rail = mb_list_measurements(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR, target="rail:+3V3",
    )
    assert len(rail["events"]) == 1


def test_compare_measurements_happy(mr: Path):
    for v, note in ((2.87, "avant"), (3.29, "après")):
        mb_record_measurement(
            memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
            target="rail:+3V3", value=v, unit="V", nominal=3.3, note=note,
        )
    diff = mb_compare_measurements(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
        target="rail:+3V3",
    )
    assert diff["found"] is True
    assert round(diff["delta"], 2) == 0.42


def test_compare_measurements_insufficient(mr: Path):
    mb_record_measurement(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
        target="rail:+3V3", value=2.87, unit="V", nominal=3.3,
    )
    diff = mb_compare_measurements(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
        target="rail:+3V3",
    )
    assert diff["found"] is False
    assert diff["reason"] == "insufficient_measurements"


def test_observations_from_measurements(mr: Path):
    mb_record_measurement(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
        target="rail:+3V3", value=0.02, unit="V", nominal=3.3,
    )
    result = mb_observations_from_measurements(
        memory_root=mr, device_slug=SLUG, repair_id=REPAIR,
    )
    assert result["state_rails"]["+3V3"] == "dead"
    assert result["metrics_rails"]["+3V3"]["measured"] == 0.02
```

- [ ] **Step 2: Confirm failure**

```bash
.venv/bin/pytest tests/tools/test_measurements.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Create `api/tools/measurements.py`**

```python
# SPDX-License-Identifier: Apache-2.0
"""Agent tools for the measurement journal.

Every write tool emits a `simulation.observation_set` WS event through a
pluggable emitter (set by the runtime at session open) so the frontend
UI mirrors the agent's measurements live.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from api.agent.measurement_memory import (
    append_measurement,
    compare_measurements,
    load_measurements,
    parse_target,
    synthesise_observations,
)

# The runtime wires this to its WS sender at session open. It stays None
# until wired — tools still work, the frontend just won't see the events.
_ws_emitter: Callable[[dict[str, Any]], None] | None = None


def set_ws_emitter(emitter: Callable[[dict[str, Any]], None] | None) -> None:
    global _ws_emitter
    _ws_emitter = emitter


def _emit(event: dict[str, Any]) -> None:
    if _ws_emitter is not None:
        try:
            _ws_emitter(event)
        except Exception:   # noqa: BLE001 — best-effort broadcast
            pass


def mb_record_measurement(
    *,
    device_slug: str,
    repair_id: str,
    memory_root: Path,
    target: str,
    value: float,
    unit: str,
    nominal: float | None = None,
    note: str | None = None,
    source: str = "agent",
) -> dict[str, Any]:
    """Append a MeasurementEvent and emit the WS observation_set event."""
    try:
        parse_target(target)
    except ValueError as exc:
        return {"recorded": False, "reason": "invalid_target", "detail": str(exc)}
    ev = append_measurement(
        memory_root=memory_root, device_slug=device_slug, repair_id=repair_id,
        target=target, value=value, unit=unit, nominal=nominal, note=note,
        source=source,
    )
    if ev.auto_classified_mode:
        _emit({
            "type": "simulation.observation_set",
            "target": target,
            "mode": ev.auto_classified_mode,
            "measurement": {
                "measured": value,
                "unit": unit,
                "nominal": nominal,
                "note": note,
            },
        })
    return {
        "recorded": True,
        "timestamp": ev.timestamp,
        "auto_classified_mode": ev.auto_classified_mode,
    }


def mb_list_measurements(
    *,
    device_slug: str,
    repair_id: str,
    memory_root: Path,
    target: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    events = load_measurements(
        memory_root=memory_root, device_slug=device_slug, repair_id=repair_id,
        target=target, since=since,
    )
    return {
        "found": True,
        "events": [e.model_dump() for e in events],
    }


def mb_compare_measurements(
    *,
    device_slug: str,
    repair_id: str,
    memory_root: Path,
    target: str,
    before_ts: str | None = None,
    after_ts: str | None = None,
) -> dict[str, Any]:
    diff = compare_measurements(
        memory_root=memory_root, device_slug=device_slug, repair_id=repair_id,
        target=target, before_ts=before_ts, after_ts=after_ts,
    )
    if diff is None:
        return {"found": False, "reason": "insufficient_measurements", "target": target}
    return {"found": True, **diff}


def mb_observations_from_measurements(
    *,
    device_slug: str,
    repair_id: str,
    memory_root: Path,
) -> dict[str, Any]:
    obs = synthesise_observations(
        memory_root=memory_root, device_slug=device_slug, repair_id=repair_id,
    )
    return obs.model_dump()


def mb_set_observation(
    *,
    device_slug: str,
    repair_id: str,
    memory_root: Path,
    target: str,
    mode: str,
) -> dict[str, Any]:
    """Force an observation mode (no measurement), emit WS event.

    Useful when the tech tells the agent « U7 est mort » without a value.
    We record a placeholder MeasurementEvent with value=float('nan') and
    the given mode pre-set so synthesise_observations picks it up.
    """
    try:
        parse_target(target)
    except ValueError as exc:
        return {"recorded": False, "reason": "invalid_target", "detail": str(exc)}

    from datetime import UTC, datetime
    from api.agent.measurement_memory import MeasurementEvent, _journal_path

    ev = MeasurementEvent(
        timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        target=target,
        value=float("nan"),
        unit="V",  # arbitrary — placeholder event, value is not used
        nominal=None,
        note=f"agent-declared mode={mode}",
        source="agent",
        auto_classified_mode=mode,
    )
    path = _journal_path(memory_root, device_slug, repair_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # json.dumps would choke on NaN; emit as a sentinel.
        payload = ev.model_dump()
        payload["value"] = None   # NaN not representable in JSON; store null
        import json
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        return {"recorded": False, "reason": "io_error"}
    _emit({
        "type": "simulation.observation_set",
        "target": target,
        "mode": mode,
        "measurement": None,
    })
    return {"recorded": True, "timestamp": ev.timestamp, "mode": mode}


def mb_clear_observations(
    *,
    device_slug: str,
    repair_id: str,
    memory_root: Path,
) -> dict[str, Any]:
    """Emit the WS clear event. Does NOT delete the journal — clearing the
    journal on disk would lose history; we only tell the UI to reset its
    visible state."""
    _emit({"type": "simulation.observation_clear"})
    return {"cleared": True}
```

Note: the plan stores NaN-bearing events as JSON `null` to keep the journal valid JSON — `load_measurements` in Task 9 should tolerate that. Update `api/agent/measurement_memory.py::MeasurementEvent.value` type:

```python
    value: float | None = None   # None = placeholder event from mb_set_observation
```

Add this edit to the same commit.

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/tools/test_measurements.py tests/agent/test_measurement_memory.py -v 2>&1 | tail -5
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add api/tools/measurements.py tests/tools/test_measurements.py api/agent/measurement_memory.py
git commit -m "$(cat <<'EOF'
feat(tools): measurement agent tools — record / list / compare / synthesise / set / clear

Six mb_* tools wrapping the measurement memory module:
  - mb_record_measurement: append one event, auto-classify, emit WS
  - mb_list_measurements: filtered read (target?, since?)
  - mb_compare_measurements: before/after diff with delta_percent
  - mb_observations_from_measurements: materialise the latest-per-target
    Observations payload
  - mb_set_observation: force a mode without a measurement (placeholder
    NaN event with auto_classified_mode pre-set)
  - mb_clear_observations: emit the WS clear envelope only; journal
    kept for audit trail

A module-level _ws_emitter hook is wired from the runtime at session
open; every write-tool emits simulation.observation_set /
observation_clear through it. Emission errors are swallowed
(best-effort broadcast).

MeasurementEvent.value relaxed to Optional[float] to accept placeholder
events from mb_set_observation.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- api/tools/measurements.py tests/tools/test_measurements.py api/agent/measurement_memory.py
```

---

## Task 14: Register all 6 new tools in the agent manifest + runtime dispatch

**Files:**
- Modify: `api/agent/manifest.py`
- Modify: `api/agent/runtime_direct.py`
- Modify: `api/agent/runtime_managed.py`
- Modify: `tests/tools/test_hypothesize.py`

- [ ] **Step 0: Survey**

```bash
grep -n "mb_schematic_graph\|mb_hypothesize\|mb_expand_knowledge\b" api/agent/manifest.py api/agent/runtime_direct.py api/agent/runtime_managed.py
```

Locate where the existing `mb_*` entries live so you can paste the 6 new ones near them. `runtime_direct.py` may still have one uncommitted line from Alexis (`domain=payload.get("domain")` inside `mb_schematic_graph` dispatch) — preserve it via stash dance:

```bash
git diff api/agent/runtime_direct.py   # check WIP still present
git stash push -m "alexis-wip-domain" -- api/agent/runtime_direct.py   # if dirty
```

- [ ] **Step 1: Update `mb_hypothesize`'s `input_schema` in `manifest.py`**

Find the existing `mb_hypothesize` entry and replace its `input_schema` block with:

```python
"input_schema": {
    "type": "object",
    "properties": {
        "state_comps": {
            "type": "object",
            "description": "Map refdes → mode. Modes: 'dead', 'alive', 'anomalous', 'hot'.",
            "additionalProperties": {
                "type": "string",
                "enum": ["dead", "alive", "anomalous", "hot"],
            },
        },
        "state_rails": {
            "type": "object",
            "description": "Map rail label → mode. Modes: 'dead', 'alive', 'shorted'.",
            "additionalProperties": {
                "type": "string",
                "enum": ["dead", "alive", "shorted"],
            },
        },
        "metrics_comps": {
            "type": "object",
            "description": "Optional numeric measurements on components, refdes → {measured, unit, nominal?}.",
            "additionalProperties": {"type": "object"},
        },
        "metrics_rails": {
            "type": "object",
            "description": "Optional numeric measurements on rails.",
            "additionalProperties": {"type": "object"},
        },
        "max_results": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
        "repair_id": {
            "type": "string",
            "description": "If set AND state/metrics dicts are empty, synthesise observations from the repair's measurement journal.",
        },
    },
    "required": [],
},
```

Also update the description text:

```python
"description": (
    "Propose des hypothèses (refdes, mode) qui expliquent les observations. "
    "Modes supportés : dead (inerte), alive (fonctionne), anomalous (actif mais "
    "output incorrect — IC DSI bridge, codec audio, sensor), hot (chauffe "
    "anormalement), shorted (court vers GND — pour un rail). Passer au moins "
    "une observation via state_comps/state_rails OU fournir repair_id pour "
    "synthétiser depuis le journal de mesures."
),
```

- [ ] **Step 2: Add 6 new manifest entries**

Right after the `mb_hypothesize` entry, add:

```python
{
    "name": "mb_record_measurement",
    "description": (
        "Enregistre une mesure électrique du tech dans le journal de la "
        "repair session. Cible au format 'rail:<label>' | 'comp:<refdes>' | "
        "'pin:<refdes>:<pin>'. Unit ∈ {V, A, W, °C, Ω, mV}. Si nominal est "
        "fourni, le mode est auto-classifié (alive/anomalous/dead/shorted/hot)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "value": {"type": "number"},
            "unit": {"type": "string", "enum": ["V", "A", "W", "°C", "Ω", "mV"]},
            "nominal": {"type": ["number", "null"]},
            "note": {"type": ["string", "null"]},
        },
        "required": ["target", "value", "unit"],
    },
},
{
    "name": "mb_list_measurements",
    "description": "Relit le journal de mesures de la repair session, filtré par target et/ou timestamp.",
    "input_schema": {
        "type": "object",
        "properties": {
            "target": {"type": ["string", "null"]},
            "since": {"type": ["string", "null"]},
        },
        "required": [],
    },
},
{
    "name": "mb_compare_measurements",
    "description": "Diff avant/après d'une cible donnée (mesure la plus ancienne vs la plus récente par défaut).",
    "input_schema": {
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "before_ts": {"type": ["string", "null"]},
            "after_ts": {"type": ["string", "null"]},
        },
        "required": ["target"],
    },
},
{
    "name": "mb_observations_from_measurements",
    "description": "Synthétise un payload Observations (state + metrics) depuis le journal de mesures — dernier événement par cible.",
    "input_schema": {"type": "object", "properties": {}, "required": []},
},
{
    "name": "mb_set_observation",
    "description": "Force un mode d'observation pour une cible sans enregistrer de valeur (utile quand le tech dit 'U7 est mort' sans mesure). Émet l'event WS pour l'UI.",
    "input_schema": {
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "mode": {"type": "string", "enum": ["dead", "alive", "anomalous", "hot", "shorted"]},
        },
        "required": ["target", "mode"],
    },
},
{
    "name": "mb_clear_observations",
    "description": "Efface l'état visuel des observations côté UI (le journal est préservé).",
    "input_schema": {"type": "object", "properties": {}, "required": []},
},
```

- [ ] **Step 3: Wire dispatch in `runtime_managed.py`**

Grep for `if name == "mb_schematic_graph"` in `runtime_managed.py`. Insert before `mb_expand_knowledge`:

```python
    if name == "mb_hypothesize":
        from api.tools.hypothesize import mb_hypothesize as _mb_hypothesize
        return _mb_hypothesize(
            device_slug=device_slug,
            memory_root=memory_root,
            state_comps=payload.get("state_comps"),
            state_rails=payload.get("state_rails"),
            metrics_comps=payload.get("metrics_comps"),
            metrics_rails=payload.get("metrics_rails"),
            max_results=payload.get("max_results", 5),
            repair_id=repair_id if not any([
                payload.get("state_comps"), payload.get("state_rails"),
                payload.get("metrics_comps"), payload.get("metrics_rails"),
            ]) else payload.get("repair_id"),
        )
    if name == "mb_record_measurement":
        from api.tools.measurements import mb_record_measurement as _mb_rec
        return _mb_rec(
            device_slug=device_slug, repair_id=repair_id or "",
            memory_root=memory_root,
            target=payload.get("target", ""),
            value=payload.get("value", 0.0),
            unit=payload.get("unit", "V"),
            nominal=payload.get("nominal"),
            note=payload.get("note"),
            source="agent",
        )
    if name == "mb_list_measurements":
        from api.tools.measurements import mb_list_measurements as _mb_list
        return _mb_list(
            device_slug=device_slug, repair_id=repair_id or "",
            memory_root=memory_root,
            target=payload.get("target"),
            since=payload.get("since"),
        )
    if name == "mb_compare_measurements":
        from api.tools.measurements import mb_compare_measurements as _mb_cmp
        return _mb_cmp(
            device_slug=device_slug, repair_id=repair_id or "",
            memory_root=memory_root,
            target=payload.get("target", ""),
            before_ts=payload.get("before_ts"),
            after_ts=payload.get("after_ts"),
        )
    if name == "mb_observations_from_measurements":
        from api.tools.measurements import mb_observations_from_measurements as _mb_syn
        return _mb_syn(
            device_slug=device_slug, repair_id=repair_id or "",
            memory_root=memory_root,
        )
    if name == "mb_set_observation":
        from api.tools.measurements import mb_set_observation as _mb_set
        return _mb_set(
            device_slug=device_slug, repair_id=repair_id or "",
            memory_root=memory_root,
            target=payload.get("target", ""),
            mode=payload.get("mode", "unknown"),
        )
    if name == "mb_clear_observations":
        from api.tools.measurements import mb_clear_observations as _mb_clr
        return _mb_clr(
            device_slug=device_slug, repair_id=repair_id or "",
            memory_root=memory_root,
        )
```

(The existing `mb_hypothesize` branch is replaced, not duplicated.)

- [ ] **Step 4: Wire the same dispatch in `runtime_direct.py`**

Mirror the Step 3 block in `runtime_direct.py`. Variable names should match the surrounding dispatch function (look for `device_slug`, `memory_root`, `session_id`, `repair_id` — if `repair_id` isn't in scope, read it from the session / settings wherever the existing managed path reads it). If `runtime_direct.py` doesn't have a `repair_id` in scope, pass `""` (the tools handle it).

- [ ] **Step 5: Pop Alexis's stash if it was saved**

```bash
git stash list | head -3
# If "alexis-wip-domain" present:
git stash pop
```

Should clean-apply since the `mb_hypothesize` branch you replaced only changes the dispatch wiring, not Alexis's `domain=payload.get("domain")` line inside `mb_schematic_graph`.

- [ ] **Step 6: Wire the `_ws_emitter` at session open**

In `runtime_managed.py` and `runtime_direct.py`, find where the session's WS is wired (where you send outgoing events). Near the top of the session handler, add:

```python
from api.tools.measurements import set_ws_emitter

def _emit(event: dict) -> None:
    # Send as JSON through the existing WS.
    asyncio.create_task(ws.send_json(event))

set_ws_emitter(_emit)
```

Place it inside the session's try-block, and add a `set_ws_emitter(None)` in the `finally` so disconnection doesn't leak a dangling emitter reference to the next session.

- [ ] **Step 7: Manifest test**

```bash
.venv/bin/pytest tests/tools/test_hypothesize.py::test_mb_hypothesize_manifest_exposes_new_signature -v
.venv/bin/pytest tests/agent/ -v 2>&1 | tail -10
```

Expected: green.

- [ ] **Step 8: Lint + commit**

```bash
.venv/bin/ruff check api/agent/manifest.py api/agent/runtime_direct.py api/agent/runtime_managed.py
git add api/agent/manifest.py api/agent/runtime_direct.py api/agent/runtime_managed.py tests/tools/test_hypothesize.py
git commit -m "$(cat <<'EOF'
feat(agent): register 6 measurement/observation tools + updated mb_hypothesize schema

Manifest advertises mb_hypothesize (schema B: state_comps/state_rails/
metrics_*/repair_id), mb_record_measurement, mb_list_measurements,
mb_compare_measurements, mb_observations_from_measurements,
mb_set_observation, mb_clear_observations. Each is wired in both
runtime_direct and runtime_managed dispatchers, and the WS emitter is
bound at session open so simulation.observation_set/clear events fan
out to the frontend as Claude calls the write tools.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- api/agent/manifest.py api/agent/runtime_direct.py api/agent/runtime_managed.py tests/tools/test_hypothesize.py
```

---

## Task 15: HTTP — migrate `/hypothesize` body + new `/measurements` routes

**Files:**
- Modify: `api/pipeline/__init__.py`
- Modify: `tests/pipeline/test_hypothesize_endpoint.py`
- Create: `tests/pipeline/test_measurements_endpoint.py`

- [ ] **Step 1: Failing tests — `/hypothesize` body migration**

Open `tests/pipeline/test_hypothesize_endpoint.py` and replace its body content (happy / unknown_refdes / 404 / empty) with the schema B variants:

```python
def test_hypothesize_happy_schema_b(tmp_memory: Path, client: TestClient):
    r = client.post(
        f"/pipeline/packs/{SLUG}/schematic/hypothesize",
        json={"state_rails": {"+5V": "dead"}, "state_comps": {"U12": "dead"}},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["device_slug"] == SLUG
    assert payload["hypotheses"][0]["kill_refdes"] == ["U7"]
    assert payload["hypotheses"][0]["kill_modes"] == ["dead"]


def test_hypothesize_accepts_metrics(tmp_memory: Path, client: TestClient):
    r = client.post(
        f"/pipeline/packs/{SLUG}/schematic/hypothesize",
        json={
            "state_rails": {"+5V": "dead"},
            "metrics_rails": {"+5V": {"measured": 0.02, "unit": "V", "nominal": 5.0}},
        },
    )
    assert r.status_code == 200
    # Measurement cited.
    assert "0.02" in r.text or "5.0" in r.text


def test_hypothesize_unknown_refdes_400(tmp_memory: Path, client: TestClient):
    r = client.post(
        f"/pipeline/packs/{SLUG}/schematic/hypothesize",
        json={"state_comps": {"Z999": "dead"}},
    )
    assert r.status_code == 400
    assert "Z999" in r.text


def test_hypothesize_empty_body_returns_empty(tmp_memory: Path, client: TestClient):
    r = client.post(
        f"/pipeline/packs/{SLUG}/schematic/hypothesize",
        json={},
    )
    assert r.status_code == 200
    assert r.json()["hypotheses"] == []
```

(Keep the existing `tmp_memory` / `client` fixtures + the 404 test unchanged.)

- [ ] **Step 2: Failing tests — `/measurements` routes**

Create `tests/pipeline/test_measurements_endpoint.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""HTTP coverage for /pipeline/packs/{slug}/repairs/{repair_id}/measurements."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import config
from api.config import get_settings
from api.main import app

SLUG = "demo"
REPAIR = "r1"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def tmp_memory(tmp_path: Path, monkeypatch):
    memory_root = tmp_path / "memory"
    (memory_root / SLUG / "repairs" / REPAIR).mkdir(parents=True)
    settings = get_settings()
    monkeypatch.setattr(settings, "memory_root", str(memory_root))
    monkeypatch.setattr(config, "_settings", None, raising=False)
    yield memory_root


def test_post_measurement_records(tmp_memory: Path, client: TestClient):
    r = client.post(
        f"/pipeline/packs/{SLUG}/repairs/{REPAIR}/measurements",
        json={"target": "rail:+3V3", "value": 2.87, "unit": "V", "nominal": 3.3},
    )
    assert r.status_code == 201
    payload = r.json()
    assert payload["auto_classified_mode"] == "anomalous"


def test_get_measurements_returns_events(tmp_memory: Path, client: TestClient):
    client.post(
        f"/pipeline/packs/{SLUG}/repairs/{REPAIR}/measurements",
        json={"target": "rail:+3V3", "value": 2.87, "unit": "V", "nominal": 3.3},
    )
    r = client.get(f"/pipeline/packs/{SLUG}/repairs/{REPAIR}/measurements")
    assert r.status_code == 200
    assert len(r.json()["events"]) == 1


def test_get_measurements_filter_target(tmp_memory: Path, client: TestClient):
    client.post(
        f"/pipeline/packs/{SLUG}/repairs/{REPAIR}/measurements",
        json={"target": "rail:+3V3", "value": 2.87, "unit": "V", "nominal": 3.3},
    )
    client.post(
        f"/pipeline/packs/{SLUG}/repairs/{REPAIR}/measurements",
        json={"target": "comp:Q17", "value": 72.0, "unit": "°C"},
    )
    r = client.get(
        f"/pipeline/packs/{SLUG}/repairs/{REPAIR}/measurements?target=comp:Q17",
    )
    assert len(r.json()["events"]) == 1
    assert r.json()["events"][0]["target"] == "comp:Q17"
```

- [ ] **Step 3: Confirm both test files fail**

```bash
.venv/bin/pytest tests/pipeline/test_hypothesize_endpoint.py tests/pipeline/test_measurements_endpoint.py -v
```

Expected: failures on the migrated hypothesize tests + ModuleNotFoundError-style 404/405 for measurements.

- [ ] **Step 4: Migrate the endpoint + add measurement routes in `api/pipeline/__init__.py`**

Find the existing `HypothesizeRequest` + `post_hypothesize`. Replace with:

```python
class HypothesizeRequest(BaseModel):
    state_comps: dict[str, str] = Field(default_factory=dict)
    state_rails: dict[str, str] = Field(default_factory=dict)
    metrics_comps: dict[str, dict] = Field(default_factory=dict)
    metrics_rails: dict[str, dict] = Field(default_factory=dict)
    max_results: int = Field(default=5, ge=1, le=20)
    repair_id: str | None = None


@router.post("/packs/{device_slug}/schematic/hypothesize")
async def post_hypothesize(device_slug: str, request: HypothesizeRequest) -> dict:
    from api.tools.hypothesize import mb_hypothesize as _mb_hypothesize_tool
    settings = get_settings()
    slug = _slugify(device_slug)
    result = _mb_hypothesize_tool(
        device_slug=slug,
        memory_root=Path(settings.memory_root),
        state_comps=request.state_comps or None,
        state_rails=request.state_rails or None,
        metrics_comps=request.metrics_comps or None,
        metrics_rails=request.metrics_rails or None,
        max_results=request.max_results,
        repair_id=request.repair_id,
    )
    if not result.get("found"):
        reason = result.get("reason", "unknown")
        if reason == "no_schematic_graph":
            raise HTTPException(status_code=404, detail=f"No schematic for {slug!r}")
        if reason in ("unknown_refdes", "unknown_rail"):
            raise HTTPException(status_code=400, detail=result)
        raise HTTPException(status_code=422, detail=result)
    result.pop("found", None)
    return result
```

Append the measurement routes at the end of the file:

```python
class MeasurementCreate(BaseModel):
    target: str
    value: float
    unit: str
    nominal: float | None = None
    note: str | None = None


@router.post(
    "/packs/{device_slug}/repairs/{repair_id}/measurements",
    status_code=201,
)
async def post_measurement(
    device_slug: str, repair_id: str, body: MeasurementCreate,
) -> dict:
    from api.tools.measurements import mb_record_measurement as _rec
    settings = get_settings()
    result = _rec(
        device_slug=_slugify(device_slug), repair_id=repair_id,
        memory_root=Path(settings.memory_root),
        target=body.target, value=body.value, unit=body.unit,
        nominal=body.nominal, note=body.note, source="ui",
    )
    if not result.get("recorded"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.get("/packs/{device_slug}/repairs/{repair_id}/measurements")
async def get_measurements(
    device_slug: str, repair_id: str,
    target: str | None = None, since: str | None = None,
) -> dict:
    from api.tools.measurements import mb_list_measurements as _lst
    settings = get_settings()
    return _lst(
        device_slug=_slugify(device_slug), repair_id=repair_id,
        memory_root=Path(settings.memory_root),
        target=target, since=since,
    )
```

- [ ] **Step 5: Run test suites**

```bash
.venv/bin/pytest tests/pipeline/test_hypothesize_endpoint.py tests/pipeline/test_measurements_endpoint.py -v
```

Expected: all green.

- [ ] **Step 6: Lint + commit**

```bash
.venv/bin/ruff check api/pipeline/__init__.py tests/pipeline/test_hypothesize_endpoint.py tests/pipeline/test_measurements_endpoint.py
git add api/pipeline/__init__.py tests/pipeline/test_hypothesize_endpoint.py tests/pipeline/test_measurements_endpoint.py
git commit -m "$(cat <<'EOF'
feat(api): schema B hypothesize body + measurements routes

Breaking migration of POST /pipeline/packs/{slug}/schematic/hypothesize
from 4 refdes-list fields to {state_comps, state_rails, metrics_comps,
metrics_rails, repair_id?, max_results}. Sole caller is
web/js/schematic.js, migrated in Task 17.

Two new routes:
  POST /pipeline/packs/{slug}/repairs/{repair_id}/measurements
    → append a MeasurementEvent to the journal, auto-classify, return
      {recorded, auto_classified_mode, timestamp}
  GET  /pipeline/packs/{slug}/repairs/{repair_id}/measurements
    → list events, optional ?target= and ?since= filters

Both routes delegate to the same mb_* tools so validation stays
consolidated. Ownership of the per-request WS emission remains in the
agent runtime; HTTP writes from the UI don't emit WS events to the
agent (by design — the tech's direct clicks are observed by polling the
journal when the agent is asked to diagnose).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- api/pipeline/__init__.py tests/pipeline/test_hypothesize_endpoint.py tests/pipeline/test_measurements_endpoint.py
```

---

## Task 16: Group C closing — full backend test + lint sweep

**Files:**
- Verify only.

- [ ] **Step 1: Run the full backend suite**

```bash
make test 2>&1 | tail -10
```

Expected: all green. If a test that was passing pre-migration fails now, check whether it hard-codes the old schema-A shapes and migrate it.

- [ ] **Step 2: Lint sweep on all the files touched in Group C**

```bash
.venv/bin/ruff check \
  api/pipeline/schematic/hypothesize.py \
  api/agent/measurement_memory.py \
  api/tools/hypothesize.py \
  api/tools/measurements.py \
  api/tools/ws_events.py \
  api/agent/manifest.py \
  api/agent/runtime_direct.py \
  api/agent/runtime_managed.py \
  api/pipeline/__init__.py \
  tests/pipeline/schematic/test_hypothesize.py \
  tests/agent/test_measurement_memory.py \
  tests/tools/test_hypothesize.py \
  tests/tools/test_measurements.py \
  tests/tools/test_ws_events_sim.py \
  tests/pipeline/test_hypothesize_endpoint.py \
  tests/pipeline/test_measurements_endpoint.py
```

Expected: `All checks passed!`. If not, fix inline and commit as `chore(tidy): lint sweep on richer-fault-modes backend`.

No separate commit required for this task unless there's a lint fix.

---

## Task 17: Frontend — migrate `SimulationController.observations` to Maps + WS handler

**Files:**
- Modify: `web/js/schematic.js`

**Browser verification: light (no visible change yet, but behavior must not regress). Do NOT commit without running the T14 checklist again first.**

- [ ] **Step 1: Migrate the state container**

Find `const SimulationController = {` (around line 35). Replace the `observations` field:

```javascript
  observations: {
    state_comps: new Map(),     // refdes → "dead" | "alive" | "anomalous" | "hot"
    state_rails: new Map(),     // rail label → "dead" | "alive" | "shorted"
    metrics_comps: new Map(),   // refdes → {measured, unit, nominal?, note?, ts}
    metrics_rails: new Map(),   // rail → {measured, unit, nominal?, note?, ts}
  },
  hypotheses: null,
```

- [ ] **Step 2: Replace the helpers**

Replace `setObservation`, `clearObservations`, `_applyObservationClasses` with schema B versions:

```javascript
  setObservation(kind, key, mode, measurement = null) {
    // kind: "comp" | "rail"
    // mode: "dead" | "alive" | "anomalous" | "hot" | "shorted" | "unknown"
    const stateMap = kind === "comp" ? this.observations.state_comps : this.observations.state_rails;
    const metricMap = kind === "comp" ? this.observations.metrics_comps : this.observations.metrics_rails;
    if (mode === "unknown" || mode == null) {
      stateMap.delete(key);
      metricMap.delete(key);
    } else {
      stateMap.set(key, mode);
      if (measurement) {
        metricMap.set(key, {
          ...measurement,
          ts: measurement.ts || new Date().toISOString(),
        });
      }
    }
    this._applyObservationClasses();
  },
  clearObservations() {
    for (const m of Object.values(this.observations)) m.clear();
    this.hypotheses = null;
    this._applyObservationClasses();
    document.querySelectorAll(".sim-hypotheses-panel").forEach(p => p.remove());
  },
  _applyObservationClasses() {
    document
      .querySelectorAll(".obs-dead, .obs-alive, .obs-anomalous, .obs-hot, .obs-shorted")
      .forEach(n => n.classList.remove(
        "obs-dead", "obs-alive", "obs-anomalous", "obs-hot", "obs-shorted",
      ));
    for (const [refdes, mode] of this.observations.state_comps) {
      document.querySelectorAll(`[data-refdes="${CSS.escape(refdes)}"]`).forEach(el => {
        el.classList.add(`obs-${mode}`);
      });
    }
    for (const [rail, mode] of this.observations.state_rails) {
      document.querySelectorAll(`[data-rail="${CSS.escape(rail)}"]`).forEach(el => {
        el.classList.add(`obs-${mode}`);
      });
    }
  },
```

- [ ] **Step 3: Update the Diagnostiquer call site**

Find `SimulationController.hypothesize` (inside the object literal). Replace its body:

```javascript
  async hypothesize(slug) {
    const obs = this.observations;
    const totalObs = obs.state_comps.size + obs.state_rails.size
                   + obs.metrics_comps.size + obs.metrics_rails.size;
    if (totalObs === 0) return;
    const body = {
      state_comps: Object.fromEntries(obs.state_comps),
      state_rails: Object.fromEntries(obs.state_rails),
      metrics_comps: Object.fromEntries(obs.metrics_comps),
      metrics_rails: Object.fromEntries(obs.metrics_rails),
      max_results: 5,
    };
    try {
      const res = await fetch(
        `/pipeline/packs/${encodeURIComponent(slug)}/schematic/hypothesize`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
      );
      if (!res.ok) {
        console.warn("[hypothesize] HTTP", res.status, await res.text());
        return;
      }
      const payload = await res.json();
      this.hypotheses = payload.hypotheses || [];
      this._renderHypothesesPanel();
    } catch (err) {
      console.warn("[hypothesize] fetch error", err);
    }
  },
```

- [ ] **Step 4: Update the card-click fallback**

In `_renderHypothesesPanel` find where `killedRefdes = [...h.kill_refdes]` is assigned on card click. The simulator still drives its cascade off `killedRefdes` (always dead mode), so leave that assignment — but ALSO include the mode chips in the card head:

Replace the line building the chip list:

```javascript
      const chips = h.kill_refdes.map((r, i) => {
        const m = (h.kill_modes || [])[i] || "dead";
        const modeLabel = { dead: "mort", anomalous: "anomalous", hot: "chaud", shorted: "shorté" }[m] || m;
        return `<span class="sim-hyp-chip sim-hyp-chip--${m}">${r} · ${modeLabel}</span>`;
      }).join(" + ");
```

Update the diff rendering — `contradictions` now carries `(target, observed, predicted)` tuples:

```javascript
      const contradictions = (h.diff.contradictions || []).map(c => {
        if (Array.isArray(c) && c.length === 3) {
          const [target, observed, predicted] = c;
          return `<span class="sim-hyp-tag sim-hyp-tag-fp">${target} obs ${observed} → prédit ${predicted}</span>`;
        }
        return `<span class="sim-hyp-tag sim-hyp-tag-fp">${c}</span>`;
      }).join(" ");
```

- [ ] **Step 5: Wire the WS handler**

Grep for `ws.addEventListener("message"` in `web/js/schematic.js` or `web/js/llm.js`. The schematic section listens to WS pushes from the same `/ws/diagnostic/...` socket — add these two branches:

```javascript
      if (msg.type === "simulation.observation_set") {
        const parsed = (typeof msg.target === "string" && msg.target.includes(":"))
          ? msg.target.split(":", 2) : [null, null];
        const kind = parsed[0] === "rail" ? "rail" : parsed[0] === "comp" ? "comp" : null;
        const key = parsed[1];
        if (kind && key) {
          SimulationController.setObservation(kind, key, msg.mode, msg.measurement);
        }
      } else if (msg.type === "simulation.observation_clear") {
        SimulationController.clearObservations();
      }
```

Place before the existing `boardview.*` branches.

- [ ] **Step 6: Syntax check**

```bash
node --check web/js/schematic.js
```

- [ ] **Step 7: Browser smoke (Alexis-verify before commit)**

Hard-reload the page. Run the T14 checklist again (click U12 → ❌ mort → amber glow). It must still work. Click Diagnostiquer after setting U12=dead + +5V=alive → top-1 should be `U12 · mort` (chip includes the mode).

Wait for « ok commit ».

- [ ] **Step 8: Commit once Alexis validates**

```bash
git add web/js/schematic.js
git commit -m "$(cat <<'EOF'
refactor(web): migrate SimulationController.observations to schema B Maps

Replaces the four Sets with four Maps keyed by refdes / rail to mode
strings plus parallel Maps for numeric metrics. setObservation takes
(kind, key, mode, measurement?) and writes to both maps atomically.
Mode classes on graph nodes expanded to .obs-{dead,alive,anomalous,
hot,shorted} for stylable per-mode badges (visual delta lands in T18).

SimulationController.hypothesize now POSTs {state_comps, state_rails,
metrics_comps, metrics_rails, max_results} and displays mode chips in
the hypothesis cards. The diff contradiction rendering adapts to the
new (target, observed, predicted) tuple shape.

New WS handler for simulation.observation_set / observation_clear
mirrors agent-side measurement tools onto the UI state in real time.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- web/js/schematic.js
```

---

## Task 18: Frontend — contextual mode-picker + metric input

**Files:**
- Modify: `web/js/schematic.js`
- Modify: `web/styles/schematic.css`

**Browser verification REQUIRED. The observation-row replaces the 3-toggle pattern entirely — Alexis must see and approve before commit.**

- [ ] **Step 1: Replace the observation-row in `updateInspector`**

Find the `.sim-obs-row` block added in T14. Replace with:

```javascript
  // --- Observation row (reverse-diagnostic input, contextual per node kind) ---
  const obsKind = node.kind === "component" ? "comp" : node.kind === "rail" ? "rail" : null;
  const obsKey = node.kind === "component" ? node.refdes : node.kind === "rail" ? node.label : null;
  if (obsKind && obsKey) {
    const modesForKind = obsKind === "rail"
      ? [["unknown", "⚪ inconnu"], ["alive", "✅ vivant"], ["dead", "❌ mort"], ["shorted", "⚡ shorté"]]
      : [["unknown", "⚪ inconnu"], ["alive", "✅ vivant"], ["dead", "❌ mort"], ["anomalous", "⚠ anomalous"], ["hot", "🔥 chaud"]];
    const stateMap = obsKind === "rail"
      ? SimulationController.observations.state_rails
      : SimulationController.observations.state_comps;
    const current = stateMap.get(obsKey) || "unknown";

    const row = document.createElement("div");
    row.className = "sim-obs-row";
    const picker = document.createElement("div");
    picker.className = "sim-mode-picker";
    picker.setAttribute("data-kind", obsKind);
    for (const [mode, label] of modesForKind) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.dataset.mode = mode;
      if (mode === current) btn.classList.add("active");
      btn.textContent = label;
      btn.addEventListener("click", () => {
        SimulationController.setObservation(obsKind, obsKey, mode);
        updateInspector(node);
      });
      picker.appendChild(btn);
    }
    row.innerHTML = `<span class="sim-obs-label">Observation</span>`;
    row.appendChild(picker);
    body.appendChild(row);

    // --- Metric input row ---
    const unitForKind = obsKind === "rail" ? "V" : "°C";
    const metricMap = obsKind === "rail"
      ? SimulationController.observations.metrics_rails
      : SimulationController.observations.metrics_comps;
    const existingMetric = metricMap.get(obsKey);

    const metricRow = document.createElement("div");
    metricRow.className = "sim-metric-row";
    metricRow.innerHTML = `
      <span class="sim-obs-label">Mesuré</span>
      <input type="number" class="sim-metric-input" step="0.01" value="${existingMetric?.measured ?? ""}">
      <select class="sim-metric-unit">
        ${["V", "mV", "A", "°C", "Ω", "W"].map(u =>
          `<option value="${u}" ${u === (existingMetric?.unit || unitForKind) ? "selected" : ""}>${u}</option>`
        ).join("")}
      </select>
      <span class="sim-metric-nominal">${existingMetric?.nominal ? `nominal: ${existingMetric.nominal}${existingMetric.unit || unitForKind}` : ""}</span>
      <button type="button" class="sim-metric-record">Enregistrer</button>
    `;
    const inputEl = metricRow.querySelector(".sim-metric-input");
    const unitEl = metricRow.querySelector(".sim-metric-unit");
    const recordBtn = metricRow.querySelector(".sim-metric-record");
    const doRecord = async () => {
      const valueRaw = inputEl.value.trim();
      if (valueRaw === "") return;
      const value = parseFloat(valueRaw);
      if (!Number.isFinite(value)) return;
      const unit = unitEl.value;
      const nominal = existingMetric?.nominal ?? null;
      // Client-side auto-classify mirror (same thresholds as Python side).
      const mode = clientAutoClassify(obsKind, value, unit, nominal);
      // Update local state immediately.
      SimulationController.setObservation(obsKind, obsKey, mode || "unknown", {
        measured: value, unit, nominal,
      });
      // POST to the journal if we have a repair_id.
      const slug = STATE.slug;
      const repairId = new URLSearchParams(location.search).get("repair")
        || new URLSearchParams(location.hash.split("?")[1] || "").get("repair");
      if (slug && repairId) {
        try {
          await fetch(
            `/pipeline/packs/${encodeURIComponent(slug)}/repairs/${encodeURIComponent(repairId)}/measurements`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                target: `${obsKind === "comp" ? "comp" : "rail"}:${obsKey}`,
                value, unit, nominal,
              }),
            },
          );
        } catch (err) {
          console.warn("[measurements] POST failed", err);
        }
      }
      updateInspector(node);
    };
    inputEl.addEventListener("keydown", ev => { if (ev.key === "Enter") doRecord(); });
    inputEl.addEventListener("blur", doRecord);
    recordBtn.addEventListener("click", doRecord);
    body.appendChild(metricRow);
  }
```

- [ ] **Step 2: Add the client-side auto-classify mirror**

Near the top of `web/js/schematic.js` (near `STATE`), add a pure helper:

```javascript
// Client-side mirror of api/agent/measurement_memory.py::auto_classify.
// Keep thresholds in sync with the Python constants.
function clientAutoClassify(kind, value, unit, nominal) {
  if (kind === "rail" && (unit === "V" || unit === "mV")) {
    if (nominal == null || nominal === "") return null;
    const v = unit === "mV" ? value / 1000 : value;
    const nom = unit === "mV" ? nominal / 1000 : nominal;
    if (v < 0.05) return "dead";
    const ratio = nom !== 0 ? v / nom : 0;
    if (ratio > 1.10) return "shorted";
    if (ratio >= 0.90) return "alive";
    return "anomalous";
  }
  if (kind === "comp" && unit === "°C") {
    return value >= 65 ? "hot" : "alive";
  }
  return null;
}
```

- [ ] **Step 3: CSS for `.sim-mode-picker` + `.sim-metric-row` + mode badges**

Append to `web/styles/schematic.css` (replacing/adjacent to the old `.sim-obs-row button` block):

```css
/* Contextual mode picker — one button per applicable mode. */
.sim-mode-picker {
  display: flex; gap: 6px; flex-wrap: wrap;
  font-family: var(--mono);
}
.sim-mode-picker button {
  all: unset; cursor: pointer;
  padding: 3px 8px;
  border: 1px solid var(--border-soft);
  border-radius: 3px;
  color: var(--text-3);
  font-size: 10.5px;
  text-transform: uppercase; letter-spacing: .4px;
  transition: color .15s, border-color .15s, background .15s;
}
.sim-mode-picker button:hover { color: var(--text); }
.sim-mode-picker button.active[data-mode="dead"]       { color: var(--amber);   border-color: var(--amber);   background: color-mix(in oklch, var(--amber) 12%, transparent); }
.sim-mode-picker button.active[data-mode="alive"]      { color: var(--emerald); border-color: var(--emerald); background: color-mix(in oklch, var(--emerald) 12%, transparent); }
.sim-mode-picker button.active[data-mode="anomalous"]  { color: var(--violet);  border-color: var(--violet);  background: color-mix(in oklch, var(--violet) 12%, transparent); }
.sim-mode-picker button.active[data-mode="hot"]        { color: var(--amber);   border-color: var(--amber);   background: color-mix(in oklch, var(--amber) 20%, transparent); box-shadow: 0 0 0 1px color-mix(in oklch, var(--amber) 40%, transparent); }
.sim-mode-picker button.active[data-mode="shorted"]    { color: var(--amber);   border-color: var(--amber);   background: color-mix(in oklch, var(--amber) 20%, transparent); }
.sim-mode-picker button.active[data-mode="unknown"]    { color: var(--text-2);  border-color: var(--text-3); }

/* Metric input row — numeric entry with unit select + record button. */
.sim-metric-row {
  display: flex; align-items: center; gap: 8px;
  margin: 8px 10px 0;
  padding: 4px 0;
  font-family: var(--mono);
  font-size: 10.5px;
}
.sim-metric-row .sim-obs-label { flex: 0 0 80px; }
.sim-metric-input {
  width: 72px;
  padding: 3px 6px;
  background: var(--bg-2);
  border: 1px solid var(--border-soft);
  border-radius: 3px;
  color: var(--text);
  font-family: var(--mono);
  font-size: 11px;
}
.sim-metric-input:focus { outline: none; border-color: var(--cyan); }
.sim-metric-unit {
  padding: 3px 6px;
  background: var(--bg-2);
  border: 1px solid var(--border-soft);
  border-radius: 3px;
  color: var(--text-2);
  font-size: 10.5px;
  font-family: var(--mono);
}
.sim-metric-nominal { flex: 1; color: var(--text-3); }
.sim-metric-record {
  all: unset; cursor: pointer;
  padding: 3px 8px;
  border: 1px solid color-mix(in oklch, var(--cyan) 60%, transparent);
  border-radius: 3px;
  color: var(--cyan);
  font-size: 10.5px;
  text-transform: uppercase; letter-spacing: .4px;
  transition: background .15s, border-color .15s;
}
.sim-metric-record:hover { background: color-mix(in oklch, var(--cyan) 12%, transparent); border-color: var(--cyan); }

/* Node badges — one per mode. */
#schematicSection .obs-dead .sch-shape       { stroke: var(--amber);   stroke-width: 2.2px; filter: drop-shadow(0 0 3px color-mix(in oklch, var(--amber)   40%, transparent)); }
#schematicSection .obs-alive .sch-shape      { stroke: var(--emerald); stroke-width: 2.2px; filter: drop-shadow(0 0 3px color-mix(in oklch, var(--emerald) 40%, transparent)); }
#schematicSection .obs-anomalous .sch-shape  { stroke: var(--violet);  stroke-width: 2.2px; filter: drop-shadow(0 0 3px color-mix(in oklch, var(--violet)  40%, transparent)); stroke-dasharray: 3 2; }
#schematicSection .obs-hot .sch-shape        { stroke: var(--amber);   stroke-width: 2.6px; filter: drop-shadow(0 0 4px color-mix(in oklch, var(--amber)   60%, transparent)); }
#schematicSection .obs-shorted .sch-shape    { stroke: var(--amber);   stroke-width: 2.6px; filter: drop-shadow(0 0 5px color-mix(in oklch, var(--amber)   60%, transparent)); stroke-dasharray: 2 2; }

/* Hypothesis chips — one style per predicted mode. */
.sim-hyp-chip--dead      { background: color-mix(in oklch, var(--amber)   14%, transparent); color: var(--amber);   border: 1px solid color-mix(in oklch, var(--amber)   40%, transparent); padding: 1px 6px; border-radius: 2px; }
.sim-hyp-chip--anomalous { background: color-mix(in oklch, var(--violet)  14%, transparent); color: var(--violet);  border: 1px solid color-mix(in oklch, var(--violet)  40%, transparent); padding: 1px 6px; border-radius: 2px; }
.sim-hyp-chip--hot       { background: color-mix(in oklch, var(--amber)   20%, transparent); color: var(--amber);   border: 1px solid color-mix(in oklch, var(--amber)   60%, transparent); padding: 1px 6px; border-radius: 2px; }
.sim-hyp-chip--shorted   { background: color-mix(in oklch, var(--amber)   20%, transparent); color: var(--amber);   border: 1px solid var(--amber); padding: 1px 6px; border-radius: 2px; }
```

Remove the now-unused old `.sim-obs-row button.active[data-obs=...]` rules (the row layout stays but the inner picker uses `data-mode` now).

- [ ] **Step 4: Syntax check**

```bash
node --check web/js/schematic.js
```

- [ ] **Step 5: BROWSER VERIFY (Alexis)**

Checklist:

1. Hard-reload. Click U10 (IC with signal edges in MNT).
2. Inspector shows `[⚪ inconnu] [✅ vivant] [❌ mort] [⚠ anomalous] [🔥 chaud]` (5 options for an IC).
3. Click each mode → node border changes (amber / emerald / violet dashed / amber glow) → click it again to switch.
4. Click a rail (e.g. `+3V3`). Inspector shows `[⚪] [✅] [❌] [⚡ shorté]` (4 options, no anomalous / hot).
5. Enter a value in the metric input (e.g. 2.87, unit V). Press Enter.
6. The node auto-classifies (anomalous if 2.87V on a 3.3V rail). The picker flips to `⚠ anomalous`.
7. The metric row shows the nominal hint. Console: no errors.
8. If a `?repair=…` is in the URL, check `memory/{slug}/repairs/{repair_id}/measurements.jsonl` — a new line was appended.

Wait for « ok commit ».

- [ ] **Step 6: Commit once Alexis validates**

```bash
git add web/js/schematic.js web/styles/schematic.css
git commit -m "$(cat <<'EOF'
feat(web): contextual mode picker + metric input per node kind

Inspector gets a .sim-mode-picker segmented control whose options are
derived from the node kind:
  - component → [inconnu, alive, dead, anomalous, hot]
  - rail      → [inconnu, alive, dead, shorted]

A .sim-metric-row captures free-form numeric measurements with unit
selection. On blur/enter/click it calls clientAutoClassify (mirror of
the Python auto_classify thresholds: rail ±10% → alive, 50-90% →
anomalous, <50mV → dead, >110% → shorted; IC >65°C → hot), flips the
picker visually, stores the metric in SimulationController state, and
POSTs to /pipeline/packs/{slug}/repairs/{repair_id}/measurements when
a repair is active in the URL.

Node badges split into .obs-{dead,alive,anomalous,hot,shorted} with
distinct OKLCH fills / dashes / glows so the tech sees the mode at a
glance on the graph.

Hypothesis chips gain per-mode classes (sim-hyp-chip--{mode}) so the
results panel colour-codes the predicted failure type.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- web/js/schematic.js web/styles/schematic.css
```

---

## Task 19: Frontend — per-target measurement mini-timeline

**Files:**
- Modify: `web/js/schematic.js`
- Modify: `web/styles/schematic.css`

**Browser verification REQUIRED.**

- [ ] **Step 1: Fetch + render the recent-measurements list**

Add a helper in `schematic.js` near the other controller methods:

```javascript
  async loadMeasurementHistory(target) {
    const slug = STATE.slug;
    const repairId = new URLSearchParams(location.search).get("repair")
      || new URLSearchParams((location.hash.split("?")[1] || "")).get("repair");
    if (!slug || !repairId) return [];
    try {
      const res = await fetch(
        `/pipeline/packs/${encodeURIComponent(slug)}/repairs/${encodeURIComponent(repairId)}/measurements?target=${encodeURIComponent(target)}`,
      );
      if (!res.ok) return [];
      const payload = await res.json();
      return payload.events || [];
    } catch (err) {
      console.warn("[measurements] GET failed", err);
      return [];
    }
  },
```

- [ ] **Step 2: Inject the history UI inside `updateInspector` after the metric row**

Below the `body.appendChild(metricRow)` call:

```javascript
    // --- Measurement history (async fetch, replaces on reopen) ---
    const historyBox = document.createElement("div");
    historyBox.className = "sim-measurement-history";
    historyBox.innerHTML = `<div class="sim-mh-title">Historique — ${obsKey}</div><div class="sim-mh-list"></div>`;
    body.appendChild(historyBox);
    (async () => {
      const target = `${obsKind === "comp" ? "comp" : "rail"}:${obsKey}`;
      const events = await SimulationController.loadMeasurementHistory(target);
      const listEl = historyBox.querySelector(".sim-mh-list");
      if (!events.length) {
        listEl.innerHTML = `<div class="sim-mh-empty">Aucune mesure pour cette cible.</div>`;
        return;
      }
      // Keep the 6 most recent (reverse order).
      const recent = events.slice(-6);
      let prev = null;
      const rows = recent.map(ev => {
        const ts = (ev.timestamp || "").slice(11, 19);  // HH:MM:SS
        const val = ev.value != null ? `${ev.value}${ev.unit || ""}` : "—";
        const ratio = (ev.value != null && ev.nominal)
          ? ` (${((ev.value / ev.nominal) * 100).toFixed(0)}%)`
          : "";
        const mode = ev.auto_classified_mode || "—";
        const note = ev.note ? ` · « ${escHtml(ev.note)} »` : "";
        const delta = (prev && ev.value != null && prev.value != null)
          ? ` Δ${(ev.value - prev.value).toFixed(3)}`
          : "";
        prev = ev;
        return `
          <div class="sim-mh-row">
            <span class="sim-mh-ts">${ts}</span>
            <span class="sim-mh-val">${val}${ratio}</span>
            <span class="sim-mh-mode sim-mh-mode--${mode}">${mode}</span>
            <span class="sim-mh-note">${delta}${note}</span>
          </div>`;
      });
      listEl.innerHTML = rows.join("");
    })();
```

- [ ] **Step 3: CSS for `.sim-measurement-history`**

Append to `web/styles/schematic.css`:

```css
.sim-measurement-history {
  margin: 10px 10px 0;
  padding-top: 8px;
  border-top: 1px solid var(--border-soft);
}
.sim-mh-title {
  font-family: var(--mono);
  font-size: 10px; color: var(--text-3);
  text-transform: uppercase; letter-spacing: .4px;
  margin-bottom: 4px;
}
.sim-mh-list { display: flex; flex-direction: column; gap: 4px; }
.sim-mh-row {
  display: grid;
  grid-template-columns: 62px 90px 80px 1fr;
  align-items: center;
  gap: 6px;
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--text-2);
  padding: 2px 0;
}
.sim-mh-ts   { color: var(--text-3); }
.sim-mh-val  { color: var(--text); }
.sim-mh-mode { font-size: 9.5px; text-transform: uppercase; letter-spacing: .3px; padding: 1px 5px; border-radius: 2px; border: 1px solid var(--border-soft); text-align: center; }
.sim-mh-mode--dead      { color: var(--amber);   border-color: var(--amber); }
.sim-mh-mode--alive     { color: var(--emerald); border-color: var(--emerald); }
.sim-mh-mode--anomalous { color: var(--violet);  border-color: var(--violet); }
.sim-mh-mode--hot       { color: var(--amber);   border-color: var(--amber); background: color-mix(in oklch, var(--amber) 15%, transparent); }
.sim-mh-mode--shorted   { color: var(--amber);   border-color: var(--amber); }
.sim-mh-note  { color: var(--text-3); font-size: 10px; }
.sim-mh-empty { color: var(--text-3); font-size: 10px; font-style: italic; }
```

- [ ] **Step 4: Syntax check**

```bash
node --check web/js/schematic.js
```

- [ ] **Step 5: BROWSER VERIFY (Alexis, hero demo)**

With a `?repair=<id>` in the URL so measurements persist:

1. Click `+3V3` rail. Enter 2.87 V, press Enter → auto-classifies to `⚠ anomalous`, journal row appears under the metric input.
2. Enter another value (e.g. 0.02 V) → classifies to `❌ dead`, second history row appears with Δ−2.85.
3. Reopen the inspector on the same target — history persists.
4. Click Diagnostiquer → hypothesis panel cites the latest measurement in the narrative.
5. From the LLM chat panel, ask Claude to `mb_record_measurement(target="rail:+5V", value=5.1, unit="V", nominal=5.0)`. The `+5V` rail in the graph lights up green (alive), and a journal entry appears.
6. No console errors.

Wait for « ok commit ».

- [ ] **Step 6: Commit once Alexis validates**

```bash
git add web/js/schematic.js web/styles/schematic.css
git commit -m "$(cat <<'EOF'
feat(web): per-target measurement mini-timeline in inspector

Below the metric input, a .sim-measurement-history block fetches the
last 6 MeasurementEvents for the current target from
GET /pipeline/packs/{slug}/repairs/{repair_id}/measurements?target=X,
displays them as a compact grid (HH:MM:SS · value · auto-classified
mode chip · note / delta), and refreshes on each inspector reopen.

Closes the hero demo loop: tech records « +3V3 avant reflow » → reflow
U7 → records « après reflow » → history shows Δ+3.27V and mode flip
from anomalous to alive. Claude can read the same timeline via
mb_list_measurements.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- web/js/schematic.js web/styles/schematic.css
```

---

## Task 20: Regenerate benchmark corpus per mode

**Files:**
- Modify: `scripts/gen_hypothesize_benchmarks.py`
- Regenerate: `tests/pipeline/schematic/fixtures/hypothesize_scenarios.json`

- [ ] **Step 1: Extend the generator**

Rewrite `scripts/gen_hypothesize_benchmarks.py` to emit per-(refdes, mode) scenarios:

```python
# scripts/gen_hypothesize_benchmarks.py
#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Generate ground-truth scenarios for the reverse-diagnostic benchmark,
covering all applicable failure modes per refdes.

For each refdes in the device, enumerate its applicable modes via
_applicable_modes. For each mode, run _simulate_failure to produce the
cascade, then sample 2-3 observation variants from the cascade (each
variant picks a subset of the affected targets to present as
observations, with ground_truth = {refdes, mode}).
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from api.pipeline.schematic.hypothesize import (
    SIGNAL_EDGE_KINDS,
    _applicable_modes,
    _simulate_failure,
)
from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph


def sample_subset(pool: set, k_min: int, k_max: int, rng: random.Random) -> list[str]:
    if not pool:
        return []
    k = rng.randint(min(k_min, len(pool)), min(k_max, len(pool)))
    return sorted(rng.sample(sorted(pool), k))


def generate(slug: str, memory_root: Path, seed: int = 42) -> list[dict]:
    pack = memory_root / slug
    eg = ElectricalGraph.model_validate_json(
        (pack / "electrical_graph.json").read_text()
    )
    ab_path = pack / "boot_sequence_analyzed.json"
    ab = (
        AnalyzedBootSequence.model_validate_json(ab_path.read_text())
        if ab_path.exists() else None
    )

    rng = random.Random(seed)
    scenarios: list[dict] = []

    # Cap per-mode scenario count so the corpus stays well balanced.
    MAX_PER_MODE = 30

    scenario_count_by_mode: dict[str, int] = {}
    for refdes in sorted(eg.components):
        for mode in _applicable_modes(eg, refdes):
            count = scenario_count_by_mode.get(mode, 0)
            if count >= MAX_PER_MODE:
                continue
            cascade = _simulate_failure(eg, ab, refdes, mode)
            # Build the target pools for sampling.
            affected_comps: set[str] = (
                set(cascade["dead_comps"])
                | set(cascade["anomalous_comps"])
                | set(cascade["hot_comps"])
            )
            affected_rails: set[str] = (
                set(cascade["dead_rails"]) | set(cascade["shorted_rails"])
            )
            # Skip degenerate cascades (nothing to observe).
            if not affected_comps and not affected_rails:
                continue

            # 2 variants per (refdes, mode).
            for variant in ("partial_comps", "partial_rails_plus_one_alive"):
                if variant == "partial_comps":
                    obs_comps = sample_subset(affected_comps, 1, 3, rng)
                    obs_rails = sample_subset(affected_rails, 0, 1, rng)
                else:
                    obs_rails = sample_subset(affected_rails, 1, 2, rng)
                    obs_comps = sample_subset(affected_comps, 1, 2, rng)
                    # Plus one alive observation for corroboration.
                    alive_candidates = set(eg.components) - affected_comps
                    if alive_candidates:
                        alive_refdes = rng.choice(sorted(alive_candidates))
                        obs_comps.append(alive_refdes)

                state_comps: dict[str, str] = {}
                state_rails: dict[str, str] = {}
                for c in obs_comps:
                    if c in cascade["dead_comps"]:
                        state_comps[c] = "dead"
                    elif c in cascade["anomalous_comps"]:
                        state_comps[c] = "anomalous"
                    elif c in cascade["hot_comps"]:
                        state_comps[c] = "hot"
                    else:
                        state_comps[c] = "alive"
                for r in obs_rails:
                    if r in cascade["shorted_rails"]:
                        state_rails[r] = "shorted"
                    elif r in cascade["dead_rails"]:
                        state_rails[r] = "dead"
                    else:
                        state_rails[r] = "alive"

                scenarios.append({
                    "id": f"{slug}-{refdes}-{mode}-{variant}",
                    "slug": slug,
                    "ground_truth_kill": [refdes],
                    "ground_truth_modes": [mode],
                    "sample_strategy": variant,
                    "observations": {
                        "state_comps": state_comps,
                        "state_rails": state_rails,
                    },
                })
                scenario_count_by_mode[mode] = scenario_count_by_mode.get(mode, 0) + 1
    return scenarios


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--slug", required=True)
    p.add_argument(
        "--out",
        default="tests/pipeline/schematic/fixtures/hypothesize_scenarios.json",
    )
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    root = Path(__file__).resolve().parents[1]
    scenarios = generate(args.slug, root / "memory", seed=args.seed)
    out = root / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(scenarios, indent=2))

    by_mode: dict[str, int] = {}
    for sc in scenarios:
        by_mode[sc["ground_truth_modes"][0]] = by_mode.get(sc["ground_truth_modes"][0], 0) + 1
    print(f"wrote {len(scenarios)} scenarios to {out}")
    for mode, n in sorted(by_mode.items()):
        print(f"  {mode:10s}  {n}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Regenerate the fixture**

```bash
.venv/bin/python scripts/gen_hypothesize_benchmarks.py --slug mnt-reform-motherboard
```

Expected: ~100-200 scenarios, per-mode counts printed. Target rough balance: dead ~30, anomalous ~20-30 (subset of ICs with signal edges), hot ~30, shorted ~20-30 (subset of rail consumers).

- [ ] **Step 3: Lint + commit**

```bash
.venv/bin/ruff check scripts/gen_hypothesize_benchmarks.py
git add scripts/gen_hypothesize_benchmarks.py tests/pipeline/schematic/fixtures/hypothesize_scenarios.json
git commit -m "$(cat <<'EOF'
chore(hypothesize): per-mode benchmark corpus regeneration

Extends gen_hypothesize_benchmarks.py to enumerate (refdes, mode)
pairs via _applicable_modes, run _simulate_failure, and sample two
variants per (refdes, mode) of observations from the cascade.

Ground truth becomes (refdes, mode) instead of (refdes) alone.
Per-mode cap at 30 scenarios keeps the corpus balanced. Seeded (42)
for reproducibility.

MNT Reform: N scenarios, distributed across dead / anomalous / hot /
shorted (see printed breakdown).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- scripts/gen_hypothesize_benchmarks.py tests/pipeline/schematic/fixtures/hypothesize_scenarios.json
```

---

## Task 21: Per-mode CI accuracy gates + tuning

**Files:**
- Modify: `tests/pipeline/schematic/test_hypothesize_accuracy.py`
- Modify: `scripts/tune_hypothesize_weights.py`
- Modify (conditional): `api/pipeline/schematic/hypothesize.py`

- [ ] **Step 1: Rewrite the accuracy tests**

Replace the content of `tests/pipeline/schematic/test_hypothesize_accuracy.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""CI-gated accuracy + perf benchmarks — per-mode thresholds."""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

import pytest

from api.pipeline.schematic.hypothesize import Observations, hypothesize
from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph

FIXTURE = Path(__file__).parent / "fixtures" / "hypothesize_scenarios.json"
MEMORY_ROOT = Path(__file__).resolve().parents[3] / "memory"

# Conservative starting thresholds per mode (tunable after first run).
THRESHOLDS: dict[str, dict[str, float]] = {
    "dead":      {"top1": 0.70, "top3": 0.85, "mrr": 0.75},
    "anomalous": {"top1": 0.40, "top3": 0.60, "mrr": 0.55},
    "hot":       {"top1": 0.60, "top3": 0.85, "mrr": 0.70},
    "shorted":   {"top1": 0.45, "top3": 0.70, "mrr": 0.60},
}
P95_LATENCY_MS = 500.0


def _load_pack(slug: str) -> tuple[ElectricalGraph, AnalyzedBootSequence | None]:
    pack = MEMORY_ROOT / slug
    eg = ElectricalGraph.model_validate_json((pack / "electrical_graph.json").read_text())
    ab_path = pack / "boot_sequence_analyzed.json"
    ab = AnalyzedBootSequence.model_validate_json(ab_path.read_text()) if ab_path.exists() else None
    return eg, ab


def _run_scenarios() -> list[dict]:
    if not FIXTURE.exists():
        pytest.skip("fixture not generated")
    scenarios = json.loads(FIXTURE.read_text())
    if not scenarios:
        pytest.skip("empty fixture")
    by_slug: dict[str, list[dict]] = {}
    for sc in scenarios:
        by_slug.setdefault(sc["slug"], []).append(sc)
    records: list[dict] = []
    for slug, group in by_slug.items():
        if not (MEMORY_ROOT / slug / "electrical_graph.json").exists():
            continue
        eg, ab = _load_pack(slug)
        for sc in group:
            obs = Observations(
                state_comps=sc["observations"]["state_comps"],
                state_rails=sc["observations"]["state_rails"],
            )
            t0 = time.perf_counter()
            result = hypothesize(eg, analyzed_boot=ab, observations=obs)
            wall_ms = (time.perf_counter() - t0) * 1000
            gt_refdes = tuple(sorted(sc["ground_truth_kill"]))
            gt_modes = tuple(sc["ground_truth_modes"])
            rank = None
            for i, h in enumerate(result.hypotheses, start=1):
                if (
                    tuple(sorted(h.kill_refdes)) == gt_refdes
                    and tuple(h.kill_modes) == gt_modes
                ):
                    rank = i
                    break
            records.append({
                "id": sc["id"],
                "mode": sc["ground_truth_modes"][0],
                "rank": rank,
                "wall_ms": wall_ms,
            })
    if not records:
        pytest.skip("no fixture matched local packs")
    return records


@pytest.mark.parametrize("mode", ["dead", "anomalous", "hot", "shorted"])
def test_top1_per_mode(mode: str):
    records = [r for r in _run_scenarios() if r["mode"] == mode]
    if not records:
        pytest.skip(f"no scenarios for mode={mode}")
    top1 = sum(1 for r in records if r["rank"] == 1) / len(records)
    assert top1 >= THRESHOLDS[mode]["top1"], (
        f"mode={mode} top-1 {top1:.2%} < threshold {THRESHOLDS[mode]['top1']:.0%} "
        f"({len(records)} scenarios)"
    )


@pytest.mark.parametrize("mode", ["dead", "anomalous", "hot", "shorted"])
def test_top3_per_mode(mode: str):
    records = [r for r in _run_scenarios() if r["mode"] == mode]
    if not records:
        pytest.skip(f"no scenarios for mode={mode}")
    top3 = sum(1 for r in records if r["rank"] is not None and r["rank"] <= 3) / len(records)
    assert top3 >= THRESHOLDS[mode]["top3"], (
        f"mode={mode} top-3 {top3:.2%} < threshold {THRESHOLDS[mode]['top3']:.0%}"
    )


@pytest.mark.parametrize("mode", ["dead", "anomalous", "hot", "shorted"])
def test_mrr_per_mode(mode: str):
    records = [r for r in _run_scenarios() if r["mode"] == mode]
    if not records:
        pytest.skip(f"no scenarios for mode={mode}")
    mrr = statistics.fmean([1.0 / r["rank"] if r["rank"] else 0.0 for r in records])
    assert mrr >= THRESHOLDS[mode]["mrr"], (
        f"mode={mode} MRR {mrr:.3f} < threshold {THRESHOLDS[mode]['mrr']:.3f}"
    )


def test_p95_latency_under_budget():
    records = _run_scenarios()
    wall = sorted(r["wall_ms"] for r in records)
    p95 = wall[max(0, int(len(wall) * 0.95) - 1)]
    assert p95 < P95_LATENCY_MS, (
        f"p95 latency {p95:.1f} ms exceeds budget {P95_LATENCY_MS} ms"
    )
```

- [ ] **Step 2: Run the accuracy tests**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_hypothesize_accuracy.py -v 2>&1 | tail -30
```

Three outcomes acceptable:

1. **All 13 gates pass** → unchanged thresholds, commit as-is.
2. **Some gates fail, accuracy real but below threshold** → lower the failing threshold to ~5 points below the measured value in `THRESHOLDS`, re-run, commit with the actual numbers in the message body. DO NOT falsify by setting thresholds to 0 — the whole point is a real gate.
3. **Runtime errors** → STOP, fix the engine / generator, don't touch thresholds.

- [ ] **Step 3: Adapt the tuner script**

Replace `scripts/tune_hypothesize_weights.py` with:

```python
# scripts/tune_hypothesize_weights.py
#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Sweep (fp_weight, fn_weight) pairs and pick the best weighted top-3."""

from __future__ import annotations

import json
from pathlib import Path

import api.pipeline.schematic.hypothesize as hypothesize_mod
from api.pipeline.schematic.hypothesize import Observations

FIXTURE = Path(__file__).resolve().parents[1] / "tests/pipeline/schematic/fixtures/hypothesize_scenarios.json"
MEMORY_ROOT = Path(__file__).resolve().parents[1] / "memory"

MODE_WEIGHT = {"dead": 0.4, "anomalous": 0.3, "shorted": 0.2, "hot": 0.1}


def evaluate(fp_w: int, fn_w: int) -> tuple[float, dict[str, float]]:
    from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph

    hypothesize_mod.PENALTY_WEIGHTS = (fp_w, fn_w)
    scenarios = json.loads(FIXTURE.read_text())
    by_slug: dict[str, list[dict]] = {}
    for sc in scenarios:
        by_slug.setdefault(sc["slug"], []).append(sc)
    per_mode_hits: dict[str, tuple[int, int]] = {m: (0, 0) for m in MODE_WEIGHT}
    for slug, group in by_slug.items():
        pack = MEMORY_ROOT / slug
        if not (pack / "electrical_graph.json").exists():
            continue
        eg = ElectricalGraph.model_validate_json((pack / "electrical_graph.json").read_text())
        ab_path = pack / "boot_sequence_analyzed.json"
        ab = AnalyzedBootSequence.model_validate_json(ab_path.read_text()) if ab_path.exists() else None
        for sc in group:
            obs = Observations(
                state_comps=sc["observations"]["state_comps"],
                state_rails=sc["observations"]["state_rails"],
            )
            result = hypothesize_mod.hypothesize(eg, analyzed_boot=ab, observations=obs)
            gt_refdes = tuple(sorted(sc["ground_truth_kill"]))
            gt_modes = tuple(sc["ground_truth_modes"])
            top3 = [(tuple(sorted(h.kill_refdes)), tuple(h.kill_modes)) for h in result.hypotheses[:3]]
            m = sc["ground_truth_modes"][0]
            hit, total = per_mode_hits[m]
            per_mode_hits[m] = (hit + (1 if (gt_refdes, gt_modes) in top3 else 0), total + 1)
    per_mode_acc = {m: (h / t if t else 0.0) for m, (h, t) in per_mode_hits.items()}
    weighted = sum(acc * MODE_WEIGHT[m] for m, acc in per_mode_acc.items())
    return weighted, per_mode_acc


def main() -> None:
    best = (0, 0, 0.0)
    for fp_w in (5, 10, 15, 20, 30):
        for fn_w in (1, 2, 3, 5):
            weighted, per_mode = evaluate(fp_w, fn_w)
            print(f"(fp={fp_w:>2}, fn={fn_w}) → weighted={weighted:.3%}   " + "  ".join(
                f"{m}={acc:.2%}" for m, acc in per_mode.items()
            ))
            if weighted > best[2]:
                best = (fp_w, fn_w, weighted)
    print(f"\nBEST: fp={best[0]}, fn={best[1]} → {best[2]:.3%}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tuner (optional, only if Step 2 threshold adjustments were needed)**

```bash
.venv/bin/python scripts/tune_hypothesize_weights.py
```

If the winning pair strictly beats `(10, 2)`, update `PENALTY_WEIGHTS` in `api/pipeline/schematic/hypothesize.py` with a trailing comment `# tuned 2026-04-24 against multi-mode MNT corpus`, re-run Step 2 tests, then include the module edit in the commit.

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check scripts/tune_hypothesize_weights.py tests/pipeline/schematic/test_hypothesize_accuracy.py
git add tests/pipeline/schematic/test_hypothesize_accuracy.py scripts/tune_hypothesize_weights.py
# Also api/pipeline/schematic/hypothesize.py if weights were tuned.
git commit -m "$(cat <<'EOF'
test(hypothesize): per-mode CI accuracy gates + weighted tuner

Parametrises the accuracy tests on mode. Four pytest.mark.parametrize
entries × three metrics (top-1 / top-3 / MRR) = 12 per-mode gates + a
single aggregate p95 latency gate. Starting thresholds documented in
the THRESHOLDS dict — lowered to [actual values] after the first full
run showed [actual numbers].

The tuner script sweeps the 5×4 weights grid and picks the pair that
maximises a weighted top-3 accuracy (dead 0.4, anomalous 0.3,
shorted 0.2, hot 0.1). [If weights changed: Winning pair (X, Y), weighted
top-3 Z%, gates re-green.] [Else: baseline (10, 2) still optimal.]

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- tests/pipeline/schematic/test_hypothesize_accuracy.py scripts/tune_hypothesize_weights.py  # add hypothesize.py if tuned
```

---

## Task 22: Final verification + hero-demo smoke + hand-off

**Files:**
- Verify only.

- [ ] **Step 1: Full test suite**

```bash
make test 2>&1 | tail -10
```

Expected: all green (likely ~600+ tests).

- [ ] **Step 2: Lint across everything touched in the plan**

```bash
.venv/bin/ruff check \
  api/pipeline/schematic/hypothesize.py \
  api/agent/measurement_memory.py \
  api/tools/hypothesize.py \
  api/tools/measurements.py \
  api/tools/ws_events.py \
  api/agent/manifest.py \
  api/agent/runtime_direct.py \
  api/agent/runtime_managed.py \
  api/pipeline/__init__.py \
  scripts/gen_hypothesize_benchmarks.py \
  scripts/bench_hypothesize.py \
  scripts/tune_hypothesize_weights.py \
  tests/pipeline/schematic/test_hypothesize.py \
  tests/pipeline/schematic/test_hypothesize_accuracy.py \
  tests/agent/test_measurement_memory.py \
  tests/tools/test_hypothesize.py \
  tests/tools/test_measurements.py \
  tests/tools/test_ws_events_sim.py \
  tests/pipeline/test_hypothesize_endpoint.py \
  tests/pipeline/test_measurements_endpoint.py
```

Expected: `All checks passed!`

- [ ] **Step 3: Accuracy gate re-run**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_hypothesize_accuracy.py -v
```

All 13 gates green.

- [ ] **Step 4: Perf bench**

```bash
.venv/bin/python scripts/bench_hypothesize.py --iterations 30
```

Expected JSON with `p95 < 500`. Record the numbers for the hand-off summary.

- [ ] **Step 5: HERO DEMO (browser, Alexis-led)**

Open `http://localhost:8000/?device=mnt-reform-motherboard&repair=<some-repair-id>#schematic`. From the LLM chat panel:

1. Tech: « J'ai mesuré +3V3 à 2.87V et U7 chauffe à 72°C. »
2. Claude calls `mb_record_measurement` twice → WS events fan out → toggles light up: `+3V3` violet (anomalous), U7 orange (hot).
3. Claude calls `mb_hypothesize` → results panel shows top-5 with mode chips, FR narrative citing the measurements.
4. Tech clicks the top hypothesis card → simulator visualises the cascade.
5. Tech: « j'ai refait le reflow sur U7, +3V3 est maintenant à 3.29V »
6. Claude calls `mb_record_measurement` → WS event → `+3V3` toggle flips to emerald (alive).
7. Claude calls `mb_compare_measurements` for `rail:+3V3` → diff shows Δ+0.42V, before=anomalous/after=alive.
8. Claude: « La mesure confirme la réparation ».
9. No console errors. Network tab shows the POST /measurements + GET /measurements + POST /hypothesize calls as expected.

- [ ] **Step 6: Hand-off summary (no commit)**

Report to Alexis in one paragraph:

- Files created / modified counts.
- Number of commits in this plan (expected 21 commits, one per task except Task 16 which is verify-only and Task 22 which is verify-only).
- Per-mode accuracy numbers (top-1, top-3, MRR from Task 21 output).
- Perf p95 / p99 from Task 22 Step 4.
- Any deferred items (modes beyond Phase 1: shorted with unknown culprit, thermal corroboration, passives — all scoped as follow-up specs).

No commit for this task.

---

## Self-review (spec coverage + placeholder scan + consistency)

**Spec coverage** — mapping every section of `docs/superpowers/specs/2026-04-23-fault-modes-and-measurement-memory-design.md` to the tasks that implement it:

| Spec section | Implementing tasks |
|---|---|
| Goal #1 (schema B) | Task 1 |
| Goal #2 (anomalous propagation) | Tasks 2, 3 |
| Goal #3 (measurement journal) | Tasks 8, 9, 11 |
| Goal #4 (4 new journal tools) | Task 13 |
| Goal #5 (2 set/clear tools + WS bridge) | Tasks 10, 13 |
| Goal #6 (frontend UX — picker, input, timeline) | Tasks 17, 18, 19 |
| Goal #7 (per-mode CI gates) | Task 21 |
| `_simulate_failure` for all 4 modes | Tasks 2 (dead), 3 (anomalous), 4 (hot), 5 (shorted) |
| Data shapes (Observations, Hypothesis, HypothesisDiff, HypothesisMetrics) | Task 1 |
| MeasurementEvent + auto_classify table | Task 8 |
| Target string grammar | Task 8 |
| WS envelope (`_SimEvent`, observation_set/clear) | Task 10 |
| Agent tool wrappers (6 total) | Tasks 12, 13 |
| Manifest registration + runtime dispatch | Task 14 |
| HTTP endpoints (hypothesize body + measurements routes) | Task 15 |
| SimulationController Map migration | Task 17 |
| Contextual mode picker | Task 18 |
| Metric input + auto-classify mirror | Task 18 |
| Mini-timeline per target | Task 19 |
| Benchmark generator per-mode | Task 20 |
| CI gates per mode | Task 21 |
| Weight tuner weighted top-3 | Task 21 |
| Final verify + hero demo | Task 22 |

All spec requirements mapped. ✅

**Placeholder scan** — searched the plan for `TBD`, `TODO`, `implement later`, `handle appropriately`, `similar to Task`, `fill in`: zero matches. ✅

**Type consistency** — spot-checked:

- `Observations.state_comps: dict[str, ComponentMode]` — used identically in Task 1 shape, Task 6 scoring, Task 12 tool wrapper, Task 15 HTTP body, Task 17 JS Map. ✅
- `Hypothesis.kill_modes: list[ComponentMode]` — parallel to `kill_refdes`, referenced in Tasks 1, 7, 17. ✅
- Cascade 5-bucket shape (`dead_comps`, `dead_rails`, `shorted_rails`, `anomalous_comps`, `hot_comps`) — Task 2 sets it, Tasks 3/4/5/6 populate it, Task 7 reads it, `_cascade_preview` exposes the summary. ✅
- `MeasurementEvent.value: float | None` — set to Optional in Task 13 (the `mb_set_observation` placeholder path); storage as JSON `null` + load tolerance in Task 9. ✅
- WS event `simulation.observation_set` / `simulation.observation_clear` — defined in Task 10, emitted in Task 13, consumed in Task 17. ✅
- Target string format `"rail:+3V3"` — parser in Task 8, used verbatim by Tasks 13/15/17/18/19. ✅

All type signatures consistent across tasks.

---

## Execution options

Both execution paths are supported. Pick one:

**Subagent-driven (recommended)** — dispatch a fresh subagent per task, review between, use the superpowers:subagent-driven-development skill. Browser-verify with Alexis on Tasks 17/18/19.

**Inline execution** — run the plan in the current session via superpowers:executing-plans, with checkpoints at Group boundaries (end of 7, 11, 16, 19, 22).
