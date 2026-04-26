# Phase 4.6 — BMS Q Roles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `cell_protection` and `cell_balancer` Q roles to the reverse-diagnostic engine so MNT Reform's BMS-side Q5-Q12 get classified (5/14 → 13/14), with one real cascade for `cell_protection` open/stuck_off (downstream fused-BAT rail dead).

**Architecture:** Two heuristic rules inserted early in `_classify_transistor` (before the 3-pin guard and before `inrush_limiter`), a dedicated `_find_cell_protection_downstream` helper that picks the protected-side rail by net-name suffix, and a single new cascade handler `_cascade_q_cell_protection_dead` wired into 4 of the 8 new dispatch-table entries. Validation via 2 hand-written scenarios on MNT Reform's Q5 (`BAT1FUSED` rail). `cell_balancer` stays alive-only because cell-level drift isn't observable from rail-level probing.

**Tech Stack:** Python 3.12, Pydantic v2, pytest. No new dependencies. Builds on the P4.5 infrastructure (`ComponentKind=passive_q`, `ComponentMode` including `stuck_on`/`stuck_off`, `_PASSIVE_CASCADE_TABLE` dispatch pattern).

**Reference spec:** `docs/superpowers/specs/2026-04-24-phase-4-6-bms-q-roles-design.md`.

**Commit policy:** The entire phase lands as **one** `feat(schematic): Phase 4.6 — BMS Q roles` commit at the end (Task 6). Intermediate tasks make changes in the working tree but do NOT commit. Tests go green incrementally; final `make test` gates the single commit.

---

## Files touched

| File | Responsibility |
|------|----------------|
| `api/pipeline/schematic/passive_classifier.py` | `_BAT_FAMILY_PATTERN` regex, Rules 0.5 + 0.6 in `_classify_transistor`, LLM system prompt + Pydantic role docstring updates |
| `api/pipeline/schematic/hypothesize.py` | `_find_cell_protection_downstream` helper, `_cascade_q_cell_protection_dead` handler, 8 new dispatch entries |
| `api/agent/manifest.py` | One new bullet in the `Modes Q` block of the diag prompt |
| `tests/pipeline/schematic/test_passive_classifier.py` | 6 new tests for the heuristic |
| `tests/pipeline/schematic/test_hypothesize.py` | 1 new cascade test + 1 dispatch-coverage test |
| `tests/pipeline/schematic/fixtures/hand_written_scenarios.yaml` | 2 new scenarios on MNT Q5 |
| `memory/mnt-reform-motherboard/electrical_graph.json` | Regenerated via `scripts/regen_electrical_graph.py` (artifact, not tracked) |

Total expected diff: ~300 lines added across 6 tracked files. No deletions.

---

## Task 1 — Heuristic: regex + `cell_protection` + `cell_balancer` rules

**Files:**
- Modify: `api/pipeline/schematic/passive_classifier.py` — add regex constant near the other module-level constants; insert Rules 0.5 and 0.6 in `_classify_transistor` after the existing Rule 0 (flyback_switch) check and **before** the `if len(nets) < 3: return None` guard.
- Test: `tests/pipeline/schematic/test_passive_classifier.py` — 6 new tests appended after the existing `test_transistor_flyback_switch_wins_over_load_switch`.

### Step 1.1 — Write 6 failing tests

Append the following block to the **end** of `tests/pipeline/schematic/test_passive_classifier.py` (after the last existing test). Use the existing `_graph_with_rails` helper. Q5-Q12 topologies mirror MNT Reform's real layout.

```python
# --------- Q BMS roles (Phase 4.6) ---------

def test_transistor_cell_protection_heuristic():
    """Q with 2 distinct BAT-family nets, no GND pin = cell_protection."""
    graph = _graph_with_rails("BAT1", "BAT1FUSED")
    q = ComponentNode(
        refdes="Q5", type="transistor",
        pins=[
            PagePin(number="1", role="signal_in", net_label=None),
            PagePin(number="2", role="signal_in", net_label="BAT1FUSED"),
            PagePin(number="3", role="signal_out", net_label="BAT1"),
        ],
    )
    graph.components["Q5"] = q
    kind, role, conf = classify_passive_refdes(graph, q)
    assert kind == "passive_q"
    assert role == "cell_protection"
    assert conf == 0.75


def test_transistor_cell_protection_rejects_with_gnd():
    """Q with BAT+BAT+GND pattern falls through — grounded Qs aren't series-protection."""
    graph = _graph_with_rails("BAT1", "BAT1FUSED")
    graph.nets["GND"] = NetNode(label="GND", is_power=True, is_global=True)
    q = ComponentNode(
        refdes="Q5", type="transistor",
        pins=[
            PagePin(number="1", role="signal_in", net_label="GND"),
            PagePin(number="2", role="signal_in", net_label="BAT1FUSED"),
            PagePin(number="3", role="signal_out", net_label="BAT1"),
        ],
    )
    graph.components["Q5"] = q
    _kind, role, _ = classify_passive_refdes(graph, q)
    assert role != "cell_protection"


def test_transistor_cell_balancer_heuristic():
    """Q with exactly one BAT-family net repeated on 2+ pins = cell_balancer
    (vision-merged bleed resistor artefact)."""
    graph = _graph_with_rails("BAT2")
    q = ComponentNode(
        refdes="Q6", type="transistor",
        pins=[
            PagePin(number="1", role="signal_in", net_label=None),
            PagePin(number="2", role="signal_in", net_label="BAT2"),
            PagePin(number="3", role="signal_out", net_label="BAT2"),
        ],
    )
    graph.components["Q6"] = q
    _kind, role, conf = classify_passive_refdes(graph, q)
    assert role == "cell_balancer"
    assert conf == 0.65


def test_transistor_cell_balancer_rejects_foreign_net():
    """Q with BAT+BAT+EN falls through — a foreign control net means it's not a pure balancer."""
    graph = _graph_with_rails("BAT2")
    graph.nets["EN_BMS"] = NetNode(label="EN_BMS")
    q = ComponentNode(
        refdes="Q6", type="transistor",
        pins=[
            PagePin(number="1", role="signal_in", net_label="EN_BMS"),
            PagePin(number="2", role="signal_in", net_label="BAT2"),
            PagePin(number="3", role="signal_out", net_label="BAT2"),
        ],
    )
    graph.components["Q6"] = q
    _kind, role, _ = classify_passive_refdes(graph, q)
    assert role != "cell_balancer"


def test_cell_protection_priority_over_inrush_limiter():
    """Q with 2 BAT-family rails must resolve to cell_protection, NOT inrush_limiter.
    The inrush rule fires on any VIN/BAT-substring rail — without priority, Q5 would
    mis-classify because its rails both carry 'BAT'."""
    graph = _graph_with_rails("BAT", "BATFUSED")
    graph.components["U_BMS"] = ComponentNode(
        refdes="U_BMS", type="ic",
        pins=[PagePin(number="1", role="power_in", net_label="BATFUSED")],
    )
    graph.power_rails["BATFUSED"].consumers = ["U_BMS"]
    q = ComponentNode(
        refdes="Q5", type="transistor",
        pins=[
            PagePin(number="1", role="signal_in", net_label=None),
            PagePin(number="2", role="signal_in", net_label="BAT"),
            PagePin(number="3", role="signal_out", net_label="BATFUSED"),
        ],
    )
    graph.components["Q5"] = q
    _kind, role, _ = classify_passive_refdes(graph, q)
    assert role == "cell_protection"


@pytest.mark.parametrize("label,should_match", [
    # Accepted
    ("BAT",         True),
    ("BAT1",        True),
    ("BAT8",        True),
    ("BAT1FUSED",   True),
    ("BAT_PROT",    False),  # underscore prefix disallowed — regex has optional suffix without underscore
    ("VBAT",        True),
    ("VBAT1",       True),
    ("CHGBAT",      True),
    ("CELL1",       True),
    ("BATPACK",     True),
    # Rejected
    ("CR1220",      False),  # coin cell RTC, not pack
    ("PVIN",        False),
    ("VIN",         False),
    ("BATRANDOM",   False),  # suffix not in allowlist
    ("+3V3",        False),
    ("GND",         False),
])
def test_bat_family_pattern_coverage(label, should_match):
    from api.pipeline.schematic.passive_classifier import _BAT_FAMILY_PATTERN
    matched = _BAT_FAMILY_PATTERN.match(label) is not None
    assert matched is should_match, f"{label!r} match={matched} expected={should_match}"
```

- [ ] **Step 1.2 — Run the new tests, confirm they fail**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_passive_classifier.py -v -k "cell_protection or cell_balancer or bat_family" 2>&1 | tail -20
```

Expected: all 6 new tests fail. The first 4 fail with `role == None` instead of the expected value. The priority test fails with `role == "inrush_limiter"`. The regex parametrized test fails on the import line with `ImportError: cannot import name '_BAT_FAMILY_PATTERN'`.

- [ ] **Step 1.3 — Add `re` import and the regex constant in `passive_classifier.py`**

The module does NOT currently import `re` — verify with
`grep "^import re" api/pipeline/schematic/passive_classifier.py` (no
output expected). Add `import re` to the stdlib import block near the
top, sorted alphabetically among the existing `asyncio` / `logging`
imports:

```python
import asyncio
import logging
import re
```

Then locate the existing `_VIN_NET_TOKENS = ("VIN", "VBAT", "BAT",
"BATT", ...)` constant near the top of the module (search for
`_VIN_NET_TOKENS`). Add the new regex immediately after it:

```python
# Phase 4.6 — strict enum pattern for BMS cell / pack rails.
# Matches: BAT, BAT\d+, BAT\d+FUSED/PROT/OUT/..., VBAT, CHGBAT, CELL\d+
# Rejects: CR1220 (coin cell), arbitrary alphanumerics, underscored variants.
_BAT_FAMILY_PATTERN = re.compile(
    r"^(?:BAT|VBAT|CHGBAT|BATTERY|CELL)\d*(?:FUSED|PROT|RAW|OUT|PACK|CHG|IN)?$"
)
```

- [ ] **Step 1.4 — Insert Rules 0.5 and 0.6 in `_classify_transistor`**

Find the end of the existing Rule 0 (flyback_switch) block in `_classify_transistor`. It ends just before the `if len(nets) < 3: return None, 0.0` guard. Insert the two new rules between them:

```python
    # ---- Rule 0.5 (Phase 4.6): cell_protection — ≥2 distinct BAT-family
    # pin-nets, no GND. Placed before the 3-pin guard because a real
    # cell_protection may expose only 2 labelled pins (gate unlabelled).
    # Placed before inrush_limiter (Rule 1) because the inrush rule fires
    # on any VIN/BAT-substring rail name — without this priority Q5
    # mis-classifies as inrush_limiter.
    unique_nets = set(nets)
    bat_nets = {n for n in unique_nets if _BAT_FAMILY_PATTERN.match(n)}
    gnd_here = any(_is_ground_net(n) for n in nets)
    if len(bat_nets) >= 2 and not gnd_here:
        return "cell_protection", 0.75

    # ---- Rule 0.6 (Phase 4.6): cell_balancer — exactly one BAT-family
    # label repeated on ≥2 pins (the vision pass merges the bleed
    # resistor in series with the Q, producing identical S and D labels).
    # All remaining labelled pins must also be on that BAT net — a
    # foreign control / EN net means it's not a passive balancer.
    if len(bat_nets) == 1:
        the_bat = next(iter(bat_nets))
        if nets.count(the_bat) >= 2:
            foreign = [n for n in unique_nets if n != the_bat]
            if not foreign:
                return "cell_balancer", 0.65
```

`_is_ground_net` is already defined at the top of `passive_classifier.py` (confirm with grep before editing). Do NOT re-import or redefine.

- [ ] **Step 1.5 — Update the docstring of `_classify_transistor`**

Find the docstring (line 226 in current file — the one that says `roles: flyback_switch, load_switch, level_shifter, inrush_limiter`). Replace that exact list with:

```
    roles: flyback_switch, load_switch, level_shifter, inrush_limiter,
    cell_protection, cell_balancer. Falls
```

- [ ] **Step 1.6 — Re-run the 6 new tests, confirm they pass**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_passive_classifier.py -v -k "cell_protection or cell_balancer or bat_family" 2>&1 | tail -20
```

Expected: all 6 pass (4 happy paths + 1 priority + 16 regex parametrizations).

- [ ] **Step 1.7 — Run the full passive_classifier suite to check for regressions**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_passive_classifier.py -v 2>&1 | tail -10
```

Expected: every test passes (new + pre-existing Q roles: load_switch, level_shifter, inrush_limiter, flyback_switch, unclassified, priority).

---

## Task 2 — Cascade: helper + handler + dispatch entries + cascade test

**Files:**
- Modify: `api/pipeline/schematic/hypothesize.py` — add `_find_cell_protection_downstream` near the existing `_find_downstream_rail`, add `_cascade_q_cell_protection_dead` near the other Q handlers, extend `_PASSIVE_CASCADE_TABLE` with 8 new entries.
- Test: `tests/pipeline/schematic/test_hypothesize.py` — 1 cascade test + 1 dispatch-coverage test.

### Step 2.1 — Write failing cascade test

Append to the end of `tests/pipeline/schematic/test_hypothesize.py`. Use local helpers (schemas imported inline, matching existing test style).

```python
def _q_cell_protection_graph():
    """Minimal graph: Q5 is a cell_protection series FET between BAT1
    (cell tap, upstream) and BAT1FUSED (protected output, downstream).
    U_BMS consumes BAT1FUSED."""
    from api.pipeline.schematic.schemas import (
        ComponentNode, ElectricalGraph, NetNode, PagePin, PowerRail,
        SchematicQualityReport,
    )
    return ElectricalGraph(
        device_slug="q-cell-prot",
        components={
            "Q5": ComponentNode(
                refdes="Q5", type="transistor",
                kind="passive_q", role="cell_protection",
                pins=[
                    PagePin(number="1", role="signal_in", net_label=None),
                    PagePin(number="2", role="signal_in", net_label="BAT1FUSED"),
                    PagePin(number="3", role="signal_out", net_label="BAT1"),
                ],
            ),
            "U_BMS": ComponentNode(refdes="U_BMS", type="ic", pins=[
                PagePin(number="1", role="power_in", net_label="BAT1FUSED"),
            ]),
        },
        nets={
            "BAT1": NetNode(label="BAT1", is_power=True),
            "BAT1FUSED": NetNode(label="BAT1FUSED", is_power=True),
        },
        power_rails={
            "BAT1": PowerRail(label="BAT1", consumers=["Q5"]),
            "BAT1FUSED": PowerRail(
                label="BAT1FUSED", source_refdes=None, consumers=["U_BMS"],
            ),
        },
        typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )


def test_cascade_q_cell_protection_dead_kills_fused_rail():
    """cell_protection open → downstream fused rail dead → consumers dead."""
    from api.pipeline.schematic.hypothesize import _cascade_q_cell_protection_dead
    graph = _q_cell_protection_graph()
    c = _cascade_q_cell_protection_dead(graph, graph.components["Q5"])
    assert "BAT1FUSED" in c["dead_rails"]
    assert "BAT1" not in c["dead_rails"]   # upstream cell tap stays alive
    assert "U_BMS" in c["dead_comps"]


def test_table_covers_cell_protection_and_cell_balancer_all_modes():
    """Phase 4.6: every (kind, role, mode) triple for the two new roles
    must dispatch somewhere — no silent fall-throughs."""
    from api.pipeline.schematic.hypothesize import _PASSIVE_CASCADE_TABLE
    for role in ("cell_protection", "cell_balancer"):
        for mode in ("open", "short", "stuck_on", "stuck_off"):
            assert ("passive_q", role, mode) in _PASSIVE_CASCADE_TABLE, (
                f"missing dispatch for passive_q / {role} / {mode}"
            )
```

- [ ] **Step 2.2 — Run the new tests, confirm they fail**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k "cell_protection or cell_balancer" 2>&1 | tail -15
```

Expected: import-error on `_cascade_q_cell_protection_dead` + AssertionError on the dispatch-coverage test.

- [ ] **Step 2.3 — Add the `_find_cell_protection_downstream` helper in `hypothesize.py`**

Find the existing `_find_downstream_rail` function (grep for `def _find_downstream_rail`). Insert this helper immediately **before** it so the cell_protection handler (added next) can see it in local scope:

```python
# Phase 4.6 — suffixes that unambiguously mark the protected-side rail
# on a cell_protection Q. BMS nomenclature varies across vendors; this
# covers MNT Reform (BAT1FUSED) and common alternatives.
_CELL_PROT_DOWNSTREAM_SUFFIXES = ("FUSED", "PROT", "OUT", "PACK")


def _find_cell_protection_downstream(
    electrical: ElectricalGraph, q: "_CompNode",
) -> str | None:
    """Return the protected-side BAT-family rail for a cell_protection Q.

    Heuristic, in priority order:

    1. Collect the Q's BAT-family rail pins (registered in
       `electrical.power_rails`).
    2. If fewer than two distinct rails: None — insufficient topology.
    3. If exactly one of them carries a `FUSED|PROT|OUT|PACK` suffix:
       return that one — asymmetric naming unambiguously marks the
       protected side.
    4. Fallback to `_find_downstream_rail` (source_refdes / consumer-
       count heuristic).

    Uses `_BAT_FAMILY_PATTERN` from `passive_classifier`.
    """
    from api.pipeline.schematic.passive_classifier import _BAT_FAMILY_PATTERN
    pin_rails = [
        p.net_label for p in q.pins
        if p.net_label and p.net_label in electrical.power_rails
    ]
    bat_rails = sorted({r for r in pin_rails if _BAT_FAMILY_PATTERN.match(r)})
    if len(bat_rails) < 2:
        return None
    suffixed = [
        r for r in bat_rails
        if any(r.endswith(s) for s in _CELL_PROT_DOWNSTREAM_SUFFIXES)
    ]
    if len(suffixed) == 1:
        return suffixed[0]
    return _find_downstream_rail(electrical, q)
```

`_CompNode` is the local type alias used for Q/passive compatibility — already imported / defined elsewhere in `hypothesize.py`. Don't redefine it; the string annotation `"_CompNode"` is deliberate so forward-reference works even if the alias is defined later.

- [ ] **Step 2.4 — Add the `_cascade_q_cell_protection_dead` handler**

Find the end of `_cascade_q_flyback_switch_short` (grep for `def _cascade_q_flyback_switch_short`). Insert the new handler immediately after it, before the `_PASSIVE_CASCADE_TABLE` dict definition:

```python
def _cascade_q_cell_protection_dead(
    electrical: ElectricalGraph, q,
) -> dict:
    """Cell-protection series FET open / stuck_off → protected-side rail
    loses power. Consumers of that rail become dead.

    Upstream cell tap stays alive (it's still electrically connected to
    its cell). Uses the suffix-aware downstream helper so we pick the
    protected side even when the compiler didn't annotate a source_refdes
    on the rail (common on BMS Qs where vision misses the `powers` edge)."""
    downstream = _find_cell_protection_downstream(electrical, q)
    if downstream is None:
        return _empty_cascade()
    return _simulate_rail_loss(electrical, downstream)
```

- [ ] **Step 2.5 — Add 8 new entries to `_PASSIVE_CASCADE_TABLE`**

Find the `# ========================= TRANSISTORS (Phase 4.5) =================` section of `_PASSIVE_CASCADE_TABLE`. The last block in that section is the `flyback_switch` quartet. Append the two new role quartets immediately after it, still inside the dict literal and before the closing `}`:

```python
    ("passive_q", "cell_protection", "open"):      _cascade_q_cell_protection_dead,
    ("passive_q", "cell_protection", "short"):     _cascade_passive_alive,
    ("passive_q", "cell_protection", "stuck_on"):  _cascade_passive_alive,
    ("passive_q", "cell_protection", "stuck_off"): _cascade_q_cell_protection_dead,

    ("passive_q", "cell_balancer",   "open"):      _cascade_passive_alive,
    ("passive_q", "cell_balancer",   "short"):     _cascade_passive_alive,
    ("passive_q", "cell_balancer",   "stuck_on"):  _cascade_passive_alive,
    ("passive_q", "cell_balancer",   "stuck_off"): _cascade_passive_alive,
```

- [ ] **Step 2.6 — Re-run cascade tests, confirm they pass**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py -v -k "cell_protection or cell_balancer" 2>&1 | tail -15
```

Expected: both new tests pass.

- [ ] **Step 2.7 — Run full hypothesize + passive_classifier suites**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_hypothesize.py tests/pipeline/schematic/test_passive_classifier.py -v 2>&1 | tail -10
```

Expected: every test passes. No regressions.

---

## Task 3 — Prompt surface updates

**Files:**
- Modify: `api/pipeline/schematic/passive_classifier.py` — extend the `_SYSTEM_PROMPT` (Opus passive classifier) and the `PassiveAssignment.role` field docstring.
- Modify: `api/agent/manifest.py` — insert one new bullet in the `Modes Q (Phase 4.5)` block of the diag prompt.

- [ ] **Step 3.1 — Extend the Opus passive classifier system prompt**

In `passive_classifier.py`, find the block that lists `flyback_switch` (inside `_SYSTEM_PROMPT`, look for `"- flyback_switch"`). The block currently ends with the line:

```
                         D-S short → continuous current, input rail stressed.
```

Append the two new role entries **immediately after** that line, preserving the 4-space+19-char indent pattern the other entries use:

```
    - cell_protection — Q in series with a battery cell or pack output
                         (source = cell-side BAT net, drain = fused /
                         pack-output BAT net). Gate controlled by the
                         BMS IC to disconnect on fault (over-discharge,
                         over-current, over-temp). Failure: channel
                         open → pack rail dead; D-S short → no fault
                         protection (silent).
    - cell_balancer   — Q + bleed resistor across a cell tap, gated by
                         the BMS to drain excess charge during balance
                         cycles. Pin pattern looks like S and D share
                         the same cell-tap net (the balance resistor
                         merges in extraction). Failure: stuck_on →
                         continuously drains that cell; open → balance
                         cycle silent, cells drift.
```

- [ ] **Step 3.2 — Extend the Pydantic role docstring**

In `passive_classifier.py`, find the `PassiveAssignment.role` Field definition (grep for `"load_switch · level_shifter · inrush_limiter · flyback_switch"`). Replace that exact string with:

```
passive_q: load_switch · level_shifter · inrush_limiter · flyback_switch · cell_protection · cell_balancer. Use null
```

No other characters change — just the inserted ` · cell_protection · cell_balancer` segment before `. Use null`.

- [ ] **Step 3.3 — Insert the agent diag prompt bullet**

In `api/agent/manifest.py`, find the bullet that starts with `- Sur un flyback_switch` (around line 704). The block ends with `rail d'entrée PVIN stressé et source chaude.` (line 707). Insert the two new bullets **between line 707 and the next bullet** (the `- `stuck_on` sur un rail...` line). Match the 2-space indent of the surrounding list.

```
  - Sur un cell_protection (Q série d'une cellule / pack, pins sur
    BATn / BATnFUSED) : `open` / `stuck_off` = cellule déconnectée →
    rail fused côté pack dead ; `short` / `stuck_on` = plus de
    protection (observable uniquement sur surcharge / déséquilibre
    cellule, pas direct sur un rail).
  - Sur un cell_balancer (Q + R de balance passive, pins sur BATn
    répétés) : modes non observables depuis un rail. Utile comme
    cible physique d'inspection quand une cellule drift seule dans
    la télémétrie BMS.
```

- [ ] **Step 3.4 — Smoke-check the prompt edits**

```bash
grep -c "cell_protection\|cell_balancer" api/pipeline/schematic/passive_classifier.py api/agent/manifest.py
```

Expected counts: `passive_classifier.py`: at least **7** mentions (regex comment + 2 role strings + 2 LLM prompt blocks + 2 docstring mentions + the two return statements = 9+). `manifest.py`: at least **2** mentions (one per bullet).

- [ ] **Step 3.5 — Run the full schematic test suite for safety**

```bash
.venv/bin/pytest tests/pipeline/schematic/ -v --tb=short 2>&1 | tail -10
```

Expected: every test passes. The prompt changes don't affect runtime — this is pure regression safety.

---

## Task 4 — Hand-written scenarios

**Files:**
- Modify: `tests/pipeline/schematic/fixtures/hand_written_scenarios.yaml`

- [ ] **Step 4.1 — Append two new scenarios at the end of the `scenarios:` list**

Open `tests/pipeline/schematic/fixtures/hand_written_scenarios.yaml`. The last existing scenario currently ends with `mnt-reform-q-inrush-limiter-open` and `accept_in_top_n: 10`. Append these two scenarios after it:

```yaml
  - id: mnt-reform-q-cell-protection-open-bat1fused
    description: |
      BAT1FUSED rail measures dead while the raw BAT1 cell tap reads
      nominal cell voltage (~3.7V). The series cell_protection FET
      between cell 1 and its fused output has gone open-channel
      (over-current trip latched, or channel burned from an inrush
      event). Ground truth: any passive_q with role=cell_protection
      whose downstream rail is BAT1FUSED.
    device_slug: mnt-reform-motherboard
    observations:
      state_rails: { "BAT1FUSED": "dead" }
    ground_truth_match:
      kind: passive_q
      role: cell_protection
      expected_mode: open
    accept_in_top_n: 5

  - id: mnt-reform-q-cell-protection-stuck-off-bat1fused
    description: |
      Same observable rail-dead symptom as the open variant, but the
      failure is described as stuck_off in the tech's vocabulary
      («le FET ne passe plus, gate drive OK mais canal mort»). The
      engine treats open and stuck_off as the same cascade — this
      scenario pins the stuck_off branch of the dispatch table.
    device_slug: mnt-reform-motherboard
    observations:
      state_rails: { "BAT1FUSED": "dead" }
    ground_truth_match:
      kind: passive_q
      role: cell_protection
      expected_mode: stuck_off
    accept_in_top_n: 5
```

- [ ] **Step 4.2 — Run hand-written scenarios to confirm they load**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_hand_written_scenarios.py -v 2>&1 | tail -15
```

Expected: The two new scenarios appear in the test collection. They will **SKIP** (not fail) because the current MNT `electrical_graph.json` on disk doesn't yet have `cell_protection` roles — the scenario's `candidates = [...]` list is empty, and the test's graceful `pytest.skip(...)` kicks in. All previously-passing scenarios still pass.

Task 5 will regenerate the MNT pack and these scenarios flip from SKIP to PASS.

---

## Task 5 — MNT regeneration + verification

**Files:**
- Artifact: `memory/mnt-reform-motherboard/electrical_graph.json` (regenerated — untracked by convention, same state as session start)

- [ ] **Step 5.1 — Regenerate MNT's electrical graph with the new heuristic**

```bash
.venv/bin/python scripts/regen_electrical_graph.py --slug mnt-reform-motherboard 2>&1 | tail -10
```

Expected output includes:
- `rails_before: 26` (or `52` if already regenerated from earlier in this session)
- `rails_after: 52`
- `passive_fills_reapplied` — a positive integer.

The script re-runs the fresh heuristic (which now classifies Q5-Q12) before re-applying snapshotted roles, so the BMS Qs pick up the new roles directly.

- [ ] **Step 5.2 — Verify the 8 new Q classifications landed**

```bash
.venv/bin/python -c "
import json
from pathlib import Path
g = json.loads(Path('memory/mnt-reform-motherboard/electrical_graph.json').read_text())
roles = {ref: c.get('role') for ref, c in g['components'].items() if c.get('kind') == 'passive_q'}
print('Q roles on MNT Reform:')
for ref in sorted(roles, key=lambda r: (r[0], int(r[1:]) if r[1:].isdigit() else 0)):
    print(f'  {ref}: {roles[ref]}')
print()
protections = [r for r, role in roles.items() if role == 'cell_protection']
balancers = [r for r, role in roles.items() if role == 'cell_balancer']
unclassified = [r for r, role in roles.items() if role is None]
print(f'cell_protection: {protections}')
print(f'cell_balancer:   {balancers}')
print(f'unclassified:    {unclassified}')
assert len(protections) == 1 and protections == ['Q5'], f'expected [Q5], got {protections}'
assert set(balancers) == {'Q6','Q7','Q8','Q9','Q10','Q11','Q12'}, f'got {balancers}'
assert unclassified == ['Q4'], f'expected [Q4], got {unclassified}'
print('OK — Phase 4.6 classifications landed as designed (1 cell_protection + 7 cell_balancer + Q4 None)')
"
```

Expected: final line prints `OK — Phase 4.6 classifications landed as designed`. If an assertion fails, investigate — re-run regen with `--min-confidence 0.7` explicitly, or inspect the Q in question for unexpected pin topology.

- [ ] **Step 5.3 — Run the hand-written scenarios against the regenerated pack**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_hand_written_scenarios.py -v 2>&1 | tail -15
```

Expected: `mnt-reform-q-cell-protection-open-bat1fused` and `mnt-reform-q-cell-protection-stuck-off-bat1fused` both **PASS**. The inrush_limiter scenario still SKIPs (no inrush Q on MNT). All previously-passing scenarios still pass.

- [ ] **Step 5.4 — Full fast test suite**

```bash
make test 2>&1 | tail -5
```

Expected: `NNN passed, 1 skipped, 38 deselected, 1 xfailed`. The 5 pre-existing agent `test_ws_flow` failures (mock `max_retries` signature mismatch — unrelated to this phase) may still appear; verify by inspection that the failure list is exactly that set and nothing from `tests/pipeline/schematic/` is in it.

- [ ] **Step 5.5 — Slow accuracy suite — confirm no gate regression**

```bash
.venv/bin/pytest tests/pipeline/schematic/test_hypothesize_accuracy.py tests/pipeline/schematic/test_hypothesize_field_accuracy.py -v --tb=short 2>&1 | tail -10
```

Expected: **23 passed, 15 skipped, 0 failed** — identical to pre-phase state. The two new roles add classifications, not new accuracy scenarios; gates are unchanged.

- [ ] **Step 5.6 — Ruff lint on touched files**

```bash
.venv/bin/ruff check api/pipeline/schematic/passive_classifier.py api/pipeline/schematic/hypothesize.py api/agent/manifest.py tests/pipeline/schematic/test_passive_classifier.py tests/pipeline/schematic/test_hypothesize.py
```

Expected: `All checks passed!`. If `ruff` reports anything, apply `--fix` and re-run.

---

## Task 6 — Commit the phase as one feat commit

- [ ] **Step 6.1 — Confirm the staging picture**

```bash
git status --short
```

Expected dirty paths (NOT exhaustive — other uncommitted work from the wider session may show too):
- `M api/pipeline/schematic/passive_classifier.py`
- `M api/pipeline/schematic/hypothesize.py`
- `M api/agent/manifest.py`
- `M tests/pipeline/schematic/test_passive_classifier.py`
- `M tests/pipeline/schematic/test_hypothesize.py`
- `M tests/pipeline/schematic/fixtures/hand_written_scenarios.yaml`

Do NOT stage `memory/…`, `scripts/…` unrelated changes, or anything outside the 6 paths above.

- [ ] **Step 6.2 — Stage only the Phase 4.6 files and commit (explicit `-- path` per CLAUDE.md)**

```bash
git add api/pipeline/schematic/passive_classifier.py \
        api/pipeline/schematic/hypothesize.py \
        api/agent/manifest.py \
        tests/pipeline/schematic/test_passive_classifier.py \
        tests/pipeline/schematic/test_hypothesize.py \
        tests/pipeline/schematic/fixtures/hand_written_scenarios.yaml

git commit -m "$(cat <<'EOF'
feat(schematic): Phase 4.6 — BMS Q roles cell_protection + cell_balancer

Adds the 5th and 6th canonical Q roles covering battery-side topology:

  - cell_protection — series FET between a cell tap (BATn) and its
    fused / pack-side output (BATnFUSED / BATnPROT). Gate is BMS-
    driven; the FET disconnects on fault. Heuristic fires on ≥2
    distinct BAT-family pin nets with no GND pin; confidence 0.75.
    Cascade: open / stuck_off → downstream fused rail dead via a
    dedicated _find_cell_protection_downstream helper that picks the
    protected side by FUSED|PROT|OUT|PACK suffix (works where the
    compiler hasn't annotated a source_refdes). short / stuck_on is
    alive-only — «no fault protection» is silent from a rail probe.

  - cell_balancer — Q + bleed resistor across a cell tap, BMS-gated
    to drain excess charge during passive balancing. Heuristic fires
    when exactly one BAT-family label appears on ≥2 pins (the
    vision pass merges the balance resistor into a single net label)
    and all other labelled pins share that same net; confidence 0.65.
    Alive-only for every mode — cell-level drift isn't observable
    from rail-level probing without BMS telemetry.

Rule priority: both rules are placed after the existing flyback_switch
check and BEFORE the 3-pin guard + inrush_limiter rule. The guard
would otherwise bail out on Q6-Q12 (only 2 labelled pins), and the
inrush rule would otherwise grab Q5 because it fires on any VIN/BAT-
substring rail. A regex _BAT_FAMILY_PATTERN pins down the accepted
label shape (BAT, BAT\d+, BAT\d+{FUSED|PROT|OUT|PACK|...}, VBAT,
CHGBAT, CELL\d+) and rejects coin-cell naming (CR1220) + foreign
prefixes.

Dispatch table: 8 new entries (2 roles × 4 modes). Opus passive
classifier prompt + Pydantic role enum + agent diag prompt updated
to teach the two new roles' failure semantics (FR user copy, EN
code identifiers — project convention).

Validation: 2 hand-written scenarios on MNT Reform's Q5/BAT1FUSED
(open + stuck_off) assert Q5 in top-5 for state_rails={BAT1FUSED:
dead}. No corpus regen — MNT has only one cell_protection Q, too
few samples to move a gate.

Impact on MNT Reform after regen: Q5 → cell_protection (conf 0.75),
Q6..Q12 → cell_balancer (conf 0.65). Coverage 5/14 → 13/14 (Q4
stays None — topology doesn't fit either role). Accuracy suite: 23
passed / 15 skipped (unchanged).
EOF
)" -- api/pipeline/schematic/passive_classifier.py \
      api/pipeline/schematic/hypothesize.py \
      api/agent/manifest.py \
      tests/pipeline/schematic/test_passive_classifier.py \
      tests/pipeline/schematic/test_hypothesize.py \
      tests/pipeline/schematic/fixtures/hand_written_scenarios.yaml
```

- [ ] **Step 6.3 — Verify the commit**

```bash
git log -1 --stat
```

Expected: one commit with 6 files changed, ~300 lines insertions, 0-few deletions. Title `feat(schematic): Phase 4.6 — BMS Q roles cell_protection + cell_balancer`.

Do NOT `git push`. CLAUDE.md requires explicit authorization from Alexis before pushing — stop here.

---

## Acceptance criteria (checklist for the orchestrator)

- [ ] `make test` green on `tests/pipeline/schematic/` entirely.
- [ ] `.venv/bin/python -c "..."` verification from Step 5.2 prints `OK — Phase 4.6 classifications landed as designed`.
- [ ] `mnt-reform-q-cell-protection-open-bat1fused` scenario passes (not skipped).
- [ ] Accuracy suite at 23 passed / 15 skipped / 0 failed.
- [ ] `grep "cell_protection\|cell_balancer" api/agent/manifest.py` returns at least 2 matches.
- [ ] Exactly one commit on `main` with title `feat(schematic): Phase 4.6 — BMS Q roles cell_protection + cell_balancer`.
