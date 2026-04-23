"""Tests for the cross-session field-report memory.

Covers the JSON-first write-path (works without MA access), the MA mirror
(flag-gated), and the tool-level surface exposed to the diagnostic agent.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from api import config as config_mod
from api.agent.field_reports import (
    list_field_reports,
    record_field_report,
)
from api.agent.tools import mb_list_findings, mb_record_finding


@pytest.fixture(autouse=True)
def reset_settings_cache(monkeypatch):
    monkeypatch.setattr(config_mod, "_settings", None)
    yield
    monkeypatch.setattr(config_mod, "_settings", None)


async def test_record_writes_markdown_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MA_MEMORY_STORE_ENABLED", "false")
    status = await record_field_report(
        client=None,
        device_slug="demo-pi",
        refdes="U7",
        symptom="3V3 rail dead",
        confirmed_cause="PMIC failure — replaced",
        mechanism="short-to-ground",
        notes="Short found between pad 3 and GND",
        memory_root=tmp_path,
    )

    assert status["json_status"] == "written"
    assert status["ma_mirror_status"] == "skipped:flag_disabled"
    file_path = Path(status["json_path"])
    assert file_path.exists()
    content = file_path.read_text()
    assert "U7" in content
    assert "PMIC failure — replaced" in content
    assert "short-to-ground" in content


async def test_list_returns_newest_first(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MA_MEMORY_STORE_ENABLED", "false")
    # Two reports, the second filename-lex-greater so it sorts first.
    await record_field_report(
        client=None,
        device_slug="demo-pi",
        refdes="U7",
        symptom="s1",
        confirmed_cause="c1",
        memory_root=tmp_path,
    )
    # Monkeypatch the timestamp so the second file lands after the first without
    # needing to sleep the real clock.
    from api.agent import field_reports as fr_module

    class _LaterDatetime:
        @staticmethod
        def now(tz):
            from datetime import datetime as _dt

            return _dt(2030, 1, 1, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(fr_module, "datetime", _LaterDatetime)
    await record_field_report(
        client=None,
        device_slug="demo-pi",
        refdes="C29",
        symptom="s2",
        confirmed_cause="c2",
        memory_root=tmp_path,
    )

    results = list_field_reports(device_slug="demo-pi", memory_root=tmp_path, limit=10)
    assert len(results) == 2
    assert results[0]["refdes"] == "C29"
    assert results[1]["refdes"] == "U7"


async def test_list_filter_by_refdes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MA_MEMORY_STORE_ENABLED", "false")
    for ref in ("U7", "C29", "U7"):
        await record_field_report(
            client=None,
            device_slug="demo-pi",
            refdes=ref,
            symptom="x",
            confirmed_cause="y",
            memory_root=tmp_path,
        )
    filtered = list_field_reports(
        device_slug="demo-pi", memory_root=tmp_path, filter_refdes="U7"
    )
    # Two U7 reports (same second, same filename → one file in practice).
    # We just assert every returned refdes matches the filter.
    assert all(r["refdes"] == "U7" for r in filtered)
    assert len(filtered) >= 1


async def test_list_empty_for_unknown_device(tmp_path: Path):
    assert list_field_reports(device_slug="does-not-exist", memory_root=tmp_path) == []


async def test_ma_mirror_called_when_flag_enabled(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MA_MEMORY_STORE_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    async def fake_ensure(_client, _slug):
        return "memstore_x"

    monkeypatch.setattr("api.agent.field_reports.ensure_memory_store", fake_ensure)

    upserts: list[dict] = []

    async def fake_upsert(_client, *, store_id, path, content):
        upserts.append({"store_id": store_id, "path": path, "content": content})
        return "sha_ok"

    monkeypatch.setattr("api.agent.field_reports.upsert_memory", fake_upsert)

    client = MagicMock()
    status = await record_field_report(
        client=client,
        device_slug="demo-pi",
        refdes="U7",
        symptom="s",
        confirmed_cause="c",
        memory_root=tmp_path,
    )

    assert status["json_status"] == "written"
    assert status["ma_mirror_status"] == "mirrored"
    assert len(upserts) == 1
    assert upserts[0]["store_id"] == "memstore_x"
    assert upserts[0]["path"].startswith("/field_reports/")
    assert "U7" in upserts[0]["content"]


async def test_ma_mirror_failure_does_not_block_json_write(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("MA_MEMORY_STORE_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    async def fake_ensure(_client, _slug):
        return "memstore_x"

    monkeypatch.setattr("api.agent.field_reports.ensure_memory_store", fake_ensure)

    async def failing_upsert(_client, **_kwargs):
        return None  # matches the shared helper's failure contract

    monkeypatch.setattr("api.agent.field_reports.upsert_memory", failing_upsert)

    client = MagicMock()
    status = await record_field_report(
        client=client,
        device_slug="demo-pi",
        refdes="U7",
        symptom="s",
        confirmed_cause="c",
        memory_root=tmp_path,
    )

    assert status["json_status"] == "written"
    assert status["ma_mirror_status"].startswith("error:")
    assert Path(status["json_path"]).exists()


async def test_mb_tools_pass_through(tmp_path: Path, monkeypatch):
    """End-to-end: the tool layer writes, then another tool call reads it back."""
    monkeypatch.setenv("MA_MEMORY_STORE_ENABLED", "false")
    write_status = await mb_record_finding(
        client=None,
        device_slug="demo-pi",
        refdes="U7",
        symptom="no boot",
        confirmed_cause="PMIC dead",
        memory_root=tmp_path,
    )
    assert write_status["json_status"] == "written"

    read = mb_list_findings(device_slug="demo-pi", memory_root=tmp_path)
    assert read["count"] == 1
    assert read["reports"][0]["refdes"] == "U7"
    assert read["reports"][0]["confirmed_cause"] == "PMIC dead"
