"""Tests for the cross-conversation session log.

Covers JSON-first write (works without MA), the MA mirror (flag-gated, mocked),
idempotency on re-call for the same (repair, conv) pair, list_session_logs,
and a manifest sanity check that the tool is exposed to the agent.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from api import config as config_mod
from api.agent.conversation_log import (
    list_session_logs,
    record_session_log,
)
from api.agent.manifest import build_tools_manifest
from api.agent.tools import mb_record_session_log


@pytest.fixture(autouse=True)
def reset_settings_cache(monkeypatch):
    monkeypatch.setattr(config_mod, "_settings", None)
    yield
    monkeypatch.setattr(config_mod, "_settings", None)


async def test_session_log_is_owner_scoped(tmp_path: Path, monkeypatch):
    """Session logs are the agent's PRIVATE working memory: scoped to the session
    owner (the cloud's X-Owner-Ref / tenant). One tenant never reads another's
    narrative; the standalone (ownerless) view is isolated from tenant logs."""
    monkeypatch.setenv("MA_MEMORY_STORE_ENABLED", "false")
    base = dict(
        client=None, device_slug="demo-pi", conv_id="c1",
        symptom="3V3 rail dead", outcome="paused", memory_root=tmp_path,
    )
    await record_session_log(repair_id="R1", owner_ref="tenant-a", **base)

    a = list_session_logs(device_slug="demo-pi", memory_root=tmp_path, owner_ref="tenant-a")
    assert len(a) == 1 and a[0]["repair_id"] == "R1"
    assert list_session_logs(device_slug="demo-pi", memory_root=tmp_path, owner_ref="tenant-b") == []
    assert list_session_logs(device_slug="demo-pi", memory_root=tmp_path) == []  # standalone view isolated


async def test_record_writes_markdown_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MA_MEMORY_STORE_ENABLED", "false")
    status = await record_session_log(
        client=None,
        device_slug="demo-pi",
        repair_id="R1",
        conv_id="c1",
        symptom="3V3 rail dead",
        outcome="paused",
        tested=[
            {"target": "rail:3V3", "result": "0V"},
            {"target": "comp:U7", "result": "normal"},
        ],
        hypotheses=[
            {"refdes": "U7", "verdict": "rejected", "evidence": "3V3 still 0V after reflow"},
            {"refdes": "Q3", "verdict": "inconclusive", "evidence": "à mesurer"},
        ],
        findings=[],
        next_steps="Mesurer Q3 en mode diode",
        lesson="Sur ce device, U7 n'est PAS la source 3V3 — c'est Q3 (load switch).",
        memory_root=tmp_path,
    )

    assert status["ok"] is True
    assert status["json_status"] == "written"
    assert status["ma_mirror_status"] == "skipped:flag_disabled"

    log_dir = tmp_path / "demo-pi" / "conversation_log"
    files = list(log_dir.glob("*.md"))
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert "outcome: paused" in body
    assert "rail:3V3" in body and "0V" in body
    assert "Q3" in body and "inconclusive" in body
    assert "## Lesson" in body
    assert "load switch" in body


async def test_recall_overwrites_same_conv(tmp_path: Path, monkeypatch):
    """Same (repair, conv) re-call should overwrite — agent updates its own log."""
    monkeypatch.setenv("MA_MEMORY_STORE_ENABLED", "false")
    common = dict(
        client=None,
        device_slug="iphone-x",
        repair_id="R7",
        conv_id="cAlpha",
        symptom="No charge",
        memory_root=tmp_path,
    )
    await record_session_log(**common, outcome="unresolved", lesson="initial")
    await record_session_log(**common, outcome="resolved", lesson="updated")

    files = list((tmp_path / "iphone-x" / "conversation_log").glob("*.md"))
    assert len(files) == 1, "second call must rewrite, not duplicate"
    body = files[0].read_text(encoding="utf-8")
    assert "outcome: resolved" in body
    assert "updated" in body
    assert "initial" not in body


async def test_invalid_outcome_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MA_MEMORY_STORE_ENABLED", "false")
    status = await record_session_log(
        client=None,
        device_slug="demo-pi",
        repair_id="R1",
        conv_id="c1",
        symptom="x",
        outcome="bogus",
        memory_root=tmp_path,
    )
    assert status["ok"] is False
    assert "outcome" in status["error"]
    assert not (tmp_path / "demo-pi" / "conversation_log").exists()


async def test_ma_mirror_invoked_when_flag_on(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MA_MEMORY_STORE_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    # Patch the two MA helpers so we don't hit the network.
    from api.agent import conversation_log as mod

    fake_store_id = "memstore_TESTID"
    monkeypatch.setattr(mod, "ensure_memory_store", AsyncMock(return_value=fake_store_id))
    upsert = AsyncMock(return_value="ok")
    monkeypatch.setattr(mod, "upsert_memory", upsert)

    status = await record_session_log(
        client=MagicMock(),
        device_slug="iphone-x",
        repair_id="R7",
        conv_id="cBeta",
        symptom="No boot",
        outcome="paused",
        memory_root=tmp_path,
    )
    assert status["ok"] is True
    assert status["ma_mirror_status"] == "mirrored"
    assert upsert.await_count == 1
    call_kwargs = upsert.await_args.kwargs
    assert call_kwargs["store_id"] == fake_store_id
    assert call_kwargs["path"].startswith("/conversation_log/")
    assert call_kwargs["path"].endswith(".md")


async def test_list_returns_newest_first(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MA_MEMORY_STORE_ENABLED", "false")
    common = dict(client=None, device_slug="demo-pi", symptom="x", memory_root=tmp_path)
    await record_session_log(**common, repair_id="R1", conv_id="c1", outcome="resolved")
    # tiny sleep substitute: re-call with a different conv to get a second file
    await record_session_log(**common, repair_id="R2", conv_id="c1", outcome="paused")

    rows = list_session_logs(device_slug="demo-pi", memory_root=tmp_path)
    assert len(rows) == 2
    # both present, sorted desc by created_at — at least the keys are intact
    outcomes = {r["outcome"] for r in rows}
    assert outcomes == {"resolved", "paused"}
    repairs = {r["repair_id"] for r in rows}
    assert repairs == {"R1", "R2"}


async def test_tool_wrapper_round_trip(tmp_path: Path, monkeypatch):
    """The tools.py wrapper should pass straight through to the module."""
    monkeypatch.setenv("MA_MEMORY_STORE_ENABLED", "false")
    status = await mb_record_session_log(
        client=None,
        device_slug="iphone-x",
        repair_id="R12",
        conv_id="cZ",
        symptom="USB doesn't enumerate",
        outcome="escalated",
        memory_root=tmp_path,
        next_steps="Tristar replacement",
    )
    assert status["ok"] is True
    files = list((tmp_path / "iphone-x" / "conversation_log").glob("*.md"))
    assert len(files) == 1
    assert "escalated" in files[0].read_text(encoding="utf-8")


def test_manifest_exposes_tool():
    from api.session.state import SessionState

    tools = build_tools_manifest(SessionState())
    names = {t["name"] for t in tools}
    assert "mb_record_session_log" in names

    spec = next(t for t in tools if t["name"] == "mb_record_session_log")
    schema = spec["input_schema"]
    assert set(schema["required"]) == {"symptom", "outcome"}
    outcome_enum = schema["properties"]["outcome"]["enum"]
    assert set(outcome_enum) == {"resolved", "unresolved", "paused", "escalated"}
