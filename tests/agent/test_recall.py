"""Unit tests for `api.agent.recall` — the direct-mode memory recall helpers.

In managed mode the agent greps three FUSE-mounted stores (per-device field
reports, global patterns, global playbooks). Direct mode has no FUSE mount, so
these pure read functions back the wrapper tools that give the direct agent the
same recall. Patterns/playbooks read the real versioned `seed_data/`; field
reports read a tmp memory root.
"""

from __future__ import annotations

from pathlib import Path

from api.agent.field_reports import FieldReport
from api.agent.recall import (
    recall_field_reports,
    search_patterns,
    search_playbooks,
)


def test_recall_tools_present_in_direct_manifest() -> None:
    """The three recall tools must be in the direct-mode manifest unconditionally
    (not gated on a board), so the direct agent always has memory parity."""
    from api.agent.manifest import build_tools_manifest
    from api.session.state import SessionState

    names = {t["name"] for t in build_tools_manifest(SessionState())}
    assert {
        "mb_recall_field_reports",
        "mb_search_patterns",
        "mb_search_playbooks",
    } <= names, names


def _write_report(memory_root: Path, slug: str, refdes: str, symptom: str, cause: str) -> None:
    d = memory_root / slug / "field_reports"
    d.mkdir(parents=True, exist_ok=True)
    fr = FieldReport(
        report_id=f"{refdes}-rep",
        device_slug=slug,
        refdes=refdes,
        symptom=symptom,
        confirmed_cause=cause,
    )
    (d / f"{fr.report_id}.md").write_text(fr.to_markdown(), encoding="utf-8")


# --- field reports -----------------------------------------------------------


def test_recall_field_reports_filters_by_query(tmp_path: Path) -> None:
    _write_report(tmp_path, "dev", "U13", "no boot", "buck converter dead")
    _write_report(tmp_path, "dev", "C5", "no display", "decoupling cap short")

    out = recall_field_reports(device_slug="dev", memory_root=tmp_path, query="buck")
    assert len(out) == 1
    assert out[0]["refdes"] == "U13"


def test_recall_field_reports_filters_by_refdes(tmp_path: Path) -> None:
    _write_report(tmp_path, "dev", "U13", "no boot", "buck dead")
    _write_report(tmp_path, "dev", "C5", "no display", "cap short")

    out = recall_field_reports(device_slug="dev", memory_root=tmp_path, refdes="C5")
    assert [r["refdes"] for r in out] == ["C5"]


def test_recall_field_reports_caps_to_limit(tmp_path: Path) -> None:
    for i in range(10):
        _write_report(tmp_path, "dev", f"R{i}", "no boot", f"cause {i}")
    out = recall_field_reports(device_slug="dev", memory_root=tmp_path, limit=3)
    assert len(out) == 3


def test_recall_field_reports_empty_when_none(tmp_path: Path) -> None:
    out = recall_field_reports(device_slug="dev", memory_root=tmp_path)
    assert out == []


# --- patterns (real seed_data) -----------------------------------------------


def test_search_patterns_matches_keyword() -> None:
    out = search_patterns("short")
    names = [p["name"] for p in out]
    assert any("short-to-gnd" in n for n in names), names
    # each hit carries readable content for the agent.
    assert all(p.get("content") for p in out)


def test_search_patterns_no_match_returns_empty() -> None:
    assert search_patterns("zzz-no-such-archetype-xyz") == []


# --- playbooks (real seed_data) ----------------------------------------------


def test_search_playbooks_matches_symptom() -> None:
    out = search_playbooks("no-power")
    ids = [p["playbook_id"] for p in out]
    assert "boot-no-power" in ids, ids
    pb = next(p for p in out if p["playbook_id"] == "boot-no-power")
    assert pb["steps"], "playbook must carry its steps for the agent to reuse"


def test_search_playbooks_no_match_returns_empty() -> None:
    assert search_playbooks("symptom-that-no-playbook-covers") == []


# --- dispatch wiring in runtime_direct ---------------------------------------


async def test_dispatch_recall_field_reports(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from api.agent.runtime_direct import _dispatch_mb_tool
    from api.session.state import SessionState

    _write_report(tmp_path, "dev", "U13", "no boot", "buck converter dead")
    res = await _dispatch_mb_tool(
        "mb_recall_field_reports", {"query": "buck"}, "dev", tmp_path,
        MagicMock(), SessionState(),
    )
    assert res["ok"] is True
    assert res["count"] == 1
    assert res["reports"][0]["refdes"] == "U13"


async def test_dispatch_search_patterns(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from api.agent.runtime_direct import _dispatch_mb_tool
    from api.session.state import SessionState

    res = await _dispatch_mb_tool(
        "mb_search_patterns", {"query": "short"}, "dev", tmp_path,
        MagicMock(), SessionState(),
    )
    assert res["ok"] is True
    assert res["count"] >= 1


async def test_dispatch_search_playbooks(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from api.agent.runtime_direct import _dispatch_mb_tool
    from api.session.state import SessionState

    res = await _dispatch_mb_tool(
        "mb_search_playbooks", {"symptom": "no-power"}, "dev", tmp_path,
        MagicMock(), SessionState(),
    )
    assert res["ok"] is True
    assert any(p["playbook_id"] == "boot-no-power" for p in res["playbooks"])
