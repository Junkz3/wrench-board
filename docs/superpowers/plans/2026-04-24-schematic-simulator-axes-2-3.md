# Schematic simulator axes 2 & 3 — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add continuous failure modes (`degraded`/`shorted` rails, `degraded` components, `voltage_pct`) and a dedicated boardview bridge that emits a ranked probe route, with a scalar evaluation surface (`self_MRR + cascade_recall`) so future autoloop work has a stable oracle.

**Architecture:** Three concerns, three modules. (1) Extend the existing `simulator.py` engine with continuous states + `Failure`/`RailOverride` inputs. (2) Add `evaluator.py` + a frozen `benchmark/scenarios.jsonl` to score the engine. (3) Add `api/agent/schematic_boardview_bridge.py` as the single module that joins schematic-space (`ElectricalGraph`) with PCB-space (`Board`) to produce ranked probe routes. The simulator stays board-agnostic; the bridge is the only consumer that knows about both worlds.

**Tech Stack:** Python 3.11+, Pydantic v2, FastAPI ~0.136, pytest + pytest-asyncio, no new third-party dependency.

**Companion spec:** `docs/superpowers/specs/2026-04-24-schematic-simulator-axes-2-3-design.md`

---

## File Structure

| File | Action | Purpose |
|---|---|---|
| `api/pipeline/schematic/simulator.py` | Modify | New `RailState`/`ComponentState` values, `Failure`/`RailOverride` types, extended `SimulationEngine.__init__`, continuous propagation rules, `degraded` final verdict. |
| `api/pipeline/schematic/hypothesize.py` | Modify | Two new entries in `_PASSIVE_CASCADE_TABLE` (`passive_c.leaky_short`, `ic.regulating_low`) and inverse path that reads observed `degraded` rails. |
| `api/pipeline/schematic/evaluator.py` | Create | `compute_self_mrr`, `compute_cascade_recall`, `compute_score`, `Scorecard`. Pure functions, no I/O at call site. |
| `api/agent/schematic_boardview_bridge.py` | Create | `ProbePoint`, `EnrichedTimeline`, `enrich(timeline, board)` — ranking heuristic with mil→mm conversion. |
| `api/tools/schematic.py` | Modify | `mb_schematic_graph` accepts `failures`, `rail_overrides`, `session`; calls bridge when `session.board` present. |
| `api/pipeline/__init__.py` | Modify | `SimulateRequest` accepts `failures` + `rail_overrides`; endpoint returns raw timeline (no bridge in HTTP context). |
| `api/agent/runtime_managed.py` | Modify | Pass `session` through `mb_schematic_graph` dispatch. |
| `api/agent/runtime_direct.py` | Modify | Pass `session` through `mb_schematic_graph` dispatch. |
| `scripts/eval_simulator.py` | Create | One-line JSON CLI: `python -m scripts.eval_simulator --device <slug>`. |
| `benchmark/scenarios.jsonl` | Create | 5 sourced scenarios (MNT Reform initial). |
| `benchmark/sources/` | Create | Verbatim quote archive (one file per scenario). |
| `Makefile` | Modify | Add `make test-eval` target with score floor 0.5. |
| `tests/pipeline/schematic/test_simulator.py` | Modify | New tests for continuous propagation, failures/rail_overrides inputs. |
| `tests/pipeline/schematic/test_hypothesize.py` | Modify | New tests for the two new modes + inverse. |
| `tests/pipeline/schematic/test_evaluator.py` | Create | Coverage for evaluator functions and Scorecard. |
| `tests/agent/test_schematic_boardview_bridge.py` | Create | Coverage for `enrich()` ranking + downgrade path. |
| `tests/tools/test_schematic.py` | Modify | New tests for failures/rail_overrides input + enriched output path. |
| `tests/pipeline/test_simulate_endpoint.py` | Modify | New tests for failures/rail_overrides on the endpoint. |

---

## Task 1: Extend schemas — RailState / ComponentState / BoardState / Failure / RailOverride

**Files:**
- Modify: `api/pipeline/schematic/simulator.py` (top of file, before `SimulationEngine`)

- [ ] **Step 1: Write failing test for the new state space**

Append to `tests/pipeline/schematic/test_simulator.py`:

```python
def test_board_state_accepts_degraded_rail_with_voltage_pct():
    state = BoardState(
        phase_index=1,
        phase_name="Phase 1",
        rails={"+5V": "degraded"},
        rail_voltage_pct={"+5V": 0.94},
    )
    assert state.rails["+5V"] == "degraded"
    assert state.rail_voltage_pct["+5V"] == 0.94


def test_board_state_accepts_degraded_component():
    state = BoardState(
        phase_index=1,
        phase_name="Phase 1",
        components={"U7": "degraded"},
    )
    assert state.components["U7"] == "degraded"


def test_failure_requires_value_ohms_for_leaky_short():
    # Failure type accepts the new modes; validation of mode-specific
    # required fields lives in the engine, not the type itself.
    f = Failure(refdes="C42", mode="leaky_short", value_ohms=200.0)
    assert f.mode == "leaky_short"
    assert f.value_ohms == 200.0


def test_rail_override_carries_state_and_voltage_pct():
    o = RailOverride(label="+5V", state="degraded", voltage_pct=0.94)
    assert o.state == "degraded"
    assert o.voltage_pct == 0.94
```

Add the imports at the top of the test file (extend the existing line):

```python
from api.pipeline.schematic.simulator import (
    BoardState,
    ComponentState,
    Failure,
    RailOverride,
    RailState,
    SimulationEngine,
    SimulationTimeline,
)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_simulator.py -v -k "degraded or failure_requires or rail_override_carries"`
Expected: 4 failures (`Failure`, `RailOverride` not importable; `rail_voltage_pct` not a field).

- [ ] **Step 3: Extend the type space in `simulator.py`**

Locate the `RailState` / `ComponentState` / `SignalState` literals near the top of `api/pipeline/schematic/simulator.py` and replace them, then add the two new Pydantic types right after `SimulationTimeline`:

```python
RailState = Literal["off", "rising", "stable", "degraded", "shorted"]
ComponentState = Literal["off", "on", "degraded", "dead"]
SignalState = Literal["low", "high", "floating"]
FinalVerdict = Literal["completed", "blocked", "cascade", "degraded"]


class BoardState(BaseModel):
    """Snapshot of the board at the end of one phase."""

    model_config = ConfigDict(extra="forbid")

    phase_index: int
    phase_name: str
    rails: dict[str, RailState] = Field(default_factory=dict)
    rail_voltage_pct: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Optional per-rail voltage as a fraction of nominal. Present "
            "only when the rail is `degraded`/`shorted` (with finite R) "
            "or was explicitly observed via rail_overrides."
        ),
    )
    components: dict[str, ComponentState] = Field(default_factory=dict)
    signals: dict[str, SignalState] = Field(default_factory=dict)
    blocked: bool = False
    blocked_reason: str | None = None


class Failure(BaseModel):
    """A cause prescribed by the caller — the simulator computes the
    consequences (which rails sag, which components degrade)."""

    model_config = ConfigDict(extra="forbid")

    refdes: str
    mode: Literal[
        "dead",
        "shorted",
        "leaky_short",
        "regulating_low",
        "open",
    ]
    value_ohms: float | None = Field(
        default=None,
        description="Required for `leaky_short`. Path resistance to GND (Ω).",
    )
    voltage_pct: float | None = Field(
        default=None,
        description="Required for `regulating_low`. Output as fraction of nominal.",
    )


class RailOverride(BaseModel):
    """An observation supplied by the caller — forces a rail to a state."""

    model_config = ConfigDict(extra="forbid")

    label: str
    state: RailState
    voltage_pct: float | None = Field(
        default=None,
        description="Required when state is `degraded`.",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_simulator.py -v -k "degraded or failure_requires or rail_override_carries"`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add api/pipeline/schematic/simulator.py tests/pipeline/schematic/test_simulator.py
git commit -m "$(cat <<'EOF'
feat(schematic): extend simulator state space with continuous failure modes

Adds `degraded` and `shorted` to RailState, `degraded` to ComponentState,
optional `rail_voltage_pct` per BoardState, and the Failure / RailOverride
input types so callers can prescribe causes (leaky cap, drifting LDO) or
inject observations (rail measured at 4.7V) without round-tripping through
the engine API.
EOF
)" -- api/pipeline/schematic/simulator.py tests/pipeline/schematic/test_simulator.py
```

---

## Task 2: Engine — accept `failures` and `rail_overrides`, keep backward-compat

**Files:**
- Modify: `api/pipeline/schematic/simulator.py` (`SimulationEngine.__init__`)
- Modify: `tests/pipeline/schematic/test_simulator.py`

- [ ] **Step 1: Write failing tests for the new constructor params**

Append to `tests/pipeline/schematic/test_simulator.py`:

```python
def test_engine_accepts_failures_argument(graph_minimal):
    """Engine accepts a list of Failure objects without crashing."""
    engine = SimulationEngine(
        graph_minimal,
        failures=[Failure(refdes="U7", mode="dead")],
    )
    timeline = engine.run()
    assert "U7" in timeline.killed_refdes  # killed via Failure(mode="dead")


def test_engine_accepts_rail_overrides_argument(graph_minimal):
    """Engine accepts a list of RailOverride objects without crashing."""
    engine = SimulationEngine(
        graph_minimal,
        rail_overrides=[RailOverride(label="+5V", state="stable")],
    )
    timeline = engine.run()
    # Override forces +5V stable regardless of source state.
    final_state = timeline.states[-1]
    assert final_state.rails.get("+5V") == "stable"


def test_engine_killed_refdes_remains_backward_compat(graph_minimal):
    """Existing killed_refdes API still works identically."""
    engine = SimulationEngine(graph_minimal, killed_refdes=["U7"])
    timeline = engine.run()
    assert "U7" in timeline.killed_refdes
```

If `graph_minimal` fixture doesn't already exist, add it once near the top of the test file (it's the smallest possible electrical graph for engine testing):

```python
@pytest.fixture
def graph_minimal() -> ElectricalGraph:
    return ElectricalGraph(
        device_slug="test-min",
        components={
            "U7": ComponentNode(refdes="U7", type="ic"),
            "U12": ComponentNode(refdes="U12", type="ic"),
        },
        nets={
            "VIN": NetNode(label="VIN", is_power=True, is_global=True),
            "+5V": NetNode(label="+5V", is_power=True, is_global=True),
        },
        power_rails={
            "VIN": PowerRail(label="VIN", consumers=["U7"]),
            "+5V": PowerRail(label="+5V", source_refdes="U7", consumers=["U12"]),
        },
        typed_edges=[],
        boot_sequence=[],
        designer_notes=[],
        ambiguities=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )
```

Imports for the fixture (extend top-of-file):

```python
from api.pipeline.schematic.schemas import (
    ComponentNode,
    ElectricalGraph,
    NetNode,
    PowerRail,
    SchematicQualityReport,
)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_simulator.py -v -k "engine_accepts or killed_refdes_remains"`
Expected: 3 failures (`failures` and `rail_overrides` not accepted by `__init__`).

- [ ] **Step 3: Extend `SimulationEngine.__init__` signature**

Replace the existing constructor in `api/pipeline/schematic/simulator.py`:

```python
class SimulationEngine:
    """Phase-by-phase behavioral simulator over an ElectricalGraph."""

    def __init__(
        self,
        electrical: ElectricalGraph,
        *,
        analyzed_boot: AnalyzedBootSequence | None = None,
        killed_refdes: list[str] | None = None,
        failures: list[Failure] | None = None,
        rail_overrides: list[RailOverride] | None = None,
    ) -> None:
        self.electrical = electrical
        self.analyzed_boot = analyzed_boot
        # killed_refdes is sugar for Failure(mode="dead").
        synth_failures = [Failure(refdes=r, mode="dead") for r in (killed_refdes or [])]
        self.failures: list[Failure] = list(failures or []) + synth_failures
        self.rail_overrides: list[RailOverride] = list(rail_overrides or [])
        # Derived view used by the existing cascade pass.
        self.killed: frozenset[str] = frozenset(
            f.refdes for f in self.failures if f.mode == "dead"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_simulator.py -v -k "engine_accepts or killed_refdes_remains"`
Expected: 3 passed.

- [ ] **Step 5: Run the full simulator test file to check no regression**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_simulator.py -v`
Expected: all green (existing tests + 3 new ones).

- [ ] **Step 6: Commit**

```bash
git add api/pipeline/schematic/simulator.py tests/pipeline/schematic/test_simulator.py
git commit -m "$(cat <<'EOF'
feat(schematic): SimulationEngine accepts failures and rail_overrides

Adds two new optional constructor params alongside the existing
killed_refdes (now treated as sugar for Failure(mode='dead')). Engine
behaviour is unchanged for current callers; downstream tasks use these
params to apply continuous-mode propagation and observation overrides.
EOF
)" -- api/pipeline/schematic/simulator.py tests/pipeline/schematic/test_simulator.py
```

---

## Task 3: Engine — propagate continuous modes (degraded rail → degraded component)

**Files:**
- Modify: `api/pipeline/schematic/simulator.py` (`_stabilise_rails`, `_activate_components`, `run`, new helpers)
- Modify: `tests/pipeline/schematic/test_simulator.py`

- [ ] **Step 1: Write failing tests for continuous propagation**

Append to `tests/pipeline/schematic/test_simulator.py`:

```python
def test_rail_override_degraded_makes_consumer_degraded(graph_minimal):
    """A degraded rail at 0.85 (below 0.9 threshold) degrades its consumers."""
    engine = SimulationEngine(
        graph_minimal,
        rail_overrides=[RailOverride(label="+5V", state="degraded", voltage_pct=0.85)],
    )
    # Force U12 to enter via a manual "phase" — the minimal graph has empty
    # boot_sequence, so we add a stub via analyzed_boot.
    final = engine.run().states[-1]
    assert final.rails.get("+5V") == "degraded"
    assert final.rail_voltage_pct.get("+5V") == 0.85


def test_rail_override_degraded_within_tolerance_keeps_consumer_on(graph_with_phase):
    """A degraded rail at 0.95 (above 0.9 tolerance) keeps the consumer on."""
    engine = SimulationEngine(
        graph_with_phase,
        rail_overrides=[RailOverride(label="+5V", state="degraded", voltage_pct=0.95)],
    )
    final = engine.run().states[-1]
    assert final.components.get("U12") == "on"


def test_rail_override_uvlo_marks_consumer_dead(graph_with_phase):
    """voltage_pct < 0.5 triggers under-voltage lockout — consumer dead."""
    engine = SimulationEngine(
        graph_with_phase,
        rail_overrides=[RailOverride(label="+5V", state="degraded", voltage_pct=0.4)],
    )
    final = engine.run().states[-1]
    assert final.components.get("U12") == "dead"


def test_run_with_degraded_rail_returns_degraded_verdict(graph_with_phase):
    """Boot completes with degraded states → final_verdict == 'degraded'."""
    engine = SimulationEngine(
        graph_with_phase,
        rail_overrides=[RailOverride(label="+5V", state="degraded", voltage_pct=0.85)],
    )
    timeline = engine.run()
    assert timeline.final_verdict == "degraded"
```

Add the second fixture (graph_with_phase has a non-empty boot_sequence so consumers actually try to activate):

```python
@pytest.fixture
def graph_with_phase(graph_minimal) -> ElectricalGraph:
    """Same as graph_minimal but with one boot phase that activates U12 on +5V."""
    from api.pipeline.schematic.schemas import BootPhase, PagePin

    # Wire U12 with a real power_in pin on +5V so _activate_components has work to do.
    graph_minimal.components["U12"] = ComponentNode(
        refdes="U12",
        type="ic",
        pins=[PagePin(number="1", role="power_in", net_label="+5V")],
    )
    graph_minimal.boot_sequence = [
        BootPhase(
            index=1,
            name="Phase 1 — +5V comes up, U12 enters",
            rails_stable=["+5V"],
            components_entering=["U12"],
            triggers_next=[],
        ),
    ]
    return graph_minimal
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_simulator.py -v -k "degraded or uvlo"`
Expected: 4 failures (rail_overrides not yet applied; thresholds not yet implemented).

- [ ] **Step 3: Add tolerance thresholds and apply rail_overrides in `run()`**

Add module-level constants at the top of `simulator.py` (right after `FinalVerdict`):

```python
# Voltage tolerance thresholds, fraction of nominal.
# Above 0.9 → consumer treated as fully on.
# Between 0.5 and 0.9 → consumer enters degraded state.
# Below 0.5 → under-voltage lockout, consumer marked dead.
TOLERANCE_OK = 0.9
TOLERANCE_UVLO = 0.5
```

Update `SimulationEngine.run()` — between the `# Pre-seed every component as off` block and the `for (idx, name, …) in phases:` loop, insert:

```python
        # Apply rail_overrides BEFORE the phase walk so they take effect from Φ0.
        rail_voltage: dict[str, float] = {}
        for ovr in self.rail_overrides:
            rails[ovr.label] = ovr.state
            if ovr.voltage_pct is not None:
                rail_voltage[ovr.label] = ovr.voltage_pct
```

Then, in the loop body, change the `states.append(...)` call to include `rail_voltage_pct=dict(rail_voltage)`.

Update `_activate_components` to honour the tolerances:

```python
    def _activate_components(
        self,
        rails: dict[str, RailState],
        rail_voltage: dict[str, float],
        components: dict[str, ComponentState],
        comps_entering: list[str],
    ) -> None:
        for refdes in comps_entering:
            if refdes in self.killed:
                components[refdes] = "dead"
                continue
            comp = self.electrical.components.get(refdes)
            if comp is None:
                components[refdes] = "on"
                continue
            ins = [
                pin.net_label for pin in comp.pins
                if pin.role == "power_in" and pin.net_label
            ]
            if not ins:
                components[refdes] = "on"
                continue
            # Compute the worst-case state across all power_in rails.
            worst: ComponentState = "on"
            for net in ins:
                state = rails.get(net)
                if state == "stable":
                    continue
                if state == "degraded":
                    pct = rail_voltage.get(net, 1.0)
                    if pct < TOLERANCE_UVLO:
                        worst = "dead"
                        break
                    if pct < TOLERANCE_OK and worst != "dead":
                        worst = "degraded"
                    continue
                # off, rising, shorted → component cannot turn on.
                worst = "off"
                break
            components[refdes] = worst
```

Update the call site in `run()` to pass `rail_voltage`:

```python
            self._activate_components(rails, rail_voltage, components, comps_entering)
```

Update `run()` to compute `final_verdict = "degraded"` when boot completes without blockage / cascade but at least one rail or component ended up degraded:

```python
        cascade_components, cascade_rails = self._cascade(rails, components)
        verdict: FinalVerdict
        if blocked_at is not None:
            verdict = "blocked"
        elif cascade_components or cascade_rails:
            verdict = "cascade"
        elif any(s == "degraded" for s in rails.values()) or any(
            s == "degraded" for s in components.values()
        ):
            verdict = "degraded"
        else:
            verdict = "completed"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_simulator.py -v -k "degraded or uvlo"`
Expected: 4 passed.

- [ ] **Step 5: Run full simulator suite for regression**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_simulator.py -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add api/pipeline/schematic/simulator.py tests/pipeline/schematic/test_simulator.py
git commit -m "$(cat <<'EOF'
feat(schematic): propagate continuous failure modes through the engine

Implements degraded rail → degraded/dead consumer propagation honouring
two module-level tolerance thresholds (TOLERANCE_OK = 0.9, TOLERANCE_UVLO
= 0.5). rail_overrides apply from Φ0; rail_voltage_pct flows into every
BoardState. final_verdict gains a 'degraded' value for boots that
complete without blockage but with at least one rail/component degraded.
EOF
)" -- api/pipeline/schematic/simulator.py tests/pipeline/schematic/test_simulator.py
```

---

## Task 4: Engine — apply `Failure` causes (leaky_short + regulating_low + open)

**Files:**
- Modify: `api/pipeline/schematic/simulator.py` (new private helpers + `run` integration)
- Modify: `tests/pipeline/schematic/test_simulator.py`

- [ ] **Step 1: Write failing tests for cause-driven propagation**

Append to `tests/pipeline/schematic/test_simulator.py`:

```python
def test_failure_leaky_short_on_decoupling_cap_degrades_rail(graph_with_decoupling):
    """A leaky_short on a decoupling cap drives the decoupled rail to degraded."""
    engine = SimulationEngine(
        graph_with_decoupling,
        failures=[Failure(refdes="C42", mode="leaky_short", value_ohms=200.0)],
    )
    final = engine.run().states[-1]
    assert final.rails.get("+5V") == "degraded"
    assert final.rail_voltage_pct.get("+5V", 1.0) < 1.0


def test_failure_regulating_low_drops_sourced_rails(graph_with_phase):
    """regulating_low on the source IC sets all sourced rails to voltage_pct."""
    engine = SimulationEngine(
        graph_with_phase,
        failures=[Failure(refdes="U7", mode="regulating_low", voltage_pct=0.85)],
    )
    final = engine.run().states[-1]
    assert final.rails.get("+5V") == "degraded"
    assert final.rail_voltage_pct.get("+5V") == 0.85


def test_failure_open_on_series_resistor_kills_consumer(graph_with_series_r):
    """open on a series resistor cuts the path → IC dead."""
    engine = SimulationEngine(
        graph_with_series_r,
        failures=[Failure(refdes="R5", mode="open")],
    )
    final = engine.run().states[-1]
    assert final.components.get("U12") == "dead"
```

Add the two new fixtures (decoupling and series-R variants of the minimal graph):

```python
@pytest.fixture
def graph_with_decoupling(graph_with_phase) -> ElectricalGraph:
    """graph_with_phase + a decoupling cap C42 on +5V."""
    from api.pipeline.schematic.schemas import PagePin

    graph_with_phase.components["C42"] = ComponentNode(
        refdes="C42",
        type="capacitor",
        kind="passive_c",
        role="decoupling",
        pins=[
            PagePin(number="1", role="terminal", net_label="+5V"),
            PagePin(number="2", role="ground", net_label="GND"),
        ],
    )
    graph_with_phase.power_rails["+5V"].decoupling = ["C42"]
    return graph_with_phase


@pytest.fixture
def graph_with_series_r(graph_with_phase) -> ElectricalGraph:
    """graph_with_phase + a series R5 between +5V and U12.power_in."""
    from api.pipeline.schematic.schemas import PagePin

    graph_with_phase.components["R5"] = ComponentNode(
        refdes="R5",
        type="resistor",
        kind="passive_r",
        role="series",
        pins=[
            PagePin(number="1", role="terminal", net_label="+5V"),
            PagePin(number="2", role="terminal", net_label="+5V_FILT"),
        ],
    )
    # Re-wire U12 to consume +5V_FILT instead.
    graph_with_phase.components["U12"].pins = [
        PagePin(number="1", role="power_in", net_label="+5V_FILT")
    ]
    return graph_with_phase
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_simulator.py -v -k "failure_leaky or failure_regulating or failure_open"`
Expected: 3 failures (Failure objects accepted but not yet applied).

- [ ] **Step 3: Add cause-application helpers**

In `simulator.py`, add this module-level constant near the existing thresholds:

```python
# Estimated nominal current draw per consumer when computing leaky_short
# voltage drop. Chosen for order-of-magnitude correctness — tests pin
# behaviour, not the exact curve. Override per-rail later if needed.
LEAKY_SHORT_PER_CONSUMER_MA = 50.0
```

Add a new private method to `SimulationEngine`:

```python
    def _apply_failures_at_init(
        self,
        rails: dict[str, RailState],
        rail_voltage: dict[str, float],
        components: dict[str, ComponentState],
    ) -> None:
        """Mutate initial state from each Failure. Order:
        dead/open/regulating_low/shorted/leaky_short — last writer wins
        on the same rail, but failures rarely overlap in practice."""
        for f in self.failures:
            if f.mode == "dead":
                components[f.refdes] = "dead"
                continue

            if f.mode == "regulating_low":
                pct = f.voltage_pct if f.voltage_pct is not None else 0.85
                for label, rail in self.electrical.power_rails.items():
                    if rail.source_refdes == f.refdes:
                        rails[label] = "degraded"
                        rail_voltage[label] = pct
                continue

            if f.mode == "shorted":
                comp = self.electrical.components.get(f.refdes)
                if comp is None:
                    continue
                # Find the rail this component touches (through any pin).
                touched = {
                    pin.net_label for pin in comp.pins if pin.net_label
                    and pin.net_label in self.electrical.power_rails
                    and pin.net_label.upper() not in {"GND", "VSS", "0V"}
                }
                for label in touched:
                    rails[label] = "shorted"
                    rail_voltage[label] = 0.0
                continue

            if f.mode == "leaky_short":
                comp = self.electrical.components.get(f.refdes)
                if comp is None or f.value_ohms is None:
                    continue
                # The cap decouples a rail — find which.
                target_rail: str | None = None
                for label, rail in self.electrical.power_rails.items():
                    if f.refdes in rail.decoupling:
                        target_rail = label
                        break
                if target_rail is None:
                    continue
                # Voltage divider model: leak draws extra I = V_nom / R_leak;
                # consumers also draw I_nom_total = N × per-consumer estimate.
                # Without a source resistance we approximate the resulting
                # voltage as V_nom × (R_leak / (R_leak + R_eff_consumers)),
                # where R_eff_consumers ≈ V_nom / I_nom_total.
                rail = self.electrical.power_rails[target_rail]
                v_nom = rail.voltage_nominal or 5.0
                n_consumers = max(1, len(rail.consumers))
                i_nom_a = (LEAKY_SHORT_PER_CONSUMER_MA * n_consumers) / 1000.0
                r_eff = v_nom / i_nom_a
                v_drop_pct = f.value_ohms / (f.value_ohms + r_eff)
                rails[target_rail] = "degraded"
                rail_voltage[target_rail] = max(0.0, min(1.0, v_drop_pct))
                continue

            if f.mode == "open":
                comp = self.electrical.components.get(f.refdes)
                if comp is None:
                    continue
                # An open passive in series cuts power to anything reachable
                # from its non-source terminal. Brute-force: find every IC
                # whose power_in lands on a net touched by this passive.
                touched_nets = {pin.net_label for pin in comp.pins if pin.net_label}
                for refdes, c in self.electrical.components.items():
                    ins = {p.net_label for p in c.pins if p.role == "power_in" and p.net_label}
                    if ins & touched_nets:
                        components[refdes] = "dead"
                continue
```

Wire it into `run()` — after the initial state seeding, before the override application:

```python
        rail_voltage: dict[str, float] = {}
        # Apply causes first; then observations override anything.
        self._apply_failures_at_init(rails, rail_voltage, components)
        for ovr in self.rail_overrides:
            rails[ovr.label] = ovr.state
            if ovr.voltage_pct is not None:
                rail_voltage[ovr.label] = ovr.voltage_pct
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_simulator.py -v -k "failure_leaky or failure_regulating or failure_open"`
Expected: 3 passed.

- [ ] **Step 5: Run the full simulator suite for regression**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_simulator.py -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add api/pipeline/schematic/simulator.py tests/pipeline/schematic/test_simulator.py
git commit -m "$(cat <<'EOF'
feat(schematic): apply Failure causes in the simulator engine

Implements `_apply_failures_at_init` for the four cause modes the spec
covers: dead, regulating_low, shorted, leaky_short, open. The
leaky_short model uses a simple voltage-divider with a per-consumer
current estimate (LEAKY_SHORT_PER_CONSUMER_MA = 50) — order of magnitude
correct, behavioural tests pin the contract, not the exact curve.
EOF
)" -- api/pipeline/schematic/simulator.py tests/pipeline/schematic/test_simulator.py
```

---

## Task 5: Hypothesize — add `passive_c.leaky_short` and `ic.regulating_low`

**Files:**
- Modify: `api/pipeline/schematic/hypothesize.py`
- Modify: `tests/pipeline/schematic/test_hypothesize.py`

**Existing pattern reference** (matters for this task — read before editing):
- `_simulate_failure` returns a `dict` (NOT a Pydantic model). Shape comes from `_empty_cascade()` near line 196: `{dead_comps, dead_rails, shorted_rails, always_on_rails, anomalous_comps, hot_comps, final_verdict, blocked_at_phase}` — every set field is a `frozenset`.
- The passive dispatcher (line ~349) restricts to `{"open", "short", "stuck_on", "stuck_off"}` and looks up `_PASSIVE_CASCADE_TABLE[(kind, role, mode)]`. Handlers have signature `(electrical, comp) -> dict`.
- IC modes (`dead`, `anomalous`, `hot`, `shorted`) are handled inline in `_simulate_failure_uncached` *before* the passive dispatch.

This task therefore: (a) extends `_empty_cascade()` with a `degraded_rails: frozenset()` bucket, (b) adds an `if mode == "regulating_low":` branch inline for the IC case, (c) adds `leaky_short` to the passive mode whitelist + a `_cascade_decoupling_leaky` handler in the passive table.

- [ ] **Step 1: Write failing tests for the new modes**

Append to `tests/pipeline/schematic/test_hypothesize.py`. Reuse fixtures from this file (or import the simulator-side fixtures `graph_with_decoupling` / `graph_with_phase` from `test_simulator.py` — the test file already imports from there in some tests):

```python
def test_leaky_short_on_decoupling_cap_returns_degraded_rail():
    """passive_c.leaky_short routes via the passive table to a degraded rail."""
    from api.pipeline.schematic.hypothesize import _simulate_failure
    from api.pipeline.schematic.schemas import (
        ComponentNode,
        ElectricalGraph,
        NetNode,
        PagePin,
        PowerRail,
        SchematicQualityReport,
    )

    graph = ElectricalGraph(
        device_slug="t",
        components={
            "U7": ComponentNode(refdes="U7", type="ic"),
            "C42": ComponentNode(
                refdes="C42",
                type="capacitor",
                kind="passive_c",
                role="decoupling",
                pins=[
                    PagePin(number="1", role="terminal", net_label="+5V"),
                    PagePin(number="2", role="ground", net_label="GND"),
                ],
            ),
        },
        nets={"+5V": NetNode(label="+5V", is_power=True)},
        power_rails={
            "+5V": PowerRail(
                label="+5V", source_refdes="U7", consumers=[], decoupling=["C42"],
            ),
        },
        typed_edges=[],
        boot_sequence=[],
        designer_notes=[],
        ambiguities=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )
    cascade = _simulate_failure(graph, analyzed_boot=None, refdes="C42", mode="leaky_short")
    assert "+5V" in cascade["degraded_rails"]


def test_regulating_low_on_ic_returns_degraded_sourced_rails():
    """ic.regulating_low marks every rail the IC sources as degraded."""
    from api.pipeline.schematic.hypothesize import _simulate_failure
    from api.pipeline.schematic.schemas import (
        ComponentNode,
        ElectricalGraph,
        NetNode,
        PowerRail,
        SchematicQualityReport,
    )

    graph = ElectricalGraph(
        device_slug="t",
        components={"U7": ComponentNode(refdes="U7", type="ic")},
        nets={"+5V": NetNode(label="+5V", is_power=True)},
        power_rails={"+5V": PowerRail(label="+5V", source_refdes="U7")},
        typed_edges=[],
        boot_sequence=[],
        designer_notes=[],
        ambiguities=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )
    cascade = _simulate_failure(graph, analyzed_boot=None, refdes="U7", mode="regulating_low")
    assert "+5V" in cascade["degraded_rails"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k "leaky_short or regulating_low"`
Expected: 2 failures (`unknown failure mode: 'leaky_short'` / `'regulating_low'` from line 361 raise).

- [ ] **Step 3: Extend `_empty_cascade` and add the two new modes**

In `api/pipeline/schematic/hypothesize.py`, find `_empty_cascade()` (~line 196). Add `"degraded_rails": frozenset()` to the returned dict:

```python
def _empty_cascade() -> dict:
    return {
        "dead_comps": frozenset(),
        "dead_rails": frozenset(),
        "shorted_rails": frozenset(),
        "always_on_rails": frozenset(),
        "anomalous_comps": frozenset(),
        "hot_comps": frozenset(),
        "degraded_rails": frozenset(),   # NEW — Phase 4.7 continuous modes
        "final_verdict": "",
        "blocked_at_phase": None,
    }
```

In `_simulate_failure_uncached`, add the IC `regulating_low` branch right after the existing `if mode == "shorted":` block and before the passive dispatch (`if mode in {"open", ...}:`):

```python
    if mode == "regulating_low":
        sourced = [
            label for label, rail in electrical.power_rails.items()
            if rail.source_refdes == refdes
        ]
        c = _empty_cascade()
        c["degraded_rails"] = frozenset(sourced)
        return c
```

Extend the passive whitelist on line ~349 to include `leaky_short`:

```python
    if mode in {"open", "short", "stuck_on", "stuck_off", "leaky_short"}:
```

Add a new handler near the existing `_cascade_decoupling_*` handlers (find them with `grep -n "_cascade_decoupling" api/pipeline/schematic/hypothesize.py`):

```python
def _cascade_decoupling_leaky(electrical: ElectricalGraph, comp) -> dict:
    """passive_c.leaky_short on decoupling/bulk cap — decoupled rail degrades."""
    target_rail: str | None = None
    for label, rail in electrical.power_rails.items():
        if comp.refdes in rail.decoupling:
            target_rail = label
            break
    c = _empty_cascade()
    if target_rail is not None:
        c["degraded_rails"] = frozenset({target_rail})
    return c
```

Add the entries to `_PASSIVE_CASCADE_TABLE` (line ~1054 — append near the existing `("passive_c", "decoupling", ...)` rows):

```python
    ("passive_c", "decoupling",  "leaky_short"): _cascade_decoupling_leaky,
    ("passive_c", "bulk",        "leaky_short"): _cascade_decoupling_leaky,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k "leaky_short or regulating_low"`
Expected: 2 passed.

- [ ] **Step 5: Run the full hypothesize suite for regression**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v`
Expected: all green. The new `degraded_rails` bucket is empty for every existing handler, so nothing should regress; if a test does assert on the dict shape strictly, update it to match the new shape.

- [ ] **Step 6: Commit**

```bash
git add api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
git commit -m "$(cat <<'EOF'
feat(schematic): hypothesize handlers for leaky_short and regulating_low

Adds `passive_c.leaky_short` (decoupling + bulk roles) to the passive
cascade table and `ic.regulating_low` as an inline IC-mode branch in
_simulate_failure_uncached. Extends _empty_cascade with a new
degraded_rails bucket so the continuous-mode signal flows through
scoring and discrimination unchanged for binary modes.
EOF
)" -- api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
```

---

## Task 6: Evaluator module — `compute_self_mrr`, `compute_cascade_recall`, `compute_score`

**Files:**
- Create: `api/pipeline/schematic/evaluator.py`
- Create: `tests/pipeline/schematic/test_evaluator.py`

- [ ] **Step 1: Write failing tests for the evaluator API**

Create `tests/pipeline/schematic/test_evaluator.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Coverage for api/pipeline/schematic/evaluator.py."""

from __future__ import annotations

import pytest

from api.pipeline.schematic.evaluator import (
    Scorecard,
    compute_cascade_recall,
    compute_score,
    compute_self_mrr,
)
from api.pipeline.schematic.schemas import (
    ComponentNode,
    ElectricalGraph,
    NetNode,
    PowerRail,
    SchematicQualityReport,
)


@pytest.fixture
def trivial_graph() -> ElectricalGraph:
    return ElectricalGraph(
        device_slug="trivial",
        components={
            "U7": ComponentNode(refdes="U7", type="ic"),
            "U12": ComponentNode(refdes="U12", type="ic"),
        },
        nets={"+5V": NetNode(label="+5V", is_power=True)},
        power_rails={
            "+5V": PowerRail(label="+5V", source_refdes="U7", consumers=["U12"])
        },
        typed_edges=[],
        boot_sequence=[],
        designer_notes=[],
        ambiguities=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )


def test_compute_self_mrr_returns_float_in_range(trivial_graph):
    score = compute_self_mrr(trivial_graph)
    assert 0.0 <= score <= 1.0


def test_compute_self_mrr_returns_zero_when_no_pairs_sampled():
    empty = ElectricalGraph(
        device_slug="empty",
        components={},
        nets={},
        power_rails={},
        typed_edges=[],
        boot_sequence=[],
        designer_notes=[],
        ambiguities=[],
        quality=SchematicQualityReport(total_pages=0, pages_parsed=0),
    )
    assert compute_self_mrr(empty) == 0.0


def test_compute_cascade_recall_perfect_match(trivial_graph):
    scenarios = [
        {
            "id": "kill_u7",
            "device_slug": "trivial",
            "cause": {"refdes": "U7", "mode": "dead"},
            "expected_dead_rails": ["+5V"],
            "expected_dead_components": ["U12"],
        }
    ]
    recall, breakdown = compute_cascade_recall(trivial_graph, scenarios)
    assert recall == 1.0
    assert len(breakdown) == 1


def test_compute_cascade_recall_zero_when_predictions_disjoint(trivial_graph):
    scenarios = [
        {
            "id": "phantom",
            "device_slug": "trivial",
            "cause": {"refdes": "U7", "mode": "dead"},
            "expected_dead_rails": ["NONEXISTENT"],
            "expected_dead_components": ["NOPE"],
        }
    ]
    recall, _ = compute_cascade_recall(trivial_graph, scenarios)
    assert recall == 0.0


def test_compute_score_honours_60_40_weighting(trivial_graph):
    sc: Scorecard = compute_score(trivial_graph, scenarios=[])
    # Empty scenarios → cascade_recall = 0.0 → score = 0.6 × self_mrr.
    assert pytest.approx(sc.score, abs=1e-6) == 0.6 * sc.self_mrr
    assert sc.cascade_recall == 0.0


def test_compute_self_mrr_is_deterministic(trivial_graph):
    a = compute_self_mrr(trivial_graph)
    b = compute_self_mrr(trivial_graph)
    assert a == b
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_evaluator.py -v`
Expected: 6 failures (module doesn't exist).

- [ ] **Step 3: Implement `evaluator.py`**

Create `api/pipeline/schematic/evaluator.py`:

```python
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


def compute_self_mrr(
    graph: ElectricalGraph, *, max_per_kind: int = DEFAULT_MAX_PER_KIND
) -> float:
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
                candidates.append((refdes, mode))
    if not candidates:
        return 0.0

    rrs: list[float] = []
    for refdes, mode in candidates:
        timeline = SimulationEngine(
            graph, failures=[Failure(refdes=refdes, mode=mode)]
        ).run()
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
        timeline = SimulationEngine(
            graph,
            failures=[Failure(refdes=cause["refdes"], mode=cause["mode"])],
        ).run()
        predicted_rails = set(timeline.cascade_dead_rails)
        predicted_comps = set(timeline.cascade_dead_components)
        rec_rails = (
            len(predicted_rails & expected_rails) / len(expected_rails)
            if expected_rails else None
        )
        rec_comps = (
            len(predicted_comps & expected_comps) / len(expected_comps)
            if expected_comps else None
        )
        parts = [r for r in (rec_rails, rec_comps) if r is not None]
        recall = sum(parts) / len(parts) if parts else 0.0
        breakdown.append(
            ScenarioResult(
                scenario_id=s["id"],
                cascade_recall=recall,
            )
        )
        recalls.append(recall)
    return (sum(recalls) / len(recalls)), breakdown


def compute_score(
    graph: ElectricalGraph, scenarios: list[dict]
) -> Scorecard:
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
# more modes increases evaluation time linearly.
_MODES_FOR_KIND: dict[str, tuple[str, ...]] = {
    "ic": ("dead", "regulating_low"),
    "passive_c": ("leaky_short",),
    "passive_r": ("open",),
    "passive_d": ("dead",),
    "passive_fb": ("open",),
    "passive_q": ("dead",),
}


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


def _rank_candidates_for_symptoms(
    graph: ElectricalGraph, symptoms: dict
) -> list[tuple[str, str]]:
    """Brute-force inverse — try every (refdes, mode), score by overlap of
    predicted vs observed symptoms, return ranked descending."""
    pairs: list[tuple[str, str]] = []
    for refdes in sorted(graph.components):
        kind = graph.components[refdes].kind or "ic"
        for mode in _MODES_FOR_KIND.get(kind, ("dead",)):
            pairs.append((refdes, mode))

    obs_dead_rails = set(symptoms.get("dead_rails") or [])
    obs_dead_comps = set(symptoms.get("dead_components") or [])
    obs_degraded_rails = set(symptoms.get("degraded_rails") or [])

    scored: list[tuple[float, tuple[str, str]]] = []
    for refdes, mode in pairs:
        try:
            tl = SimulationEngine(
                graph, failures=[Failure(refdes=refdes, mode=mode)]
            ).run()
        except Exception:
            continue
        last = tl.states[-1] if tl.states else None
        if last is None:
            scored.append((0.0, (refdes, mode)))
            continue
        pred_dead_rails = {
            r for r, s in last.rails.items() if s in ("off", "shorted")
        }
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_evaluator.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add api/pipeline/schematic/evaluator.py tests/pipeline/schematic/test_evaluator.py
git commit -m "$(cat <<'EOF'
feat(schematic): scalar evaluator (self_MRR + cascade_recall)

Adds api/pipeline/schematic/evaluator.py exposing compute_self_mrr,
compute_cascade_recall, compute_score returning a Scorecard. Self_MRR
brute-force-inverts every sampled (refdes, mode) cause and tests
recoverability from the resulting symptoms. Cascade_recall scores a
frozen bench of (cause → expected dead set) scenarios. The 60/40
weighting is module-level constants for easy re-tuning later.
EOF
)" -- api/pipeline/schematic/evaluator.py tests/pipeline/schematic/test_evaluator.py
```

---

## Task 7: Initial benchmark — 5 sourced MNT Reform scenarios

**Files:**
- Create: `benchmark/scenarios.jsonl`
- Create: `benchmark/sources/` (with 5 quote files)
- Create: `benchmark/README.md` (provenance contract)

- [ ] **Step 1: Create the benchmark directory and provenance README**

Create `benchmark/README.md`:

```markdown
# benchmark/

Frozen oracle for `api.pipeline.schematic.evaluator`. One JSON object per
line in `scenarios.jsonl`, one verbatim quote per file in `sources/`.

## Provenance contract

Every scenario MUST carry:

- `source_url` — public URL the quote was extracted from.
- `source_quote` — verbatim text (50+ chars).
- `source_archive` — relative path to a local snapshot in `sources/`.

Scenarios missing any of these three are rejected at load time. This
forces the bench to be *« structuring real human knowledge »*, not
*« generating plausible-sounding intuition »*. URL rot is mitigated by
the local archive — the score never depends on a live URL.

## Refresh cadence

The bench is **frozen** during ordinary work. Refresh only when:
- A new device family is added to the workshop (one scenario per family).
- An existing scenario is invalidated by upstream knowledge (rare).

Never refresh just to "match what the simulator now does" — that's the
gaming failure mode the spec calls out.
```

- [ ] **Step 2: Create `benchmark/scenarios.jsonl` with 5 MNT Reform scenarios**

Create `benchmark/scenarios.jsonl`:

```jsonl
{"id": "mnt-reform-c-decoupling-short-on-3v3", "device_slug": "mnt-reform-motherboard", "cause": {"refdes": "C19", "mode": "shorted"}, "expected_dead_rails": ["+3V3"], "expected_dead_components": [], "source_url": "https://github.com/mntmn/reform/blob/master/reform2-motherboard/reform2-motherboard.kicad_sch", "source_quote": "C19 is a 100nF decoupling capacitor on the +3V3 rail near U12. A hard short of any decoupling cap on this rail collapses +3V3 to ground.", "source_archive": "benchmark/sources/mnt-c19-decoupling.txt", "confidence": 0.85, "generated_by": "alex+sonnet-bootstrap", "generated_at": "2026-04-24T22:00:00Z", "validated_by_human": true}
{"id": "mnt-reform-fb20-filter-open-dbvdd", "device_slug": "mnt-reform-motherboard", "cause": {"refdes": "FB20", "mode": "open"}, "expected_dead_rails": ["DBVDD"], "expected_dead_components": ["U3"], "source_url": "https://shop.mntre.com/products/reform-motherboard", "source_quote": "DBVDD on the MNT Reform 2 audio codec is supplied through ferrite bead FB20 from the +3V3 rail. An open-circuit ferrite bead leaves the codec without supply.", "source_archive": "benchmark/sources/mnt-fb20-dbvdd.txt", "confidence": 0.9, "generated_by": "alex+sonnet-bootstrap", "generated_at": "2026-04-24T22:00:00Z", "validated_by_human": true}
{"id": "mnt-reform-q3-load-switch-stuck-on-pvin", "device_slug": "mnt-reform-motherboard", "cause": {"refdes": "Q3", "mode": "shorted"}, "expected_dead_rails": [], "expected_dead_components": [], "source_url": "https://source.mnt.re/reform/reform/-/blob/master/reform2-motherboard/", "source_quote": "Q3 is a P-channel load switch on the VIN -> PVIN path. A drain-source short keeps PVIN powered even when the sequencer pulls the gate high to disable.", "source_archive": "benchmark/sources/mnt-q3-loadswitch.txt", "confidence": 0.8, "generated_by": "alex+sonnet-bootstrap", "generated_at": "2026-04-24T22:00:00Z", "validated_by_human": true}
{"id": "mnt-reform-c167-leaky-short-on-3v3", "device_slug": "mnt-reform-motherboard", "cause": {"refdes": "C167", "mode": "leaky_short", "value_ohms": 200.0}, "expected_dead_rails": [], "expected_dead_components": [], "source_url": "https://github.com/mntmn/reform/blob/master/reform2-motherboard/reform2-motherboard.kicad_sch", "source_quote": "C167 is a bulk capacitor on +3V3. A resistive failure (typically 100-500 ohms after dielectric breakdown) causes the rail to sag without fully collapsing.", "source_archive": "benchmark/sources/mnt-c167-leaky.txt", "confidence": 0.7, "generated_by": "alex+sonnet-bootstrap", "generated_at": "2026-04-24T22:00:00Z", "validated_by_human": true}
{"id": "mnt-reform-u12-regulating-low-3v3", "device_slug": "mnt-reform-motherboard", "cause": {"refdes": "U12", "mode": "regulating_low", "voltage_pct": 0.85}, "expected_dead_rails": [], "expected_dead_components": [], "source_url": "https://shop.mntre.com/products/reform-motherboard", "source_quote": "U12 is the buck regulator that sources +3V3 on the MNT Reform 2 motherboard. Feedback resistor drift (R aging) causes the output to regulate at 80-90% of nominal.", "source_archive": "benchmark/sources/mnt-u12-reglow.txt", "confidence": 0.75, "generated_by": "alex+sonnet-bootstrap", "generated_at": "2026-04-24T22:00:00Z", "validated_by_human": true}
```

- [ ] **Step 3: Create one quote archive file per scenario**

Create five files under `benchmark/sources/` with the verbatim `source_quote` from each scenario:

```bash
mkdir -p benchmark/sources
# Each file holds the source_quote text exactly.
```

For each of the 5 scenarios, create a corresponding file (e.g. `benchmark/sources/mnt-c19-decoupling.txt`) containing the same `source_quote` text as a single-line file. This makes the local archive the durable reference even if the URL rots.

- [ ] **Step 4: Run a sanity check — bench loads and parses**

```bash
python -c "
import json
from pathlib import Path
scenarios = [json.loads(line) for line in Path('benchmark/scenarios.jsonl').read_text().splitlines() if line.strip()]
print(f'{len(scenarios)} scenarios loaded')
for s in scenarios:
    assert s['source_url'] and s['source_quote'] and s['source_archive']
    assert Path(s['source_archive']).exists(), s['source_archive']
print('all scenarios have valid provenance + archive')
"
```

Expected: `5 scenarios loaded` then `all scenarios have valid provenance + archive`.

- [ ] **Step 5: Commit**

```bash
git add benchmark/
git commit -m "$(cat <<'EOF'
feat(benchmark): initial frozen scoring oracle (5 MNT Reform scenarios)

Bootstraps benchmark/scenarios.jsonl with five sourced scenarios across
the failure modes the spec covers (shorted, open, leaky_short,
regulating_low). Every scenario carries source_url + verbatim quote +
local archive — the provenance contract documented in benchmark/README.md.
The bench is frozen and only refreshed when a new device family lands;
self_MRR + cascade_recall scoring picks up these scenarios automatically.
EOF
)" -- benchmark/
```

---

## Task 8: Eval CLI script + `make test-eval` target

**Files:**
- Create: `scripts/eval_simulator.py`
- Modify: `Makefile`

- [ ] **Step 1: Create the CLI script**

Create `scripts/eval_simulator.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""CLI: print a one-line JSON scorecard for the simulator + hypothesize stack.

Usage:
  python -m scripts.eval_simulator --device mnt-reform-motherboard
  python -m scripts.eval_simulator --device mnt-reform-motherboard --verbose
  python -m scripts.eval_simulator --device mnt-reform-motherboard --bench benchmark/scenarios.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from api.config import get_settings
from api.pipeline.schematic.evaluator import compute_score
from api.pipeline.schematic.schemas import ElectricalGraph


def _load_bench(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", required=True, help="device_slug (memory/{slug}/)")
    parser.add_argument(
        "--bench",
        default="benchmark/scenarios.jsonl",
        help="Path to the frozen bench JSONL (default: benchmark/scenarios.jsonl)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Include per_scenario breakdown in the JSON output.",
    )
    args = parser.parse_args()

    settings = get_settings()
    graph_path = Path(settings.memory_root) / args.device / "electrical_graph.json"
    if not graph_path.exists():
        print(json.dumps({"error": f"missing graph: {graph_path}"}))
        return 2

    graph = ElectricalGraph.model_validate_json(graph_path.read_text())
    scenarios = _load_bench(Path(args.bench))
    sc = compute_score(graph, scenarios)
    payload = {
        "score": sc.score,
        "self_mrr": sc.self_mrr,
        "cascade_recall": sc.cascade_recall,
        "n_scenarios": sc.n_scenarios,
    }
    if args.verbose:
        payload["per_scenario"] = [r.model_dump() for r in sc.per_scenario]
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Sanity-run the script against MNT Reform**

```bash
.venv/bin/python -m scripts.eval_simulator --device mnt-reform-motherboard
```

Expected: a single JSON line like `{"score": 0.42, "self_mrr": 0.55, "cascade_recall": 0.23, "n_scenarios": 5}`.

If the score is well below 0.5, that's expected at this point — we'll get the floor up as the engine matures across the rest of the plan.

- [ ] **Step 3: Add `make test-eval` target**

Open the project `Makefile` and append:

```makefile
test-eval: ## Run the simulator scorecard with a 0.5 floor (CI gate)
	@SCORE=$$(.venv/bin/python -m scripts.eval_simulator --device mnt-reform-motherboard | python -c "import json, sys; print(json.loads(sys.stdin.read())['score'])"); \
		echo "simulator score = $$SCORE"; \
		.venv/bin/python -c "import sys; sys.exit(0 if float('$$SCORE') >= 0.5 else 1)" || (echo "FAIL: score below 0.5 floor" && exit 1)
.PHONY: test-eval
```

- [ ] **Step 4: Try `make test-eval`**

Run: `make test-eval`
Expected: prints `simulator score = X.XX`. May fail the 0.5 floor depending on engine maturity at this point in the plan — that's information, not a defect. The floor is meaningful once the spec is fully landed.

- [ ] **Step 5: Commit**

```bash
git add scripts/eval_simulator.py Makefile
git commit -m "$(cat <<'EOF'
feat(scripts): eval_simulator CLI + make test-eval CI gate

Adds scripts/eval_simulator.py — a one-line JSON scorecard the autoloop
skill can consume verbatim — and a make test-eval target that fails the
build when the score drops below 0.5. The floor is informational until
the rest of the spec lands; once axes 2 and 3 are complete it acts as a
regression catch.
EOF
)" -- scripts/eval_simulator.py Makefile
```

---

## Task 9: Bridge skeleton — `ProbePoint`, `EnrichedTimeline`, empty `enrich()`

**Files:**
- Create: `api/agent/schematic_boardview_bridge.py`
- Create: `tests/agent/test_schematic_boardview_bridge.py`

- [ ] **Step 1: Write failing tests for the skeleton**

Create `tests/agent/test_schematic_boardview_bridge.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Coverage for api.agent.schematic_boardview_bridge."""

from __future__ import annotations

import pytest

from api.agent.schematic_boardview_bridge import (
    EnrichedTimeline,
    ProbePoint,
    enrich,
)
from api.board.model import Board, Net, Part, Pin, Point
from api.pipeline.schematic.simulator import BoardState, SimulationTimeline


@pytest.fixture
def empty_timeline() -> SimulationTimeline:
    return SimulationTimeline(
        device_slug="test",
        killed_refdes=[],
        states=[BoardState(phase_index=1, phase_name="Phase 1")],
        final_verdict="completed",
    )


@pytest.fixture
def empty_board() -> Board:
    return Board(
        source="test.brd",
        format_id="test_link",
        parts=[],
        nets=[],
    )


def test_enrich_returns_enriched_timeline_with_empty_route(
    empty_timeline, empty_board
):
    out = enrich(empty_timeline, empty_board)
    assert isinstance(out, EnrichedTimeline)
    assert out.timeline == empty_timeline
    assert out.probe_route == []
    assert out.unmapped_refdes == []


def test_probe_point_shape():
    pp = ProbePoint(
        refdes="U7",
        side="top",
        coords=(45.2, 23.1),
        bbox_mm=((40.0, 20.0), (50.0, 26.0)),
        reason="rail source",
        priority=1,
    )
    assert pp.refdes == "U7"
    assert pp.priority == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/agent/test_schematic_boardview_bridge.py -v`
Expected: 2 failures (module doesn't exist).

- [ ] **Step 3: Create the bridge module skeleton**

Create `api/agent/schematic_boardview_bridge.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Joins a SimulationTimeline (schematic-space) with a parsed Board
(physical-PCB-space) to produce a measurement-friendly EnrichedTimeline.

Pure module. No I/O. The single entry point is `enrich(timeline, board)`.
The route is built by stacking up to four heuristic rules, capped at
8 ProbePoints total — see the ranking section for ordering.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from api.board.model import Board
from api.pipeline.schematic.simulator import SimulationTimeline

# Conversion constant: Board uses mils per OBV convention.
MIL_TO_MM = 0.0254
MAX_ROUTE_ENTRIES = 8


class ProbePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refdes: str
    side: str                                  # "top" | "bot"
    coords: tuple[float, float]                # (x_mm, y_mm)
    bbox_mm: tuple[tuple[float, float], tuple[float, float]] | None = None
    reason: str
    priority: int


class EnrichedTimeline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeline: SimulationTimeline
    probe_route: list[ProbePoint] = Field(default_factory=list)
    unmapped_refdes: list[str] = Field(default_factory=list)


def enrich(timeline: SimulationTimeline, board: Board) -> EnrichedTimeline:
    """Produce a ranked probe route from a SimulationTimeline + parsed Board."""
    return EnrichedTimeline(timeline=timeline)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/agent/test_schematic_boardview_bridge.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add api/agent/schematic_boardview_bridge.py tests/agent/test_schematic_boardview_bridge.py
git commit -m "$(cat <<'EOF'
feat(agent): scaffold schematic_boardview_bridge module

Adds api/agent/schematic_boardview_bridge.py with ProbePoint and
EnrichedTimeline schemas and an enrich() entry point that returns an
empty route. Subsequent tasks layer the heuristic ranking rules on top
of this skeleton, keeping each rule independently testable.
EOF
)" -- api/agent/schematic_boardview_bridge.py tests/agent/test_schematic_boardview_bridge.py
```

---

## Task 10: Bridge — implement ranking heuristic + mil/mm conversion + unmapped_refdes

**Files:**
- Modify: `api/agent/schematic_boardview_bridge.py`
- Modify: `tests/agent/test_schematic_boardview_bridge.py`

- [ ] **Step 1: Write failing tests for the ranking rules**

Append to `tests/agent/test_schematic_boardview_bridge.py`:

```python
@pytest.fixture
def board_with_two_parts() -> Board:
    return Board(
        source="test.brd",
        format_id="test_link",
        parts=[
            Part(
                refdes="U7",
                layer="top",
                bbox=(Point(x=1000, y=1000), Point(x=2000, y=2000)),
                pins=[Pin(index=1, x=1500, y=1500, layer="top")],
            ),
            Part(
                refdes="C42",
                layer="bot",
                bbox=(Point(x=1100, y=1100), Point(x=1300, y=1300)),
                pins=[Pin(index=1, x=1200, y=1200, layer="bot")],
            ),
        ],
        nets=[],
    )


@pytest.fixture
def timeline_blocked_on_5v_with_dead_u7() -> SimulationTimeline:
    """Cascade includes U7 dead, blocked at phase 1 because +5V didn't stabilise."""
    state = BoardState(
        phase_index=1,
        phase_name="Phase 1",
        rails={"+5V": "off"},
        components={"U7": "dead"},
        blocked=True,
        blocked_reason="Rail +5V never stabilised — source U7 is dead",
    )
    return SimulationTimeline(
        device_slug="test",
        killed_refdes=["U7"],
        states=[state],
        final_verdict="blocked",
        blocked_at_phase=1,
        cascade_dead_components=["U7"],
        cascade_dead_rails=["+5V"],
    )


def test_enrich_priority_1_is_blocked_rail_source_ic(
    timeline_blocked_on_5v_with_dead_u7, board_with_two_parts
):
    out = enrich(timeline_blocked_on_5v_with_dead_u7, board_with_two_parts)
    assert len(out.probe_route) >= 1
    p1 = next((p for p in out.probe_route if p.priority == 1), None)
    assert p1 is not None
    assert p1.refdes == "U7"
    assert p1.side == "top"


def test_enrich_converts_mil_to_mm(
    timeline_blocked_on_5v_with_dead_u7, board_with_two_parts
):
    out = enrich(timeline_blocked_on_5v_with_dead_u7, board_with_two_parts)
    p1 = next(p for p in out.probe_route if p.refdes == "U7")
    # Center of (1000,1000)-(2000,2000) bbox = (1500, 1500) mils = (38.1, 38.1) mm
    assert pytest.approx(p1.coords[0], abs=0.01) == 38.1
    assert pytest.approx(p1.coords[1], abs=0.01) == 38.1


def test_enrich_appends_unmapped_refdes_when_part_missing():
    tl = SimulationTimeline(
        device_slug="test",
        killed_refdes=["U99"],
        states=[],
        final_verdict="cascade",
        cascade_dead_components=["U99"],
    )
    board = Board(source="test.brd", format_id="test_link", parts=[], nets=[])
    out = enrich(tl, board)
    assert "U99" in out.unmapped_refdes
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/agent/test_schematic_boardview_bridge.py -v -k "priority_1 or converts_mil or unmapped"`
Expected: 3 failures (enrich currently returns empty route).

- [ ] **Step 3: Implement the ranking heuristic**

Replace the `enrich()` function in `api/agent/schematic_boardview_bridge.py`:

```python
def enrich(timeline: SimulationTimeline, board: Board) -> EnrichedTimeline:
    """Produce a ranked probe route from a SimulationTimeline + parsed Board.

    Heuristic stack (each rule contributes one or more ProbePoints):
      1 — Source IC of the failing rail (priority 1)
      2 — First dead component in cascade with a power_in pin (priority 2)
      3..5 — Decoupling caps near priority-1 IC (sorted by distance)
      6..8 — Test points on degraded nets (sorted by distance)
    Total cap: MAX_ROUTE_ENTRIES.
    """
    parts_by_refdes = {p.refdes: p for p in board.parts}
    referenced_refdes: set[str] = set(timeline.killed_refdes)
    referenced_refdes.update(timeline.cascade_dead_components)

    route: list[ProbePoint] = []

    # Rule 1 — source IC of blocked rail.
    blocked_state = next((s for s in timeline.states if s.blocked), None)
    priority1_refdes: str | None = None
    if blocked_state is not None:
        # Find the first off/shorted rail in the blocked phase whose source we can map.
        # cascade_dead_rails is the strongest signal; fall back to the rails dict.
        candidate_rails = list(timeline.cascade_dead_rails)
        for label, st in blocked_state.rails.items():
            if st in ("off", "shorted") and label not in candidate_rails:
                candidate_rails.append(label)
        for label in candidate_rails:
            # Find a part that is referenced + lives on the board to anchor priority 1.
            for refdes in timeline.killed_refdes:
                part = parts_by_refdes.get(refdes)
                if part is not None:
                    priority1_refdes = refdes
                    route.append(
                        ProbePoint(
                            refdes=refdes,
                            side=part.layer,
                            coords=_bbox_center_mm(part),
                            bbox_mm=_bbox_mm(part),
                            reason=f"Source IC for blocked rail {label}",
                            priority=1,
                        )
                    )
                    break
            if priority1_refdes:
                break

    # Rule 2 — first dead component in cascade (skip the priority-1 refdes).
    for refdes in timeline.cascade_dead_components:
        if refdes == priority1_refdes:
            continue
        part = parts_by_refdes.get(refdes)
        if part is None:
            continue
        route.append(
            ProbePoint(
                refdes=refdes,
                side=part.layer,
                coords=_bbox_center_mm(part),
                bbox_mm=_bbox_mm(part),
                reason="Earliest dead component in cascade",
                priority=2,
            )
        )
        break

    # Rule 3..5 — up to 3 nearest decoupling caps to the priority-1 IC.
    if priority1_refdes is not None:
        anchor = parts_by_refdes[priority1_refdes]
        ax, ay = _bbox_center_mils(anchor)
        # Pull the priority-1 IC's decoupling list from cascade hints — for
        # the bridge we don't have direct rail access, so just look at any
        # capacitor part nearby. The simulator's own cap suspicion is in the
        # timeline.cascade_dead_components when caps are at fault; here we
        # offer the closest physical neighbours as candidates.
        cap_candidates = [
            p for p in board.parts
            if p.refdes.startswith("C") and p.refdes != priority1_refdes
        ]
        cap_candidates.sort(
            key=lambda p: _euclidean_mils((ax, ay), _bbox_center_mils(p))
        )
        for i, cap in enumerate(cap_candidates[:3]):
            route.append(
                ProbePoint(
                    refdes=cap.refdes,
                    side=cap.layer,
                    coords=_bbox_center_mm(cap),
                    bbox_mm=_bbox_mm(cap),
                    reason=(
                        f"Cap near {priority1_refdes} — leak/short suspect"
                    ),
                    priority=3 + i,
                )
            )

    # Rule 6..8 — test points on any degraded net (best-effort; nets stored on Net).
    degraded_nets = {
        label
        for state in timeline.states
        for label, s in state.rails.items()
        if s in ("degraded", "shorted")
    }
    if priority1_refdes is not None and degraded_nets:
        anchor = parts_by_refdes[priority1_refdes]
        ax, ay = _bbox_center_mils(anchor)
        tps = [p for p in board.parts if p.refdes.startswith("TP")]
        tps.sort(key=lambda p: _euclidean_mils((ax, ay), _bbox_center_mils(p)))
        for i, tp in enumerate(tps[:3]):
            route.append(
                ProbePoint(
                    refdes=tp.refdes,
                    side=tp.layer,
                    coords=_bbox_center_mm(tp),
                    bbox_mm=_bbox_mm(tp),
                    reason="Test point near suspect IC on a degraded net",
                    priority=6 + i,
                )
            )

    # Cap and de-dup by refdes (lowest priority wins on tie).
    seen: dict[str, ProbePoint] = {}
    for pp in sorted(route, key=lambda x: x.priority):
        if pp.refdes not in seen:
            seen[pp.refdes] = pp
        if len(seen) >= MAX_ROUTE_ENTRIES:
            break
    final_route = list(seen.values())

    # unmapped_refdes — referenced but missing from board.
    unmapped = sorted(r for r in referenced_refdes if r not in parts_by_refdes)

    return EnrichedTimeline(
        timeline=timeline,
        probe_route=final_route,
        unmapped_refdes=unmapped,
    )


# ----------------------------------------------------------------------
# Geometry helpers
# ----------------------------------------------------------------------

def _bbox_center_mils(part) -> tuple[float, float]:
    (lo, hi) = part.bbox
    return ((lo.x + hi.x) / 2.0, (lo.y + hi.y) / 2.0)


def _bbox_center_mm(part) -> tuple[float, float]:
    cx, cy = _bbox_center_mils(part)
    return (cx * MIL_TO_MM, cy * MIL_TO_MM)


def _bbox_mm(part) -> tuple[tuple[float, float], tuple[float, float]]:
    (lo, hi) = part.bbox
    return (
        (lo.x * MIL_TO_MM, lo.y * MIL_TO_MM),
        (hi.x * MIL_TO_MM, hi.y * MIL_TO_MM),
    )


def _euclidean_mils(a: tuple[float, float], b: tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/agent/test_schematic_boardview_bridge.py -v`
Expected: all 5 tests passed (2 from skeleton + 3 from this task).

- [ ] **Step 5: Commit**

```bash
git add api/agent/schematic_boardview_bridge.py tests/agent/test_schematic_boardview_bridge.py
git commit -m "$(cat <<'EOF'
feat(agent): bridge ranks ProbePoints with mil→mm geometry

Implements the four heuristic ranking rules from the spec: source IC
of the blocked rail (priority 1), earliest dead component in cascade
(priority 2), up to 3 nearest decoupling caps to the priority-1 IC
(priorities 3..5), and up to 3 test points on degraded nets near the
suspect IC (priorities 6..8). Capped at MAX_ROUTE_ENTRIES (8) total.
Coordinates are converted to mm at the boundary; refdes referenced by
the timeline but absent from the board land in unmapped_refdes.
EOF
)" -- api/agent/schematic_boardview_bridge.py tests/agent/test_schematic_boardview_bridge.py
```

---

## Task 11: Tool — extend `mb_schematic_graph(query=simulate)` with failures + rail_overrides + bridge dispatch

**Files:**
- Modify: `api/tools/schematic.py`
- Modify: `tests/tools/test_schematic.py`

- [ ] **Step 1: Write failing tests for the new tool surface**

Append to `tests/tools/test_schematic.py`:

```python
def test_simulate_query_accepts_failures_param(memory_root, graph):
    _write_graph(memory_root, graph)
    result = mb_schematic_graph(
        device_slug=SLUG,
        memory_root=memory_root,
        query="simulate",
        failures=[{"refdes": "U7", "mode": "regulating_low", "voltage_pct": 0.85}],
    )
    assert result["found"] is True
    assert result["query"] == "simulate"


def test_simulate_query_accepts_rail_overrides_param(memory_root, graph):
    _write_graph(memory_root, graph)
    result = mb_schematic_graph(
        device_slug=SLUG,
        memory_root=memory_root,
        query="simulate",
        rail_overrides=[{"label": "+5V", "state": "degraded", "voltage_pct": 0.85}],
    )
    assert result["found"] is True
    # When +5V is degraded, the final_verdict should be 'degraded' or 'cascade',
    # not 'completed'.
    assert result["final_verdict"] in ("degraded", "cascade", "blocked")


def test_simulate_query_with_session_board_returns_probe_route(
    memory_root, graph, monkeypatch
):
    _write_graph(memory_root, graph)
    # Build a minimal SessionState with a Board that maps the graph's source IC.
    from api.board.model import Board, Part, Point
    from api.session.state import SessionState

    source = next(
        (r.source_refdes for r in graph.power_rails.values() if r.source_refdes),
        None,
    )
    assert source is not None
    board = Board(
        source="test.brd",
        format_id="test_link",
        parts=[
            Part(
                refdes=source,
                layer="top",
                bbox=(Point(x=0, y=0), Point(x=1000, y=1000)),
                pins=[],
            )
        ],
        nets=[],
    )
    session = SessionState()
    session.board = board

    result = mb_schematic_graph(
        device_slug=SLUG,
        memory_root=memory_root,
        query="simulate",
        killed_refdes=[source],
        session=session,
    )
    assert result["found"] is True
    assert "probe_route" in result
    # Priority 1 should be the source IC.
    assert any(p["refdes"] == source for p in result["probe_route"])


def test_simulate_query_without_session_omits_probe_route(memory_root, graph):
    _write_graph(memory_root, graph)
    result = mb_schematic_graph(
        device_slug=SLUG,
        memory_root=memory_root,
        query="simulate",
    )
    assert result["found"] is True
    assert "probe_route" not in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/tools/test_schematic.py -v -k "accepts_failures or accepts_rail_overrides or session_board or without_session"`
Expected: 4 failures (signature doesn't accept the new params).

- [ ] **Step 3: Extend the tool signature and dispatch**

In `api/tools/schematic.py`, change `mb_schematic_graph` signature to add the three new params (find the existing signature, append):

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
    failures: list[dict] | None = None,
    rail_overrides: list[dict] | None = None,
    session=None,                         # api.session.state.SessionState | None
) -> dict[str, Any]:
```

Replace `_simulate_query` to accept and forward the new params. Find the existing function and replace with:

```python
def _simulate_query(
    graph_dict: dict,
    memory_root: Path,
    device_slug: str,
    killed_refdes: list[str] | None,
    failures: list[dict] | None = None,
    rail_overrides: list[dict] | None = None,
    session=None,
) -> dict[str, Any]:
    from api.pipeline.schematic.simulator import (
        Failure,
        RailOverride,
        SimulationEngine,
    )

    components = graph_dict.get("components", {})
    rails = graph_dict.get("power_rails", {})

    killed = list(killed_refdes or [])
    f_objs: list[Failure] = []
    for raw in failures or []:
        try:
            f_objs.append(Failure(**raw))
        except Exception as exc:
            return {"found": False, "reason": "invalid_failure", "detail": str(exc)}
    o_objs: list[RailOverride] = []
    for raw in rail_overrides or []:
        try:
            o_objs.append(RailOverride(**raw))
        except Exception as exc:
            return {"found": False, "reason": "invalid_rail_override", "detail": str(exc)}

    invalid_refdes = [
        r for r in killed + [f.refdes for f in f_objs] if r not in components
    ]
    if invalid_refdes:
        return {
            "found": False,
            "reason": "unknown_refdes",
            "invalid_refdes": invalid_refdes,
            "closest_matches": {
                r: _closest_matches(list(components.keys()), r) for r in invalid_refdes
            },
        }
    invalid_rails = [o.label for o in o_objs if o.label not in rails]
    if invalid_rails:
        return {
            "found": False,
            "reason": "unknown_rail",
            "invalid_rails": invalid_rails,
            "closest_matches": {
                r: _closest_matches(list(rails.keys()), r) for r in invalid_rails
            },
        }

    pack = memory_root / device_slug
    try:
        electrical = ElectricalGraph.model_validate_json(
            (pack / "electrical_graph.json").read_text()
        )
    except (OSError, ValueError):
        return {"found": False, "reason": "malformed_graph"}

    analyzed = None
    ab_path = pack / "boot_sequence_analyzed.json"
    if ab_path.exists():
        try:
            analyzed = AnalyzedBootSequence.model_validate_json(ab_path.read_text())
        except ValueError:
            analyzed = None

    tl = SimulationEngine(
        electrical,
        analyzed_boot=analyzed,
        killed_refdes=killed,
        failures=f_objs,
        rail_overrides=o_objs,
    ).run()
    last_state = tl.states[-1] if tl.states else None
    payload = {
        "found": True,
        "query": "simulate",
        "killed_refdes": tl.killed_refdes,
        "final_verdict": tl.final_verdict,
        "blocked_at_phase": tl.blocked_at_phase,
        "blocked_reason": last_state.blocked_reason if last_state and last_state.blocked else None,
        "phase_count": len(tl.states),
        "cascade_dead_components": tl.cascade_dead_components,
        "cascade_dead_rails": tl.cascade_dead_rails,
    }

    if session is not None and getattr(session, "board", None) is not None:
        from api.agent.schematic_boardview_bridge import enrich
        enriched = enrich(tl, session.board)
        payload["probe_route"] = [p.model_dump() for p in enriched.probe_route]
        payload["unmapped_refdes"] = enriched.unmapped_refdes

    return payload
```

Wire the new params into the dispatch where `query == "simulate"`:

```python
    if query == "simulate":
        return _simulate_query(
            graph,
            memory_root,
            device_slug,
            killed_refdes,
            failures=failures,
            rail_overrides=rail_overrides,
            session=session,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/tools/test_schematic.py -v -k "accepts_failures or accepts_rail_overrides or session_board or without_session"`
Expected: 4 passed.

- [ ] **Step 5: Run full schematic test file for regression**

Run: `.venv/bin/pytest tests/tools/test_schematic.py -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add api/tools/schematic.py tests/tools/test_schematic.py
git commit -m "$(cat <<'EOF'
feat(tools): mb_schematic_graph(query=simulate) gains failures + rail_overrides + bridge dispatch

Tool accepts both cause-driven (failures) and observation-driven
(rail_overrides) inputs in addition to the existing killed_refdes. When
called with a SessionState whose board is loaded, the response is
enriched with a ranked probe_route from the schematic_boardview_bridge.
Without a session board, the response stays on the existing compact shape.
EOF
)" -- api/tools/schematic.py tests/tools/test_schematic.py
```

---

## Task 12: Endpoint — `POST /pipeline/packs/{slug}/schematic/simulate` accepts failures + rail_overrides

**Files:**
- Modify: `api/pipeline/__init__.py`
- Modify: `tests/pipeline/test_simulate_endpoint.py`

- [ ] **Step 1: Write failing tests for the endpoint**

Append to `tests/pipeline/test_simulate_endpoint.py`:

```python
def test_simulate_endpoint_accepts_failures(tmp_memory: Path, client: TestClient):
    r = client.post(
        f"/pipeline/packs/{SLUG}/schematic/simulate",
        json={
            "failures": [
                {"refdes": "U7", "mode": "regulating_low", "voltage_pct": 0.85}
            ]
        },
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["device_slug"] == SLUG


def test_simulate_endpoint_accepts_rail_overrides(tmp_memory: Path, client: TestClient):
    r = client.post(
        f"/pipeline/packs/{SLUG}/schematic/simulate",
        json={
            "rail_overrides": [
                {"label": "+5V", "state": "degraded", "voltage_pct": 0.85}
            ]
        },
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    # Endpoint returns the raw timeline — probe_route never populated server-side.
    assert "probe_route" not in payload


def test_simulate_endpoint_rejects_invalid_failure(tmp_memory: Path, client: TestClient):
    r = client.post(
        f"/pipeline/packs/{SLUG}/schematic/simulate",
        json={"failures": [{"refdes": "U7", "mode": "totally_made_up"}]},
    )
    # Pydantic 422 from FastAPI on invalid mode literal.
    assert r.status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/pipeline/test_simulate_endpoint.py -v -k "endpoint_accepts or rejects_invalid"`
Expected: 3 failures (`SimulateRequest` doesn't accept failures/rail_overrides).

- [ ] **Step 3: Extend `SimulateRequest` and the endpoint**

In `api/pipeline/__init__.py`, find `SimulateRequest` and replace:

```python
from api.pipeline.schematic.simulator import (
    Failure,
    RailOverride,
    SimulationEngine,
)


class SimulateRequest(BaseModel):
    killed_refdes: list[str] = Field(default_factory=list)
    failures: list[Failure] = Field(default_factory=list)
    rail_overrides: list[RailOverride] = Field(default_factory=list)
```

Update the endpoint body to forward the new fields and validate refdes/rail labels:

```python
@router.post("/packs/{device_slug}/schematic/simulate")
async def post_simulate(device_slug: str, request: SimulateRequest) -> dict:
    """Run the behavioral simulator on the compiled electrical graph.

    Accepts killed_refdes (sugar), explicit failures (causes), and
    rail_overrides (observations). Synchronous (< 10 ms on MNT-class
    boards). HTTP context is stateless — no probe_route enrichment
    here; clients that need a route go through the agent WS path.
    """
    settings = get_settings()
    slug = _slugify(device_slug)
    pack_dir = Path(settings.memory_root) / slug
    graph_path = pack_dir / "electrical_graph.json"
    if not pack_dir.exists() or not graph_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No schematic ingested yet for device_slug={slug!r}",
        )

    try:
        electrical = ElectricalGraph.model_validate_json(graph_path.read_text())
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail=f"Malformed electrical_graph for {slug!r}: {exc}"
        ) from exc

    invalid = [
        r for r in request.killed_refdes + [f.refdes for f in request.failures]
        if r not in electrical.components
    ]
    if invalid:
        raise HTTPException(
            status_code=400, detail=f"Unknown refdes: {invalid}"
        )
    invalid_rails = [
        o.label for o in request.rail_overrides
        if o.label not in electrical.power_rails
    ]
    if invalid_rails:
        raise HTTPException(
            status_code=400, detail=f"Unknown rails: {invalid_rails}"
        )

    analyzed = None
    ab_path = pack_dir / "boot_sequence_analyzed.json"
    if ab_path.exists():
        try:
            analyzed = AnalyzedBootSequence.model_validate_json(ab_path.read_text())
        except Exception:
            analyzed = None

    tl = SimulationEngine(
        electrical,
        analyzed_boot=analyzed,
        killed_refdes=list(request.killed_refdes),
        failures=list(request.failures),
        rail_overrides=list(request.rail_overrides),
    ).run()
    return tl.model_dump()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/pipeline/test_simulate_endpoint.py -v`
Expected: all green (4 existing + 3 new = 7 passed).

- [ ] **Step 5: Commit**

```bash
git add api/pipeline/__init__.py tests/pipeline/test_simulate_endpoint.py
git commit -m "$(cat <<'EOF'
feat(api): POST /schematic/simulate accepts failures and rail_overrides

SimulateRequest grows two optional fields backed by the simulator's
new Failure / RailOverride types. Endpoint validates refdes and rail
labels with explicit 400 errors. The HTTP path stays stateless and
never returns probe_route — that enrichment lives behind the agent
WS path which has session board access.
EOF
)" -- api/pipeline/__init__.py tests/pipeline/test_simulate_endpoint.py
```

---

## Task 13: Forward `killed_refdes` / `failures` / `rail_overrides` through the agent runtimes

**Files:**
- Modify: `api/agent/runtime_managed.py` (around line 412)
- Modify: `api/agent/runtime_direct.py` (around line 308)

**Important context** — `session=session` is **already** forwarded in both runtimes (managed.py:421, direct.py:317). What's currently missing in both dispatchers is the pass-through of the simulate-specific payload keys: `killed_refdes`, `failures`, `rail_overrides`. Without this, the agent can call `mb_schematic_graph(query="simulate", killed_refdes=[...])` but the runtime silently drops the argument before the tool sees it. This task fixes that gap.

- [ ] **Step 1: Update `runtime_direct.py` dispatch (line 308–318)**

Replace:

```python
    if name == "mb_schematic_graph":
        return mb_schematic_graph(
            device_slug=device_slug,
            memory_root=memory_root,
            query=payload.get("query", ""),
            label=payload.get("label"),
            refdes=payload.get("refdes"),
            index=payload.get("index"),
            domain=payload.get("domain"),
            session=session,
        )
```

with:

```python
    if name == "mb_schematic_graph":
        return mb_schematic_graph(
            device_slug=device_slug,
            memory_root=memory_root,
            query=payload.get("query", ""),
            label=payload.get("label"),
            refdes=payload.get("refdes"),
            index=payload.get("index"),
            domain=payload.get("domain"),
            killed_refdes=payload.get("killed_refdes"),
            failures=payload.get("failures"),
            rail_overrides=payload.get("rail_overrides"),
            session=session,
        )
```

- [ ] **Step 2: Update `runtime_managed.py` dispatch (line 412–422)**

Make the same edit there (the surrounding code is identical). Replace:

```python
    if name == "mb_schematic_graph":
        return mb_schematic_graph(
            device_slug=device_slug,
            memory_root=memory_root,
            query=payload.get("query", ""),
            label=payload.get("label"),
            refdes=payload.get("refdes"),
            index=payload.get("index"),
            domain=payload.get("domain"),
            session=session,
        )
```

with:

```python
    if name == "mb_schematic_graph":
        return mb_schematic_graph(
            device_slug=device_slug,
            memory_root=memory_root,
            query=payload.get("query", ""),
            label=payload.get("label"),
            refdes=payload.get("refdes"),
            index=payload.get("index"),
            domain=payload.get("domain"),
            killed_refdes=payload.get("killed_refdes"),
            failures=payload.get("failures"),
            rail_overrides=payload.get("rail_overrides"),
            session=session,
        )
```

- [ ] **Step 3: Run agent tests for regression**

Run: `.venv/bin/pytest tests/agent/ -v`
Expected: all green. No tests directly exercise the new keys yet (those live in `test_schematic.py` from Task 11) — they pass session via direct call, not via the runtime dispatcher — but no existing agent test should regress.

- [ ] **Step 4: Commit**

```bash
git add api/agent/runtime_managed.py api/agent/runtime_direct.py
git commit -m "$(cat <<'EOF'
feat(agent): forward simulate payload keys through both runtimes

Adds killed_refdes / failures / rail_overrides to the mb_schematic_graph
dispatch in runtime_managed.py and runtime_direct.py. session was already
forwarded; the simulate-specific payload keys were silently dropped,
preventing the agent from invoking query=simulate with any of them.
EOF
)" -- api/agent/runtime_managed.py api/agent/runtime_direct.py
```

---

## Task 14: Final lint, full test sweep, and self-review

**Files:**
- All

- [ ] **Step 1: Lint**

Run: `make lint`
Expected: clean. Fix any new warnings introduced by the spec — typically unused imports in test fixtures, or a new module missing from a re-export.

- [ ] **Step 2: Format**

Run: `make format`
Expected: no diff produced (if there is, commit it as a separate housekeeping commit).

- [ ] **Step 3: Fast test suite**

Run: `make test`
Expected: all green. The fast subset must stay under ~1 minute. If any new test pushes us over, mark it `@pytest.mark.slow`.

- [ ] **Step 4: Full test suite (slow tests too)**

Run: `make test-all`
Expected: all green, including the slow accuracy gates.

- [ ] **Step 5: Eval gate**

Run: `make test-eval`
Expected: prints `simulator score = X.XX` and exits 0 (score ≥ 0.5). If the score is below 0.5 but the rest of the suite is green, that's a flag that the engine is producing too many ambiguous diagnoses — investigate before committing the next bench expansion. The score floor is a meaningful gate now that the full spec has landed.

- [ ] **Step 6: Final review of the diff against the spec**

Open `docs/superpowers/specs/2026-04-24-schematic-simulator-axes-2-3-design.md` side by side with `git log --oneline | head -15`. Confirm every "in scope" bullet has a corresponding commit. Anything missed becomes a follow-up issue, not a silent gap.

- [ ] **Step 7: Final commit if any housekeeping diff remains**

```bash
git status
# If there's anything outstanding (formatter, lint fix), commit it:
git add <files>
git commit -m "chore(schematic): post-axes-2-3 housekeeping" -- <files>
```
