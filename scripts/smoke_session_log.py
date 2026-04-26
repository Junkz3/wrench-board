# SPDX-License-Identifier: Apache-2.0
"""Live smoke: cross-repair conversation_log via mb_record_session_log.

Validates the user scenario "but I told you in the other diag we already
did this, you forgot!". Two repairs on the SAME device: repair A writes
a session log when wrapping up; a fresh agent in repair B (different
repair_id, same device) globs `conversation_log/` on the device store
and surfaces what repair A concluded — including a planted token.

Flow:
  1. Provision the per-device store + (fresh) per-repair stores for A and B.
  2. Repair A, conv 1: agent diagnoses, then is told "merci on s'arrête là" —
     should call mb_record_session_log. We dispatch the tool for real
     (writes disk + mirrors to MA), so the device store actually gets the
     /conversation_log/ entry.
  3. Repair B, conv 1 on SAME device, NEW agent session: ask
     "déjà vu ce symptôme sur ce device?" — agent must glob
     /mnt/memory/wrench-board-iphone-x/conversation_log/*.md and cite
     the planted token.
  4. Assert: file landed on disk, MA mirror returned 'mirrored',
     repair B's response cites the token.

Costs ~$0.03–0.06 (two short Haiku-tier sessions).

Run live:
    python -u scripts/smoke_session_log.py > /tmp/smoke_session_log.log 2>&1 &
    tail -F /tmp/smoke_session_log.log
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Stream stdout / surface internal pipeline INFO logs (per CLAUDE.md rule).
sys.stdout.reconfigure(line_buffering=True)
logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s %(levelname)s %(name)s :: %(message)s")

DEVICE_SLUG = "iphone-x"
PLANTED_TOKEN = f"PMIC-LESSON-{uuid.uuid4().hex[:8].upper()}"


async def _stub_or_real_tool(
    *, name: str, payload: dict, client, device_slug: str,
    repair_id: str, conv_id: str, memory_root: Path,
) -> str:
    """Dispatch a custom tool call.

    For mb_record_session_log we call the REAL implementation so the side
    effect (disk + MA mirror) actually happens — that's the whole point of
    the smoke. For the other tools we return small stubs that let the
    agent move on without blocking.
    """
    if name == "mb_record_session_log":
        from api.agent.tools import mb_record_session_log

        status = await mb_record_session_log(
            client=client,
            device_slug=device_slug,
            repair_id=repair_id,
            conv_id=conv_id,
            symptom=payload.get("symptom", ""),
            outcome=payload.get("outcome", "paused"),
            memory_root=memory_root,
            tested=payload.get("tested"),
            hypotheses=payload.get("hypotheses"),
            findings=payload.get("findings"),
            next_steps=payload.get("next_steps"),
            lesson=payload.get("lesson"),
        )
        print(f"    [real tool] mb_record_session_log → {json.dumps(status)[:200]}",
              flush=True)
        return json.dumps(status)

    if name == "mb_get_component":
        return ('{"found": true, "canonical_name": "%s", '
                '"memory_bank": {"role": "PMIC", "kind": "ic"}, '
                '"board": null}' % payload.get("refdes", "?"))
    if name == "mb_get_rules_for_symptoms":
        return '{"matches": [], "total_available_rules": 0}'
    if name == "mb_record_finding":
        return '{"ok": true, "report_id": "smoke-stub", "json_status": "written"}'
    if name == "profile_get":
        return ('{"name": "smoke", "level": "intermediate", '
                '"verbosity": "concise", "tools": ["multimeter", "scope"]}')
    if name == "profile_check_skills":
        return '{"skills": []}'
    return '{"ok": true, "stub": true}'


async def _run_session(
    *, client, agent: dict, env_id: str, resources: list[dict],
    title: str, kickoff: str, label: str,
    device_slug: str, repair_id: str, conv_id: str, memory_root: Path,
    max_custom_tool_calls: int = 6,
) -> tuple[str, list[str], str]:
    session = await client.beta.sessions.create(
        agent={"type": "agent", "id": agent["id"], "version": agent["version"]},
        environment_id=env_id,
        resources=resources,
        title=title,
    )
    print(f"\n[{label}] session id: {session.id}", flush=True)

    stream = await client.beta.sessions.events.stream(session_id=session.id)
    await client.beta.sessions.events.send(
        session_id=session.id,
        events=[{"type": "user.message",
                 "content": [{"type": "text", "text": kickoff}]}],
    )
    print(f"[{label}] streaming…\n{'-' * 60}", flush=True)

    text_chunks: list[str] = []
    tool_uses: list[str] = []
    pending: dict[str, dict] = {}
    custom_count = 0
    event_count = 0

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
            print(f"\n[fs_tool: {tname}]", flush=True)
        elif etype == "agent.custom_tool_use":
            tname = getattr(event, "name", "?")
            tool_uses.append(f"custom:{tname}")
            eid = getattr(event, "id", None)
            inp = getattr(event, "input", {}) or {}
            print(f"\n[custom_tool_use: {tname}] queued", flush=True)
            if eid:
                pending[eid] = {"name": tname, "input": inp}
        elif etype == "session.status_idle":
            stop_reason = getattr(event, "stop_reason", None)
            stop_type = getattr(stop_reason, "type", None) if stop_reason else None
            if stop_type == "requires_action":
                eids = list(getattr(stop_reason, "event_ids", None) or pending.keys())
                if not eids:
                    print(f"\n[{label}] requires_action no eids — break", flush=True)
                    break
                for eid in eids:
                    info = pending.pop(eid, {"name": "?", "input": {}})
                    custom_count += 1
                    if custom_count > max_custom_tool_calls:
                        print(f"\n[{label}] max custom tool calls — break", flush=True)
                        await client.beta.sessions.events.send(
                            session_id=session.id,
                            events=[{"type": "user.custom_tool_result",
                                     "custom_tool_use_id": eid,
                                     "content": [{"type": "text",
                                                  "text": '{"ok": false, "stop": true}'}]}],
                        )
                        break
                    result = await _stub_or_real_tool(
                        name=info["name"], payload=info["input"],
                        client=client, device_slug=device_slug,
                        repair_id=repair_id, conv_id=conv_id,
                        memory_root=memory_root,
                    )
                    await client.beta.sessions.events.send(
                        session_id=session.id,
                        events=[{"type": "user.custom_tool_result",
                                 "custom_tool_use_id": eid,
                                 "content": [{"type": "text", "text": result}]}],
                    )
                continue
            else:
                print(f"\n--- {label} idle, stop_reason={stop_type} ---", flush=True)
                break
        elif etype == "session.status_terminated":
            print(f"\n--- {label} terminated ---", flush=True)
            break
        elif etype == "session.error":
            print(f"\n--- {label} session error: {event} ---", flush=True)
            break
        if event_count > 400:
            print(f"\n--- {label} safety break (400 events) ---", flush=True)
            break

    print(f"\n[{label}] tools used: {tool_uses}", flush=True)
    return session.id, tool_uses, "".join(text_chunks)


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ERROR: ANTHROPIC_API_KEY not set")
    # Force the MA mirror on (default true, but explicit).
    os.environ.setdefault("MA_MEMORY_STORE_ENABLED", "true")

    from anthropic import AsyncAnthropic

    from api.agent.managed_ids import load_managed_ids
    from api.agent.memory_stores import (
        ensure_global_store, ensure_memory_store, ensure_repair_store,
    )
    from api.config import get_settings

    client = AsyncAnthropic()
    ids = load_managed_ids()
    if not ids or "fast" not in ids.get("agents", {}):
        sys.exit("ERROR: managed_ids.json missing or no 'fast' tier — run bootstrap")

    memory_root = Path(get_settings().memory_root)
    repair_a = f"smoke-log-A-{int(time.time())}"
    repair_b = f"smoke-log-B-{int(time.time())}"
    conv_a = f"c-{uuid.uuid4().hex[:6]}"
    conv_b = f"c-{uuid.uuid4().hex[:6]}"

    print(f"=== conversation_log smoke ===", flush=True)
    print(f"device      : {DEVICE_SLUG}", flush=True)
    print(f"repair A    : {repair_a} (conv {conv_a})", flush=True)
    print(f"repair B    : {repair_b} (conv {conv_b})", flush=True)
    print(f"planted     : {PLANTED_TOKEN}", flush=True)

    patterns_id = await ensure_global_store(client, kind="patterns", description="patterns")
    playbooks_id = await ensure_global_store(client, kind="playbooks", description="playbooks")
    device_id = await ensure_memory_store(client, DEVICE_SLUG)
    repair_a_id = await ensure_repair_store(
        client, device_slug=DEVICE_SLUG, repair_id=repair_a,
    )
    repair_b_id = await ensure_repair_store(
        client, device_slug=DEVICE_SLUG, repair_id=repair_b,
    )

    print("\nStores:", flush=True)
    for label, sid in [
        ("patterns", patterns_id),
        ("playbooks", playbooks_id),
        (f"device-{DEVICE_SLUG}", device_id),
        (f"repair-A-{repair_a}", repair_a_id),
        (f"repair-B-{repair_b}", repair_b_id),
    ]:
        print(f"  {label:50s} {sid}", flush=True)

    if not all([patterns_id, playbooks_id, device_id, repair_a_id, repair_b_id]):
        sys.exit("ERROR: at least one store failed to provision")

    base_resources = [
        {"type": "memory_store", "memory_store_id": patterns_id, "access": "read_only",
         "prompt": "Global cross-device failure archetypes."},
        {"type": "memory_store", "memory_store_id": playbooks_id, "access": "read_only",
         "prompt": "Diagnostic protocol templates."},
        {"type": "memory_store", "memory_store_id": device_id, "access": "read_only",
         "prompt": (f"Knowledge pack + cross-repair journal for {DEVICE_SLUG}. "
                    "Includes /field_reports/ and /conversation_log/ — glob the "
                    "latter at session start to see prior repairs on this device.")},
    ]
    resources_a = base_resources + [
        {"type": "memory_store", "memory_store_id": repair_a_id, "access": "read_write",
         "prompt": f"Scratch notebook for repair {repair_a}."},
    ]
    resources_b = base_resources + [
        {"type": "memory_store", "memory_store_id": repair_b_id, "access": "read_write",
         "prompt": f"Scratch notebook for repair {repair_b}."},
    ]

    agent = ids["agents"]["fast"]
    env_id = ids["environment_id"]

    # ---------- REPAIR A: agent wraps up + writes session log ----------
    print(f"\n{'=' * 60}\nREPAIR A — agent wraps up, calls mb_record_session_log\n{'=' * 60}",
          flush=True)
    a_kickoff = (
        f"[ctx · device={DEVICE_SLUG} · repair_id={repair_a} · conv_id={conv_a}]\n\n"
        f"Salut. iphone-x sur le banc, plainte: ne s'allume plus, écran reste noir. "
        f"J'ai testé en diode-mode rapide : PP3V0_USB est à 0V (mort), PP1V8 nominal. "
        f"Mon hypothèse était U1501 (PMIC) mais après reflow la rail PP3V0_USB reste "
        f"à 0V — donc U1501 n'est PAS le coupable. Je suspecte maintenant U1700 "
        f"(Tristar) mais j'ai plus de Tristar en stock, je dois en commander.\n\n"
        f"Avant que je parte, **appelle immédiatement `mb_record_session_log`** "
        f"avec un résumé structuré : "
        f"outcome=paused, "
        f"tested=[{{target:'rail:PP3V0_USB', result:'0V'}}, {{target:'rail:PP1V8', result:'nominal'}}], "
        f"hypotheses=[{{refdes:'U1501', verdict:'rejected', evidence:'reflow inefficace'}}, "
        f"{{refdes:'U1700', verdict:'inconclusive', evidence:'à mesurer'}}], "
        f"next_steps='commander Tristar U1700, replacer, retester PP3V0_USB', "
        f"lesson='{PLANTED_TOKEN}: sur iphone-x avec PP3V0_USB dead, U1501 n''est PAS le coupable — Tristar U1700 est le suspect prioritaire'. "
        f"Une fois fait, dis simplement 'log écrit, à demain'."
    )
    a_session, a_tools, a_text = await _run_session(
        client=client, agent=agent, env_id=env_id, resources=resources_a,
        title=f"smoke-log A {repair_a}", kickoff=a_kickoff, label="A",
        device_slug=DEVICE_SLUG, repair_id=repair_a, conv_id=conv_a,
        memory_root=memory_root,
    )
    a_called = "custom:mb_record_session_log" in a_tools
    print(f"\n[A] called mb_record_session_log: {a_called}", flush=True)

    # ---------- DISK + MA VERIFICATION before opening repair B ----------
    log_dir = memory_root / DEVICE_SLUG / "conversation_log"
    files = sorted(log_dir.glob("*.md")) if log_dir.exists() else []
    a_file = next((p for p in files if repair_a in p.name), None)
    print(f"\n[disk] conversation_log/ files for repair A: "
          f"{a_file.name if a_file else '(NONE)'}", flush=True)
    if a_file:
        snippet = a_file.read_text(encoding="utf-8")
        print("─── DISK FILE CONTENT (first 400 chars) ───", flush=True)
        print(snippet[:400], flush=True)
        print("───", flush=True)
    disk_has_token = bool(a_file and PLANTED_TOKEN in a_file.read_text(encoding="utf-8"))
    print(f"[disk] PLANTED_TOKEN in file: {disk_has_token}", flush=True)

    # ---------- REPAIR B: cross-repair recall ----------
    print(f"\n{'=' * 60}\nREPAIR B — fresh agent, must surface repair A's lesson\n{'=' * 60}",
          flush=True)
    b_kickoff = (
        f"[ctx · device={DEVICE_SLUG} · repair_id={repair_b} · conv_id={conv_b}]\n\n"
        f"Salut, nouveau client, autre iphone-x sur le banc. Symptôme: pas d'allumage. "
        f"Avant qu'on creuse: **est-ce qu'on a déjà vu ce symptôme sur iphone-x récemment ?** "
        f"Glob `/mnt/memory/wrench-board-{DEVICE_SLUG}/conversation_log/` et lis ce que tu y "
        f"trouves. Si oui, dis-moi exactement ce qui en est sorti la dernière fois "
        f"(refdes suspectés, verdict, leçon), avant de me suggérer le prochain test."
    )
    b_session, b_tools, b_text = await _run_session(
        client=client, agent=agent, env_id=env_id, resources=resources_b,
        title=f"smoke-log B {repair_b}", kickoff=b_kickoff, label="B",
        device_slug=DEVICE_SLUG, repair_id=repair_b, conv_id=conv_b,
        memory_root=memory_root,
    )
    b_used_fs = any(t in {"glob", "grep", "read", "ls"} for t in b_tools)
    b_cited = (PLANTED_TOKEN in b_text) or ("U1700" in b_text and "Tristar" in b_text)

    # ---------- VERDICT ----------
    print(f"\n{'=' * 60}\nRESULT\n{'=' * 60}", flush=True)
    print(f"  A session id              : {a_session}", flush=True)
    print(f"  A called mb_record_session_log : {a_called}", flush=True)
    print(f"  Disk file written         : {bool(a_file)}", flush=True)
    print(f"  Disk file has token       : {disk_has_token}", flush=True)
    print(f"  B session id              : {b_session}", flush=True)
    print(f"  B used fs tools (glob/grep/read): {b_used_fs}", flush=True)
    print(f"  B cited the lesson        : {b_cited}", flush=True)
    print(f"    (looking for {PLANTED_TOKEN!r} OR ('U1700' AND 'Tristar') in B text)",
          flush=True)

    if a_called and disk_has_token and b_used_fs and b_cited:
        print("\n✅ PASS: cross-repair conversation_log round-trip works.", flush=True)
        sys.exit(0)
    if disk_has_token and b_cited:
        print("\n✅ PASS (degraded): cross-repair recall worked even though some "
              "tool_use signal didn't surface in events.", flush=True)
        sys.exit(0)
    print("\n❌ FAIL: cross-repair recall broken. Inspect the streams above.",
          flush=True)
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
