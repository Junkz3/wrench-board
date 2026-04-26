# Passive Component Injection (Phase 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Inject passive components (R/C/FB/D) into the reverse-diagnostic engine by adding `kind`+`role` metadata on `ComponentNode`, shipping a deterministic passive-role classifier, wiring a `(kind, role, mode)` cascade dispatch table in `hypothesize.py`, and adding hand-written scenarios to the CI corpus to mitigate the auto-referential scoring bias of Phase 1.

**Architecture:** Additive schema extension (defaults preserve Phase 1 compatibility). A new `passive_classifier.py` module parallel to `net_classifier.py`. A cascade dispatch table in `hypothesize.py` keyed by `(ComponentKind, role_str, mode_str)` → handler function; handler resolves the passive's affected rail/IC via the graph and returns a 7-field cascade dict. A score visibility multiplier dampens topologically-weak cascades so they don't bloat the top-3. Before the main work, task **T0** refactors the transitive-rails fixpoint from `hypothesize.py` up into `SimulationEngine` (separate commit).

**Tech Stack:** Python 3.11, Pydantic v2 (`extra="forbid"`), FastAPI, pytest + pytest-asyncio, anthropic SDK (optional Opus pass), vanilla JS + D3 (frontend picker). Deterministic hot path, no LLM in the engine.

**Canonical spec:** `docs/superpowers/specs/2026-04-24-passive-component-injection-design.md`.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `api/pipeline/schematic/simulator.py` | modify | **T0** — transitive-rails fixpoint in `_cascade` |
| `api/pipeline/schematic/schemas.py` | modify | `ComponentKind` Literal, `ComponentNode.kind`+`role` fields |
| `api/pipeline/schematic/passive_classifier.py` | **create** | Deterministic heuristic classifier (R/C/D/FB) + optional Opus pass |
| `api/pipeline/schematic/compiler.py` | modify | Invoke classifier, write `kind`/`role` onto components, populate `PowerRail.decoupling` |
| `api/pipeline/schematic/orchestrator.py` | modify | Thread `AsyncAnthropic` client into classifier; persist `passive_classification.json` |
| `api/pipeline/schematic/hypothesize.py` | modify | Mode vocab extension, graph-aware observation validator, `_PASSIVE_CASCADE_TABLE`, handlers, `_applicable_modes` update, `_SCORE_VISIBILITY` multiplier |
| `api/pipeline/schematic/cli.py` | modify | `--classify-passives` re-run switch |
| `api/pipeline/__init__.py` | modify | `GET /pipeline/packs/{slug}/schematic/passives` endpoint |
| `web/js/schematic.js` | modify | `MODE_SETS` dict, kind-aware picker, confidence-tinted rendering |
| `web/styles/schematic.css` | modify | `.sim-mode-picker[data-kind=passive_*]` CSS |
| `tests/pipeline/schematic/test_simulator.py` | modify | T0 transitive-rails fixpoint coverage |
| `tests/pipeline/schematic/test_passive_classifier.py` | **create** | Heuristic rule tests + LLM fallback path |
| `tests/pipeline/schematic/test_hypothesize.py` | modify | Passive cascade handlers, coherence validator, visibility multiplier |
| `tests/pipeline/schematic/test_hypothesize_accuracy.py` | modify | Per-mode CI gates for `open` / `short` |
| `tests/pipeline/schematic/test_hand_written_scenarios.py` | **create** | YAML loader + top-N assertion |
| `tests/pipeline/schematic/fixtures/hand_written_scenarios.yaml` | **create** | 3 initial scenarios (C decoupling short, R feedback open, FB filter open) |
| `tests/pipeline/schematic/fixtures/hypothesize_scenarios.json` | regenerate | Per-mode corpus (~1700 scenarios total) |
| `tests/pipeline/test_schematic_api.py` | modify | `GET /schematic/passives` smoke |
| `scripts/gen_hypothesize_benchmarks.py` | modify | Passive scenario sampling |
| `scripts/bench_hypothesize.py` | modify | Per-mode p95 reporting (open/short) |
| `scripts/tune_hypothesize_weights.py` | modify | Sweep `_SCORE_VISIBILITY` alongside `PENALTY_WEIGHTS` |

**Locked decisions (from the spec):**
- Schema extension is **additive** — `kind: ComponentKind = "ic"`, `role: str | None = None` defaults. Zero migration required for Phase 1 `electrical_graph.json` on disk.
- `ComponentMode = Literal["dead", "alive", "anomalous", "hot", "open", "short"]`. `FailureMode` gets the same two new members.
- `_PASSIVE_CASCADE_TABLE` is an **explicit dict** — grep-friendly, one entry per `(kind, role, mode)` triple. Unmapped triples return empty cascade → pruned.
- Modes that map to `_cascade_passive_alive` are **filtered out of `_applicable_modes`** — they never become candidates.
- `_SCORE_VISIBILITY` applies a `tp_comps` multiplier (rails untouched). FP/FN costs stay at full weight.
- T0 is **isolated** — lands in its own commit before Group A starts.
- No breaking change on `POST /hypothesize` body shape. Only new mode values accepted.
- 6 representative primitives are fully implemented. The ~15 narrower handlers follow the same shape (resolve topology → assemble cascade dict).
- Hand-written scenarios file is authoritative for the "no regression on known field cases" CI gate. At least 3 scenarios, each linked to a specific failure mode.

---

## Phase structure

The 19 tasks cluster into 6 groups. Each group's last task is a strict commit gate.

| Group | Tasks | Goal |
|---|---|---|
| **T0** | 1 | `SimulationEngine` transitive-rails fixpoint refactor (isolated commit) |
| **A — Shape + classifier** | T1–T4 | `ComponentKind`, `ComponentNode` fields, heuristic classifier, compiler wiring |
| **B — Cascade dispatch** | T5–T9 | Mode vocab, graph-aware validator, dispatch table, handlers, scoring multiplier |
| **C — Corpus + CI** | T10–T13 | Hand-written YAML + loader, auto-corpus extension, per-mode gates, weight tuning |
| **D — Frontend** | T14–T16 | Kind-aware picker, confidence-tinted rendering, browser-verify with Alexis |
| **E — LLM enrichment** | T17–T18 | Opus post-pass, HTTP endpoint, CLI switch |

Tasks T14–T16 require **browser-verify with Alexis before commit** (UI changes). All others can enchaîner per the batch-mechanical-tasks memory.

Every commit uses `git commit -- path/to/file1 path/to/file2` explicitly to avoid sweeping the parallel agent's staged files (per CLAUDE.md hard rule).

---

## Task T0: Lift transitive-rails fixpoint into `SimulationEngine`

**Why this ships first:** Phase 4 passive cascades for `short` modes will hit transitive rail failures constantly (a passive shorts rail A → source of rail A dies → rail B whose source was on A is now orphaned). The current patch in `hypothesize.py::_simulate_failure` lines 270–280 works for the single observed case but doesn't compose. Moving it into `SimulationEngine._cascade` solves it once and cleans up `_simulate_failure` later.

**Files:**
- Modify: `api/pipeline/schematic/simulator.py:266-292` (`_cascade` method)
- Modify: `tests/pipeline/schematic/test_simulator.py` (add fixpoint coverage)

- [ ] **Step 1: Read the current `_cascade` implementation**

Run: `sed -n '266,293p' api/pipeline/schematic/simulator.py`
Expected: existing one-pass implementation — dead_rails only contains rails whose source is in `self.killed` (no transitive follow-up).

- [ ] **Step 2: Write the failing test for transitive-rail death**

Append to `tests/pipeline/schematic/test_simulator.py`:

```python
def test_cascade_transitive_dead_rails_via_dead_source():
    """Rail B sourced by a consumer of rail A: if rail A's source dies,
    the consumer never powers on, so rail B is transitively dead too."""
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, NetNode, PagePin, PowerRail,
        SchematicQualityReport, BootPhase,
    )
    from api.pipeline.schematic.simulator import SimulationEngine

    comps = {
        "U1": ComponentNode(
            refdes="U1", type="ic",
            pins=[PagePin(number="1", role="power_in", net_label="VIN")],
        ),
        "U2": ComponentNode(
            refdes="U2", type="ic",
            pins=[
                PagePin(number="1", role="power_in", net_label="RAIL_A"),
                PagePin(number="2", role="power_out", net_label="RAIL_B"),
            ],
        ),
        "U3": ComponentNode(
            refdes="U3", type="ic",
            pins=[PagePin(number="1", role="power_in", net_label="RAIL_B")],
        ),
    }
    nets = {
        "VIN": NetNode(label="VIN", is_power=True),
        "RAIL_A": NetNode(label="RAIL_A", is_power=True),
        "RAIL_B": NetNode(label="RAIL_B", is_power=True),
    }
    rails = {
        "VIN":    PowerRail(label="VIN",    source_refdes=None, consumers=["U1"]),
        "RAIL_A": PowerRail(label="RAIL_A", source_refdes="U1", consumers=["U2"]),
        "RAIL_B": PowerRail(label="RAIL_B", source_refdes="U2", consumers=["U3"]),
    }
    graph = ElectricalGraph(
        device_slug="transitive-test",
        components=comps,
        nets=nets,
        power_rails=rails,
        typed_edges=[],
        boot_sequence=[
            BootPhase(index=1, name="p1", rails_stable=["RAIL_A"], components_entering=["U1", "U2"]),
            BootPhase(index=2, name="p2", rails_stable=["RAIL_B"], components_entering=["U3"]),
        ],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    tl = SimulationEngine(graph, killed_refdes=["U1"]).run()
    # U1 is the kill source → RAIL_A dead. U2 can't power on → RAIL_B also dead
    # transitively. U3 never powers on.
    assert "RAIL_A" in tl.cascade_dead_rails
    assert "RAIL_B" in tl.cascade_dead_rails, (
        "expected RAIL_B in cascade after fixpoint refactor; got %r" % tl.cascade_dead_rails
    )
    assert "U2" in tl.cascade_dead_components
    assert "U3" in tl.cascade_dead_components
```

- [ ] **Step 3: Run the test to confirm it fails**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_simulator.py::test_cascade_transitive_dead_rails_via_dead_source -v`
Expected: FAIL — `RAIL_B not in cascade_dead_rails`. Pre-refactor behavior.

- [ ] **Step 4: Refactor `_cascade` to iterate to fixpoint**

Replace the body of `SimulationEngine._cascade` (lines 266–292) with:

```python
def _cascade(
    self,
    rails: dict[str, RailState],
    components: dict[str, ComponentState],
) -> tuple[list[str], list[str]]:
    """Compute dead components + dead rails, iterating to fixpoint.

    A rail is dead if its source is in the kill set OR if its source is
    itself dead-by-cascade (transitive). A component is dead if it was
    killed directly OR if its `power_in` pin sits on a dead rail. The
    two sets feed each other — a dead component starves rails, a dead
    rail starves components — so we iterate until neither grows.
    """
    dead_components: set[str] = set(self.killed)
    # Seed with components that never powered on during the phase loop.
    for refdes, comp in self.electrical.components.items():
        if refdes in dead_components:
            continue
        if components.get(refdes) == "on":
            continue
        ins = [p.net_label for p in comp.pins if p.role == "power_in" and p.net_label]
        if not ins:
            continue
        if any(
            rails.get(n) != "stable"
            and self.electrical.power_rails.get(n) is not None
            and self.electrical.power_rails[n].source_refdes in self.killed
            for n in ins
        ):
            dead_components.add(refdes)

    dead_rails: set[str] = set()
    # Fixpoint — each pass may unlock more dead rails (sources become dead)
    # which in turn unlock more dead consumers on the next pass.
    for _ in range(len(self.electrical.power_rails) + 1):
        grew = False
        for label, rail in self.electrical.power_rails.items():
            if label in dead_rails:
                continue
            if rails.get(label) == "stable":
                continue
            if rail.source_refdes and rail.source_refdes in dead_components:
                dead_rails.add(label)
                grew = True
        for refdes, comp in self.electrical.components.items():
            if refdes in dead_components:
                continue
            if components.get(refdes) == "on":
                continue
            ins = [p.net_label for p in comp.pins if p.role == "power_in" and p.net_label]
            if not ins:
                continue
            if any(n in dead_rails for n in ins):
                dead_components.add(refdes)
                grew = True
        if not grew:
            break
    return sorted(dead_components), sorted(dead_rails)
```

- [ ] **Step 5: Run the new test**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_simulator.py::test_cascade_transitive_dead_rails_via_dead_source -v`
Expected: PASS.

- [ ] **Step 6: Run the full simulator test suite**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_simulator.py -v`
Expected: ALL PASS. If any pre-existing tests break, the fixpoint is now over-eager; narrow the seed set (line-by-line).

- [ ] **Step 7: Run the full hypothesize test suite**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v`
Expected: ALL PASS — `_simulate_failure("shorted", ...)` in `hypothesize.py` still does its in-place transitive patch (lines 270–280), which is now redundant but still correct.

- [ ] **Step 8: Remove the now-redundant patch in `_simulate_failure("shorted", ...)`**

Edit `api/pipeline/schematic/hypothesize.py`, replace:

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
    # The SimulationEngine only marks rails dead when their source_refdes
    # is in `killed`. For shorted we need a second pass: any rail whose
    # source is itself a dead component (transitively starved) is also dead.
    all_dead_comps: frozenset[str] = downstream["dead_comps"]
    transitive_dead_rails: set[str] = set(downstream["dead_rails"])
    for label, pr in electrical.power_rails.items():
        if label == rail:
            continue  # already in shorted_rails
        if pr.source_refdes and pr.source_refdes in all_dead_comps:
            transitive_dead_rails.add(label)
    c = _empty_cascade()
    # shorted rail tagged separately so scoring matches observed "shorted"
    c["shorted_rails"] = frozenset({rail})
    c["dead_rails"] = frozenset(transitive_dead_rails) - {rail}
    c["dead_comps"] = all_dead_comps
    c["hot_comps"] = frozenset({source}) if source else frozenset()
    c["final_verdict"] = downstream["final_verdict"]
    c["blocked_at_phase"] = downstream["blocked_at_phase"]
    return c
```

with:

```python
if mode == "shorted":
    rail = _find_powered_rail(electrical, refdes)
    if rail is None:
        c = _empty_cascade()
        c["dead_comps"] = frozenset({refdes})
        return c
    source = electrical.power_rails[rail].source_refdes
    downstream = (
        _simulate_dead(electrical, analyzed_boot, [source])
        if source else _empty_cascade()
    )
    # SimulationEngine now handles transitive rail death internally — no
    # second-pass patch needed.
    c = _empty_cascade()
    c["shorted_rails"] = frozenset({rail})
    c["dead_rails"] = downstream["dead_rails"] - {rail}
    c["dead_comps"] = downstream["dead_comps"]
    c["hot_comps"] = frozenset({source}) if source else frozenset()
    c["final_verdict"] = downstream["final_verdict"]
    c["blocked_at_phase"] = downstream["blocked_at_phase"]
    return c
```

- [ ] **Step 9: Run hypothesize suite to confirm the cleanup holds**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py tests/pipeline/schematic/test_hypothesize_accuracy.py -v`
Expected: ALL PASS.

- [ ] **Step 10: Commit T0**

```bash
git commit -m "$(cat <<'EOF'
refactor(simulator): lift transitive-rails fixpoint into SimulationEngine

Phase 4 passive cascades will repeatedly trigger source-kills that
orphan downstream rails. The patch previously lived in
hypothesize._simulate_failure for the "shorted" mode only; moving it
into SimulationEngine._cascade makes it uniformly correct and lets
Phase 4 handlers rely on the primitive.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/simulator.py tests/pipeline/schematic/test_simulator.py api/pipeline/schematic/hypothesize.py
```

---

## Task T1: Extend `ComponentNode` with `kind` + `role`

**Files:**
- Modify: `api/pipeline/schematic/schemas.py` (add `ComponentKind`, extend `ComponentNode`)
- Modify: `tests/pipeline/schematic/test_schemas.py` (new test file — create if it doesn't exist)

- [ ] **Step 1: Check if `test_schemas.py` exists**

Run: `ls tests/pipeline/schematic/test_schemas.py 2>/dev/null || echo MISSING`
Expected: either the path or "MISSING". Create if missing with a one-line docstring.

- [ ] **Step 2: Write the failing tests for the new fields**

Create (or append to) `tests/pipeline/schematic/test_schemas.py`:

```python
"""Tests for schematic schemas — extension coverage for Phase 4 passives."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.pipeline.schematic.schemas import ComponentNode


def test_component_node_defaults_to_ic_kind_and_null_role():
    """Phase 1 data on disk reloads unchanged — default kind="ic"."""
    node = ComponentNode(refdes="U7", type="ic")
    assert node.kind == "ic"
    assert node.role is None


def test_component_node_accepts_passive_kind():
    node = ComponentNode(
        refdes="C156", type="capacitor",
        kind="passive_c", role="decoupling",
    )
    assert node.kind == "passive_c"
    assert node.role == "decoupling"


def test_component_node_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        ComponentNode(refdes="Q5", type="transistor", kind="passive_q")


def test_component_node_role_is_free_form_string():
    """Role follows the PinRole pattern — free-form string, not enum."""
    node = ComponentNode(
        refdes="R42", type="resistor",
        kind="passive_r", role="some_new_role_not_yet_canonical",
    )
    assert node.role == "some_new_role_not_yet_canonical"


def test_component_node_round_trip_preserves_kind_and_role():
    original = ComponentNode(
        refdes="FB2", type="ferrite",
        kind="passive_fb", role="filter",
    )
    restored = ComponentNode.model_validate(original.model_dump())
    assert restored.kind == "passive_fb"
    assert restored.role == "filter"
```

- [ ] **Step 3: Run the tests — all should fail (fields don't exist yet)**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_schemas.py -v`
Expected: 5 failures, all citing either "unexpected keyword argument 'kind'" or default `kind=="ic"` assertions.

- [ ] **Step 4: Extend `ComponentNode` in `schemas.py`**

Edit `api/pipeline/schematic/schemas.py`, find the `ComponentNode` class (~line 376) and add the `ComponentKind` literal + fields. Insert just above the `class ComponentNode`:

```python
ComponentKind = Literal[
    "ic",
    "passive_r",
    "passive_c",
    "passive_d",
    "passive_fb",
]
"""Kind of component in the electrical graph. `ic` is the Phase 1 default
(active components: ICs, modules, transistors, connectors, LEDs, crystals,
oscillators). Passive kinds (`passive_r`, `passive_c`, `passive_d`,
`passive_fb`) are Phase 4 additions and are assigned by the passive role
classifier during `compile_electrical_graph`. `passive_q` reserved for a
future Phase 4.5 and intentionally not included."""
```

Then modify `ComponentNode` to add two fields (keep the existing `extra="forbid"`):

```python
class ComponentNode(BaseModel):
    """A component unified across pages (same refdes = same node)."""

    model_config = ConfigDict(extra="forbid")

    refdes: str
    type: ComponentType
    kind: ComponentKind = "ic"          # Phase 4 addition — defaults to "ic" so
                                         # every Phase 1 electrical_graph.json reloads
                                         # untouched.
    role: str | None = None              # Phase 4 addition — passive role per
                                         # spec 2026-04-24 (§Data shapes). Free-form
                                         # string, canonical values non-enforced.
    value: ComponentValue | None = None
    pages: list[int] = Field(default_factory=list)
    pins: list[PagePin] = Field(default_factory=list)
    populated: bool = True
```

- [ ] **Step 5: Run the tests — all should pass**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_schemas.py -v`
Expected: 5 passes.

- [ ] **Step 6: Run the full schematic test suite to check nothing regressed**

Run: `.venv/bin/pytest tests/pipeline/schematic/ -v`
Expected: ALL PASS. The defaults guarantee backward compatibility.

- [ ] **Step 7: Commit T1**

```bash
git commit -m "$(cat <<'EOF'
feat(schematic): add ComponentKind + ComponentNode.kind/role fields

Additive extension for Phase 4 passive injection. Defaults
(kind="ic", role=None) preserve Phase 1 electrical_graph.json
compatibility and keep every existing test green.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/schemas.py tests/pipeline/schematic/test_schemas.py
```

---

## Task T2: Heuristic classifier — resistors (R)

**Files:**
- Create: `api/pipeline/schematic/passive_classifier.py`
- Create: `tests/pipeline/schematic/test_passive_classifier.py`

- [ ] **Step 1: Write the failing tests for the resistor classifier**

Create `tests/pipeline/schematic/test_passive_classifier.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Tests for the passive role classifier (heuristic + Opus post-pass).

Deterministic path exercised directly. LLM path mocked at
`call_with_forced_tool` — never hits Anthropic in tests.
"""

from __future__ import annotations

import pytest

from api.pipeline.schematic.passive_classifier import (
    classify_passive_refdes,
    classify_passives_heuristic,
)
from api.pipeline.schematic.schemas import (
    ComponentNode, ElectricalGraph, NetNode, PagePin, PowerRail,
    SchematicQualityReport, TypedEdge,
)


def _graph_with_rails(*rail_labels: str) -> ElectricalGraph:
    return ElectricalGraph(
        device_slug="passive-test",
        components={},
        nets={r: NetNode(label=r, is_power=True) for r in rail_labels},
        power_rails={r: PowerRail(label=r) for r in rail_labels},
        typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )


# --------- resistors ---------

def test_resistor_feedback_edge_wins():
    """R with an explicit `feedback_in` typed edge is feedback."""
    graph = _graph_with_rails("+5V")
    r = ComponentNode(
        refdes="R43", type="resistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="+5V"),
            PagePin(number="2", role="unknown", net_label="FB_5V"),
        ],
    )
    graph.components["R43"] = r
    graph.typed_edges.append(TypedEdge(src="FB_5V", dst="R43", kind="feedback_in"))
    kind, role, _conf = classify_passive_refdes(graph, r)
    assert kind == "passive_r"
    assert role == "feedback"


def test_resistor_pull_up_signal_to_rail():
    graph = _graph_with_rails("+3V3")
    graph.nets["I2C_SDA"] = NetNode(label="I2C_SDA")
    r = ComponentNode(
        refdes="R11", type="resistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="+3V3"),
            PagePin(number="2", role="unknown", net_label="I2C_SDA"),
        ],
    )
    graph.components["R11"] = r
    _kind, role, _ = classify_passive_refdes(graph, r)
    assert role == "pull_up"


def test_resistor_series_between_rail_and_consumer():
    graph = _graph_with_rails("VIN", "LPC_VCC")
    # LPC consumer
    graph.components["U7"] = ComponentNode(
        refdes="U7", type="ic",
        pins=[PagePin(number="1", role="power_in", net_label="LPC_VCC")],
    )
    graph.power_rails["LPC_VCC"].consumers = ["U7"]
    r = ComponentNode(
        refdes="R17", type="resistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="VIN"),
            PagePin(number="2", role="unknown", net_label="LPC_VCC"),
        ],
    )
    graph.components["R17"] = r
    _kind, role, _ = classify_passive_refdes(graph, r)
    assert role == "series"


def test_resistor_unclassified_returns_none_role():
    graph = _graph_with_rails()
    graph.nets["SIG_A"] = NetNode(label="SIG_A")
    graph.nets["SIG_B"] = NetNode(label="SIG_B")
    r = ComponentNode(
        refdes="R99", type="resistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="SIG_A"),
            PagePin(number="2", role="unknown", net_label="SIG_B"),
        ],
    )
    graph.components["R99"] = r
    kind, role, _ = classify_passive_refdes(graph, r)
    assert kind == "passive_r"
    assert role == "damping"  # both signals, no rail → damping heuristic


def test_heuristic_classifier_assigns_every_passive():
    """Whole-graph pass emits one assignment per passive refdes."""
    graph = _graph_with_rails("+5V", "+3V3")
    graph.components["R43"] = ComponentNode(
        refdes="R43", type="resistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="+5V"),
            PagePin(number="2", role="unknown", net_label="GND"),
        ],
    )
    graph.components["U1"] = ComponentNode(refdes="U1", type="ic")  # IC, not passive
    result = classify_passives_heuristic(graph)
    # Only the passive is classified.
    assert "R43" in result
    assert result["R43"][0] == "passive_r"
    assert "U1" not in result
```

- [ ] **Step 2: Run the tests — all should fail (module doesn't exist)**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_passive_classifier.py -v`
Expected: collection error or 5 ImportErrors.

- [ ] **Step 3: Create the classifier module skeleton with the R rules**

Create `api/pipeline/schematic/passive_classifier.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Passive role classifier — deterministic heuristic + optional Opus pass.

Same architecture as `net_classifier.py`:
- `classify_passives_heuristic(graph)` — rule-driven, no LLM, always available.
- `classify_passives_llm(graph, client, model)` — optional Opus enrichment.
- `classify_passives(graph, client=None)` — public entry point with graceful
  fallback.

Output shape: `dict[str, tuple[ComponentKind, str | None, float]]` mapping
refdes → (kind, role, confidence). Confidence is 0.6 for heuristic hits,
0.9+ for LLM-confirmed, 0.0 when unclassifiable.

Only passive refdes (R / C / D / FB) are emitted; ICs / connectors / modules
are absent from the result (classifier is a no-op for them).
"""

from __future__ import annotations

import logging

from api.pipeline.schematic.schemas import (
    ComponentKind,
    ComponentNode,
    ElectricalGraph,
)

logger = logging.getLogger("wrench_board.pipeline.schematic.passive_classifier")

# Map schema `ComponentType` → `ComponentKind` for passives we handle.
_TYPE_TO_KIND: dict[str, str] = {
    "resistor":  "passive_r",
    "capacitor": "passive_c",
    "diode":     "passive_d",
    "ferrite":   "passive_fb",
}

_GND_TOKENS = frozenset({"GND", "AGND", "DGND", "PGND", "SGND"})


def _is_ground_net(label: str | None) -> bool:
    if not label:
        return False
    up = label.upper()
    return up in _GND_TOKENS or up.startswith("GND_")


def _is_power_rail(graph: ElectricalGraph, label: str | None) -> bool:
    if not label:
        return False
    return label in graph.power_rails


def _pin_nets(component: ComponentNode) -> list[str]:
    """Return the 2 (or more) net labels attached to this passive's pins."""
    return [p.net_label for p in component.pins if p.net_label]


# ---------------------------------------------------------------------------
# Resistors
# ---------------------------------------------------------------------------

def _classify_resistor(
    graph: ElectricalGraph, comp: ComponentNode,
) -> tuple[str | None, float]:
    """Return (role, confidence) for a resistor. Role is None if
    unclassifiable; the dispatcher downstream silently drops such cases."""
    # Evidence 1 — explicit `feedback_in` typed edge pointing at us.
    for edge in graph.typed_edges:
        if edge.kind == "feedback_in" and edge.dst == comp.refdes:
            return "feedback", 0.85

    nets = _pin_nets(comp)
    if len(nets) < 2:
        return None, 0.0

    n1, n2 = nets[0], nets[1]
    rail1 = _is_power_rail(graph, n1)
    rail2 = _is_power_rail(graph, n2)
    gnd1 = _is_ground_net(n1)
    gnd2 = _is_ground_net(n2)

    # Evidence 2 — pull-up / pull-down.
    if rail1 and not rail2 and not gnd2:
        return "pull_up", 0.65
    if rail2 and not rail1 and not gnd1:
        return "pull_up", 0.65

    # Evidence 3 — pull-down (rail/signal + GND).
    if (rail1 or rail2) and (gnd1 or gnd2):
        # Ambiguous without a value — classify as pull_down with warn-level conf.
        return "pull_down", 0.5

    # Evidence 4 — series: rail on one side, the other pin feeds a consumer
    # of a (possibly different) rail.
    if rail1 or rail2:
        other = n2 if rail1 else n1
        # Any IC's power_in pin sits on `other` → this resistor is in series
        # between two rail domains (typical VIN → regulator_in path).
        for ic in graph.components.values():
            if ic.kind != "ic":
                continue
            for pin in ic.pins:
                if pin.role == "power_in" and pin.net_label == other:
                    return "series", 0.6

    # Evidence 5 — damping (two signals, no rails, no GND).
    if not rail1 and not rail2 and not gnd1 and not gnd2:
        return "damping", 0.4

    return None, 0.0


# ---------------------------------------------------------------------------
# Capacitors / Diodes / Ferrites — stubs filled in T3
# ---------------------------------------------------------------------------

def _classify_capacitor(
    graph: ElectricalGraph, comp: ComponentNode,
) -> tuple[str | None, float]:
    # Filled in T3.
    return None, 0.0


def _classify_diode(
    graph: ElectricalGraph, comp: ComponentNode,
) -> tuple[str | None, float]:
    # Filled in T3.
    return None, 0.0


def _classify_ferrite(
    graph: ElectricalGraph, comp: ComponentNode,
) -> tuple[str | None, float]:
    # Filled in T3.
    return None, 0.0


# ---------------------------------------------------------------------------
# Public dispatchers
# ---------------------------------------------------------------------------

def classify_passive_refdes(
    graph: ElectricalGraph, comp: ComponentNode,
) -> tuple[ComponentKind, str | None, float]:
    """Classify a single component. Returns ("ic", None, 0.0) if not passive."""
    kind = _TYPE_TO_KIND.get(comp.type)
    if kind is None:
        return "ic", None, 0.0
    if comp.type == "resistor":
        role, conf = _classify_resistor(graph, comp)
    elif comp.type == "capacitor":
        role, conf = _classify_capacitor(graph, comp)
    elif comp.type == "diode":
        role, conf = _classify_diode(graph, comp)
    elif comp.type == "ferrite":
        role, conf = _classify_ferrite(graph, comp)
    else:
        role, conf = None, 0.0
    return kind, role, conf


def classify_passives_heuristic(
    graph: ElectricalGraph,
) -> dict[str, tuple[str, str | None, float]]:
    """Whole-graph pass. Emits one entry per passive refdes only."""
    out: dict[str, tuple[str, str | None, float]] = {}
    for refdes, comp in graph.components.items():
        if comp.type not in _TYPE_TO_KIND:
            continue
        kind, role, conf = classify_passive_refdes(graph, comp)
        out[refdes] = (kind, role, conf)
    logger.info(
        "passive_classifier(heuristic): slug=%s classified=%d",
        graph.device_slug, len(out),
    )
    return out
```

- [ ] **Step 4: Run the resistor tests**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_passive_classifier.py -v`
Expected: 5 passes.

- [ ] **Step 5: Commit T2**

```bash
git commit -m "$(cat <<'EOF'
feat(schematic): passive role classifier — resistor heuristics

Heuristic rule set for R: feedback (from feedback_in edge), pull_up
(signal-to-rail), pull_down (rail+GND), series (between two rail
domains via consumer pin), damping (fallback on signal-to-signal).
Capacitor / diode / ferrite handlers land in T3.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/passive_classifier.py tests/pipeline/schematic/test_passive_classifier.py
```

---

## Task T3: Heuristic classifier — capacitors / diodes / ferrites

**Files:**
- Modify: `api/pipeline/schematic/passive_classifier.py` (fill C/D/FB stubs)
- Modify: `tests/pipeline/schematic/test_passive_classifier.py` (add cases)

- [ ] **Step 1: Write the failing tests for C / D / FB**

Append to `tests/pipeline/schematic/test_passive_classifier.py`:

```python
# --------- capacitors ---------

def test_capacitor_decoupling_explicit_edge():
    graph = _graph_with_rails("+3V3")
    graph.nets["GND"] = NetNode(label="GND", is_global=True)
    c = ComponentNode(
        refdes="C156", type="capacitor",
        pins=[
            PagePin(number="1", role="unknown", net_label="+3V3"),
            PagePin(number="2", role="unknown", net_label="GND"),
        ],
    )
    graph.components["C156"] = c
    graph.typed_edges.append(TypedEdge(src="+3V3", dst="C156", kind="decouples"))
    _kind, role, _ = classify_passive_refdes(graph, c)
    assert role == "decoupling"


def test_capacitor_rail_to_gnd_heuristic_decoupling():
    """Without an explicit edge, a rail-to-GND cap still counts as decoupling
    when a consumer IC sits on the same rail."""
    graph = _graph_with_rails("+3V3")
    graph.nets["GND"] = NetNode(label="GND")
    graph.components["U7"] = ComponentNode(
        refdes="U7", type="ic",
        pins=[PagePin(number="1", role="power_in", net_label="+3V3")],
    )
    graph.power_rails["+3V3"].consumers = ["U7"]
    c = ComponentNode(
        refdes="C29", type="capacitor",
        pins=[
            PagePin(number="1", role="unknown", net_label="+3V3"),
            PagePin(number="2", role="unknown", net_label="GND"),
        ],
    )
    graph.components["C29"] = c
    _kind, role, _ = classify_passive_refdes(graph, c)
    assert role == "decoupling"


def test_capacitor_signal_to_signal_is_ac_coupling():
    graph = _graph_with_rails()
    graph.nets["AUDIO_L"] = NetNode(label="AUDIO_L")
    graph.nets["AUDIO_L_AC"] = NetNode(label="AUDIO_L_AC")
    c = ComponentNode(
        refdes="C77", type="capacitor",
        pins=[
            PagePin(number="1", role="unknown", net_label="AUDIO_L"),
            PagePin(number="2", role="unknown", net_label="AUDIO_L_AC"),
        ],
    )
    graph.components["C77"] = c
    _kind, role, _ = classify_passive_refdes(graph, c)
    assert role == "ac_coupling"


# --------- diodes ---------

def test_diode_flyback_edge_wins():
    graph = _graph_with_rails()
    d = ComponentNode(
        refdes="D5", type="diode",
        pins=[
            PagePin(number="1", role="unknown", net_label="SW_NODE"),
            PagePin(number="2", role="unknown", net_label="VBAT"),
        ],
    )
    graph.components["D5"] = d
    # Flyback convention — cathode on the inductor output, anode on return.
    # We detect it via the presence of an inductor across the same nets.
    graph.components["L2"] = ComponentNode(
        refdes="L2", type="inductor",
        pins=[
            PagePin(number="1", role="unknown", net_label="SW_NODE"),
            PagePin(number="2", role="unknown", net_label="VBAT"),
        ],
    )
    _kind, role, _ = classify_passive_refdes(graph, d)
    assert role == "flyback"


def test_diode_signal_to_gnd_is_esd():
    graph = _graph_with_rails()
    graph.nets["USB_DP"] = NetNode(label="USB_DP")
    graph.nets["GND"] = NetNode(label="GND")
    d = ComponentNode(
        refdes="D9", type="diode",
        pins=[
            PagePin(number="1", role="unknown", net_label="USB_DP"),
            PagePin(number="2", role="unknown", net_label="GND"),
        ],
    )
    graph.components["D9"] = d
    _kind, role, _ = classify_passive_refdes(graph, d)
    assert role == "esd"


# --------- ferrites ---------

def test_ferrite_between_rail_and_variant_is_filter():
    graph = _graph_with_rails("+3V3", "+3V3_AUDIO")
    fb = ComponentNode(
        refdes="FB2", type="ferrite",
        pins=[
            PagePin(number="1", role="unknown", net_label="+3V3"),
            PagePin(number="2", role="unknown", net_label="+3V3_AUDIO"),
        ],
    )
    graph.components["FB2"] = fb
    _kind, role, _ = classify_passive_refdes(graph, fb)
    assert role == "filter"
```

- [ ] **Step 2: Run — all new tests should fail (stubs return None)**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_passive_classifier.py -v -k "capacitor or diode or ferrite"`
Expected: 6 FAILs.

- [ ] **Step 3: Fill `_classify_capacitor`**

Replace the `_classify_capacitor` stub in `passive_classifier.py` with:

```python
def _classify_capacitor(
    graph: ElectricalGraph, comp: ComponentNode,
) -> tuple[str | None, float]:
    # Evidence 1 — explicit `decouples` edge pointing at this cap.
    for edge in graph.typed_edges:
        if edge.kind == "decouples" and edge.dst == comp.refdes:
            return "decoupling", 0.85

    nets = _pin_nets(comp)
    if len(nets) < 2:
        return None, 0.0
    n1, n2 = nets[0], nets[1]
    rail1 = _is_power_rail(graph, n1)
    rail2 = _is_power_rail(graph, n2)
    gnd1 = _is_ground_net(n1)
    gnd2 = _is_ground_net(n2)

    # Evidence 2 — rail-to-GND near a consumer IC on the same rail.
    if (rail1 and gnd2) or (rail2 and gnd1):
        rail_label = n1 if rail1 else n2
        rail = graph.power_rails.get(rail_label)
        if rail and rail.consumers:
            # Large-value caps classify as bulk; without value info we default
            # to decoupling. `value.primary` parsing left to the LLM pass.
            return "decoupling", 0.65
        # Rail with no consumers found — fall back to filter.
        return "filter", 0.45

    # Evidence 3 — signal-to-signal (both non-power, non-GND) = AC coupling.
    if not rail1 and not rail2 and not gnd1 and not gnd2:
        return "ac_coupling", 0.55

    return None, 0.0
```

- [ ] **Step 4: Fill `_classify_diode`**

Replace the `_classify_diode` stub with:

```python
def _classify_diode(
    graph: ElectricalGraph, comp: ComponentNode,
) -> tuple[str | None, float]:
    nets = _pin_nets(comp)
    if len(nets) < 2:
        return None, 0.0
    n1, n2 = sorted(nets)  # sort to make the inductor lookup symmetric
    gnd1 = _is_ground_net(n1)
    gnd2 = _is_ground_net(n2)
    rail1 = _is_power_rail(graph, n1)
    rail2 = _is_power_rail(graph, n2)

    # Evidence 1 — flyback: an inductor spans the same two nets.
    my_nets = set(nets)
    for other in graph.components.values():
        if other.refdes == comp.refdes or other.type != "inductor":
            continue
        other_nets = set(_pin_nets(other))
        if my_nets == other_nets:
            return "flyback", 0.75

    # Evidence 2 — signal to GND = ESD clamp.
    if gnd1 or gnd2:
        # One end GND, other end a non-rail net → ESD.
        other = n2 if gnd1 else n1
        if not _is_power_rail(graph, other):
            return "esd", 0.6

    # Evidence 3 — rail to rail = rectifier-ish.
    if rail1 and rail2:
        return "rectifier", 0.5

    return None, 0.0
```

- [ ] **Step 5: Fill `_classify_ferrite`**

Replace the `_classify_ferrite` stub with:

```python
def _classify_ferrite(
    graph: ElectricalGraph, comp: ComponentNode,
) -> tuple[str | None, float]:
    """A ferrite bead's only practical role is `filter` — between a
    rail and a filtered variant of it (`+3V3` → `+3V3_AUDIO`)."""
    nets = _pin_nets(comp)
    if len(nets) < 2:
        return None, 0.0
    n1, n2 = nets[0], nets[1]
    rail1 = _is_power_rail(graph, n1)
    rail2 = _is_power_rail(graph, n2)
    if rail1 and rail2:
        return "filter", 0.85
    # One side rail, other side a net-not-yet-promoted-to-rail is still filter.
    if rail1 or rail2:
        return "filter", 0.65
    return None, 0.0
```

- [ ] **Step 6: Run the full classifier suite**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_passive_classifier.py -v`
Expected: ALL PASS (11 tests total from T2+T3).

- [ ] **Step 7: Commit T3**

```bash
git commit -m "$(cat <<'EOF'
feat(schematic): passive classifier — capacitor/diode/ferrite heuristics

C: decouples-edge / rail-to-GND-with-consumer / signal-to-signal (ac).
D: flyback (inductor spans same nets) / esd (signal-to-GND) / rectifier.
FB: single role `filter` when between two power rails.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/passive_classifier.py tests/pipeline/schematic/test_passive_classifier.py
```

---

## Task T4: Wire classifier into the compiler

**Files:**
- Modify: `api/pipeline/schematic/compiler.py` (call classifier, populate `kind`/`role`/`PowerRail.decoupling`)
- Modify: `tests/pipeline/schematic/test_compiler.py` (add coverage)

- [ ] **Step 1: Check compiler test file exists**

Run: `ls tests/pipeline/schematic/test_compiler.py 2>/dev/null || echo MISSING`
Expected: existing path (Phase 1 already has compiler tests).

- [ ] **Step 2: Write the failing integration test**

Append to `tests/pipeline/schematic/test_compiler.py`:

```python
def test_compile_populates_passive_kind_and_role():
    """After compilation, every passive has kind=passive_* and a role
    (or null) on the ComponentNode."""
    from api.pipeline.schematic.compiler import compile_electrical_graph
    from api.pipeline.schematic.schemas import (
        ComponentNode, NetNode, PagePin, SchematicGraph, TypedEdge,
    )

    graph = SchematicGraph(
        device_slug="compiler-passive-test",
        source_pdf="n/a", page_count=1,
        components={
            "U1": ComponentNode(
                refdes="U1", type="ic",
                pins=[
                    PagePin(number="1", role="power_out", net_label="+3V3"),
                ],
            ),
            "U7": ComponentNode(
                refdes="U7", type="ic",
                pins=[
                    PagePin(number="1", role="power_in", net_label="+3V3"),
                ],
            ),
            "C156": ComponentNode(
                refdes="C156", type="capacitor",
                pins=[
                    PagePin(number="1", role="unknown", net_label="+3V3"),
                    PagePin(number="2", role="unknown", net_label="GND"),
                ],
            ),
        },
        nets={
            "+3V3": NetNode(label="+3V3", is_power=True, is_global=True),
            "GND":  NetNode(label="GND",  is_power=True, is_global=True),
        },
        typed_edges=[
            TypedEdge(src="U1", dst="+3V3", kind="powers"),
        ],
    )
    result = compile_electrical_graph(graph)
    # IC kept as-is
    assert result.components["U1"].kind == "ic"
    assert result.components["U1"].role is None
    # Passive classified
    assert result.components["C156"].kind == "passive_c"
    assert result.components["C156"].role in {"decoupling", "filter"}
    # PowerRail.decoupling populated with the refdes
    assert "C156" in result.power_rails["+3V3"].decoupling
```

- [ ] **Step 3: Run — should fail (compiler doesn't invoke classifier yet)**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_compiler.py::test_compile_populates_passive_kind_and_role -v`
Expected: FAIL — `C156.kind == "ic"` (the default).

- [ ] **Step 4: Integrate the classifier call into `compile_electrical_graph`**

Edit `api/pipeline/schematic/compiler.py`. Add import at top:

```python
from api.pipeline.schematic.passive_classifier import classify_passives_heuristic
```

Find the `return ElectricalGraph(...)` block (near line 66). Before returning, enrich the components dict with classifier output AND populate `PowerRail.decoupling`. Modify the function to:

```python
def compile_electrical_graph(
    graph: SchematicGraph,
    *,
    page_confidences: dict[int, float] | None = None,
) -> ElectricalGraph:
    power_rails = _build_power_rails(graph)
    _augment_consumers_from_pins(power_rails, graph)
    depends_on, ambiguities = _derive_depends_on(graph, power_rails)
    boot_sequence = _build_boot_sequence(graph, power_rails, depends_on)
    quality = _build_quality_report(
        graph=graph,
        ambiguities=ambiguities,
        page_confidences=page_confidences or {},
    )

    # --- Phase 4: passive role classifier ---
    # Run heuristic classifier against the pre-compiled graph + rails.
    # We build a minimal ElectricalGraph view so the classifier can use
    # `power_rails`. Then copy `kind`/`role` onto each passive and
    # populate `PowerRail.decoupling` for decoupling/bulk/filter caps.
    proxy = ElectricalGraph(
        device_slug=graph.device_slug,
        components=graph.components,
        nets=graph.nets,
        power_rails=power_rails,
        typed_edges=graph.typed_edges + depends_on,
        quality=quality,
    )
    assignments = classify_passives_heuristic(proxy)
    enriched = dict(graph.components)
    for refdes, (kind, role, _conf) in assignments.items():
        node = enriched.get(refdes)
        if node is None:
            continue
        enriched[refdes] = node.model_copy(update={"kind": kind, "role": role})
    # Populate PowerRail.decoupling from classifier output (cap-on-rail roles).
    for refdes, (kind, role, _) in assignments.items():
        if kind != "passive_c":
            continue
        if role not in {"decoupling", "bulk", "bypass"}:
            continue
        # Find the rail this cap sits on (any non-GND pin).
        comp = enriched.get(refdes)
        if comp is None:
            continue
        for pin in comp.pins:
            if pin.net_label and pin.net_label in power_rails:
                rail = power_rails[pin.net_label]
                if refdes not in rail.decoupling:
                    rail.decoupling.append(refdes)
                break

    return ElectricalGraph(
        device_slug=graph.device_slug,
        components=enriched,
        nets=graph.nets,
        power_rails=power_rails,
        typed_edges=graph.typed_edges + depends_on,
        boot_sequence=boot_sequence,
        designer_notes=graph.designer_notes,
        ambiguities=ambiguities,
        quality=quality,
        hierarchy=graph.hierarchy,
    )
```

- [ ] **Step 5: Run the new test**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_compiler.py::test_compile_populates_passive_kind_and_role -v`
Expected: PASS.

- [ ] **Step 6: Run the full compiler + classifier suite**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_compiler.py tests/pipeline/schematic/test_passive_classifier.py -v`
Expected: ALL PASS.

- [ ] **Step 7: Regenerate the MNT Reform electrical_graph.json sanity check**

Run: `.venv/bin/python -m api.pipeline.schematic.cli --pdf=board_assets/mnt-reform-motherboard.pdf --slug=mnt-reform-motherboard`
Expected: completes; `memory/mnt-reform-motherboard/electrical_graph.json` now has passive `kind`/`role` on many components. Quick check:
`jq '[.components | to_entries[] | select(.value.kind != "ic")] | length' memory/mnt-reform-motherboard/electrical_graph.json`
Expected: > 500 (passives classified).

*If Alexis is not comfortable re-running the full pipeline yet, skip this step and rely on the synthetic test graphs instead.* The hand-written scenarios in T10 will re-exercise the real graph.

- [ ] **Step 8: Commit T4**

```bash
git commit -m "$(cat <<'EOF'
feat(schematic): compiler populates passive kind/role + PowerRail.decoupling

compile_electrical_graph now runs the heuristic passive classifier at
the end of its pass and annotates every R/C/D/FB ComponentNode with
its classified kind + role. Decoupling / bulk / bypass caps also land
in their rail's `decoupling` list for agent-side lookup.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/compiler.py tests/pipeline/schematic/test_compiler.py
```

---

## Task T5: Extend `ComponentMode` + graph-aware observation validator

**Files:**
- Modify: `api/pipeline/schematic/hypothesize.py` (extend `ComponentMode` / `FailureMode`, add `_validate_obs_against_graph`)
- Modify: `tests/pipeline/schematic/test_hypothesize.py` (add coherence validator tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/pipeline/schematic/test_hypothesize.py`:

```python
def test_hypothesize_rejects_ic_observation_with_passive_mode():
    """state_comps[U7] = "open" is meaningless — U7 is an IC."""
    from api.pipeline.schematic.hypothesize import Observations, hypothesize
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="coh-test",
        components={"U7": ComponentNode(refdes="U7", type="ic", kind="ic")},
        nets={}, power_rails={}, typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    with pytest.raises(ValueError, match="U7.*not a passive mode"):
        hypothesize(graph, observations=Observations(state_comps={"U7": "open"}))


def test_hypothesize_rejects_passive_observation_with_ic_mode():
    from api.pipeline.schematic.hypothesize import Observations, hypothesize
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="coh-test",
        components={
            "C156": ComponentNode(
                refdes="C156", type="capacitor",
                kind="passive_c", role="decoupling",
            ),
        },
        nets={}, power_rails={}, typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    with pytest.raises(ValueError, match="C156.*not a valid IC mode"):
        hypothesize(graph, observations=Observations(state_comps={"C156": "anomalous"}))


def test_hypothesize_accepts_coherent_observations():
    from api.pipeline.schematic.hypothesize import Observations, hypothesize
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="coh-test",
        components={
            "U7":   ComponentNode(refdes="U7", type="ic", kind="ic"),
            "C156": ComponentNode(
                refdes="C156", type="capacitor",
                kind="passive_c", role="decoupling",
            ),
        },
        nets={}, power_rails={}, typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    # Should not raise.
    hypothesize(graph, observations=Observations(
        state_comps={"U7": "dead", "C156": "short"},
    ))
```

- [ ] **Step 2: Run — new tests should fail (no validator yet)**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k "rejects or accepts_coherent"`
Expected: 3 FAILs (either wrong error type or no error raised).

- [ ] **Step 3: Extend the mode Literals in `hypothesize.py`**

Edit `api/pipeline/schematic/hypothesize.py`. Replace the current `ComponentMode` and `FailureMode` lines (around line 41-47) with:

```python
ComponentMode = Literal[
    "dead", "alive", "anomalous", "hot",
    "open", "short",
]
RailMode = Literal["dead", "alive", "shorted"]

# Failure modes that can be attributed to a component as the root-cause kill.
# `alive` is omitted (a live component is not a failure). `shorted` is a rail
# observation but it's produced by a shorted component pulling its input rail
# to GND, so it's a legitimate component-level failure mode in this engine.
# `open` / `short` are the Phase 4 additions for passives.
FailureMode = Literal[
    "dead", "anomalous", "hot", "shorted",
    "open", "short",
]

_IC_MODES: frozenset[str] = frozenset({"dead", "alive", "anomalous", "hot"})
_PASSIVE_MODES: frozenset[str] = frozenset({"open", "short", "alive"})
```

- [ ] **Step 4: Add `_validate_obs_against_graph` helper**

In `hypothesize.py`, add this helper just above the `hypothesize()` function:

```python
def _validate_obs_against_graph(
    electrical: ElectricalGraph, observations: Observations,
) -> None:
    """Cross-check each observation's mode against the target's ComponentKind.

    Raises ValueError with a specific target-and-mode message. The Pydantic
    shape accepts any value in the unified ComponentMode Literal; this
    function is the source of truth for `(kind, mode)` coherence.
    """
    for refdes, mode in observations.state_comps.items():
        comp = electrical.components.get(refdes)
        if comp is None:
            # Unknown refdes — no kind info; allow and let scoring drop it.
            continue
        kind = getattr(comp, "kind", "ic")
        if kind == "ic" and mode not in _IC_MODES:
            raise ValueError(
                f"Observation for {refdes!r} uses {mode!r} — not a valid IC mode "
                f"(expected one of {sorted(_IC_MODES)})."
            )
        if kind != "ic" and mode not in _PASSIVE_MODES:
            raise ValueError(
                f"Observation for {refdes!r} (kind={kind}) uses {mode!r} — "
                f"not a passive mode (expected one of {sorted(_PASSIVE_MODES)})."
            )
```

- [ ] **Step 5: Wire the validator into `hypothesize()`**

Find the `def hypothesize(` entry point (~line 602). Add a single line at the top of the function body, after `t0 = time.perf_counter()`:

```python
    _validate_obs_against_graph(electrical, observations)
```

- [ ] **Step 6: Run the new tests**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k "rejects or accepts_coherent"`
Expected: 3 PASS.

- [ ] **Step 7: Run the full hypothesize suite**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py tests/pipeline/schematic/test_hypothesize_accuracy.py -v`
Expected: ALL PASS. Phase 1 scenarios untouched.

- [ ] **Step 8: Commit T5**

```bash
git commit -m "$(cat <<'EOF'
feat(hypothesize): extend ComponentMode with open/short + coherence validator

ComponentMode and FailureMode both gain open/short. A new graph-aware
validator (_validate_obs_against_graph) cross-checks every observation
mode against the target's ComponentKind — IC targets must use an IC
mode, passive targets must use a passive mode. Raised at hypothesize()
entry, before any candidate enumeration.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
```

---

## Task T6: Cascade table skeleton + primitives (series open, passive_alive, helpers)

**Files:**
- Modify: `api/pipeline/schematic/hypothesize.py` (add table + 6 primitive handlers + topology helpers)
- Modify: `tests/pipeline/schematic/test_hypothesize.py` (add handler unit tests)

- [ ] **Step 1: Write the failing tests for the 6 primitive handlers**

Append to `tests/pipeline/schematic/test_hypothesize.py`:

```python
def _fb_graph():
    """Simple graph: +3V3 → FB2 → LPC_VCC → U7."""
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, NetNode, PagePin, PowerRail,
        SchematicQualityReport,
    )
    return ElectricalGraph(
        device_slug="fb-test",
        components={
            "U1": ComponentNode(refdes="U1", type="ic", pins=[
                PagePin(number="1", role="power_out", net_label="+3V3"),
            ]),
            "FB2": ComponentNode(
                refdes="FB2", type="ferrite",
                kind="passive_fb", role="filter",
                pins=[
                    PagePin(number="1", role="unknown", net_label="+3V3"),
                    PagePin(number="2", role="unknown", net_label="LPC_VCC"),
                ],
            ),
            "U7": ComponentNode(refdes="U7", type="ic", pins=[
                PagePin(number="1", role="power_in", net_label="LPC_VCC"),
            ]),
        },
        nets={
            "+3V3":    NetNode(label="+3V3",    is_power=True),
            "LPC_VCC": NetNode(label="LPC_VCC", is_power=True),
        },
        power_rails={
            "+3V3":    PowerRail(label="+3V3",    source_refdes="U1", consumers=[]),
            "LPC_VCC": PowerRail(label="LPC_VCC", source_refdes=None, consumers=["U7"]),
        },
        typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )


def test_cascade_series_open_kills_downstream_rail():
    """A series R/D/FB open → downstream rail dead."""
    from api.pipeline.schematic.hypothesize import _cascade_series_open
    graph = _fb_graph()
    fb = graph.components["FB2"]
    result = _cascade_series_open(graph, fb)
    assert "LPC_VCC" in result["dead_rails"]
    # U7 is on that rail → dead by starvation.
    assert "U7" in result["dead_comps"]


def test_cascade_passive_alive_returns_empty():
    from api.pipeline.schematic.hypothesize import _cascade_passive_alive
    graph = _fb_graph()
    result = _cascade_passive_alive(graph, graph.components["FB2"])
    assert result["dead_comps"] == frozenset()
    assert result["dead_rails"] == frozenset()
    assert result["shorted_rails"] == frozenset()
    assert result["anomalous_comps"] == frozenset()
    assert result["hot_comps"] == frozenset()


def test_cascade_filter_open_identical_to_series_open():
    """FB filter open → same behavior as a series element open."""
    from api.pipeline.schematic.hypothesize import (
        _cascade_filter_open, _cascade_series_open,
    )
    graph = _fb_graph()
    fb = graph.components["FB2"]
    a = _cascade_filter_open(graph, fb)
    b = _cascade_series_open(graph, fb)
    assert a == b
```

- [ ] **Step 2: Run — 3 FAILs (functions don't exist)**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k "cascade_series_open or passive_alive or filter_open"`
Expected: 3 FAIL / ImportError.

- [ ] **Step 3: Add the handler skeleton to `hypothesize.py`**

Append this section to `api/pipeline/schematic/hypothesize.py`, right before the `# Public entry point` comment block:

```python
# ---------------------------------------------------------------------------
# Phase 4: passive cascade dispatch
# ---------------------------------------------------------------------------


def _find_downstream_rail(
    electrical: ElectricalGraph, passive: "ComponentNode",
) -> str | None:
    """Return the rail sourced on one side of a series passive (R/FB/D/C).

    Heuristic: both pin nets must be power rails. The "downstream" rail
    is the one with a consumer list (fed by nothing else) — the other is
    the upstream source. Ambiguous returns None.
    """
    nets = [p.net_label for p in passive.pins if p.net_label]
    if len(nets) < 2:
        return None
    rail_labels = [n for n in nets if n in electrical.power_rails]
    if len(rail_labels) < 2:
        return None
    # Downstream = the one whose source_refdes is null (no IC drives it)
    # OR whose consumers list is non-empty.
    candidates = []
    for label in rail_labels:
        rail = electrical.power_rails[label]
        # A downstream-of-passive rail typically has source_refdes=None
        # because the passive is the implicit source.
        if rail.source_refdes is None:
            candidates.append(label)
    if len(candidates) == 1:
        return candidates[0]
    # Fall back: pick the rail with more consumers.
    rail_labels.sort(
        key=lambda r: len(electrical.power_rails[r].consumers or []),
        reverse=True,
    )
    return rail_labels[0]


def _find_decoupled_rail(
    electrical: ElectricalGraph, passive: "ComponentNode",
) -> str | None:
    """A decoupling cap has one pin on a rail and one on GND. Return the rail."""
    nets = [p.net_label for p in passive.pins if p.net_label]
    for n in nets:
        if n in electrical.power_rails:
            return n
    return None


def _find_decoupled_ic(
    electrical: ElectricalGraph, passive: "ComponentNode",
) -> str | None:
    """The IC most likely decoupled by this cap — explicit `decouples` edge
    target, or the first consumer IC on the decoupled rail."""
    for edge in electrical.typed_edges:
        if edge.kind == "decouples" and edge.src == passive.refdes:
            if edge.dst in electrical.components:
                return edge.dst
        if edge.kind == "decouples" and edge.dst == passive.refdes:
            if edge.src in electrical.components:
                return edge.src
    rail = _find_decoupled_rail(electrical, passive)
    if rail is None:
        return None
    consumers = electrical.power_rails[rail].consumers or []
    return consumers[0] if consumers else None


def _find_regulated_rail_of_feedback(
    electrical: ElectricalGraph, passive: "ComponentNode",
) -> str | None:
    """Walk a `feedback_in` edge from the divider's signal pin back to the
    regulator that drives the rail being regulated."""
    # Find the non-GND, non-rail net — that's the feedback signal net.
    fb_net: str | None = None
    for pin in passive.pins:
        n = pin.net_label
        if not n:
            continue
        if n in electrical.power_rails:
            continue
        up = n.upper()
        if up in {"GND", "AGND", "DGND", "PGND"}:
            continue
        fb_net = n
        break
    if fb_net is None:
        return None
    # Find the IC with a pin named `feedback_in` on `fb_net`; then find
    # its power_out rail.
    for ic in electrical.components.values():
        if ic.kind != "ic":
            continue
        has_fb = any(p.role == "feedback_in" and p.net_label == fb_net for p in ic.pins)
        if not has_fb:
            continue
        for p in ic.pins:
            if p.role == "power_out" and p.net_label in electrical.power_rails:
                return p.net_label
    return None


def _simulate_rail_loss(
    electrical: ElectricalGraph, rail_label: str,
) -> dict:
    """Mark a rail dead and propagate through SimulationEngine by killing
    its source. If the rail has no source (passive-driven rail), fall
    back to a local cascade: the rail + every consumer of it dead."""
    rail = electrical.power_rails.get(rail_label)
    if rail is None:
        return _empty_cascade()
    if rail.source_refdes:
        return _simulate_dead(electrical, None, [rail.source_refdes])
    # Passive-driven rail — no upstream IC to kill. Build the cascade
    # directly.
    c = _empty_cascade()
    c["dead_rails"] = frozenset({rail_label})
    c["dead_comps"] = frozenset(rail.consumers or [])
    return c


# --- Cascade handlers (one per (kind, role, mode) family) ---

def _cascade_passive_alive(electrical: ElectricalGraph, passive) -> dict:
    """Physically plausible but no observable cascade. Empty → pruned."""
    return _empty_cascade()


def _cascade_series_open(electrical: ElectricalGraph, passive) -> dict:
    downstream = _find_downstream_rail(electrical, passive)
    if downstream is None:
        return _empty_cascade()
    return _simulate_rail_loss(electrical, downstream)


def _cascade_filter_open(electrical: ElectricalGraph, passive) -> dict:
    # FB filter open is functionally identical to a series element open.
    return _cascade_series_open(electrical, passive)


def _cascade_decoupling_open(electrical: ElectricalGraph, passive) -> dict:
    ic = _find_decoupled_ic(electrical, passive)
    if ic is None:
        return _empty_cascade()
    c = _empty_cascade()
    c["anomalous_comps"] = frozenset({ic})
    return c


def _cascade_decoupling_short(electrical: ElectricalGraph, passive) -> dict:
    rail = _find_decoupled_rail(electrical, passive)
    if rail is None:
        return _empty_cascade()
    source = electrical.power_rails[rail].source_refdes
    downstream = _simulate_dead(electrical, None, [source]) if source else _empty_cascade()
    c = _empty_cascade()
    c["shorted_rails"] = frozenset({rail})
    c["dead_rails"] = downstream["dead_rails"] - {rail}
    c["dead_comps"] = downstream["dead_comps"]
    c["hot_comps"] = frozenset({source}) if source else frozenset()
    return c


def _cascade_feedback_open_overvolt(electrical: ElectricalGraph, passive) -> dict:
    rail = _find_regulated_rail_of_feedback(electrical, passive)
    if rail is None:
        return _empty_cascade()
    c = _empty_cascade()
    c["shorted_rails"] = frozenset({rail})  # Phase 1 encoding for overvoltage
    consumers = electrical.power_rails[rail].consumers or []
    c["anomalous_comps"] = frozenset(consumers)
    return c


# The dispatch table is filled in T7 (C), T8 (D/FB). For T6 we register
# just the primitives so the three unit tests pass.
_PASSIVE_CASCADE_TABLE: dict[tuple[str, str, str], "CascadeFn"] = {
    ("passive_r",  "series", "open"):  _cascade_series_open,
    ("passive_fb", "filter", "open"):  _cascade_filter_open,
    # (rest added in T7/T8)
}
```

- [ ] **Step 4: Add the `CascadeFn` type alias (at the top of the module)**

Just below the existing imports in `hypothesize.py`, add:

```python
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from api.pipeline.schematic.schemas import ComponentNode as _CompNode

CascadeFn = Callable[[ElectricalGraph, "_CompNode"], dict]
```

(The `TYPE_CHECKING` guard avoids a runtime circular import since `schemas.py` is imported already but keeps the type readable.)

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k "cascade_series_open or passive_alive or filter_open"`
Expected: 3 PASS.

- [ ] **Step 6: Run the full hypothesize suite**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v`
Expected: ALL PASS — nothing depending on `_PASSIVE_CASCADE_TABLE` yet.

- [ ] **Step 7: Commit T6**

```bash
git commit -m "$(cat <<'EOF'
feat(hypothesize): passive cascade primitives + topology helpers

Introduce the _PASSIVE_CASCADE_TABLE skeleton and the 6 reusable
cascade primitives (series_open, filter_open, decoupling_open,
decoupling_short, feedback_open_overvolt, passive_alive). Helpers
for topology lookup (downstream rail, decoupled rail, decoupled IC,
feedback regulator). Further handlers (C/D/FB specifics) land in
T7 and T8.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
```

---

## Task T7: Cascade handlers — capacitor + resistor full table

**Files:**
- Modify: `api/pipeline/schematic/hypothesize.py` (fill capacitor/resistor table entries and narrow handlers)
- Modify: `tests/pipeline/schematic/test_hypothesize.py` (add coverage)

- [ ] **Step 1: Write the failing tests for new handlers + table completeness**

Append to `tests/pipeline/schematic/test_hypothesize.py`:

```python
def _mnt_like_graph():
    """A graph with: +3V3 source U1, decoupling C156 on U7 VCC, pull-up R11
    on I2C_SDA, feedback divider R43 on +5V regulator U3."""
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, NetNode, PagePin, PowerRail,
        SchematicQualityReport, TypedEdge,
    )
    return ElectricalGraph(
        device_slug="mnt-like",
        components={
            "U1": ComponentNode(refdes="U1", type="ic", pins=[
                PagePin(number="1", role="power_out", net_label="+3V3"),
            ]),
            "U7": ComponentNode(refdes="U7", type="ic", pins=[
                PagePin(number="1", role="power_in", net_label="+3V3"),
            ]),
            "U3": ComponentNode(refdes="U3", type="ic", pins=[
                PagePin(number="1", role="feedback_in", net_label="FB_5V"),
                PagePin(number="2", role="power_out", net_label="+5V"),
            ]),
            "C156": ComponentNode(
                refdes="C156", type="capacitor",
                kind="passive_c", role="decoupling",
                pins=[
                    PagePin(number="1", role="unknown", net_label="+3V3"),
                    PagePin(number="2", role="unknown", net_label="GND"),
                ],
            ),
            "R43": ComponentNode(
                refdes="R43", type="resistor",
                kind="passive_r", role="feedback",
                pins=[
                    PagePin(number="1", role="unknown", net_label="+5V"),
                    PagePin(number="2", role="unknown", net_label="FB_5V"),
                ],
            ),
            "R11": ComponentNode(
                refdes="R11", type="resistor",
                kind="passive_r", role="pull_up",
                pins=[
                    PagePin(number="1", role="unknown", net_label="+3V3"),
                    PagePin(number="2", role="unknown", net_label="I2C_SDA"),
                ],
            ),
            "U9": ComponentNode(refdes="U9", type="ic", pins=[
                PagePin(number="1", role="bus_pin", net_label="I2C_SDA"),
            ]),
        },
        nets={
            "+3V3":    NetNode(label="+3V3", is_power=True),
            "+5V":     NetNode(label="+5V",  is_power=True),
            "FB_5V":   NetNode(label="FB_5V"),
            "I2C_SDA": NetNode(label="I2C_SDA"),
            "GND":     NetNode(label="GND", is_global=True),
        },
        power_rails={
            "+3V3": PowerRail(label="+3V3", source_refdes="U1", consumers=["U7"]),
            "+5V":  PowerRail(label="+5V",  source_refdes="U3", consumers=[]),
        },
        typed_edges=[
            TypedEdge(src="U7", dst="+3V3", kind="powers"),
            TypedEdge(src="C156", dst="+3V3", kind="decouples"),
            TypedEdge(src="FB_5V", dst="R43", kind="feedback_in"),
            TypedEdge(src="U9", dst="I2C_SDA", kind="consumes_signal"),
        ],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )


def test_cascade_decoupling_short_kills_rail():
    from api.pipeline.schematic.hypothesize import _cascade_decoupling_short
    graph = _mnt_like_graph()
    c = _cascade_decoupling_short(graph, graph.components["C156"])
    assert "+3V3" in c["shorted_rails"]
    assert "U1" in c["hot_comps"]
    assert "U7" in c["dead_comps"]


def test_cascade_decoupling_open_marks_upstream_ic_anomalous():
    from api.pipeline.schematic.hypothesize import _cascade_decoupling_open
    graph = _mnt_like_graph()
    c = _cascade_decoupling_open(graph, graph.components["C156"])
    assert c["anomalous_comps"] == frozenset({"U7"})


def test_cascade_feedback_open_triggers_overvoltage():
    from api.pipeline.schematic.hypothesize import _cascade_feedback_open_overvolt
    graph = _mnt_like_graph()
    c = _cascade_feedback_open_overvolt(graph, graph.components["R43"])
    assert "+5V" in c["shorted_rails"]


def test_cascade_pull_up_open_marks_signal_consumers_anomalous():
    from api.pipeline.schematic.hypothesize import _cascade_pull_up_open
    graph = _mnt_like_graph()
    c = _cascade_pull_up_open(graph, graph.components["R11"])
    assert "U9" in c["anomalous_comps"]


def test_table_covers_all_resistor_and_capacitor_roles():
    from api.pipeline.schematic.hypothesize import _PASSIVE_CASCADE_TABLE
    # After T7, the table has all R + C entries.
    for r_role in ("series", "feedback", "pull_up", "pull_down"):
        for mode in ("open", "short"):
            assert ("passive_r", r_role, mode) in _PASSIVE_CASCADE_TABLE, (
                f"missing handler for passive_r/{r_role}/{mode}"
            )
    for c_role in ("decoupling", "bulk", "filter", "ac_coupling", "tank", "bypass"):
        for mode in ("open", "short"):
            assert ("passive_c", c_role, mode) in _PASSIVE_CASCADE_TABLE, (
                f"missing handler for passive_c/{c_role}/{mode}"
            )
```

- [ ] **Step 2: Run — expected many FAILs (handlers + table entries missing)**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k "decoupling or feedback_open or pull_up_open or table_covers"`
Expected: 5 FAILs / ImportErrors.

- [ ] **Step 3: Add the narrow handlers to `hypothesize.py`**

Insert the following handlers in `hypothesize.py` just after the ones added in T6:

```python
def _cascade_feedback_short_undervolt(electrical: ElectricalGraph, passive) -> dict:
    """R feedback short → divider collapses → regulator shuts output → rail dead."""
    rail = _find_regulated_rail_of_feedback(electrical, passive)
    if rail is None:
        return _empty_cascade()
    return _simulate_rail_loss(electrical, rail)


def _cascade_pull_up_open(electrical: ElectricalGraph, passive) -> dict:
    """Pull-up/pull-down open → signal floats → consumers anomalous."""
    # Identify the signal net (the non-rail, non-GND pin).
    sig_net: str | None = None
    for pin in passive.pins:
        n = pin.net_label
        if not n or n in electrical.power_rails:
            continue
        up = n.upper()
        if up in {"GND", "AGND", "DGND", "PGND"} or up.startswith("GND_"):
            continue
        sig_net = n
        break
    if sig_net is None:
        return _empty_cascade()
    anomalous: set[str] = set()
    for edge in electrical.typed_edges:
        if edge.kind in {"consumes_signal", "depends_on"} and edge.dst == sig_net:
            if edge.src in electrical.components:
                anomalous.add(edge.src)
    c = _empty_cascade()
    c["anomalous_comps"] = frozenset(anomalous)
    return c


def _cascade_pull_up_short(electrical: ElectricalGraph, passive) -> dict:
    """Pull-up short → rail shorted to signal (or bus stuck) → rail dead.
    Using rail-loss primitive on the rail-side pin."""
    for pin in passive.pins:
        n = pin.net_label
        if n and n in electrical.power_rails:
            return _simulate_rail_loss(electrical, n)
    return _empty_cascade()


def _cascade_filter_cap_open(electrical: ElectricalGraph, passive) -> dict:
    """Filter cap open on a regulated rail → ripple → upstream IC anomalous.
    Same topological signature as decoupling_open."""
    return _cascade_decoupling_open(electrical, passive)


def _cascade_signal_path_open(electrical: ElectricalGraph, passive) -> dict:
    """AC-coupling cap open → signal broken downstream of the cap → consumers
    of the output net anomalous."""
    nets = [p.net_label for p in passive.pins if p.net_label]
    if len(nets) < 2:
        return _empty_cascade()
    # Pick the net with the most downstream consumers as the "output" side.
    consumer_counts = {
        n: sum(
            1 for e in electrical.typed_edges
            if e.kind in {"consumes_signal", "depends_on"} and e.dst == n
        )
        for n in nets
    }
    output = max(nets, key=lambda n: consumer_counts[n])
    c = _empty_cascade()
    consumers: set[str] = set()
    for e in electrical.typed_edges:
        if e.kind in {"consumes_signal", "depends_on"} and e.dst == output:
            if e.src in electrical.components:
                consumers.add(e.src)
    c["anomalous_comps"] = frozenset(consumers)
    return c


def _cascade_signal_path_dc(electrical: ElectricalGraph, passive) -> dict:
    """AC-coupling cap short → DC offset propagates → downstream anomalous."""
    return _cascade_signal_path_open(electrical, passive)


def _cascade_tank_open(electrical: ElectricalGraph, passive) -> dict:
    """Tank cap open near oscillator → clock dead → clock consumers anomalous.
    Tank has 1 pin on GND, 1 on the oscillator output. Treat oscillator as
    anomalous."""
    for pin in passive.pins:
        n = pin.net_label
        if not n:
            continue
        # Find an IC (oscillator) with clock_out on this net.
        for ic in electrical.components.values():
            if ic.kind != "ic":
                continue
            for p in ic.pins:
                if p.role == "clock_out" and p.net_label == n:
                    c = _empty_cascade()
                    c["anomalous_comps"] = frozenset({ic.refdes})
                    return c
    return _empty_cascade()


def _cascade_tank_short(electrical: ElectricalGraph, passive) -> dict:
    """Tank cap short → oscillator dead."""
    # Same lookup as tank_open but tag oscillator dead instead of anomalous.
    for pin in passive.pins:
        n = pin.net_label
        if not n:
            continue
        for ic in electrical.components.values():
            if ic.kind != "ic":
                continue
            for p in ic.pins:
                if p.role == "clock_out" and p.net_label == n:
                    return _simulate_dead(electrical, None, [ic.refdes])
    return _empty_cascade()
```

- [ ] **Step 4: Fill the `_PASSIVE_CASCADE_TABLE` with R + C entries**

Replace the current 2-entry `_PASSIVE_CASCADE_TABLE` with:

```python
_PASSIVE_CASCADE_TABLE: dict[tuple[str, str, str], CascadeFn] = {
    # ========================= RESISTORS =========================
    ("passive_r", "series",       "open"):  _cascade_series_open,
    ("passive_r", "series",       "short"): _cascade_passive_alive,
    ("passive_r", "feedback",     "open"):  _cascade_feedback_open_overvolt,
    ("passive_r", "feedback",     "short"): _cascade_feedback_short_undervolt,
    ("passive_r", "pull_up",      "open"):  _cascade_pull_up_open,
    ("passive_r", "pull_up",      "short"): _cascade_pull_up_short,
    ("passive_r", "pull_down",    "open"):  _cascade_pull_up_open,
    ("passive_r", "pull_down",    "short"): _cascade_passive_alive,
    ("passive_r", "current_sense","open"):  _cascade_series_open,
    ("passive_r", "current_sense","short"): _cascade_passive_alive,
    ("passive_r", "damping",      "open"):  _cascade_passive_alive,
    ("passive_r", "damping",      "short"): _cascade_passive_alive,

    # ========================= CAPACITORS ========================
    ("passive_c", "decoupling",  "open"):  _cascade_decoupling_open,
    ("passive_c", "decoupling",  "short"): _cascade_decoupling_short,
    ("passive_c", "bulk",        "open"):  _cascade_decoupling_open,
    ("passive_c", "bulk",        "short"): _cascade_decoupling_short,
    ("passive_c", "filter",      "open"):  _cascade_filter_cap_open,
    ("passive_c", "filter",      "short"): _cascade_decoupling_short,
    ("passive_c", "ac_coupling", "open"):  _cascade_signal_path_open,
    ("passive_c", "ac_coupling", "short"): _cascade_signal_path_dc,
    ("passive_c", "tank",        "open"):  _cascade_tank_open,
    ("passive_c", "tank",        "short"): _cascade_tank_short,
    ("passive_c", "bypass",      "open"):  _cascade_decoupling_open,
    ("passive_c", "bypass",      "short"): _cascade_decoupling_short,

    # (ferrite + diode entries added in T8)
    ("passive_fb", "filter", "open"):  _cascade_filter_open,
    ("passive_fb", "filter", "short"): _cascade_passive_alive,
}
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k "decoupling or feedback_open or pull_up_open or table_covers"`
Expected: 5 PASS.

- [ ] **Step 6: Run the full hypothesize suite**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v`
Expected: ALL PASS.

- [ ] **Step 7: Commit T7**

```bash
git commit -m "$(cat <<'EOF'
feat(hypothesize): passive cascade table — R + C entries

All 12 R entries (series/feedback/pull_up/pull_down/current_sense/damping,
each × open/short) and all 12 C entries (decoupling/bulk/filter/
ac_coupling/tank/bypass, each × open/short) wired to the dispatch
table. Narrow handlers for feedback_short, pull_up_open/short,
signal_path_open/dc, tank_open/short added.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
```

---

## Task T8: Cascade handlers — diodes + remaining entries

**Files:**
- Modify: `api/pipeline/schematic/hypothesize.py` (diode handlers + full table entries)
- Modify: `tests/pipeline/schematic/test_hypothesize.py` (diode coverage + table completeness)

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_cascade_rectifier_short_shorts_input_rail():
    from api.pipeline.schematic.hypothesize import _cascade_rectifier_short
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, NetNode, PagePin, PowerRail,
        SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="rect-test",
        components={
            "D1": ComponentNode(
                refdes="D1", type="diode",
                kind="passive_d", role="rectifier",
                pins=[
                    PagePin(number="1", role="unknown", net_label="VIN"),
                    PagePin(number="2", role="unknown", net_label="VOUT"),
                ],
            ),
        },
        nets={"VIN": NetNode(label="VIN", is_power=True),
              "VOUT": NetNode(label="VOUT", is_power=True)},
        power_rails={"VIN":  PowerRail(label="VIN"),
                     "VOUT": PowerRail(label="VOUT")},
        typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    c = _cascade_rectifier_short(graph, graph.components["D1"])
    # Either VIN or VOUT becomes shorted — implementation defines the
    # direction. Accept either.
    assert len(c["shorted_rails"]) == 1


def test_table_covers_every_diode_role():
    from api.pipeline.schematic.hypothesize import _PASSIVE_CASCADE_TABLE
    for role in ("flyback", "rectifier", "esd", "reverse_protection", "signal_clamp"):
        for mode in ("open", "short"):
            assert ("passive_d", role, mode) in _PASSIVE_CASCADE_TABLE


def test_table_every_entry_is_callable():
    from api.pipeline.schematic.hypothesize import _PASSIVE_CASCADE_TABLE
    for key, fn in _PASSIVE_CASCADE_TABLE.items():
        assert callable(fn), f"non-callable handler at {key}"
```

- [ ] **Step 2: Run — should fail**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k "rectifier_short or every_diode_role or every_entry"`
Expected: 3 FAILs.

- [ ] **Step 3: Add diode-specific handlers**

Append to `hypothesize.py`:

```python
def _cascade_rectifier_short(electrical: ElectricalGraph, passive) -> dict:
    """Shorted rectifier → its upstream rail shorted (input pulled to output)."""
    nets = [p.net_label for p in passive.pins if p.net_label]
    rails = [n for n in nets if n in electrical.power_rails]
    if not rails:
        return _empty_cascade()
    # Pick the input-side rail — heuristic: the one with a source_refdes.
    rails_with_source = [
        r for r in rails
        if electrical.power_rails[r].source_refdes is not None
    ]
    target = rails_with_source[0] if rails_with_source else rails[0]
    source = electrical.power_rails[target].source_refdes
    downstream = _simulate_dead(electrical, None, [source]) if source else _empty_cascade()
    c = _empty_cascade()
    c["shorted_rails"] = frozenset({target})
    c["dead_rails"] = downstream["dead_rails"] - {target}
    c["dead_comps"] = downstream["dead_comps"]
    c["hot_comps"] = frozenset({source}) if source else frozenset()
    return c


def _cascade_rectifier_open(electrical: ElectricalGraph, passive) -> dict:
    """Open rectifier → output rail dead."""
    nets = [p.net_label for p in passive.pins if p.net_label]
    rails = [n for n in nets if n in electrical.power_rails]
    if not rails:
        return _empty_cascade()
    # Pick the output-side rail (no source_refdes — the diode is the source).
    rails_without_source = [
        r for r in rails if electrical.power_rails[r].source_refdes is None
    ]
    target = rails_without_source[0] if rails_without_source else rails[0]
    return _simulate_rail_loss(electrical, target)


def _cascade_flyback_open(electrical: ElectricalGraph, passive) -> dict:
    """Flyback diode open → inductor kickback damages downstream → anomalous."""
    nets = set(p.net_label for p in passive.pins if p.net_label)
    consumers: set[str] = set()
    for ic in electrical.components.values():
        if ic.kind != "ic":
            continue
        for p in ic.pins:
            if p.role == "power_in" and p.net_label in nets:
                consumers.add(ic.refdes)
    c = _empty_cascade()
    c["anomalous_comps"] = frozenset(consumers)
    return c


def _cascade_flyback_short(electrical: ElectricalGraph, passive) -> dict:
    """Flyback short → continuous current path → source hot + rail shorted."""
    nets = [p.net_label for p in passive.pins if p.net_label]
    rails = [n for n in nets if n in electrical.power_rails]
    if not rails:
        return _empty_cascade()
    target = rails[0]
    source = electrical.power_rails[target].source_refdes
    downstream = _simulate_dead(electrical, None, [source]) if source else _empty_cascade()
    c = _empty_cascade()
    c["shorted_rails"] = frozenset({target})
    c["dead_rails"] = downstream["dead_rails"] - {target}
    c["dead_comps"] = downstream["dead_comps"]
    c["hot_comps"] = frozenset({source}) if source else frozenset()
    return c


def _cascade_signal_to_ground(electrical: ElectricalGraph, passive) -> dict:
    """ESD clamp short / signal clamp short → signal stuck → consumers anomalous.
    Uses the signal-net side (non-GND pin)."""
    sig_net: str | None = None
    for pin in passive.pins:
        n = pin.net_label
        if not n:
            continue
        if n in electrical.power_rails:
            continue
        up = n.upper()
        if up in {"GND", "AGND", "DGND", "PGND"} or up.startswith("GND_"):
            continue
        sig_net = n
        break
    if sig_net is None:
        return _empty_cascade()
    consumers: set[str] = set()
    for edge in electrical.typed_edges:
        if edge.kind in {"consumes_signal", "depends_on"} and edge.dst == sig_net:
            if edge.src in electrical.components:
                consumers.add(edge.src)
    c = _empty_cascade()
    c["anomalous_comps"] = frozenset(consumers)
    return c
```

- [ ] **Step 4: Extend the `_PASSIVE_CASCADE_TABLE` with D entries**

Extend the table to also contain:

```python
    # ========================= DIODES ===========================
    ("passive_d", "flyback",           "open"):  _cascade_flyback_open,
    ("passive_d", "flyback",           "short"): _cascade_flyback_short,
    ("passive_d", "rectifier",         "open"):  _cascade_rectifier_open,
    ("passive_d", "rectifier",         "short"): _cascade_rectifier_short,
    ("passive_d", "esd",               "open"):  _cascade_passive_alive,
    ("passive_d", "esd",               "short"): _cascade_signal_to_ground,
    ("passive_d", "reverse_protection","open"):  _cascade_series_open,
    ("passive_d", "reverse_protection","short"): _cascade_passive_alive,
    ("passive_d", "signal_clamp",      "open"):  _cascade_passive_alive,
    ("passive_d", "signal_clamp",      "short"): _cascade_signal_to_ground,
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k "rectifier_short or every_diode_role or every_entry"`
Expected: 3 PASS.

- [ ] **Step 6: Commit T8**

```bash
git commit -m "$(cat <<'EOF'
feat(hypothesize): passive cascade table — diode entries + complete coverage

All 10 D entries wired (flyback/rectifier/esd/reverse_protection/
signal_clamp × open/short). Dispatch table now covers every
(passive_kind, role, mode) combination from the spec — 34 entries
total (12 R, 12 C, 10 D, 2 FB).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
```

---

## Task T9: `_applicable_modes` + scoring visibility multiplier

**Files:**
- Modify: `api/pipeline/schematic/hypothesize.py` (update `_applicable_modes`, add `_SCORE_VISIBILITY`, update `_score_candidate`, wire into `_simulate_failure`)
- Modify: `tests/pipeline/schematic/test_hypothesize.py` (coverage)

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_applicable_modes_ic_unchanged():
    from api.pipeline.schematic.hypothesize import _applicable_modes
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="am-test",
        components={"U1": ComponentNode(refdes="U1", type="ic")},
        nets={}, power_rails={}, typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    modes = _applicable_modes(graph, "U1")
    assert "dead" in modes
    assert "hot" in modes
    assert "open" not in modes
    assert "short" not in modes


def test_applicable_modes_passive_with_role_returns_open_short():
    from api.pipeline.schematic.hypothesize import _applicable_modes
    graph = _mnt_like_graph()
    modes = _applicable_modes(graph, "C156")  # decoupling
    assert "open" in modes
    assert "short" in modes
    assert "dead" not in modes


def test_applicable_modes_passive_without_role_returns_empty():
    from api.pipeline.schematic.hypothesize import _applicable_modes
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="unrole",
        components={
            "R99": ComponentNode(
                refdes="R99", type="resistor",
                kind="passive_r", role=None,
            ),
        },
        nets={}, power_rails={}, typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    modes = _applicable_modes(graph, "R99")
    assert modes == []


def test_applicable_modes_skips_passive_alive_entries():
    """When a (kind, role, mode) maps to `_cascade_passive_alive`, the mode
    is not enumerated — no observable cascade."""
    from api.pipeline.schematic.hypothesize import _applicable_modes
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="alive-test",
        components={
            "R50": ComponentNode(
                refdes="R50", type="resistor",
                kind="passive_r", role="damping",
            ),
        },
        nets={}, power_rails={}, typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    # damping open AND short both map to `_cascade_passive_alive` → no modes.
    assert _applicable_modes(graph, "R50") == []


def test_score_visibility_multiplier_dampens_decoupling_open():
    """A decoupling-open hypothesis that matches 1 anomalous IC should score
    tp_comps = 0.5, not 1.0."""
    from api.pipeline.schematic.hypothesize import (
        Observations, hypothesize,
    )
    graph = _mnt_like_graph()
    result = hypothesize(
        graph,
        observations=Observations(state_comps={"U7": "anomalous"}),
    )
    # Look for a C156-open hypothesis with visibility applied.
    c156_hyps = [h for h in result.hypotheses
                 if h.kill_refdes == ["C156"] and h.kill_modes == ["open"]]
    if c156_hyps:
        h = c156_hyps[0]
        # TP is 1 component (U7), but score reflects 0.5 × tp = 0.5.
        assert 0.3 <= h.score <= 0.6, (
            "expected dampened score ~0.5 for decoupling_open, got %s" % h.score
        )
```

- [ ] **Step 2: Run — 5 FAILs expected**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k "applicable_modes or score_visibility"`
Expected: 5 FAIL.

- [ ] **Step 3: Rewrite `_applicable_modes`**

In `hypothesize.py`, replace the existing `_applicable_modes` (around line 473) with:

```python
def _applicable_modes(
    electrical: ElectricalGraph, refdes: str,
) -> list[str]:
    """Return the list of modes worth simulating for a given refdes.

    - ICs: `dead`, `hot` always; `anomalous` when the IC has an outgoing
      signal edge; `shorted` when the IC is a rail consumer.
    - Passives with a known role: `open` and/or `short` when the dispatch
      table has a non-alive handler for the (kind, role, mode) triple.
    - Passives without a role: no applicable mode (returns [])."""
    comp = electrical.components.get(refdes)
    if comp is None:
        return []
    kind = getattr(comp, "kind", "ic")
    role = getattr(comp, "role", None)

    if kind == "ic":
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

    # Passive.
    if role is None:
        return []
    applicable: list[str] = []
    for mode in ("open", "short"):
        handler = _PASSIVE_CASCADE_TABLE.get((kind, role, mode))
        if handler is not None and handler is not _cascade_passive_alive:
            applicable.append(mode)
    return applicable
```

- [ ] **Step 4: Add the visibility multiplier table**

In `hypothesize.py`, just below `PENALTY_WEIGHTS`, add:

```python
# ---------------------------------------------------------------------------
# Phase 4: visibility multiplier — dampens topologically weak passive cascades.
# Key is (kind, role, mode). Missing entries default to 1.0 (no dampening).
# Applied to `tp_comps` only; FP/FN weights are unchanged.
# ---------------------------------------------------------------------------

_SCORE_VISIBILITY: dict[tuple[str, str, str], float] = {
    ("passive_c", "decoupling", "open"): 0.5,
    ("passive_c", "bulk",       "open"): 0.5,
    ("passive_c", "filter",     "open"): 0.5,
    ("passive_r", "pull_up",    "open"): 0.5,
    ("passive_r", "pull_down",  "open"): 0.5,
    # shorts are visible at rail level → no multiplier.
}
```

- [ ] **Step 5: Update `_score_candidate` to accept the multiplier**

Modify `_score_candidate` (around line 294) to take an optional `tp_mult` argument:

```python
def _score_candidate(
    cascade: dict,
    obs: Observations,
    *,
    tp_mult: float = 1.0,
) -> tuple[float, HypothesisMetrics, HypothesisDiff]:
    # ... existing body unchanged through the metrics assembly ...
    # Then compute the score with the multiplier on component TPs only:
    tp = (tp_c * tp_mult) + tp_r
    fp = fp_c + fp_r
    fn = fn_c + fn_r
    score = float(tp - fp_w * fp - fn_w * fn)
    # (rest unchanged)
```

(The full body stays the same — only the `tp` line gains `* tp_mult` on `tp_c`. Keep every other line.)

- [ ] **Step 6: Wire `_SCORE_VISIBILITY` into the single-fault enumeration**

Update `_enumerate_single_fault` (around line 514) so each call to `_score_candidate` passes the right multiplier:

```python
def _enumerate_single_fault(
    electrical, analyzed_boot, observations,
):
    cascades_cache = {}
    ranked = []
    for refdes in electrical.components:
        comp = electrical.components[refdes]
        kind = getattr(comp, "kind", "ic")
        role = getattr(comp, "role", None)
        for mode in _applicable_modes(electrical, refdes):
            cascade = _simulate_failure(electrical, analyzed_boot, refdes, mode)
            cascades_cache[(refdes, mode)] = cascade
            if not _relevant_to_observations(cascade, observations):
                continue
            tp_mult = _SCORE_VISIBILITY.get((kind, role, mode), 1.0) if role else 1.0
            score, metrics, diff = _score_candidate(
                cascade, observations, tp_mult=tp_mult,
            )
            ranked.append((refdes, mode, score, metrics, diff))
    ranked.sort(key=lambda t: -t[2])
    return cascades_cache, ranked
```

- [ ] **Step 7: Wire `_simulate_failure` to route passive modes through the table**

In `_simulate_failure` (around line 233), after the existing `if mode == "shorted"` branch and before the final `raise ValueError`, add:

```python
    # Phase 4: passive modes.
    if mode in {"open", "short"}:
        comp = electrical.components.get(refdes)
        if comp is None:
            return _empty_cascade()
        kind = getattr(comp, "kind", "ic")
        role = getattr(comp, "role", None)
        if kind == "ic" or role is None:
            return _empty_cascade()
        handler = _PASSIVE_CASCADE_TABLE.get((kind, role, mode))
        if handler is None:
            return _empty_cascade()
        return handler(electrical, comp)
```

- [ ] **Step 8: Run the tests**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k "applicable_modes or score_visibility"`
Expected: 5 PASS.

- [ ] **Step 9: Run the full hypothesize suite**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py tests/pipeline/schematic/test_hypothesize_accuracy.py -v`
Expected: ALL PASS. Phase 1 accuracies untouched — only IC candidates were scored before, and the multiplier defaults to 1.0.

- [ ] **Step 10: Commit T9**

```bash
git commit -m "$(cat <<'EOF'
feat(hypothesize): applicable_modes for passives + visibility multiplier

_applicable_modes gates passive candidates to (open, short) and only
for roles with a non-alive handler. _SCORE_VISIBILITY dampens
topologically-weak cascades (decoupling/bulk/filter open, pull_up/
pull_down open) to tp × 0.5 so they don't bloat the top-3 on
single-observation cases. _simulate_failure dispatches passive modes
through the table; _enumerate_single_fault threads the multiplier.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
```

---

## Task T10: Hand-written scenarios (YAML fixture + loader test)

**Files:**
- Create: `tests/pipeline/schematic/fixtures/hand_written_scenarios.yaml`
- Create: `tests/pipeline/schematic/test_hand_written_scenarios.py`

- [ ] **Step 1: Check the MNT Reform graph for available refdes**

Run:
```
jq '[.components | to_entries[] | select(.value.type == "capacitor") | .key] | length' memory/mnt-reform-motherboard/electrical_graph.json 2>/dev/null || echo "graph not compiled yet — skip this step"
```
Expected: a number (hundreds). If the graph isn't compiled, skip — the scenarios file has a SKIP fallback and CI will surface it as a warning.

Also pick 3 real refdes from the graph that match the 3 cases:
```
jq '[.components | to_entries[] | select(.value.kind == "passive_c" and .value.role == "decoupling") | .key] | .[0:5]' memory/mnt-reform-motherboard/electrical_graph.json 2>/dev/null
```
Note down the first result — that's the `C` candidate. Same for `passive_r` + `role=feedback` and `passive_fb` + `role=filter`.

(If real refdes differ from `C156`/`R43`/`FB2`, **substitute them in the YAML below**. The IDs in the YAML are illustrative.)

- [ ] **Step 2: Create the YAML fixture**

Create `tests/pipeline/schematic/fixtures/hand_written_scenarios.yaml`:

```yaml
# Phase 4 hand-written scenarios — anti-auto-referential corpus.
# Each scenario encodes an observation shape a technician would report,
# with a physically-motivated ground truth. If the named refdes is absent
# from the compiled graph, the scenario is SKIPPED (warning, not failure)
# so the file remains valid across graph regenerations.

scenarios:
  - id: mnt-reform-c-decoupling-short
    description: |
      +3V3 rail measures shorted to GND, U7 (LPC) dead. A decoupling cap
      on +3V3 is the likely root cause. Ground truth: any passive_c with
      role=decoupling on +3V3.
    device_slug: mnt-reform-motherboard
    observations:
      state_rails: { "+3V3": "shorted" }
      state_comps: { "U7": "dead" }
    ground_truth_match:
      kind: passive_c
      role: decoupling
      expected_mode: short
    accept_in_top_n: 5

  - id: mnt-reform-r-feedback-overvolt
    description: |
      +5V measures 7.2 V (overvoltage encoded as rail `shorted` per Phase 1).
      A feedback divider R open is the likely root cause. Ground truth: any
      passive_r with role=feedback connected to the +5V regulator's feedback
      net.
    device_slug: mnt-reform-motherboard
    observations:
      state_rails: { "+5V": "shorted" }
    ground_truth_match:
      kind: passive_r
      role: feedback
      expected_mode: open
    accept_in_top_n: 10

  - id: mnt-reform-fb-filter-open
    description: |
      LPC_VCC rail entirely dead, U7 dead. A ferrite bead open between
      +3V3 and LPC_VCC is the likely root cause.
    device_slug: mnt-reform-motherboard
    observations:
      state_rails: { "LPC_VCC": "dead" }
      state_comps: { "U7": "dead" }
    ground_truth_match:
      kind: passive_fb
      role: filter
      expected_mode: open
    accept_in_top_n: 5
```

- [ ] **Step 3: Write the loader test**

Create `tests/pipeline/schematic/test_hand_written_scenarios.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Hand-written Phase 4 scenario gate.

Anti-auto-referential corpus — scenarios encode observations that were
NOT generated by _simulate_failure. Each scenario declares a
ground_truth_match (kind + role [+ expected_mode]); the test asserts
that at least one hypothesis matching the criteria lands in the top-N.

If the named device_slug / observation target isn't present in the
compiled graph (e.g. fresh checkout, different board), the scenario
is SKIPPED with a warning, not failed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from api.pipeline.schematic.hypothesize import Observations, hypothesize
from api.pipeline.schematic.schemas import ElectricalGraph

_FIXTURE = Path(__file__).parent / "fixtures" / "hand_written_scenarios.yaml"


def _load_graph(slug: str) -> ElectricalGraph | None:
    path = Path("memory") / slug / "electrical_graph.json"
    if not path.exists():
        return None
    return ElectricalGraph.model_validate_json(path.read_text())


def _scenarios():
    data = yaml.safe_load(_FIXTURE.read_text())
    return data.get("scenarios", [])


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda s: s["id"])
def test_hand_written_scenario_has_matching_top_n(scenario):
    graph = _load_graph(scenario["device_slug"])
    if graph is None:
        pytest.skip(f"graph missing for {scenario['device_slug']}")

    obs = Observations(
        state_comps=scenario["observations"].get("state_comps", {}),
        state_rails=scenario["observations"].get("state_rails", {}),
    )

    # Pre-check: every observation target must exist in the graph.
    missing = [
        t for t in list(obs.state_comps) + list(obs.state_rails)
        if t not in graph.components and t not in graph.power_rails
    ]
    if missing:
        pytest.skip(f"graph missing targets: {missing}")

    # Pre-check: a matching passive must exist in the compiled graph.
    match = scenario["ground_truth_match"]
    candidates = [
        r for r, c in graph.components.items()
        if getattr(c, "kind", "ic") == match["kind"]
        and getattr(c, "role", None) == match["role"]
    ]
    if not candidates:
        pytest.skip(
            f"no passive matches kind={match['kind']} role={match['role']} "
            f"in {scenario['device_slug']} graph"
        )

    result = hypothesize(graph, observations=obs, max_results=scenario["accept_in_top_n"])
    hits = [
        h for h in result.hypotheses
        if len(h.kill_refdes) == 1
        and h.kill_refdes[0] in candidates
        and h.kill_modes[0] == match["expected_mode"]
    ]
    assert hits, (
        f"scenario {scenario['id']}: no matching hypothesis in top "
        f"{scenario['accept_in_top_n']}. Top hypotheses: "
        f"{[(h.kill_refdes, h.kill_modes, h.score) for h in result.hypotheses[:5]]}"
    )
```

- [ ] **Step 4: Install `pyyaml` if not already a dep**

Run: `.venv/bin/pip show pyyaml >/dev/null 2>&1 || .venv/bin/pip install pyyaml`
Expected: silent or install message. Add to `pyproject.toml` under `[dev]` if missing.

- [ ] **Step 5: Run the gate**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hand_written_scenarios.py -v`
Expected: Either PASS (if graph compiled + scenarios match) or SKIPPED (if graph absent). Never a hard fail on a fresh checkout.

If the gate ASSERTS (fail) on a present graph, the passive classifier's role assignment or cascade dispatch is mismatched — investigate before declaring T10 done.

- [ ] **Step 6: Commit T10**

```bash
git commit -m "$(cat <<'EOF'
test(hypothesize): hand-written scenario gate against auto-ref bias

YAML fixture with 3 initial scenarios (decoupling C short,
feedback R open overvolt, filter FB open). Loader test
parametrizes scenarios, skips when the named graph or passive
match is absent, fails when the compiled graph matches but
hypothesize doesn't surface the ground truth in top-N. Mitigates
the self-consistency bias of the auto-generated corpus.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- tests/pipeline/schematic/fixtures/hand_written_scenarios.yaml tests/pipeline/schematic/test_hand_written_scenarios.py
```

---

## Task T11: Extend `gen_hypothesize_benchmarks.py` for passive sampling

**Files:**
- Modify: `scripts/gen_hypothesize_benchmarks.py`
- Modify: `scripts/bench_hypothesize.py` (per-mode p95 including open/short)

- [ ] **Step 1: Read the existing generator**

Run: `cat scripts/gen_hypothesize_benchmarks.py | head -80`
Expected: Understand the current loop structure — it iterates components and samples cascades.

- [ ] **Step 2: Extend the candidate enumeration**

Modify the main loop in `gen_hypothesize_benchmarks.py` so that, in addition to sampling `(refdes, mode)` from IC kinds with applicable modes, it also samples from passives:

```python
# After the existing IC scenario emission, add:

for refdes, comp in electrical.components.items():
    kind = getattr(comp, "kind", "ic")
    if kind == "ic":
        continue
    role = getattr(comp, "role", None)
    if role is None:
        continue
    for mode in ("open", "short"):
        from api.pipeline.schematic.hypothesize import (
            _PASSIVE_CASCADE_TABLE, _cascade_passive_alive,
            _simulate_failure,
        )
        handler = _PASSIVE_CASCADE_TABLE.get((kind, role, mode))
        if handler is None or handler is _cascade_passive_alive:
            continue
        cascade = _simulate_failure(electrical, None, refdes, mode)
        affected_comps = (
            cascade["dead_comps"] | cascade["anomalous_comps"] | cascade["hot_comps"]
        )
        affected_rails = cascade["dead_rails"] | cascade["shorted_rails"]
        if not affected_comps and not affected_rails:
            continue
        # Sample a handful of observations — the spec asks for 2-4 around the
        # cascade with some alive neighbours as controls.
        state_comps = {}
        state_rails = {}
        for r in sorted(affected_comps)[:3]:
            # Use the right per-refdes mode key for the observation.
            if r in cascade["dead_comps"]:
                state_comps[r] = "dead"
            elif r in cascade["anomalous_comps"]:
                state_comps[r] = "anomalous"
            elif r in cascade["hot_comps"]:
                state_comps[r] = "hot"
        for r in sorted(affected_rails)[:2]:
            if r in cascade["shorted_rails"]:
                state_rails[r] = "shorted"
            else:
                state_rails[r] = "dead"
        scenario = {
            "id": f"{refdes}-{mode}",
            "kill_refdes": [refdes],
            "kill_modes":  [mode],
            "state_comps": state_comps,
            "state_rails": state_rails,
        }
        all_scenarios.append(scenario)
```

- [ ] **Step 3: Run the generator**

Run: `.venv/bin/python scripts/gen_hypothesize_benchmarks.py --slug=mnt-reform-motherboard`
Expected: corpus grows from ~155 to >1000 scenarios. Warning: this REGENERATES
`tests/pipeline/schematic/fixtures/hypothesize_scenarios.json` — verify by:
`python -c "import json; d = json.load(open('tests/pipeline/schematic/fixtures/hypothesize_scenarios.json')); print(len(d['scenarios']))"`

- [ ] **Step 4: Update `bench_hypothesize.py` to report p95 per mode including open/short**

Find the per-mode aggregation in `bench_hypothesize.py` (search for `mode` keying) and ensure it includes `"open"` and `"short"`:

```python
PER_MODE_KEYS = ["dead", "anomalous", "hot", "shorted", "open", "short"]
# ... (rest of the reporter uses PER_MODE_KEYS for the bucket loop)
```

- [ ] **Step 5: Run the benchmark**

Run: `.venv/bin/python scripts/bench_hypothesize.py --slug=mnt-reform-motherboard --samples=200`
Expected: reports p95 for every mode including open/short. Aggregate p95 should stay under 1.5 s.

- [ ] **Step 6: Commit T11**

```bash
git commit -m "$(cat <<'EOF'
feat(bench): passive (open/short) sampling in hypothesize corpus

gen_hypothesize_benchmarks now samples (passive_refdes, mode) pairs in
addition to IC kills, respecting per-role applicability. bench reports
per-mode p95 for all 6 modes. Corpus grows ~10x (~155 → >1000 scenarios).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- scripts/gen_hypothesize_benchmarks.py scripts/bench_hypothesize.py tests/pipeline/schematic/fixtures/hypothesize_scenarios.json
```

---

## Task T12: Per-mode CI gates for open/short

**Files:**
- Modify: `tests/pipeline/schematic/test_hypothesize_accuracy.py`

- [ ] **Step 1: Read current parametrize**

Run: `grep -n "parametrize.*mode" tests/pipeline/schematic/test_hypothesize_accuracy.py`
Expected: existing line with Phase 1 modes. Note the THRESHOLDS dict.

- [ ] **Step 2: Extend the parametrize and thresholds**

Update the parametrize call to include `"open"` and `"short"`, and extend
the `THRESHOLDS` dict (search for it, typically near the top of the file):

```python
THRESHOLDS = {
    "dead":      {"top1": 0.80, "top3": 0.90, "mrr": 0.85},
    "anomalous": {"top1": 0.50, "top3": 0.70, "mrr": 0.65},
    "hot":       {"top1": 0.70, "top3": 0.90, "mrr": 0.80},
    "shorted":   {"top1": 0.55, "top3": 0.75, "mrr": 0.65},
    "open":      {"top1": 0.40, "top3": 0.65, "mrr": 0.55},
    "short":     {"top1": 0.55, "top3": 0.75, "mrr": 0.65},
}

@pytest.mark.parametrize("mode", [
    "dead", "anomalous", "hot", "shorted",
    "open", "short",
])
def test_top1_accuracy_per_mode(mode):
    # ... (body unchanged)
```

- [ ] **Step 3: Run the accuracy suite**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize_accuracy.py -v`
Expected: ALL PASS — corpus regenerated in T11 now includes open/short scenarios, and the engine ranks the passives.

If a gate fails:
1. Inspect the failure. Is it under-ranking a KNOWN-correct case? Drop the multiplier on that (kind, role, mode) by 0.1 and re-run.
2. Is it over-ranking a noisy case? Tighten `_applicable_modes` or confirm the classifier doesn't over-assign the role.
3. If the corpus itself is off (bad ground truth), patch `gen_hypothesize_benchmarks.py` in a separate commit.

- [ ] **Step 4: Commit T12**

```bash
git commit -m "$(cat <<'EOF'
test(hypothesize): per-mode CI gates for passive modes (open/short)

Thresholds land conservative — open at 40%/65% top1/top3 (soft
cascades), short at 55%/75% (rail-visible). Calibrate after the
first full run against the regenerated corpus.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- tests/pipeline/schematic/test_hypothesize_accuracy.py
```

---

## Task T13: Weight tuner includes `_SCORE_VISIBILITY`

**Files:**
- Modify: `scripts/tune_hypothesize_weights.py`

- [ ] **Step 1: Read the current tuner**

Run: `cat scripts/tune_hypothesize_weights.py | head -60`
Expected: existing grid sweep over `PENALTY_WEIGHTS`.

- [ ] **Step 2: Add a coarse sweep over the visibility multiplier**

Extend the sweep loop to also try multipliers in `{0.3, 0.5, 0.7}` for the 5 entries currently in `_SCORE_VISIBILITY`:

```python
from api.pipeline.schematic.hypothesize import _SCORE_VISIBILITY

VISIBILITY_SWEEP = [0.3, 0.5, 0.7]
VISIBILITY_KEYS = list(_SCORE_VISIBILITY.keys())

# Outer loop adds a cross-product over visibility values. Use a coarse
# search to keep runtime reasonable — the full grid explodes fast.
for mult in VISIBILITY_SWEEP:
    # Apply the same mult to every key (not independent per-key — too
    # expensive). Report accuracy per configuration.
    for key in VISIBILITY_KEYS:
        _SCORE_VISIBILITY[key] = mult
    # ... run the corpus, compute aggregate weighted top-3 ...
```

Keep the inner grid as-is for `PENALTY_WEIGHTS`. The full product is
`4 × 5 × 3 = 60` configs — ~2 min on the regenerated corpus.

- [ ] **Step 3: Re-run the tuner (optional — skip if short on time)**

Run: `.venv/bin/python scripts/tune_hypothesize_weights.py`
Expected: completes in ~2 min, reports the best `(PENALTY_WEIGHTS, _SCORE_VISIBILITY)` pair. If it improves weighted top-3 AND the hand-written gate still passes, apply the values; otherwise leave defaults.

- [ ] **Step 4: Run the full bench + accuracy suite to sanity-check**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize_accuracy.py tests/pipeline/schematic/test_hand_written_scenarios.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit T13**

```bash
git commit -m "$(cat <<'EOF'
chore(bench): tune_hypothesize_weights sweeps _SCORE_VISIBILITY

Coarse 3-step sweep over the passive visibility multiplier, composed
with the existing PENALTY_WEIGHTS grid. Reports per-mode accuracy at
every step; only commits new defaults when the hand-written gate
still passes.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- scripts/tune_hypothesize_weights.py
```

---

## Task T14: Frontend — kind-aware mode picker

**Files:**
- Modify: `web/js/schematic.js` (add `MODE_SETS`, branch `updateInspector(node)` on `node.kind`)
- Modify: `web/styles/schematic.css` (CSS tokens for passive picker variants)

**This task requires browser-verify with Alexis before commit (feedback memory).**

- [ ] **Step 1: Read the current `updateInspector`**

Run: `grep -n "updateInspector\|sim-mode-picker\|MODE_SETS" web/js/schematic.js | head`
Expected: inspector entry point + current picker rendering.

- [ ] **Step 2: Add `MODE_SETS` constant**

Near the top of `web/js/schematic.js`, add:

```javascript
const MODE_SETS = {
  ic:         ["unknown", "alive", "dead", "anomalous", "hot"],
  passive_r:  ["unknown", "alive", "open", "short"],
  passive_c:  ["unknown", "alive", "open", "short"],
  passive_d:  ["unknown", "alive", "open", "short"],
  passive_fb: ["unknown", "alive", "open", "short"],
  rail:       ["unknown", "alive", "dead", "shorted"],
};

const MODE_GLYPH = {
  unknown:   "⚪",
  alive:     "✅",
  dead:      "❌",
  anomalous: "⚠",
  hot:       "🔥",
  shorted:   "⚡",
  open:      "⚪",   // passive open rendered same as unknown but cyan-tinted via CSS
  short:     "⚡",   // passive short shares the shorted glyph
};
```

- [ ] **Step 3: Branch the inspector on `node.kind`**

Find the block that currently emits the 3-state toggle (search for
`sim-mode-picker`). Replace the hard-coded mode list with a lookup:

```javascript
function updateInspector(node) {
  // ... existing title / metadata rendering ...
  const kind = node.kind || (node.isRail ? "rail" : "ic");
  const modes = MODE_SETS[kind] || MODE_SETS.ic;
  const picker = document.querySelector(".sim-mode-picker");
  picker.dataset.kind = kind;
  picker.innerHTML = modes.map(m => `
    <button data-mode="${m}" class="${
      SimulationController.observations.state_comps.get(node.refdes) === m
      || SimulationController.observations.state_rails.get(node.refdes) === m
      ? "active" : ""
    }">${MODE_GLYPH[m] || "·"} ${m}</button>
  `).join("");
  // Re-attach click handlers.
  picker.querySelectorAll("button[data-mode]").forEach(b => {
    b.addEventListener("click", () => handleModeClick(node, b.dataset.mode));
  });
}
```

- [ ] **Step 4: Add CSS tokens for the passive picker**

Append to `web/styles/schematic.css`:

```css
.sim-mode-picker[data-kind^="passive"] button[data-mode="open"] {
  color: var(--cyan);
  border-color: color-mix(in oklch, var(--cyan) 40%, transparent);
}
.sim-mode-picker[data-kind^="passive"] button[data-mode="short"] {
  color: var(--amber);
  border-color: color-mix(in oklch, var(--amber) 40%, transparent);
}
.sim-mode-picker[data-kind^="passive"] button[data-mode="open"].active {
  background: color-mix(in oklch, var(--cyan) 30%, var(--panel-2));
}
.sim-mode-picker[data-kind^="passive"] button[data-mode="short"].active {
  background: color-mix(in oklch, var(--amber) 30%, var(--panel-2));
}
```

- [ ] **Step 5: Hand-off to Alexis for browser-verify**

Write a 3-line note to Alexis:

```
T14 frontend picker ready. Please load http://localhost:8000/#schematic,
click a capacitor (e.g. C156 on the MNT graph), and confirm the picker
shows [unknown, alive, open, short] with cyan/amber tints on the
passive-specific buttons. ping me after verify.
```

**DO NOT COMMIT until Alexis confirms.** This is the feedback-memory gate.

- [ ] **Step 6: On Alexis's OK, commit T14**

```bash
git commit -m "$(cat <<'EOF'
feat(web): kind-aware mode picker for passives

Inspector picker now branches on node.kind — passives get
[unknown, alive, open, short] with cyan (open) / amber (short) tints.
IC and rail picker unchanged. Browser-verified with Alexis.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- web/js/schematic.js web/styles/schematic.css
```

---

## Task T15: Frontend — confidence-tinted rendering

**Files:**
- Modify: `web/js/schematic.js` (apply opacity based on classifier confidence)

**Browser-verify required before commit.**

- [ ] **Step 1: Add opacity styling based on `node.confidence`**

In the D3 node rendering loop (search for `attr("fill"` or `.node` class), add:

```javascript
.style("opacity", d => {
  // Passives with low classifier confidence render dimmer — cues the
  // tech that role assignment is tentative.
  if (d.kind && d.kind.startsWith("passive") && d.confidence != null) {
    return Math.max(0.4, Math.min(1.0, d.confidence));
  }
  return 1.0;
})
```

(Ensure `confidence` is emitted on the node payload — add it in the
backend graph-to-payload step if missing. Search for where `kind`/`role`
are serialized; `confidence` follows the same route.)

- [ ] **Step 2: Backend — ensure `confidence` is on the node payload**

Grep `graph_transform.py` (or wherever the graph is flattened for the
frontend) for how `kind`/`role` are emitted. Pass through the classifier's
confidence alongside. If the classifier's output isn't persisted yet,
add a `ComponentNode.classifier_confidence: float | None = None` field
in T1 (retroactive — land as an additive commit).

(*Or* simpler: skip confidence wiring in this phase, use a constant 0.7 for all
passives. Leave a FOLLOWUP comment for Phase 4.1.)

- [ ] **Step 3: Browser-verify with Alexis**

Ask Alexis to load the schematic view, hover passives with <0.6 confidence
(if any), and confirm they render dimmer than high-confidence ones.

- [ ] **Step 4: On OK, commit T15**

```bash
git commit -m "$(cat <<'EOF'
feat(web): confidence-tinted rendering for classified passives

Passive nodes render at opacity = max(0.4, confidence) so the tech
immediately sees which role assignments are tentative. Confidence
passes through from the classifier via the node payload.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- web/js/schematic.js
```

---

## Task T16: Frontend — end-to-end smoke with Alexis

**Files:**
- none (verification task — triggers a walk-through)

**Browser-verify is the whole task.**

- [ ] **Step 1: Ask Alexis to smoke the end-to-end flow**

Script:
1. Load http://localhost:8000/#schematic?slug=mnt-reform-motherboard
2. Click a decoupling capacitor — confirm picker shows `[unknown, alive, open, short]`
3. Set it to `short` — confirm WS event fires AND the graph re-highlights
4. Click « hypothèses » — confirm a `C*** short` candidate appears in the top-5
5. Clear, click a feedback resistor (R43 if visible) — confirm `[open, short]` picker
6. Set rail `+5V` to `shorted` + submit — confirm `R43 open` (or similar) appears in top-10

- [ ] **Step 2: Fix any issue surfaced**

- If the picker doesn't change → T14 regression. Fix + re-verify.
- If the WS doesn't relay passive modes → extend the event envelope in `ws_events.py` and the handler in `schematic.js`.
- If hypotheses don't surface the passive → classifier role assignment wrong OR cascade handler wrong. Debug with a direct curl on `POST /hypothesize`.

- [ ] **Step 3: On Alexis's OK, NO commit** (there's nothing to commit — smoke-only)

Note T16 as **verified** on the plan checklist. Move to Group E.

---

## Task T17: `GET /pipeline/packs/{slug}/schematic/passives` endpoint

**Files:**
- Modify: `api/pipeline/__init__.py` (new route)
- Modify: `tests/pipeline/test_schematic_api.py` (smoke coverage)

- [ ] **Step 1: Write the failing test**

Append to `tests/pipeline/test_schematic_api.py`:

```python
def test_get_schematic_passives_returns_classifier_output(tmp_path, client):
    """Smoke — the endpoint returns kind/role/confidence per passive."""
    # Assumes a pre-compiled graph fixture for `test-slug`.
    # Build one in-place.
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, NetNode, SchematicQualityReport,
    )
    slug = "passives-endpoint-test"
    mem = tmp_path / "memory" / slug
    mem.mkdir(parents=True)
    graph = ElectricalGraph(
        device_slug=slug,
        components={
            "C156": ComponentNode(
                refdes="C156", type="capacitor",
                kind="passive_c", role="decoupling",
            ),
            "U7":   ComponentNode(refdes="U7", type="ic"),
        },
        nets={}, power_rails={}, typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    (mem / "electrical_graph.json").write_text(graph.model_dump_json())
    # Monkeypatch settings.memory_root if necessary for the test fixture.

    resp = client.get(f"/pipeline/packs/{slug}/schematic/passives")
    assert resp.status_code == 200
    body = resp.json()
    assert any(row["refdes"] == "C156" for row in body)
    # U7 is an IC and MUST NOT appear in the response.
    assert all(row["refdes"] != "U7" for row in body)
    row = next(r for r in body if r["refdes"] == "C156")
    assert row["kind"] == "passive_c"
    assert row["role"] == "decoupling"
```

- [ ] **Step 2: Run — should fail (endpoint doesn't exist)**

Run: `.venv/bin/pytest tests/pipeline/test_schematic_api.py::test_get_schematic_passives_returns_classifier_output -v`
Expected: 404 / FAIL.

- [ ] **Step 3: Add the route**

In `api/pipeline/__init__.py`, add:

```python
@router.get("/packs/{slug}/schematic/passives")
def get_schematic_passives(slug: str):
    path = settings.memory_root / slug / "electrical_graph.json"
    if not path.exists():
        raise HTTPException(404, f"no electrical_graph for {slug}")
    graph = ElectricalGraph.model_validate_json(path.read_text())
    return [
        {
            "refdes":     refdes,
            "kind":       comp.kind,
            "role":       comp.role,
            "confidence": 0.7,  # classifier confidence not yet persisted; T15 followup
            "source":     "heuristic",
        }
        for refdes, comp in graph.components.items()
        if comp.kind != "ic"
    ]
```

- [ ] **Step 4: Run the test**

Run: `.venv/bin/pytest tests/pipeline/test_schematic_api.py::test_get_schematic_passives_returns_classifier_output -v`
Expected: PASS.

- [ ] **Step 5: Commit T17**

```bash
git commit -m "$(cat <<'EOF'
feat(api): GET /schematic/passives read-only endpoint

Returns classifier output per passive refdes (kind, role, confidence,
source). Filters ICs out — only R/C/D/FB emitted. Used for debugging
and for the hand-written scenarios to look up candidate refdes.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/__init__.py tests/pipeline/test_schematic_api.py
```

---

## Task T18: Optional Opus enrichment pass

**Files:**
- Modify: `api/pipeline/schematic/passive_classifier.py` (add `classify_passives_llm`)
- Modify: `api/pipeline/schematic/orchestrator.py` (invoke in parallel with `net_classifier`)
- Modify: `api/pipeline/schematic/cli.py` (`--classify-passives` re-run switch)
- Modify: `tests/pipeline/schematic/test_passive_classifier.py` (mocked LLM path)

- [ ] **Step 1: Add the LLM path to `passive_classifier.py`**

Append to `passive_classifier.py`:

```python
# ---------------------------------------------------------------------------
# Optional Opus pass — same shape as net_classifier.classify_nets_llm.
# ---------------------------------------------------------------------------

from pydantic import BaseModel, ConfigDict, Field

from api.pipeline.tool_call import call_with_forced_tool

_DEFAULT_CLASSIFIER_MODEL = "claude-sonnet-4-6"
_BATCH_SIZE = 150


class _PassiveAssignment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    refdes: str
    kind: str
    role: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.7)


class PassiveClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_slug: str
    assignments: list[_PassiveAssignment]
    model_used: str


SUBMIT_TOOL_NAME = "submit_passive_classification"

_SYSTEM_PROMPT = """You are an expert in board-level passive role
classification. For each passive (R, C, D, FB) in the input list, emit
one of the canonical roles:

- R: series · feedback · pull_up · pull_down · current_sense · damping
- C: decoupling · bulk · filter · ac_coupling · tank · bypass
- D: flyback · rectifier · esd · reverse_protection · signal_clamp
- FB: filter

Use the attached per-refdes context (connected nets, pin roles,
nearby components, typed edges). Output null role when you genuinely
can't tell. Confidence 0-1 — lower when evidence is thin.
"""


async def classify_passives_llm(
    graph, *, client, model=None,
):
    model = model or _DEFAULT_CLASSIFIER_MODEL
    passives = [
        (r, c) for r, c in graph.components.items()
        if c.type in {"resistor", "capacitor", "diode", "ferrite"}
    ]
    # ... batch + call_with_forced_tool dispatch, identical shape to
    # net_classifier.classify_nets_llm ...
    # (see net_classifier.py for the batch pattern; replicate it with the
    # PassiveClassification schema + SUBMIT_TOOL_NAME.)
    # Implementation mirrors net_classifier — elided here for brevity but
    # fully specified by reference.
    raise NotImplementedError(
        "Opus pass — see net_classifier.classify_nets_llm for the shape"
    )
```

**Note:** T18's LLM path is marked optional. If the Phase 4 drop is
already on the bench and Alexis wants to skip the Opus pass until a
follow-up, leave `NotImplementedError` in place and gate the public
`classify_passives()` entry point on client being None → always take
the heuristic path. Completing T18 is a one-day follow-up, not a
blocker for merging Phase 4.

- [ ] **Step 2: Mirror the `classify_nets_llm` batch implementation**

Copy the batch + merge loop pattern verbatim from `net_classifier.py`
(`classify_nets_llm`), swapping out the schema. If Alexis prefers to
defer, leave the `NotImplementedError` and move on.

- [ ] **Step 3: Add the CLI switch**

In `api/pipeline/schematic/cli.py`, add the `--classify-passives` flag:

```python
parser.add_argument(
    "--classify-passives", action="store_true",
    help="Re-run the passive classifier in isolation on an existing "
         "electrical_graph.json (no recompile).",
)
```

and dispatch:

```python
if args.classify_passives:
    from api.pipeline.schematic.passive_classifier import classify_passives_heuristic
    from api.pipeline.schematic.schemas import ElectricalGraph
    path = Path("memory") / args.slug / "electrical_graph.json"
    graph = ElectricalGraph.model_validate_json(path.read_text())
    assignments = classify_passives_heuristic(graph)
    enriched = dict(graph.components)
    for refdes, (kind, role, _) in assignments.items():
        if refdes in enriched:
            enriched[refdes] = enriched[refdes].model_copy(
                update={"kind": kind, "role": role},
            )
    path.write_text(graph.model_copy(update={"components": enriched}).model_dump_json())
    print(f"re-classified {len(assignments)} passives in {path}")
    return
```

- [ ] **Step 4: Run the CLI to smoke it**

Run: `.venv/bin/python -m api.pipeline.schematic.cli --slug=mnt-reform-motherboard --classify-passives`
Expected: prints "re-classified N passives" and writes the updated graph.

- [ ] **Step 5: Commit T18**

```bash
git commit -m "$(cat <<'EOF'
feat(schematic): optional Opus classifier pass + CLI re-classify switch

classify_passives_llm mirrors net_classifier.classify_nets_llm shape
(batch + forced-tool + merge). Left as NotImplementedError stub when
Opus enrichment is deferred — heuristic path is production-ready.
CLI gets --classify-passives for in-place re-runs against a compiled
graph.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/passive_classifier.py api/pipeline/schematic/cli.py
```

---

## Self-Review Checklist (run after writing plan, fix inline)

**1. Spec coverage** — every section of `2026-04-24-passive-component-injection-design.md`:
- [x] ComponentKind + ComponentNode.kind/role → T1
- [x] ComponentMode extension + coherence validator → T5
- [x] passive_classifier.py heuristic (R/C/D/FB) → T2+T3
- [x] Compiler integration (kind, role, PowerRail.decoupling) → T4
- [x] _PASSIVE_CASCADE_TABLE + 6 primitive handlers → T6
- [x] Remaining handlers (R, C full table, D full table) → T7+T8
- [x] _applicable_modes + _SCORE_VISIBILITY → T9
- [x] SimulationEngine transitive-rails cleanup → T0
- [x] Hand-written scenarios → T10
- [x] Corpus regeneration for open/short → T11
- [x] Per-mode CI gates (open/short) → T12
- [x] Weight tuning with _SCORE_VISIBILITY → T13
- [x] Frontend kind-aware picker → T14
- [x] Frontend confidence-tinted rendering → T15
- [x] Frontend end-to-end smoke → T16
- [x] GET /schematic/passives endpoint → T17
- [x] Opus enrichment pass (optional) + CLI switch → T18

**2. Placeholder scan** — search the plan for red-flag patterns:
- No "TBD" / "TODO" / "implement later" / "similar to Task N".
- All code blocks contain the actual content an engineer needs.
- T18's NotImplementedError is explicit and bounded with a ship-or-defer guard.

**3. Type consistency:**
- `ComponentKind` used identically in schemas, classifier, hypothesize, frontend.
- `_PASSIVE_CASCADE_TABLE` key tuple `(kind, role, mode)` matches in every task that references it.
- Handler signature `Callable[[ElectricalGraph, ComponentNode], dict]` consistent across T6/T7/T8.
- `_SCORE_VISIBILITY` keyed identically across T9 and T13.

**4. Scope:** 19 tasks, ~2000 LOC, one Phase 4 ship. Focused on a single feature; no bundled unrelated concerns.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-24-passive-component-injection.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task (Haiku for T1/T2/T3/T10/T11/T12/T17 mechanical work, Sonnet for T4/T5/T6/T7/T8/T9 touch-sensitive hypothesize work, Opus for T0 and any brainstorm interruption). Review between tasks. Faster iteration.

2. **Inline Execution** — Execute tasks in this session using `superpowers:executing-plans`, batch with checkpoints at each group boundary.

**Which approach?**
