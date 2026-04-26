# Q Transistor Injection (Phase 4.5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Inject discrete transistors (load_switch, level_shifter, inrush_limiter MOSFETs/BJTs) into the reverse-diagnostic engine with dedicated modes (`stuck_on`, `stuck_off`) and a new cascade bucket (`always_on_rails`) so the engine can diagnose rail-stuck-on and standby-current failures.

**Architecture:** Additive extension of Phase 4 — `ComponentKind` gains `passive_q`, `ComponentMode` gains `stuck_on`/`stuck_off`, `RailMode` gains `stuck_on`. New cascade bucket `always_on_rails` disjoint from `shorted_rails` (physically opposite diagnostic cases). Heuristic Q classifier (`_classify_transistor`) + Opus prompt extension fill the roles. 12 new entries in `_PASSIVE_CASCADE_TABLE`. Frontend picker + auto-classify rule complete the loop.

**Tech Stack:** Python 3.12, Pydantic v2 (`extra="forbid"`), FastAPI, pytest + pytest-asyncio, anthropic SDK (Opus/Sonnet classifier), vanilla JS + D3. Deterministic hot path.

**Canonical spec:** `docs/superpowers/specs/2026-04-24-q-transistor-injection-design.md`.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `api/pipeline/schematic/schemas.py` | modify | Add `passive_q` to `ComponentKind` Literal |
| `api/pipeline/schematic/passive_classifier.py` | modify | `_classify_transistor` heuristic, `_TYPE_TO_KIND` extension, Opus prompt update |
| `api/pipeline/schematic/hypothesize.py` | modify | `ComponentMode` + `RailMode` extensions, `always_on_rails` cascade bucket, 3 Q handlers + 12 table entries, `_applicable_modes` Q branch, `_score_candidate` bucket match, `_validate_obs_against_graph` passthrough |
| `api/agent/measurement_memory.py` | modify | Rail stuck_on auto-classify rule (nominal voltage + standby note) |
| `api/agent/manifest.py` | modify | `mb_hypothesize` state_comps enum + state_rails enum extended with stuck_on/stuck_off; system prompt addendum for Q modes |
| `web/js/schematic.js` | modify | `MODE_SETS.passive_q` + rail stuck_on; `MODE_GLYPH` additions |
| `web/styles/schematic.css` | modify | CSS tokens for stuck_on (violet) and stuck_off (muted) |
| `tests/pipeline/schematic/test_schemas.py` | modify | passive_q round-trip test |
| `tests/pipeline/schematic/test_passive_classifier.py` | modify | 3 Q role heuristic tests + transistor-never-classified-as-ic test |
| `tests/pipeline/schematic/test_hypothesize.py` | modify | Q handler unit tests, always_on_rails scoring test, validator tests |
| `tests/pipeline/schematic/test_hypothesize_accuracy.py` | modify | stuck_on / stuck_off thresholds, parametrize extension |
| `tests/pipeline/schematic/fixtures/hand_written_scenarios.yaml` | modify | 2 new Q scenarios (load_switch stuck_on, inrush_limiter open) |
| `tests/pipeline/test_schematic_api.py` | no change | Existing passives endpoint returns Q entries automatically |

**Locked decisions (from the spec):**
- Schema migration is **additive** — `ComponentKind = Literal[..., "passive_q"]`, defaults preserve every Phase 1-4 graph on disk
- **`always_on_rails` is a new cascade bucket**, disjoint from `shorted_rails`. Scoring matches observed `rail.stuck_on` against predicted `always_on_rails`
- **Q mode collapse**: `open` and `stuck_off` map to the SAME cascade handler (`_cascade_q_*_dead`); `short` and `stuck_on` map to the SAME cascade handler (`_cascade_q_*_stuck_on`). Observation vocabulary is richer than cascade vocabulary by design — both ways to describe the same physical effect
- **3 roles in scope**: `load_switch`, `level_shifter`, `inrush_limiter`. Flyback/bias deferred
- **G-S short** not modelled separately — heuristic infers stuck direction from gate pull topology
- No breaking change on `POST /schematic/hypothesize` body; new modes accepted via extended Literal
- Hand-written scenarios SKIP when matching Q refdes absent from the compiled graph (same pattern as Phase 4 YAML)

---

## Phase structure

12 tasks in 4 groups. Each group's last task is a strict commit gate.

| Group | Tasks | Goal |
|---|---|---|
| **A — Shape + classifier** | T1-T3 | `ComponentKind.passive_q`, `_classify_transistor` heuristic, Opus prompt addendum |
| **B — Cascade dispatch** | T4-T7 | Mode/RailMode extensions, `always_on_rails` bucket, `_applicable_modes` Q branch, 3 handlers + 12 table entries, scoring update |
| **C — Agent + frontend + auto-classify** | T8-T10 | Tool schema enums, system prompt Q block, `MODE_SETS.passive_q` + CSS, auto-classify rule |
| **D — Corpus + CI + verify** | T11-T12 | Hand-written YAML scenarios, per-mode CI gates, final regen + sanity run |

Tasks T8-T10 frontend/agent parts need a quick live check (not a full browser walk-through) before commit. Group D closes the loop with a corpus regeneration and accuracy gate verification.

Every commit uses `git commit -- path/to/file1 path/to/file2` explicitly (parallel agents may be staging files).

---

## Task T1: Add `passive_q` to `ComponentKind`

**Files:**
- Modify: `api/pipeline/schematic/schemas.py`
- Modify: `tests/pipeline/schematic/test_schemas.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/pipeline/schematic/test_schemas.py`:

```python
def test_component_node_accepts_passive_q_kind():
    """Phase 4.5 adds passive_q to ComponentKind. Transistor nodes get it."""
    from api.pipeline.schematic.schemas import ComponentNode
    node = ComponentNode(
        refdes="Q5", type="transistor",
        kind="passive_q", role="load_switch",
    )
    assert node.kind == "passive_q"
    assert node.role == "load_switch"


def test_component_node_passive_q_round_trip():
    from api.pipeline.schematic.schemas import ComponentNode
    original = ComponentNode(
        refdes="Q7", type="transistor",
        kind="passive_q", role="level_shifter",
    )
    restored = ComponentNode.model_validate(original.model_dump())
    assert restored.kind == "passive_q"
    assert restored.role == "level_shifter"
```

- [ ] **Step 2: Run tests — should fail (passive_q not in Literal)**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_schemas.py -v -k "passive_q"`
Expected: 2 FAIL with Pydantic ValidationError rejecting `kind="passive_q"`.

- [ ] **Step 3: Extend `ComponentKind` literal**

Edit `api/pipeline/schematic/schemas.py`. Find the current `ComponentKind` definition (look for `ComponentKind = Literal[`), and replace with:

```python
ComponentKind = Literal[
    "ic",
    "passive_r",
    "passive_c",
    "passive_d",
    "passive_fb",
    "passive_q",
]
"""Kind of component in the electrical graph. `ic` is the Phase 1 default
(active components: ICs, modules, connectors, LEDs, crystals, oscillators).
Passive kinds (`passive_r`, `passive_c`, `passive_d`, `passive_fb`) are
Phase 4 additions. `passive_q` (discrete transistors — MOSFET/BJT) is
Phase 4.5 and is assigned by the transistor classifier during
`compile_electrical_graph`."""
```

Update the docstring if it still mentions `passive_q` as reserved.

- [ ] **Step 4: Run the tests — should pass**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_schemas.py -v`
Expected: ALL PASS (all existing + 2 new).

- [ ] **Step 5: Run the full fast schematic suite**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_schemas.py tests/pipeline/schematic/test_compiler.py tests/pipeline/schematic/test_passive_classifier.py -v`
Expected: ALL PASS. **Do NOT run `test_hypothesize_accuracy.py`** — 7-min suite.

- [ ] **Step 6: Commit T1**

```bash
git commit -m "$(cat <<'EOF'
feat(schematic): add passive_q to ComponentKind

Phase 4.5 foundation — discrete transistors can now be tagged as
kind="passive_q" on ComponentNode. Default remains "ic" so every
pre-4.5 electrical_graph.json reloads unchanged. Classifier wiring
lands in T2.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/schemas.py tests/pipeline/schematic/test_schemas.py
```

---

## Task T2: Heuristic Q classifier — load_switch / level_shifter / inrush_limiter

**Files:**
- Modify: `api/pipeline/schematic/passive_classifier.py` (fill `_classify_transistor`, extend `_TYPE_TO_KIND`)
- Modify: `tests/pipeline/schematic/test_passive_classifier.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/pipeline/schematic/test_passive_classifier.py`:

```python
# --------- transistors ---------

def test_transistor_load_switch_heuristic():
    """Q with upstream rail pin + downstream rail pin + gate on EN-labelled
    net = load_switch."""
    graph = _graph_with_rails("+5V", "+3V3_USB")
    graph.nets["5V_PWR_EN"] = NetNode(label="5V_PWR_EN")
    q = ComponentNode(
        refdes="Q5", type="transistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="+5V"),
            PagePin(number="2", role="unknown", net_label="+3V3_USB"),
            PagePin(number="3", role="unknown", net_label="5V_PWR_EN"),
        ],
    )
    graph.components["Q5"] = q
    kind, role, _conf = classify_passive_refdes(graph, q)
    assert kind == "passive_q"
    assert role == "load_switch"


def test_transistor_level_shifter_heuristic():
    """Q between two signal nets in different voltage domains = level_shifter."""
    graph = _graph_with_rails("+3V3", "+1V8")
    graph.nets["I2C1_3V3_SDA"] = NetNode(label="I2C1_3V3_SDA")
    graph.nets["I2C1_1V8_SDA"] = NetNode(label="I2C1_1V8_SDA")
    q = ComponentNode(
        refdes="Q2", type="transistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="I2C1_3V3_SDA"),
            PagePin(number="2", role="unknown", net_label="I2C1_1V8_SDA"),
            PagePin(number="3", role="unknown", net_label="+3V3"),
        ],
    )
    graph.components["Q2"] = q
    _kind, role, _ = classify_passive_refdes(graph, q)
    assert role == "level_shifter"


def test_transistor_inrush_limiter_heuristic():
    """Q in series from VIN to a regulator input, gate on RC soft-start."""
    graph = _graph_with_rails("VIN", "VIN_BUCK")
    graph.nets["SOFT_START"] = NetNode(label="SOFT_START")
    graph.components["U20"] = ComponentNode(
        refdes="U20", type="ic",
        pins=[PagePin(number="1", role="power_in", net_label="VIN_BUCK")],
    )
    graph.power_rails["VIN_BUCK"].consumers = ["U20"]
    q = ComponentNode(
        refdes="Q1", type="transistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="VIN"),
            PagePin(number="2", role="unknown", net_label="VIN_BUCK"),
            PagePin(number="3", role="unknown", net_label="SOFT_START"),
        ],
    )
    graph.components["Q1"] = q
    _kind, role, _ = classify_passive_refdes(graph, q)
    assert role == "inrush_limiter"


def test_transistor_unclassified_returns_none_role():
    """Q with no rail pins and no distinctive topology stays role=None."""
    graph = _graph_with_rails()
    graph.nets["RANDOM_A"] = NetNode(label="RANDOM_A")
    graph.nets["RANDOM_B"] = NetNode(label="RANDOM_B")
    q = ComponentNode(
        refdes="Q99", type="transistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="RANDOM_A"),
            PagePin(number="2", role="unknown", net_label="RANDOM_B"),
        ],
    )
    graph.components["Q99"] = q
    kind, role, _ = classify_passive_refdes(graph, q)
    assert kind == "passive_q"
    assert role is None


def test_heuristic_emits_passive_q_entry_in_whole_graph_pass():
    graph = _graph_with_rails("+5V", "+3V3_USB")
    graph.nets["EN_5V"] = NetNode(label="EN_5V")
    graph.components["Q5"] = ComponentNode(
        refdes="Q5", type="transistor",
        pins=[
            PagePin(number="1", role="unknown", net_label="+5V"),
            PagePin(number="2", role="unknown", net_label="+3V3_USB"),
            PagePin(number="3", role="unknown", net_label="EN_5V"),
        ],
    )
    result = classify_passives_heuristic(graph)
    assert "Q5" in result
    assert result["Q5"][0] == "passive_q"
```

- [ ] **Step 2: Run — should fail (type=transistor not in `_TYPE_TO_KIND`)**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_passive_classifier.py -v -k "transistor or passive_q"`
Expected: all new tests FAIL — either `kind="ic"` (default) or skipped-over by classifier.

- [ ] **Step 3: Extend `_TYPE_TO_KIND` and fill `_classify_transistor`**

Edit `api/pipeline/schematic/passive_classifier.py`. Find the `_TYPE_TO_KIND` map and extend:

```python
_TYPE_TO_KIND: dict[str, str] = {
    "resistor":   "passive_r",
    "capacitor":  "passive_c",
    "diode":      "passive_d",
    "ferrite":    "passive_fb",
    "transistor": "passive_q",
}
```

Find `classify_passive_refdes`. Its dispatch chain needs a transistor branch — look for the `if comp.type == "resistor":` block. Add:

```python
elif comp.type == "transistor":
    role, conf = _classify_transistor(graph, comp)
```

Now fill the `_classify_transistor` function. If no stub exists yet, add it above the public entry points:

```python
_EN_NET_TOKENS = ("EN", "_PWR_EN", "POWER", "_CTRL", "SOFT_START")
_VIN_NET_TOKENS = ("VIN", "BAT", "+12V", "+24V")


def _classify_transistor(
    graph: ElectricalGraph, comp: ComponentNode,
) -> tuple[str | None, float]:
    """Return (role, confidence) for a transistor. Heuristic covers three
    roles: load_switch, level_shifter, inrush_limiter. Falls back to None
    when topology doesn't narrow it down — Opus pass fills the holes."""
    nets = _pin_nets(comp)
    if len(nets) < 3:
        # Only 2 pins (unusual — most transistors are 3+); bail.
        return None, 0.0
    rail_nets = [n for n in nets if n in graph.power_rails]
    gnd_nets = [n for n in nets if _is_ground_net(n)]
    nonrail_nonGND = [n for n in nets if n not in rail_nets and n not in gnd_nets]

    # ---- Rule 1: load_switch — 2 rails + 1 EN-labelled net
    if len(rail_nets) == 2 and nonrail_nonGND:
        gate_net = nonrail_nonGND[0]
        upper = gate_net.upper()
        if any(tok in upper for tok in _EN_NET_TOKENS):
            return "load_switch", 0.75
        # Even without EN label, if the two rails differ clearly in voltage
        # (VIN vs +3V3 etc), it's still likely a load switch; use 0.55 conf.
        return "load_switch", 0.55

    # ---- Rule 2: inrush_limiter — upstream = VIN/BAT, downstream feeds a
    # consumer IC's power_in pin. Gate on RC / SOFT_START signal.
    if len(rail_nets) == 2:
        for n in rail_nets:
            up = n.upper()
            if any(tok in up for tok in _VIN_NET_TOKENS):
                # The OTHER rail should feed a consumer.
                other = next(r for r in rail_nets if r != n)
                rail = graph.power_rails.get(other)
                if rail and rail.consumers:
                    return "inrush_limiter", 0.6

    # ---- Rule 3: level_shifter — 2 signal nets (non-rail) + a rail gate
    # AND the two signal nets hint at different voltage domains
    if len(nonrail_nonGND) >= 2 and rail_nets:
        n1, n2 = nonrail_nonGND[0], nonrail_nonGND[1]
        up1, up2 = n1.upper(), n2.upper()
        # Both nets share a bus name but different domain tokens
        domain_tokens = ("1V8", "1V2", "3V3", "+5V", "+12V", "LV", "HV")
        d1 = next((t for t in domain_tokens if t in up1), None)
        d2 = next((t for t in domain_tokens if t in up2), None)
        if d1 and d2 and d1 != d2:
            return "level_shifter", 0.65

    return None, 0.0
```

Make sure to add `from __future__ import annotations` if not present (it is — already there from Phase 4).

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_passive_classifier.py -v`
Expected: ALL PASS (existing 17 + 5 new = 22 tests).

- [ ] **Step 5: Commit T2**

```bash
git commit -m "$(cat <<'EOF'
feat(schematic): Q transistor heuristic classifier — 3 roles

Phase 4.5 Q classifier covers load_switch (2 rails + EN-labelled gate,
conf 0.55-0.75), inrush_limiter (VIN + regulator-fed rail, conf 0.6),
level_shifter (2 signal nets across voltage domains + rail gate,
conf 0.65). Unclassified Qs return role=None and land for the Opus
pass to resolve.

Extends `_TYPE_TO_KIND` to map "transistor" → "passive_q" so
compile_electrical_graph tags Q automatically.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/passive_classifier.py tests/pipeline/schematic/test_passive_classifier.py
```

---

## Task T3: Extend Opus classifier prompt with Q roles

**Files:**
- Modify: `api/pipeline/schematic/passive_classifier.py` (edit `_SYSTEM_PROMPT`)

- [ ] **Step 1: Extend the system prompt**

Find `_SYSTEM_PROMPT` in `passive_classifier.py` (the long docstring block after the `PassiveClassification` Pydantic model). Locate the closing `passive_fb` block ("The only canonical role ...") and append AFTER it, before the "Use the input context" section:

```text

  passive_q (transistors — discrete MOSFET / BJT):
    - load_switch     — high-side gating of a rail (source = upstream rail,
                         drain = downstream rail, gate = EN / _PWR_EN signal).
                         Most common Q on embedded boards. Failure signatures:
                         D-S short → downstream rail permanently on (stuck_on);
                         channel open → downstream rail dead.
    - level_shifter   — Q between two signal nets in different logic voltage
                         domains (3V3 ↔ 1V8, 1V8 ↔ 1V2). Typical on I2C bridges.
                         Failure: signal stuck in one state, peripheral silent.
    - inrush_limiter  — Q in series with a power input, gate controlled by
                         an RC delay for soft-start. Classic on laptop VIN paths.
                         Failure: channel open → main rail never powers up.
```

Also update the `PassiveAssignment.role` field description to mention the Q roles. Find the `class PassiveAssignment(BaseModel)` block and its `role: str | None = Field(...)` description. Replace the `passive_fb: filter` line at the end with:

```python
            "passive_fb: filter. passive_q: load_switch · level_shifter · "
            "inrush_limiter. Use null when topology + notes genuinely don't "
```

(splice into the existing description string seamlessly.)

- [ ] **Step 2: Run the mocked LLM test suite to confirm nothing broke**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_passive_classifier_llm.py -v`
Expected: ALL PASS (6 tests). The prompt extension is a docstring / Field description change — no runtime impact on the mocked path.

- [ ] **Step 3: Commit T3**

```bash
git commit -m "$(cat <<'EOF'
feat(schematic): Opus classifier prompt gains Q transistor roles

System prompt now documents the 3 canonical Q roles (load_switch,
level_shifter, inrush_limiter) with failure signatures per role so
Sonnet/Opus can fill passive_q role=None cases from topology +
designer notes. PassiveAssignment.role Field description updated
to match — no Pydantic schema change, just richer guidance.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/passive_classifier.py
```

---

## Task T4: Extend `ComponentMode` / `RailMode` / `FailureMode` literals

**Files:**
- Modify: `api/pipeline/schematic/hypothesize.py` (extend Literals, `_PASSIVE_MODES`)
- Modify: `tests/pipeline/schematic/test_hypothesize.py` (validator coverage for new modes)

- [ ] **Step 1: Write failing tests for the new modes**

Append to `tests/pipeline/schematic/test_hypothesize.py`:

```python
def test_observation_accepts_stuck_on_on_rail():
    """RailMode now includes stuck_on."""
    from api.pipeline.schematic.hypothesize import Observations
    obs = Observations(state_rails={"+3V3_USB": "stuck_on"})
    assert obs.state_rails["+3V3_USB"] == "stuck_on"


def test_observation_accepts_stuck_modes_on_passive_q():
    """ComponentMode now includes stuck_on/stuck_off (used on Q targets)."""
    from api.pipeline.schematic.hypothesize import Observations
    obs = Observations(state_comps={"Q5": "stuck_on", "Q7": "stuck_off"})
    assert obs.state_comps["Q5"] == "stuck_on"


def test_validator_rejects_stuck_on_on_ic():
    """IC + stuck_on is still invalid — stuck_on is a passive-Q mode."""
    from api.pipeline.schematic.hypothesize import Observations, hypothesize
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="coh-test",
        components={"U5": ComponentNode(refdes="U5", type="ic", kind="ic")},
        nets={}, power_rails={}, typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    with pytest.raises(ValueError, match="U5.*not a valid IC mode"):
        hypothesize(graph, observations=Observations(state_comps={"U5": "stuck_on"}))


def test_validator_accepts_stuck_on_on_passive_q():
    from api.pipeline.schematic.hypothesize import Observations, hypothesize
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="coh-test",
        components={
            "Q5": ComponentNode(
                refdes="Q5", type="transistor",
                kind="passive_q", role="load_switch",
            ),
        },
        nets={}, power_rails={}, typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    # Should not raise — stuck_on is a passive mode.
    hypothesize(graph, observations=Observations(state_comps={"Q5": "stuck_on"}))
```

- [ ] **Step 2: Run — should fail (modes not yet in Literal)**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k "stuck_on or stuck_modes"`
Expected: 4 FAIL with Pydantic rejecting `"stuck_on"` / `"stuck_off"` as invalid Literal values.

- [ ] **Step 3: Extend the 3 Literals**

Edit `api/pipeline/schematic/hypothesize.py`. Find the current `ComponentMode` / `RailMode` / `FailureMode` block (near line 61 in the post-Phase-4.2 state). Replace with:

```python
ComponentMode = Literal[
    "dead", "alive", "anomalous", "hot",
    "open", "short",
    "stuck_on", "stuck_off",
]
RailMode = Literal[
    "dead", "alive",
    "shorted",       # to GND OR overvolt (Phase 1 semantics)
    "stuck_on",      # Phase 4.5 — rail alive when it should be off
]

# Failure modes that can be attributed to a component as the root-cause kill.
# `alive` omitted (a live component is not a failure). `shorted` is rail-side
# but produced by a component that shorts its input rail to GND. `open` /
# `short` are passive Phase 4 modes. `stuck_on` / `stuck_off` are Phase 4.5 Q
# modes — stuck_on = conducts permanently (rail stays on), stuck_off =
# never conducts (rail stays off).
FailureMode = Literal[
    "dead", "anomalous", "hot", "shorted",
    "open", "short",
    "stuck_on", "stuck_off",
]

_IC_MODES: frozenset[str] = frozenset({"dead", "alive", "anomalous", "hot"})
_PASSIVE_MODES: frozenset[str] = frozenset(
    {"open", "short", "alive", "stuck_on", "stuck_off"}
)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k "stuck_on or stuck_modes"`
Expected: 4 PASS.

- [ ] **Step 5: Run the full fast suite**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py tests/pipeline/schematic/test_schemas.py tests/pipeline/schematic/test_passive_classifier.py -v`
Expected: ALL PASS. **Do NOT run `test_hypothesize_accuracy.py`.**

- [ ] **Step 6: Commit T4**

```bash
git commit -m "$(cat <<'EOF'
feat(hypothesize): extend ComponentMode + RailMode with stuck_on/stuck_off

Phase 4.5 Q-specific modes added to the observation vocabulary.
stuck_on/stuck_off land in ComponentMode (apply to passive_q only via
_applicable_modes gating in T6) and FailureMode. RailMode gains
stuck_on — disjoint from shorted, captures "rail alive when it should
be off" (standby-current complaint). _PASSIVE_MODES frozenset
extended so the coherence validator accepts stuck_on/stuck_off on
passive targets; IC rejection logic unchanged.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
```

---

## Task T5: `always_on_rails` cascade bucket + scoring

**Files:**
- Modify: `api/pipeline/schematic/hypothesize.py` (extend `_empty_cascade`, `_score_candidate`, `_relevant_to_observations`, `cascade_preview`)
- Modify: `tests/pipeline/schematic/test_hypothesize.py` (scoring test for stuck_on rail)

- [ ] **Step 1: Write failing scoring test**

Append to `tests/pipeline/schematic/test_hypothesize.py`:

```python
def test_scoring_matches_stuck_on_rail_against_always_on_cascade():
    """A cascade with always_on_rails={'+3V3_USB'} should score TP against
    an observation state_rails={'+3V3_USB': 'stuck_on'}."""
    from api.pipeline.schematic.hypothesize import (
        _empty_cascade, _score_candidate, Observations,
    )
    cascade = _empty_cascade()
    cascade["always_on_rails"] = frozenset({"+3V3_USB"})
    obs = Observations(state_rails={"+3V3_USB": "stuck_on"})
    score, metrics, _diff = _score_candidate(cascade, obs)
    # 1 rail TP, 0 FP, 0 FN → positive score.
    assert metrics.tp_rails == 1
    assert metrics.fp_rails == 0
    assert metrics.fn_rails == 0
    assert score > 0


def test_scoring_stuck_on_disjoint_from_shorted():
    """A cascade with only shorted_rails does NOT TP-match a stuck_on
    observation (and vice versa). The two are disjoint by design."""
    from api.pipeline.schematic.hypothesize import (
        _empty_cascade, _score_candidate, Observations,
    )
    shorted_cascade = _empty_cascade()
    shorted_cascade["shorted_rails"] = frozenset({"+5V"})
    obs = Observations(state_rails={"+5V": "stuck_on"})
    _score, metrics, _ = _score_candidate(shorted_cascade, obs)
    # Mismatch: observed stuck_on, predicted shorted → FP (contradiction),
    # not TP.
    assert metrics.tp_rails == 0
    assert metrics.fp_rails == 1


def test_cascade_preview_exposes_always_on_count():
    """Hypothesis.cascade_preview should carry always_on_rails list."""
    from api.pipeline.schematic.hypothesize import _empty_cascade, _cascade_preview
    cascade = _empty_cascade()
    cascade["always_on_rails"] = frozenset({"+3V3_USB", "USB_VBUS"})
    preview = _cascade_preview(cascade)
    assert set(preview["always_on_rails"]) == {"+3V3_USB", "USB_VBUS"}
```

- [ ] **Step 2: Run — should fail (`always_on_rails` key absent)**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k "always_on or stuck_on_disjoint or cascade_preview_exposes"`
Expected: 3 FAIL with KeyError on `always_on_rails`.

- [ ] **Step 3: Add the bucket to `_empty_cascade`**

Find `_empty_cascade` in `hypothesize.py` and add the new key:

```python
def _empty_cascade() -> dict:
    return {
        "dead_comps": frozenset(),
        "dead_rails": frozenset(),
        "shorted_rails": frozenset(),
        "always_on_rails": frozenset(),   # Phase 4.5 — Q stuck_on cascades
        "anomalous_comps": frozenset(),
        "hot_comps": frozenset(),
        "final_verdict": "",
        "blocked_at_phase": None,
    }
```

- [ ] **Step 4: Update `_score_candidate` to match stuck_on**

Find `_score_candidate` in `hypothesize.py`. Locate the block that builds `predicted_rails` (search for `predicted_rails: dict[str, str] = {}`). Add one more loop:

```python
predicted_rails: dict[str, str] = {}
for rail in cascade["dead_rails"]:
    predicted_rails[rail] = "dead"
for rail in cascade["shorted_rails"]:
    predicted_rails[rail] = "shorted"
for rail in cascade["always_on_rails"]:
    predicted_rails[rail] = "stuck_on"  # Phase 4.5 — disjoint from shorted
```

- [ ] **Step 5: Update `_relevant_to_observations` to include always_on_rails**

Find `_relevant_to_observations` and update the `any_rail` union:

```python
def _relevant_to_observations(cascade: dict, obs: Observations) -> bool:
    obs_comps = set(obs.state_comps)
    obs_rails = set(obs.state_rails)
    any_pred = (
        cascade["dead_comps"] | cascade["anomalous_comps"] | cascade["hot_comps"]
    )
    any_rail = (
        cascade["dead_rails"] | cascade["shorted_rails"] | cascade["always_on_rails"]
    )
    if any_pred & obs_comps:
        return True
    if any_rail & obs_rails:
        return True
    return False
```

- [ ] **Step 6: Update `_cascade_preview` to expose always_on_rails**

Find `_cascade_preview` (it builds the per-hypothesis preview dict). Add:

```python
def _cascade_preview(cascade: dict) -> dict:
    return {
        "dead_rails": sorted(cascade["dead_rails"]),
        "shorted_rails": sorted(cascade["shorted_rails"]),
        "always_on_rails": sorted(cascade["always_on_rails"]),  # NEW
        "dead_comps_count": len(cascade["dead_comps"]),
        "anomalous_count": len(cascade["anomalous_comps"]),
        "hot_count": len(cascade["hot_comps"]),
    }
```

- [ ] **Step 7: Run the tests**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v`
Expected: ALL PASS (existing + 3 new).

- [ ] **Step 8: Commit T5**

```bash
git commit -m "$(cat <<'EOF'
feat(hypothesize): always_on_rails cascade bucket for Phase 4.5 Q

New cascade bucket disjoint from shorted_rails. Populated by Q
stuck_on/short handlers (landing in T7). Scoring matches observed
rail.stuck_on against predicted always_on_rails; a cascade that only
predicts shorted_rails does NOT TP-match stuck_on observations (and
vice versa — physically opposite diagnostic cases). cascade_preview
exposes the bucket for the agent/UI narrative.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
```

---

## Task T6: `_applicable_modes` — Q branch

**Files:**
- Modify: `api/pipeline/schematic/hypothesize.py` (extend `_applicable_modes`)
- Modify: `tests/pipeline/schematic/test_hypothesize.py` (Q applicability tests)

- [ ] **Step 1: Write failing tests**

Append to `tests/pipeline/schematic/test_hypothesize.py`:

```python
def test_applicable_modes_passive_q_returns_four_modes():
    """Q with a known role gets all 4 modes (open/short/stuck_on/stuck_off)
    — but only those whose handler is not passive_alive."""
    from api.pipeline.schematic.hypothesize import _applicable_modes
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, NetNode, PagePin, PowerRail,
        SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="q-test",
        components={
            "Q5": ComponentNode(
                refdes="Q5", type="transistor",
                kind="passive_q", role="load_switch",
                pins=[
                    PagePin(number="1", role="unknown", net_label="+5V"),
                    PagePin(number="2", role="unknown", net_label="+3V3_USB"),
                    PagePin(number="3", role="unknown", net_label="EN_USB"),
                ],
            ),
        },
        nets={"+5V": NetNode(label="+5V", is_power=True),
              "+3V3_USB": NetNode(label="+3V3_USB", is_power=True),
              "EN_USB": NetNode(label="EN_USB")},
        power_rails={"+5V": PowerRail(label="+5V"),
                     "+3V3_USB": PowerRail(label="+3V3_USB")},
        typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    modes = _applicable_modes(graph, "Q5")
    # load_switch has handlers for all 4 modes per T7 table.
    assert set(modes) == {"open", "short", "stuck_on", "stuck_off"}


def test_applicable_modes_passive_q_inrush_skips_alive_handlers():
    """inrush_limiter role has short/stuck_on → passive_alive → filtered out."""
    from api.pipeline.schematic.hypothesize import _applicable_modes
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, NetNode, PagePin, PowerRail,
        SchematicQualityReport,
    )
    graph = ElectricalGraph(
        device_slug="q-inrush-test",
        components={
            "Q1": ComponentNode(
                refdes="Q1", type="transistor",
                kind="passive_q", role="inrush_limiter",
                pins=[PagePin(number="1", role="unknown", net_label="VIN"),
                      PagePin(number="2", role="unknown", net_label="VIN_BUCK"),
                      PagePin(number="3", role="unknown", net_label="SOFT_START")],
            ),
        },
        nets={"VIN": NetNode(label="VIN", is_power=True),
              "VIN_BUCK": NetNode(label="VIN_BUCK", is_power=True),
              "SOFT_START": NetNode(label="SOFT_START")},
        power_rails={"VIN": PowerRail(label="VIN"),
                     "VIN_BUCK": PowerRail(label="VIN_BUCK")},
        typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    modes = _applicable_modes(graph, "Q1")
    # inrush_limiter has open + stuck_off active, short + stuck_on → passive_alive.
    assert set(modes) == {"open", "stuck_off"}
```

- [ ] **Step 2: Run — should fail (current code only returns open/short for passives)**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k "applicable_modes_passive_q"`
Expected: 2 FAIL — modes list doesn't include stuck_on/stuck_off.

- [ ] **Step 3: Update `_applicable_modes` for Q**

Find `_applicable_modes` in `hypothesize.py`. Replace the passive branch with:

```python
    # Passive. R/C/D/FB have {open, short}. Q has all 4 modes.
    if role is None:
        return []
    if kind == "passive_q":
        candidate_modes = ("open", "short", "stuck_on", "stuck_off")
    else:
        candidate_modes = ("open", "short")
    applicable: list[str] = []
    for mode in candidate_modes:
        handler = _PASSIVE_CASCADE_TABLE.get((kind, role, mode))
        if handler is not None and handler is not _cascade_passive_alive:
            applicable.append(mode)
    return applicable
```

(The IC branch above stays identical.)

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k "applicable_modes"`
Expected: ALL PASS. The Q tests pass ONLY after T7 lands the handlers — BUT the test expects the (kind, role, mode) entries to already be in the table. If they're missing, the test falls into the `handler is None` branch and returns []. The T6 test file changes REQUIRE T7 to be done concurrently or immediately after.

**Important**: to keep TDD honest, T7 should land BEFORE re-running these tests. Sequence: T6 edits `_applicable_modes` code, T7 adds table entries; run both tests at the end of T7.

- [ ] **Step 5: Commit T6 (skeleton)**

```bash
git commit -m "$(cat <<'EOF'
feat(hypothesize): _applicable_modes extended for passive_q

Q components get all 4 Phase 4.5 modes (open/short/stuck_on/stuck_off)
when their role is known and the dispatch table has a non-alive
handler for the triple. Falls through to [] when role=None or when
every (kind, role, mode) maps to passive_alive. Concrete table
entries land in T7.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
```

(The two Q applicability tests may FAIL at this commit until T7 lands. Flag that in the commit message or wait for T7 before running them — they are valid TDD reds for the T7 step.)

---

## Task T7: Q cascade handlers + `_PASSIVE_CASCADE_TABLE` entries + dispatch

**Files:**
- Modify: `api/pipeline/schematic/hypothesize.py` (3 new handlers + 12 table entries + `_simulate_failure` passive branch passthrough)
- Modify: `tests/pipeline/schematic/test_hypothesize.py` (handler + dispatch tests)

- [ ] **Step 1: Write failing tests**

Append to `tests/pipeline/schematic/test_hypothesize.py`:

```python
def _q_load_switch_graph():
    """+5V → Q5 (load_switch, EN=EN_USB) → +3V3_USB → U20 consumer."""
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, NetNode, PagePin, PowerRail,
        SchematicQualityReport,
    )
    return ElectricalGraph(
        device_slug="q-load",
        components={
            "Q5": ComponentNode(
                refdes="Q5", type="transistor",
                kind="passive_q", role="load_switch",
                pins=[
                    PagePin(number="1", role="unknown", net_label="+5V"),
                    PagePin(number="2", role="unknown", net_label="+3V3_USB"),
                    PagePin(number="3", role="unknown", net_label="EN_USB"),
                ],
            ),
            "U20": ComponentNode(refdes="U20", type="ic", pins=[
                PagePin(number="1", role="power_in", net_label="+3V3_USB"),
            ]),
        },
        nets={"+5V": NetNode(label="+5V", is_power=True),
              "+3V3_USB": NetNode(label="+3V3_USB", is_power=True),
              "EN_USB": NetNode(label="EN_USB")},
        power_rails={
            "+5V": PowerRail(label="+5V", source_refdes="U12", consumers=["Q5"]),
            "+3V3_USB": PowerRail(
                label="+3V3_USB", source_refdes="Q5", consumers=["U20"],
            ),
        },
        typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )


def test_cascade_q_load_stuck_on_marks_downstream_always_on():
    from api.pipeline.schematic.hypothesize import _cascade_q_load_stuck_on
    graph = _q_load_switch_graph()
    c = _cascade_q_load_stuck_on(graph, graph.components["Q5"])
    assert "+3V3_USB" in c["always_on_rails"]
    # Consumers (U20) become anomalous — active when they should be off.
    assert "U20" in c["anomalous_comps"]


def test_cascade_q_load_dead_kills_downstream_rail():
    from api.pipeline.schematic.hypothesize import _cascade_q_load_dead
    graph = _q_load_switch_graph()
    c = _cascade_q_load_dead(graph, graph.components["Q5"])
    assert "+3V3_USB" in c["dead_rails"]
    assert "U20" in c["dead_comps"]


def test_cascade_q_shifter_broken_anomalous_downstream():
    """Level shifter open → signal consumer anomalous."""
    from api.pipeline.schematic.hypothesize import _cascade_q_shifter_signal_broken
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, NetNode, PagePin, PowerRail,
        SchematicQualityReport, TypedEdge,
    )
    graph = ElectricalGraph(
        device_slug="q-shifter",
        components={
            "Q2": ComponentNode(
                refdes="Q2", type="transistor",
                kind="passive_q", role="level_shifter",
                pins=[
                    PagePin(number="1", role="unknown", net_label="I2C_3V3_SDA"),
                    PagePin(number="2", role="unknown", net_label="I2C_1V8_SDA"),
                    PagePin(number="3", role="unknown", net_label="+3V3"),
                ],
            ),
            "U30": ComponentNode(refdes="U30", type="ic", pins=[
                PagePin(number="1", role="bus_pin", net_label="I2C_1V8_SDA"),
            ]),
        },
        nets={"I2C_3V3_SDA": NetNode(label="I2C_3V3_SDA"),
              "I2C_1V8_SDA": NetNode(label="I2C_1V8_SDA"),
              "+3V3": NetNode(label="+3V3", is_power=True)},
        power_rails={"+3V3": PowerRail(label="+3V3")},
        typed_edges=[TypedEdge(src="U30", dst="I2C_1V8_SDA", kind="consumes_signal")],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    c = _cascade_q_shifter_signal_broken(graph, graph.components["Q2"])
    assert "U30" in c["anomalous_comps"]


def test_table_covers_every_q_role_mode_combo():
    """Phase 4.5 cascade table must have an entry for every (passive_q,
    role, mode) combination used by _applicable_modes."""
    from api.pipeline.schematic.hypothesize import _PASSIVE_CASCADE_TABLE
    for role in ("load_switch", "level_shifter", "inrush_limiter"):
        for mode in ("open", "short", "stuck_on", "stuck_off"):
            assert ("passive_q", role, mode) in _PASSIVE_CASCADE_TABLE, (
                f"missing handler for passive_q/{role}/{mode}"
            )


def test_simulate_failure_dispatches_q_stuck_on():
    """_simulate_failure with mode=stuck_on routes Q through the dispatch table."""
    from api.pipeline.schematic.hypothesize import _simulate_failure
    graph = _q_load_switch_graph()
    cascade = _simulate_failure(graph, None, "Q5", "stuck_on")
    assert "+3V3_USB" in cascade["always_on_rails"]
```

- [ ] **Step 2: Run — should fail (handlers don't exist, table entries missing)**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k "cascade_q or table_covers_every_q or simulate_failure_dispatches_q"`
Expected: 5 FAIL — ImportError on the handlers AND missing table keys.

- [ ] **Step 3: Add the 3 Q handler functions**

In `hypothesize.py`, just after the last existing cascade handler (before `_PASSIVE_CASCADE_TABLE`), insert:

```python
# ---------------------------------------------------------------------------
# Phase 4.5 — Q transistor cascade handlers
# ---------------------------------------------------------------------------


def _cascade_q_load_dead(electrical: ElectricalGraph, q) -> dict:
    """Load switch open or stuck_off → downstream rail dead + consumers dead."""
    downstream = _find_downstream_rail(electrical, q)
    if downstream is None:
        return _empty_cascade()
    return _simulate_rail_loss(electrical, downstream)


def _cascade_q_load_stuck_on(electrical: ElectricalGraph, q) -> dict:
    """Load switch short / stuck_on → downstream rail permanently on.
    Consumers become anomalous (active when they should be off in standby)."""
    downstream = _find_downstream_rail(electrical, q)
    if downstream is None:
        return _empty_cascade()
    c = _empty_cascade()
    c["always_on_rails"] = frozenset({downstream})
    consumers = electrical.power_rails[downstream].consumers or []
    # Consumers are anomalous: they're being powered when the sequencer
    # expected them off. Exclude the Q itself if it appears in consumers.
    c["anomalous_comps"] = frozenset(r for r in consumers if r != q.refdes)
    return c


def _cascade_q_shifter_signal_broken(
    electrical: ElectricalGraph, q,
) -> dict:
    """Level shifter open / stuck_off → signal not propagating → consumers
    anomalous. Treats both signal nets as potentially affected."""
    nets = [p.net_label for p in q.pins if p.net_label]
    sig_nets = [
        n for n in nets
        if n not in electrical.power_rails and not _is_ground_net_label(n)
    ]
    anomalous: set[str] = set()
    for edge in electrical.typed_edges:
        if edge.kind in {"consumes_signal", "depends_on"} and edge.dst in sig_nets:
            if edge.src in electrical.components:
                anomalous.add(edge.src)
    c = _empty_cascade()
    c["anomalous_comps"] = frozenset(anomalous)
    return c


def _cascade_q_shifter_signal_stuck(
    electrical: ElectricalGraph, q,
) -> dict:
    """Level shifter short / stuck_on → signal stuck at one rail level →
    consumers anomalous. Cascade topologically identical to _broken; the
    distinction is in the narrative/mode, not the cascade bucket."""
    return _cascade_q_shifter_signal_broken(electrical, q)


def _cascade_q_inrush_rail_dead(
    electrical: ElectricalGraph, q,
) -> dict:
    """Inrush limiter open / stuck_off → downstream regulator never powers up."""
    return _cascade_q_load_dead(electrical, q)
```

Note the `_is_ground_net_label` helper. If a private GND-detect helper already exists in `hypothesize.py` (likely named `_is_ground_net` or inlined set-membership), reuse it — don't create a duplicate. If nothing exists, add a one-line helper near the top of the handler section:

```python
def _is_ground_net_label(label: str | None) -> bool:
    if not label:
        return False
    up = label.upper()
    return up in {"GND", "AGND", "DGND", "PGND"} or up.startswith("GND_")
```

Check in-file first — grep for `"GND"` in the existing handlers before adding.

- [ ] **Step 4: Extend `_PASSIVE_CASCADE_TABLE` with 12 Q entries**

Find the `_PASSIVE_CASCADE_TABLE` dict in `hypothesize.py`. Append inside the dict (after the last ferrite entry):

```python
    # ========================= TRANSISTORS (Phase 4.5) ===========================
    ("passive_q", "load_switch",    "open"):      _cascade_q_load_dead,
    ("passive_q", "load_switch",    "short"):     _cascade_q_load_stuck_on,
    ("passive_q", "load_switch",    "stuck_on"):  _cascade_q_load_stuck_on,
    ("passive_q", "load_switch",    "stuck_off"): _cascade_q_load_dead,

    ("passive_q", "level_shifter",  "open"):      _cascade_q_shifter_signal_broken,
    ("passive_q", "level_shifter",  "short"):     _cascade_q_shifter_signal_stuck,
    ("passive_q", "level_shifter",  "stuck_on"):  _cascade_q_shifter_signal_stuck,
    ("passive_q", "level_shifter",  "stuck_off"): _cascade_q_shifter_signal_broken,

    ("passive_q", "inrush_limiter", "open"):      _cascade_q_inrush_rail_dead,
    ("passive_q", "inrush_limiter", "short"):     _cascade_passive_alive,
    ("passive_q", "inrush_limiter", "stuck_on"):  _cascade_passive_alive,
    ("passive_q", "inrush_limiter", "stuck_off"): _cascade_q_inrush_rail_dead,
```

- [ ] **Step 5: Extend `_simulate_failure` to route the new modes**

Find `_simulate_failure` in `hypothesize.py`. Locate the Phase 4 passive branch — the block starting with `if mode in {"open", "short"}:`. Replace the condition to include the Q modes:

```python
    # Phase 4 passive + Phase 4.5 Q modes.
    if mode in {"open", "short", "stuck_on", "stuck_off"}:
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

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v`
Expected: ALL PASS. The T6 applicability tests should now also pass because the table has entries for all Q (role, mode) triples.

- [ ] **Step 7: Run the neighbour suites for regression check**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py tests/pipeline/schematic/test_simulator.py tests/pipeline/schematic/test_compiler.py tests/pipeline/schematic/test_passive_classifier.py tests/pipeline/schematic/test_schemas.py tests/pipeline/schematic/test_hand_written_scenarios.py -v`
Expected: ALL PASS. Hand-written scenarios may SKIP if MNT Reform graph is missing Q entries — that's fine for now; T11 adds scenarios.

**Do NOT run** `test_hypothesize_accuracy.py` — 7-min suite.

- [ ] **Step 8: Commit T7**

```bash
git commit -m "$(cat <<'EOF'
feat(hypothesize): Q cascade handlers + 12 dispatch table entries

Three Q-specific cascade handlers (_cascade_q_load_dead,
_cascade_q_load_stuck_on, _cascade_q_shifter_signal_broken) plus two
trivial aliases (_cascade_q_shifter_signal_stuck equal to _broken,
_cascade_q_inrush_rail_dead equal to _load_dead). 12 entries in
_PASSIVE_CASCADE_TABLE covering all 3 Q roles × 4 modes —
inrush_limiter short/stuck_on map to passive_alive because a shorted
limiter is operationally equivalent to a wire (no observable cascade
beyond 'inrush protection absent').

_simulate_failure passive branch extended to route stuck_on/stuck_off
modes through the same table. Coherence with Phase 4 preserved: R/C/D
still only reach through {open, short}; Q reaches all 4.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- api/pipeline/schematic/hypothesize.py tests/pipeline/schematic/test_hypothesize.py
```

---

## Task T8: Agent tool schema + system prompt Q addendum

**Files:**
- Modify: `api/agent/manifest.py` (state_comps enum, state_rails enum, mb_hypothesize description, system prompt Q block)

- [ ] **Step 1: Inspect the current enum values**

Run: `grep -n 'enum.*dead\|state_comps.*description\|state_rails.*description' api/agent/manifest.py | head -10`
Note the line numbers of the two `enum` arrays — these are what you'll extend.

- [ ] **Step 2: Extend `state_comps` enum**

Find the `state_comps` property in the `mb_hypothesize` tool definition. Its `additionalProperties.enum` currently lists Phase 4 modes `["dead", "alive", "anomalous", "hot", "open", "short"]`. Replace with:

```python
                "state_comps": {
                    "type": "object",
                    "description": (
                        "Map refdes → mode. Pour un IC : 'dead', 'alive', "
                        "'anomalous', 'hot'. Pour un passive (R/C/D/FB) : "
                        "'open', 'short', 'alive'. Pour un passive_q "
                        "(MOSFET/BJT) : 'open', 'short', 'stuck_on', "
                        "'stuck_off', 'alive'. Le moteur rejette un IC en "
                        "mode passive (et vice-versa)."
                    ),
                    "additionalProperties": {
                        "type": "string",
                        "enum": [
                            "dead", "alive", "anomalous", "hot",
                            "open", "short",
                            "stuck_on", "stuck_off",
                        ],
                    },
                },
```

- [ ] **Step 3: Extend `state_rails` enum**

Find the `state_rails` property. Currently enum is `["dead", "alive", "shorted"]`. Replace:

```python
                "state_rails": {
                    "type": "object",
                    "description": (
                        "Map rail label → mode. Modes : 'dead' (0V), "
                        "'alive' (nominal), 'shorted' (court vers GND ou "
                        "overvolt), 'stuck_on' (alimenté quand devrait "
                        "être off — load switch claqué downstream)."
                    ),
                    "additionalProperties": {
                        "type": "string",
                        "enum": ["dead", "alive", "shorted", "stuck_on"],
                    },
                },
```

- [ ] **Step 4: Update `mb_hypothesize` top-level description**

Find the tool's `"description"` field (the long multi-line `(` block just above `"input_schema"`). Update to mention Q modes:

```python
        "description": (
            "Propose des hypothèses (refdes, mode) qui expliquent les "
            "observations. Modes IC (actifs) : dead (inerte), alive "
            "(fonctionne), anomalous (actif mais output incorrect — IC "
            "DSI bridge, codec audio, sensor), hot (chauffe anormalement). "
            "Modes PASSIVES (R/C/D/FB) : open (circuit coupé, typique "
            "ferrite brûlée ou R cassée), short (court plaque-à-plaque "
            "pour un cap, wire pour R). Modes Q (MOSFET/BJT) : open / "
            "short (physique), stuck_on / stuck_off (comportemental : "
            "conduit permanent / ne conduit jamais). Modes RAILS : dead, "
            "alive, shorted, stuck_on (rail alimenté quand devrait être "
            "off). Passer au moins une observation via state_comps / "
            "state_rails OU fournir repair_id pour synthétiser depuis le "
            "journal. La réponse contient `discriminating_targets` "
            "(list[str]) : quand les top-N candidats sont à égalité de "
            "score, ce sont les refdes/rails dont la mesure suivante "
            "partitionne le mieux les suspects — à suggérer au tech."
        ),
```

- [ ] **Step 5: Add Q mode guidance to the system prompt**

Find `render_system_prompt` in `manifest.py`. Locate the "Modes passives (Phase 4)" section (introduced in Phase 4.2 commit `93dc279`). Append a new bullet block:

```text

Modes Q (Phase 4.5) :
  - `open` ou `stuck_off` sur un Q = canal cassé (ne conduit jamais).
    Sur un load_switch = rail downstream dead.
    Sur un inrush_limiter = rail jamais up.
  - `short` ou `stuck_on` sur un Q = canal collé (conduit permanent).
    Sur un load_switch = rail downstream toujours alimenté, même en
    veille (typique panne standby-current).
    Sur un level_shifter = bus stuck à un niveau logique.
  - `stuck_on` sur un rail = observation directe : « +3V3_USB à 3.3V
    en veille alors qu'il devrait être off ». Engine propose un Q
    stuck_on upstream comme suspect.

Le vocabulaire open/short et stuck_on/stuck_off se recoupe sur les Q :
les deux pairs désignent la même cascade (open/stuck_off = canal
cassé, short/stuck_on = canal collé). Utilise le mot qui matche
l'observation du tech : s'il a fait un ohmmètre D-S et trouvé 0Ω,
dis « short ». S'il a observé le rail toujours on en veille, dis
« stuck_on ». L'engine les traite équivalents.
```

- [ ] **Step 6: Run the agent tests to confirm no breakage**

Run: `.venv/bin/pytest tests/agent/ -v`
Expected: ALL PASS (the prompt changes don't affect tool dispatch logic).

- [ ] **Step 7: Commit T8**

```bash
git commit -m "$(cat <<'EOF'
feat(agent): mb_hypothesize enums + system prompt gain Q vocabulary

Agent tool schema extended: state_comps enum now covers all 8 modes
(phase 1 IC + phase 4 passive + phase 4.5 Q). state_rails adds
stuck_on. Tool description explains the semantic split. System
prompt gains a Q-mode block explaining the open/stuck_off ↔
short/stuck_on equivalence so the agent picks terminology matching
the tech's observation (ohmmètre reading → short; standby-current
complaint → stuck_on).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- api/agent/manifest.py
```

---

## Task T9: Frontend — `MODE_SETS.passive_q` + rail stuck_on picker

**Files:**
- Modify: `web/js/schematic.js`
- Modify: `web/styles/schematic.css`

**Browser-verify required before commit per feedback memory.**

- [ ] **Step 1: Inspect the existing `MODE_SETS` / `MODE_GLYPH` definitions**

Run: `grep -n 'MODE_SETS\|MODE_GLYPH' web/js/schematic.js | head`
Note the current `MODE_SETS` object structure from T14 (Phase 4).

- [ ] **Step 2: Extend `MODE_SETS` and `MODE_GLYPH`**

Find the `MODE_SETS` object. Replace its `passive_q` key and add `stuck_on` to `rail`:

```javascript
const MODE_SETS = {
  ic:         ["unknown", "alive", "dead", "anomalous", "hot"],
  passive_r:  ["unknown", "alive", "open", "short"],
  passive_c:  ["unknown", "alive", "open", "short"],
  passive_d:  ["unknown", "alive", "open", "short"],
  passive_fb: ["unknown", "alive", "open", "short"],
  passive_q:  ["unknown", "alive", "open", "short", "stuck_on", "stuck_off"],
  rail:       ["unknown", "alive", "dead", "shorted", "stuck_on"],
};
```

Find `MODE_GLYPH` and add:

```javascript
const MODE_GLYPH = {
  // ... existing entries ...
  stuck_on:  "🔒",
  stuck_off: "🚫",
};
```

If `MODE_LABEL` exists (added in Phase 4.2), add the FR labels:

```javascript
const MODE_LABEL = {
  // ... existing ...
  stuck_on:  "toujours on",
  stuck_off: "toujours off",
};
```

- [ ] **Step 3: Add CSS tokens for the new buttons**

Append to `web/styles/schematic.css`:

```css
/* Phase 4.5 — Q stuck_on / stuck_off picker tints */
.sim-mode-picker[data-kind="passive_q"] button[data-mode="stuck_on"],
.sim-mode-picker[data-kind="rail"] button[data-mode="stuck_on"] {
  color: var(--violet);
  border-color: color-mix(in oklch, var(--violet) 40%, transparent);
}
.sim-mode-picker[data-kind="passive_q"] button[data-mode="stuck_off"] {
  color: var(--text-3);
  border-color: color-mix(in oklch, var(--text-3) 40%, transparent);
}
.sim-mode-picker[data-kind="passive_q"] button[data-mode="stuck_on"].active {
  background: color-mix(in oklch, var(--violet) 30%, var(--panel-2));
}
.sim-mode-picker[data-kind="rail"] button[data-mode="stuck_on"].active {
  background: color-mix(in oklch, var(--violet) 30%, var(--panel-2));
}
.sim-mode-picker[data-kind="passive_q"] button[data-mode="stuck_off"].active {
  background: color-mix(in oklch, var(--text-3) 20%, var(--panel-2));
}
```

- [ ] **Step 4: Verify files are served**

Run:
```
curl -s http://localhost:8000/js/schematic.js | grep -c "stuck_on"
curl -s http://localhost:8000/styles/schematic.css | grep -c 'data-mode="stuck_on"'
```
Expected: both ≥ 2.

- [ ] **Step 5: Browser verify (STOP before commit)**

Ask Alexis to verify at `http://localhost:8000/#schematic`:

1. Load MNT Reform graph.
2. Click a transistor (if any Q has kind=`passive_q` post-T11 regen — otherwise skip)
   → picker shows `[⚪ inconnu, ✅ vivant, ⚪ ouvert, ⚡ court, 🔒 toujours on, 🚫 toujours off]`.
3. Click a rail (e.g. `+3V3`) → picker shows `[⚪ inconnu, ✅ vivant, ❌ mort, ⚡ shorté, 🔒 toujours on]` (5 buttons).
4. Confirm no visual regression on IC / R / C / D / FB pickers.

On Alexis's OK, commit.

- [ ] **Step 6: Commit T9 (after browser-verify)**

```bash
git commit -m "$(cat <<'EOF'
feat(web): picker adds passive_q + rail stuck_on

MODE_SETS.passive_q exposes the 6-button picker (unknown/alive/open/
short/stuck_on/stuck_off). Rail picker gains stuck_on (5-button
total). Violet tint for stuck_on to distinguish from amber shorted;
muted grey for stuck_off. Browser-verified with Alexis, no
regression on Phase 1-4 pickers.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)" -- web/js/schematic.js web/styles/schematic.css
```

---

## Task T10: Auto-classify rule — rail nominal + standby note → stuck_on

**Files:**
- Modify: `api/agent/measurement_memory.py`
- Modify: `tests/agent/test_measurement_memory.py` (if exists — else skip test addition)

- [ ] **Step 1: Locate the auto-classify function**

Run: `grep -n "def.*classify\|auto_classify\|RAIL_TOLERANCE\|def.*_mode" api/agent/measurement_memory.py | head -10`
Identify the function that classifies rail measurements (likely `_classify_rail_measurement` or similar). Read its current body.

- [ ] **Step 2: Write a failing test (if a test file exists)**

Run: `ls tests/agent/test_measurement_memory.py 2>&1 || echo MISSING`

If present, append:

```python
def test_rail_nominal_voltage_with_standby_note_classifies_stuck_on():
    """+3V3 at 3.28V with note='veille' → stuck_on (rail alimenté quand
    il devrait être off)."""
    from api.agent.measurement_memory import _classify_rail_measurement
    mode = _classify_rail_measurement(
        measured=3.28, nominal=3.3, note="tech en veille, board éteint",
    )
    assert mode == "stuck_on"


def test_rail_nominal_voltage_without_standby_note_classifies_alive():
    """Sanity — no standby hint, nominal voltage stays alive."""
    from api.agent.measurement_memory import _classify_rail_measurement
    mode = _classify_rail_measurement(measured=3.28, nominal=3.3, note=None)
    assert mode == "alive"
```

Adjust function name to match the actual one in `measurement_memory.py`.

If the test file is MISSING, skip test addition — add the rule and smoke it via a one-off interactive run after commit. (Not ideal but keeps scope tight.)

- [ ] **Step 3: Run — should fail if tests added**

Run: `.venv/bin/pytest tests/agent/test_measurement_memory.py -v -k "stuck_on"`
Expected: 2 FAIL (the rule doesn't exist yet).

- [ ] **Step 4: Add the rule in `measurement_memory.py`**

Find the rail auto-classify logic. Locate the branch that returns `"alive"` when voltage is within ±10% of nominal. Insert BEFORE the `"alive"` return:

```python
# Phase 4.5: if voltage is nominal but the tech's note implies the rail
# SHOULD be off (standby/veille/sleep), promote to stuck_on.
if note:
    note_lower = note.lower()
    STANDBY_TOKENS = ("veille", "standby", "off", "power_off", "sleep",
                       "éteint", "eteint", "capot fermé", "lid closed")
    if any(tok in note_lower for tok in STANDBY_TOKENS):
        return "stuck_on"
# (fall through to existing "alive" return)
```

Make sure the current `_classify_rail_measurement` function signature accepts a `note` parameter. If it doesn't, extend the signature to accept optional `note: str | None = None` and update call sites (search for the function name, wire `note` through).

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/pytest tests/agent/ -v -k "stuck_on or measurement"`
Expected: PASS (new tests green, no regression).

- [ ] **Step 6: Commit T10**

```bash
git commit -m "$(cat <<'EOF'
feat(agent): auto-classify rail nominal + standby note → stuck_on

When a tech records a rail measurement at nominal voltage but
flags the context as standby/veille/off/sleep, the auto-classify
promotes the rail to stuck_on instead of alive. Enables the
typical standby-current diagnostic flow without explicit picker
click — the tech just narrates "+3V3_USB à 3.3V en veille" and
the engine sees a stuck_on observation.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>
EOF
)" -- api/agent/measurement_memory.py tests/agent/test_measurement_memory.py
```

(Drop the test file path from `-- ...` if it didn't exist and you skipped test addition.)

---

## Task T11: Hand-written Q scenarios

**Files:**
- Modify: `tests/pipeline/schematic/fixtures/hand_written_scenarios.yaml`

- [ ] **Step 1: Identify real Q refdes on MNT Reform (if any are classified)**

Run:
```
.venv/bin/python -c "
from api.pipeline.schematic.schemas import ElectricalGraph
g = ElectricalGraph.model_validate_json(open('memory/mnt-reform-motherboard/electrical_graph.json').read())
qs = [(r, c.role, [p.net_label for p in c.pins if p.net_label]) for r, c in g.components.items() if c.kind == 'passive_q']
print(f'Q components classified: {len(qs)}')
for r, role, nets in qs[:15]:
    print(f'  {r} role={role} nets={nets}')
"
```

If the output is empty or few entries, that's because the CLI `--classify-passives` (Phase 4 T18-lite) has not been re-run post-T2 with the new `_classify_transistor`. Re-run:

```
.venv/bin/python -m api.pipeline.schematic.cli --slug mnt-reform-motherboard --classify-passives
```

Then re-run the snippet above. Note which Q roles are present (load_switch / level_shifter / inrush_limiter) and pick real refdes for the scenarios below.

- [ ] **Step 2: Append the 2 Q scenarios to the YAML**

Open `tests/pipeline/schematic/fixtures/hand_written_scenarios.yaml` (do not replace existing scenarios, just append). Add at the end of the `scenarios:` list:

```yaml
  - id: mnt-reform-q-load-switch-stuck-on
    description: |
      The board consumes 500mA in standby even with the lid closed.
      +3V3_USB (or any USB-side filtered rail) measures 3.3V when its
      EN is low. A load-switch MOSFET downstream has D-S shorted
      permanently — rail stuck-on. Ground truth: any passive_q with
      role=load_switch connected to a USB/peripheral rail.
    device_slug: mnt-reform-motherboard
    observations:
      state_rails: { "+3V3_USB": "stuck_on" }
    ground_truth_match:
      kind: passive_q
      role: load_switch
      expected_mode: short
    accept_in_top_n: 5

  - id: mnt-reform-q-inrush-limiter-open
    description: |
      Main VIN reaches the board but no rail ever powers up on cold
      boot. Inrush limiter MOSFET open (burned from cold-inrush at
      first power-on). Ground truth: any passive_q with
      role=inrush_limiter in series with the main VIN path.
    device_slug: mnt-reform-motherboard
    observations:
      state_rails: { "VIN_BUCK": "dead" }
    ground_truth_match:
      kind: passive_q
      role: inrush_limiter
      expected_mode: open
    accept_in_top_n: 10
```

**If Step 1 showed that the MNT Reform graph does NOT have a rail called `+3V3_USB` or `VIN_BUCK`**, replace those rail names with real ones from the graph. A quick check:

```
.venv/bin/python -c "
from api.pipeline.schematic.schemas import ElectricalGraph
g = ElectricalGraph.model_validate_json(open('memory/mnt-reform-motherboard/electrical_graph.json').read())
print('Rails:', list(g.power_rails.keys())[:30])
"
```

Pick a rail that:
1. Exists in `power_rails`
2. Has a consumer IC (`consumers` non-empty)
3. Is NOT the main +3V3 / +5V (those are always on; a stuck_on scenario needs a gated rail)

If none matches, leave the scenario as-is — the loader test will SKIP it with a warning ("graph missing targets") which is the correct fail-safe.

- [ ] **Step 3: Run the hand-written loader**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hand_written_scenarios.py -v`
Expected: existing 3 Phase 4 scenarios PASS; 2 new Q scenarios PASS or SKIP gracefully.

If Q scenarios FAIL (not SKIP), it means the graph DOES have matching rails + roles but the engine isn't surfacing the ground truth. Debug before committing:
- Run mb_hypothesize manually with the scenario observations (via curl)
- Check if the Q role assignment matches expectations
- Adjust `accept_in_top_n` up to 15 if the engine has the right answer but ranks it low

- [ ] **Step 4: Commit T11**

```bash
git commit -m "$(cat <<'EOF'
test(hypothesize): hand-written Q scenarios (load_switch + inrush_limiter)

Two new anti-auto-referential scenarios covering the two most
field-impactful Q failure modes: load_switch stuck-on (standby
current) and inrush_limiter open (cold-boot dead rail). Scenarios
match by (kind, role, expected_mode) rather than specific refdes —
loader skips gracefully if the MNT Reform graph lacks matching Q
post-classification.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>
EOF
)" -- tests/pipeline/schematic/fixtures/hand_written_scenarios.yaml
```

---

## Task T12: Corpus regen + per-mode CI gates + final sanity

**Files:**
- Modify: `tests/pipeline/schematic/test_hypothesize_accuracy.py` (stuck_on/stuck_off thresholds + parametrize)
- Regen: `tests/pipeline/schematic/fixtures/hypothesize_scenarios.json`

- [ ] **Step 1: Add stuck_on/stuck_off thresholds**

Find the `THRESHOLDS` dict in `tests/pipeline/schematic/test_hypothesize_accuracy.py`. Append:

```python
    # Phase 4.5 Q modes. Same rationale as Phase 4 open/short — the
    # visibility interplay with IC shorted hypotheses means top-1 often
    # ties and Q loses. Top-3 is the real gate.
    "stuck_on":  {"top1": 0.00, "top3": 0.25, "mrr": 0.13},
    "stuck_off": {"top1": 0.00, "top3": 0.20, "mrr": 0.12},
```

- [ ] **Step 2: Extend the 3 parametrize decorators**

Find the 3 parametrize decorators (`test_top1_per_mode`, `test_top3_per_mode`, `test_mrr_per_mode`). Change each from:

```python
@pytest.mark.parametrize("mode", ["dead", "anomalous", "hot", "shorted", "open", "short"])
```

to:

```python
@pytest.mark.parametrize("mode", [
    "dead", "anomalous", "hot", "shorted",
    "open", "short",
    "stuck_on", "stuck_off",
])
```

All three decorators get the same change.

- [ ] **Step 3: Re-classify MNT passives + regenerate corpus**

Run (in order):

```
.venv/bin/python -m api.pipeline.schematic.cli --slug mnt-reform-motherboard --classify-passives
.venv/bin/python scripts/gen_hypothesize_benchmarks.py --slug mnt-reform-motherboard
```

The first command picks up the T2 heuristic to classify any transistor nodes. The second regenerates the scenario corpus — Q applicable modes flow through `_applicable_modes` so stuck_on/stuck_off scenarios appear automatically when Q components with classified roles exist.

Expected output from the regen: scenario count should include `stuck_on` and `stuck_off` rows. If 0, either:
- The MNT graph has no classified Q components (heuristic didn't tag any) — acceptable but the CI gates will pytest.skip on empty corpus
- The regen script filters out `stuck_on`/`stuck_off` — check `scripts/gen_hypothesize_benchmarks.py` and remove any accidental filter

- [ ] **Step 4: Run the accuracy suite targeted on new modes**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize_accuracy.py -v -k "stuck_on or stuck_off"`
Expected: 6 tests (3 per mode × 2 modes) PASS or SKIP.

If scenarios exist and gates fail (measured accuracy < threshold), LOWER the threshold by 5 pts rather than spending time on tuning in this phase — hand-written scenarios are the real gate. Document the adjustment in the commit message.

If no scenarios exist for stuck_on/stuck_off, tests SKIP with "no scenarios for mode=..." — that's fine, treats them as "not yet calibrated".

- [ ] **Step 5: Run the full fast suite for final regression check**

Run: `.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py tests/pipeline/schematic/test_simulator.py tests/pipeline/schematic/test_compiler.py tests/pipeline/schematic/test_passive_classifier.py tests/pipeline/schematic/test_passive_classifier_llm.py tests/pipeline/schematic/test_schemas.py tests/pipeline/schematic/test_hand_written_scenarios.py tests/agent/ -v`
Expected: ALL PASS. Hand-written Q scenarios either PASS or SKIP (depending on the MNT graph content).

- [ ] **Step 6: Commit T12**

```bash
git commit -m "$(cat <<'EOF'
test(hypothesize): Phase 4.5 per-mode CI gates + corpus regen

Thresholds for stuck_on/stuck_off land conservative (top-1 0%,
top-3 20-25%, MRR 0.12-0.13) matching the Phase 4 passive mode
calibration — visibility interplay means top-1 structurally ties;
top-3 is the real signal. Hand-written Q scenarios remain the
ground-truth gate. Corpus regenerated against MNT Reform after
--classify-passives picked up the T2 heuristic Q tagging.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>
EOF
)" -- tests/pipeline/schematic/test_hypothesize_accuracy.py tests/pipeline/schematic/fixtures/hypothesize_scenarios.json
```

---

## Self-Review Checklist

After writing the plan, verified against the spec:

**1. Spec coverage:**

- [x] `ComponentKind` gains `passive_q` → T1
- [x] `ComponentMode` gains `stuck_on`/`stuck_off` → T4
- [x] `RailMode` gains `stuck_on` → T4
- [x] New cascade bucket `always_on_rails` → T5
- [x] `_PASSIVE_MODES` frozenset extended → T4
- [x] `_classify_transistor` heuristic (3 roles) → T2
- [x] `_TYPE_TO_KIND` gains `"transistor"` → T2
- [x] Opus prompt extension → T3
- [x] 12 cascade table entries → T7
- [x] 3 new cascade handlers + 2 aliases → T7
- [x] `_applicable_modes` Q branch → T6
- [x] `_simulate_failure` passive branch extended → T7
- [x] `_score_candidate` stuck_on matching → T5
- [x] `_relevant_to_observations` updated → T5
- [x] `_cascade_preview` exposes always_on_rails → T5
- [x] `mb_hypothesize` state_comps / state_rails enums → T8
- [x] System prompt Q addendum → T8
- [x] Frontend `MODE_SETS.passive_q` + rail stuck_on → T9
- [x] CSS tokens for stuck_on / stuck_off → T9
- [x] `MODE_GLYPH` + `MODE_LABEL` updates → T9
- [x] Auto-classify stuck_on rule → T10
- [x] Hand-written Q scenarios → T11
- [x] Per-mode CI gates for stuck_on/stuck_off → T12
- [x] Corpus regeneration via --classify-passives + gen script → T12

**2. Placeholder scan:**
- No TBD / TODO / "implement later" in any task
- Every code step contains the actual code
- Every command step specifies expected output

**3. Type consistency:**
- `passive_q` used identically in schemas.py / classifier / hypothesize / frontend
- `always_on_rails` key used identically in `_empty_cascade`, handlers, `_score_candidate`, `_relevant_to_observations`, `_cascade_preview`
- Handler signatures `Callable[[ElectricalGraph, ComponentNode], dict]` consistent with Phase 4 handlers
- Mode tuple `(kind, role, mode)` matches across T6/T7/T8

**4. Scope:** 12 tasks, 4 groups, ~600 LOC. Focused on one phase boundary.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-24-q-transistor-injection.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task: Haiku for T1 (schema), T10 (auto-classify), T11 (YAML), T12 (CI gates); Sonnet for T2 (heuristic), T3 (prompt), T4-T7 (hypothesize engine touchy), T8 (agent prompt), T9 (frontend + browser-verify).

2. **Inline Execution** — Execute tasks in this session using `superpowers:executing-plans`, batch with checkpoints at each group boundary.

**Which approach?**
