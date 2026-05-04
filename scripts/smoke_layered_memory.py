"""Live smoke test for the 4-layer MA memory architecture.

Runs TWO sessions back-to-back on the same repair_id to validate the scribe
pattern end-to-end :
  - Session 1 : agent writes state.md + decisions/initial.md to the repair
    mount (kickoff explicitly nudges the write).
  - Session 2 : agent must grep the mount and surface content from
    Session 1 (asserts on referenced text + filesystem tool use).

Costs ~5-10 cents of Anthropic credits per run (two Haiku-tier sessions).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# Note on path casing : the MA infra normalizes mount paths to lowercase, so
# even though the store name is `wrench-board-repair-iphone-x-smoke-R1`, the
# mount appears at `…-smoke-r1`. We use lowercase in the kickoffs to match.
SESSION_1_KICKOFF = (
    "Salut. iphone-x sur le banc, plainte: ne s'allume pas, écran reste noir. "
    "Avant de proposer un plan, va voir si tes mounts contiennent un playbook "
    "qui match ce symptôme — montre-moi ce que tu trouves. "
    "Avant de partir, écris ton état actuel dans "
    "/mnt/memory/wrench-board-repair-iphone-x-smoke-r1/state.md "
    "(symptôme initial, hypothèse en cours, prochaine action) et un fichier "
    "decisions/initial.md résumant ta première décision. Confirme-moi 'OK noté' "
    "à la fin."
)

SESSION_2_KICKOFF = (
    "Re-bonjour. On reprend la repair iphone-x R1 en cours. "
    "Lis /mnt/memory/wrench-board-repair-iphone-x-smoke-r1/state.md et "
    "raconte-moi ce que tu trouves : quel symptôme, quelle hypothèse, quelle "
    "prochaine action. Cite explicitement le contenu du fichier — pas de "
    "paraphrase, je veux vérifier que tu as bien grep le mount."
)


async def _build_resources(client: Any) -> tuple[list[dict], dict[str, Any]]:
    """Provision (or fetch) the 4 stores and return resources + ids dict."""
    from api.agent.memory_stores import (
        ensure_global_store,
        ensure_memory_store,
        ensure_repair_store,
    )

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
    return resources, {
        "patterns_id": patterns_id,
        "playbooks_id": playbooks_id,
        "device_id": device_id,
        "repair_store_id": repair_store_id,
    }


async def run_session(
    client: Any,
    *,
    agent: dict[str, Any],
    env_id: str,
    resources: list[dict],
    repair_id: str,
    label: str,
    kickoff: str,
) -> dict[str, Any]:
    """Open one session, send kickoff, drain stream until idle/terminated.

    Returns {full_text, tool_uses, session_id}.
    """
    print(f"\n=== {label} (repair={repair_id}) ===")
    print("Creating session…")
    session = await client.beta.sessions.create(
        agent={"type": "agent", "id": agent["id"], "version": agent["version"]},
        environment_id=env_id,
        resources=resources,
        title=f"smoke layered memory {label}",
    )
    print(f"  session id: {session.id}")

    # Stream-first: open stream BEFORE sending the kickoff
    stream = await client.beta.sessions.events.stream(session_id=session.id)

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

    return {
        "full_text": "".join(text_seen),
        "tool_uses": tool_uses,
        "session_id": session.id,
    }


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ERROR: ANTHROPIC_API_KEY not set")

    from anthropic import AsyncAnthropic

    from api.agent.managed_ids import load_managed_ids

    client = AsyncAnthropic()
    ids = load_managed_ids()
    if not ids or "fast" not in ids.get("agents", {}):
        sys.exit("ERROR: managed_ids.json missing or no 'fast' tier — run bootstrap")

    resources, _store_ids = await _build_resources(client)
    agent = ids["agents"]["fast"]
    env_id = ids["environment_id"]
    repair_id = "smoke-R1"

    # ---- Session 1 : write state.md + decisions/initial.md ----
    result_1 = await run_session(
        client,
        agent=agent, env_id=env_id, resources=resources,
        repair_id=repair_id, label="Session 1 (write)",
        kickoff=SESSION_1_KICKOFF,
    )

    fs_write_tools = {"write", "edit"}
    fs_read_tools = {"grep", "read", "glob", "ls", "cat"}
    s1_wrote = any(t in fs_write_tools for t in result_1["tool_uses"])

    print("\n" + "=" * 60)
    print("SESSION 1 RESULT")
    print("=" * 60)
    print(f"Tool uses observed: {result_1['tool_uses']}")
    print(f"  ✓ used a write/edit tool: {s1_wrote}")

    # ---- Session 2 : grep state.md + cite content ----
    result_2 = await run_session(
        client,
        agent=agent, env_id=env_id, resources=resources,
        repair_id=repair_id, label="Session 2 (resume)",
        kickoff=SESSION_2_KICKOFF,
    )

    s2_grepped = any(t in fs_read_tools for t in result_2["tool_uses"])
    s2_text_lower = result_2["full_text"].lower()
    # Strict assertion : the agent must surface specific content from
    # Session 1, not just incidentally mention the device. Looking for
    # diagnostic-specific terms the agent likely wrote into state.md
    # (boot-no-power playbook, the boot sequence rails, etc.).
    diagnostic_terms = ["vbus", "f1", "vbat", "pmu", "vddmain", "soc_reset",
                        "boot", "no-power", "playbook", "rail", "séquence"]
    diagnostic_hits = [t for t in diagnostic_terms if t in s2_text_lower]
    s2_recovered = bool(diagnostic_hits)
    # Also reject explicit failure patterns from the agent.
    failure_phrases = ["n'existe pas", "pas accessible", "introuvable",
                       "no such file", "file not found", "aucun mount"]
    s2_reported_failure = any(p in s2_text_lower for p in failure_phrases)

    print("\n" + "=" * 60)
    print("SESSION 2 RESULT")
    print("=" * 60)
    print(f"Tool uses observed: {result_2['tool_uses']}")
    print(f"  ✓ used a filesystem read tool: {s2_grepped}")
    print(f"  ✓ surfaced diagnostic content from S1: {diagnostic_hits}")
    print(f"  ✗ reported missing-file failure: {s2_reported_failure}")

    # ---- Verdict ----
    print("\n" + "=" * 60)
    print("VERDICT")
    print("=" * 60)

    failures = []
    if not s1_wrote:
        failures.append("Session 1 did not invoke a write/edit tool — scribe write missing.")
    if not s2_grepped:
        failures.append("Session 2 did not invoke a filesystem read tool — no resume scribe read.")
    if not s2_recovered:
        failures.append(
            "Session 2 did not surface specific diagnostic content from Session 1. "
            "Checked for: vbus/f1/vbat/pmu/vddmain/soc_reset/boot/no-power/playbook/rail/séquence."
        )
    if s2_reported_failure:
        failures.append(
            "Session 2 reported the file/mount as missing — the scribe handoff actually "
            "broke. Check store provisioning idempotence and mount path normalization."
        )

    if failures:
        print("\n❌ FAIL")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("\n✅ PASS — scribe pattern validated end-to-end across 2 sessions.")


if __name__ == "__main__":
    asyncio.run(main())
