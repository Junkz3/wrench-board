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
        json.dumps(
            {
                "device_slug": "mnt-reform-motherboard",
                "score": 0.78,
                "self_mrr": 0.82,
                "cascade_recall": 0.72,
                "n_scenarios": 17,
                "generated_at": "2026-04-24T21:00:00Z",
                "source_run_date": "2026-04-24",
                "notes": [],
            }
        ),
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
