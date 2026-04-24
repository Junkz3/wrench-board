"""Real-API smoke-test for the 22-commit optimization sweep.

This script makes real Anthropic API calls.
Expected spend: ~$0.20-0.50 depending on flags.
Run with ``--dry-run`` first to see the plan.

Usage
-----
# Preview plan, no API calls:
    python scripts/smoke_test_optimizations.py --dry-run

# Run only the cheapest cache test (~$0.05):
    python scripts/smoke_test_optimizations.py --phase cache

# Run the full pipeline test on a custom slug (~$0.30):
    python scripts/smoke_test_optimizations.py --phase pipeline --slug my-device

# Run the session phase against a running server (~$0.10):
    python scripts/smoke_test_optimizations.py --phase session --slug mnt-reform-motherboard

# Run all three phases (~$0.45):
    python scripts/smoke_test_optimizations.py --phase all

Note: The server must be running on localhost:8000 for --phase session.
Start it in another terminal: make run > /tmp/microsolder.log 2>&1
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Bootstrap: ensure project root is importable
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Load .env before importing project modules (they read env at import time)
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv

    load_dotenv(_REPO_ROOT / ".env", override=False)
except ImportError:
    print("[warn] python-dotenv not installed; relying on OS environment")


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class PhaseResult:
    phase: str
    passed: bool
    detail: str
    cost_estimate_usd: float
    assertions: list[tuple[bool, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

PASS = "✓"
FAIL = "✗"


def _check(ok: bool, label: str) -> tuple[bool, str]:
    marker = PASS if ok else FAIL
    print(f"  {marker} {label}")
    return ok, label


def _header(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Phase 1 — Auditor cache
# ---------------------------------------------------------------------------


async def phase_cache() -> PhaseResult:
    """Call run_auditor twice on a minimal fixture; assert cache_read on 2nd call."""
    _header("Phase 1: Auditor cache (P1 + P2)")
    print("  Calling run_auditor x2 on minimal fixture (~2 KB input)...")

    from anthropic import AsyncAnthropic

    from api.config import get_settings
    from api.pipeline.auditor import run_auditor
    from api.pipeline.schemas import (
        DeviceTaxonomy,
        Dictionary,
        KnowledgeGraph,
        Registry,
        RegistryComponent,
        RegistrySignal,
        RulesSet,
    )
    from api.pipeline.telemetry.token_stats import PhaseTokenStats

    # Minimal but plausible fixture — keep it small to limit cost.
    reg = Registry(
        device_label="Smoke Test Pi Zero",
        taxonomy=DeviceTaxonomy(
            brand="Test",
            model="Pi",
            version="zero-1",
            form_factor="mainboard",
        ),
        components=[
            RegistryComponent(
                canonical_name="U1",
                kind="ic",
                aliases=[],
                description="Main SoC",
            ),
            RegistryComponent(
                canonical_name="C1",
                kind="capacitor",
                aliases=[],
                description="Decoupling cap on VCC3V3",
            ),
        ],
        signals=[
            RegistrySignal(
                canonical_name="VCC3V3",
                aliases=["3V3_RAIL"],
                kind="power_rail",
                nominal_voltage=3.3,
            )
        ],
    )
    kg = KnowledgeGraph(nodes=[], edges=[])
    rules = RulesSet(rules=[])
    dct = Dictionary(entries=[])
    drift: list = []

    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    model = settings.anthropic_model_main

    stats1 = PhaseTokenStats(phase="auditor_smoke_1")
    stats2 = PhaseTokenStats(phase="auditor_smoke_2")

    common = dict(
        client=client,
        model=model,
        device_label="Smoke Test Pi Zero",
        registry=reg,
        knowledge_graph=kg,
        rules=rules,
        dictionary=dct,
        precomputed_drift=drift,
    )

    print("  [1/2] First auditor call — populates cache...")
    t0 = time.monotonic()
    await run_auditor(**common, stats=stats1)
    dur1 = time.monotonic() - t0
    print(f"        done in {dur1:.1f}s")

    print("  [2/2] Second auditor call — should hit cache...")
    t0 = time.monotonic()
    await run_auditor(**common, stats=stats2)
    dur2 = time.monotonic() - t0
    print(f"        done in {dur2:.1f}s")

    assertions: list[tuple[bool, str]] = []

    assertions.append(_check(
        stats1.cache_creation_input_tokens > 0,
        f"1st call wrote to cache: cache_write={stats1.cache_creation_input_tokens}",
    ))
    assertions.append(_check(
        stats2.cache_read_input_tokens > 0,
        f"2nd call read from cache: cache_read={stats2.cache_read_input_tokens} "
        f"(MUST be > 0 to prove P1+P2)",
    ))
    assertions.append(_check(
        stats2.input_tokens < stats1.input_tokens + stats1.cache_creation_input_tokens,
        f"2nd call billed fewer non-cached tokens: "
        f"1st_total={stats1.input_tokens + stats1.cache_creation_input_tokens} "
        f"2nd_non_cached={stats2.input_tokens}",
    ))

    detail = (
        f"1st call: cache_read={stats1.cache_read_input_tokens}, "
        f"cache_write={stats1.cache_creation_input_tokens}, "
        f"input={stats1.input_tokens}, output={stats1.output_tokens}\n"
        f"2nd call: cache_read={stats2.cache_read_input_tokens}, "
        f"cache_write={stats2.cache_creation_input_tokens}, "
        f"input={stats2.input_tokens}, output={stats2.output_tokens}\n"
        f"Expected: 2nd call cache_read > 0 to prove Block-A ephemeral caching works."
    )

    return PhaseResult(
        phase="auditor cache",
        passed=all(ok for ok, _ in assertions),
        detail=detail,
        cost_estimate_usd=0.05,
        assertions=assertions,
    )


# ---------------------------------------------------------------------------
# Phase 2 — Full pipeline + token_stats.json
# ---------------------------------------------------------------------------


async def phase_pipeline(slug_override: str | None = None) -> PhaseResult:
    """Run generate_knowledge_pack on a cheap label; assert token_stats.json."""
    _header("Phase 2: Pipeline token_stats.json (P3)")

    import tempfile

    from api.pipeline.orchestrator import generate_knowledge_pack
    from api.pipeline.telemetry.token_stats import read_token_stats

    device_label = "Test Device Pi Zero 1"
    tmp_root = Path(tempfile.mkdtemp(prefix="smoke_pipeline_"))

    # Force max_revise_rounds=0 so the pipeline finishes in one audit pass —
    # cheaper and sufficient to exercise the token_stats write path.
    os.environ.setdefault("PIPELINE_MAX_REVISE_ROUNDS", "0")
    os.environ.setdefault("PIPELINE_SCOUT_MIN_SYMPTOMS", "1")
    os.environ.setdefault("PIPELINE_SCOUT_MIN_COMPONENTS", "1")
    os.environ.setdefault("PIPELINE_SCOUT_MIN_SOURCES", "1")

    # Reset the settings singleton so it picks up the overrides above.
    import api.config as _cfg

    _cfg._settings = None

    from api.config import get_settings

    settings = get_settings()
    slug = slug_override or "test-device-pi-zero-1"

    print(f"  memory_root (temp): {tmp_root}")
    print(f"  device_label: {device_label!r}")
    print(f"  max_revise_rounds: {settings.pipeline_max_revise_rounds}")
    print("  Running pipeline — this takes ~2-5 min and costs ~$0.30...")

    assertions: list[tuple[bool, str]] = []

    try:
        result = await generate_knowledge_pack(
            device_label=device_label,
            memory_root=tmp_root,
            max_revise_rounds=0,
        )
    except RuntimeError as exc:
        ok, lbl = _check(False, f"generate_knowledge_pack raised: {exc}")
        assertions.append((ok, lbl))
        return PhaseResult(
            phase="pipeline",
            passed=False,
            detail=str(exc),
            cost_estimate_usd=0.30,
            assertions=assertions,
        )

    stats_path = tmp_root / result.device_slug / "token_stats.json"

    assertions.append(_check(
        stats_path.exists(),
        f"token_stats.json written at {stats_path}",
    ))

    phases_detail = ""
    if stats_path.exists():
        stats = read_token_stats(stats_path)
        phases_detail = f"{len(stats)} phases: " + ", ".join(s.phase for s in stats)

        assertions.append(_check(
            len(stats) >= 4,
            f"At least 4 phases recorded (got {len(stats)}): {phases_detail}",
        ))

        all_have_duration = all(s.duration_s > 0 for s in stats)
        assertions.append(_check(
            all_have_duration,
            f"Every phase has duration_s > 0",
        ))

        all_have_input = all(
            s.input_tokens > 0 or s.cache_read_input_tokens > 0
            for s in stats
        )
        assertions.append(_check(
            all_have_input,
            "Every phase has input_tokens > 0 (or cache_read_input_tokens > 0)",
        ))

        # Writers phase cache_read check — the writers share an ephemeral cache
        # prefix so writers 2+3 should read from writer 1's cache entry.
        writer_phases = [s for s in stats if s.phase.startswith("writer_")]
        if len(writer_phases) >= 2:
            any_writer_cache_read = any(s.cache_read_input_tokens > 0 for s in writer_phases)
            assertions.append(_check(
                any_writer_cache_read,
                f"At least one writer phase has cache_read_input_tokens > 0 "
                f"(writers: {[(s.phase, s.cache_read_input_tokens) for s in writer_phases]})",
            ))
        else:
            assertions.append(_check(
                False,
                f"Expected >= 2 writer phases, got {len(writer_phases)}: "
                f"{[s.phase for s in writer_phases]}",
            ))

        assertions.append(_check(
            result.cache_read_tokens_total > 0,
            f"PipelineResult.cache_read_tokens_total={result.cache_read_tokens_total} > 0",
        ))

    detail = (
        f"verdict={result.verdict.overall_status}, "
        f"rounds={result.revise_rounds_used}, "
        f"total_tokens={result.tokens_used_total}, "
        f"cache_read={result.cache_read_tokens_total}, "
        f"cache_write={result.cache_write_tokens_total}\n"
        f"{phases_detail}"
    )

    return PhaseResult(
        phase="pipeline",
        passed=all(ok for ok, _ in assertions),
        detail=detail,
        cost_estimate_usd=0.30,
        assertions=assertions,
    )


# ---------------------------------------------------------------------------
# Phase 3 — Session (WS + auto-seed + D2 read_only)
# ---------------------------------------------------------------------------


async def phase_session(slug_override: str | None = None) -> PhaseResult:
    """Open a WS session, watch events, grep log for auto-seed and D2."""
    _header("Phase 3: Session (D1 mirror + D2 read_only + auto-seed T2)")

    import httpx

    assertions: list[tuple[bool, str]] = []

    # --- 1. Find a slug with a pack on disk -----------------------------------
    memory_root = _REPO_ROOT / "memory"
    candidate_slugs = ["mnt-reform-motherboard", "demo", "demo-pi"]
    if slug_override:
        candidate_slugs.insert(0, slug_override)

    slug: str | None = None
    for s in candidate_slugs:
        if (memory_root / s / "registry.json").exists():
            slug = s
            break

    if slug is None:
        return PhaseResult(
            phase="session",
            passed=False,
            detail=(
                "No device slug with a registry.json found on disk. "
                "Run the pipeline phase first or supply --slug."
            ),
            cost_estimate_usd=0.10,
            assertions=[_check(False, "Pack on disk found")],
        )

    print(f"  Using slug: {slug!r}")

    # --- 2. Check server is up -----------------------------------------------
    server_up = False
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get("http://localhost:8000/health")
            server_up = resp.status_code < 500
    except Exception:
        pass

    if not server_up:
        # Try the pipeline/packs endpoint as a fallback health check
        try:
            async with httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.get("http://localhost:8000/pipeline/packs")
                server_up = resp.status_code < 500
        except Exception:
            pass

    if not server_up:
        return PhaseResult(
            phase="session",
            passed=False,
            detail=(
                "Server not running on localhost:8000.\n"
                "Start it in another terminal:\n"
                "    make run > /tmp/microsolder.log 2>&1"
            ),
            cost_estimate_usd=0.10,
            assertions=[_check(False, "Server reachable on localhost:8000")],
        )

    assertions.append(_check(True, "Server reachable on localhost:8000"))

    # --- 3. Redirect server logs to a tempfile so we can grep them ------------
    log_path = Path("/tmp/microsolder.log")
    log_existed = log_path.exists()
    log_start_size = log_path.stat().st_size if log_existed else 0
    print(f"  Log file: {log_path} (pre-existing={log_existed}, offset={log_start_size})")
    if not log_existed:
        print(
            "  TIP: For auto-seed log lines to be visible, run the server with:\n"
            "       make run > /tmp/microsolder.log 2>&1"
        )

    # --- 4. Open WebSocket and exchange one turn ----------------------------
    import websockets

    repair_id = f"smoke_test_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    ws_url = f"ws://localhost:8000/ws/diagnostic/{slug}?tier=fast&repair={repair_id}"
    print(f"  WS URL: {ws_url}")
    print("  Connecting and waiting up to 30s for a complete agent turn...")

    events_received: list[dict[str, Any]] = []
    session_create_failed = False
    turn_completed = False
    ws_error: str | None = None

    try:
        async with websockets.connect(ws_url, open_timeout=15) as ws:  # type: ignore[attr-defined]
            # Send the user message
            msg = json.dumps({
                "type": "message",
                "text": "Quick sanity check, don't do anything — just acknowledge.",
            })
            await ws.send(msg)

            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 5.0))
                    ev = json.loads(raw)
                    events_received.append(ev)
                    etype = ev.get("type", "")
                    print(f"    <- {etype}" + (
                        f": {ev.get('text', '')[:60]}" if etype in ("message", "error", "thinking") else ""
                    ))
                    if etype == "error":
                        ws_error = ev.get("text", "")
                        if "session create failed" in ws_error.lower():
                            session_create_failed = True
                        break
                    if etype == "turn_complete":
                        turn_completed = True
                        break
                except asyncio.TimeoutError:
                    continue
                except websockets.exceptions.ConnectionClosed:
                    break
    except Exception as exc:
        ws_error = str(exc)
        print(f"  WebSocket error: {exc}")

    # --- 5. Assertions on WS events ------------------------------------------
    event_types = {ev.get("type") for ev in events_received}

    assertions.append(_check(
        "session_ready" in event_types,
        f"Received 'session_ready' event (got types: {sorted(event_types)})",
    ))

    assertions.append(_check(
        not session_create_failed,
        f"sessions.create did NOT return 4xx (D2 read_only check). "
        f"ws_error={ws_error!r}",
    ))

    assertions.append(_check(
        turn_completed or (
            "message" in event_types and
            any(ev.get("role") == "assistant" for ev in events_received)
        ),
        "Agent responded with at least one message",
    ))

    # --- 6. Grep log for auto-seed markers -----------------------------------
    auto_seed_attempted = False
    auto_seed_log_lines: list[str] = []
    mirror_log_lines: list[str] = []

    if log_path.exists():
        with log_path.open("r", errors="replace") as fh:
            fh.seek(log_start_size)
            new_lines = fh.readlines()

        for line in new_lines:
            lo = line.lower()
            if "auto-seed" in lo or "auto_seed" in lo:
                auto_seed_log_lines.append(line.rstrip())
                auto_seed_attempted = True
            if "validationmirror" in lo or "mirror_outcome" in lo or "mirror task" in lo.replace("-", ""):
                mirror_log_lines.append(line.rstrip())

        assertions.append(_check(
            auto_seed_attempted,
            f"maybe_auto_seed was called (log lines found: {len(auto_seed_log_lines)})",
        ))
        if auto_seed_log_lines:
            for ln in auto_seed_log_lines[-3:]:
                print(f"    LOG: {ln}")
    else:
        assertions.append(_check(
            False,
            f"Log file {log_path} not found — cannot verify auto-seed. "
            f"Run server with: make run > /tmp/microsolder.log 2>&1",
        ))

    detail_parts = [
        f"slug={slug}, repair_id={repair_id}",
        f"events_received={len(events_received)}, event_types={sorted(event_types)}",
        f"turn_completed={turn_completed}, ws_error={ws_error}",
        f"auto_seed_log_lines={len(auto_seed_log_lines)}",
    ]
    if auto_seed_log_lines:
        detail_parts.append("Last auto-seed log lines:\n  " + "\n  ".join(auto_seed_log_lines[-5:]))
    if mirror_log_lines:
        detail_parts.append("Mirror log lines:\n  " + "\n  ".join(mirror_log_lines[-3:]))

    return PhaseResult(
        phase="session",
        passed=all(ok for ok, _ in assertions),
        detail="\n".join(detail_parts),
        cost_estimate_usd=0.10,
        assertions=assertions,
    )


# ---------------------------------------------------------------------------
# Dry-run plan
# ---------------------------------------------------------------------------


def print_dry_run(phase: str) -> None:
    _header("Dry-run plan (no API calls will be made)")
    phases: list[tuple[str, str, float]] = []

    if phase in ("cache", "all"):
        phases.append((
            "cache",
            "Call run_auditor x2 on a 2-component fixture. "
            "Assert cache_read_input_tokens > 0 on 2nd call (P1+P2).",
            0.05,
        ))
    if phase in ("pipeline", "all"):
        phases.append((
            "pipeline",
            "Run generate_knowledge_pack('Test Device Pi Zero 1') in a tmpdir "
            "with max_revise_rounds=0. Assert token_stats.json written, "
            ">= 4 phases, writers have cache_read > 0 (P3).",
            0.30,
        ))
    if phase in ("session", "all"):
        phases.append((
            "session",
            "Open WS /ws/diagnostic/{slug}?tier=fast&repair=smoke_test_… "
            "Send one message, wait 30s for turn_complete. "
            "Assert session_ready, D2 read_only no-error, auto-seed log line (T2).",
            0.10,
        ))

    total = sum(c for _, _, c in phases)
    for name, desc, cost in phases:
        print(f"\n  [{name}]  est. ${cost:.2f}")
        print(f"    {desc}")
    print(f"\n  Total estimated cost: ${total:.2f}")
    print("\n  Re-run without --dry-run to execute.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Real-API smoke test for the 22-commit optimization sweep.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--phase",
        choices=["cache", "pipeline", "session", "all"],
        default="cache",
        help="Which phase(s) to run (default: cache — cheapest).",
    )
    parser.add_argument(
        "--slug",
        default=None,
        help="Override device slug for pipeline / session phases.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and expected costs without making API calls.",
    )
    args = parser.parse_args()

    if args.dry_run:
        print_dry_run(args.phase)
        return 0

    # --- Preflight: API key --------------------------------------------------
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY is not set.")
        print("  Copy .env.example to .env and set your key, or export it in your shell.")
        return 1

    # Suppress noisy library logs during the run
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("microsolder").setLevel(logging.INFO)

    results: list[PhaseResult] = []

    try:
        if args.phase in ("cache", "all"):
            results.append(await phase_cache())
        if args.phase in ("pipeline", "all"):
            results.append(await phase_pipeline(args.slug))
        if args.phase in ("session", "all"):
            results.append(await phase_session(args.slug))
    except KeyboardInterrupt:
        print("\n[interrupted]")
        return 1

    # --- Summary -------------------------------------------------------------
    print(f"\n{'=' * 60}")
    print("  Smoke Test Summary")
    print("=" * 60)

    total_cost = 0.0
    any_fail = False
    for res in results:
        status = f"{PASS} PASS" if res.passed else f"{FAIL} FAIL"
        # First line of detail as a short label
        first_line = res.detail.split("\n")[0][:80]
        print(f"  Phase ({res.phase:<20})  {status}  —  {first_line}")
        if not res.passed:
            any_fail = True
            # Print the failing assertions
            for ok, lbl in res.assertions:
                if not ok:
                    print(f"    {FAIL} {lbl}")
        total_cost += res.cost_estimate_usd

    print(f"\n  Total estimated cost: ${total_cost:.2f}")
    exit_code = 1 if any_fail else 0
    print(f"  Exit code: {exit_code}")
    print("=" * 60)

    return exit_code


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
