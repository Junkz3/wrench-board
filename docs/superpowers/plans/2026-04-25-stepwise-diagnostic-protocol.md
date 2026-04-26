# Stepwise Diagnostic Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land an agent-emitted, board-anchored, typed diagnostic protocol surface — the agent proposes ordered steps tied to refdes, the tech submits typed results via UI or chat, agent adapts the plan live.

**Architecture:** A new `api/tools/protocol.py` module owns Pydantic schemas, JSON persistence under `memory/{slug}/repairs/{rid}/protocol(s)/`, state-machine transitions, and the entry points for 4 new agent tools (`bv_propose_protocol`, `bv_update_protocol`, `bv_record_step_result`, `bv_get_protocol`). Both runtimes (managed + direct) route to the same module. Frontend has a central `web/js/protocol.js` state module fed by new WS events; renders three coexisting modes (floating card on board, wizard panel, inline chat fallback). Reuses the existing `mb_record_measurement` / `mb_set_observation` plumbing — no new persistence for measurements.

**Tech Stack:** Python 3.11+ · Pydantic v2 · FastAPI · Anthropic Managed Agents 2026-04-01 · vanilla JS · Canvas 2D (existing `brd_viewer.js`).

**Spec:** `docs/superpowers/specs/2026-04-25-stepwise-diagnostic-protocol-design.md`

---

## File Structure

**Backend (commit 2):**
- Create `api/tools/protocol.py` — schemas, persistence, state machine, tool entry points (~400 lines, single file because all of it is one concern; if it grows past 600 lines split persistence into `_protocol_store.py`)
- Modify `api/agent/manifest.py` — register 4 tools in `MB_TOOLS` neighbour list (add a `PROTOCOL_TOOLS` constant), extend `render_system_prompt` with PROTOCOL section
- Modify `api/agent/runtime_managed.py` — dispatch the new tools in `_dispatch_tool`, handle `protocol_step_result` payload from WS in `_forward_ws_to_session`
- Modify `api/agent/runtime_direct.py` — same dispatch + WS handling
- Modify `scripts/bootstrap_managed_agent.py` — add the 4 tools to TOOLS, append PROTOCOL block to `SYSTEM_PROMPT`
- Modify `api/pipeline/__init__.py` — add `GET /pipeline/repairs/{rid}/protocol` endpoint
- Create `tests/tools/test_protocol.py` — unit tests (state machine, persistence, validation, dispatch routing)
- Create `tests/agent/test_protocol_e2e.py` — integration test (mocked agent → tools → WS events → step submission round-trip)

**Frontend (commit 3):**
- Create `web/js/protocol.js` — central state module + WS event consumer + DOM coordination
- Create `web/styles/protocol.css` — wizard, inline, floating card styles (no new tokens)
- Modify `web/index.html` — add wizard panel mount + link CSS
- Modify `web/js/main.js` — boot `protocol.js`
- Modify `web/js/llm.js` — relay `protocol_*` WS events to `protocol.js`, embed inline cards in chat stream
- Modify `web/brd_viewer.js` — render numbered step badges over components, expose `setProtocolBadges(steps)` on `window.Boardview`

---

## Phase 1 — Backend

### Task 1 : Pydantic schemas

**Files:**
- Create: `api/tools/protocol.py`
- Test: `tests/tools/test_protocol.py`

- [ ] **Step 1: Write the failing test for Step / Protocol schema validation**

```python
# tests/tools/test_protocol.py
"""Unit tests for the diagnostic protocol module."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.tools.protocol import (
    Step,
    StepInput,
    StepResult,
    Protocol,
    HistoryEntry,
    StepStatus,
    StepType,
)


def test_numeric_step_requires_unit():
    with pytest.raises(ValidationError, match="unit"):
        StepInput(
            type="numeric",
            target="R49",
            instruction="Probe VIN",
            rationale="check input rail",
            # unit missing → invalid
        )


def test_numeric_step_accepts_optional_pass_range():
    s = StepInput(
        type="numeric",
        target="R49",
        instruction="Probe VIN",
        rationale="check input rail",
        unit="V",
        nominal=24.0,
        pass_range=[9.0, 32.0],
    )
    assert s.pass_range == [9.0, 32.0]


def test_step_must_have_target_or_test_point_when_numeric():
    with pytest.raises(ValidationError, match="target.*test_point"):
        StepInput(
            type="numeric",
            instruction="Probe somewhere",
            rationale="?",
            unit="V",
            target=None,
            test_point=None,
        )


def test_boolean_step_no_unit_required():
    s = StepInput(
        type="boolean",
        target="D11",
        instruction="LED D11 allumée ?",
        rationale="confirms 3V3 rail healthy",
        expected=True,
    )
    assert s.type == "boolean"


def test_observation_step_minimal_fields():
    s = StepInput(
        type="observation",
        target=None,
        test_point=None,
        instruction="Inspecte la zone autour de C42 — joint sec ?",
        rationale="visual cue for cold solder",
    )
    assert s.target is None and s.test_point is None  # observation may have neither


def test_ack_step_minimal():
    s = StepInput(
        type="ack",
        target="U7",
        instruction="Reflow U7 à 350°C avec flux",
        rationale="reseat package",
    )
    assert s.type == "ack"
```

- [ ] **Step 2: Run tests — expect ImportError**

Run: `.venv/bin/pytest tests/tools/test_protocol.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'api.tools.protocol'`

- [ ] **Step 3: Implement schemas in api/tools/protocol.py**

```python
# api/tools/protocol.py
# SPDX-License-Identifier: Apache-2.0
"""Stepwise diagnostic protocol — schemas, persistence, state machine.

The agent emits a typed plan via `bv_propose_protocol`; the tech submits
results step by step (UI or chat); the agent observes outcomes via a
synthetic `user.message` and may insert / skip / reorder via
`bv_update_protocol`. Measurement values reuse the existing
`mb_record_measurement` / `mb_set_observation` plumbing.

Spec: docs/superpowers/specs/2026-04-25-stepwise-diagnostic-protocol-design.md
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger("wrench_board.tools.protocol")


StepType = Literal["numeric", "boolean", "observation", "ack"]
StepStatus = Literal["pending", "active", "done", "skipped", "failed"]
ProtocolStatus = Literal["active", "completed", "abandoned", "replaced"]
HistoryAction = Literal[
    "proposed",
    "step_completed",
    "step_skipped",
    "step_failed",
    "step_inserted",
    "step_replaced",
    "step_reordered",
    "replaced_protocol",
    "completed",
    "abandoned",
]


class StepInput(BaseModel):
    """Step shape as emitted by the agent (no id / status / result yet)."""

    type: StepType
    target: str | None = None
    test_point: str | None = None
    instruction: str = Field(..., min_length=4, max_length=400)
    rationale: str = Field(..., min_length=4, max_length=400)
    unit: str | None = None
    nominal: float | None = None
    pass_range: tuple[float, float] | None = None
    expected: bool | None = None  # boolean only

    @model_validator(mode="after")
    def _validate_type_specific(self) -> "StepInput":
        if self.type == "numeric":
            if not self.unit:
                raise ValueError("numeric step requires `unit`")
            if not self.target and not self.test_point:
                raise ValueError(
                    "numeric step requires either `target` (refdes) or `test_point`"
                )
            if self.pass_range is not None:
                lo, hi = self.pass_range
                if lo >= hi:
                    raise ValueError("pass_range must be (lo < hi)")
        if self.type == "boolean":
            if not self.target and not self.test_point:
                raise ValueError(
                    "boolean step requires either `target` (refdes) or `test_point`"
                )
        # observation + ack have no further constraint
        return self


class StepResult(BaseModel):
    """Result payload attached to a step after submission."""

    value: float | bool | str | None = None
    unit: str | None = None
    observation: str | None = None
    skip_reason: str | None = None
    outcome: Literal["pass", "fail", "skipped", "neutral"] = "neutral"
    submitted_by: Literal["agent", "tech"] = "agent"
    ts: str  # ISO-8601 UTC


class Step(StepInput):
    """Persisted step — adds id, status, result."""

    id: str
    status: StepStatus = "pending"
    result: StepResult | None = None


class HistoryEntry(BaseModel):
    action: HistoryAction
    ts: str
    step_id: str | None = None
    after: str | None = None
    reason: str | None = None
    outcome: str | None = None
    verdict: str | None = None
    step_count: int | None = None
    new_order: list[str] | None = None


class Protocol(BaseModel):
    protocol_id: str
    repair_id: str
    device_slug: str
    title: str
    rationale: str
    rule_inspirations: list[str] = Field(default_factory=list)
    current_step_id: str | None = None
    status: ProtocolStatus = "active"
    created_at: str
    completed_at: str | None = None
    steps: list[Step] = Field(default_factory=list)
    history: list[HistoryEntry] = Field(default_factory=list)
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `.venv/bin/pytest tests/tools/test_protocol.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit so far**

(Hold all backend commits for end of Task 8 — see §14 phasing in the spec; one cohesive backend commit.)

---

### Task 2 : Persistence layer

**Files:**
- Modify: `api/tools/protocol.py`
- Test: `tests/tools/test_protocol.py`

- [ ] **Step 1: Write the failing test for persistence roundtrip**

```python
# Append to tests/tools/test_protocol.py

def test_persist_and_load_roundtrip(tmp_path):
    from api.tools.protocol import (
        save_protocol,
        load_protocol,
        load_active_pointer,
        save_active_pointer,
    )

    proto = Protocol(
        protocol_id="p_abc",
        repair_id="r1",
        device_slug="demo",
        title="t",
        rationale="r",
        current_step_id="s_1",
        created_at="2026-04-25T10:00:00+00:00",
        steps=[
            Step(
                id="s_1",
                type="numeric",
                target="R49",
                instruction="probe",
                rationale="why",
                unit="V",
                nominal=24.0,
                status="active",
            )
        ],
        history=[HistoryEntry(action="proposed", step_count=1, ts="2026-04-25T10:00:00+00:00")],
    )
    save_protocol(tmp_path, proto)
    loaded = load_protocol(tmp_path, "demo", "r1", "p_abc")
    assert loaded == proto

    save_active_pointer(tmp_path, "demo", "r1", "p_abc")
    pointer = load_active_pointer(tmp_path, "demo", "r1")
    assert pointer["active_protocol_id"] == "p_abc"


def test_load_protocol_returns_none_when_missing(tmp_path):
    from api.tools.protocol import load_protocol
    assert load_protocol(tmp_path, "demo", "r1", "p_missing") is None


def test_load_active_pointer_empty_when_no_pointer(tmp_path):
    from api.tools.protocol import load_active_pointer
    out = load_active_pointer(tmp_path, "demo", "r1")
    assert out["active_protocol_id"] is None
    assert out["history"] == []
```

- [ ] **Step 2: Run — expect FAIL (functions undefined)**

Run: `.venv/bin/pytest tests/tools/test_protocol.py -v -k persist`

- [ ] **Step 3: Implement persistence in api/tools/protocol.py**

```python
# Append to api/tools/protocol.py

# --- Persistence -------------------------------------------------------------

POINTER_FILENAME = "protocol.json"
PROTOCOLS_SUBDIR = "protocols"


def _repair_dir(memory_root: Path, device_slug: str, repair_id: str) -> Path:
    return memory_root / device_slug / "repairs" / repair_id


def _pointer_path(memory_root: Path, device_slug: str, repair_id: str) -> Path:
    return _repair_dir(memory_root, device_slug, repair_id) / POINTER_FILENAME


def _protocol_path(
    memory_root: Path, device_slug: str, repair_id: str, protocol_id: str
) -> Path:
    return (
        _repair_dir(memory_root, device_slug, repair_id)
        / PROTOCOLS_SUBDIR
        / f"{protocol_id}.json"
    )


def save_protocol(memory_root: Path, proto: Protocol) -> None:
    """Atomically write the full protocol artifact to disk."""
    path = _protocol_path(memory_root, proto.device_slug, proto.repair_id, proto.protocol_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = proto.model_dump(mode="json")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_protocol(
    memory_root: Path, device_slug: str, repair_id: str, protocol_id: str
) -> Protocol | None:
    path = _protocol_path(memory_root, device_slug, repair_id, protocol_id)
    if not path.exists():
        return None
    try:
        return Protocol.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[protocol] failed to load %s: %s", path, exc)
        return None


def save_active_pointer(
    memory_root: Path, device_slug: str, repair_id: str, protocol_id: str | None,
    *, prior_status: ProtocolStatus | None = None,
) -> None:
    """Set the active pointer; appends an entry to its rolling history.

    `prior_status` is the status that the previously-active protocol takes
    (typically `replaced` or `abandoned`); a fresh repair has no prior.
    """
    path = _pointer_path(memory_root, device_slug, repair_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {"active_protocol_id": None, "history": []}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    now = datetime.now(UTC).isoformat()
    if prior_status and existing.get("active_protocol_id"):
        existing["history"].append({
            "protocol_id": existing["active_protocol_id"],
            "status": prior_status,
            "ts": now,
        })
    existing["active_protocol_id"] = protocol_id
    if protocol_id:
        existing["history"].append({"protocol_id": protocol_id, "status": "active", "ts": now})
    path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")


def load_active_pointer(
    memory_root: Path, device_slug: str, repair_id: str
) -> dict[str, Any]:
    path = _pointer_path(memory_root, device_slug, repair_id)
    if not path.exists():
        return {"active_protocol_id": None, "history": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"active_protocol_id": None, "history": []}


def load_active_protocol(
    memory_root: Path, device_slug: str, repair_id: str
) -> Protocol | None:
    pointer = load_active_pointer(memory_root, device_slug, repair_id)
    pid = pointer.get("active_protocol_id")
    if not pid:
        return None
    return load_protocol(memory_root, device_slug, repair_id, pid)
```

- [ ] **Step 4: Run — expect PASS**

Run: `.venv/bin/pytest tests/tools/test_protocol.py -v -k persist`

---

### Task 3 : ID generation + propose-protocol entry point

**Files:**
- Modify: `api/tools/protocol.py`
- Test: `tests/tools/test_protocol.py`

- [ ] **Step 1: Failing test — propose creates protocol on disk + pointer**

```python
# Append to tests/tools/test_protocol.py

def test_propose_protocol_persists_and_sets_pointer(tmp_path):
    from api.tools.protocol import propose_protocol

    inputs = [
        StepInput(
            type="numeric",
            target="R49",
            instruction="probe VIN",
            rationale="check input",
            unit="V",
            nominal=24.0,
            pass_range=(9.0, 32.0),
        ),
        StepInput(
            type="ack",
            target="F1",
            instruction="reflow F1",
            rationale="reseat fuse",
        ),
    ]
    out = propose_protocol(
        memory_root=tmp_path,
        device_slug="demo",
        repair_id="r1",
        title="VIN dead",
        rationale="symptom matches rule-vin-dead-001",
        rule_inspirations=["rule-vin-dead-001"],
        steps=inputs,
        valid_refdes={"R49", "F1"},  # board parts (or None to skip validation)
    )
    assert out["ok"] is True
    pid = out["protocol_id"]
    assert out["step_count"] == 2

    from api.tools.protocol import load_active_protocol
    loaded = load_active_protocol(tmp_path, "demo", "r1")
    assert loaded.protocol_id == pid
    assert loaded.steps[0].id == "s_1"
    assert loaded.steps[0].status == "active"
    assert loaded.steps[1].status == "pending"
    assert loaded.current_step_id == "s_1"
    assert loaded.history[0].action == "proposed"


def test_propose_protocol_rejects_unknown_refdes(tmp_path):
    from api.tools.protocol import propose_protocol

    out = propose_protocol(
        memory_root=tmp_path,
        device_slug="demo",
        repair_id="r1",
        title="t",
        rationale="r",
        steps=[
            StepInput(
                type="numeric",
                target="UNKNOWN_999",
                instruction="probe somewhere",
                rationale="?",
                unit="V",
            )
        ],
        valid_refdes={"R49", "F1"},  # UNKNOWN_999 not in board
    )
    assert out["ok"] is False
    assert out["reason"] == "unknown-refdes"
    assert "UNKNOWN_999" in out["unknown_targets"]


def test_propose_protocol_caps_step_count(tmp_path):
    from api.tools.protocol import propose_protocol, MAX_STEPS_PER_PROTOCOL

    too_many = [
        StepInput(
            type="ack",
            target=None,
            test_point="TP1",
            instruction=f"step {i}",
            rationale="bulk",
        )
        for i in range(MAX_STEPS_PER_PROTOCOL + 1)
    ]
    out = propose_protocol(
        memory_root=tmp_path,
        device_slug="demo",
        repair_id="r1",
        title="t",
        rationale="r",
        steps=too_many,
        valid_refdes=None,
    )
    assert out["ok"] is False
    assert out["reason"] == "step_count_cap"


def test_propose_protocol_replaces_active(tmp_path):
    from api.tools.protocol import propose_protocol, load_active_pointer

    s = StepInput(type="ack", target="U1", instruction="x", rationale="y")
    out1 = propose_protocol(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        title="first", rationale="r", steps=[s], valid_refdes={"U1"},
    )
    out2 = propose_protocol(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        title="second", rationale="r", steps=[s], valid_refdes={"U1"},
    )
    pointer = load_active_pointer(tmp_path, "demo", "r1")
    assert pointer["active_protocol_id"] == out2["protocol_id"]
    statuses = [h["status"] for h in pointer["history"]]
    assert "replaced" in statuses
```

- [ ] **Step 2: Run — expect FAIL**

Run: `.venv/bin/pytest tests/tools/test_protocol.py -v -k propose`

- [ ] **Step 3: Implement `propose_protocol`**

```python
# Append to api/tools/protocol.py

MAX_STEPS_PER_PROTOCOL = 12


def _new_protocol_id() -> str:
    return f"p_{secrets.token_hex(4)}"


def propose_protocol(
    *,
    memory_root: Path,
    device_slug: str,
    repair_id: str,
    title: str,
    rationale: str,
    steps: list[StepInput],
    rule_inspirations: list[str] | None = None,
    valid_refdes: set[str] | None,
) -> dict[str, Any]:
    """Create a protocol; archive any prior active one. Returns ok / reason dict.

    `valid_refdes` is the set of refdes known on the loaded board. Pass None
    when no board is loaded (skips refdes validation — the agent may still
    target test points or unbounded refdes; the frontend renders them
    text-only).
    """
    if len(steps) > MAX_STEPS_PER_PROTOCOL:
        return {"ok": False, "reason": "step_count_cap", "max": MAX_STEPS_PER_PROTOCOL}
    if not steps:
        return {"ok": False, "reason": "empty_protocol"}

    if valid_refdes is not None:
        unknown = [
            s.target for s in steps
            if s.target and s.target not in valid_refdes
        ]
        if unknown:
            return {
                "ok": False,
                "reason": "unknown-refdes",
                "unknown_targets": sorted(set(unknown)),
            }

    now = datetime.now(UTC).isoformat()

    # Mark prior active as replaced.
    pointer = load_active_pointer(memory_root, device_slug, repair_id)
    prior_id = pointer.get("active_protocol_id")
    if prior_id:
        prior = load_protocol(memory_root, device_slug, repair_id, prior_id)
        if prior is not None and prior.status == "active":
            prior.status = "replaced"
            prior.history.append(HistoryEntry(action="replaced_protocol", ts=now,
                                              reason="superseded by fresh propose"))
            save_protocol(memory_root, prior)

    pid = _new_protocol_id()
    materialised: list[Step] = []
    for idx, s_in in enumerate(steps, start=1):
        step = Step(
            id=f"s_{idx}",
            status="active" if idx == 1 else "pending",
            **s_in.model_dump(),
        )
        materialised.append(step)

    proto = Protocol(
        protocol_id=pid,
        repair_id=repair_id,
        device_slug=device_slug,
        title=title.strip(),
        rationale=rationale.strip(),
        rule_inspirations=rule_inspirations or [],
        current_step_id=materialised[0].id,
        status="active",
        created_at=now,
        steps=materialised,
        history=[HistoryEntry(action="proposed", step_count=len(materialised), ts=now)],
    )
    save_protocol(memory_root, proto)
    save_active_pointer(
        memory_root, device_slug, repair_id, pid,
        prior_status="replaced" if prior_id else None,
    )
    return {"ok": True, "protocol_id": pid, "step_count": len(materialised),
            "current_step_id": proto.current_step_id}
```

- [ ] **Step 4: Run — expect PASS**

Run: `.venv/bin/pytest tests/tools/test_protocol.py -v -k propose`

---

### Task 4 : Record-step-result entry point + measurement plumbing reuse

**Files:**
- Modify: `api/tools/protocol.py`
- Test: `tests/tools/test_protocol.py`

- [ ] **Step 1: Failing test — numeric submit advances + records measurement**

```python
# Append to tests/tools/test_protocol.py

def test_record_numeric_advances_step_and_persists_measurement(tmp_path, monkeypatch):
    from api.tools import protocol as P

    P.propose_protocol(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        title="t", rationale="r",
        steps=[
            StepInput(type="numeric", target="R49", instruction="probe VIN",
                      rationale="check", unit="V", nominal=24.0,
                      pass_range=(9.0, 32.0)),
            StepInput(type="ack", target="F1", instruction="reflow",
                      rationale="re-seat"),
        ],
        valid_refdes={"R49", "F1"},
    )

    rec_calls: list[dict] = []
    def _fake_rec(**kwargs):
        rec_calls.append(kwargs)
        return {"recorded": True, "timestamp": "2026-04-25T10:00:00Z"}
    monkeypatch.setattr(P, "_record_measurement", _fake_rec)

    out = P.record_step_result(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        step_id="s_1", value=24.5, unit="V", submitted_by="tech",
    )
    assert out["ok"] is True
    assert out["outcome"] == "pass"
    assert out["current_step_id"] == "s_2"
    assert len(rec_calls) == 1
    assert rec_calls[0]["target"] == "R49"
    assert rec_calls[0]["value"] == 24.5

    proto = P.load_active_protocol(tmp_path, "demo", "r1")
    assert proto.steps[0].status == "done"
    assert proto.steps[0].result.value == 24.5
    assert proto.steps[1].status == "active"


def test_record_numeric_out_of_range_fails(tmp_path, monkeypatch):
    from api.tools import protocol as P
    P.propose_protocol(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        title="t", rationale="r",
        steps=[
            StepInput(type="numeric", target="R49", instruction="probe",
                      rationale="r", unit="V", pass_range=(9.0, 32.0)),
        ],
        valid_refdes={"R49"},
    )
    monkeypatch.setattr(P, "_record_measurement",
                        lambda **k: {"recorded": True, "timestamp": "x"})
    out = P.record_step_result(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        step_id="s_1", value=0.5, unit="V", submitted_by="tech",
    )
    assert out["outcome"] == "fail"
    proto = P.load_active_protocol(tmp_path, "demo", "r1")
    assert proto.steps[0].status == "failed"


def test_record_skip_marks_skipped_no_measurement(tmp_path, monkeypatch):
    from api.tools import protocol as P
    P.propose_protocol(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        title="t", rationale="r",
        steps=[
            StepInput(type="numeric", target="R49", instruction="probe",
                      rationale="r", unit="V"),
        ],
        valid_refdes={"R49"},
    )
    rec_calls = []
    monkeypatch.setattr(P, "_record_measurement",
                        lambda **k: rec_calls.append(k) or {"recorded": True, "timestamp": "x"})
    out = P.record_step_result(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        step_id="s_1", value=None, skip_reason="pas de DMM",
        submitted_by="tech",
    )
    assert out["outcome"] == "skipped"
    assert rec_calls == []  # no measurement on skip
    proto = P.load_active_protocol(tmp_path, "demo", "r1")
    assert proto.steps[0].status == "skipped"
    assert proto.steps[0].result.skip_reason == "pas de DMM"


def test_record_step_result_not_active(tmp_path):
    from api.tools.protocol import propose_protocol, record_step_result
    propose_protocol(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        title="t", rationale="r",
        steps=[StepInput(type="ack", target="U1", instruction="x", rationale="y"),
               StepInput(type="ack", target="U2", instruction="x", rationale="y")],
        valid_refdes={"U1", "U2"},
    )
    out = record_step_result(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        step_id="s_2", submitted_by="tech",
    )
    assert out["ok"] is False
    assert out["reason"] == "step_not_active"
```

- [ ] **Step 2: Run — expect FAIL**

Run: `.venv/bin/pytest tests/tools/test_protocol.py -v -k record`

- [ ] **Step 3: Implement `record_step_result` + private measurement bridge**

```python
# Append to api/tools/protocol.py

def _measurement_target(step: Step) -> str | None:
    """Resolve which target string to feed to mb_record_measurement.

    Refdes wins; otherwise prefix the test_point with `tp:` so the
    measurement log can disambiguate (refdes-shaped vs free-form anchor).
    """
    if step.target:
        return step.target
    if step.test_point:
        return f"tp:{step.test_point}"
    return None


def _record_measurement(**kwargs) -> dict[str, Any]:
    """Indirection so tests can monkey-patch.

    Routes to the production `mb_record_measurement` at call time."""
    from api.tools.measurements import mb_record_measurement
    return mb_record_measurement(**kwargs)


def _set_observation(**kwargs) -> dict[str, Any]:
    from api.tools.measurements import mb_set_observation
    return mb_set_observation(**kwargs)


def _classify_numeric_outcome(
    value: float, pass_range: tuple[float, float] | None
) -> Literal["pass", "fail", "neutral"]:
    if pass_range is None:
        return "neutral"
    lo, hi = pass_range
    return "pass" if lo <= value <= hi else "fail"


def _classify_boolean_outcome(
    value: bool, expected: bool | None
) -> Literal["pass", "fail", "neutral"]:
    if expected is None:
        return "neutral"
    return "pass" if value == expected else "fail"


def _next_pending_step_id(steps: list[Step]) -> str | None:
    for s in steps:
        if s.status == "pending":
            return s.id
    return None


def _persist_step_result_and_advance(
    proto: Protocol,
    *,
    step: Step,
    new_status: StepStatus,
    result: StepResult,
    history_action: HistoryAction,
    outcome_for_history: str | None,
    skip_reason: str | None = None,
) -> str | None:
    """Mutate proto in place; return new current_step_id (may be None when done)."""
    step.status = new_status
    step.result = result
    proto.history.append(HistoryEntry(
        action=history_action,
        step_id=step.id,
        outcome=outcome_for_history,
        reason=skip_reason,
        ts=result.ts,
    ))
    next_id = _next_pending_step_id(proto.steps)
    proto.current_step_id = next_id
    if next_id is None:
        # Plan exhausted naturally — caller (or agent) may complete or replan.
        pass
    else:
        for s in proto.steps:
            if s.id == next_id:
                s.status = "active"
                break
    return next_id


def record_step_result(
    *,
    memory_root: Path,
    device_slug: str,
    repair_id: str,
    step_id: str,
    value: float | bool | str | None = None,
    unit: str | None = None,
    observation: str | None = None,
    skip_reason: str | None = None,
    submitted_by: Literal["agent", "tech"] = "agent",
) -> dict[str, Any]:
    proto = load_active_protocol(memory_root, device_slug, repair_id)
    if proto is None:
        return {"ok": False, "reason": "no_active_protocol"}
    step = next((s for s in proto.steps if s.id == step_id), None)
    if step is None:
        return {"ok": False, "reason": "unknown_step_id"}
    if step.status != "active":
        return {"ok": False, "reason": "step_not_active", "current_status": step.status}

    now = datetime.now(UTC).isoformat()

    # Skip path — no measurement, mark skipped.
    if skip_reason is not None:
        result = StepResult(
            value=None, skip_reason=skip_reason, outcome="skipped",
            submitted_by=submitted_by, ts=now,
        )
        next_id = _persist_step_result_and_advance(
            proto, step=step, new_status="skipped", result=result,
            history_action="step_skipped", outcome_for_history="skipped",
            skip_reason=skip_reason,
        )
        save_protocol(memory_root, proto)
        return {"ok": True, "outcome": "skipped", "current_step_id": next_id,
                "protocol_id": proto.protocol_id}

    # Type-specific routing to measurement plumbing.
    outcome: Literal["pass", "fail", "neutral"] = "neutral"
    if step.type == "numeric":
        if not isinstance(value, (int, float)):
            return {"ok": False, "reason": "value_must_be_numeric"}
        target = _measurement_target(step)
        if target is None:
            return {"ok": False, "reason": "no_target_for_numeric"}
        _record_measurement(
            device_slug=device_slug, repair_id=repair_id,
            memory_root=memory_root, target=target,
            value=float(value), unit=unit or step.unit or "V",
            nominal=step.nominal, note=observation,
            source=submitted_by,
        )
        outcome = _classify_numeric_outcome(float(value), step.pass_range)
        result = StepResult(value=float(value), unit=unit or step.unit,
                            observation=observation, outcome=outcome,
                            submitted_by=submitted_by, ts=now)
    elif step.type == "boolean":
        if not isinstance(value, bool):
            return {"ok": False, "reason": "value_must_be_boolean"}
        target = _measurement_target(step)
        if target is None:
            return {"ok": False, "reason": "no_target_for_boolean"}
        # Map to sim observation: True → alive, False → dead.
        _set_observation(
            device_slug=device_slug, repair_id=repair_id,
            memory_root=memory_root, target=target,
            mode="alive" if value else "dead",
        )
        outcome = _classify_boolean_outcome(bool(value), step.expected)
        result = StepResult(value=bool(value), observation=observation,
                            outcome=outcome, submitted_by=submitted_by, ts=now)
    elif step.type == "observation":
        if not isinstance(value, str) or not value.strip():
            return {"ok": False, "reason": "value_must_be_text"}
        result = StepResult(value=value.strip(), outcome="neutral",
                            submitted_by=submitted_by, ts=now)
    elif step.type == "ack":
        result = StepResult(value="done", outcome="neutral",
                            submitted_by=submitted_by, ts=now)
    else:
        return {"ok": False, "reason": "unknown_step_type"}

    new_status: StepStatus = "failed" if outcome == "fail" else "done"
    history_action: HistoryAction = "step_failed" if outcome == "fail" else "step_completed"
    next_id = _persist_step_result_and_advance(
        proto, step=step, new_status=new_status, result=result,
        history_action=history_action, outcome_for_history=outcome,
    )
    save_protocol(memory_root, proto)
    return {"ok": True, "outcome": outcome, "current_step_id": next_id,
            "protocol_id": proto.protocol_id}
```

- [ ] **Step 4: Run — expect PASS**

Run: `.venv/bin/pytest tests/tools/test_protocol.py -v -k record`

---

### Task 5 : Update-protocol entry point (insert / skip / reorder / complete / abandon)

**Files:**
- Modify: `api/tools/protocol.py`
- Test: `tests/tools/test_protocol.py`

- [ ] **Step 1: Failing tests — each transition**

```python
# Append to tests/tools/test_protocol.py

def _seed_three_step_protocol(tmp_path) -> str:
    from api.tools.protocol import propose_protocol
    out = propose_protocol(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        title="t", rationale="r",
        steps=[
            StepInput(type="ack", target=f"U{i}", instruction=f"x{i}",
                      rationale="y") for i in range(1, 4)
        ],
        valid_refdes={"U1", "U2", "U3"},
    )
    return out["protocol_id"]


def test_update_insert_after(tmp_path):
    from api.tools.protocol import update_protocol, load_active_protocol
    _seed_three_step_protocol(tmp_path)
    new_step = StepInput(type="ack", target="U1", instruction="extra",
                         rationale="cause forced it")
    out = update_protocol(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        action="insert", after="s_1", new_step=new_step,
        reason="surprise on s_1",
    )
    assert out["ok"] is True
    proto = load_active_protocol(tmp_path, "demo", "r1")
    ids = [s.id for s in proto.steps]
    assert ids[0] == "s_1"
    assert ids[1].startswith("s_")  # inserted got fresh id
    assert ids[1] != "s_2"          # not the renumbered one
    assert proto.history[-1].action == "step_inserted"


def test_update_skip_marks_step(tmp_path):
    from api.tools.protocol import update_protocol, load_active_protocol
    _seed_three_step_protocol(tmp_path)
    out = update_protocol(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        action="skip", step_id="s_1", reason="N/A on this rev",
    )
    assert out["ok"] is True
    proto = load_active_protocol(tmp_path, "demo", "r1")
    assert proto.steps[0].status == "skipped"
    assert proto.current_step_id == "s_2"
    assert proto.steps[1].status == "active"


def test_update_reorder_changes_order(tmp_path):
    from api.tools.protocol import update_protocol, load_active_protocol
    _seed_three_step_protocol(tmp_path)
    # current is s_1; reorder pending tail.
    out = update_protocol(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        action="reorder", new_order=["s_1", "s_3", "s_2"],
        reason="prioritize s_3",
    )
    assert out["ok"] is True
    proto = load_active_protocol(tmp_path, "demo", "r1")
    assert [s.id for s in proto.steps] == ["s_1", "s_3", "s_2"]


def test_update_reorder_must_keep_current_first(tmp_path):
    from api.tools.protocol import update_protocol
    _seed_three_step_protocol(tmp_path)
    out = update_protocol(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        action="reorder", new_order=["s_2", "s_1", "s_3"],
        reason="bad",
    )
    assert out["ok"] is False
    assert out["reason"] == "cannot_displace_active"


def test_update_complete_protocol(tmp_path):
    from api.tools.protocol import update_protocol, load_active_protocol
    _seed_three_step_protocol(tmp_path)
    out = update_protocol(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        action="complete_protocol", verdict="symptom resolved by reflow",
        reason="all checks passed",
    )
    assert out["ok"] is True
    proto = load_active_protocol(tmp_path, "demo", "r1")
    assert proto.status == "completed"
    assert proto.completed_at is not None
    assert proto.history[-1].action == "completed"


def test_update_abandon(tmp_path):
    from api.tools.protocol import update_protocol, load_active_pointer
    _seed_three_step_protocol(tmp_path)
    out = update_protocol(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        action="abandon_protocol", reason="tech declined",
    )
    assert out["ok"] is True
    pointer = load_active_pointer(tmp_path, "demo", "r1")
    assert pointer["active_protocol_id"] is None
```

- [ ] **Step 2: Run — expect FAIL**

Run: `.venv/bin/pytest tests/tools/test_protocol.py -v -k update`

- [ ] **Step 3: Implement `update_protocol`**

```python
# Append to api/tools/protocol.py

def _new_inserted_step_id(existing_ids: set[str]) -> str:
    """Generate a non-clashing step id for inserts. Format `ins_<hex>`."""
    while True:
        candidate = f"ins_{secrets.token_hex(2)}"
        if candidate not in existing_ids:
            return candidate


def update_protocol(
    *,
    memory_root: Path,
    device_slug: str,
    repair_id: str,
    action: Literal[
        "insert", "skip", "replace_step", "reorder",
        "complete_protocol", "abandon_protocol",
    ],
    reason: str,
    step_id: str | None = None,
    after: str | None = None,
    new_step: StepInput | None = None,
    new_order: list[str] | None = None,
    verdict: str | None = None,
) -> dict[str, Any]:
    proto = load_active_protocol(memory_root, device_slug, repair_id)
    if proto is None:
        return {"ok": False, "reason": "no_active_protocol"}
    if proto.status != "active":
        return {"ok": False, "reason": "protocol_not_active",
                "current_status": proto.status}
    now = datetime.now(UTC).isoformat()
    existing_ids = {s.id for s in proto.steps}

    if action == "insert":
        if new_step is None or after is None:
            return {"ok": False, "reason": "insert_needs_after_and_new_step"}
        if after not in existing_ids:
            return {"ok": False, "reason": "unknown_after_step_id"}
        anchor = next(s for s in proto.steps if s.id == after)
        if anchor.status not in ("pending", "active"):
            return {"ok": False, "reason": "cannot_insert_after_completed_step"}
        new_id = _new_inserted_step_id(existing_ids)
        ins = Step(id=new_id, status="pending", **new_step.model_dump())
        idx = proto.steps.index(anchor)
        proto.steps.insert(idx + 1, ins)
        proto.history.append(HistoryEntry(
            action="step_inserted", step_id=new_id, after=after,
            reason=reason, ts=now,
        ))

    elif action == "skip":
        if step_id is None or step_id not in existing_ids:
            return {"ok": False, "reason": "unknown_step_id"}
        step = next(s for s in proto.steps if s.id == step_id)
        if step.status not in ("pending", "active"):
            return {"ok": False, "reason": "step_not_skippable",
                    "current_status": step.status}
        result = StepResult(value=None, skip_reason=reason, outcome="skipped",
                            submitted_by="agent", ts=now)
        next_id = _persist_step_result_and_advance(
            proto, step=step, new_status="skipped", result=result,
            history_action="step_skipped", outcome_for_history="skipped",
            skip_reason=reason,
        )

    elif action == "replace_step":
        if step_id is None or step_id not in existing_ids or new_step is None:
            return {"ok": False, "reason": "replace_needs_step_id_and_new_step"}
        step = next(s for s in proto.steps if s.id == step_id)
        if step.status != "pending":
            return {"ok": False, "reason": "can_only_replace_pending_step"}
        idx = proto.steps.index(step)
        replacement = Step(
            id=_new_inserted_step_id(existing_ids),
            status="pending",
            **new_step.model_dump(),
        )
        proto.steps[idx] = replacement
        proto.history.append(HistoryEntry(
            action="step_replaced", step_id=replacement.id, reason=reason, ts=now,
        ))

    elif action == "reorder":
        if not new_order or set(new_order) != existing_ids:
            return {"ok": False, "reason": "new_order_must_be_full_id_set"}
        if proto.current_step_id is not None and new_order[0] != proto.current_step_id:
            # We allow reorder of pending tail only — current step stays first.
            return {"ok": False, "reason": "cannot_displace_active"}
        index = {s.id: s for s in proto.steps}
        proto.steps = [index[i] for i in new_order]
        proto.history.append(HistoryEntry(
            action="step_reordered", new_order=list(new_order),
            reason=reason, ts=now,
        ))

    elif action == "complete_protocol":
        if not verdict:
            return {"ok": False, "reason": "complete_needs_verdict"}
        proto.status = "completed"
        proto.completed_at = now
        proto.current_step_id = None
        proto.history.append(HistoryEntry(
            action="completed", verdict=verdict, reason=reason, ts=now,
        ))
        save_protocol(memory_root, proto)
        save_active_pointer(
            memory_root, device_slug, repair_id, None,
            prior_status="completed",
        )
        return {"ok": True, "current_step_id": None, "protocol_id": proto.protocol_id,
                "status": "completed"}

    elif action == "abandon_protocol":
        proto.status = "abandoned"
        proto.completed_at = now
        proto.current_step_id = None
        proto.history.append(HistoryEntry(action="abandoned", reason=reason, ts=now))
        save_protocol(memory_root, proto)
        save_active_pointer(
            memory_root, device_slug, repair_id, None,
            prior_status="abandoned",
        )
        return {"ok": True, "current_step_id": None, "protocol_id": proto.protocol_id,
                "status": "abandoned"}

    else:
        return {"ok": False, "reason": "unknown_action"}

    save_protocol(memory_root, proto)
    return {"ok": True, "current_step_id": proto.current_step_id,
            "protocol_id": proto.protocol_id}
```

- [ ] **Step 4: Run — expect PASS**

Run: `.venv/bin/pytest tests/tools/test_protocol.py -v -k update`

---

### Task 6 : Manifest registration + system prompt section

**Files:**
- Modify: `api/agent/manifest.py`
- Test: `tests/agent/test_manifest_dynamic.py` (existing — verify our tools surface)

- [ ] **Step 1: Failing test — manifest exposes all 4 tools**

```python
# Append to tests/agent/test_manifest_dynamic.py (or a new test file)

def test_protocol_tools_in_manifest():
    from api.agent.manifest import PROTOCOL_TOOLS
    names = {t["name"] for t in PROTOCOL_TOOLS}
    assert names == {
        "bv_propose_protocol",
        "bv_update_protocol",
        "bv_record_step_result",
        "bv_get_protocol",
    }
    for t in PROTOCOL_TOOLS:
        assert len(t["description"]) <= 1024  # MA cap
        assert "input_schema" in t


def test_render_system_prompt_includes_protocol_section():
    from api.agent.manifest import render_system_prompt
    from api.session.state import SessionState
    out = render_system_prompt(SessionState(), device_slug="demo")
    assert "PROTOCOLE" in out or "protocol" in out.lower()
    assert "bv_propose_protocol" in out
```

- [ ] **Step 2: Run — expect FAIL (PROTOCOL_TOOLS undefined)**

Run: `.venv/bin/pytest tests/agent/ -v -k protocol`

- [ ] **Step 3: Add `PROTOCOL_TOOLS` to manifest.py**

Locate the existing `MB_TOOLS` / `BV_TOOLS` / `PROFILE_TOOLS` constants. Add after them:

```python
# In api/agent/manifest.py

PROTOCOL_TOOLS = [
    {
        "name": "bv_propose_protocol",
        "description": (
            "Émettre un protocole de diagnostic ordonné et typé que l'UI "
            "rend visuellement (cartes flottantes sur la board + wizard "
            "latéral, ou cartes inline si pas de board). Chaque step a un "
            "type (numeric/boolean/observation/ack), un target refdes, une "
            "instruction et un rationale. Appelle ce tool seulement après "
            "avoir matché une règle (confidence ≥ 0.6) ou identifié ≥ 2 "
            "likely_causes. UNE protocol active à la fois — réémettre en "
            "remplace la précédente. Cap : 12 steps."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "rationale": {"type": "string"},
                "rule_inspirations": {
                    "type": "array", "items": {"type": "string"},
                },
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["numeric", "boolean", "observation", "ack"],
                            },
                            "target": {"type": ["string", "null"]},
                            "test_point": {"type": ["string", "null"]},
                            "instruction": {"type": "string"},
                            "rationale": {"type": "string"},
                            "unit": {"type": ["string", "null"]},
                            "nominal": {"type": ["number", "null"]},
                            "pass_range": {
                                "type": ["array", "null"],
                                "items": {"type": "number"},
                                "minItems": 2, "maxItems": 2,
                            },
                            "expected": {"type": ["boolean", "null"]},
                        },
                        "required": ["type", "instruction", "rationale"],
                    },
                },
            },
            "required": ["title", "rationale", "steps"],
        },
    },
    {
        "name": "bv_update_protocol",
        "description": (
            "Modifier la protocol active : insert (nouveau step après un "
            "anchor), skip (le tech n'a pas l'outil ou tu décides de "
            "passer), replace_step (un step pending qui n'a plus de sens), "
            "reorder (les steps pending — l'active reste en tête), "
            "complete_protocol (tout est fait, donne un verdict en 1 "
            "phrase), abandon_protocol (le tech décline). reason est "
            "obligatoire et sera loggé dans l'historique."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "insert", "skip", "replace_step", "reorder",
                        "complete_protocol", "abandon_protocol",
                    ],
                },
                "reason": {"type": "string"},
                "step_id": {"type": ["string", "null"]},
                "after": {"type": ["string", "null"]},
                "new_step": {"type": ["object", "null"]},
                "new_order": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                },
                "verdict": {"type": ["string", "null"]},
            },
            "required": ["action", "reason"],
        },
    },
    {
        "name": "bv_record_step_result",
        "description": (
            "Persister le résultat d'un step toi-même (utile quand le tech "
            "donne la valeur en chat plutôt que via l'UI : 'VBUS = 4.8V'). "
            "Pour numeric, value est un nombre + unit. Pour boolean, "
            "value est true/false. Pour observation, value est du texte. "
            "Pour ack, value=null. skip_reason renseigné = step marqué "
            "skipped sans mesure. Le state machine avance ensuite au step "
            "suivant pending automatiquement."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "step_id": {"type": "string"},
                "value": {},
                "unit": {"type": ["string", "null"]},
                "observation": {"type": ["string", "null"]},
                "skip_reason": {"type": ["string", "null"]},
            },
            "required": ["step_id"],
        },
    },
    {
        "name": "bv_get_protocol",
        "description": (
            "Lire la protocol active complète (steps, statuses, results, "
            "history). À utiliser quand tu reprends une session ou que tu "
            "soupçonnes un drift d'état après une déconnexion. Retourne "
            "{active: false} si aucune protocol active."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]
```

- [ ] **Step 4: Update `render_system_prompt` to include the PROTOCOL section**

In `render_system_prompt`, locate the section that lists the existing tools. Add a new block after `mb_expand_knowledge` documentation:

```python
# Inside render_system_prompt, after the existing capability list, append:

protocol_block = """

PROTOCOLE — afficher un diagnostic stepwise visuellement.

Tu as 4 tools dédiés à un protocole de diagnostic guidé que l'UI rend
sur la board (badges numérotés sur les composants + carte flottante +
wizard latéral) :

  - bv_propose_protocol(title, rationale, steps) — émettre un plan typé
    de N steps (N ≤ 12). Appelle-le SEULEMENT après avoir matché une
    règle (confidence ≥ 0.6) OU identifié ≥ 2 likely_causes via
    mb_hypothesize. Pas au premier tour, sauf symptôme évident.
  - bv_update_protocol(action, reason, …) — insert / skip / replace_step
    / reorder / complete_protocol / abandon_protocol. Utilise quand un
    résultat te force à revoir le plan. reason est OBLIGATOIRE et
    devient visible dans l'historique du tech.
  - bv_record_step_result(step_id, value, unit?, observation?, skip_reason?)
    — quand le tech donne le résultat en CHAT au lieu de l'UI ("VBUS =
    4.8V", "non, D11 éteint"), c'est TOI qui appelles ce tool. Le state
    machine avance et émet l'event vers le frontend.
  - bv_get_protocol() — read-only, pour récupérer l'état complet sur
    resume / drift suspecté.

Quand le tech submit un résultat via l'UI, tu reçois un message
[step_result] step=… target=… value=… outcome=pass|fail|skipped ·
plan: N steps, current=… au tour suivant. Si outcome=pass et plan se
poursuit, tu peux soit rester silencieux (laisser le tech avancer) soit
narrer une ligne ("VIN nominal, on enchaîne sur F1."). Si outcome=fail,
analyse et utilise bv_update_protocol pour insérer / skip / réordonner.

Si le tech dit "pas de protocole" / "on bavarde" / "no steps" ou
similaire, n'émets pas. Reste en mode chat libre comme avant.
"""

return base_prompt + protocol_block
```

(Note : the actual integration point depends on where `render_system_prompt` builds the string. Locate the final `return f"""..."""` block and inject the protocol section before the closing triple-quote, OR concatenate at return as shown.)

- [ ] **Step 5: Run — expect PASS**

Run: `.venv/bin/pytest tests/agent/ -v -k protocol`

---

### Task 7 : Bootstrap script — register tools + extend SYSTEM_PROMPT

**Files:**
- Modify: `scripts/bootstrap_managed_agent.py`

- [ ] **Step 1: Add PROTOCOL_TOOLS to TOOLS**

Find the line:
```python
TOOLS = _ma_filter(MB_TOOLS + BV_TOOLS + PROFILE_TOOLS) + [_AGENT_TOOLSET]
```

Replace with:
```python
from api.agent.manifest import BV_TOOLS, MB_TOOLS, PROFILE_TOOLS, PROTOCOL_TOOLS

# ...

TOOLS = _ma_filter(MB_TOOLS + BV_TOOLS + PROFILE_TOOLS + PROTOCOL_TOOLS) + [_AGENT_TOOLSET]
```

- [ ] **Step 2: Append PROTOCOL block to `SYSTEM_PROMPT`**

Find the end of the existing `SYSTEM_PROMPT = """..."""` triple-quoted string. Just before the closing `"""`, append the same protocol_block content from Task 6 step 4 (in French, same wording for both runtimes).

- [ ] **Step 3: Verify with a syntax check**

Run: `.venv/bin/python -c "from scripts.bootstrap_managed_agent import SYSTEM_PROMPT, TOOLS; print(len(SYSTEM_PROMPT), len(TOOLS))"`
Expected: prints two integers, no error.

(The actual `--refresh-tools` invocation is held until commit lands; per the spec §14 this is a post-merge ops step.)

---

### Task 8 : Wire dispatch into both runtimes

**Files:**
- Modify: `api/agent/runtime_managed.py`
- Modify: `api/agent/runtime_direct.py`
- Test: `tests/agent/test_protocol_e2e.py` (new)

- [ ] **Step 1: Failing integration test**

```python
# tests/agent/test_protocol_e2e.py
"""Integration: tools dispatch + WS event roundtrip."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.agent.runtime_managed import _dispatch_tool
from api.session.state import SessionState


@pytest.mark.asyncio
async def test_dispatch_propose_protocol(tmp_path):
    session = SessionState()
    # No board = valid_refdes is None = no refdes validation
    out = await _dispatch_tool(
        name="bv_propose_protocol",
        payload={
            "title": "test",
            "rationale": "test",
            "steps": [
                {"type": "ack", "target": None, "test_point": "TP1",
                 "instruction": "do x", "rationale": "y"},
            ],
        },
        device_slug="demo", memory_root=tmp_path, client=None,
        session=session, repair_id="r1",
    )
    assert out["ok"] is True
    pid = out["protocol_id"]

    out2 = await _dispatch_tool(
        name="bv_get_protocol", payload={},
        device_slug="demo", memory_root=tmp_path, client=None,
        session=session, repair_id="r1",
    )
    assert out2["protocol_id"] == pid
    assert out2["current_step_id"] == "s_1"


@pytest.mark.asyncio
async def test_dispatch_record_step_result(tmp_path, monkeypatch):
    from api.tools import protocol as P
    monkeypatch.setattr(P, "_record_measurement",
                        lambda **k: {"recorded": True, "timestamp": "x"})

    session = SessionState()
    await _dispatch_tool(
        name="bv_propose_protocol",
        payload={
            "title": "t", "rationale": "r",
            "steps": [{"type": "numeric", "target": "R49", "instruction": "p",
                       "rationale": "y", "unit": "V", "pass_range": [9.0, 32.0]}],
        },
        device_slug="demo", memory_root=tmp_path, client=None,
        session=session, repair_id="r1",
    )
    # (For test simplicity SessionState.board is None so refdes validation skipped.)
    # Now agent records.
    out = await _dispatch_tool(
        name="bv_record_step_result",
        payload={"step_id": "s_1", "value": 24.5, "unit": "V"},
        device_slug="demo", memory_root=tmp_path, client=None,
        session=session, repair_id="r1",
    )
    assert out["outcome"] == "pass"
    assert out["current_step_id"] is None  # only 1 step
```

- [ ] **Step 2: Run — expect FAIL (dispatch doesn't know the new tools)**

Run: `.venv/bin/pytest tests/agent/test_protocol_e2e.py -v`

- [ ] **Step 3: Add dispatch in runtime_managed.py**

In `api/agent/runtime_managed.py::_dispatch_tool`, locate the chain of `if name == "mb_…"` branches. Add **before** the final `unknown mb_* tool` warning:

```python
    if name == "bv_propose_protocol":
        from api.tools.protocol import (
            propose_protocol as _propose,
            StepInput as _SI,
        )

        valid_refdes = (
            set(session.board.part_by_refdes().keys())
            if session.board is not None
            else None
        )
        try:
            step_inputs = [_SI.model_validate(s) for s in payload.get("steps", [])]
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": "invalid_step_input", "detail": str(exc)}
        result = _propose(
            memory_root=memory_root,
            device_slug=device_slug,
            repair_id=repair_id or "",
            title=payload.get("title", ""),
            rationale=payload.get("rationale", ""),
            steps=step_inputs,
            rule_inspirations=payload.get("rule_inspirations") or None,
            valid_refdes=valid_refdes,
        )
        if result.get("ok"):
            from api.tools.protocol import load_active_protocol
            proto = load_active_protocol(memory_root, device_slug, repair_id or "")
            if proto is not None:
                # Emit protocol_proposed WS event piggy-backed on the tool result.
                result["event"] = {
                    "type": "protocol_proposed",
                    "protocol_id": proto.protocol_id,
                    "title": proto.title,
                    "rationale": proto.rationale,
                    "steps": [s.model_dump() for s in proto.steps],
                    "current_step_id": proto.current_step_id,
                }
        return result

    if name == "bv_update_protocol":
        from api.tools.protocol import update_protocol as _update, StepInput as _SI

        new_step_payload = payload.get("new_step")
        new_step = None
        if new_step_payload is not None:
            try:
                new_step = _SI.model_validate(new_step_payload)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "reason": "invalid_new_step", "detail": str(exc)}
        result = _update(
            memory_root=memory_root,
            device_slug=device_slug,
            repair_id=repair_id or "",
            action=payload.get("action", ""),
            reason=payload.get("reason", ""),
            step_id=payload.get("step_id"),
            after=payload.get("after"),
            new_step=new_step,
            new_order=payload.get("new_order"),
            verdict=payload.get("verdict"),
        )
        if result.get("ok"):
            from api.tools.protocol import load_active_protocol
            proto = load_active_protocol(memory_root, device_slug, repair_id or "")
            history_tail = proto.history[-3:] if proto is not None else []
            result["event"] = {
                "type": "protocol_updated",
                "protocol_id": result.get("protocol_id"),
                "action": payload.get("action"),
                "current_step_id": result.get("current_step_id"),
                "steps": [s.model_dump() for s in (proto.steps if proto else [])],
                "history_tail": [h.model_dump() for h in history_tail],
            }
        return result

    if name == "bv_record_step_result":
        from api.tools.protocol import record_step_result as _record
        result = _record(
            memory_root=memory_root,
            device_slug=device_slug,
            repair_id=repair_id or "",
            step_id=payload.get("step_id", ""),
            value=payload.get("value"),
            unit=payload.get("unit"),
            observation=payload.get("observation"),
            skip_reason=payload.get("skip_reason"),
            submitted_by="agent",
        )
        if result.get("ok"):
            from api.tools.protocol import load_active_protocol
            proto = load_active_protocol(memory_root, device_slug, repair_id or "")
            history_tail = proto.history[-3:] if proto is not None else []
            result["event"] = {
                "type": "protocol_updated",
                "protocol_id": result.get("protocol_id"),
                "action": "step_completed",
                "current_step_id": result.get("current_step_id"),
                "steps": [s.model_dump() for s in (proto.steps if proto else [])],
                "history_tail": [h.model_dump() for h in history_tail],
            }
        return result

    if name == "bv_get_protocol":
        from api.tools.protocol import load_active_protocol
        proto = load_active_protocol(memory_root, device_slug, repair_id or "")
        if proto is None:
            return {"ok": True, "active": False}
        return {
            "ok": True, "active": True,
            "protocol_id": proto.protocol_id,
            "title": proto.title,
            "rationale": proto.rationale,
            "current_step_id": proto.current_step_id,
            "status": proto.status,
            "steps": [s.model_dump() for s in proto.steps],
            "history": [h.model_dump() for h in proto.history],
        }
```

- [ ] **Step 4: Mirror the same dispatch in runtime_direct.py**

In `api/agent/runtime_direct.py::_dispatch_mb_tool` (or similar — check the actual structure), add the same 4 branches. The body is identical except `repair_id` may flow differently (verify against direct's calling convention).

- [ ] **Step 5: Run — expect PASS**

Run: `.venv/bin/pytest tests/agent/test_protocol_e2e.py -v`

---

### Task 9 : WS event handler for client-submitted step result + GET endpoint

**Files:**
- Modify: `api/agent/runtime_managed.py` (and `runtime_direct.py`)
- Modify: `api/pipeline/__init__.py`

- [ ] **Step 1: Failing test for the GET endpoint**

```python
# Append to tests/agent/test_protocol_e2e.py

def test_get_protocol_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api import config as config_mod
    from api.main import app
    from api.tools.protocol import propose_protocol, StepInput

    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))

    propose_protocol(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        title="t", rationale="r",
        steps=[StepInput(type="ack", target="U1", instruction="x", rationale="y")],
        valid_refdes={"U1"},
    )

    client = TestClient(app)
    res = client.get("/pipeline/repairs/r1/protocol?device_slug=demo")
    assert res.status_code == 200
    body = res.json()
    assert body["active"] is True
    assert body["current_step_id"] == "s_1"

    res404 = client.get("/pipeline/repairs/missing/protocol?device_slug=demo")
    assert res404.json()["active"] is False
```

- [ ] **Step 2: Run — expect 404 (endpoint missing)**

Run: `.venv/bin/pytest tests/agent/test_protocol_e2e.py::test_get_protocol_endpoint -v`

- [ ] **Step 3: Add the endpoint in api/pipeline/__init__.py**

After the existing repair endpoints (around `GET /repairs/{repair_id}`):

```python
# In api/pipeline/__init__.py

@router.get("/repairs/{repair_id}/protocol")
async def get_repair_protocol(repair_id: str, device_slug: str) -> dict:
    """Return the active protocol artifact for this repair (or {active: false}).

    `device_slug` is required as a query param because repairs are scoped by
    device on disk. The frontend already knows the slug at the time it
    requests the protocol (from the WS session_ready event)."""
    from api.tools.protocol import load_active_protocol
    settings = get_settings()
    proto = load_active_protocol(Path(settings.memory_root), device_slug, repair_id)
    if proto is None:
        return {"active": False}
    return {
        "active": True,
        "protocol_id": proto.protocol_id,
        "title": proto.title,
        "rationale": proto.rationale,
        "current_step_id": proto.current_step_id,
        "status": proto.status,
        "steps": [s.model_dump(mode="json") for s in proto.steps],
        "history": [h.model_dump(mode="json") for h in proto.history],
    }
```

- [ ] **Step 4: Add WS handler for `protocol_step_result` from client**

In `api/agent/runtime_managed.py::_forward_ws_to_session`, locate the existing `payload.get("type")` branches (`interrupt`, `validation.start`). Add:

```python
        if payload.get("type") == "protocol_step_result":
            # Client submitted a step result via the protocol UI. Persist
            # via record_step_result with submitted_by="tech", emit the
            # protocol_updated event back, and synthesize a user.message
            # so the agent reacts on its next turn.
            from api.tools.protocol import (
                record_step_result as _record,
                load_active_protocol,
            )
            res = _record(
                memory_root=memory_root,
                device_slug=device_slug,
                repair_id=repair_id or "",
                step_id=payload.get("step_id", ""),
                value=payload.get("value"),
                unit=payload.get("unit"),
                observation=payload.get("observation"),
                skip_reason=payload.get("skip_reason"),
                submitted_by="tech",
            )
            if res.get("ok"):
                proto = load_active_protocol(memory_root, device_slug, repair_id or "")
                history_tail = proto.history[-3:] if proto is not None else []
                await ws.send_json({
                    "type": "protocol_updated",
                    "protocol_id": res.get("protocol_id"),
                    "action": "step_completed",
                    "current_step_id": res.get("current_step_id"),
                    "steps": [s.model_dump(mode="json") for s in (proto.steps if proto else [])],
                    "history_tail": [h.model_dump(mode="json") for h in history_tail],
                })
                # Synthesize a user.message so the agent observes the result.
                step_id = payload.get("step_id", "")
                target = ""
                value = payload.get("value")
                unit = payload.get("unit") or ""
                outcome = res.get("outcome", "neutral")
                current = res.get("current_step_id") or "completed"
                step_count = len(proto.steps) if proto else 0
                if proto is not None:
                    src_step = next((s for s in proto.steps if s.id == step_id), None)
                    if src_step is not None:
                        target = src_step.target or src_step.test_point or ""
                synthetic = (
                    f"[step_result] step={step_id} target={target} "
                    f"value={value}{unit} outcome={outcome} · "
                    f"plan: {step_count} steps, current={current}"
                )
                await client.beta.sessions.events.send(
                    session_id,
                    events=[{"type": "user.message",
                             "content": [{"type": "text", "text": synthetic}]}],
                )
            else:
                await ws.send_json({"type": "error", "code": "protocol_result_rejected",
                                     "text": res.get("reason", "unknown")})
            continue
```

- [ ] **Step 5: Mirror the same handler in runtime_direct.py**

In the direct runtime's WS receive loop (similar structure), add the same `protocol_step_result` branch. The synthetic-message injection is different — direct mode appends to `messages` list with role=user instead of going through MA events:

```python
            if isinstance(incoming, dict) and incoming.get("type") == "protocol_step_result":
                from api.tools.protocol import (
                    record_step_result as _record,
                    load_active_protocol,
                )
                res = _record(
                    memory_root=memory_root,
                    device_slug=device_slug,
                    repair_id=repair_id or "",
                    step_id=incoming.get("step_id", ""),
                    value=incoming.get("value"),
                    unit=incoming.get("unit"),
                    observation=incoming.get("observation"),
                    skip_reason=incoming.get("skip_reason"),
                    submitted_by="tech",
                )
                if res.get("ok"):
                    proto = load_active_protocol(memory_root, device_slug, repair_id or "")
                    history_tail = proto.history[-3:] if proto is not None else []
                    await ws.send_json({
                        "type": "protocol_updated",
                        "protocol_id": res.get("protocol_id"),
                        "action": "step_completed",
                        "current_step_id": res.get("current_step_id"),
                        "steps": [s.model_dump(mode="json") for s in (proto.steps if proto else [])],
                        "history_tail": [h.model_dump(mode="json") for h in history_tail],
                    })
                    # In direct mode, push a synthetic user message into messages.
                    target = ""
                    if proto is not None:
                        src_step = next((s for s in proto.steps if s.id == incoming.get("step_id")),
                                        None)
                        if src_step is not None:
                            target = src_step.target or src_step.test_point or ""
                    synthetic = (
                        f"[step_result] step={incoming.get('step_id', '')} target={target} "
                        f"value={incoming.get('value')}{incoming.get('unit') or ''} "
                        f"outcome={res.get('outcome')} · "
                        f"plan: {len(proto.steps) if proto else 0} steps, "
                        f"current={res.get('current_step_id') or 'completed'}"
                    )
                    user_msg = {"role": "user", "content": synthetic}
                    messages.append(user_msg)
                    if resolved_conv_id:
                        append_event(
                            device_slug=device_slug, repair_id=repair_id,
                            conv_id=resolved_conv_id, event=user_msg,
                            memory_root=memory_root,
                        )
                    # Fire an agent turn off the synthetic message.
                    await _run_agent_turn(
                        ws=ws, client=client, model=model,
                        system_prompt=system_prompt, tools=tools,
                        messages=messages, session=session,
                        device_slug=device_slug, repair_id=repair_id,
                        conv_id=resolved_conv_id, memory_root=memory_root,
                    )
                else:
                    await ws.send_json({"type": "error", "code": "protocol_result_rejected",
                                         "text": res.get("reason", "unknown")})
                continue
```

- [ ] **Step 6: On WS open, hydrate the active protocol if any**

In both runtimes, right after `session_ready` is sent, add:

```python
        from api.tools.protocol import load_active_protocol as _lap
        active = _lap(memory_root, device_slug, repair_id or "")
        if active is not None:
            await ws.send_json({
                "type": "protocol_proposed",
                "protocol_id": active.protocol_id,
                "title": active.title,
                "rationale": active.rationale,
                "steps": [s.model_dump(mode="json") for s in active.steps],
                "current_step_id": active.current_step_id,
                "replay": True,
            })
```

This makes the wizard panel hydrate before chat replay.

- [ ] **Step 7: Run all backend tests — expect PASS**

Run: `.venv/bin/pytest tests/tools/test_protocol.py tests/agent/test_protocol_e2e.py -v`
Expected: all green.

---

### Task 10 : Backend commit

- [ ] **Step 1: Run full backend test suite**

Run: `.venv/bin/pytest tests/agent/ tests/tools/test_protocol.py tests/pipeline/test_repairs.py -q`
Expected: all green.

- [ ] **Step 2: Lint touched files**

Run: `.venv/bin/ruff check api/tools/protocol.py api/agent/manifest.py api/agent/runtime_managed.py api/agent/runtime_direct.py api/pipeline/__init__.py scripts/bootstrap_managed_agent.py tests/tools/test_protocol.py tests/agent/test_protocol_e2e.py`
Expected: "All checks passed!" — fix any issues that surface specifically in the new code (pre-existing lint elsewhere is out of scope).

- [ ] **Step 3: Commit backend**

```bash
git add api/tools/protocol.py api/agent/manifest.py api/agent/runtime_managed.py api/agent/runtime_direct.py api/pipeline/__init__.py scripts/bootstrap_managed_agent.py tests/tools/test_protocol.py tests/agent/test_protocol_e2e.py
git commit -m "$(cat <<'EOF'
feat(protocol): backend for stepwise diagnostic protocol

Adds api/tools/protocol.py — schemas, persistence, state machine, and
the four bv_protocol tool entry points (propose / update / record /
get). Both runtimes route the new tools and handle the
protocol_step_result WS payload from the client (which calls
record_step_result with submitted_by="tech" and synthesizes a
user.message so the agent reacts on its next turn). On WS open the
runtime hydrates the active protocol via a protocol_proposed event so
the frontend can rebuild state.

Reuses existing mb_record_measurement / mb_set_observation plumbing —
numeric step submissions land in the measurement log (auto-classified
into sim observations when applicable), boolean steps set observations.
No new persistence layer for measurements.

Manifest registration adds PROTOCOL_TOOLS to MA bootstrap and direct
runtime; both system prompts gain a PROTOCOL block describing
trigger conditions (rule match ≥ 0.6 OR ≥ 2 likely_causes), opt-out,
and step_result reading. Run python scripts/bootstrap_managed_agent.py
--refresh-tools after merge to push the system prompt update to the
3 MA agents.

GET /pipeline/repairs/{rid}/protocol added for the wizard panel's
hydration path.

Spec: docs/superpowers/specs/2026-04-25-stepwise-diagnostic-protocol-design.md
EOF
)"
```

- [ ] **Step 4: After commit, run the bootstrap refresh** (manual ops step, not part of the commit but required before testing)

```bash
.venv/bin/python scripts/bootstrap_managed_agent.py --refresh-tools
```

Expect 3 archived agents + 3 freshly created agents with the new system prompt.

---

## Phase 2 — Frontend

### Task 11 : HTML mounting points + CSS shell

**Files:**
- Modify: `web/index.html`
- Create: `web/styles/protocol.css`

- [ ] **Step 1: Add wizard panel mount in index.html**

Find the right slide-in column markup (the chat panel container). Add **above** the chat container a section for the wizard:

```html
<!-- web/index.html — inside the right slide-in column, above the chat -->
<section id="protocolWizard" class="protocol-wizard hidden">
  <header class="protocol-wizard-head">
    <span class="protocol-wizard-title" id="protocolTitle">—</span>
    <button class="protocol-wizard-abandon" id="protocolAbandonBtn" type="button">abandonner</button>
  </header>
  <ul class="protocol-step-list" id="protocolStepList"></ul>
  <details class="protocol-history" id="protocolHistoryFold">
    <summary>historique</summary>
    <ol class="protocol-history-list" id="protocolHistoryList"></ol>
  </details>
</section>
```

Add the floating card mount inside the canvas container (will be position:absolute):

```html
<div id="protocolFloatingCard" class="protocol-float hidden" role="dialog" aria-label="step instruction"></div>
```

- [ ] **Step 2: Link the new CSS**

Inside `<head>`, add after the existing token / layout stylesheets:

```html
<link rel="stylesheet" href="/styles/protocol.css" />
```

- [ ] **Step 3: Create `web/styles/protocol.css`**

```css
/* web/styles/protocol.css
   Stepwise diagnostic protocol — wizard, floating card, inline cards.
   Tokens reused from tokens.css; no new colors per spec §6.5. */

.protocol-wizard {
  position: relative;
  display: flex;
  flex-direction: column;
  flex: 0 0 40%;
  min-height: 220px;
  max-height: 360px;
  background: linear-gradient(180deg, var(--panel) 0%, var(--panel-2) 100%);
  border-bottom: 1px solid var(--border);
  overflow: hidden;
}
.protocol-wizard.hidden { display: none; }

.protocol-wizard-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border-soft);
  font-size: 13px;
}
.protocol-wizard-title {
  color: var(--text);
  font-weight: 600;
}
.protocol-wizard-abandon {
  background: none;
  border: none;
  color: var(--text-3);
  font-size: 11px;
  cursor: pointer;
  padding: 0;
}
.protocol-wizard-abandon:hover { color: var(--amber); }

.protocol-step-list {
  list-style: none;
  margin: 0;
  padding: 6px 0;
  overflow-y: auto;
  flex: 1;
}
.protocol-step {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 6px 12px;
  border-bottom: 1px solid var(--border-soft);
  cursor: default;
}
.protocol-step.is-pending { opacity: 0.55; }
.protocol-step.is-active  { background: rgba(56, 189, 248, 0.06); }  /* very faint cyan */
.protocol-step.is-done    { opacity: 0.7; }
.protocol-step.is-skipped { opacity: 0.55; }
.protocol-step.is-failed  { /* keep full opacity, attention-required */ }

.protocol-step-badge {
  flex: 0 0 22px;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font: 600 11px 'JetBrains Mono', ui-monospace, monospace;
  border: 1.5px solid var(--cyan);
}
.protocol-step.is-pending .protocol-step-badge {
  background: var(--panel-2);
  color: var(--text-2);
}
.protocol-step.is-active  .protocol-step-badge {
  background: var(--cyan); color: var(--bg-deep);
}
.protocol-step.is-done    .protocol-step-badge {
  background: var(--cyan); color: var(--bg-deep);
}
.protocol-step.is-skipped .protocol-step-badge,
.protocol-step.is-failed  .protocol-step-badge {
  background: var(--amber); color: var(--bg-deep); border-color: var(--amber);
}

.protocol-step-body { flex: 1; min-width: 0; }
.protocol-step-target {
  font: 600 10.5px 'JetBrains Mono', monospace;
  color: var(--cyan);
  text-transform: uppercase;
  letter-spacing: .4px;
}
.protocol-step-instruction {
  font-size: 12.5px;
  color: var(--text);
  margin: 2px 0 0;
}
.protocol-step-rationale {
  font-size: 11px;
  color: var(--text-3);
  margin: 2px 0 0;
}
.protocol-step-result {
  font: 600 11px 'JetBrains Mono', monospace;
  color: var(--text-2);
  margin-top: 2px;
}
.protocol-step.is-active .protocol-step-form {
  display: flex;
  gap: 6px;
  margin-top: 6px;
  align-items: stretch;
}
.protocol-step-form input,
.protocol-step-form select,
.protocol-step-form textarea {
  background: var(--bg-2);
  color: var(--text);
  border: 1px solid var(--border);
  font: 12px Inter, sans-serif;
  padding: 4px 6px;
  border-radius: 3px;
}
.protocol-step-form button {
  background: var(--cyan);
  color: var(--bg-deep);
  border: none;
  padding: 4px 10px;
  border-radius: 3px;
  font: 600 12px Inter, sans-serif;
  cursor: pointer;
}
.protocol-step-form button.is-skip {
  background: transparent;
  color: var(--text-3);
  border: 1px solid var(--border);
}

.protocol-history {
  border-top: 1px solid var(--border-soft);
  padding: 4px 12px;
  font-size: 11px;
}
.protocol-history summary {
  cursor: pointer;
  color: var(--text-3);
  user-select: none;
}
.protocol-history-list {
  list-style: none;
  margin: 4px 0 0;
  padding: 0;
  font: 10.5px 'JetBrains Mono', monospace;
  color: var(--text-3);
}

/* Floating card — Mode A — anchored on the canvas above the active component. */
.protocol-float {
  position: absolute;
  z-index: 50;
  background: rgba(20, 24, 30, 0.92);
  backdrop-filter: blur(10px);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 10px;
  min-width: 220px;
  max-width: 320px;
  font-size: 12.5px;
  color: var(--text);
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.4);
}
.protocol-float.hidden { display: none; }
.protocol-float-head {
  display: flex; align-items: center; gap: 6px; margin-bottom: 4px;
}
.protocol-float-badge {
  width: 18px; height: 18px;
  border-radius: 50%;
  background: var(--cyan); color: var(--bg-deep);
  display: inline-flex; align-items: center; justify-content: center;
  font: 600 10.5px 'JetBrains Mono', monospace;
}
.protocol-float-target {
  font: 600 10.5px 'JetBrains Mono', monospace;
  color: var(--cyan); text-transform: uppercase; letter-spacing: .4px;
}
.protocol-float-instruction { margin: 0 0 6px; }
.protocol-float-form { display: flex; gap: 6px; }

/* Mode C — inline step bubble inside the chat panel */
.protocol-inline-card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-left: 3px solid var(--cyan);
  border-radius: 4px;
  padding: 8px 10px;
  margin: 6px 0;
  font-size: 12.5px;
}
.protocol-inline-card .protocol-step-form { margin-top: 6px; }
```

- [ ] **Step 4: Visual verification**

Open the page in the browser, confirm the wizard panel is hidden by default (`hidden` class), the layout is unchanged when no protocol is active. No commit yet.

---

### Task 12 : `web/js/protocol.js` — central state module

**Files:**
- Create: `web/js/protocol.js`

- [ ] **Step 1: Create the module skeleton**

```javascript
// web/js/protocol.js
// Central state + DOM coordination for the diagnostic protocol surface.
// Receives WS events relayed by llm.js, owns the protocol object in
// memory, dispatches to the three render modules (wizard / floating /
// inline) which read state via getProtocol() and re-render on change.

const state = {
  proto: null,           // {protocol_id, title, steps:[…], current_step_id, …} or null
  send: null,            // (payload) => void  — set by main.js
  hasBoard: false,
};

const subscribers = new Set();

function notify() { subscribers.forEach((cb) => cb(state.proto)); }

export function init({ send, hasBoard }) {
  state.send = send;
  state.hasBoard = !!hasBoard;
  notify();
}

export function setHasBoard(value) {
  state.hasBoard = !!value;
  notify();
}

export function subscribe(cb) {
  subscribers.add(cb);
  cb(state.proto);
  return () => subscribers.delete(cb);
}

export function getProtocol() { return state.proto; }
export function hasBoard() { return state.hasBoard; }

export function applyEvent(ev) {
  if (!ev || typeof ev !== "object") return;
  switch (ev.type) {
    case "protocol_proposed":
      state.proto = {
        protocol_id: ev.protocol_id,
        title: ev.title,
        rationale: ev.rationale,
        steps: ev.steps || [],
        current_step_id: ev.current_step_id,
        history: [],
      };
      break;
    case "protocol_updated":
      if (!state.proto || state.proto.protocol_id !== ev.protocol_id) break;
      state.proto.steps = ev.steps || state.proto.steps;
      state.proto.current_step_id = ev.current_step_id;
      if (Array.isArray(ev.history_tail)) {
        state.proto.history = state.proto.history.concat(ev.history_tail);
      }
      break;
    case "protocol_completed":
      state.proto = null;
      break;
    default:
      return;
  }
  notify();
}

export function submitStepResult({ stepId, value, unit, observation }) {
  if (!state.proto || !state.send) return;
  state.send({
    type: "protocol_step_result",
    protocol_id: state.proto.protocol_id,
    step_id: stepId,
    value, unit, observation,
  });
}

export function skipStep({ stepId, reason }) {
  if (!state.proto || !state.send) return;
  state.send({
    type: "protocol_step_result",
    protocol_id: state.proto.protocol_id,
    step_id: stepId,
    skip_reason: reason || "tech: skip",
  });
}

export function abandonProtocol() {
  if (!state.proto || !state.send) return;
  state.send({
    type: "protocol_abandon",
    protocol_id: state.proto.protocol_id,
    reason: "tech_dismiss",
  });
}
```

---

### Task 13 : Wizard panel renderer

**Files:**
- Create: `web/js/protocol.js` (extend with wizard render)
- Modify: `web/js/main.js`

- [ ] **Step 1: Add wizard render in protocol.js**

```javascript
// Append to web/js/protocol.js

function renderStepRow(step, isActive) {
  const li = document.createElement("li");
  li.className = `protocol-step is-${step.status}`;
  li.dataset.stepId = step.id;

  const badge = document.createElement("span");
  badge.className = "protocol-step-badge";
  badge.textContent = step.status === "done" ? "✓"
                    : step.status === "skipped" ? "·"
                    : step.status === "failed" ? "✗"
                    : numberFromStepId(step.id);
  li.appendChild(badge);

  const body = document.createElement("div");
  body.className = "protocol-step-body";

  const target = document.createElement("div");
  target.className = "protocol-step-target";
  target.textContent = step.target || step.test_point || "—";
  body.appendChild(target);

  const instr = document.createElement("p");
  instr.className = "protocol-step-instruction";
  instr.textContent = step.instruction;
  body.appendChild(instr);

  const why = document.createElement("p");
  why.className = "protocol-step-rationale";
  why.textContent = step.rationale;
  body.appendChild(why);

  if (step.result && step.status !== "active") {
    const res = document.createElement("div");
    res.className = "protocol-step-result";
    res.textContent = formatResult(step);
    body.appendChild(res);
  }

  if (isActive) {
    body.appendChild(buildStepForm(step));
  }

  li.appendChild(body);
  return li;
}

function numberFromStepId(id) {
  // s_1 → 1, ins_xx → "+"
  const m = /^s_(\d+)$/.exec(id);
  return m ? m[1] : "+";
}

function formatResult(step) {
  const r = step.result;
  if (!r) return "";
  if (step.type === "numeric") return `${r.value} ${r.unit || step.unit || ""} (${r.outcome})`;
  if (step.type === "boolean") return `${r.value ? "oui" : "non"} (${r.outcome})`;
  if (step.type === "observation") return r.value || "—";
  if (step.type === "ack") return "fait";
  return JSON.stringify(r);
}

function buildStepForm(step) {
  const form = document.createElement("form");
  form.className = "protocol-step-form";
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    handleSubmit(step, form);
  });

  if (step.type === "numeric") {
    const input = document.createElement("input");
    input.type = "number"; input.step = "any"; input.required = true;
    input.placeholder = step.nominal != null ? `nominal ${step.nominal}` : "valeur";
    input.name = "value";
    form.appendChild(input);
    const unit = document.createElement("select");
    unit.name = "unit";
    for (const u of ["V", "mV", "A", "mA", "Ω", "kΩ"]) {
      const opt = document.createElement("option");
      opt.value = u; opt.textContent = u;
      if (u === step.unit) opt.selected = true;
      unit.appendChild(opt);
    }
    form.appendChild(unit);
  } else if (step.type === "boolean") {
    const yes = document.createElement("button");
    yes.type = "button"; yes.textContent = "Oui";
    yes.addEventListener("click", () => submitBoolean(step, true));
    const no = document.createElement("button");
    no.type = "button"; no.textContent = "Non"; no.classList.add("is-skip");
    no.addEventListener("click", () => submitBoolean(step, false));
    form.appendChild(yes); form.appendChild(no);
  } else if (step.type === "observation") {
    const ta = document.createElement("textarea");
    ta.name = "observation"; ta.rows = 2; ta.required = true;
    ta.placeholder = "ce que tu observes…";
    form.appendChild(ta);
  } else if (step.type === "ack") {
    // ack: just a Done button below; submit fires submit event with no value.
  }

  const submit = document.createElement("button");
  submit.type = "submit";
  submit.textContent = step.type === "ack" ? "Fait" : "Valider";
  form.appendChild(submit);

  const skip = document.createElement("button");
  skip.type = "button"; skip.className = "is-skip"; skip.textContent = "Skip";
  skip.addEventListener("click", () => {
    const reason = window.prompt("Pourquoi tu skip ce step ?", "");
    if (reason !== null) skipStep({ stepId: step.id, reason });
  });
  form.appendChild(skip);

  return form;
}

function submitBoolean(step, value) {
  submitStepResult({ stepId: step.id, value });
}

function handleSubmit(step, form) {
  const fd = new FormData(form);
  if (step.type === "numeric") {
    const val = parseFloat(fd.get("value"));
    if (Number.isNaN(val)) return;
    submitStepResult({ stepId: step.id, value: val, unit: fd.get("unit") || step.unit });
  } else if (step.type === "observation") {
    const obs = String(fd.get("observation") || "").trim();
    if (!obs) return;
    submitStepResult({ stepId: step.id, value: obs });
  } else if (step.type === "ack") {
    submitStepResult({ stepId: step.id, value: "done" });
  }
}

function renderWizard(proto) {
  const root = document.getElementById("protocolWizard");
  if (!root) return;
  if (!proto) {
    root.classList.add("hidden");
    return;
  }
  root.classList.remove("hidden");
  document.getElementById("protocolTitle").textContent = proto.title;
  const list = document.getElementById("protocolStepList");
  list.innerHTML = "";
  for (const step of proto.steps) {
    list.appendChild(renderStepRow(step, step.id === proto.current_step_id));
  }
  const histList = document.getElementById("protocolHistoryList");
  histList.innerHTML = "";
  for (const h of proto.history.slice(-10)) {
    const li = document.createElement("li");
    li.textContent = `${h.action}${h.step_id ? " · " + h.step_id : ""}${h.reason ? " · " + h.reason : ""}`;
    histList.appendChild(li);
  }
}

const abandonBtn = () => {
  const btn = document.getElementById("protocolAbandonBtn");
  if (btn && !btn.dataset.bound) {
    btn.addEventListener("click", () => {
      if (window.confirm("Abandonner le protocole en cours ?")) abandonProtocol();
    });
    btn.dataset.bound = "1";
  }
};

subscribe(renderWizard);
subscribe(abandonBtn);
```

- [ ] **Step 2: Wire `protocol.js` in `web/js/main.js`**

```javascript
// web/js/main.js — append to the existing boot path

import * as Protocol from "./protocol.js";

// After the WS opens (you have a `send` function for outbound messages
// and you know whether a board is loaded — adapt to existing globals):
Protocol.init({
  send: (payload) => llmWs.send(JSON.stringify(payload)),
  hasBoard: !!window.Boardview?.hasBoard?.(),
});
window.Protocol = Protocol; // expose for llm.js relay
```

- [ ] **Step 3: Manual browser verify**

Reload page (hard reload). Inspect: nothing visible (no protocol active). No JS errors in console.

---

### Task 14 : `llm.js` relay + inline cards (Mode C)

**Files:**
- Modify: `web/js/llm.js`

- [ ] **Step 1: Relay protocol_* events to Protocol module**

In `llm.js`'s WS message handler, where the agent message types are dispatched, add:

```javascript
// In llm.js incoming message handler

if (data.type === "protocol_proposed" || data.type === "protocol_updated" || data.type === "protocol_completed") {
  if (window.Protocol) window.Protocol.applyEvent(data);
  // Mode C inline rendering — only when no board is loaded.
  if (!window.Boardview?.hasBoard?.() && data.type !== "protocol_completed") {
    renderInlineProtocolCard(data);
  }
  return;
}
```

- [ ] **Step 2: Implement `renderInlineProtocolCard`**

```javascript
// In llm.js, alongside other render helpers

function renderInlineProtocolCard(ev) {
  // Render only the active step as an inline bubble. Past steps are
  // summarized in a thin row separately.
  const proto = window.Protocol.getProtocol();
  if (!proto) return;
  const active = proto.steps.find((s) => s.id === proto.current_step_id);
  if (!active) return;

  const chat = document.getElementById("llmChatStream");
  if (!chat) return;

  // Avoid rendering duplicates for the same active step id.
  const already = chat.querySelector(`.protocol-inline-card[data-step="${active.id}"]`);
  if (already) return;

  const card = document.createElement("div");
  card.className = "protocol-inline-card";
  card.dataset.step = active.id;
  card.innerHTML = `
    <div class="protocol-step-target">${escapeHtml(active.target || active.test_point || "—")}</div>
    <p class="protocol-step-instruction">${escapeHtml(active.instruction)}</p>
    <p class="protocol-step-rationale">${escapeHtml(active.rationale)}</p>
  `;
  card.appendChild(window.Protocol.buildStepForm(active));
  chat.appendChild(card);
  chat.scrollTop = chat.scrollHeight;
}
```

For this to work, `protocol.js` must expose `buildStepForm` as part of its public ES module API. Add this export at the top of `protocol.js` (near the existing `init` / `applyEvent` exports):

```javascript
// In web/js/protocol.js — make form builder reachable from llm.js
export { buildStepForm };
```

And update the `Protocol = { … }` global assignment in `main.js` to include it:

```javascript
window.Protocol = { ...Protocol, buildStepForm: Protocol.buildStepForm };
```

Already covered by `import * as Protocol from "./protocol.js"; window.Protocol = Protocol;` (the namespace-import form picks up every named export automatically) — so the only required diff is adding `export { buildStepForm }` in `protocol.js`.

- [ ] **Step 3: Manual browser verify (no-board path)**

Open a repair on a device with no board file. Trigger the agent to emit a protocol (or simulate by calling `window.Protocol.applyEvent({…})` from devtools with a fake event). Verify the inline card renders, submit advances, etc.

---

### Task 15 : Floating card on canvas (Mode A) + numbered badges in `brd_viewer.js`

**Files:**
- Modify: `web/brd_viewer.js`
- Modify: `web/js/protocol.js`

- [ ] **Step 1: Expose protocol badges on `window.Boardview`**

In `brd_viewer.js`, alongside the existing `state.agent` block, add a `protocol` substate:

```javascript
// In brd_viewer.js — extend `state.agent` or add a sibling
state.agent.protocolSteps = [];        // list of {id, target, status} from the active protocol
state.agent.protocolActive = null;     // current_step_id

// Public API
window.Boardview = window.Boardview || {};
window.Boardview.setProtocolBadges = function (steps, currentId) {
  state.agent.protocolSteps = Array.isArray(steps) ? steps : [];
  state.agent.protocolActive = currentId || null;
  requestRedraw();
};
window.Boardview.clearProtocolBadges = function () {
  state.agent.protocolSteps = [];
  state.agent.protocolActive = null;
  requestRedraw();
};
window.Boardview.hasBoard = function () {
  return !!state.partByRefdes && state.partByRefdes.size > 0;
};
```

- [ ] **Step 2: Render numbered badges in the draw pass**

After the existing `state.agent.highlights` render block, add:

```javascript
// In brd_viewer.js draw() — after the existing agent highlight render

if (state.agent.protocolSteps && state.agent.protocolSteps.length > 0) {
  const cyan = cssVar('--cyan') || '#38bdf8';
  const amber = cssVar('--amber') || '#f59e0b';
  const bgDeep = cssVar('--bg-deep') || '#06080d';
  const now = performance.now();

  for (let i = 0; i < state.agent.protocolSteps.length; i++) {
    const st = state.agent.protocolSteps[i];
    if (!st.target) continue;  // test-point steps have no canvas anchor
    const part = state.partByRefdes?.get(st.target);
    if (!part) continue;
    if (part.layer !== LAYER_BOTH) {
      if (activeSide === LAYER_TOP    && part.layer !== LAYER_TOP)    continue;
      if (activeSide === LAYER_BOTTOM && part.layer !== LAYER_BOTTOM) continue;
    }
    const bbox = state.partBodyBboxes?.get(st.target) || part.bbox;
    if (!bbox || bbox.length < 2) continue;
    const a = milsToScreen(bbox[0].x, bbox[0].y, boardW);
    const b = milsToScreen(bbox[1].x, bbox[1].y, boardW);
    const cx = (a.x + b.x) / 2;
    const cy = Math.min(a.y, b.y) - 10;  // above the bbox

    const isActive = st.id === state.agent.protocolActive;
    const isDone   = st.status === "done";
    const isFail   = st.status === "failed";
    const isSkip   = st.status === "skipped";
    const fill     = (isFail || isSkip) ? amber : cyan;
    const glyph    = isDone ? "✓" : isFail ? "✗" : isSkip ? "·" : (i + 1).toString();

    ctx.save();
    if (isActive) {
      // Reuse the same pulse cadence we already tuned (3.2s envelope).
      const elapsed = state.agent.highlightPulseAt ? now - state.agent.highlightPulseAt : 0;
      const env = Math.max(0, 1 - elapsed / 3200);
      ctx.globalAlpha = 0.4 + 0.4 * env;
    } else if (isDone) {
      ctx.globalAlpha = 0.7;
    }
    ctx.fillStyle = fill;
    ctx.beginPath();
    ctx.arc(cx, cy, 9, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = bgDeep;
    ctx.font = "600 11px 'JetBrains Mono', monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(glyph, cx, cy + 0.5);
    ctx.restore();
  }
}
```

- [ ] **Step 3: Push badge state from `protocol.js`**

```javascript
// In web/js/protocol.js — add another subscriber

function pushBadgesToBoard(proto) {
  if (!window.Boardview || !window.Boardview.setProtocolBadges) return;
  if (!proto) {
    window.Boardview.clearProtocolBadges();
    return;
  }
  const minimal = proto.steps.map((s) => ({
    id: s.id, target: s.target, status: s.status,
  }));
  window.Boardview.setProtocolBadges(minimal, proto.current_step_id);
}
subscribe(pushBadgesToBoard);
```

- [ ] **Step 4: Implement Mode A floating card**

Append to `protocol.js`:

```javascript
function renderFloating(proto) {
  const card = document.getElementById("protocolFloatingCard");
  if (!card) return;
  if (!proto || !state.hasBoard) {
    card.classList.add("hidden");
    return;
  }
  const active = proto.steps.find((s) => s.id === proto.current_step_id);
  if (!active || !active.target) {
    card.classList.add("hidden");
    return;
  }
  const screenPos = window.Boardview?.refdesScreenPos?.(active.target);
  if (!screenPos) { card.classList.add("hidden"); return; }

  card.classList.remove("hidden");
  card.style.left = `${screenPos.x + 12}px`;
  card.style.top  = `${screenPos.y - 12}px`;

  card.innerHTML = `
    <div class="protocol-float-head">
      <span class="protocol-float-badge">${numberFromStepId(active.id)}</span>
      <span class="protocol-float-target">${active.target}</span>
    </div>
    <p class="protocol-float-instruction">${escapeHtml(active.instruction)}</p>
  `;
  card.appendChild(buildStepForm(active));
}
subscribe(renderFloating);

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}
```

- [ ] **Step 5: Add `refdesScreenPos` to `window.Boardview`**

In `brd_viewer.js`:

```javascript
window.Boardview.refdesScreenPos = function (refdes) {
  const part = state.partByRefdes?.get(refdes);
  if (!part) return null;
  const bbox = state.partBodyBboxes?.get(refdes) || part.bbox;
  if (!bbox || bbox.length < 2) return null;
  const a = milsToScreen(bbox[0].x, bbox[0].y, boardW);
  const b = milsToScreen(bbox[1].x, bbox[1].y, boardW);
  return { x: (a.x + b.x) / 2, y: Math.min(a.y, b.y) };
};
```

- [ ] **Step 6: Manual browser verify (with-board path)**

Open a repair on `mnt-reform-motherboard` (board loaded). Have the agent emit a protocol via chat ("propose-moi un protocole pour pas de boot écran noir"). Verify:
- Wizard panel appears top-right
- Numbered badges appear on R49 / F1 / etc.
- Active step's badge pulses
- Floating card appears above the active component
- Submitting via either floating or wizard advances state and updates the board

Report any visual issue (this is the manual verification gate per `feedback_visual_changes_require_user_verify`).

---

### Task 16 : Frontend commit

- [ ] **Step 1: Wait for Alexis's visual OK** (manual verification gate per memory rule).

- [ ] **Step 2: Commit frontend**

```bash
git add web/js/protocol.js web/js/main.js web/js/llm.js web/brd_viewer.js web/styles/protocol.css web/index.html
git commit -m "$(cat <<'EOF'
feat(web): protocol UI surfaces — wizard, board badges, floating card, inline fallback

Adds web/js/protocol.js as the single source of frontend state for
the diagnostic protocol — receives protocol_proposed / protocol_updated
WS events relayed by llm.js, owns the in-memory protocol object,
notifies subscribers (wizard panel, floating card, board badges,
inline cards) on any change.

Wizard panel sits in the top 40 % of the right slide-in column when a
protocol is active and is unmounted otherwise (no layout shift on
absence). Floating instruction card anchors above the active
component's bbox using a new window.Boardview.refdesScreenPos
helper. Numbered step badges render in brd_viewer alongside the
existing agent-highlight pass — colors restricted to --cyan and
--amber per spec §6.5; --emerald and --violet stay locked to their
existing meanings.

When no board is loaded, Modes A and B are not mounted; the protocol
surfaces inline as chat-stream "step card" bubbles in llm.js. Same
input shape across all three modes (numeric / boolean / observation /
ack + universal skip).

Spec: docs/superpowers/specs/2026-04-25-stepwise-diagnostic-protocol-design.md
EOF
)"
```

---

## Self-Review

After writing the plan, re-read against the spec:

- **Spec coverage** — every section of the spec maps to at least one task:
  - §1-3 (problem / goals / non-goals) → preamble framing in commits.
  - §4.1 happy path with board → Tasks 11-15 (wizard + floating + badges).
  - §4.2 without board → Task 14 (inline cards).
  - §4.3 tech-in-chat → §11 of the spec is reflected in the system prompt PROTOCOL block (Task 6 step 4) and `bv_record_step_result` dispatch (Task 8).
  - §4.4 tech declines → in the system prompt opt-out wording (Task 6).
  - §5 data shape → Task 1 (schemas).
  - §6.1 floating → Task 15.
  - §6.2 wizard → Task 13.
  - §6.3 inline → Task 14.
  - §6.4 / §6.5 board badges + cyan/amber-only → Task 15 step 2.
  - §7.1 propose tool → Task 3.
  - §7.2 update tool → Task 5.
  - §7.3 record tool → Task 4.
  - §8 WS protocol → Task 9.
  - §9 state machine → Tasks 3-5.
  - §10 persistence → Task 2.
  - §10.2 `bv_get_protocol` → Task 8 step 3 (final dispatch branch).
  - §11 prompt updates → Task 6 step 4 + Task 7.
  - §12 error handling → covered by the per-tool soft-error returns in Tasks 3-5.
  - §13 testing → tests live alongside each backend task; manual verify gate in Task 15 / 16.
  - §14 phasing → matches: 1 spec commit (already done) + 1 backend (Task 10) + 1 frontend (Task 16).
- **Placeholders** — no `TBD` / `add validation` / `similar to Task N` in any step.
- **Type consistency** — `propose_protocol` returns `{ok, protocol_id, step_count, current_step_id}`, used identically downstream. `record_step_result` returns `{ok, outcome, current_step_id, protocol_id}`, used identically. `update_protocol` returns `{ok, current_step_id, protocol_id}` (and `status` for terminal actions). Step IDs are `s_N` for original, `ins_xx` for inserts — both formats allowed everywhere.

If any issue surfaces during execution that contradicts this plan, treat the spec as the source of truth and update both files in lock-step.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-25-stepwise-diagnostic-protocol.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
