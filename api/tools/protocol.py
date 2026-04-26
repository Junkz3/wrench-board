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
    def _validate_type_specific(self) -> StepInput:
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
                    raise ValueError("pass_range must be [lo, hi] with lo < hi")
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


# --- Persistence -------------------------------------------------------------

POINTER_FILENAME = "protocol.json"
PROTOCOLS_SUBDIR = "protocols"


def _repair_dir(memory_root: Path, device_slug: str, repair_id: str) -> Path:
    return memory_root / device_slug / "repairs" / repair_id


def _conv_scope_dir(
    memory_root: Path,
    device_slug: str,
    repair_id: str,
    conv_id: str | None,
) -> Path:
    """Return the directory the protocol artefacts live under.

    Per-conv when `conv_id` is given (each chat thread holds its own
    plan, so opening a fresh conv shows no protocol even if a sibling
    conv has one running). Falls back to the repair-root location for
    callers that don't track conv (legacy artefacts + the REST endpoint
    `GET /pipeline/repairs/{rid}/protocol` until it grows a `?conv=`).
    """
    base = _repair_dir(memory_root, device_slug, repair_id)
    if conv_id:
        return base / "conversations" / conv_id
    return base


def _pointer_path(
    memory_root: Path, device_slug: str, repair_id: str, conv_id: str | None = None,
) -> Path:
    return _conv_scope_dir(memory_root, device_slug, repair_id, conv_id) / POINTER_FILENAME


def _protocol_path(
    memory_root: Path,
    device_slug: str,
    repair_id: str,
    protocol_id: str,
    conv_id: str | None = None,
) -> Path:
    return (
        _conv_scope_dir(memory_root, device_slug, repair_id, conv_id)
        / PROTOCOLS_SUBDIR
        / f"{protocol_id}.json"
    )


def save_protocol(
    memory_root: Path, proto: Protocol, *, conv_id: str | None = None,
) -> None:
    """Atomically write the full protocol artifact to disk."""
    path = _protocol_path(
        memory_root, proto.device_slug, proto.repair_id, proto.protocol_id, conv_id,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = proto.model_dump(mode="json")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_protocol(
    memory_root: Path,
    device_slug: str,
    repair_id: str,
    protocol_id: str,
    *,
    conv_id: str | None = None,
) -> Protocol | None:
    path = _protocol_path(memory_root, device_slug, repair_id, protocol_id, conv_id)
    if not path.exists():
        return None
    try:
        return Protocol.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[protocol] failed to load %s: %s", path, exc)
        return None


def save_active_pointer(
    memory_root: Path, device_slug: str, repair_id: str, protocol_id: str | None,
    *, prior_status: ProtocolStatus | None = None, conv_id: str | None = None,
) -> None:
    """Set the active pointer; appends an entry to its rolling history.

    `prior_status` is the status that the previously-active protocol takes
    (typically `replaced` or `abandoned`); a fresh repair has no prior.
    """
    path = _pointer_path(memory_root, device_slug, repair_id, conv_id)
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
    memory_root: Path, device_slug: str, repair_id: str,
    *, conv_id: str | None = None,
) -> dict[str, Any]:
    path = _pointer_path(memory_root, device_slug, repair_id, conv_id)
    if not path.exists():
        return {"active_protocol_id": None, "history": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"active_protocol_id": None, "history": []}


def load_active_protocol(
    memory_root: Path, device_slug: str, repair_id: str,
    *, conv_id: str | None = None,
) -> Protocol | None:
    pointer = load_active_pointer(memory_root, device_slug, repair_id, conv_id=conv_id)
    pid = pointer.get("active_protocol_id")
    if not pid:
        return None
    return load_protocol(memory_root, device_slug, repair_id, pid, conv_id=conv_id)


# --- Protocol factory --------------------------------------------------------

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
    conv_id: str | None = None,
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
    pointer = load_active_pointer(memory_root, device_slug, repair_id, conv_id=conv_id)
    prior_id = pointer.get("active_protocol_id")
    if prior_id:
        prior = load_protocol(memory_root, device_slug, repair_id, prior_id, conv_id=conv_id)
        if prior is not None and prior.status == "active":
            prior.status = "replaced"
            prior.history.append(HistoryEntry(action="replaced_protocol", ts=now,
                                              reason="superseded by fresh propose"))
            save_protocol(memory_root, prior, conv_id=conv_id)

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
    save_protocol(memory_root, proto, conv_id=conv_id)
    save_active_pointer(
        memory_root, device_slug, repair_id, pid,
        prior_status="replaced" if prior_id else None,
        conv_id=conv_id,
    )
    return {"ok": True, "protocol_id": pid, "step_count": len(materialised),
            "current_step_id": proto.current_step_id}


# --- Measurement bridge helpers -----------------------------------------------


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


def _record_measurement(**kwargs: Any) -> dict[str, Any]:
    """Indirection so tests can monkey-patch.

    Routes to the production `mb_record_measurement` at call time.
    """
    from api.tools.measurements import mb_record_measurement
    return mb_record_measurement(**kwargs)


def _set_observation(**kwargs: Any) -> dict[str, Any]:
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
    if next_id is not None:
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
    conv_id: str | None = None,
) -> dict[str, Any]:
    """Record the tech's result for a step and advance the state machine.

    Returns ``{"ok": True, "outcome": ..., "current_step_id": ...}`` on
    success, or ``{"ok": False, "reason": ...}`` on validation failure.
    """
    proto = load_active_protocol(memory_root, device_slug, repair_id, conv_id=conv_id)
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
        save_protocol(memory_root, proto, conv_id=conv_id)
        return {"ok": True, "outcome": "skipped", "current_step_id": next_id,
                "protocol_id": proto.protocol_id}

    # Type-specific routing to measurement plumbing.
    outcome: Literal["pass", "fail", "neutral"] = "neutral"
    if step.type == "numeric":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
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
    save_protocol(memory_root, proto, conv_id=conv_id)
    return {"ok": True, "outcome": outcome, "current_step_id": next_id,
            "protocol_id": proto.protocol_id}


# --- Protocol update (insert / skip / reorder / complete / abandon / replace) -


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
    conv_id: str | None = None,
) -> dict[str, Any]:
    proto = load_active_protocol(memory_root, device_slug, repair_id, conv_id=conv_id)
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
        _persist_step_result_and_advance(
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
        save_protocol(memory_root, proto, conv_id=conv_id)
        # Keep the pointer aimed at this protocol so callers can still load it
        # to inspect the final verdict; the non-active status gates re-entry.
        return {"ok": True, "current_step_id": None, "protocol_id": proto.protocol_id,
                "status": "completed"}

    elif action == "abandon_protocol":
        proto.status = "abandoned"
        proto.completed_at = now
        proto.current_step_id = None
        proto.history.append(HistoryEntry(action="abandoned", reason=reason, ts=now))
        save_protocol(memory_root, proto, conv_id=conv_id)
        save_active_pointer(
            memory_root, device_slug, repair_id, None,
            prior_status="abandoned",
            conv_id=conv_id,
        )
        return {"ok": True, "current_step_id": None, "protocol_id": proto.protocol_id,
                "status": "abandoned"}

    else:
        return {"ok": False, "reason": "unknown_action"}

    save_protocol(memory_root, proto, conv_id=conv_id)
    return {"ok": True, "current_step_id": proto.current_step_id,
            "protocol_id": proto.protocol_id}
