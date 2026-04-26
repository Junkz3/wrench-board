"""Archive any MA agent that's not in the current managed_ids.json.

When iterating on agent prompts, dropping the entry from managed_ids.json
and re-running bootstrap creates a NEW agent without archiving the old
one (bootstrap only archives agents it can see in the existing JSON).
That leaves orphaned agents charging session-h potentially via stale
sessions and clutters the org.

This script:
  1. Reads managed_ids.json → set of "live" agent IDs
  2. Lists every agent via client.beta.agents.list (paginated)
  3. Archives any non-archived agent whose ID isn't in the live set
  4. Reports counts
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

load_dotenv(REPO_ROOT / ".env")

from api.agent.managed_ids import load_managed_ids  # noqa: E402


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 2

    ids = load_managed_ids()
    live = {info["id"] for info in ids["agents"].values()}
    print(f"[cleanup] live agents in managed_ids.json: {len(live)}")
    for tier, info in ids["agents"].items():
        print(f"   • {tier:8s} → {info['id']}")
    print()

    client = Anthropic()
    archived: list[tuple[str, str]] = []
    kept: list[str] = []
    skipped: list[str] = []

    for agent in client.beta.agents.list():
        is_archived = getattr(agent, "archived_at", None) is not None
        if is_archived:
            skipped.append(agent.id)
            continue
        if agent.id in live:
            kept.append(agent.id)
            continue
        # orphan — archive
        try:
            client.beta.agents.archive(agent.id)
            archived.append((agent.id, getattr(agent, "name", "?")))
            print(f"[cleanup] archived orphan {agent.id} ({getattr(agent, 'name', '?')})")
        except Exception as exc:  # noqa: BLE001
            print(f"[cleanup] failed to archive {agent.id}: {exc}", file=sys.stderr)

    print()
    print(f"[cleanup] kept (live):     {len(kept)}")
    print(f"[cleanup] archived now:    {len(archived)}")
    print(f"[cleanup] already archived: {len(skipped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
