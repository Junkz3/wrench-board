"""Integration: tools dispatch + WS event roundtrip."""

from __future__ import annotations

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
                 "instruction": "do something useful", "rationale": "needed"},
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
            "steps": [{"type": "numeric", "target": "R49",
                       "instruction": "probe VIN",
                       "rationale": "needed", "unit": "V",
                       "pass_range": [9.0, 32.0]}],
        },
        device_slug="demo", memory_root=tmp_path, client=None,
        session=session, repair_id="r1",
    )
    # (For test simplicity SessionState.board is None so refdes validation skipped.)
    out = await _dispatch_tool(
        name="bv_record_step_result",
        payload={"step_id": "s_1", "value": 24.5, "unit": "V"},
        device_slug="demo", memory_root=tmp_path, client=None,
        session=session, repair_id="r1",
    )
    assert out["outcome"] == "pass"
    assert out["current_step_id"] is None  # only 1 step


def test_get_protocol_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from api import config as config_mod
    from api.main import app
    from api.tools.protocol import StepInput, propose_protocol

    # Reset module-level _settings singleton so MEMORY_ROOT env var is re-read.
    config_mod._settings = None
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))

    propose_protocol(
        memory_root=tmp_path, device_slug="demo", repair_id="r1",
        title="t", rationale="r",
        steps=[StepInput(type="ack", target="U1", instruction="reflow", rationale="reseat")],
        valid_refdes={"U1"},
    )

    client = TestClient(app)
    res = client.get("/pipeline/repairs/r1/protocol?device_slug=demo")
    assert res.status_code == 200
    body = res.json()
    assert body["active"] is True
    assert body["current_step_id"] == "s_1"

    res404 = client.get("/pipeline/repairs/missing/protocol?device_slug=demo")
    assert res404.status_code == 200
    assert res404.json()["active"] is False

    # Restore singleton for sibling tests.
    config_mod._settings = None
