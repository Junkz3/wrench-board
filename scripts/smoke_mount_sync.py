"""Smoke the memory-store mount sync that runs after `mb_expand_knowledge`.

Calls `seed_memory_store_from_pack(only_files=["rules.json", "registry.json"])`
on an existing device pack and reports the per-file status. Doesn't mutate
the pack on disk — purely a re-upload to the MA memory store.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

load_dotenv(REPO_ROOT / ".env")

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:  # noqa: BLE001
    pass

import logging  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    stream=sys.stderr,
)

from api.agent.memory_seed import seed_memory_store_from_pack  # noqa: E402
from api.config import get_settings  # noqa: E402


async def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 2

    slug = sys.argv[1] if len(sys.argv) > 1 else "iphone-x"
    settings = get_settings()
    pack_dir = Path(settings.memory_root) / slug
    if not pack_dir.exists():
        print(f"ERROR: no pack at {pack_dir}", file=sys.stderr)
        return 2

    if not settings.ma_memory_store_enabled:
        print(
            "ma_memory_store_enabled=False in settings — sync would no-op",
            file=sys.stderr,
        )
        return 2

    print(f"[smoke] device={slug}, pack_dir={pack_dir}")
    client = AsyncAnthropic()
    started = time.monotonic()
    status = await seed_memory_store_from_pack(
        client=client,
        device_slug=slug,
        pack_dir=pack_dir,
        only_files=["rules.json", "registry.json"],
    )
    elapsed = time.monotonic() - started

    print(f"[smoke] elapsed={elapsed:.2f}s")
    for memory_path, state in status.items():
        emoji = "✅" if state == "seeded" else "⚠️"
        print(f"  {emoji} {memory_path}: {state}")

    if all(s == "seeded" for s in status.values()):
        print("[smoke] PASS")
        return 0
    print("[smoke] FAIL — at least one file did not seed")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
