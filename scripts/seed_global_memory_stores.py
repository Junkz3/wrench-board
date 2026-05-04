"""Push api/agent/seed_data/global_{patterns,playbooks}/* to the singleton
Managed Agents memory stores.

Idempotent: stores are created on first run via `ensure_global_store`,
then re-runs are upsert-by-path so unchanged files just get re-replaced
(API doesn't deduplicate, but the cost is negligible for ~7 small files).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_ROOT = REPO_ROOT / "api" / "agent" / "seed_data"

PATTERNS_DESC = (
    "Cross-device failure archetypes for board-level diagnostics: "
    "short-to-GND on power rails, thermal cascade failures, BGA solder "
    "ball lift, bench anti-patterns. Markdown documents under "
    "/patterns/<id>.md. Read this store first when the device-specific "
    "rules return 0 matches — global archetypes often apply across "
    "device families."
)
PLAYBOOKS_DESC = (
    "Diagnostic protocol templates conformant to bv_propose_protocol's "
    "schema (steps with target/nominal/unit/pass_range). JSON documents "
    "under /playbooks/<id>.json indexed by symptom (boot-no-power, "
    "usb-no-charge, pmic-rail-collapse). Reference these BEFORE "
    "synthesizing a protocol from scratch — they are field-tested."
)


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ERROR: ANTHROPIC_API_KEY not set")

    # Import after dotenv so settings pick up env vars
    from api.agent.memory_stores import ensure_global_store, upsert_memory

    client = AsyncAnthropic()

    print("Ensuring global stores exist…")
    patterns_id = await ensure_global_store(
        client, kind="patterns", description=PATTERNS_DESC,
    )
    playbooks_id = await ensure_global_store(
        client, kind="playbooks", description=PLAYBOOKS_DESC,
    )
    print(f"  patterns:  {patterns_id}")
    print(f"  playbooks: {playbooks_id}")

    if not patterns_id or not playbooks_id:
        sys.exit("ERROR: failed to ensure one or both global stores")

    pairs = [
        (patterns_id, SEED_ROOT / "global_patterns", "/patterns", ".md"),
        (playbooks_id, SEED_ROOT / "global_playbooks", "/playbooks", ".json"),
    ]
    for store_id, src_dir, dest_prefix, ext in pairs:
        if not src_dir.exists():
            print(f"WARN: {src_dir} missing, skipping")
            continue
        for src in sorted(src_dir.iterdir()):
            if src.suffix != ext:
                continue
            dest_path = f"{dest_prefix}/{src.name}"
            content = src.read_text(encoding="utf-8")
            result = await upsert_memory(
                client, store_id=store_id, path=dest_path, content=content,
            )
            status = "OK" if result else "FAIL"
            print(f"  [{status}] {dest_path} ({len(content)}B)")

    print("\n✅ Global stores seeded.")


if __name__ == "__main__":
    asyncio.run(main())
