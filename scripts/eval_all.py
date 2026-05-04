"""Composite eval orchestrator — runs the 4 microsolder evals, consolidates
scores, compares against the previous run, and flags regressions.

The 4 underlying evals (in scripts/eval_*.py) all emit a one-line JSON on
stdout containing a "score" float. This orchestrator invokes each as a
subprocess, captures stdout/stderr, parses the score, writes a single
report under benchmark/eval_runs/{iso_ts}.json, and compares to the
previous report's per-runner scores.

By default ONLY the simulator eval is run (free, deterministic, ~1s).
The other three (pipeline, vision, agent) are real-API and cost money;
they are opt-in via --include-* flags.

Usage:
    .venv/bin/python scripts/eval_all.py
    .venv/bin/python scripts/eval_all.py --include-pipeline
    .venv/bin/python scripts/eval_all.py --include-all --slug iphone-x

Exit codes:
    0  — all enabled runners ran OK, no regression > threshold
    1  — at least one regression > threshold detected
    2  — at least one runner crashed / unparseable / returned non-zero
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = REPO_ROOT / "benchmark" / "eval_runs"
SCHEMA_VERSION = "1.0"

# Runner names — kept stable; the regression engine matches by these.
RUNNER_SIMULATOR = "simulator"
RUNNER_PIPELINE = "pipeline"
RUNNER_VISION = "vision"
RUNNER_AGENT = "agent"

# Default timeouts (seconds). Simulator is deterministic and fast; the others
# hit the network and are bounded by sub-script-level timeouts already, but we
# still cap to avoid runaway hangs.
DEFAULT_TIMEOUTS = {
    RUNNER_SIMULATOR: 120,
    RUNNER_PIPELINE: 600,
    RUNNER_VISION: 600,
    RUNNER_AGENT: 1800,
}

# Regex fallback when stdout isn't strict JSON (e.g. extra log lines).
_SCORE_RE = re.compile(r'"score"\s*:\s*([0-9.eE+-]+)')


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class EvalResult:
    """Outcome of running a single sub-eval.

    `score` is None when ok=False (crash, parse failure, missing field).
    `raw_output` keeps stdout for forensic inspection; trimmed to keep the
    JSON report small.
    """

    name: str
    ok: bool
    score: float | None
    duration_ms: int
    timestamp: str
    raw_output: str = ""
    stderr: str = ""
    error: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Regression:
    name: str
    score_now: float
    score_prev: float
    delta: float


# ---------------------------------------------------------------------------
# Runner protocol + implementations
# ---------------------------------------------------------------------------


class EvalRunner(Protocol):
    name: str

    def run(self) -> EvalResult: ...  # pragma: no cover


def _parse_stdout_score(stdout: str) -> tuple[float | None, dict[str, Any], str | None]:
    """Try strict JSON parse first, then regex on the last non-empty line,
    then a regex over the whole output. Returns (score, payload, error)."""
    text = stdout.strip()
    if not text:
        return None, {}, "empty stdout"

    # Strict path: full text is JSON.
    try:
        payload = json.loads(text)
        if isinstance(payload, dict) and "score" in payload:
            score = payload["score"]
            if isinstance(score, (int, float)):
                return float(score), payload, None
            return None, payload, f"score not numeric: {score!r}"
    except json.JSONDecodeError:
        pass

    # Last-line JSON path (some scripts may emit log lines before the JSON).
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "score" in payload:
            score = payload["score"]
            if isinstance(score, (int, float)):
                return float(score), payload, None
        break

    # Regex fallback (no full JSON anywhere).
    m = _SCORE_RE.search(text)
    if m:
        try:
            return float(m.group(1)), {}, None
        except ValueError:
            return None, {}, "score regex matched but not parseable as float"

    return None, {}, "no score field found in stdout"


def _truncate(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit - 200]
    tail = text[-200:]
    return f"{head}\n...[truncated {len(text) - limit} chars]...\n{tail}"


@dataclass
class _SubprocessRunner:
    """Common impl: run a python module as subprocess, parse stdout for score."""

    name: str
    cmd: list[str]
    timeout: int

    def run(self) -> EvalResult:
        started = time.monotonic()
        ts = datetime.now(timezone.utc).isoformat()
        try:
            proc = subprocess.run(
                self.cmd,
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            return EvalResult(
                name=self.name,
                ok=False,
                score=None,
                duration_ms=duration_ms,
                timestamp=ts,
                raw_output=_truncate(exc.stdout or ""),
                stderr=_truncate(exc.stderr or ""),
                error=f"timeout after {self.timeout}s",
            )
        except FileNotFoundError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            return EvalResult(
                name=self.name,
                ok=False,
                score=None,
                duration_ms=duration_ms,
                timestamp=ts,
                error=f"runner binary missing: {exc}",
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""

        if proc.returncode != 0:
            return EvalResult(
                name=self.name,
                ok=False,
                score=None,
                duration_ms=duration_ms,
                timestamp=ts,
                raw_output=_truncate(stdout),
                stderr=_truncate(stderr),
                error=f"non-zero exit {proc.returncode}",
            )

        score, payload, err = _parse_stdout_score(stdout)
        if score is None:
            return EvalResult(
                name=self.name,
                ok=False,
                score=None,
                duration_ms=duration_ms,
                timestamp=ts,
                raw_output=_truncate(stdout),
                stderr=_truncate(stderr),
                error=err or "score parse failure",
                payload=payload,
            )

        return EvalResult(
            name=self.name,
            ok=True,
            score=score,
            duration_ms=duration_ms,
            timestamp=ts,
            raw_output=_truncate(stdout),
            stderr=_truncate(stderr),
            payload=payload,
        )


def make_simulator_runner(
    *, slug: str | None = None, timeout: int | None = None, python: str | None = None
) -> EvalRunner:
    py = python or sys.executable
    cmd = [py, "-m", "scripts.eval_simulator"]
    if slug:
        cmd += ["--device", slug]
    return _SubprocessRunner(
        name=RUNNER_SIMULATOR,
        cmd=cmd,
        timeout=timeout or DEFAULT_TIMEOUTS[RUNNER_SIMULATOR],
    )


def make_pipeline_runner(
    *, slug: str | None = None, timeout: int | None = None, python: str | None = None
) -> EvalRunner:
    py = python or sys.executable
    cmd = [py, "-m", "scripts.eval_pipeline"]
    if slug:
        cmd += ["--devices", slug]
    return _SubprocessRunner(
        name=RUNNER_PIPELINE,
        cmd=cmd,
        timeout=timeout or DEFAULT_TIMEOUTS[RUNNER_PIPELINE],
    )


def make_vision_runner(
    *, slug: str | None = None, timeout: int | None = None, python: str | None = None
) -> EvalRunner:
    py = python or sys.executable
    cmd = [py, "-m", "scripts.eval_pipeline_vision"]
    if slug:
        # vision script wants slug:N,M tokens; without explicit pages we let
        # the script keep its built-in defaults but restrict to this slug.
        # If the user passed a bare slug we just drop it (vision needs pages).
        # Fail-soft: don't pass --pages and let the script use defaults.
        pass
    return _SubprocessRunner(
        name=RUNNER_VISION,
        cmd=cmd,
        timeout=timeout or DEFAULT_TIMEOUTS[RUNNER_VISION],
    )


def make_agent_runner(
    *,
    slug: str | None = None,
    timeout: int | None = None,
    python: str | None = None,
    tier: str = "normal",
) -> EvalRunner:
    py = python or sys.executable
    cmd = [py, "-m", "scripts.eval_diagnostic_agent", "--tier", tier]
    # eval_diagnostic_agent does not take a --slug flag (scenarios scope it).
    # Slug filtering is therefore not enforced here; the parameter is
    # accepted for API symmetry only.
    _ = slug
    return _SubprocessRunner(
        name=RUNNER_AGENT,
        cmd=cmd,
        timeout=timeout or DEFAULT_TIMEOUTS[RUNNER_AGENT],
    )


# ---------------------------------------------------------------------------
# Report consolidation + regression detection
# ---------------------------------------------------------------------------


def find_previous_report(output_dir: Path, exclude: Path | None = None) -> Path | None:
    """Locate the most recent report in `output_dir`, excluding `exclude`."""
    if not output_dir.exists():
        return None
    candidates = sorted(
        (p for p in output_dir.glob("*.json") if p != exclude),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def compute_regressions(
    current: list[EvalResult],
    previous_report: dict | None,
    threshold: float,
) -> list[Regression]:
    """Return regressions: runner present in both reports, score dropped by
    more than `threshold` (absolute, in score units). Crashed runners are
    skipped from regression detection — they surface via exit code 2 already.
    """
    if not previous_report:
        return []
    prev_scores: dict[str, float] = {}
    for r in previous_report.get("runners", []):
        if r.get("ok") and isinstance(r.get("score"), (int, float)):
            prev_scores[r["name"]] = float(r["score"])

    regressions: list[Regression] = []
    for r in current:
        if not r.ok or r.score is None:
            continue
        prev = prev_scores.get(r.name)
        if prev is None:
            continue
        delta = r.score - prev
        if delta < -threshold:
            regressions.append(
                Regression(
                    name=r.name,
                    score_now=r.score,
                    score_prev=prev,
                    delta=delta,
                )
            )
    return regressions


def build_report(
    *,
    runners: list[EvalResult],
    regressions: list[Regression],
    threshold: float,
    previous_path: Path | None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "regression_threshold": threshold,
        "previous_report": str(previous_path) if previous_path else None,
        "runners": [asdict(r) for r in runners],
        "regressions": [asdict(r) for r in regressions],
    }


# ---------------------------------------------------------------------------
# Console formatting
# ---------------------------------------------------------------------------


def _row(name: str, status: str, score: str, dur: str, note: str = "") -> str:
    return f"  {status} {name:<11} score={score:<10} duration={dur:<8}  {note}"


def format_console(
    runners: list[EvalResult],
    regressions: list[Regression],
    output_path: Path,
    previous_path: Path | None,
) -> str:
    lines: list[str] = []
    lines.append("eval_all — composite microsolder eval suite")
    lines.append(f"  output: {output_path}")
    if previous_path:
        lines.append(f"  previous: {previous_path}")
    else:
        lines.append("  previous: (none — baseline run)")
    lines.append("")

    reg_by_name = {r.name: r for r in regressions}
    for r in runners:
        if not r.ok:
            status = "[X]"
            score = "ERR"
            note = r.error or ""
        elif r.name in reg_by_name:
            reg = reg_by_name[r.name]
            status = "[!]"
            score = f"{r.score:.4f}"
            note = f"REGRESSION delta={reg.delta:+.4f} (was {reg.score_prev:.4f})"
        else:
            status = "[OK]"
            score = f"{r.score:.4f}" if r.score is not None else "-"
            note = ""
        dur = f"{r.duration_ms / 1000:.1f}s"
        lines.append(_row(r.name, status, score, dur, note))

    lines.append("")
    if regressions:
        lines.append(f"REGRESSIONS DETECTED ({len(regressions)}):")
        for reg in regressions:
            lines.append(
                f"  - {reg.name}: {reg.score_prev:.4f} -> {reg.score_now:.4f} "
                f"(delta {reg.delta:+.4f})"
            )
    else:
        lines.append("No regressions detected.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def select_runners(
    *,
    include_pipeline: bool,
    include_vision: bool,
    include_agent: bool,
    slug: str | None,
    factories: dict[str, Any] | None = None,
) -> list[EvalRunner]:
    """Build the ordered list of runners to invoke.

    Simulator always runs (free, deterministic). Others gated by flags.
    `factories` is a hook for tests to inject mock builders.
    """
    f = factories or {}
    sim_factory = f.get(RUNNER_SIMULATOR, make_simulator_runner)
    pipe_factory = f.get(RUNNER_PIPELINE, make_pipeline_runner)
    vision_factory = f.get(RUNNER_VISION, make_vision_runner)
    agent_factory = f.get(RUNNER_AGENT, make_agent_runner)

    runners: list[EvalRunner] = [sim_factory(slug=slug)]
    if include_pipeline:
        runners.append(pipe_factory(slug=slug))
    if include_vision:
        runners.append(vision_factory(slug=slug))
    if include_agent:
        runners.append(agent_factory(slug=slug))
    return runners


def run_orchestrator(args: argparse.Namespace) -> int:
    output_dir: Path = Path(args.output).parent if args.output else DEFAULT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.output:
        output_path = Path(args.output)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_path = output_dir / f"{ts}.json"

    include_all = bool(args.include_all)
    runners = select_runners(
        include_pipeline=include_all or bool(args.include_pipeline),
        include_vision=include_all or bool(args.include_vision),
        include_agent=include_all or bool(args.include_agent),
        slug=args.slug,
    )

    print(f"eval_all: running {len(runners)} runner(s)...", file=sys.stderr)
    results: list[EvalResult] = []
    for runner in runners:
        print(f"  -> {runner.name} ...", file=sys.stderr, flush=True)
        result = runner.run()
        status = "ok" if result.ok else f"FAIL ({result.error})"
        score_str = f"{result.score:.4f}" if result.ok and result.score is not None else "-"
        print(
            f"     {status}  score={score_str}  ({result.duration_ms / 1000:.1f}s)",
            file=sys.stderr,
            flush=True,
        )
        results.append(result)

    previous_path: Path | None = None
    if not args.no_compare:
        previous_path = find_previous_report(output_dir, exclude=output_path)

    previous_report: dict | None = None
    if previous_path is not None:
        try:
            previous_report = json.loads(previous_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"eval_all: warning — previous report unreadable ({exc}); skipping compare",
                file=sys.stderr,
            )
            previous_report = None
            previous_path = None

    regressions = compute_regressions(results, previous_report, args.regression_threshold)

    report = build_report(
        runners=results,
        regressions=regressions,
        threshold=args.regression_threshold,
        previous_path=previous_path,
    )
    output_path.write_text(json.dumps(report, indent=2) + "\n")

    print("", file=sys.stderr)
    print(format_console(results, regressions, output_path, previous_path))

    if any(not r.ok for r in results):
        return 2
    if regressions:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eval_all",
        description=(
            "Composite microsolder eval orchestrator. Runs eval_simulator by "
            "default; eval_pipeline, eval_pipeline_vision, eval_diagnostic_agent "
            "are opt-in (real-API, costs money)."
        ),
    )
    p.add_argument(
        "--include-pipeline",
        action="store_true",
        help="Run scripts/eval_pipeline.py (real API, ~$1-5/run).",
    )
    p.add_argument(
        "--include-vision",
        action="store_true",
        help="Run scripts/eval_pipeline_vision.py (real API, Opus 4.7 per page).",
    )
    p.add_argument(
        "--include-agent",
        action="store_true",
        help="Run scripts/eval_diagnostic_agent.py (real API, requires dev server).",
    )
    p.add_argument(
        "--include-all",
        action="store_true",
        help="Shortcut for --include-pipeline --include-vision --include-agent.",
    )
    p.add_argument(
        "--slug",
        default=None,
        help="Restrict slug-aware sub-evals to this device_slug.",
    )
    p.add_argument(
        "--regression-threshold",
        type=float,
        default=0.01,
        help="Absolute score drop that flags a regression (default: 0.01).",
    )
    p.add_argument(
        "--output",
        default=None,
        help=(
            "Override the report path (default: "
            "benchmark/eval_runs/{iso_timestamp}.json)."
        ),
    )
    p.add_argument(
        "--no-compare",
        action="store_true",
        help="Skip comparison against the previous run.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run_orchestrator(args)


if __name__ == "__main__":
    sys.exit(main())
