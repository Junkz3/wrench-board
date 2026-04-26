"""Smoke test the KnowledgeCurator sub-agent — instrumented version.

Streams the raw events directly so we can see every tool_use / tool_result /
message and time-stamp each one. Helps diagnose timeouts.

Usage:
    .venv/bin/python scripts/smoke_curator.py
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
# (eg piped to a file or backgrounded). Without this, Python buffers the
# whole 2-3 min run and only flushes at exit. See CLAUDE.md streaming rule.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:  # noqa: BLE001
    pass

# Surface runtime_managed + anthropic INFO logs (curator session lifecycle,
# tool dispatch, retries) to stderr so progress is visible during the run.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    stream=sys.stderr,
)

from api.agent.managed_ids import get_agent, load_managed_ids  # noqa: E402
from api.agent.runtime_managed import _sessions_create_with_retry  # noqa: E402


async def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 2

    ids = load_managed_ids()
    env_id = ids["environment_id"]

    if "curator" not in ids["agents"]:
        print(
            "ERROR: 'curator' agent not in managed_ids.json — run "
            "scripts/bootstrap_managed_agent.py",
            file=sys.stderr,
        )
        return 2

    client = AsyncAnthropic()
    curator = get_agent(ids, tier="curator")

    prompt = (
        "Device: iPhone X (A1865 / A1901)\n\n"
        "Focus symptoms (target THESE only):\n"
        "  - earpiece dead but loudspeaker works\n\n"
        "Run a focused web research pass and produce the Markdown dump in "
        "your system-prompt format. 2-4 searches max. Stop at 2 symptom "
        "blocks with sources."
    )

    print(f"[smoke] curator agent: {curator['id']}")
    print("[smoke] prompt length:", len(prompt))
    print("[smoke] starting session…\n")

    sub = await _sessions_create_with_retry(
        client,
        agent={"type": "agent", "id": curator["id"], "version": curator["version"]},
        environment_id=env_id,
        title="smoke-curator",
    )
    sid = sub.id
    print(f"[smoke] session={sid}\n")

    started = time.monotonic()
    text_chunks: list[str] = []
    tool_count = 0

    def t() -> str:
        return f"+{time.monotonic() - started:5.1f}s"

    try:
        stream_ctx = await client.beta.sessions.events.stream(sid)
        async with stream_ctx as stream:
            await client.beta.sessions.events.send(
                sid,
                events=[{
                    "type": "user.message",
                    "content": [{"type": "text", "text": prompt}],
                }],
            )

            async def consume() -> None:
                nonlocal tool_count
                async for event in stream:
                    etype = getattr(event, "type", None)
                    if etype == "agent.message":
                        for block in getattr(event, "content", []) or []:
                            if getattr(block, "type", None) == "text":
                                text_chunks.append(block.text)
                                preview = block.text[:100].replace("\n", " ")
                                print(f"{t()} agent.message text={preview!r}")
                    elif etype == "agent.tool_use":
                        tool_count += 1
                        tn = getattr(event, "name", None)
                        ti = getattr(event, "input", {}) or {}
                        print(f"{t()} agent.tool_use name={tn} input={ti}")
                    elif etype == "agent.tool_result":
                        rs = getattr(event, "is_error", False)
                        print(f"{t()} agent.tool_result is_error={rs}")
                    elif etype == "session.status_idle":
                        stop = getattr(event, "stop_reason", None)
                        sr = getattr(stop, "type", None) if stop else None
                        print(f"{t()} session.status_idle stop_reason={sr}")
                        if sr != "requires_action":
                            return
                    elif etype == "session.status_running":
                        print(f"{t()} session.status_running")
                    elif etype == "session.status_terminated":
                        print(f"{t()} session.status_terminated")
                        return
                    elif etype == "session.error":
                        err = getattr(event, "error", None)
                        msg = getattr(err, "message", None) if err else None
                        print(f"{t()} session.error: {msg}")
                    elif etype == "span.model_request_end":
                        u = getattr(event, "model_usage", None)
                        if u:
                            print(
                                f"{t()} span.model_request_end "
                                f"in={u.input_tokens} out={u.output_tokens} "
                                f"cache_r={u.cache_read_input_tokens}"
                            )
                    else:
                        print(f"{t()} {etype}")

            try:
                await asyncio.wait_for(consume(), timeout=300.0)
            except TimeoutError:
                print(f"{t()} TIMEOUT 300s")
    finally:
        try:
            await client.beta.sessions.archive(sid)
        except Exception:  # noqa: BLE001
            pass

    full = "\n".join(c for c in text_chunks if c)
    print()
    print(f"[smoke] elapsed={time.monotonic() - started:.1f}s")
    print(f"[smoke] tool_uses={tool_count}")
    print(f"[smoke] text chunks={len(text_chunks)} total_bytes={len(full)}")

    if not full.strip():
        print("[smoke] FAIL: no text output")
        return 1

    print("\n--- CURATOR OUTPUT ---")
    print(full[:3000])
    if len(full) > 3000:
        print(f"\n… ({len(full) - 3000} more chars)")
    print("--- END ---\n[smoke] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
