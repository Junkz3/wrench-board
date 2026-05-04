#!/usr/bin/env python3
"""End-to-end agent flow benchmark — scripted repair session.

Plays a canonical MNT Reform U13 buck-dead scenario against a live
backend to verify:

  1. The WS wire protocol reaches session_ready within reasonable time.
  2. The agent picks up measurements.jsonl / diagnosis_log.jsonl writes.
  3. The agent, when asked to validate, calls `mb_validate_finding`
     (proves the Managed Agents toolbox is current after --refresh-tools).
  4. `outcome.json` lands on disk.
  5. `simulation.repair_validated` fires over the WS.

Usage:
    .venv/bin/python scripts/bench_agent_flow.py
    .venv/bin/python scripts/bench_agent_flow.py --tier normal --slug mnt-reform-motherboard

Requires the dev server on localhost:8000. Coûts un peu de tokens
Anthropic — le scénario fait ~4 user turns.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import websockets

REPO = Path(__file__).resolve().parents[1]

DEFAULT_SLUG = "mnt-reform-motherboard"
DEFAULT_TIER = "fast"
DEFAULT_HOST = "http://localhost:8000"

# 4-turn canonical scenario. Each turn is the tech's user message; the
# agent is expected to answer (and optionally call tools) in between.
SCENARIO: list[dict[str, Any]] = [
    {
        "stage": "describe",
        "text": (
            "Bonjour, MNT Reform qui ne boote plus. "
            "Aucun signe de vie, pas de LED, rien à l'écran. "
            "Qu'est-ce que je devrais mesurer en premier ?"
        ),
        "wait_turn_end_s": 45,
    },
    {
        "stage": "measure_dead_rail",
        "text": (
            "J'ai mesuré +1V2 sur le test point, je lis 0.02 V au multimètre. "
            "+5V et +3V3 sont bien là (5.01 V et 3.29 V). Enregistre cette "
            "mesure dans le journal et propose-moi une hypothèse."
        ),
        "wait_turn_end_s": 60,
    },
    {
        "stage": "post_fix_measure",
        "text": (
            "J'ai remplacé U13 par une pièce neuve. Après reflow, "
            "je remesure +1V2 et je lis 1.19 V stable. La carte boote. "
            "Enregistre la nouvelle mesure."
        ),
        "wait_turn_end_s": 45,
    },
]


@dataclass
class Report:
    tier: str
    slug: str
    repair_id: str
    session_ready_ms: float | None = None
    turn_events: list[dict[str, Any]] = field(default_factory=list)
    validation_sent_ts: float | None = None
    repair_validated_event: dict[str, Any] | None = None
    mb_validate_finding_called: bool = False
    outcome_file_found: bool = False
    diagnosis_log_entries: int = 0
    measurements_entries: int = 0
    tool_calls: list[str] = field(default_factory=list)
    memory_tool_calls: list[str] = field(default_factory=list)
    error: str | None = None
    total_cost_usd: float = 0.0
    total_turns: int = 0


def _color(s: str, code: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"\x1b[{code}m{s}\x1b[0m"


def GREEN(s: str) -> str: return _color(s, "32")
def RED(s: str) -> str: return _color(s, "31")
def YELLOW(s: str) -> str: return _color(s, "33")
def DIM(s: str) -> str: return _color(s, "2")


async def _create_repair(host: str, slug: str) -> str:
    """POST /pipeline/repairs with force_rebuild=False — re-use existing pack."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{host}/pipeline/repairs",
            json={
                "device_label": "MNT Reform motherboard (bench)",
                "device_slug": slug,
                "symptom": "bench_agent_flow — scripted U13 buck-dead scenario",
                "force_rebuild": False,
            },
        )
        resp.raise_for_status()
        return resp.json()["repair_id"]


def _cleanup_bench_repair(slug: str, repair_id: str) -> None:
    """Remove the bench's ephemeral repair dir + metadata so the home view
    doesn't accumulate `bench_agent_flow` entries every run. Called after
    the report is printed so any disk assertion is already done."""
    repairs_dir = REPO / "memory" / slug / "repairs"
    target_dir = repairs_dir / repair_id
    target_meta = repairs_dir / f"{repair_id}.json"
    import shutil
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    if target_meta.exists():
        try:
            target_meta.unlink()
        except OSError:
            pass


async def _play_turn(
    ws, user_text: str, report: Report, stage: str, timeout: float,
) -> None:
    """Send one user message, consume events until `turn_complete` fires.

    `turn_complete` is emitted by the managed runtime when the MA session
    goes idle with stop_reason != requires_action — i.e. all tool_use
    events have been resolved and the agent is waiting for the next user
    input. Using this explicit signal avoids the 400 "waiting on responses
    to events [...]" failure that happens when new user.message is sent
    while tool_uses are still pending.
    """
    report.turn_events.append({"stage": stage, "text": user_text, "events": []})
    cur_events = report.turn_events[-1]["events"]
    await ws.send(json.dumps({"type": "message", "text": user_text}))

    t0 = time.perf_counter()
    while True:
        if time.perf_counter() - t0 > timeout:
            raise TimeoutError(f"stage={stage!r}: no turn_complete after {timeout}s")
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        ev = json.loads(raw)
        cur_events.append(ev)
        t = ev.get("type")
        if t == "tool_use":
            name = ev.get("name", "?")
            report.tool_calls.append(name)
            # An agent may spontaneously call mb_validate_finding during a
            # turn (e.g. after "j'ai remplacé U13, ça marche") without
            # waiting for an explicit validation.start trigger. Capture it.
            if name == "mb_validate_finding":
                report.mb_validate_finding_called = True
        elif t == "memory_tool_use":
            report.memory_tool_calls.append(ev.get("name", "?"))
        elif t == "simulation.repair_validated":
            report.repair_validated_event = ev
        elif t == "error":
            report.error = f"{stage}: {ev.get('text', '?')}"
            return
        elif t == "turn_cost":
            report.total_cost_usd += float(ev.get("cost_usd", 0.0))
            report.total_turns += 1
        elif t == "turn_complete":
            return


async def _drain_validation(
    ws, report: Report, timeout: float = 60.0,
) -> None:
    """Consume events after sending validation.start until turn_complete."""
    t0 = time.perf_counter()
    while True:
        if time.perf_counter() - t0 > timeout:
            raise TimeoutError(f"validation: no turn_complete after {timeout}s")
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            print(RED(f"  ! drain recv failed: {type(exc).__name__}: {exc}"))
            report.error = f"drain: {type(exc).__name__}: {exc}"
            return
        ev = json.loads(raw)
        t = ev.get("type")
        summary = ev.get("name") or ev.get("text") or ev.get("role") or ""
        if isinstance(summary, str) and len(summary) > 60:
            summary = summary[:60] + "…"
        print(f"  {DIM('ev')} {t:<30} {summary}")
        if t == "tool_use":
            name = ev.get("name", "?")
            report.tool_calls.append(name)
            if name == "mb_validate_finding":
                report.mb_validate_finding_called = True
        elif t == "memory_tool_use":
            report.memory_tool_calls.append(ev.get("name", "?"))
        elif t == "simulation.repair_validated":
            report.repair_validated_event = ev
        elif t == "turn_cost":
            report.total_cost_usd += float(ev.get("cost_usd", 0.0))
            report.total_turns += 1
        elif t == "error":
            report.error = f"validation: {ev.get('text', '?')}"
            return
        elif t == "turn_complete":
            return


async def run(args) -> Report:
    memory_root = REPO / "memory"
    repair_id = await _create_repair(args.host, args.slug)
    print(GREEN(f"✓ repair created: {repair_id}"))

    report = Report(tier=args.tier, slug=args.slug, repair_id=repair_id)

    ws_url = (
        args.host.replace("http://", "ws://").replace("https://", "wss://")
        + f"/ws/diagnostic/{args.slug}?tier={args.tier}&repair={repair_id}"
    )

    t0 = time.perf_counter()
    async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
        # 1. Wait for session_ready.
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
            ev = json.loads(raw)
            if ev.get("type") == "session_ready":
                report.session_ready_ms = (time.perf_counter() - t0) * 1000
                print(GREEN(f"✓ session_ready in {report.session_ready_ms:.0f}ms — {ev.get('mode')} · {ev.get('model')}"))
                break
            if ev.get("type") == "history_replay_start":
                # Fresh repair — no history to replay; should be immediate.
                continue
            if ev.get("type") == "history_replay_end":
                continue

        # 2. Play the canonical scenario.
        for turn in SCENARIO:
            stage = turn["stage"]
            print(YELLOW(f"→ turn {stage}: {turn['text'][:80]}..."))
            try:
                await _play_turn(ws, turn["text"], report, stage, turn["wait_turn_end_s"])
            except TimeoutError as exc:
                report.error = str(exc)
                return report
            if report.error:
                return report

        # 3. Send validation.start — the whole point of this benchmark.
        print(YELLOW("→ sending validation.start"))
        report.validation_sent_ts = time.perf_counter()
        await ws.send(json.dumps({"type": "validation.start", "repair_id": repair_id}))
        await _drain_validation(ws, report)

    # 4. Inspect disk.
    outcome_path = memory_root / args.slug / "repairs" / repair_id / "outcome.json"
    report.outcome_file_found = outcome_path.exists()
    measurements_path = memory_root / args.slug / "repairs" / repair_id / "measurements.jsonl"
    if measurements_path.exists():
        report.measurements_entries = sum(
            1 for line in measurements_path.read_text().splitlines() if line.strip()
        )
    diag_log_path = memory_root / args.slug / "repairs" / repair_id / "diagnosis_log.jsonl"
    if diag_log_path.exists():
        report.diagnosis_log_entries = sum(
            1 for line in diag_log_path.read_text().splitlines() if line.strip()
        )

    return report


def _print_report(report: Report) -> int:
    """Pretty report + exit code. 0 = all green, 1 = failure."""
    print()
    print("━" * 72)
    print(f" Agent-flow benchmark — tier={report.tier} slug={report.slug}")
    print(f" repair_id={report.repair_id}")
    print("━" * 72)

    checks: list[tuple[bool, str, str]] = [
        (
            report.session_ready_ms is not None,
            "session_ready received",
            f"{report.session_ready_ms:.0f}ms" if report.session_ready_ms else "TIMEOUT",
        ),
        (
            report.error is None,
            "no error during scenario",
            report.error or "clean",
        ),
        (
            report.measurements_entries >= 1,
            "measurements.jsonl populated",
            f"{report.measurements_entries} entries",
        ),
        (
            report.diagnosis_log_entries >= 1,
            "diagnosis_log.jsonl populated",
            f"{report.diagnosis_log_entries} entries",
        ),
        (
            report.mb_validate_finding_called,
            "agent called mb_validate_finding",
            "YES" if report.mb_validate_finding_called else "NO — manifest out of date?",
        ),
        (
            report.outcome_file_found,
            "outcome.json written to disk",
            "YES" if report.outcome_file_found else "NO",
        ),
        (
            report.repair_validated_event is not None,
            "simulation.repair_validated fired",
            str(report.repair_validated_event) if report.repair_validated_event else "NOT RECEIVED",
        ),
    ]
    ok_count = sum(1 for ok, _, _ in checks if ok)
    for ok, label, detail in checks:
        icon = GREEN("✓") if ok else RED("✗")
        print(f" {icon}  {label:<40}  {DIM(detail)}")

    print()
    print(f" Custom tools ({len(report.tool_calls)}): {', '.join(report.tool_calls) or '—'}")
    print(f" Memory tools ({len(report.memory_tool_calls)}): {', '.join(report.memory_tool_calls) or '—'}")
    print(f" Agent turns: {report.total_turns}  ·  LLM cost: ${report.total_cost_usd:.4f}"
          + (f"  ·  +{len(report.memory_tool_calls)} memory ops (MA-billed)"
             if report.memory_tool_calls else ""))
    print("━" * 72)
    if ok_count == len(checks):
        print(GREEN(f" ALL GREEN ({ok_count}/{len(checks)})"))
        return 0
    print(RED(f" FAIL ({ok_count}/{len(checks)})"))
    return 1


async def main_async(args) -> int:
    repair_id: str | None = None
    try:
        report = await run(args)
        repair_id = report.repair_id
    except Exception as exc:  # noqa: BLE001
        print(RED(f"Fatal: {exc}"))
        return 2
    rc = _print_report(report)
    # Tidy the home view — the scripted flow is an ephemeral test fixture,
    # it shouldn't leave a stale "en cours" repair behind on every run.
    if repair_id and not args.keep_repair:
        _cleanup_bench_repair(args.slug, repair_id)
        print(DIM(f" · cleaned bench repair {repair_id}"))
    return rc


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--slug", default=DEFAULT_SLUG)
    p.add_argument("--tier", default=DEFAULT_TIER, choices=["fast", "normal", "deep"])
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument(
        "--keep-repair",
        action="store_true",
        help="Skip the post-run cleanup of the ephemeral repair dir "
             "(useful when you want to inspect outcome.json / measurements.jsonl "
             "after the bench finishes).",
    )
    args = p.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
