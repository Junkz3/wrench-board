from pathlib import Path
import json


def test_phase_token_stats_accumulates():
    from api.pipeline.telemetry.token_stats import PhaseTokenStats
    stats = PhaseTokenStats(phase="writers")
    stats.record(input_tokens=1000, output_tokens=500, cache_read=0, cache_write=800, duration_s=1.2)
    stats.record(input_tokens=900, output_tokens=300, cache_read=800, cache_write=0, duration_s=0.8)
    assert stats.input_tokens == 1900
    assert stats.output_tokens == 800
    assert stats.cache_read_input_tokens == 800
    assert stats.cache_creation_input_tokens == 800
    assert round(stats.duration_s, 2) == 2.0


def test_write_and_read_token_stats(tmp_path: Path):
    from api.pipeline.telemetry.token_stats import (
        PhaseTokenStats, write_token_stats, read_token_stats,
    )
    stats = [
        PhaseTokenStats(phase="scout", input_tokens=500, output_tokens=4000),
        PhaseTokenStats(phase="auditor", input_tokens=12000, output_tokens=1500, cache_read_input_tokens=10000),
    ]
    path = tmp_path / "token_stats.json"
    write_token_stats(path, stats)
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["phases"][1]["cache_read_input_tokens"] == 10000

    loaded = read_token_stats(path)
    assert len(loaded) == 2
    assert loaded[0].phase == "scout"


def test_render_table_has_header_row():
    from api.pipeline.telemetry.token_stats import PhaseTokenStats, render_table
    out = render_table([PhaseTokenStats(phase="scout", input_tokens=100, output_tokens=50)])
    lines = out.splitlines()
    assert lines[0].startswith("phase")
    assert "scout" in lines[2]
