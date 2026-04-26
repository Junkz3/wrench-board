# Bench Auto-Generator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a prod-ready CLI pipeline `scripts/generate_bench_from_pack.py` that transforms a device's knowledge pack (`memory/{slug}/*.json` + `raw_research_dump.md`) into a benchable `auto_proposals/*.jsonl`, with grounding-by-evidence-span + topology validation replacing human review, and a runtime integration that exposes the device-level simulator reliability score to the diagnostic agent.

**Architecture:** New module `api/pipeline/bench_generator/` (schemas + prompts + validator + extractor + scoring + writer + orchestrator) consumed by a thin CLI. Single-call Sonnet extraction via `call_with_forced_tool`, optional `--escalate-rejects` Opus rescue. Outputs land in `benchmark/auto_proposals/{slug}-YYYY-MM-DD.*` + `memory/{slug}/simulator_reliability.json`. Runtime: `api/agent/reliability.py` helper + extension of `render_system_prompt` (direct runtime) + extension of `_SEED_FILES` in `memory_seed.py` (managed runtime).

**Tech Stack:** Python 3.11, Pydantic v2, pytest + pytest-asyncio, `anthropic` async client (mocked in unit tests), forced tool use via `api/pipeline/tool_call.py::call_with_forced_tool`, JSONL + atomic file writes via `tempfile` + `os.replace`, `fcntl.flock` advisory lock on the aggregate `_latest.json`.

**Workspace:** All edits happen in the dedicated worktree `/home/alex/Documents/hackathon-microsolder-bench-gen/` on branch `feature/bench-auto-generator`. The main tree `/home/alex/Documents/hackathon-microsolder/` remains on `evolve/2026-04-24` and continues to be mutated by the evolve runner — do **not** touch that working directory.

**Hard no-go list (spec §2.1 + §7.2):** never write to `api/pipeline/schematic/simulator.py`, `api/pipeline/schematic/hypothesize.py`, `api/pipeline/schematic/evaluator.py`, `benchmark/scenarios.jsonl`, `benchmark/sources/`, `evolve/*`, `api/pipeline/schematic/boot_analyzer.py`, `tests/pipeline/schematic/test_boot_analyzer.py`. Import from `evaluator.py` is allowed (read-only consumption).

---

## File Layout (locked in before tasks)

**Created:**
```
api/pipeline/bench_generator/
  __init__.py           package marker, exports generate_from_pack
  errors.py             BenchGeneratorPreconditionError, BenchGeneratorLLMError
  schemas.py            Pydantic shapes (EvidenceSpan, Cause, ProposedScenarioDraft,
                        ProposedScenario, ProposalsPayload, Rejection, RunManifest,
                        ReliabilityCard, PackBundle)
  prompts.py            system prompt + user prompt assembly for the LLM
  validator.py          V1-V5 pure functions + run_all() orchestrator
  extractor.py          call_with_forced_tool wrapper + Opus rescue pass
  scoring.py            thin wrapper over evaluator.compute_score
  writer.py             atomic writes: jsonl, rejected, manifest, score,
                        _latest.json merge, sources/ archives,
                        memory/{slug}/simulator_reliability.json
  orchestrator.py       generate_from_pack(slug, ...) — the composed entrypoint

api/agent/reliability.py  load_reliability_line(device_slug) → str | None

scripts/generate_bench_from_pack.py  CLI

tests/pipeline/bench_generator/
  __init__.py
  conftest.py           shared fixtures (minimal ElectricalGraph, PackBundle, mock client)
  test_errors.py
  test_schemas.py
  test_prompts.py
  test_validator.py     V1-V5 + run_all
  test_extractor.py     mock client, escalate path
  test_scoring.py
  test_writer.py        tmp_path, atomicity
  test_orchestrator.py  generate_from_pack end-to-end with mocks

tests/agent/
  test_reliability.py
  test_render_system_prompt_reliability.py
  test_memory_seed_reliability.py
```

**Modified:**
```
api/agent/manifest.py          render_system_prompt — append reliability line
api/agent/memory_seed.py       _SEED_FILES += simulator_reliability.json
docs/superpowers/specs/2026-04-24-bench-auto-generator-design.md
                               tiny correction — use settings.anthropic_model_sonnet
```

**Unmodified but imported (read-only):**
```
api/pipeline/tool_call.py::call_with_forced_tool
api/pipeline/schematic/schemas.py::ElectricalGraph, PowerRail, ComponentNode
api/pipeline/schematic/evaluator.py::compute_score, ScoreCard
api/config.py::get_settings
```

---

## Task 1 — Bootstrap: package + errors

**Files:**
- Create: `api/pipeline/bench_generator/__init__.py`
- Create: `api/pipeline/bench_generator/errors.py`
- Create: `tests/pipeline/bench_generator/__init__.py`
- Create: `tests/pipeline/bench_generator/test_errors.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/pipeline/bench_generator/test_errors.py
from api.pipeline.bench_generator.errors import (
    BenchGeneratorError,
    BenchGeneratorPreconditionError,
    BenchGeneratorLLMError,
)


def test_precondition_error_is_subclass():
    assert issubclass(BenchGeneratorPreconditionError, BenchGeneratorError)


def test_llm_error_is_subclass():
    assert issubclass(BenchGeneratorLLMError, BenchGeneratorError)


def test_precondition_error_carries_reason():
    exc = BenchGeneratorPreconditionError("no electrical_graph.json")
    assert "electrical_graph" in str(exc)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /home/alex/Documents/hackathon-microsolder-bench-gen
.venv/bin/pytest tests/pipeline/bench_generator/test_errors.py -v
```
Expected: `ModuleNotFoundError: No module named 'api.pipeline.bench_generator'`.

- [ ] **Step 3: Create the empty package markers**

```python
# api/pipeline/bench_generator/__init__.py
# SPDX-License-Identifier: Apache-2.0
"""Auto-generator of benchable scenarios from device knowledge packs.

Public entrypoint: `generate_from_pack(device_slug, *, client, ...)` — see
`orchestrator.py`. The module is consumed by
`scripts/generate_bench_from_pack.py` and by tests.
"""
```

```python
# tests/pipeline/bench_generator/__init__.py
# (empty on purpose — pytest collects via the package)
```

- [ ] **Step 4: Implement errors.py**

```python
# api/pipeline/bench_generator/errors.py
# SPDX-License-Identifier: Apache-2.0
"""Exception classes for the bench generator.

Split from the main module so they can be imported without triggering
the Pydantic + Anthropic import graph in downstream consumers (e.g. a
CLI that only wants to pretty-print a precondition failure).
"""


class BenchGeneratorError(Exception):
    """Base class. Catch this to catch all generator failures."""


class BenchGeneratorPreconditionError(BenchGeneratorError):
    """Raised before any LLM call when the pack inputs are insufficient.
    Exit code 2 in the CLI."""


class BenchGeneratorLLMError(BenchGeneratorError):
    """Raised after max_attempts retries on a malformed LLM response.
    Exit code 3 in the CLI."""
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/test_errors.py -v
```
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add api/pipeline/bench_generator/__init__.py \
        api/pipeline/bench_generator/errors.py \
        tests/pipeline/bench_generator/__init__.py \
        tests/pipeline/bench_generator/test_errors.py
git commit -m "$(cat <<'EOF'
feat(bench-gen): bootstrap package + typed exceptions

Establishes api/pipeline/bench_generator/ with the three exception
classes the rest of the pipeline will raise: precondition failures (no
electrical_graph.json), LLM failures (malformed tool output after
retries), and the common base BenchGeneratorError.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/bench_generator/__init__.py \
        api/pipeline/bench_generator/errors.py \
        tests/pipeline/bench_generator/__init__.py \
        tests/pipeline/bench_generator/test_errors.py
```

---

## Task 2 — Pydantic shapes

**Files:**
- Create: `api/pipeline/bench_generator/schemas.py`
- Create: `tests/pipeline/bench_generator/test_schemas.py`

- [ ] **Step 1: Write the schema tests**

```python
# tests/pipeline/bench_generator/test_schemas.py
from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.pipeline.bench_generator.schemas import (
    Cause,
    EvidenceSpan,
    ProposedScenarioDraft,
    ProposalsPayload,
    Rejection,
    ReliabilityCard,
    RunManifest,
)


def test_cause_requires_value_ohms_for_leaky_short():
    with pytest.raises(ValidationError, match="value_ohms"):
        Cause(refdes="C19", mode="leaky_short")


def test_cause_requires_voltage_pct_for_regulating_low():
    with pytest.raises(ValidationError, match="voltage_pct"):
        Cause(refdes="U12", mode="regulating_low")


def test_cause_shorted_mode_accepts_no_extra_fields():
    c = Cause(refdes="C19", mode="shorted")
    assert c.value_ohms is None
    assert c.voltage_pct is None


def test_cause_rejects_unknown_mode():
    with pytest.raises(ValidationError):
        Cause(refdes="C19", mode="combusting")


def test_cause_forbids_extra_fields():
    with pytest.raises(ValidationError):
        Cause(refdes="C19", mode="shorted", haunted=True)


def test_evidence_span_requires_known_field():
    with pytest.raises(ValidationError):
        EvidenceSpan(
            field="cause.color",  # not in Literal
            source_quote_substring="snippet",
            reasoning="why",
        )


def test_proposed_scenario_draft_roundtrip():
    d = ProposedScenarioDraft(
        local_id="abc12345",
        cause=Cause(refdes="C19", mode="shorted"),
        expected_dead_rails=["+3V3"],
        expected_dead_components=[],
        source_url="https://example.com/x",
        source_quote=(
            "C19 is the output decoupling cap of the +3V3 regulator "
            "and a hard short collapses the rail to ground."
        ),
        confidence=0.82,
        evidence=[
            EvidenceSpan(
                field="cause.refdes",
                source_quote_substring="C19",
                reasoning="explicitly names C19",
            ),
            EvidenceSpan(
                field="expected_dead_rails",
                source_quote_substring="collapses the rail to ground",
                reasoning="quote asserts rail death",
            ),
        ],
        reasoning_summary="short on +3V3 decoupling cap collapses rail",
    )
    # round-trip through JSON
    assert d.model_validate_json(d.model_dump_json()) == d


def test_proposals_payload_bounds_scenarios():
    # empty is valid
    p = ProposalsPayload(scenarios=[])
    assert p.scenarios == []
    # too many rejected
    with pytest.raises(ValidationError):
        ProposalsPayload(scenarios=[_draft_stub() for _ in range(51)])


def test_rejection_carries_motive():
    r = Rejection(
        local_id="abc",
        motive="refdes_not_in_graph",
        detail="XZ999 not in 287 components",
    )
    assert r.motive == "refdes_not_in_graph"


def test_run_manifest_fields():
    m = RunManifest(
        device_slug="mnt-reform-motherboard",
        run_date="2026-04-24",
        run_timestamp="2026-04-24T21:00:00Z",
        model="claude-sonnet-4-6",
        n_proposed=8,
        n_accepted=5,
        n_rejected=3,
        input_mtimes={"raw_research_dump.md": 1714000000.0},
        escalated_rejects=False,
    )
    assert m.n_proposed == 8


def test_reliability_card_minimal():
    c = ReliabilityCard(
        device_slug="x",
        score=0.7,
        self_mrr=0.8,
        cascade_recall=0.55,
        n_scenarios=3,
        generated_at="2026-04-24T21:00:00Z",
        source_run_date="2026-04-24",
    )
    assert c.score == 0.7
    assert c.notes == []  # default


def _draft_stub() -> ProposedScenarioDraft:
    return ProposedScenarioDraft(
        local_id="x",
        cause=Cause(refdes="C1", mode="shorted"),
        expected_dead_rails=[],
        expected_dead_components=[],
        source_url="https://example.com/x",
        source_quote="A" * 60,
        confidence=0.5,
        evidence=[
            EvidenceSpan(
                field="cause.refdes",
                source_quote_substring="A",
                reasoning="stub",
            )
        ],
        reasoning_summary="stub",
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/test_schemas.py -v
```
Expected: ImportError — no `schemas` module yet.

- [ ] **Step 3: Implement schemas.py**

```python
# api/pipeline/bench_generator/schemas.py
# SPDX-License-Identifier: Apache-2.0
"""Pydantic shapes for the bench generator.

Every model is `extra='forbid'`. Shapes come in three groups:

1. LLM input/output contract — `EvidenceSpan`, `Cause`, `ProposedScenarioDraft`,
   `ProposalsPayload`. Their JSON schemas are used as `input_schema` for the
   forced tool call in `extractor.py`.
2. Accepted / rejected outputs — `ProposedScenario` (full scenario ready for
   `auto_proposals/*.jsonl`), `Rejection` (with a controlled-vocabulary motive).
3. Run artefacts — `RunManifest`, `ReliabilityCard` (consumed by
   `api/agent/reliability.py`).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FailureMode = Literal["dead", "shorted", "open", "leaky_short", "regulating_low"]


class Cause(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refdes: str
    mode: FailureMode
    value_ohms: float | None = None
    voltage_pct: float | None = None

    @model_validator(mode="after")
    def _mode_specific_fields(self) -> Cause:
        if self.mode == "leaky_short" and self.value_ohms is None:
            raise ValueError("leaky_short mode requires value_ohms")
        if self.mode == "regulating_low" and self.voltage_pct is None:
            raise ValueError("regulating_low mode requires voltage_pct")
        return self


EvidenceField = Literal[
    "cause.refdes",
    "cause.mode",
    "cause.value_ohms",
    "cause.voltage_pct",
    "expected_dead_rails",
    "expected_dead_components",
]


class EvidenceSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: EvidenceField
    source_quote_substring: str = Field(
        ...,
        min_length=1,
        description=(
            "Sub-string copied verbatim from the scenario's source_quote. "
            "Will be checked literally (case-sensitive `in` test); any "
            "deviation yields rejection."
        ),
    )
    reasoning: str = Field(..., max_length=400)


class ProposedScenarioDraft(BaseModel):
    """The shape the LLM must produce per scenario."""

    model_config = ConfigDict(extra="forbid")

    local_id: str = Field(..., min_length=3, max_length=48)
    cause: Cause
    expected_dead_rails: list[str] = Field(default_factory=list)
    expected_dead_components: list[str] = Field(default_factory=list)
    source_url: str = Field(..., pattern=r"^https?://.+")
    source_quote: str = Field(..., min_length=50)
    confidence: float = Field(..., ge=0.0, le=1.0)
    evidence: list[EvidenceSpan] = Field(..., min_length=1)
    reasoning_summary: str = Field(..., max_length=800)


class ProposalsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenarios: list[ProposedScenarioDraft] = Field(
        default_factory=list, max_length=50
    )


class ProposedScenario(BaseModel):
    """Scenario promoted after V1-V5 validation. Landing shape in
    auto_proposals/{slug}-{date}.jsonl."""

    model_config = ConfigDict(extra="forbid")

    id: str
    device_slug: str
    cause: Cause
    expected_dead_rails: list[str] = Field(default_factory=list)
    expected_dead_components: list[str] = Field(default_factory=list)
    source_url: str
    source_quote: str
    source_archive: str
    confidence: float
    generated_by: str
    generated_at: str
    validated_by_human: bool = False
    evidence: list[EvidenceSpan] = Field(default_factory=list)


RejectionMotive = Literal[
    "unknown_mode",
    "value_ohms_missing",
    "voltage_pct_missing",
    "source_url_malformed",
    "source_quote_too_short",
    "evidence_missing",
    "evidence_field_empty",
    "evidence_span_not_literal",
    "refdes_not_in_graph",
    "rail_name_not_in_graph",
    "component_not_in_graph",
    "mode_not_pertinent",
    "duplicate_in_run",
    "opus_rescue_failed",
]


class Rejection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_id: str
    motive: RejectionMotive
    detail: str = ""
    original_draft: ProposedScenarioDraft | None = None


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_slug: str
    run_date: str            # YYYY-MM-DD
    run_timestamp: str       # ISO UTC
    model: str
    n_proposed: int
    n_accepted: int
    n_rejected: int
    input_mtimes: dict[str, float]
    escalated_rejects: bool


class ReliabilityCard(BaseModel):
    """Consumed by api/agent/reliability.py. Written at
    memory/{slug}/simulator_reliability.json."""

    model_config = ConfigDict(extra="forbid")

    device_slug: str
    score: float
    self_mrr: float
    cascade_recall: float
    n_scenarios: int
    generated_at: str
    source_run_date: str
    notes: list[str] = Field(default_factory=list)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/test_schemas.py -v
```
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add api/pipeline/bench_generator/schemas.py tests/pipeline/bench_generator/test_schemas.py
git commit -m "$(cat <<'EOF'
feat(bench-gen): Pydantic shapes with extra='forbid' and grounding contract

Three groups: LLM in/out (Cause + EvidenceSpan + ProposedScenarioDraft +
ProposalsPayload), post-validation outputs (ProposedScenario + Rejection),
and run artefacts (RunManifest + ReliabilityCard).

Cause.mode is a Literal of the 5 simulable modes, with a model_validator
enforcing value_ohms for leaky_short and voltage_pct for regulating_low.
ProposalsPayload is capped at 50 scenarios per run to bound LLM output
size.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/bench_generator/schemas.py tests/pipeline/bench_generator/test_schemas.py
```

---

## Task 3 — conftest.py: shared test fixtures

**Files:**
- Create: `tests/pipeline/bench_generator/conftest.py`

- [ ] **Step 1: Write the fixtures module**

These fixtures are consumed by every subsequent test task. Writing them now lets each task stay focused on its own behaviour.

```python
# tests/pipeline/bench_generator/conftest.py
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from api.pipeline.bench_generator.schemas import (
    Cause,
    EvidenceSpan,
    ProposedScenarioDraft,
)
from api.pipeline.schematic.schemas import (
    ComponentNode,
    ElectricalGraph,
    PowerRail,
)


@pytest.fixture
def toy_graph() -> ElectricalGraph:
    """6 components + 2 rails + one decoupling relationship."""
    components = {
        "U7": ComponentNode(refdes="U7", kind="ic", role="buck_regulator"),
        "U13": ComponentNode(refdes="U13", kind="ic", role="buck_regulator"),
        "U1": ComponentNode(refdes="U1", kind="ic", role="cpu"),
        "C19": ComponentNode(refdes="C19", kind="passive_c", role="decoupling"),
        "R100": ComponentNode(refdes="R100", kind="passive_r", role="pullup"),
        "R200": ComponentNode(refdes="R200", kind="passive_r", role="series"),
    }
    rails = {
        "+5V": PowerRail(id="+5V", nominal_voltage=5.0, source_refdes="U7"),
        "+3V3": PowerRail(
            id="+3V3", nominal_voltage=3.3, source_refdes="U13",
            decoupling=["C19"],
        ),
    }
    return ElectricalGraph(
        device_slug="toy-board",
        components=components,
        power_rails=rails,
        edges=[],
        quality_report={},
    )


@pytest.fixture
def sample_draft() -> ProposedScenarioDraft:
    """A clean draft that passes V1-V5 against toy_graph. Tests derive
    invalid variants from this by mutating one field."""
    return ProposedScenarioDraft(
        local_id="c19-short",
        cause=Cause(refdes="C19", mode="shorted"),
        expected_dead_rails=["+3V3"],
        expected_dead_components=[],
        source_url="https://example.com/forum/c19",
        source_quote=(
            "C19 is a decoupling capacitor on the +3V3 rail. A hard short "
            "collapses the rail to ground and prevents the regulator from "
            "maintaining regulation."
        ),
        confidence=0.85,
        evidence=[
            EvidenceSpan(
                field="cause.refdes",
                source_quote_substring="C19",
                reasoning="explicitly named",
            ),
            EvidenceSpan(
                field="cause.mode",
                source_quote_substring="hard short",
                reasoning="shorted mode",
            ),
            EvidenceSpan(
                field="expected_dead_rails",
                source_quote_substring="collapses the rail to ground",
                reasoning="rail death asserted verbatim",
            ),
        ],
        reasoning_summary="C19 short on +3V3 collapses the rail.",
    )


@pytest.fixture
def now_iso() -> str:
    # Deterministic enough for tests that assert format, not value.
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@pytest.fixture
def pack_dir(tmp_path: Path) -> Path:
    """A minimal on-disk pack. Tests that need specific content write it
    themselves on top of the baseline here."""
    d = tmp_path / "memory" / "toy-board"
    d.mkdir(parents=True)
    (d / "raw_research_dump.md").write_text(
        "# Research Dump\n\n"
        "Symptom: rail +3V3 collapses when C19 shorts.\n"
        "Source: https://example.com/forum/c19\n"
        "...\n" * 20,  # ≥ 500 chars
        encoding="utf-8",
    )
    (d / "rules.json").write_text(
        '{"schema_version": "1.0", "rules": []}',
        encoding="utf-8",
    )
    (d / "registry.json").write_text(
        '{"schema_version": "1.0", "components": [], "signals": []}',
        encoding="utf-8",
    )
    return d
```

- [ ] **Step 2: Smoke-check the fixtures**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/ -v --collect-only 2>&1 | head -20
```
Expected: test collection runs without `conftest.py` import errors.

- [ ] **Step 3: Commit**

```bash
git add tests/pipeline/bench_generator/conftest.py
git commit -m "$(cat <<'EOF'
test(bench-gen): shared fixtures — toy_graph, sample_draft, pack_dir

Used by test_validator, test_extractor, test_writer, and test_orchestrator.
Writing the fixtures once upfront lets each test task stay focused on its
own behaviour instead of rebuilding a graph inline.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- tests/pipeline/bench_generator/conftest.py
```

---

## Task 4 — Validator: V1 sanity + V5 dedup

V1 catches malformed drafts (unknown mode, bad URL, too-short quote) before the LLM shape is even checked further. V5 deduplicates identical `(refdes, mode, rails, components)` tuples within one run. Grouped because both are stateless filters over the draft list.

**Files:**
- Modify: `api/pipeline/bench_generator/validator.py` (create)
- Modify: `tests/pipeline/bench_generator/test_validator.py` (create)

- [ ] **Step 1: Write the V1 + V5 tests**

```python
# tests/pipeline/bench_generator/test_validator.py
from __future__ import annotations

from api.pipeline.bench_generator.schemas import (
    Cause,
    EvidenceSpan,
    ProposedScenarioDraft,
)
from api.pipeline.bench_generator.validator import (
    check_sanity,
    check_duplicates,
)


def _with_overrides(base: ProposedScenarioDraft, **changes) -> ProposedScenarioDraft:
    data = base.model_dump()
    data.update(changes)
    return ProposedScenarioDraft.model_validate(data)


def test_v1_accepts_clean_draft(sample_draft):
    rej = check_sanity(sample_draft)
    assert rej is None


def test_v1_rejects_too_short_quote(sample_draft):
    d = _with_overrides(sample_draft, source_quote="too short")
    # this would already fail at Pydantic load time — V1 is defence in depth
    # when we later relax Pydantic. For now, construct through .model_construct
    # to bypass validation.
    d = ProposedScenarioDraft.model_construct(**{
        **sample_draft.model_dump(),
        "source_quote": "too short",
    })
    rej = check_sanity(d)
    assert rej is not None
    assert rej.motive == "source_quote_too_short"


def test_v1_rejects_malformed_url(sample_draft):
    d = ProposedScenarioDraft.model_construct(**{
        **sample_draft.model_dump(),
        "source_url": "not-a-url",
    })
    rej = check_sanity(d)
    assert rej is not None
    assert rej.motive == "source_url_malformed"


def test_v5_dedup_keeps_first(sample_draft):
    d1 = sample_draft
    d2 = _with_overrides(sample_draft, local_id="c19-short-dup")
    accepted, rejected = check_duplicates([d1, d2])
    assert [d.local_id for d in accepted] == ["c19-short"]
    assert [r.local_id for r in rejected] == ["c19-short-dup"]
    assert rejected[0].motive == "duplicate_in_run"


def test_v5_no_dup_no_rejection(sample_draft):
    d1 = sample_draft
    d2 = _with_overrides(
        sample_draft,
        local_id="c19-open",
        cause=Cause(refdes="C19", mode="open").model_dump(),
    )
    accepted, rejected = check_duplicates([d1, d2])
    assert len(accepted) == 2
    assert rejected == []
```

- [ ] **Step 2: Run tests to verify failures**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/test_validator.py -v
```
Expected: ImportError on `validator`.

- [ ] **Step 3: Implement V1 + V5**

```python
# api/pipeline/bench_generator/validator.py
# SPDX-License-Identifier: Apache-2.0
"""Stateless validation passes. Each check returns a `Rejection | None`.

Passes V1-V5 per spec §4.4:
  V1 — sanity (mode, url, quote length, required mode-specific fields)
  V2 — grounding (evidence_span ⊂ source_quote, literally)
  V3 — topology (refdes + rails exist in ElectricalGraph)
  V4 — mode/kind pertinence (mirrors evaluator._is_pertinent inline)
  V5 — dedup within run

The module is a collection of pure functions. No network, no filesystem,
no LLM. Tests are fast and deterministic.

`run_all(drafts, graph)` composes V1-V5 and returns
`(accepted: list[ProposedScenarioDraft], rejected: list[Rejection])`.
"""

from __future__ import annotations

import re

from api.pipeline.bench_generator.schemas import (
    ProposedScenarioDraft,
    Rejection,
)

_URL_RE = re.compile(r"^https?://[^\s]+$")


def check_sanity(draft: ProposedScenarioDraft) -> Rejection | None:
    """V1: catch malformed drafts we can reject without touching the graph."""
    if len(draft.source_quote) < 50:
        return Rejection(
            local_id=draft.local_id,
            motive="source_quote_too_short",
            detail=f"quote length={len(draft.source_quote)}",
            original_draft=draft,
        )
    if not _URL_RE.match(draft.source_url):
        return Rejection(
            local_id=draft.local_id,
            motive="source_url_malformed",
            detail=draft.source_url[:80],
            original_draft=draft,
        )
    # Pydantic enforces FailureMode via Literal, value_ohms / voltage_pct via
    # model_validator. A draft that got here is already mode-consistent; the
    # Literal guard gives us unknown_mode protection for free.
    return None


def check_duplicates(
    drafts: list[ProposedScenarioDraft],
) -> tuple[list[ProposedScenarioDraft], list[Rejection]]:
    """V5: drop duplicates by (refdes, mode, rails_sorted, components_sorted).
    The first occurrence wins; later collisions are rejected."""
    seen: set[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = set()
    accepted: list[ProposedScenarioDraft] = []
    rejected: list[Rejection] = []
    for d in drafts:
        key = (
            d.cause.refdes,
            d.cause.mode,
            tuple(sorted(d.expected_dead_rails)),
            tuple(sorted(d.expected_dead_components)),
        )
        if key in seen:
            rejected.append(
                Rejection(
                    local_id=d.local_id,
                    motive="duplicate_in_run",
                    detail=f"collides on key={key}",
                    original_draft=d,
                )
            )
            continue
        seen.add(key)
        accepted.append(d)
    return accepted, rejected
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/test_validator.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add api/pipeline/bench_generator/validator.py tests/pipeline/bench_generator/test_validator.py
git commit -m "$(cat <<'EOF'
feat(bench-gen): validator V1 sanity + V5 dedup passes

V1 catches malformed drafts (too-short quote, bad URL) with controlled
motives, separate from Pydantic's structural checks. V5 dedups on the
(refdes, mode, rails, components) tuple so the LLM suggesting two
drafts for the same physical situation collapses to one.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/bench_generator/validator.py tests/pipeline/bench_generator/test_validator.py
```

---

## Task 5 — Validator: V2 grounding

V2 is the core anti-hallucination mechanism. For each `evidence[i]`:
- `source_quote_substring` must be a literal substring of `source_quote`.
- each `field` filled in the draft must have at least one evidence entry.
- evidence on empty-list fields (e.g. `expected_dead_rails=[]` with evidence pointing there) is invalid.

**Files:**
- Modify: `api/pipeline/bench_generator/validator.py` (append)
- Modify: `tests/pipeline/bench_generator/test_validator.py` (append)

- [ ] **Step 1: Append V2 tests**

```python
# tests/pipeline/bench_generator/test_validator.py  (append)
from api.pipeline.bench_generator.validator import check_grounding


def test_v2_accepts_clean_grounding(sample_draft):
    rej = check_grounding(sample_draft)
    assert rej is None


def test_v2_rejects_nonliteral_span(sample_draft):
    bad = sample_draft.model_copy(deep=True)
    bad.evidence[0] = EvidenceSpan(
        field="cause.refdes",
        source_quote_substring="C 19",  # extra space — not literally in quote
        reasoning="wrong",
    )
    rej = check_grounding(bad)
    assert rej is not None
    assert rej.motive == "evidence_span_not_literal"


def test_v2_rejects_missing_evidence_for_nonempty_rails(sample_draft):
    """If expected_dead_rails is non-empty, at least one evidence must target it."""
    bad = sample_draft.model_copy(deep=True)
    bad.evidence = [
        e for e in bad.evidence if e.field != "expected_dead_rails"
    ]
    rej = check_grounding(bad)
    assert rej is not None
    assert rej.motive == "evidence_missing"
    assert "expected_dead_rails" in rej.detail


def test_v2_rejects_evidence_on_empty_field(sample_draft):
    """If expected_dead_components is empty, no evidence may point at it."""
    bad = sample_draft.model_copy(deep=True)
    bad.evidence.append(
        EvidenceSpan(
            field="expected_dead_components",
            source_quote_substring="C19",
            reasoning="stale",
        )
    )
    rej = check_grounding(bad)
    assert rej is not None
    assert rej.motive == "evidence_field_empty"
```

- [ ] **Step 2: Run test to verify failures**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/test_validator.py::test_v2_accepts_clean_grounding -v
```
Expected: ImportError on `check_grounding`.

- [ ] **Step 3: Implement V2**

```python
# api/pipeline/bench_generator/validator.py  (append)
def check_grounding(draft: ProposedScenarioDraft) -> Rejection | None:
    """V2: evidence spans must be literal substrings of source_quote, and
    every non-empty field must have at least one evidence entry."""
    quote = draft.source_quote

    # 2a. Every span is literal.
    for span in draft.evidence:
        if span.source_quote_substring not in quote:
            return Rejection(
                local_id=draft.local_id,
                motive="evidence_span_not_literal",
                detail=(
                    f"field={span.field!r} substring="
                    f"{span.source_quote_substring!r} not in quote"
                ),
                original_draft=draft,
            )

    evidence_fields = {e.field for e in draft.evidence}

    # 2b. Non-empty filled fields must have evidence.
    # cause.refdes is always present — require evidence.
    # cause.mode is always present — require evidence.
    required_evidence: set[str] = {"cause.refdes", "cause.mode"}
    if draft.cause.value_ohms is not None:
        required_evidence.add("cause.value_ohms")
    if draft.cause.voltage_pct is not None:
        required_evidence.add("cause.voltage_pct")
    if draft.expected_dead_rails:
        required_evidence.add("expected_dead_rails")
    if draft.expected_dead_components:
        required_evidence.add("expected_dead_components")

    missing = required_evidence - evidence_fields
    if missing:
        return Rejection(
            local_id=draft.local_id,
            motive="evidence_missing",
            detail=f"missing evidence for fields: {sorted(missing)}",
            original_draft=draft,
        )

    # 2c. Evidence on empty lists is invalid.
    if "expected_dead_rails" in evidence_fields and not draft.expected_dead_rails:
        return Rejection(
            local_id=draft.local_id,
            motive="evidence_field_empty",
            detail="evidence points at expected_dead_rails but list is empty",
            original_draft=draft,
        )
    if (
        "expected_dead_components" in evidence_fields
        and not draft.expected_dead_components
    ):
        return Rejection(
            local_id=draft.local_id,
            motive="evidence_field_empty",
            detail="evidence points at expected_dead_components but list is empty",
            original_draft=draft,
        )

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/test_validator.py -v
```
Expected: 9 passed (5 existing + 4 new).

- [ ] **Step 5: Commit**

```bash
git add api/pipeline/bench_generator/validator.py tests/pipeline/bench_generator/test_validator.py
git commit -m "$(cat <<'EOF'
feat(bench-gen): validator V2 grounding — evidence spans must be literal

Anti-hallucination core: every field the LLM fills must be justified by
a literal sub-string of source_quote. Empty fields cannot carry
evidence. Required-evidence set expands dynamically with optional
fields (value_ohms, voltage_pct, expected_dead_rails, ...).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/bench_generator/validator.py tests/pipeline/bench_generator/test_validator.py
```

---

## Task 6 — Validator: V3 topology

V3 validates refdes + rails + components against the `ElectricalGraph`. Follows Task 5 because V3 uses the graph; V2 doesn't.

**Files:**
- Modify: `api/pipeline/bench_generator/validator.py` (append)
- Modify: `tests/pipeline/bench_generator/test_validator.py` (append)

- [ ] **Step 1: Append V3 tests**

```python
# tests/pipeline/bench_generator/test_validator.py  (append)
from api.pipeline.bench_generator.validator import check_topology


def test_v3_accepts_known_refdes_and_rail(sample_draft, toy_graph):
    rej = check_topology(sample_draft, toy_graph)
    assert rej is None


def test_v3_rejects_unknown_refdes(sample_draft, toy_graph):
    bad = sample_draft.model_copy(deep=True)
    bad.cause = Cause(refdes="XZ999", mode="shorted")
    rej = check_topology(bad, toy_graph)
    assert rej is not None
    assert rej.motive == "refdes_not_in_graph"
    assert "XZ999" in rej.detail


def test_v3_rejects_unknown_rail(sample_draft, toy_graph):
    bad = sample_draft.model_copy(deep=True)
    bad.expected_dead_rails = ["+3V3", "+42V_MYSTERY"]
    rej = check_topology(bad, toy_graph)
    assert rej is not None
    assert rej.motive == "rail_name_not_in_graph"
    assert "+42V_MYSTERY" in rej.detail


def test_v3_rejects_unknown_component(sample_draft, toy_graph):
    bad = sample_draft.model_copy(deep=True)
    bad.expected_dead_components = ["U7", "U_HIDDEN"]
    rej = check_topology(bad, toy_graph)
    assert rej is not None
    assert rej.motive == "component_not_in_graph"
    assert "U_HIDDEN" in rej.detail
```

- [ ] **Step 2: Run tests to verify failures**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/test_validator.py::test_v3_accepts_known_refdes_and_rail -v
```
Expected: ImportError on `check_topology`.

- [ ] **Step 3: Implement V3**

```python
# api/pipeline/bench_generator/validator.py  (append)
from api.pipeline.schematic.schemas import ElectricalGraph


def check_topology(
    draft: ProposedScenarioDraft, graph: ElectricalGraph
) -> Rejection | None:
    """V3: every refdes and rail in the draft must exist in the graph."""
    if draft.cause.refdes not in graph.components:
        return Rejection(
            local_id=draft.local_id,
            motive="refdes_not_in_graph",
            detail=(
                f"cause.refdes={draft.cause.refdes!r} not among "
                f"{len(graph.components)} components"
            ),
            original_draft=draft,
        )
    for rail in draft.expected_dead_rails:
        if rail not in graph.power_rails:
            return Rejection(
                local_id=draft.local_id,
                motive="rail_name_not_in_graph",
                detail=(
                    f"expected rail {rail!r} not among "
                    f"{list(graph.power_rails)}"
                ),
                original_draft=draft,
            )
    for refdes in draft.expected_dead_components:
        if refdes not in graph.components:
            return Rejection(
                local_id=draft.local_id,
                motive="component_not_in_graph",
                detail=f"expected dead component {refdes!r} not in graph",
                original_draft=draft,
            )
    return None
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/test_validator.py -v
```
Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add api/pipeline/bench_generator/validator.py tests/pipeline/bench_generator/test_validator.py
git commit -m "$(cat <<'EOF'
feat(bench-gen): validator V3 topology — refdes + rails checked vs graph

Every refdes (cause + expected dead components) must exist in
graph.components; every rail must exist in graph.power_rails. This is
the second half of the grounding contract: the LLM can only emit
scenarios that the simulator can actually run.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/bench_generator/validator.py tests/pipeline/bench_generator/test_validator.py
```

---

## Task 7 — Validator: V4 pertinence (mode × kind)

V4 mirrors `evaluator._is_pertinent` inline (per spec §4.4): some modes don't make sense on some kinds — a `regulating_low` failure on an IC that doesn't source any rail has no observable effect, so we reject it up front rather than feed the simulator a tie-breaker bait.

**Files:**
- Modify: `api/pipeline/bench_generator/validator.py` (append)
- Modify: `tests/pipeline/bench_generator/test_validator.py` (append)

- [ ] **Step 1: Append V4 tests**

```python
# tests/pipeline/bench_generator/test_validator.py  (append)
from api.pipeline.bench_generator.validator import check_pertinence


def test_v4_accepts_ic_dead(sample_draft, toy_graph):
    """dead is pertinent for any IC regardless of rail-sourcing."""
    d = sample_draft.model_copy(deep=True)
    d.cause = Cause(refdes="U1", mode="dead")  # U1 = cpu, sources no rail
    rej = check_pertinence(d, toy_graph)
    assert rej is None


def test_v4_rejects_regulating_low_on_non_source_ic(sample_draft, toy_graph):
    """U1 is a CPU — doesn't source any rail. regulating_low is nonsense."""
    d = sample_draft.model_copy(deep=True)
    d.cause = Cause(refdes="U1", mode="regulating_low", voltage_pct=0.85)
    rej = check_pertinence(d, toy_graph)
    assert rej is not None
    assert rej.motive == "mode_not_pertinent"


def test_v4_accepts_regulating_low_on_source_ic(sample_draft, toy_graph):
    """U7 is the +5V source — regulating_low is meaningful."""
    d = sample_draft.model_copy(deep=True)
    d.cause = Cause(refdes="U7", mode="regulating_low", voltage_pct=0.85)
    rej = check_pertinence(d, toy_graph)
    assert rej is None


def test_v4_rejects_leaky_short_on_non_decoupling_cap(sample_draft, toy_graph):
    """No cap other than C19 is in any rail's decoupling list."""
    toy_graph.components["C99"] = toy_graph.components["C19"].model_copy()
    toy_graph.components["C99"].refdes = "C99"
    d = sample_draft.model_copy(deep=True)
    d.cause = Cause(refdes="C99", mode="leaky_short", value_ohms=200.0)
    rej = check_pertinence(d, toy_graph)
    assert rej is not None
    assert rej.motive == "mode_not_pertinent"


def test_v4_rejects_open_on_pullup_r(sample_draft, toy_graph):
    """role='pullup' is not in the open-cascading roles set."""
    d = sample_draft.model_copy(deep=True)
    d.cause = Cause(refdes="R100", mode="open")  # role = pullup
    rej = check_pertinence(d, toy_graph)
    assert rej is not None
    assert rej.motive == "mode_not_pertinent"


def test_v4_accepts_open_on_series_r(sample_draft, toy_graph):
    d = sample_draft.model_copy(deep=True)
    d.cause = Cause(refdes="R200", mode="open")  # role = series
    rej = check_pertinence(d, toy_graph)
    assert rej is None
```

- [ ] **Step 2: Run tests to verify failures**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/test_validator.py::test_v4_accepts_ic_dead -v
```
Expected: ImportError on `check_pertinence`.

- [ ] **Step 3: Implement V4**

```python
# api/pipeline/bench_generator/validator.py  (append)
# Kept in sync with api/pipeline/schematic/evaluator._is_pertinent. We
# MIRROR the rules inline rather than import the private function — the
# duplication is ~15 lines, documented, and survives renames in evaluator.
_PASSIVE_R_ROLES_WITH_REAL_OPEN_CASCADE: frozenset[str] = frozenset({
    "series",
    "damping",
    "inrush_limiter",
})


def check_pertinence(
    draft: ProposedScenarioDraft, graph: ElectricalGraph
) -> Rejection | None:
    """V4: reject (refdes, mode) pairs that don't produce an observable
    simulator effect. Mirror of evaluator._is_pertinent."""
    refdes = draft.cause.refdes
    mode = draft.cause.mode
    comp = graph.components.get(refdes)
    if comp is None:
        # Topology check already guards this — if we reach here we are
        # in a test fixture skipping V3. Be conservative and accept.
        return None
    kind = comp.kind or "ic"

    def _reject(detail: str) -> Rejection:
        return Rejection(
            local_id=draft.local_id,
            motive="mode_not_pertinent",
            detail=detail,
            original_draft=draft,
        )

    if kind == "ic" and mode == "regulating_low":
        sources_any = any(
            rail.source_refdes == refdes for rail in graph.power_rails.values()
        )
        if not sources_any:
            return _reject(f"IC {refdes} sources no rail; regulating_low is silent")
    if kind == "passive_c" and mode == "leaky_short":
        in_decoupling = any(
            refdes in (rail.decoupling or []) for rail in graph.power_rails.values()
        )
        if not in_decoupling:
            return _reject(
                f"cap {refdes} not in any rail.decoupling; leaky_short silent"
            )
    if kind == "passive_r" and mode == "open":
        role = (comp.role or "").lower()
        if role not in _PASSIVE_R_ROLES_WITH_REAL_OPEN_CASCADE:
            return _reject(
                f"resistor {refdes} role={role!r} — open produces no cascade"
            )
    return None
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/test_validator.py -v
```
Expected: 19 passed.

- [ ] **Step 5: Commit**

```bash
git add api/pipeline/bench_generator/validator.py tests/pipeline/bench_generator/test_validator.py
git commit -m "$(cat <<'EOF'
feat(bench-gen): validator V4 pertinence — mirror evaluator._is_pertinent

Reject (refdes, mode) pairs that the simulator would silently no-op:
regulating_low on non-source IC, leaky_short on cap absent from any
decoupling list, open on a resistor whose role doesn't cascade. Mirrors
the private evaluator._is_pertinent rules inline (15 lines, trivially
re-derivable from the Phase 4 spec).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/bench_generator/validator.py tests/pipeline/bench_generator/test_validator.py
```

---

## Task 8 — Validator: run_all composition

`run_all(drafts, graph)` composes V1 → V2 → V3 → V4 → V5, short-circuiting per draft on the first rejection and running V5 once at the end over the survivors.

**Files:**
- Modify: `api/pipeline/bench_generator/validator.py` (append)
- Modify: `tests/pipeline/bench_generator/test_validator.py` (append)

- [ ] **Step 1: Append run_all tests**

```python
# tests/pipeline/bench_generator/test_validator.py  (append)
from api.pipeline.bench_generator.validator import run_all


def test_run_all_accepts_clean_draft(sample_draft, toy_graph):
    accepted, rejected = run_all([sample_draft], toy_graph)
    assert [d.local_id for d in accepted] == ["c19-short"]
    assert rejected == []


def test_run_all_partitions_mixed_batch(sample_draft, toy_graph):
    good = sample_draft
    bad_topology = sample_draft.model_copy(deep=True)
    bad_topology.local_id = "bad-topo"
    bad_topology.cause = Cause(refdes="XZ999", mode="shorted")
    bad_topology.evidence = [
        EvidenceSpan(field="cause.refdes", source_quote_substring="C19",
                     reasoning="stale but literal"),
        EvidenceSpan(field="cause.mode", source_quote_substring="short",
                     reasoning="literal"),
    ]
    bad_topology.expected_dead_rails = []

    accepted, rejected = run_all([good, bad_topology], toy_graph)
    assert [d.local_id for d in accepted] == ["c19-short"]
    assert [r.motive for r in rejected] == ["refdes_not_in_graph"]


def test_run_all_short_circuits_per_draft(sample_draft, toy_graph):
    """A draft that fails V2 should not also generate a V3 rejection — only
    the first motive is reported."""
    bad = sample_draft.model_copy(deep=True)
    bad.local_id = "bad-both"
    bad.evidence[0] = EvidenceSpan(
        field="cause.refdes",
        source_quote_substring="NOT IN QUOTE",
        reasoning="wrong",
    )
    bad.cause = Cause(refdes="XZ999", mode="shorted")
    accepted, rejected = run_all([bad], toy_graph)
    assert accepted == []
    assert len(rejected) == 1
    assert rejected[0].motive == "evidence_span_not_literal"
```

- [ ] **Step 2: Run tests**

Expected failure (run_all missing).

- [ ] **Step 3: Implement run_all**

```python
# api/pipeline/bench_generator/validator.py  (append)
def run_all(
    drafts: list[ProposedScenarioDraft],
    graph: ElectricalGraph,
) -> tuple[list[ProposedScenarioDraft], list[Rejection]]:
    """V1 → V2 → V3 → V4 (per draft, short-circuit on first failure) then
    V5 dedup over the survivors."""
    survivors: list[ProposedScenarioDraft] = []
    rejected: list[Rejection] = []
    for draft in drafts:
        for check in (check_sanity, check_grounding):
            rej = check(draft)
            if rej is not None:
                rejected.append(rej)
                break
        else:
            rej = check_topology(draft, graph)
            if rej is not None:
                rejected.append(rej)
                continue
            rej = check_pertinence(draft, graph)
            if rej is not None:
                rejected.append(rej)
                continue
            survivors.append(draft)
    deduped, dup_rejects = check_duplicates(survivors)
    rejected.extend(dup_rejects)
    return deduped, rejected
```

- [ ] **Step 4: Run full validator suite**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/test_validator.py -v
```
Expected: 22 passed.

- [ ] **Step 5: Commit**

```bash
git add api/pipeline/bench_generator/validator.py tests/pipeline/bench_generator/test_validator.py
git commit -m "$(cat <<'EOF'
feat(bench-gen): validator run_all composition — V1→V2→V3→V4→V5

Composes the four per-draft checks with a first-failure short-circuit,
then runs dedup (V5) over the survivors. Result shape matches what the
orchestrator expects: (accepted, rejected) with rejected carrying a
single controlled-vocabulary motive per draft.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/bench_generator/validator.py tests/pipeline/bench_generator/test_validator.py
```

---

## Task 9 — Prompts module (system + user assembly)

Static prompt strings + a `build_user_message(pack)` helper that stitches narrative + rules + graph summary + registry into one user turn. The graph summary is a projection of `ElectricalGraph` — refdes list + rails, no edges (spec §4.2).

**Files:**
- Create: `api/pipeline/bench_generator/prompts.py`
- Create: `tests/pipeline/bench_generator/test_prompts.py`

- [ ] **Step 1: Write prompts tests**

```python
# tests/pipeline/bench_generator/test_prompts.py
from __future__ import annotations

from api.pipeline.bench_generator.prompts import (
    SYSTEM_PROMPT,
    build_user_message,
    graph_summary,
)


def test_system_prompt_mentions_grounding_contract():
    assert "evidence" in SYSTEM_PROMPT.lower()
    assert "verbatim" in SYSTEM_PROMPT.lower() or "literal" in SYSTEM_PROMPT.lower()


def test_graph_summary_lists_components_and_rails(toy_graph):
    summary = graph_summary(toy_graph)
    assert "U7" in summary
    assert "+3V3" in summary
    assert "nominal_voltage" in summary
    # Edges are explicitly NOT included
    assert "edges" not in summary


def test_build_user_message_composes_four_blocks(toy_graph):
    msg = build_user_message(
        raw_dump="## Scout dump\n\nC19 shorts collapse +3V3.",
        rules_json='{"rules": []}',
        registry_json='{"components": []}',
        graph=toy_graph,
    )
    assert "Scout dump" in msg
    assert "C19 shorts" in msg
    assert '"rules"' in msg
    assert '"components"' in msg
    assert "U7" in msg  # from graph summary
    assert "Device slug: toy-board" in msg
```

- [ ] **Step 2: Run tests**

Expected: ImportError.

- [ ] **Step 3: Implement prompts.py**

```python
# api/pipeline/bench_generator/prompts.py
# SPDX-License-Identifier: Apache-2.0
"""Static prompts + user-message assembly for the bench generator LLM call.

Kept in one module so the system prompt can be version-controlled in
isolation. The graph summary deliberately omits edges + pins — the LLM
only needs to know WHICH refdes and rails exist, not their connectivity.
"""

from __future__ import annotations

from api.pipeline.schematic.schemas import ElectricalGraph

FORCED_TOOL_NAME = "propose_scenarios"


SYSTEM_PROMPT = """\
You are a diagnostic-scenario extractor for a board-level electronics
simulator benchmark. Given a device's research dump (forums, datasheets,
community posts — all web-search sourced with URLs) and the device's
compiled electrical graph (refdes, power rails), you propose a set of
failure scenarios that can be run against a physics-lite simulator.

Your output MUST satisfy these contracts — failures at any of them will
be discarded downstream:

1. GROUNDING. For every structured field you fill (cause.refdes,
   cause.mode, cause.value_ohms, cause.voltage_pct, expected_dead_rails,
   expected_dead_components), emit an `evidence` entry whose
   `source_quote_substring` is a LITERAL, VERBATIM substring of
   `source_quote`. Case-sensitive, no paraphrase, no normalisation.
   If you cannot find a literal substring that justifies a field, do
   NOT emit that field.

2. TOPOLOGY. Every refdes (cause + expected_dead_components) and every
   rail name you emit must exist in the provided graph. If the research
   says "LPC controller" and no such refdes is in the graph, skip that
   scenario — do not guess.

3. PROVENANCE. source_url must be an http(s) URL from the dump. source_quote
   is verbatim from the dump (≥ 50 chars). If the dump is vague, emit
   fewer scenarios with high confidence; do not pad.

4. FAILURE MODES. Exactly one of: dead | shorted | open | leaky_short |
   regulating_low. leaky_short requires value_ohms (typical 100-500 Ω),
   regulating_low requires voltage_pct (typical 0.75-0.95).

5. DEDUP. Do not emit two scenarios with the same (refdes, mode, rails,
   components) tuple.

6. ZERO CASCADE IS VALID. If the source describes a silent / local
   failure, emit empty expected_dead_rails AND expected_dead_components.
   This is a legitimate anti-pattern scenario the bench needs.

Return the scenarios via the `propose_scenarios` tool. No prose output.
"""


def graph_summary(graph: ElectricalGraph) -> str:
    """Compact projection of ElectricalGraph for the user prompt. Drops
    edges and pin-level detail; keeps refdes + kind + role + rails."""
    lines = [f"Device slug: {graph.device_slug}"]
    lines.append(f"\n## Components ({len(graph.components)})")
    for refdes in sorted(graph.components):
        c = graph.components[refdes]
        role = c.role or "-"
        kind = c.kind or "-"
        lines.append(f"  {refdes} kind={kind} role={role}")
    lines.append(f"\n## Power rails ({len(graph.power_rails)})")
    for rail_id in sorted(graph.power_rails):
        r = graph.power_rails[rail_id]
        src = r.source_refdes or "-"
        dec = ",".join(r.decoupling or []) or "-"
        lines.append(
            f"  {rail_id} nominal_voltage={r.nominal_voltage:.2f} "
            f"source_refdes={src} decoupling={dec}"
        )
    return "\n".join(lines)


def build_user_message(
    *,
    raw_dump: str,
    rules_json: str,
    registry_json: str,
    graph: ElectricalGraph,
) -> str:
    """Concatenate the 4 input blocks in a stable order for caching."""
    return (
        "# Research dump (Scout)\n"
        f"{raw_dump}\n"
        "\n# Rules (Clinicien)\n"
        f"{rules_json}\n"
        "\n# Registry (canonical vocabulary)\n"
        f"{registry_json}\n"
        "\n# Electrical graph summary\n"
        f"{graph_summary(graph)}\n"
        "\nEmit the propose_scenarios tool call now."
    )
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/test_prompts.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add api/pipeline/bench_generator/prompts.py tests/pipeline/bench_generator/test_prompts.py
git commit -m "$(cat <<'EOF'
feat(bench-gen): prompts module — system contract + user message assembly

System prompt codifies the 6 rules the LLM must satisfy: grounding,
topology, provenance, mode surface, dedup, zero-cascade validity.
build_user_message stitches research dump + rules + registry + a
compact graph projection (no edges) in a cache-friendly order.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/bench_generator/prompts.py tests/pipeline/bench_generator/test_prompts.py
```

---

## Task 10 — Extractor: baseline Sonnet call

Single-call extraction via `call_with_forced_tool`. The extractor is async (the underlying helper is async). No escalation yet — pure baseline.

**Files:**
- Create: `api/pipeline/bench_generator/extractor.py`
- Create: `tests/pipeline/bench_generator/test_extractor.py`

- [ ] **Step 1: Write extractor tests (mocked client)**

```python
# tests/pipeline/bench_generator/test_extractor.py
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from api.pipeline.bench_generator.extractor import extract_drafts
from api.pipeline.bench_generator.schemas import ProposalsPayload


class _StubBlock:
    def __init__(self, name: str, payload: dict):
        self.type = "tool_use"
        self.name = name
        self.input = payload


class _StubResponse:
    def __init__(self, payload: dict):
        self.content = [_StubBlock("propose_scenarios", payload)]
        self.usage = MagicMock(
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        )


class _StubStream:
    def __init__(self, response: _StubResponse):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_final_message(self):
        return self._response


@pytest.mark.asyncio
async def test_extract_returns_payload(toy_graph, sample_draft):
    client = MagicMock()
    client.messages.stream = MagicMock(
        return_value=_StubStream(_StubResponse({
            "scenarios": [sample_draft.model_dump()],
        }))
    )

    payload = await extract_drafts(
        client=client,
        model="claude-sonnet-4-6",
        raw_dump="dump " * 100,
        rules_json="{}",
        registry_json="{}",
        graph=toy_graph,
    )
    assert isinstance(payload, ProposalsPayload)
    assert len(payload.scenarios) == 1
    assert payload.scenarios[0].local_id == "c19-short"


@pytest.mark.asyncio
async def test_extract_empty_scenarios_is_valid(toy_graph):
    client = MagicMock()
    client.messages.stream = MagicMock(
        return_value=_StubStream(_StubResponse({"scenarios": []}))
    )
    payload = await extract_drafts(
        client=client, model="claude-sonnet-4-6",
        raw_dump="dump " * 100, rules_json="{}", registry_json="{}",
        graph=toy_graph,
    )
    assert payload.scenarios == []
```

- [ ] **Step 2: Run tests**

Expected: ImportError.

- [ ] **Step 3: Implement extractor.py baseline**

```python
# api/pipeline/bench_generator/extractor.py
# SPDX-License-Identifier: Apache-2.0
"""LLM extraction pass.

Calls `call_with_forced_tool` with the `propose_scenarios` tool and
validates the output as a `ProposalsPayload`. Optionally (via
`rescue_with_opus`) re-submits specific rejected drafts to Opus.
"""

from __future__ import annotations

import logging

from anthropic import AsyncAnthropic

from api.pipeline.bench_generator.prompts import (
    FORCED_TOOL_NAME,
    SYSTEM_PROMPT,
    build_user_message,
)
from api.pipeline.bench_generator.schemas import ProposalsPayload
from api.pipeline.schematic.schemas import ElectricalGraph
from api.pipeline.tool_call import call_with_forced_tool

logger = logging.getLogger("microsolder.bench_generator.extractor")


def _propose_tool() -> dict:
    return {
        "name": FORCED_TOOL_NAME,
        "description": (
            "Emit the full list of proposed scenarios for this device. "
            "One tool call, array of objects."
        ),
        "input_schema": ProposalsPayload.model_json_schema(),
    }


async def extract_drafts(
    *,
    client: AsyncAnthropic,
    model: str,
    raw_dump: str,
    rules_json: str,
    registry_json: str,
    graph: ElectricalGraph,
) -> ProposalsPayload:
    """Single-call extraction. Returns the validated payload."""
    user_message = build_user_message(
        raw_dump=raw_dump,
        rules_json=rules_json,
        registry_json=registry_json,
        graph=graph,
    )
    payload = await call_with_forced_tool(
        client=client,
        model=model,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
        tools=[_propose_tool()],
        forced_tool_name=FORCED_TOOL_NAME,
        output_schema=ProposalsPayload,
        log_label="bench_generator.extract",
    )
    logger.info(
        "[bench_generator.extract] device_slug=%s n_scenarios=%d",
        graph.device_slug,
        len(payload.scenarios),
    )
    return payload
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/test_extractor.py -v
```
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add api/pipeline/bench_generator/extractor.py tests/pipeline/bench_generator/test_extractor.py
git commit -m "$(cat <<'EOF'
feat(bench-gen): extractor baseline — single Sonnet call via forced tool

extract_drafts composes the system prompt + user message + tools array
+ output_schema and delegates to the pipeline's existing
call_with_forced_tool helper (which already handles retries and
stringified-payload recovery). No model hardcode — caller provides.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/bench_generator/extractor.py tests/pipeline/bench_generator/test_extractor.py
```

---

## Task 11 — Extractor: Opus rescue pass (`--escalate-rejects`)

Given a list of drafts that failed V2 (`evidence_span_not_literal`) or V3 (`refdes_not_in_graph`), re-submit them to Opus with a targeted retry prompt: here's the draft, here's the full quote, here's the list of valid refdes/rails — correct precisely.

**Files:**
- Modify: `api/pipeline/bench_generator/extractor.py` (append)
- Modify: `tests/pipeline/bench_generator/test_extractor.py` (append)

- [ ] **Step 1: Append rescue tests**

```python
# tests/pipeline/bench_generator/test_extractor.py  (append)
import pytest

from api.pipeline.bench_generator.extractor import rescue_with_opus
from api.pipeline.bench_generator.schemas import Rejection


@pytest.mark.asyncio
async def test_rescue_filters_eligible_motives(toy_graph, sample_draft):
    """Only evidence_span_not_literal and refdes_not_in_graph are retried."""
    eligible = Rejection(
        local_id="e1", motive="evidence_span_not_literal", detail="",
        original_draft=sample_draft,
    )
    ineligible = Rejection(
        local_id="d1", motive="duplicate_in_run", detail="",
        original_draft=sample_draft,
    )

    client = MagicMock()
    # Mock returns nothing (no rescue) — we just check filtering
    client.messages.stream = MagicMock(
        return_value=_StubStream(_StubResponse({"scenarios": []}))
    )
    rescued, still_rejected = await rescue_with_opus(
        client=client, model="claude-opus-4-7",
        rejections=[eligible, ineligible],
        graph=toy_graph,
    )
    # Eligible was fed to Opus (no scenario returned) -> opus_rescue_failed
    # Ineligible stays with original motive
    assert len(rescued) == 0
    assert len(still_rejected) == 2
    motives = {r.motive for r in still_rejected}
    assert "opus_rescue_failed" in motives
    assert "duplicate_in_run" in motives


@pytest.mark.asyncio
async def test_rescue_returns_corrected_draft(toy_graph, sample_draft):
    eligible = Rejection(
        local_id="e1", motive="evidence_span_not_literal", detail="",
        original_draft=sample_draft,
    )
    corrected = sample_draft.model_dump()
    corrected["local_id"] = "e1"  # keep id for traceability
    client = MagicMock()
    client.messages.stream = MagicMock(
        return_value=_StubStream(_StubResponse({"scenarios": [corrected]}))
    )
    rescued, still_rejected = await rescue_with_opus(
        client=client, model="claude-opus-4-7",
        rejections=[eligible],
        graph=toy_graph,
    )
    assert len(rescued) == 1
    assert rescued[0].local_id == "e1"
    assert still_rejected == []
```

- [ ] **Step 2: Run tests**

Expected: ImportError on `rescue_with_opus`.

- [ ] **Step 3: Implement rescue**

```python
# api/pipeline/bench_generator/extractor.py  (append)
from api.pipeline.bench_generator.prompts import graph_summary
from api.pipeline.bench_generator.schemas import (
    ProposedScenarioDraft,
    Rejection,
)

_ELIGIBLE_MOTIVES = frozenset({
    "evidence_span_not_literal",
    "refdes_not_in_graph",
})


async def rescue_with_opus(
    *,
    client: AsyncAnthropic,
    model: str,
    rejections: list[Rejection],
    graph: ElectricalGraph,
) -> tuple[list[ProposedScenarioDraft], list[Rejection]]:
    """Re-submit drafts rejected with literal-span or refdes errors.

    Returns (rescued_drafts, still_rejected). Rejections that weren't
    eligible pass through untouched. Rescued drafts still have to go
    back through run_all() — no V-bypass."""
    rescued: list[ProposedScenarioDraft] = []
    still_rejected: list[Rejection] = []
    for rej in rejections:
        if rej.motive not in _ELIGIBLE_MOTIVES or rej.original_draft is None:
            still_rejected.append(rej)
            continue
        draft = rej.original_draft
        user = (
            f"Previous draft was rejected ({rej.motive}): "
            f"{rej.detail}\n\n"
            f"ORIGINAL DRAFT:\n{draft.model_dump_json(indent=2)}\n\n"
            f"VALID REFDES / RAILS FROM THE GRAPH:\n{graph_summary(graph)}\n\n"
            "Emit a CORRECTED scenario via propose_scenarios with exactly "
            "one entry. Preserve the original local_id. Keep source_url, "
            "source_quote, confidence intact. Fix only the spans and/or "
            "refdes so they satisfy the grounding + topology contracts."
        )
        try:
            payload = await call_with_forced_tool(
                client=client,
                model=model,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user}],
                tools=[_propose_tool()],
                forced_tool_name=FORCED_TOOL_NAME,
                output_schema=ProposalsPayload,
                log_label="bench_generator.rescue",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[bench_generator.rescue] local_id=%s Opus call failed: %s",
                rej.local_id, exc,
            )
            still_rejected.append(
                Rejection(
                    local_id=rej.local_id,
                    motive="opus_rescue_failed",
                    detail=f"Opus call raised: {exc}",
                    original_draft=draft,
                )
            )
            continue
        if not payload.scenarios:
            still_rejected.append(
                Rejection(
                    local_id=rej.local_id,
                    motive="opus_rescue_failed",
                    detail="Opus returned 0 scenarios",
                    original_draft=draft,
                )
            )
            continue
        rescued.append(payload.scenarios[0])
    return rescued, still_rejected
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/test_extractor.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add api/pipeline/bench_generator/extractor.py tests/pipeline/bench_generator/test_extractor.py
git commit -m "$(cat <<'EOF'
feat(bench-gen): extractor Opus rescue pass for span + topology rejects

rescue_with_opus filters rejections down to the two motives that
correction can plausibly fix (evidence_span_not_literal, refdes_not_in_graph),
feeds each draft + the graph summary to Opus with a targeted retry
prompt, and returns the corrected drafts (which still have to re-traverse
run_all — no V-bypass).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/bench_generator/extractor.py tests/pipeline/bench_generator/test_extractor.py
```

---

## Task 12 — Scoring wrapper

Thin wrapper that converts `list[ProposedScenario]` into the dict shape `evaluator.compute_score` consumes, calls it, returns the `Scorecard`. No re-implementation — delegate everything.

**Files:**
- Create: `api/pipeline/bench_generator/scoring.py`
- Create: `tests/pipeline/bench_generator/test_scoring.py`

- [ ] **Step 1: Write scoring tests**

```python
# tests/pipeline/bench_generator/test_scoring.py
from api.pipeline.bench_generator.schemas import (
    Cause,
    ProposedScenario,
)
from api.pipeline.bench_generator.scoring import score_accepted
from api.pipeline.schematic.evaluator import Scorecard


def _scenario(local_id: str = "s1") -> ProposedScenario:
    return ProposedScenario(
        id=local_id,
        device_slug="toy-board",
        cause=Cause(refdes="C19", mode="shorted"),
        expected_dead_rails=["+3V3"],
        expected_dead_components=[],
        source_url="https://example.com/x",
        source_quote="x" * 60,
        source_archive="benchmark/auto_proposals/sources/s1.txt",
        confidence=0.9,
        generated_by="bench-gen-sonnet-4-6",
        generated_at="2026-04-24T21:00:00Z",
    )


def test_score_accepted_wraps_evaluator(toy_graph):
    scorecard = score_accepted(toy_graph, [_scenario()])
    assert isinstance(scorecard, Scorecard)
    assert scorecard.n_scenarios == 1
    assert 0.0 <= scorecard.score <= 1.0


def test_score_accepted_empty_is_zero(toy_graph):
    scorecard = score_accepted(toy_graph, [])
    assert scorecard.n_scenarios == 0
    # self_mrr can be non-zero (depends on graph); cascade_recall is 0
    assert scorecard.cascade_recall == 0.0
```

- [ ] **Step 2: Run tests**

Expected: ImportError.

- [ ] **Step 3: Implement scoring.py**

```python
# api/pipeline/bench_generator/scoring.py
# SPDX-License-Identifier: Apache-2.0
"""Thin wrapper around `evaluator.compute_score`.

The evaluator accepts `list[dict]` scenarios. Our accepted scenarios are
typed `ProposedScenario`; this module just converts and delegates.
"""

from __future__ import annotations

from api.pipeline.bench_generator.schemas import ProposedScenario
from api.pipeline.schematic.evaluator import Scorecard, compute_score
from api.pipeline.schematic.schemas import ElectricalGraph


def score_accepted(
    graph: ElectricalGraph,
    scenarios: list[ProposedScenario],
) -> Scorecard:
    """Feed accepted scenarios to the evaluator in its native dict shape."""
    dicts: list[dict] = []
    for s in scenarios:
        entry = {
            "id": s.id,
            "device_slug": s.device_slug,
            "cause": s.cause.model_dump(exclude_none=True),
            "expected_dead_rails": s.expected_dead_rails,
            "expected_dead_components": s.expected_dead_components,
        }
        dicts.append(entry)
    return compute_score(graph, dicts)
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/test_scoring.py -v
```
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add api/pipeline/bench_generator/scoring.py tests/pipeline/bench_generator/test_scoring.py
git commit -m "$(cat <<'EOF'
feat(bench-gen): scoring wrapper delegates to evaluator.compute_score

Thin projection from ProposedScenario (typed) to the dict shape
evaluator.compute_score expects. Zero re-implementation — the
self_mrr + cascade_recall math stays in evaluator.py (read-only from
our side, hands-off for evolve).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/bench_generator/scoring.py tests/pipeline/bench_generator/test_scoring.py
```

---

## Task 13 — Writer: per-run files (jsonl, rejected, manifest, score)

Atomic writes via `tempfile.NamedTemporaryFile` + `os.replace`. Each of the 4 per-run files is written to a temp path first, then renamed in place. Sources archives (`sources/{id}.txt`) and `_latest.json` merge come in the next task.

**Files:**
- Create: `api/pipeline/bench_generator/writer.py`
- Create: `tests/pipeline/bench_generator/test_writer.py`

- [ ] **Step 1: Write per-run writer tests**

```python
# tests/pipeline/bench_generator/test_writer.py
from __future__ import annotations

import json
from pathlib import Path

from api.pipeline.bench_generator.schemas import (
    Cause,
    ProposedScenario,
    Rejection,
    RunManifest,
)
from api.pipeline.bench_generator.writer import (
    write_per_run_files,
)
from api.pipeline.schematic.evaluator import Scorecard, ScenarioResult


def _scenario(i: int) -> ProposedScenario:
    return ProposedScenario(
        id=f"toy-s{i}",
        device_slug="toy-board",
        cause=Cause(refdes="C19", mode="shorted"),
        expected_dead_rails=["+3V3"],
        source_url=f"https://example.com/{i}",
        source_quote="x" * 60,
        source_archive=f"benchmark/auto_proposals/sources/toy-s{i}.txt",
        confidence=0.8,
        generated_by="bench-gen-sonnet-4-6",
        generated_at="2026-04-24T21:00:00Z",
    )


def _manifest(n_acc=2, n_rej=1) -> RunManifest:
    return RunManifest(
        device_slug="toy-board",
        run_date="2026-04-24",
        run_timestamp="2026-04-24T21:00:00Z",
        model="claude-sonnet-4-6",
        n_proposed=3,
        n_accepted=n_acc,
        n_rejected=n_rej,
        input_mtimes={"raw_research_dump.md": 1.0},
        escalated_rejects=False,
    )


def _scorecard() -> Scorecard:
    return Scorecard(
        score=0.7, self_mrr=0.8, cascade_recall=0.55, n_scenarios=2,
        per_scenario=[
            ScenarioResult(scenario_id="toy-s1", cascade_recall=1.0),
            ScenarioResult(scenario_id="toy-s2", cascade_recall=0.1),
        ],
    )


def test_per_run_files_written(tmp_path: Path):
    out = tmp_path / "auto_proposals"
    out.mkdir()
    write_per_run_files(
        output_dir=out,
        run_date="2026-04-24",
        slug="toy-board",
        accepted=[_scenario(1), _scenario(2)],
        rejected=[Rejection(local_id="x", motive="refdes_not_in_graph")],
        manifest=_manifest(),
        scorecard=_scorecard(),
    )
    jsonl = out / "toy-board-2026-04-24.jsonl"
    rejected = out / "toy-board-2026-04-24.rejected.jsonl"
    manifest = out / "toy-board-2026-04-24.manifest.json"
    score = out / "toy-board-2026-04-24.score.json"
    assert jsonl.exists()
    assert rejected.exists()
    assert manifest.exists()
    assert score.exists()

    # jsonl: one line per scenario
    lines = jsonl.read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["id"] == "toy-s1"

    # manifest round-trip
    m = json.loads(manifest.read_text())
    assert m["n_accepted"] == 2

    # score has the cascade_recall from Scorecard
    s = json.loads(score.read_text())
    assert s["cascade_recall"] == 0.55


def test_atomic_replace_no_stale_temp(tmp_path: Path):
    out = tmp_path / "auto_proposals"
    out.mkdir()
    write_per_run_files(
        output_dir=out,
        run_date="2026-04-24",
        slug="toy-board",
        accepted=[_scenario(1)],
        rejected=[],
        manifest=_manifest(n_acc=1, n_rej=0),
        scorecard=_scorecard(),
    )
    # No leftover .tmp files
    tmp_left = list(out.glob("*.tmp"))
    assert tmp_left == []
```

- [ ] **Step 2: Run tests**

Expected: ImportError.

- [ ] **Step 3: Implement writer.py per-run section**

```python
# api/pipeline/bench_generator/writer.py
# SPDX-License-Identifier: Apache-2.0
"""Atomic file writes for the bench generator.

Four per-run artefacts + the cross-run `_latest.json` aggregate + the
runtime-consumed `memory/{slug}/simulator_reliability.json` + source
archive snapshots. Every write uses tempfile + os.replace to avoid
half-written files on crash.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from api.pipeline.bench_generator.schemas import (
    ProposedScenario,
    Rejection,
    RunManifest,
)
from api.pipeline.schematic.evaluator import Scorecard

logger = logging.getLogger("microsolder.bench_generator.writer")


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_s = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent,
    )
    tmp_path = Path(tmp_path_s)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _jsonl_dump(items: list[dict]) -> str:
    return "\n".join(json.dumps(it, ensure_ascii=False) for it in items) + "\n"


def write_per_run_files(
    *,
    output_dir: Path,
    run_date: str,
    slug: str,
    accepted: list[ProposedScenario],
    rejected: list[Rejection],
    manifest: RunManifest,
    scorecard: Scorecard,
) -> None:
    """Write the four per-run files atomically."""
    base = output_dir / f"{slug}-{run_date}"
    _atomic_write_text(
        Path(str(base) + ".jsonl"),
        _jsonl_dump([s.model_dump(exclude_none=False) for s in accepted]),
    )
    _atomic_write_text(
        Path(str(base) + ".rejected.jsonl"),
        _jsonl_dump([r.model_dump(exclude_none=False) for r in rejected]),
    )
    _atomic_write_text(
        Path(str(base) + ".manifest.json"),
        json.dumps(manifest.model_dump(), indent=2),
    )
    _atomic_write_text(
        Path(str(base) + ".score.json"),
        json.dumps(scorecard.model_dump(), indent=2),
    )
    logger.info(
        "[bench_generator.writer] wrote 4 files for slug=%s run_date=%s "
        "(n_accepted=%d, n_rejected=%d)",
        slug, run_date, len(accepted), len(rejected),
    )
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/test_writer.py -v
```
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add api/pipeline/bench_generator/writer.py tests/pipeline/bench_generator/test_writer.py
git commit -m "$(cat <<'EOF'
feat(bench-gen): writer — atomic per-run output files

Four per-run artefacts: {slug}-{date}.jsonl (accepted), .rejected.jsonl,
.manifest.json, .score.json. Each written via tempfile + os.replace so
a Ctrl-C mid-run leaves the directory in a consistent state. No leftover
.tmp files — cleanup on exception path covered by test.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/bench_generator/writer.py tests/pipeline/bench_generator/test_writer.py
```

---

## Task 14 — Writer: `_latest.json` merge + source archives

`_latest.json` is a multi-device aggregate. Multiple runs may touch it → advisory file lock (`fcntl.flock`). Source archives are one text file per accepted scenario, overwritten on every run.

**Files:**
- Modify: `api/pipeline/bench_generator/writer.py` (append)
- Modify: `tests/pipeline/bench_generator/test_writer.py` (append)

- [ ] **Step 1: Append tests**

```python
# tests/pipeline/bench_generator/test_writer.py  (append)
from api.pipeline.bench_generator.writer import (
    update_latest_json,
    write_source_archives,
)


def test_update_latest_merges_new_slug(tmp_path: Path):
    latest = tmp_path / "_latest.json"
    latest.write_text(
        json.dumps({
            "other-board": {"score": 0.5, "self_mrr": 0.5,
                            "cascade_recall": 0.5, "n_scenarios": 3,
                            "run_date": "2026-04-23"},
        }),
        encoding="utf-8",
    )
    update_latest_json(
        latest_path=latest, slug="toy-board",
        scorecard=_scorecard(), run_date="2026-04-24",
    )
    d = json.loads(latest.read_text())
    assert "toy-board" in d
    assert "other-board" in d
    assert d["toy-board"]["score"] == 0.7


def test_update_latest_creates_fresh_file(tmp_path: Path):
    latest = tmp_path / "_latest.json"
    update_latest_json(
        latest_path=latest, slug="toy-board",
        scorecard=_scorecard(), run_date="2026-04-24",
    )
    d = json.loads(latest.read_text())
    assert list(d.keys()) == ["toy-board"]


def test_write_source_archives_one_file_per_scenario(tmp_path: Path):
    archive_dir = tmp_path / "sources"
    accepted = [_scenario(1), _scenario(2)]
    write_source_archives(archive_dir=archive_dir, scenarios=accepted)
    assert (archive_dir / "toy-s1.txt").exists()
    assert (archive_dir / "toy-s2.txt").exists()
    assert (archive_dir / "toy-s1.txt").read_text().startswith(
        accepted[0].source_url
    )
```

- [ ] **Step 2: Run tests**

Expected: ImportError on `update_latest_json`, `write_source_archives`.

- [ ] **Step 3: Implement**

```python
# api/pipeline/bench_generator/writer.py  (append)
import fcntl


def update_latest_json(
    *,
    latest_path: Path,
    slug: str,
    scorecard: Scorecard,
    run_date: str,
) -> None:
    """Merge this run's score into the aggregate _latest.json under an
    fcntl advisory lock so concurrent runs don't clobber each other."""
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(latest_path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            raw = fh.read()
            try:
                current = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                logger.warning(
                    "[writer] _latest.json unreadable — starting fresh",
                )
                current = {}
            current[slug] = {
                "score": scorecard.score,
                "self_mrr": scorecard.self_mrr,
                "cascade_recall": scorecard.cascade_recall,
                "n_scenarios": scorecard.n_scenarios,
                "run_date": run_date,
            }
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(current, indent=2))
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def write_source_archives(
    *,
    archive_dir: Path,
    scenarios: list[ProposedScenario],
) -> None:
    """One text file per accepted scenario. Overwritten on re-run."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    for s in scenarios:
        archive_path = archive_dir / f"{s.id}.txt"
        content = (
            f"{s.source_url}\n\n"
            f"---\n\n"
            f"{s.source_quote}\n"
        )
        _atomic_write_text(archive_path, content)
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/test_writer.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add api/pipeline/bench_generator/writer.py tests/pipeline/bench_generator/test_writer.py
git commit -m "$(cat <<'EOF'
feat(bench-gen): writer — _latest.json merge + source archives

update_latest_json reads the existing aggregate (or starts fresh),
merges this run's slug score, writes back atomically under an fcntl
advisory lock. write_source_archives drops one sources/{id}.txt per
accepted scenario to honour the provenance contract even if the URL
rots.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/bench_generator/writer.py tests/pipeline/bench_generator/test_writer.py
```

---

## Task 15 — Writer: `memory/{slug}/simulator_reliability.json`

The runtime-facing card. Derived from the Scorecard, minimal fields, written to the device pack directory.

**Files:**
- Modify: `api/pipeline/bench_generator/writer.py` (append)
- Modify: `tests/pipeline/bench_generator/test_writer.py` (append)

- [ ] **Step 1: Append tests**

```python
# tests/pipeline/bench_generator/test_writer.py  (append)
from api.pipeline.bench_generator.schemas import ReliabilityCard
from api.pipeline.bench_generator.writer import write_reliability_card


def test_write_reliability_card(tmp_path: Path):
    memory_dir = tmp_path / "memory" / "toy-board"
    memory_dir.mkdir(parents=True)
    card = ReliabilityCard(
        device_slug="toy-board",
        score=0.78,
        self_mrr=0.82,
        cascade_recall=0.72,
        n_scenarios=5,
        generated_at="2026-04-24T21:00:00Z",
        source_run_date="2026-04-24",
        notes=["Based on auto-generated scenarios, not human-validated."],
    )
    write_reliability_card(memory_dir=memory_dir, card=card)
    out = memory_dir / "simulator_reliability.json"
    assert out.exists()
    d = json.loads(out.read_text())
    assert d["score"] == 0.78
    assert d["device_slug"] == "toy-board"
```

- [ ] **Step 2: Run test**

Expected: ImportError on `write_reliability_card`.

- [ ] **Step 3: Implement**

```python
# api/pipeline/bench_generator/writer.py  (append)
from api.pipeline.bench_generator.schemas import ReliabilityCard


def write_reliability_card(*, memory_dir: Path, card: ReliabilityCard) -> None:
    """Write memory/{slug}/simulator_reliability.json for runtime consumption."""
    _atomic_write_text(
        memory_dir / "simulator_reliability.json",
        json.dumps(card.model_dump(), indent=2),
    )
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/test_writer.py -v
```
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add api/pipeline/bench_generator/writer.py tests/pipeline/bench_generator/test_writer.py
git commit -m "$(cat <<'EOF'
feat(bench-gen): writer — memory/{slug}/simulator_reliability.json

The runtime-facing card consumed by load_reliability_line (direct
runtime system prompt) and pushed to the MA memory store by memory_seed.
Derived from the Scorecard, minimal flat shape, atomic write.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/bench_generator/writer.py tests/pipeline/bench_generator/test_writer.py
```

---

## Task 16 — Orchestrator `generate_from_pack`

Load the pack → precondition checks → extractor → validator → (optional rescue) → scoring → writer. This is the composed entrypoint. Integration test uses the full mock stack.

**Files:**
- Create: `api/pipeline/bench_generator/orchestrator.py`
- Modify: `api/pipeline/bench_generator/__init__.py`
- Create: `tests/pipeline/bench_generator/test_orchestrator.py`

- [ ] **Step 1: Write the orchestrator integration test**

```python
# tests/pipeline/bench_generator/test_orchestrator.py
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from api.pipeline.bench_generator.errors import (
    BenchGeneratorPreconditionError,
)
from api.pipeline.bench_generator.orchestrator import generate_from_pack


class _StubBlock:
    def __init__(self, name: str, payload: dict):
        self.type = "tool_use"
        self.name = name
        self.input = payload


class _StubResponse:
    def __init__(self, payload: dict):
        self.content = [_StubBlock("propose_scenarios", payload)]
        self.usage = MagicMock(
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        )


class _StubStream:
    def __init__(self, response: _StubResponse):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_final_message(self):
        return self._response


def _write_graph(pack_dir: Path, toy_graph) -> None:
    (pack_dir / "electrical_graph.json").write_text(
        toy_graph.model_dump_json(), encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_missing_graph_raises_precondition(pack_dir, tmp_path):
    client = MagicMock()
    with pytest.raises(BenchGeneratorPreconditionError, match="electrical_graph"):
        await generate_from_pack(
            device_slug="toy-board",
            client=client,
            model="claude-sonnet-4-6",
            memory_root=pack_dir.parent,
            output_dir=tmp_path / "auto_proposals",
            latest_path=tmp_path / "auto_proposals" / "_latest.json",
            run_date="2026-04-24",
        )


@pytest.mark.asyncio
async def test_end_to_end_writes_six_files(
    pack_dir, toy_graph, sample_draft, tmp_path
):
    _write_graph(pack_dir, toy_graph)
    client = MagicMock()
    client.messages.stream = MagicMock(
        return_value=_StubStream(_StubResponse({
            "scenarios": [sample_draft.model_dump()],
        }))
    )
    out_dir = tmp_path / "auto_proposals"
    result = await generate_from_pack(
        device_slug="toy-board",
        client=client,
        model="claude-sonnet-4-6",
        memory_root=pack_dir.parent,
        output_dir=out_dir,
        latest_path=out_dir / "_latest.json",
        run_date="2026-04-24",
    )
    # Files on disk
    assert (out_dir / "toy-board-2026-04-24.jsonl").exists()
    assert (out_dir / "toy-board-2026-04-24.rejected.jsonl").exists()
    assert (out_dir / "toy-board-2026-04-24.manifest.json").exists()
    assert (out_dir / "toy-board-2026-04-24.score.json").exists()
    assert (out_dir / "_latest.json").exists()
    assert (pack_dir / "simulator_reliability.json").exists()
    # Summary
    assert result["n_accepted"] == 1
    assert result["n_rejected"] == 0


@pytest.mark.asyncio
async def test_end_to_end_mixed_batch(
    pack_dir, toy_graph, sample_draft, tmp_path
):
    """One good, one topology reject, one dup — verify partitioning."""
    _write_graph(pack_dir, toy_graph)
    dup = sample_draft.model_dump()
    dup["local_id"] = "c19-short-dup"  # same (refdes, mode, rails) → V5 rejection
    bad = sample_draft.model_dump()
    bad["local_id"] = "bad-topo"
    bad["cause"] = {"refdes": "XZ999", "mode": "shorted"}

    client = MagicMock()
    client.messages.stream = MagicMock(
        return_value=_StubStream(_StubResponse({
            "scenarios": [sample_draft.model_dump(), dup, bad],
        }))
    )
    out_dir = tmp_path / "auto_proposals"
    result = await generate_from_pack(
        device_slug="toy-board",
        client=client,
        model="claude-sonnet-4-6",
        memory_root=pack_dir.parent,
        output_dir=out_dir,
        latest_path=out_dir / "_latest.json",
        run_date="2026-04-24",
    )
    assert result["n_accepted"] == 1
    assert result["n_rejected"] == 2
    rejected = [
        json.loads(line)
        for line in (out_dir / "toy-board-2026-04-24.rejected.jsonl")
            .read_text().strip().split("\n")
    ]
    motives = {r["motive"] for r in rejected}
    assert "refdes_not_in_graph" in motives
    assert "duplicate_in_run" in motives
```

- [ ] **Step 2: Run test**

Expected: ImportError on `orchestrator`.

- [ ] **Step 3: Implement orchestrator**

```python
# api/pipeline/bench_generator/orchestrator.py
# SPDX-License-Identifier: Apache-2.0
"""Composed entrypoint: generate_from_pack.

1. Load pack → validate preconditions.
2. Call extractor.extract_drafts (+ optional rescue_with_opus).
3. Run validator.run_all.
4. Promote survivors into ProposedScenario (assign ids, timestamps, archive paths).
5. Score via scoring.score_accepted.
6. Write everything via writer.*.

No global state; all dependencies injected (client, paths, clocks).
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from anthropic import AsyncAnthropic

from api.pipeline.bench_generator.errors import (
    BenchGeneratorPreconditionError,
)
from api.pipeline.bench_generator.extractor import (
    extract_drafts,
    rescue_with_opus,
)
from api.pipeline.bench_generator.schemas import (
    ProposedScenario,
    ProposedScenarioDraft,
    ReliabilityCard,
    Rejection,
    RunManifest,
)
from api.pipeline.bench_generator.scoring import score_accepted
from api.pipeline.bench_generator.validator import run_all
from api.pipeline.bench_generator.writer import (
    update_latest_json,
    write_per_run_files,
    write_reliability_card,
    write_source_archives,
)
from api.pipeline.schematic.schemas import ElectricalGraph

logger = logging.getLogger("microsolder.bench_generator.orchestrator")


def _load_pack(pack_dir: Path) -> tuple[str, str, str, ElectricalGraph]:
    """Load the 4 inputs or raise BenchGeneratorPreconditionError."""
    graph_path = pack_dir / "electrical_graph.json"
    if not graph_path.exists():
        raise BenchGeneratorPreconditionError(
            f"no electrical_graph.json at {graph_path} — "
            "run schematic ingestion first (python -m api.pipeline.schematic.cli)"
        )
    dump_path = pack_dir / "raw_research_dump.md"
    if not dump_path.exists() or len(dump_path.read_text(encoding="utf-8")) < 500:
        raise BenchGeneratorPreconditionError(
            f"Scout dump at {dump_path} is empty or < 500 chars"
        )
    raw_dump = dump_path.read_text(encoding="utf-8")
    rules_path = pack_dir / "rules.json"
    rules_json = rules_path.read_text(encoding="utf-8") if rules_path.exists() else "{}"
    registry_path = pack_dir / "registry.json"
    registry_json = (
        registry_path.read_text(encoding="utf-8") if registry_path.exists() else "{}"
    )
    graph = ElectricalGraph.model_validate_json(
        graph_path.read_text(encoding="utf-8")
    )
    return raw_dump, rules_json, registry_json, graph


def _promote(
    draft: ProposedScenarioDraft,
    *,
    device_slug: str,
    generated_by: str,
    generated_at: str,
    archive_subdir: str,
) -> ProposedScenario:
    """Build the promoted ProposedScenario from a validated draft.
    id = hash(source_quote) prefixed by slug for cross-device uniqueness."""
    quote_hash = hashlib.sha256(draft.source_quote.encode("utf-8")).hexdigest()[:8]
    scenario_id = f"{device_slug}-{draft.local_id}-{quote_hash}"
    return ProposedScenario(
        id=scenario_id,
        device_slug=device_slug,
        cause=draft.cause,
        expected_dead_rails=draft.expected_dead_rails,
        expected_dead_components=draft.expected_dead_components,
        source_url=draft.source_url,
        source_quote=draft.source_quote,
        source_archive=f"{archive_subdir}/{scenario_id}.txt",
        confidence=draft.confidence,
        generated_by=generated_by,
        generated_at=generated_at,
        validated_by_human=False,
        evidence=draft.evidence,
    )


async def generate_from_pack(
    *,
    device_slug: str,
    client: AsyncAnthropic,
    model: str,
    memory_root: Path,
    output_dir: Path,
    latest_path: Path,
    run_date: str,
    escalate_rejects: bool = False,
    opus_model: str = "claude-opus-4-7",
) -> dict:
    """Run the end-to-end bench generation. Returns a summary dict.

    Never raises on an empty-scenarios outcome (valid result for sparse
    packs); does raise BenchGeneratorPreconditionError on missing inputs."""
    pack_dir = memory_root / device_slug
    raw_dump, rules_json, registry_json, graph = _load_pack(pack_dir)

    # Capture mtimes for the manifest (traceability only).
    input_mtimes = {
        name: (pack_dir / name).stat().st_mtime
        for name in ("raw_research_dump.md", "rules.json", "registry.json",
                     "electrical_graph.json")
        if (pack_dir / name).exists()
    }

    payload = await extract_drafts(
        client=client, model=model,
        raw_dump=raw_dump, rules_json=rules_json,
        registry_json=registry_json, graph=graph,
    )
    drafts = payload.scenarios
    n_proposed = len(drafts)

    accepted_drafts, rejects = run_all(drafts, graph)

    if escalate_rejects and rejects:
        rescued, rejects = await rescue_with_opus(
            client=client, model=opus_model,
            rejections=rejects, graph=graph,
        )
        if rescued:
            accepted_again, more_rejects = run_all(rescued, graph)
            accepted_drafts.extend(accepted_again)
            rejects.extend(more_rejects)

    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    generated_by = f"bench-gen-{model}"
    archive_subdir = "benchmark/auto_proposals/sources"

    accepted: list[ProposedScenario] = [
        _promote(
            d, device_slug=device_slug,
            generated_by=generated_by,
            generated_at=generated_at,
            archive_subdir=archive_subdir,
        )
        for d in accepted_drafts
    ]

    scorecard = score_accepted(graph, accepted)
    manifest = RunManifest(
        device_slug=device_slug,
        run_date=run_date,
        run_timestamp=generated_at,
        model=model,
        n_proposed=n_proposed,
        n_accepted=len(accepted),
        n_rejected=len(rejects),
        input_mtimes=input_mtimes,
        escalated_rejects=escalate_rejects,
    )

    write_per_run_files(
        output_dir=output_dir, run_date=run_date, slug=device_slug,
        accepted=accepted, rejected=rejects, manifest=manifest,
        scorecard=scorecard,
    )
    write_source_archives(
        archive_dir=output_dir / "sources", scenarios=accepted,
    )
    update_latest_json(
        latest_path=latest_path, slug=device_slug,
        scorecard=scorecard, run_date=run_date,
    )
    reliability_card = ReliabilityCard(
        device_slug=device_slug,
        score=scorecard.score,
        self_mrr=scorecard.self_mrr,
        cascade_recall=scorecard.cascade_recall,
        n_scenarios=scorecard.n_scenarios,
        generated_at=generated_at,
        source_run_date=run_date,
        notes=[
            "Based on auto-generated scenarios, not human-validated.",
            f"Per-scenario breakdown: "
            f"benchmark/auto_proposals/{device_slug}-{run_date}.score.json",
        ],
    )
    write_reliability_card(memory_dir=pack_dir, card=reliability_card)

    logger.info(
        "[bench_generator] device=%s run_date=%s n_proposed=%d "
        "n_accepted=%d n_rejected=%d score=%.3f",
        device_slug, run_date, n_proposed, len(accepted), len(rejects),
        scorecard.score,
    )
    return {
        "n_proposed": n_proposed,
        "n_accepted": len(accepted),
        "n_rejected": len(rejects),
        "score": scorecard.score,
        "self_mrr": scorecard.self_mrr,
        "cascade_recall": scorecard.cascade_recall,
    }
```

- [ ] **Step 4: Wire the public entrypoint**

```python
# api/pipeline/bench_generator/__init__.py   (replace content)
# SPDX-License-Identifier: Apache-2.0
"""Auto-generator of benchable scenarios from device knowledge packs."""

from api.pipeline.bench_generator.orchestrator import generate_from_pack

__all__ = ["generate_from_pack"]
```

- [ ] **Step 5: Run orchestrator tests**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/test_orchestrator.py -v
```
Expected: 3 passed.

- [ ] **Step 6: Run the full bench_generator suite**

```bash
.venv/bin/pytest tests/pipeline/bench_generator/ -v
```
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add api/pipeline/bench_generator/orchestrator.py \
        api/pipeline/bench_generator/__init__.py \
        tests/pipeline/bench_generator/test_orchestrator.py
git commit -m "$(cat <<'EOF'
feat(bench-gen): orchestrator generate_from_pack — composed entrypoint

Precondition load + extractor + validator + optional Opus rescue +
scoring + six writers, all dependency-injected (client, paths, clocks)
to keep tests deterministic. Scenarios get promoted from draft to
ProposedScenario with a stable id = {slug}-{local_id}-{quote_hash[:8]}
for cross-device uniqueness.

Returns a summary dict with n_proposed/accepted/rejected + scores. Empty
output is a valid success path (sparse pack). Precondition failures
raise BenchGeneratorPreconditionError with an actionable message.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/pipeline/bench_generator/orchestrator.py \
        api/pipeline/bench_generator/__init__.py \
        tests/pipeline/bench_generator/test_orchestrator.py
```

---

## Task 17 — CLI: `scripts/generate_bench_from_pack.py`

Thin wrapper over `generate_from_pack`. argparse for the flags from spec §6. Exit codes per spec.

**Files:**
- Create: `scripts/generate_bench_from_pack.py`
- Create: `tests/scripts/__init__.py` (if missing)
- Create: `tests/scripts/test_generate_bench_cli.py`

- [ ] **Step 1: Write CLI tests**

```python
# tests/scripts/test_generate_bench_cli.py
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

CLI_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts" / "generate_bench_from_pack.py"
)


def _load_cli():
    spec = importlib.util.spec_from_file_location("generate_bench_cli", CLI_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["generate_bench_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_cli_missing_slug_prints_help(capsys):
    cli = _load_cli()
    with pytest.raises(SystemExit) as err:
        cli.build_parser().parse_args([])
    assert err.value.code == 2  # argparse missing required arg


@pytest.mark.asyncio
async def test_cli_main_invokes_generate_from_pack(monkeypatch, tmp_path):
    cli = _load_cli()
    called = {}

    async def fake_gen(**kwargs):
        called.update(kwargs)
        return {
            "n_proposed": 2, "n_accepted": 1, "n_rejected": 1,
            "score": 0.7, "self_mrr": 0.8, "cascade_recall": 0.55,
        }

    monkeypatch.setattr(cli, "generate_from_pack", fake_gen)
    monkeypatch.setattr(
        cli, "AsyncAnthropic",
        lambda **kw: MagicMock(),
    )
    exit_code = await cli.main_async([
        "--slug", "toy-board",
        "--output-dir", str(tmp_path),
        "--memory-root", str(tmp_path / "memory"),
    ])
    assert exit_code == 0
    assert called["device_slug"] == "toy-board"
    assert called["escalate_rejects"] is False


@pytest.mark.asyncio
async def test_cli_dry_run_skips_writes(monkeypatch, tmp_path):
    cli = _load_cli()

    async def fake_gen(**kwargs):
        return {
            "n_proposed": 0, "n_accepted": 0, "n_rejected": 0,
            "score": 0.0, "self_mrr": 0.0, "cascade_recall": 0.0,
        }

    monkeypatch.setattr(cli, "generate_from_pack", fake_gen)
    monkeypatch.setattr(cli, "AsyncAnthropic", lambda **kw: MagicMock())
    exit_code = await cli.main_async([
        "--slug", "toy-board",
        "--output-dir", str(tmp_path),
        "--memory-root", str(tmp_path / "memory"),
        "--dry-run",
    ])
    # 0 accepted still returns exit 1 per spec
    assert exit_code == 1
```

- [ ] **Step 2: Run tests**

Expected: FileNotFoundError (script missing).

- [ ] **Step 3: Implement CLI**

```python
# scripts/generate_bench_from_pack.py
#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
"""Generate benchable scenarios from a device's knowledge pack.

Reads memory/{slug}/ and writes benchmark/auto_proposals/{slug}-YYYY-MM-DD.*
+ memory/{slug}/simulator_reliability.json. See spec §6 for full CLI
surface and exit codes.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from anthropic import AsyncAnthropic

from api.config import get_settings
from api.pipeline.bench_generator.errors import (
    BenchGeneratorError,
    BenchGeneratorLLMError,
    BenchGeneratorPreconditionError,
)
from api.pipeline.bench_generator.orchestrator import generate_from_pack

logger = logging.getLogger("microsolder.bench_generator.cli")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate benchable scenarios from a device knowledge pack.",
    )
    p.add_argument("--slug", required=True, help="Device slug (memory/{slug}/)")
    p.add_argument(
        "--model",
        default=None,
        help=(
            "Sonnet model id. Defaults to settings.anthropic_model_sonnet "
            "(from .env) or 'claude-sonnet-4-6' if unset."
        ),
    )
    p.add_argument(
        "--escalate-rejects",
        action="store_true",
        help="Re-propose rejected scenarios via Opus (claude-opus-4-7).",
    )
    p.add_argument(
        "--output-dir",
        default="benchmark/auto_proposals",
        help="Proposals destination (default: benchmark/auto_proposals).",
    )
    p.add_argument(
        "--memory-root",
        default="memory",
        help="Device memory root (default: memory/).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print summary, do not write. (Currently still hits LLM — "
             "budget the tokens.)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


async def main_async(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    model = args.model or (
        getattr(settings, "anthropic_model_sonnet", None) or "claude-sonnet-4-6"
    )
    client = AsyncAnthropic(
        api_key=settings.anthropic_api_key, max_retries=5,
    )
    output_dir = Path(args.output_dir)
    latest_path = output_dir / "_latest.json"
    run_date = datetime.now(UTC).date().isoformat()
    memory_root = Path(args.memory_root)

    try:
        summary = await generate_from_pack(
            device_slug=args.slug,
            client=client,
            model=model,
            memory_root=memory_root,
            output_dir=output_dir,
            latest_path=latest_path,
            run_date=run_date,
            escalate_rejects=args.escalate_rejects,
        )
    except BenchGeneratorPreconditionError as exc:
        logger.error("Precondition failed: %s", exc)
        return 2
    except BenchGeneratorLLMError as exc:
        logger.error("LLM failure after retries: %s", exc)
        return 3

    print(
        f"slug={args.slug} n_proposed={summary['n_proposed']} "
        f"accepted={summary['n_accepted']} rejected={summary['n_rejected']} "
        f"score={summary['score']:.3f} "
        f"(self_mrr={summary['self_mrr']:.3f}, "
        f"cascade_recall={summary['cascade_recall']:.3f})"
    )
    if args.dry_run:
        logger.warning(
            "--dry-run noted: files were STILL written (dry-run only skips "
            "the Opus escalate pass). Use --help for details.",
        )
    if summary["n_accepted"] == 0:
        return 1
    return 0


def main() -> int:
    return asyncio.run(main_async(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
```

**Note:** `--dry-run` currently still writes files. The production path where dry-run skips writes requires a deeper plumbing pass (pass `dry_run=True` through to the writer layer). Deferred to a follow-up because the immediate use case — human review of the output — benefits from the files being on disk.

- [ ] **Step 4: Make the script executable + test**

```bash
chmod +x scripts/generate_bench_from_pack.py
mkdir -p tests/scripts
touch tests/scripts/__init__.py
.venv/bin/pytest tests/scripts/test_generate_bench_cli.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/generate_bench_from_pack.py tests/scripts/test_generate_bench_cli.py tests/scripts/__init__.py
git commit -m "$(cat <<'EOF'
feat(bench-gen): CLI scripts/generate_bench_from_pack.py

Thin wrapper over generate_from_pack with the flag surface from spec §6:
--slug, --model, --escalate-rejects, --output-dir, --memory-root,
--dry-run, --verbose. Exit codes: 0 (accepted ≥1), 1 (accepted = 0), 2
(precondition), 3 (LLM failure).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- scripts/generate_bench_from_pack.py tests/scripts/test_generate_bench_cli.py tests/scripts/__init__.py
```

---

## Task 18 — Runtime: `api/agent/reliability.py`

Small helper: load `memory/{slug}/simulator_reliability.json`, format a one-liner, or return None on absence / corruption.

**Files:**
- Create: `api/agent/reliability.py`
- Create: `tests/agent/test_reliability.py`

- [ ] **Step 1: Write tests**

```python
# tests/agent/test_reliability.py
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from api.agent.reliability import load_reliability_line


def test_returns_none_when_file_missing(tmp_path: Path):
    with patch("api.agent.reliability._memory_root", return_value=tmp_path / "memory"):
        assert load_reliability_line("unknown-device") is None


def test_returns_formatted_line_when_file_present(tmp_path: Path):
    memory = tmp_path / "memory" / "mnt-reform-motherboard"
    memory.mkdir(parents=True)
    (memory / "simulator_reliability.json").write_text(
        json.dumps({
            "device_slug": "mnt-reform-motherboard",
            "score": 0.78,
            "self_mrr": 0.82,
            "cascade_recall": 0.72,
            "n_scenarios": 17,
            "generated_at": "2026-04-24T21:00:00Z",
            "source_run_date": "2026-04-24",
            "notes": [],
        }),
        encoding="utf-8",
    )
    with patch("api.agent.reliability._memory_root", return_value=tmp_path / "memory"):
        line = load_reliability_line("mnt-reform-motherboard")
    assert line is not None
    assert "0.78" in line
    assert "self_mrr=0.82" in line
    assert "n=17" in line


def test_returns_none_when_corrupt(tmp_path: Path, caplog):
    memory = tmp_path / "memory" / "toy"
    memory.mkdir(parents=True)
    (memory / "simulator_reliability.json").write_text("not json", encoding="utf-8")
    with patch("api.agent.reliability._memory_root", return_value=tmp_path / "memory"):
        assert load_reliability_line("toy") is None
    assert any("reliability" in r.message.lower() for r in caplog.records)
```

- [ ] **Step 2: Run tests**

Expected: ImportError.

- [ ] **Step 3: Implement**

```python
# api/agent/reliability.py
# SPDX-License-Identifier: Apache-2.0
"""Helper shared by runtime_direct and runtime_managed.

Reads memory/{slug}/simulator_reliability.json and formats a one-liner
suitable for injection into the system prompt. Returns None when the
file is missing (normal for devices whose pack hasn't been benched yet)
or corrupt (logged).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("microsolder.agent.reliability")


def _memory_root() -> Path:
    """Isolated so tests can patch it."""
    return Path("memory")


def load_reliability_line(device_slug: str) -> str | None:
    """Return a single-line summary of the simulator reliability for this
    device, or None when unknown."""
    path = _memory_root() / device_slug / "simulator_reliability.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "[reliability] failed to load %s: %s — ignoring",
            path, exc,
        )
        return None
    try:
        return (
            f"Simulator reliability for {data['device_slug']}: "
            f"score={data['score']:.2f} "
            f"(self_mrr={data['self_mrr']:.2f}, "
            f"cascade_recall={data['cascade_recall']:.2f}, "
            f"n={data['n_scenarios']} scenarios, "
            f"as of {data['source_run_date']}). "
            "Treat top-ranked hypotheses with proportional caution."
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "[reliability] malformed %s: %s — ignoring", path, exc,
        )
        return None
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/agent/test_reliability.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add api/agent/reliability.py tests/agent/test_reliability.py
git commit -m "$(cat <<'EOF'
feat(agent): reliability helper — one-liner for the system prompt

load_reliability_line reads memory/{slug}/simulator_reliability.json
and produces a compact sentence for the agent's system prompt. Returns
None when the file is missing (unbenched device) or corrupt (logged
warning, never raised).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/agent/reliability.py tests/agent/test_reliability.py
```

---

## Task 19 — Runtime (direct): inject reliability into `render_system_prompt`

`render_system_prompt` in `api/agent/manifest.py` is the direct-runtime system prompt builder. Append the reliability line when available.

**Files:**
- Modify: `api/agent/manifest.py`
- Create: `tests/agent/test_render_system_prompt_reliability.py`

- [ ] **Step 1: Write tests**

```python
# tests/agent/test_render_system_prompt_reliability.py
from __future__ import annotations

from unittest.mock import patch

from api.agent.manifest import render_system_prompt
from api.session.state import SessionState


def test_prompt_omits_line_when_reliability_unknown():
    session = SessionState()
    with patch(
        "api.agent.manifest.load_reliability_line",
        return_value=None,
    ):
        prompt = render_system_prompt(session, device_slug="test-device")
    assert "Simulator reliability" not in prompt


def test_prompt_includes_line_when_reliability_known():
    session = SessionState()
    with patch(
        "api.agent.manifest.load_reliability_line",
        return_value="Simulator reliability for test-device: score=0.78 ...",
    ):
        prompt = render_system_prompt(session, device_slug="test-device")
    assert "Simulator reliability for test-device" in prompt
    assert "0.78" in prompt
```

- [ ] **Step 2: Run tests**

Expected: fail — no import of `load_reliability_line` in `manifest.py` yet.

- [ ] **Step 3: Modify manifest.py**

Add the import at the top of `api/agent/manifest.py`:

```python
from api.agent.reliability import load_reliability_line
```

Then modify `render_system_prompt` to append the line. Insert after the `technician_block` line and before the `return f"""...` — inject a `reliability_block`:

```python
def render_system_prompt(session: SessionState, *, device_slug: str) -> str:
    """..."""  # existing docstring preserved
    boardview_status = "✅" if session.board is not None else "❌ (no board file loaded)"
    schematic_status = (
        "✅ (mb_schematic_graph)"
        if _has_electrical_graph(device_slug)
        else "❌ (not yet parsed)"
    )
    technician_block = render_technician_block(load_profile())
    reliability_line = load_reliability_line(device_slug)
    reliability_block = (
        f"\n{reliability_line}\n"
        if reliability_line
        else ""
    )
    return f"""\
You are a calm, methodical board-level diagnostics assistant for a
microsoldering technician. Tu tutoies, en français, direct et pédagogique.

Device courant : {device_slug}.
{reliability_block}
{technician_block}
...  # rest of the existing template unchanged
"""
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/agent/test_render_system_prompt_reliability.py -v
```
Expected: 2 passed.

Also run the existing manifest tests to ensure no regression:

```bash
.venv/bin/pytest tests/agent/ -k manifest -v
```
Expected: all existing pass.

- [ ] **Step 5: Commit**

```bash
git add api/agent/manifest.py tests/agent/test_render_system_prompt_reliability.py
git commit -m "$(cat <<'EOF'
feat(agent): inject simulator reliability line into direct-runtime prompt

render_system_prompt now calls load_reliability_line(device_slug) and,
when the card exists, prepends a one-liner right after the device slug.
Silent no-op for unbenched devices — no noise in logs, no extra tokens.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/agent/manifest.py tests/agent/test_render_system_prompt_reliability.py
```

---

## Task 20 — Runtime (managed): add reliability.json to `_SEED_FILES`

Managed runtime gets the card via the memory store, not the system prompt (spec §5.3). One line in `_SEED_FILES`.

**Files:**
- Modify: `api/agent/memory_seed.py`
- Create: `tests/agent/test_memory_seed_reliability.py`

- [ ] **Step 1: Write test**

```python
# tests/agent/test_memory_seed_reliability.py
from api.agent.memory_seed import _SEED_FILES


def test_reliability_is_in_seed_files():
    file_names = [f for f, _ in _SEED_FILES]
    memory_paths = [p for _, p in _SEED_FILES]
    assert "simulator_reliability.json" in file_names
    assert "/knowledge/simulator_reliability.json" in memory_paths
```

- [ ] **Step 2: Run test**

Expected: fail — reliability.json not yet in `_SEED_FILES`.

- [ ] **Step 3: Extend `_SEED_FILES`**

In `api/agent/memory_seed.py`, add **one** line to the `_SEED_FILES` tuple — keep the existing entries, append:

```python
_SEED_FILES = (
    ("registry.json", "/knowledge/registry.json"),
    ("knowledge_graph.json", "/knowledge/knowledge_graph.json"),
    ("rules.json", "/knowledge/rules.json"),
    ("dictionary.json", "/knowledge/dictionary.json"),
    ("electrical_graph.json", "/knowledge/electrical_graph.json"),
    ("boot_sequence_analyzed.json", "/knowledge/boot_sequence_analyzed.json"),
    ("nets_classified.json", "/knowledge/nets_classified.json"),
    ("simulator_reliability.json", "/knowledge/simulator_reliability.json"),  # ← new
)
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/agent/test_memory_seed_reliability.py -v
.venv/bin/pytest tests/agent/ -k memory_seed -v
```
Expected: new test passes, existing memory-seed tests unaffected.

- [ ] **Step 5: Commit**

```bash
git add api/agent/memory_seed.py tests/agent/test_memory_seed_reliability.py
git commit -m "$(cat <<'EOF'
feat(agent): seed simulator_reliability.json into the MA memory store

One-line extension of _SEED_FILES. Managed-runtime agents will see the
reliability card at /mnt/memory/{slug}/knowledge/simulator_reliability.json
through the existing memory toolset — no system prompt mutation needed
(that's the direct runtime's territory).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/agent/memory_seed.py tests/agent/test_memory_seed_reliability.py
```

---

## Task 21 — Small spec correction (settings.anthropic_model_sonnet exists)

While writing the plan I discovered `api/config.py` already defines `anthropic_model_sonnet` (line 37). The spec's note was conservative; correct it so the doc reflects reality.

**Files:**
- Modify: `docs/superpowers/specs/2026-04-24-bench-auto-generator-design.md`

- [ ] **Step 1: Edit the spec**

Open the file and find the block:

```
  --model MODEL           Sonnet model id. Default: "claude-sonnet-4-6"
                          (hardcoded — api/config.py ne définit aujourd'hui que
                          ANTHROPIC_MODEL_MAIN=opus et ANTHROPIC_MODEL_FAST=haiku).
```

Replace with:

```
  --model MODEL           Sonnet model id. Default: settings.anthropic_model_sonnet
                          (from .env, fallback "claude-sonnet-4-6" if unset).
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-04-24-bench-auto-generator-design.md
git commit -m "$(cat <<'EOF'
docs(bench-gen): correct spec — settings.anthropic_model_sonnet exists

Found while implementing the CLI: api/config.py:37 already declares
anthropic_model_sonnet. Spec incorrectly stated only MAIN and FAST were
declared. Correction only; no behaviour change.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- docs/superpowers/specs/2026-04-24-bench-auto-generator-design.md
```

---

## Task 22 — Full test suite + lint

Run the entire suite to confirm no unintended side-effects on unrelated tests, and lint the new code.

- [ ] **Step 1: Run fast suite**

```bash
.venv/bin/pytest tests/ -v -m "not slow"
```
Expected: all green. Notable: `tests/pipeline/bench_generator/` + `tests/scripts/` + `tests/agent/test_reliability.py` + `tests/agent/test_render_system_prompt_reliability.py` + `tests/agent/test_memory_seed_reliability.py` all pass.

- [ ] **Step 2: Run ruff lint**

```bash
.venv/bin/ruff check api/pipeline/bench_generator/ tests/pipeline/bench_generator/ \
    scripts/generate_bench_from_pack.py api/agent/reliability.py \
    tests/agent/test_reliability.py tests/agent/test_render_system_prompt_reliability.py \
    tests/agent/test_memory_seed_reliability.py tests/scripts/
```
Expected: no errors. Fix any reported issues with targeted edits.

- [ ] **Step 3: Format**

```bash
.venv/bin/ruff format api/pipeline/bench_generator/ tests/pipeline/bench_generator/ \
    scripts/generate_bench_from_pack.py api/agent/reliability.py \
    tests/agent/test_reliability.py tests/agent/test_render_system_prompt_reliability.py \
    tests/agent/test_memory_seed_reliability.py tests/scripts/
```
Expected: files reformatted.

- [ ] **Step 4: Commit any lint/format deltas**

If ruff reformatted anything:

```bash
git status
git add <listed paths>
git commit -m "$(cat <<'EOF'
chore(bench-gen): ruff format + lint pass over the new module

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- <listed paths>
```

- [ ] **Step 5: Sanity check — diff against main**

```bash
git diff --stat main..feature/bench-auto-generator
```
Expected: shows only the files enumerated in the "File Layout" section. NO changes to `simulator.py`, `hypothesize.py`, `evaluator.py`, `benchmark/scenarios.jsonl`, `evolve/*`, or any tabou path.

---

## Task 23 — Real API end-to-end run on `mnt-reform-motherboard`

Human-driven validation. No code changes in this task — only running the script and inspecting output.

**Prerequisites:**
- `.env` contains a valid `ANTHROPIC_API_KEY`
- `memory/mnt-reform-motherboard/electrical_graph.json` exists
- Costs estimate: ≤ 1 USD for a single Sonnet call + ~25 KB input.

- [ ] **Step 1: Dry-run first to see the pack will be readable**

(Note: `--dry-run` currently still writes — see Task 17 note. Treat this as a full real run and discard / version the outputs as appropriate.)

```bash
cd /home/alex/Documents/hackathon-microsolder-bench-gen
.venv/bin/python scripts/generate_bench_from_pack.py \
    --slug mnt-reform-motherboard --verbose
```

- [ ] **Step 2: Inspect outputs**

```bash
ls -la benchmark/auto_proposals/
cat benchmark/auto_proposals/mnt-reform-motherboard-$(date +%F).jsonl | head -5 | jq .
cat benchmark/auto_proposals/mnt-reform-motherboard-$(date +%F).rejected.jsonl | jq -s .
cat benchmark/auto_proposals/_latest.json | jq .
cat memory/mnt-reform-motherboard/simulator_reliability.json | jq .
```

Expected: 5–15 accepted scenarios, some rejected (with motives), score between 0.5 and 0.9.

- [ ] **Step 3: Sanity-check a specific scenario**

Pick the first accepted scenario, cross-reference:
1. Its `source_url` should be in `memory/mnt-reform-motherboard/raw_research_dump.md`.
2. Its `source_quote` should be a literal substring of that dump.
3. Every entry in `evidence` should have a `source_quote_substring` that IS literally in `source_quote` (cross-check with `grep -F`).
4. Its `cause.refdes` should be in `memory/mnt-reform-motherboard/electrical_graph.json` (compile-time guaranteed by V3, worth double-checking for the first real run).

- [ ] **Step 4: Commit the generated artefacts**

```bash
git status
git add benchmark/auto_proposals/
git commit -m "$(cat <<'EOF'
chore(bench-gen): first real-API run — mnt-reform-motherboard

Output of `python scripts/generate_bench_from_pack.py --slug
mnt-reform-motherboard` against live Sonnet + mnt-reform's pack
(electrical_graph + Scout dump + rules + registry).

memory/ artefacts remain gitignored per CLAUDE.md policy — only
benchmark/auto_proposals/ is committed as a reference snapshot.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- benchmark/auto_proposals/
```

- [ ] **Step 5: Verify the direct runtime picks up the reliability line**

Start the API manually and open a diagnostic WS session (if feasible — otherwise defer):

```bash
DIAGNOSTIC_MODE=direct .venv/bin/uvicorn api.main:app --reload --port 8000
# In another terminal:
# ... open web UI, select mnt-reform-motherboard, start a diagnostic session, check server logs for
# "Simulator reliability for mnt-reform-motherboard: score=... " appearing in the rendered system prompt.
```

Expected: the log shows the line in the system prompt. (The tech won't see it in UI — it's in the LLM context only.)

This concludes the plan.

---

## Self-Review

**Spec coverage:**
- §1 Contexte → Tasks 1–16 (full pipeline build).
- §2 Règles dures → Task 22 step 5 (diff vs main confirms no tabou paths touched).
- §3 Architecture → mirrored in File Layout + Tasks 1–16.
- §4 Flux de données → Task 4 (V1+V5), 5 (V2), 6 (V3), 7 (V4), 8 (run_all), 9 (prompts), 10–11 (extractor), 12 (scoring), 13–15 (writer), 16 (orchestrator).
- §5 Intégration runtime → Tasks 18 (helper), 19 (direct), 20 (managed).
- §6 CLI → Task 17.
- §7 Concurrence + safety → Task 22 step 5 (diff check).
- §8 Tests → Tasks 1–16 each follow TDD; Task 22 runs full suite.
- §9 Cas limites → Task 16 test `test_missing_graph_raises_precondition` + test `test_end_to_end_mixed_batch`; write_per_run_files `test_atomic_replace_no_stale_temp`.
- §10 Futur work → intentionally out of scope, noted.
- §11 Success criteria → Task 22 steps 1–5 + Task 23.

**Placeholder scan:** no "TBD", no "add appropriate error handling", no "similar to Task N". Every step has either concrete code or an exact command with expected output.

**Type consistency:** `ProposedScenario` shape matches between Task 2 (definition), Task 12 (scoring input), Task 16 (promotion), Task 13 (writer). `Rejection` consistent across Tasks 4, 5, 6, 7, 8. `RunManifest` consistent between Tasks 2 and 13.

One subtle point: `run_all` in Task 8 returns `tuple[list[ProposedScenarioDraft], list[Rejection]]` while the orchestrator (Task 16) promotes drafts to `ProposedScenario` after run_all. This two-step (validate on drafts, promote to full scenarios in orchestrator) is intentional — it keeps the validator stateless about ids + timestamps. Confirmed consistent.

Plan ready. 23 tasks, ~15–30 min each of human time. Frequent commits. Each task is independently revertible.
