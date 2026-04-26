# SPDX-License-Identifier: Apache-2.0
"""Live smoke test for the 4-layer MA memory architecture.

Opens an MA session against the existing seeded device (defaults to
iphone-x), provisions all 4 stores, sends one user message that nudges
the agent to consult the global playbooks mount, and asserts the agent
either references the playbooks or invokes a filesystem tool.

Costs a few cents of Anthropic credits per run (one Haiku-tier session,
~5k tokens).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ERROR: ANTHROPIC_API_KEY not set")

    from anthropic import AsyncAnthropic

    from api.agent.managed_ids import load_managed_ids
    from api.agent.memory_stores import (
        ensure_global_store,
        ensure_memory_store,
        ensure_repair_store,
    )

    client = AsyncAnthropic()
    ids = load_managed_ids()
    if not ids or "fast" not in ids.get("agents", {}):
        sys.exit("ERROR: managed_ids.json missing or no 'fast' tier — run bootstrap")

    patterns_id = await ensure_global_store(
        client, kind="patterns", description="patterns",
    )
    playbooks_id = await ensure_global_store(
        client, kind="playbooks", description="playbooks",
    )
    device_id = await ensure_memory_store(client, "iphone-x")
    repair_id = "smoke-R1"
    repair_store_id = await ensure_repair_store(
        client, device_slug="iphone-x", repair_id=repair_id,
    )

    print("Stores:")
    for label, sid in [
        ("patterns", patterns_id),
        ("playbooks", playbooks_id),
        ("device-iphone-x", device_id),
        (f"repair-{repair_id}", repair_store_id),
    ]:
        print(f"  {label:25s} {sid}")

    if not all([patterns_id, playbooks_id, device_id, repair_store_id]):
        sys.exit("ERROR: at least one store failed to provision")

    resources = [
        {"type": "memory_store", "memory_store_id": patterns_id, "access": "read_only",
         "prompt": "Global cross-device failure archetypes."},
        {"type": "memory_store", "memory_store_id": playbooks_id, "access": "read_only",
         "prompt": "Diagnostic protocol templates."},
        {"type": "memory_store", "memory_store_id": device_id, "access": "read_only",
         "prompt": "Knowledge pack + field reports for iphone-x."},
        {"type": "memory_store", "memory_store_id": repair_store_id, "access": "read_write",
         "prompt": "Scratch notebook for THIS repair (smoke-R1)."},
    ]

    agent = ids["agents"]["fast"]
    env_id = ids["environment_id"]

    print("\nCreating session…")
    session = await client.beta.sessions.create(
        agent={"type": "agent", "id": agent["id"], "version": agent["version"]},
        environment_id=env_id,
        resources=resources,
        title=f"smoke layered memory {repair_id}",
    )
    print(f"  session id: {session.id}")

    # Stream-first: open stream BEFORE sending the kickoff
    stream = await client.beta.sessions.events.stream(session_id=session.id)

    kickoff = (
        "Salut. iphone-x sur le banc, plainte: ne s'allume pas, écran reste noir. "
        "Avant de proposer un plan, va voir si tes mounts contiennent un playbook "
        "qui match ce symptôme — montre-moi ce que tu trouves."
    )
    await client.beta.sessions.events.send(
        session_id=session.id,
        events=[{"type": "user.message",
                 "content": [{"type": "text", "text": kickoff}]}],
    )

    print(f"\nKickoff sent. Streaming events…\n{'-'*60}")

    text_seen: list[str] = []
    tool_uses: list[str] = []
    event_count = 0
    async for event in stream:
        event_count += 1
        etype = getattr(event, "type", "?")
        if etype == "agent.message":
            for blk in getattr(event, "content", []):
                if getattr(blk, "type", "") == "text":
                    chunk = getattr(blk, "text", "")
                    text_seen.append(chunk)
                    print(chunk, end="", flush=True)
        elif etype == "agent.tool_use":
            tname = getattr(event, "tool_name", None) or getattr(event, "name", "?")
            tool_uses.append(tname)
            print(f"\n[tool_use: {tname}]", flush=True)
        elif etype == "agent.custom_tool_use":
            tname = getattr(event, "name", "?")
            tool_uses.append(f"custom:{tname}")
            print(f"\n[custom_tool_use: {tname}] (session needs response)", flush=True)
            break  # custom tools require a response; smoke test stops here
        elif etype == "session.status_idle":
            stop_reason = getattr(event, "stop_reason", None)
            stop_type = getattr(stop_reason, "type", None) if stop_reason else None
            if stop_type != "requires_action":
                print(f"\n--- idle, stop_reason={stop_type} ---")
                break
        elif etype == "session.status_terminated":
            print("\n--- terminated ---")
            break
        elif etype == "session.error":
            print(f"\n--- session error: {event} ---")
            break
        if event_count > 300:
            print("\n--- safety break (300 events) ---")
            break

    print("\n" + "="*60)
    print("RESULT")
    print("="*60)
    full_text = "".join(text_seen)
    print(f"Total response chars: {len(full_text)}")
    print(f"Tool uses observed: {tool_uses}")

    fs_tools = {"grep", "read", "glob", "ls", "write", "edit"}
    hit_playbooks = (
        "playbook" in full_text.lower()
        or "boot-no-power" in full_text.lower()
        or "/playbooks/" in full_text.lower()
    )
    hit_fs_tool = any(t in fs_tools for t in tool_uses)

    print(f"\n  ✓ referenced playbooks layer: {hit_playbooks}")
    print(f"  ✓ used filesystem tools:      {hit_fs_tool}")

    if hit_playbooks or hit_fs_tool:
        print("\n✅ PASS: agent reached the global mounts via filesystem tools or content.")
    else:
        print("\n❌ FAIL: agent did not appear to consult the global mounts.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
