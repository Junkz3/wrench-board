# SPDX-License-Identifier: Apache-2.0
"""Live smoke test: per-repair scribe mount survives across two sessions.

Validates the *real* value of the layered MA memory architecture: an agent
in session 1 writes to its repair-scoped scratch mount, the session ends,
a NEW session on the SAME repair_id reads the mount and continues from
where the previous agent left off — without any pre-cuisined LLM summary.

Flow:
  1. Provision a fresh per-repair store (timestamped repair_id, never
     reused by previous runs).
  2. Session 1: kickoff explicitly tells the agent to write a
     diagnostic snapshot to /mnt/memory/microsolder-repair-*/state.md
     plus a decision file. Wait for end_turn.
  3. Session 2 on the SAME repair_id (so the same store reattaches):
     kickoff asks "what did we figure out earlier?" with NO context.
     The agent must self-orient by reading the mount.
  4. Assert the session-2 response cites a token planted in session 1
     (ENABLES the scribe round-trip; without the mount, session 2 sees
     nothing).

Costs ~$0.02-0.05 (two short Haiku-tier sessions, ~10k total tokens).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# Unique token planted in session 1's kickoff. Session 2 must surface it.
PLANTED_TOKEN = f"BENCHMARK-{uuid.uuid4().hex[:8].upper()}"


def _stub_custom_tool_result(name: str, payload: dict) -> str:
    """Smoke-only minimal responses for custom tools the agent might call.

    The smoke must let the agent reach its `write` without blocking on a
    custom tool that has no real backend in this script. We return shapes
    that look 'good enough' so the agent doesn't loop or escalate. JSON
    text blocks (the wire format MA expects in user.custom_tool_result).
    """
    if name == "mb_get_component":
        # Pretend the refdes is valid so the agent can move on.
        refdes = payload.get("refdes", "?")
        return (
            '{"found": true, "canonical_name": "%s", '
            '"memory_bank": {"role": "decoupling", "kind": "passive_c"}, '
            '"board": null}' % refdes
        )
    if name == "mb_get_rules_for_symptoms":
        # No matches — encourages the agent to fall back to its own reasoning.
        return '{"matches": [], "total_available_rules": 0}'
    if name == "mb_record_finding":
        return '{"ok": true, "report_id": "smoke-stub", "json_status": "written"}'
    if name == "profile_get":
        return ('{"name": "smoke", "level": "intermediate", '
                '"verbosity": "concise", "tools": ["multimeter", "scope"]}')
    if name == "profile_check_skills":
        return '{"skills": []}'
    # Default stub: ack so the agent doesn't loop on it.
    return '{"ok": true, "stub": true}'


async def _run_session(
    *,
    client: Any,
    agent: dict,
    env_id: str,
    resources: list[dict],
    title: str,
    kickoff: str,
    label: str,
    max_custom_tool_calls: int = 6,
) -> tuple[str, list[str], str]:
    """Open a session, send kickoff, drain to idle (handling custom tool calls).

    Custom tool calls are answered with stub payloads (see _stub_custom_tool_result)
    so the agent can keep progressing toward its file-system writes without
    needing the real backend. Caps at `max_custom_tool_calls` to avoid runaway.
    """
    session = await client.beta.sessions.create(
        agent={"type": "agent", "id": agent["id"], "version": agent["version"]},
        environment_id=env_id,
        resources=resources,
        title=title,
    )
    print(f"\n[{label}] session id: {session.id}")

    stream = await client.beta.sessions.events.stream(session_id=session.id)

    await client.beta.sessions.events.send(
        session_id=session.id,
        events=[{"type": "user.message",
                 "content": [{"type": "text", "text": kickoff}]}],
    )

    print(f"[{label}] streaming…")
    print("-" * 60)

    text_chunks: list[str] = []
    tool_uses: list[str] = []
    pending_custom_tools: dict[str, dict] = {}
    custom_tool_call_count = 0
    event_count = 0
    fs_tools = {"grep", "read", "glob", "ls", "write", "edit"}

    async for event in stream:
        event_count += 1
        etype = getattr(event, "type", "?")
        if etype == "agent.message":
            for blk in getattr(event, "content", []):
                if getattr(blk, "type", "") == "text":
                    chunk = getattr(blk, "text", "")
                    text_chunks.append(chunk)
                    print(chunk, end="", flush=True)
        elif etype == "agent.tool_use":
            tname = getattr(event, "tool_name", None) or getattr(event, "name", "?")
            tool_uses.append(tname)
            input_repr = ""
            try:
                inp = getattr(event, "input", {}) or {}
                if isinstance(inp, dict):
                    input_repr = " " + " ".join(
                        f"{k}={str(v)[:60]!r}" for k, v in list(inp.items())[:3]
                    )
            except Exception:  # noqa: BLE001
                pass
            print(f"\n[tool_use: {tname}{input_repr}]", flush=True)
        elif etype == "agent.custom_tool_use":
            tname = getattr(event, "name", "?")
            tool_uses.append(f"custom:{tname}")
            eid = getattr(event, "id", None)
            inp = getattr(event, "input", {}) or {}
            print(f"\n[custom_tool_use: {tname}] queued, will stub", flush=True)
            if eid:
                pending_custom_tools[eid] = {"name": tname, "input": inp}
        elif etype == "session.status_idle":
            stop_reason = getattr(event, "stop_reason", None)
            stop_type = getattr(stop_reason, "type", None) if stop_reason else None
            if stop_type == "requires_action":
                # Dispatch all pending custom tool calls with stub responses.
                event_ids = list(getattr(stop_reason, "event_ids", None) or [])
                if not event_ids:
                    event_ids = list(pending_custom_tools.keys())
                if not event_ids:
                    print(f"\n[{label}] requires_action but no event ids — breaking")
                    break
                for eid in event_ids:
                    info = pending_custom_tools.pop(eid, {"name": "?", "input": {}})
                    custom_tool_call_count += 1
                    if custom_tool_call_count > max_custom_tool_calls:
                        print(f"\n[{label}] max custom tool calls hit — breaking")
                        await client.beta.sessions.events.send(
                            session_id=session.id,
                            events=[{"type": "user.custom_tool_result",
                                     "custom_tool_use_id": eid,
                                     "content": [{"type": "text", "text": '{"ok": false, "stop": true}'}]}],
                        )
                        break
                    stub = _stub_custom_tool_result(info["name"], info["input"])
                    print(f"\n[{label}] → stubbing {info['name']!r} with {stub[:80]}", flush=True)
                    await client.beta.sessions.events.send(
                        session_id=session.id,
                        events=[{"type": "user.custom_tool_result",
                                 "custom_tool_use_id": eid,
                                 "content": [{"type": "text", "text": stub}]}],
                    )
                continue  # session will resume; keep streaming
            else:
                print(f"\n--- {label} idle, stop_reason={stop_type} ---")
                break
        elif etype == "session.status_terminated":
            print(f"\n--- {label} terminated ---")
            break
        elif etype == "session.error":
            print(f"\n--- {label} session error: {event} ---")
            break
        if event_count > 400:
            print(f"\n--- {label} safety break (400 events) ---")
            break

    print(f"\n[{label}] tools used: {tool_uses}")
    print(f"[{label}] used fs tools: {[t for t in tool_uses if t in fs_tools]}")
    return session.id, tool_uses, "".join(text_chunks)


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

    # Fresh repair_id every run guarantees a virgin scribe store.
    repair_id = f"smoke-multi-{int(time.time())}"
    print(f"=== Multi-session scribe test ===")
    print(f"repair_id : {repair_id}")
    print(f"planted   : {PLANTED_TOKEN}")

    patterns_id = await ensure_global_store(
        client, kind="patterns", description="patterns",
    )
    playbooks_id = await ensure_global_store(
        client, kind="playbooks", description="playbooks",
    )
    device_id = await ensure_memory_store(client, "iphone-x")
    repair_store_id = await ensure_repair_store(
        client, device_slug="iphone-x", repair_id=repair_id,
    )

    print("\nStores:")
    for label, sid in [
        ("patterns", patterns_id),
        ("playbooks", playbooks_id),
        ("device-iphone-x", device_id),
        (f"repair-{repair_id}", repair_store_id),
    ]:
        print(f"  {label:35s} {sid}")

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
         "prompt": "Scratch notebook for THIS repair. Read state.md at session "
                   "start to orient yourself; write state.md / decisions/{ts}.md "
                   "as the diagnosis progresses."},
    ]

    agent = ids["agents"]["fast"]
    env_id = ids["environment_id"]

    # ---------------- SESSION 1: agent writes the scribe mount ----------------
    s1_kickoff = (
        "Salut. iphone-x sur le banc, plainte: ne s'allume pas, écran reste noir. "
        "Mon hypothèse principale après une diode-mode rapide est que C156 est "
        f"shorté plaque-à-plaque (rail VDD_MAIN à ~0Ω, code de tracking interne {PLANTED_TOKEN}). "
        "Avant que je parte chercher du matériel, écris immédiatement dans ton mount "
        "repair (`/mnt/memory/microsolder-repair-iphone-x-{repair_id}/`) un `state.md` "
        "avec ton snapshot, ET un `decisions/initial.md` justifiant pourquoi tu te "
        f"focus sur C156 + le tracking code {PLANTED_TOKEN}. Liste les outils utilisés "
        "(read/write/edit/glob). Court et structuré."
    ).replace("{repair_id}", repair_id)

    s1_id, s1_tools, s1_text = await _run_session(
        client=client, agent=agent, env_id=env_id, resources=resources,
        title=f"smoke-multi S1 {repair_id}",
        kickoff=s1_kickoff, label="S1",
    )

    s1_wrote = any(t in {"write", "edit"} for t in s1_tools)
    print(f"\n[S1] wrote to scribe mount: {s1_wrote}")
    if not s1_wrote:
        print("⚠️  S1 did not write — session 2 will have nothing to read.")
        # Don't fail yet; session 2 might still try to read

    # ---------------- SESSION 2: NEW session, same repair_id ----------------
    print("\n" + "=" * 60)
    print("Opening SESSION 2 on same repair_id (no context handoff)…")
    print("=" * 60)

    # In production, the runtime injects a `[ctx · device=… · plainte_init=…]`
    # tag at the head of every user message. Reproduce that here so S2 knows
    # which repair to look for in the mount.
    s2_kickoff = (
        f"[ctx · device=iphone-x · repair_id={repair_id} · "
        "plainte_init=\"ne s'allume pas, écran reste noir\"]\n\n"
        "Salut, je reprends ce repair. Avant de me redire le symptôme: "
        "qu'est-ce qu'on avait conclu la dernière fois? Va lire ton mount "
        "repair (state.md, decisions/) et résume-moi en 3 lignes l'état exact, "
        "notamment quel composant on suspectait et pourquoi. Cite les éléments "
        "exacts que tu trouves (refdes, codes de tracking)."
    )

    s2_id, s2_tools, s2_text = await _run_session(
        client=client, agent=agent, env_id=env_id, resources=resources,
        title=f"smoke-multi S2 {repair_id}",
        kickoff=s2_kickoff, label="S2",
    )

    # ---------------- ASSERTIONS ----------------
    print("\n" + "=" * 60)
    print("RESULT")
    print("=" * 60)

    s2_read_mount = any(t in {"read", "grep", "glob"} for t in s2_tools)
    s2_cited_token = PLANTED_TOKEN in s2_text or "C156" in s2_text

    print(f"  S1 session id:                 {s1_id}")
    print(f"  S2 session id:                 {s2_id}")
    print(f"  S1 wrote scribe mount:         {s1_wrote}")
    print(f"  S2 read mount via fs tools:    {s2_read_mount}")
    print(f"  S2 cited planted content:      {s2_cited_token}")
    print(f"    (looking for {PLANTED_TOKEN!r} or 'C156' in S2 response)")

    if s1_wrote and s2_read_mount and s2_cited_token:
        print("\n✅ PASS: scribe pattern works end-to-end across sessions.")
    elif s2_read_mount and s2_cited_token:
        print("\n✅ PASS (degraded): S2 cited content from the mount even though "
              "S1's write may not have been detected via tool_use events.")
    elif s2_cited_token and not s2_read_mount:
        print("\n⚠️  S2 cited the token but didn't appear to use fs tools — "
              "may have read pre-existing context. Inspect manually.")
        sys.exit(1)
    else:
        print("\n❌ FAIL: scribe round-trip broke — session 2 did not surface "
              "session 1's planted content. The architecture promises continuity; "
              "this is the regression test for that promise.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
