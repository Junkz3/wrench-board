# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from pathlib import Path

from api.pipeline.bench_generator.schemas import (
    Cause,
    ProposedScenario,
    Rejection,
    ReliabilityCard,
    RunManifest,
)
from api.pipeline.bench_generator.writer import (
    update_latest_json,
    write_per_run_files,
    write_reliability_card,
    write_source_archives,
)
from api.pipeline.schematic.evaluator import ScenarioResult, Scorecard


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
        score=0.7,
        self_mrr=0.8,
        cascade_recall=0.55,
        n_scenarios=2,
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


def test_update_latest_merges_new_slug(tmp_path: Path):
    latest = tmp_path / "_latest.json"
    latest.write_text(
        json.dumps(
            {
                "other-board": {
                    "score": 0.5,
                    "self_mrr": 0.5,
                    "cascade_recall": 0.5,
                    "n_scenarios": 3,
                    "run_date": "2026-04-23",
                },
            }
        ),
        encoding="utf-8",
    )
    update_latest_json(
        latest_path=latest,
        slug="toy-board",
        scorecard=_scorecard(),
        run_date="2026-04-24",
    )
    d = json.loads(latest.read_text())
    assert "toy-board" in d
    assert "other-board" in d
    assert d["toy-board"]["score"] == 0.7


def test_update_latest_creates_fresh_file(tmp_path: Path):
    latest = tmp_path / "_latest.json"
    update_latest_json(
        latest_path=latest,
        slug="toy-board",
        scorecard=_scorecard(),
        run_date="2026-04-24",
    )
    d = json.loads(latest.read_text())
    assert list(d.keys()) == ["toy-board"]


def test_write_source_archives_one_file_per_scenario(tmp_path: Path):
    archive_dir = tmp_path / "sources"
    accepted = [_scenario(1), _scenario(2)]
    write_source_archives(archive_dir=archive_dir, scenarios=accepted)
    assert (archive_dir / "toy-s1.txt").exists()
    assert (archive_dir / "toy-s2.txt").exists()
    assert (archive_dir / "toy-s1.txt").read_text().startswith(accepted[0].source_url)


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
