"""Smoke test for the sub-agent consultation helper.

Calls `_run_subagent_consultation` against a real MA tier (default: fast)
with a question that needs no tools, and prints the answer + cost-side
metrics. Exits non-zero on failure (FAIL or "19" missing from the answer).

Usage:
    .venv/bin/python scripts/smoke_subagent.py [tier]
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

load_dotenv(REPO_ROOT / ".env")

# Force line-buffered stdout so the script streams live when not on a TTY
# (eg piped to a file or backgrounded). Without this Python buffers the
# whole run and only flushes at exit. See CLAUDE.md streaming rule.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:  # noqa: BLE001
    pass

# Surface runtime_managed INFO logs (session lifecycle, tool dispatch,
# retries) to stderr so progress is visible during the run.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    stream=sys.stderr,
)

from api.agent.managed_ids import load_managed_ids  # noqa: E402
from api.agent.runtime_managed import _run_subagent_consultation  # noqa: E402


async def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set in .env", file=sys.stderr)
        return 2

    tier = sys.argv[1] if len(sys.argv) > 1 else "fast"
    if tier not in {"fast", "normal", "deep"}:
        print(f"ERROR: tier must be fast|normal|deep, got {tier!r}", file=sys.stderr)
        return 2

    ids = load_managed_ids()
    env_id = ids["environment_id"]

    client = AsyncAnthropic()

    query = "What is sqrt(144) + 7? Answer with just a number."
    context = (
        "You are being smoke-tested. Reply concisely. The expected answer is 19."
    )

    print(f"[smoke] tier={tier} env={env_id[:25]}…")
    print(f"[smoke] query={query!r}")
    started = time.monotonic()

    result = await _run_subagent_consultation(
        client=client,
        tier=tier,  # type: ignore[arg-type]
        query=query,
        context=context,
        environment_id=env_id,
        parent_session_id="smoke-test",
        timeout_s=60.0,
    )

    elapsed = time.monotonic() - started
    print(f"[smoke] elapsed={elapsed:.1f}s")
    print(f"[smoke] result={result!r}")

    if not result.get("ok"):
        print(f"[smoke] FAIL: {result.get('reason')} - {result.get('error')}")
        return 1

    answer = result.get("answer", "")
    print(f"\n[smoke] answer:\n{answer}\n")

    if "19" not in answer:
        print("[smoke] FAIL: answer doesn't contain '19' (expected 5+14)")
        return 1

    print("[smoke] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
