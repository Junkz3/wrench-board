"""Unit tests for `scripts/eval_all.py`.

All tests are fast (no `slow` marker), filesystem-only, no network. Sub-eval
subprocesses are stubbed via dataclass-based fake runners.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

CLI_PATH = Path(__file__).resolve().parents[2] / "scripts" / "eval_all.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("eval_all", CLI_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["eval_all"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


eval_all = _load_module()


# ---------------------------------------------------------------------------
# Fake runner used to short-circuit subprocess in orchestrator tests.
# ---------------------------------------------------------------------------


@dataclass
class FakeRunner:
    name: str
    score: float | None = 0.5
    ok: bool = True
    error: str | None = None
    duration_ms: int = 12

    def run(self) -> eval_all.EvalResult:
        return eval_all.EvalResult(
            name=self.name,
            ok=self.ok,
            score=self.score,
            duration_ms=self.duration_ms,
            timestamp="2026-04-28T00:00:00+00:00",
            raw_output=json.dumps({"score": self.score}) if self.ok else "",
            stderr="",
            error=self.error,
            payload={"score": self.score} if self.ok else {},
        )


def _run_with(args, monkeypatch, fakes_by_name: dict[str, FakeRunner]) -> int:
    """Patch the runner factories to return the supplied fakes by name."""

    def factory_for(name):
        def _build(**kwargs):
            return fakes_by_name[name]

        return _build

    monkeypatch.setattr(eval_all, "make_simulator_runner", factory_for(eval_all.RUNNER_SIMULATOR))
    monkeypatch.setattr(eval_all, "make_pipeline_runner", factory_for(eval_all.RUNNER_PIPELINE))
    monkeypatch.setattr(eval_all, "make_vision_runner", factory_for(eval_all.RUNNER_VISION))
    monkeypatch.setattr(eval_all, "make_agent_runner", factory_for(eval_all.RUNNER_AGENT))
    return eval_all.main(args)


# ---------------------------------------------------------------------------
# 1. Score parsing in the SubprocessRunner
# ---------------------------------------------------------------------------


def test_simulator_runner_parses_score(monkeypatch):
    """A subprocess that returns a clean JSON line should parse to EvalResult."""
    captured = {}

    class FakeProc:
        returncode = 0
        stdout = '{"score": 0.93, "self_mrr": 0.88, "cascade_recall": 1.0, "n_scenarios": 17}\n'
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(eval_all.subprocess, "run", fake_run)

    runner = eval_all.make_simulator_runner(slug="mnt-reform-motherboard")
    result = runner.run()

    assert result.ok is True
    assert result.score == pytest.approx(0.93)
    assert result.name == eval_all.RUNNER_SIMULATOR
    assert result.payload["self_mrr"] == pytest.approx(0.88)
    assert "eval_simulator" in " ".join(captured["cmd"])
    assert "--device" in captured["cmd"]
    assert "mnt-reform-motherboard" in captured["cmd"]


def test_score_parser_falls_back_to_last_line():
    """Stdout with log noise before a final JSON line still parses."""
    stdout = "[info] starting\n[info] running\n{\"score\": 0.42, \"n_scenarios\": 3}\n"
    score, payload, err = eval_all._parse_stdout_score(stdout)
    assert err is None
    assert score == pytest.approx(0.42)
    assert payload["n_scenarios"] == 3


def test_score_parser_regex_fallback():
    """Fully malformed but containing a `\"score\":` token still extracts."""
    stdout = 'Random prose with "score": 0.7 buried inside, no valid JSON.'
    score, _payload, err = eval_all._parse_stdout_score(stdout)
    assert err is None
    assert score == pytest.approx(0.7)


def test_score_parser_empty_stdout_errors():
    score, _payload, err = eval_all._parse_stdout_score("")
    assert score is None
    assert err == "empty stdout"


# ---------------------------------------------------------------------------
# 2. Default runner selection
# ---------------------------------------------------------------------------


def test_orchestrator_only_runs_simulator_by_default(tmp_path, monkeypatch, capsys):
    sim = FakeRunner(name=eval_all.RUNNER_SIMULATOR, score=0.91)
    pipe = FakeRunner(name=eval_all.RUNNER_PIPELINE, score=0.0, ok=False, error="should-not-run")
    vis = FakeRunner(name=eval_all.RUNNER_VISION, score=0.0, ok=False, error="should-not-run")
    agt = FakeRunner(name=eval_all.RUNNER_AGENT, score=0.0, ok=False, error="should-not-run")

    out = tmp_path / "report.json"
    rc = _run_with(
        ["--no-compare", "--output", str(out)],
        monkeypatch,
        {
            eval_all.RUNNER_SIMULATOR: sim,
            eval_all.RUNNER_PIPELINE: pipe,
            eval_all.RUNNER_VISION: vis,
            eval_all.RUNNER_AGENT: agt,
        },
    )
    assert rc == 0
    report = json.loads(out.read_text())
    names = [r["name"] for r in report["runners"]]
    assert names == [eval_all.RUNNER_SIMULATOR]
    assert report["runners"][0]["score"] == pytest.approx(0.91)


# ---------------------------------------------------------------------------
# 3. --include-all wires up four runners
# ---------------------------------------------------------------------------


def test_include_all_runs_four_runners(tmp_path, monkeypatch):
    sim = FakeRunner(name=eval_all.RUNNER_SIMULATOR, score=0.9)
    pipe = FakeRunner(name=eval_all.RUNNER_PIPELINE, score=0.7)
    vis = FakeRunner(name=eval_all.RUNNER_VISION, score=0.6)
    agt = FakeRunner(name=eval_all.RUNNER_AGENT, score=0.8)

    out = tmp_path / "report.json"
    rc = _run_with(
        ["--no-compare", "--include-all", "--output", str(out)],
        monkeypatch,
        {
            eval_all.RUNNER_SIMULATOR: sim,
            eval_all.RUNNER_PIPELINE: pipe,
            eval_all.RUNNER_VISION: vis,
            eval_all.RUNNER_AGENT: agt,
        },
    )
    assert rc == 0
    report = json.loads(out.read_text())
    names = sorted(r["name"] for r in report["runners"])
    assert names == sorted(
        [
            eval_all.RUNNER_SIMULATOR,
            eval_all.RUNNER_PIPELINE,
            eval_all.RUNNER_VISION,
            eval_all.RUNNER_AGENT,
        ]
    )


# ---------------------------------------------------------------------------
# 4. Regression detected when score drops past threshold
# ---------------------------------------------------------------------------


def _write_previous(out_dir: Path, name: str, score: float) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    prev = out_dir / "20260101T000000Z.json"
    prev.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "runners": [
                    {
                        "name": name,
                        "ok": True,
                        "score": score,
                        "duration_ms": 10,
                        "timestamp": "2026-01-01T00:00:00+00:00",
                        "raw_output": "",
                        "stderr": "",
                        "error": None,
                        "payload": {},
                    }
                ],
                "regressions": [],
            }
        )
    )
    return prev


def test_regression_detected_when_score_drops(tmp_path, monkeypatch):
    out_dir = tmp_path / "eval_runs"
    _write_previous(out_dir, eval_all.RUNNER_SIMULATOR, 0.95)
    out = out_dir / "current.json"

    sim = FakeRunner(name=eval_all.RUNNER_SIMULATOR, score=0.80)
    rc = _run_with(
        ["--regression-threshold", "0.01", "--output", str(out)],
        monkeypatch,
        {
            eval_all.RUNNER_SIMULATOR: sim,
            eval_all.RUNNER_PIPELINE: FakeRunner(name=eval_all.RUNNER_PIPELINE),
            eval_all.RUNNER_VISION: FakeRunner(name=eval_all.RUNNER_VISION),
            eval_all.RUNNER_AGENT: FakeRunner(name=eval_all.RUNNER_AGENT),
        },
    )
    assert rc == 1
    report = json.loads(out.read_text())
    assert len(report["regressions"]) == 1
    reg = report["regressions"][0]
    assert reg["name"] == eval_all.RUNNER_SIMULATOR
    assert reg["score_now"] == pytest.approx(0.80)
    assert reg["score_prev"] == pytest.approx(0.95)
    assert reg["delta"] == pytest.approx(-0.15)


# ---------------------------------------------------------------------------
# 5. No regression within threshold
# ---------------------------------------------------------------------------


def test_no_regression_within_threshold(tmp_path, monkeypatch):
    out_dir = tmp_path / "eval_runs"
    _write_previous(out_dir, eval_all.RUNNER_SIMULATOR, 0.95)
    out = out_dir / "current.json"

    sim = FakeRunner(name=eval_all.RUNNER_SIMULATOR, score=0.949)
    rc = _run_with(
        ["--regression-threshold", "0.01", "--output", str(out)],
        monkeypatch,
        {
            eval_all.RUNNER_SIMULATOR: sim,
            eval_all.RUNNER_PIPELINE: FakeRunner(name=eval_all.RUNNER_PIPELINE),
            eval_all.RUNNER_VISION: FakeRunner(name=eval_all.RUNNER_VISION),
            eval_all.RUNNER_AGENT: FakeRunner(name=eval_all.RUNNER_AGENT),
        },
    )
    assert rc == 0
    report = json.loads(out.read_text())
    assert report["regressions"] == []


# ---------------------------------------------------------------------------
# 6. Subprocess failure / non-zero exit yields exit code 2
# ---------------------------------------------------------------------------


def test_subprocess_failure_yields_exit_2(tmp_path, monkeypatch):
    out = tmp_path / "report.json"
    sim = FakeRunner(
        name=eval_all.RUNNER_SIMULATOR,
        score=None,
        ok=False,
        error="non-zero exit 1",
    )
    rc = _run_with(
        ["--no-compare", "--output", str(out)],
        monkeypatch,
        {
            eval_all.RUNNER_SIMULATOR: sim,
            eval_all.RUNNER_PIPELINE: FakeRunner(name=eval_all.RUNNER_PIPELINE),
            eval_all.RUNNER_VISION: FakeRunner(name=eval_all.RUNNER_VISION),
            eval_all.RUNNER_AGENT: FakeRunner(name=eval_all.RUNNER_AGENT),
        },
    )
    assert rc == 2
    report = json.loads(out.read_text())
    assert report["runners"][0]["ok"] is False
    assert report["runners"][0]["error"] == "non-zero exit 1"


def test_subprocess_runner_captures_nonzero_exit(monkeypatch):
    """Direct test of _SubprocessRunner — non-zero return -> ok=False."""

    class FakeProc:
        returncode = 1
        stdout = "{}"
        stderr = "boom"

    monkeypatch.setattr(eval_all.subprocess, "run", lambda *a, **kw: FakeProc())

    runner = eval_all._SubprocessRunner(
        name=eval_all.RUNNER_SIMULATOR,
        cmd=["python", "-m", "scripts.eval_simulator"],
        timeout=10,
    )
    result = runner.run()
    assert result.ok is False
    assert result.error == "non-zero exit 1"
    assert "boom" in result.stderr


# ---------------------------------------------------------------------------
# 7. Output JSON is parsable & contains expected fields
# ---------------------------------------------------------------------------


def test_output_json_is_parsable(tmp_path, monkeypatch):
    out = tmp_path / "report.json"
    sim = FakeRunner(name=eval_all.RUNNER_SIMULATOR, score=0.91)
    rc = _run_with(
        ["--no-compare", "--output", str(out)],
        monkeypatch,
        {
            eval_all.RUNNER_SIMULATOR: sim,
            eval_all.RUNNER_PIPELINE: FakeRunner(name=eval_all.RUNNER_PIPELINE),
            eval_all.RUNNER_VISION: FakeRunner(name=eval_all.RUNNER_VISION),
            eval_all.RUNNER_AGENT: FakeRunner(name=eval_all.RUNNER_AGENT),
        },
    )
    assert rc == 0
    report = json.loads(out.read_text())

    # Schema fields
    for field in (
        "schema_version",
        "timestamp",
        "host",
        "platform",
        "python",
        "regression_threshold",
        "previous_report",
        "runners",
        "regressions",
    ):
        assert field in report, f"missing field: {field}"
    assert report["schema_version"] == eval_all.SCHEMA_VERSION
    assert isinstance(report["runners"], list)
    assert report["runners"][0]["name"] == eval_all.RUNNER_SIMULATOR
    assert report["runners"][0]["score"] == pytest.approx(0.91)
    assert report["runners"][0]["ok"] is True


# ---------------------------------------------------------------------------
# 8. find_previous_report ignores the file we are about to write
# ---------------------------------------------------------------------------


def test_find_previous_report_excludes_current(tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text("{}")
    b.write_text("{}")
    # Touch b last so it is newest.
    import os

    os.utime(a, (1000, 1000))
    os.utime(b, (2000, 2000))

    prev = eval_all.find_previous_report(tmp_path, exclude=b)
    assert prev == a

    prev_no_exclude = eval_all.find_previous_report(tmp_path)
    assert prev_no_exclude == b


# ---------------------------------------------------------------------------
# 9. select_runners honors flags
# ---------------------------------------------------------------------------


def test_select_runners_pipeline_only():
    runners = eval_all.select_runners(
        include_pipeline=True,
        include_vision=False,
        include_agent=False,
        slug=None,
    )
    names = [r.name for r in runners]
    assert names == [eval_all.RUNNER_SIMULATOR, eval_all.RUNNER_PIPELINE]
