# tests/tools/test_protocol.py
"""Unit tests for the diagnostic protocol module."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.tools.protocol import (
    HistoryEntry,
    Protocol,
    Step,
    StepInput,
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
    assert list(s.pass_range) == [9.0, 32.0]


def test_step_must_have_target_or_test_point_when_numeric():
    with pytest.raises(ValidationError, match="target.*test_point"):
        StepInput(
            type="numeric",
            instruction="Probe somewhere",
            rationale="check",
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


def test_persist_and_load_roundtrip(tmp_path):
    from api.tools.protocol import (
        load_active_pointer,
        load_protocol,
        save_active_pointer,
        save_protocol,
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
                rationale="why?",
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
                rationale="rationale",
                unit="V",
            )
        ],
        valid_refdes={"R49", "F1"},  # UNKNOWN_999 not in board
    )
    assert out["ok"] is False
    assert out["reason"] == "unknown-refdes"
    assert "UNKNOWN_999" in out["unknown_targets"]


def test_propose_protocol_caps_step_count(tmp_path):
    from api.tools.protocol import MAX_STEPS_PER_PROTOCOL, propose_protocol

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
    from api.tools.protocol import load_active_pointer, propose_protocol

    s = StepInput(type="ack", target="U1", instruction="reflow U1", rationale="reseat")
    propose_protocol(
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


def test_record_numeric_advances_step_and_persists_measurement(tmp_path, monkeypatch):
    from api.tools import protocol as P

    P.propose_protocol(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        title="t", rationale="r",
        steps=[
            StepInput(type="numeric", target="R49", instruction="probe VIN",
                      rationale="check", unit="V", nominal=24.0,
                      pass_range=(9.0, 32.0)),
            StepInput(type="ack", target="F1", instruction="reflow F1",
                      rationale="re-seat fuse"),
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
            StepInput(type="numeric", target="R49", instruction="probe VIN",
                      rationale="check", unit="V", pass_range=(9.0, 32.0)),
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
                      rationale="check", unit="V"),
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
        steps=[StepInput(type="ack", target="U1", instruction="reflow", rationale="reseat"),
               StepInput(type="ack", target="U2", instruction="reflow", rationale="reseat")],
        valid_refdes={"U1", "U2"},
    )
    out = record_step_result(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        step_id="s_2", submitted_by="tech",
    )
    assert out["ok"] is False
    assert out["reason"] == "step_not_active"


# ---------------------------------------------------------------------------
# Task 5 — update_protocol tests
# ---------------------------------------------------------------------------


def _seed_three_step_protocol(tmp_path) -> str:
    from api.tools.protocol import propose_protocol
    out = propose_protocol(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        title="t", rationale="r",
        steps=[
            StepInput(type="ack", target=f"U{i}", instruction=f"reflow {i}",
                      rationale="reseat") for i in range(1, 4)
        ],
        valid_refdes={"U1", "U2", "U3"},
    )
    return out["protocol_id"]


def test_update_insert_after(tmp_path):
    from api.tools.protocol import load_active_protocol, update_protocol
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
    assert ids[1].startswith("s_") or ids[1].startswith("ins_")  # inserted got fresh id
    assert ids[1] != "s_2"          # not the renumbered one
    assert proto.history[-1].action == "step_inserted"


def test_update_skip_marks_step(tmp_path):
    from api.tools.protocol import load_active_protocol, update_protocol
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
    from api.tools.protocol import load_active_protocol, update_protocol
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
    from api.tools.protocol import load_active_protocol, update_protocol
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
    from api.tools.protocol import load_active_pointer, update_protocol
    _seed_three_step_protocol(tmp_path)
    out = update_protocol(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        action="abandon_protocol", reason="tech declined",
    )
    assert out["ok"] is True
    pointer = load_active_pointer(tmp_path, "demo", "r1")
    assert pointer["active_protocol_id"] is None
