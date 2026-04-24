# SPDX-License-Identifier: Apache-2.0
"""Benchmark: does Haiku use Managed-Agents memory stores effectively?

Scenario — two sequential repairs on the same device, run twice:
  condition A = memory store attached (read_write)
  condition B = no memory store (baseline)

Repair 1 teaches a confirmed finding (U2 short on PP_VDD_MAIN).
Repair 2 opens a fresh session on the same device with a related but
distinct symptom, and asks the agent if it sees a link to past cases.

For each turn the script records token usage (per MA's span events),
cost, and every tool invocation — distinguishing built-in `memory_*`
tools (emitted as `agent.tool_use`) from our custom `mb_*`/`bv_*` tools
(`agent.custom_tool_use`). Prints a summary table at the end.

The custom-tool dispatcher is intentionally minimal: mb_get_* returns
stubs, mb_record_finding returns ok. We are measuring the agent's
memory behaviour, not the pack quality.

Run: `.venv/bin/python scripts/bench_memory.py`
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from anthropic import AsyncAnthropic

from api.agent.field_reports import list_field_reports, record_field_report
from api.agent.managed_ids import get_agent, load_managed_ids
from api.agent.memory_stores import ensure_memory_store
from api.agent.pricing import compute_turn_cost
from api.config import get_settings

logger = logging.getLogger("bench.memory")

PACK_SOURCE = Path(__file__).resolve().parent.parent / "memory" / "mnt-reform-motherboard"
PACK_SEED_FILES = (
    "registry.json",
    "knowledge_graph.json",
    "rules.json",
    "dictionary.json",
)

# 8 short repair scenarios matching plausible MNT Reform motherboard symptoms.
# Each one is a single-turn prompt that forces the agent to consult the pack
# (registry to resolve refdes, rules to pick a diagnostic path). That's exactly
# the kind of query that either reads the mount (A) or relies on the injected
# pack context (B).
BENCH_REPAIRS: list[list[str]] = [
    [
        "Salut, MNT Reform sur le banc. La machine ne boot pas du tout, "
        "LED de statut fixe, CPU SOM froid. Par quoi je commence ?"
    ],
    [
        "Nouveau MNT Reform : le port USB-C supérieur ne négocie pas la PD. "
        "Charge 5V seulement. Quels composants sur le chemin PD je dois "
        "vérifier d'abord ?"
    ],
    [
        "MNT Reform, clavier interne répond pas du tout mais le système boote "
        "normalement. Tu suggères quoi à inspecter en priorité ?"
    ],
    [
        "J'ai un MNT Reform qui ne voit pas son slot M.2 NVMe. Autres ports "
        "fonctionnent. Je cherche quoi ?"
    ],
    [
        "MNT Reform : le port HDMI sort une image glitchée, pixels qui "
        "scintillent. Quel ordre de mesures tu recommandes ?"
    ],
    [
        "Pas de son sur le jack 3.5mm du MNT Reform. Audio OK via USB. "
        "Quel composant audio je check en priorité ?"
    ],
    [
        "MNT Reform, le RTC perd l'heure à chaque cold boot même avec la pile "
        "neuve. Tu vois un suspect immédiat ?"
    ],
    [
        "MNT Reform, ventilateur tourne à fond dès le boot et ne baisse pas, "
        "mais pas de message thermal dans dmesg. D'où ça peut venir ?"
    ],
]


def build_pack_context_block(pack_dir: Path) -> str:
    """Return the 4 seedable pack files concatenated as a markdown block,
    ready to prepend to a condition-B user message. This mimics what
    `build_session_intro` would do if it pushed the full pack into the
    prompt for every session (the no-memory baseline scenario)."""
    chunks: list[str] = ["<device_context>"]
    for name in PACK_SEED_FILES:
        path = pack_dir / name
        if not path.exists():
            continue
        chunks.append(f"\n## {name}\n\n```json\n{path.read_text(encoding='utf-8')}\n```\n")
    chunks.append("</device_context>")
    return "".join(chunks)


async def seed_store_with_pack(
    client: AsyncAnthropic,
    *,
    store_id: str,
    pack_dir: Path,
) -> dict[str, str]:
    """Upsert the 4 pack artefacts into the store under /knowledge/*.
    Mirrors memory_seed.seed_memory_store_from_pack but scoped to bench use."""
    from api.agent.memory_stores import upsert_memory

    status: dict[str, str] = {}
    for name in PACK_SEED_FILES:
        src = pack_dir / name
        if not src.exists():
            status[f"/knowledge/{name}"] = "missing"
            continue
        content = src.read_text(encoding="utf-8")
        sha = await upsert_memory(
            client,
            store_id=store_id,
            path=f"/knowledge/{name}",
            content=content,
        )
        status[f"/knowledge/{name}"] = "seeded" if sha else "failed"
    return status


@dataclass
class TurnMetrics:
    turn_index: int
    user_text: str = ""
    agent_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: float = 0.0
    memory_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    custom_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    duration_sec: float = 0.0
    _started_at: float = 0.0


@dataclass
class RepairResult:
    repair_id: str
    condition: str  # "memory_on" | "memory_off"
    turns: list[TurnMetrics] = field(default_factory=list)

    @property
    def total_cost(self) -> float:
        return round(sum(t.cost_usd for t in self.turns), 6)

    @property
    def total_input(self) -> int:
        return sum(
            t.input_tokens + t.cache_read_input_tokens + t.cache_creation_input_tokens
            for t in self.turns
        )

    @property
    def total_output(self) -> int:
        return sum(t.output_tokens for t in self.turns)

    @property
    def memory_tool_count(self) -> int:
        return sum(len(t.memory_tool_calls) for t in self.turns)

    @property
    def custom_tool_count(self) -> int:
        return sum(len(t.custom_tool_calls) for t in self.turns)

    @property
    def duration_sec(self) -> float:
        return round(sum(t.duration_sec for t in self.turns), 2)


async def dispatch_custom_tool(
    name: str,
    payload: dict,
    *,
    client: AsyncAnthropic,
    device_slug: str,
    session_id: str | None,
) -> dict:
    """Dispatcher for the bench. The two memory-sensitive tools are wired
    to their real implementations so the store actually fills up between
    repairs — everything else returns a minimal stub (we're measuring
    memory behaviour, not pack quality)."""
    if name == "mb_list_findings":
        # Real list — reads memory/{slug}/field_reports/*.md on disk.
        reports = list_field_reports(
            device_slug=device_slug,
            limit=int(payload.get("limit", 20)),
            filter_refdes=payload.get("filter_refdes"),
        )
        return {"findings": reports, "device_slug": device_slug}
    if name == "mb_record_finding":
        # Real write — JSON on disk, plus MA mirror when ma_memory_store_enabled
        # AND the device's memstore exists (condition A only, because ensure_
        # memory_store was only called for slug_a).
        result = await record_field_report(
            client=client,
            device_slug=device_slug,
            refdes=payload.get("refdes", ""),
            symptom=payload.get("symptom", ""),
            confirmed_cause=payload.get("confirmed_cause", ""),
            mechanism=payload.get("mechanism"),
            notes=payload.get("notes"),
            session_id=session_id,
        )
        result["ok"] = True
        return result
    if name == "mb_get_rules_for_symptoms":
        return {"rules": [], "symptoms": payload.get("symptoms", [])}
    if name == "mb_get_component":
        return {"found": False, "refdes": payload.get("refdes", "?"), "reason": "bench stub"}
    if name.startswith("bv_") or name.startswith("mb_") or name.startswith("profile_"):
        return {"ok": True, "note": "bench stub"}
    return {"ok": False, "reason": f"unknown tool {name}"}


async def run_repair(
    client: AsyncAnthropic,
    *,
    agent_info: dict,
    environment_id: str,
    device_slug: str,
    memory_store_id: str | None,
    user_script: list[str],
    condition: str,
    repair_index: int,
) -> RepairResult:
    session_kwargs: dict[str, Any] = {
        "agent": {
            "type": "agent",
            "id": agent_info["id"],
            "version": agent_info["version"],
        },
        "environment_id": environment_id,
        "title": f"bench-{condition}-repair{repair_index}",
    }
    if memory_store_id:
        session_kwargs["resources"] = [
            {
                "type": "memory_store",
                "memory_store_id": memory_store_id,
                "access": "read_write",
                "prompt": (
                    "Historique de réparations pour ce device. Consulte-moi "
                    "avant chaque hypothèse, écris les findings confirmés après."
                ),
            }
        ]

    session = await client.beta.sessions.create(**session_kwargs)
    print(f"    session={session.id}")

    result = RepairResult(
        repair_id=f"bench-repair-{repair_index}", condition=condition
    )
    events_by_id: dict[str, Any] = {}
    dispatched_ids: set[str] = set()
    seen_event_types: dict[str, int] = {}
    current_turn: TurnMetrics | None = None
    user_idx = 0

    def new_turn(user_text: str) -> TurnMetrics:
        t = TurnMetrics(
            turn_index=len(result.turns), user_text=user_text, _started_at=time.monotonic()
        )
        result.turns.append(t)
        return t

    def close_turn(t: TurnMetrics | None) -> None:
        if t is not None and t._started_at:
            t.duration_sec = round(time.monotonic() - t._started_at, 2)

    async def send_user(text: str) -> None:
        await client.beta.sessions.events.send(
            session.id,
            events=[
                {"type": "user.message", "content": [{"type": "text", "text": text}]}
            ],
        )

    stream_ctx = await client.beta.sessions.events.stream(session.id)
    async with stream_ctx as stream:
        # Send the first user message after the stream opens.
        first_msg = user_script[user_idx]
        print(f"    > turn {user_idx + 1}: {first_msg[:90]}…")
        current_turn = new_turn(first_msg)
        await send_user(first_msg)
        user_idx += 1

        async for event in stream:
            etype = getattr(event, "type", None)
            seen_event_types[etype or "?"] = seen_event_types.get(etype or "?", 0) + 1

            if etype == "agent.message":
                for block in getattr(event, "content", None) or []:
                    if getattr(block, "type", None) == "text" and current_turn:
                        current_turn.agent_text += getattr(block, "text", "") or ""

            elif etype == "agent.tool_use":
                name = getattr(event, "name", None)
                inp = getattr(event, "input", {}) or {}
                if current_turn:
                    current_turn.memory_tool_calls.append({"name": name, "input": inp})
                print(
                    f"    [memory tool] {name}("
                    f"{json.dumps(inp, ensure_ascii=False)[:120]})"
                )

            elif etype == "agent.custom_tool_use":
                events_by_id[event.id] = event
                name = getattr(event, "name", None)
                inp = getattr(event, "input", {}) or {}
                if current_turn:
                    current_turn.custom_tool_calls.append({"name": name, "input": inp})
                print(
                    f"    [custom tool] {name}("
                    f"{json.dumps(inp, ensure_ascii=False)[:120]})"
                )

            elif etype == "span.model_request_end":
                usage = getattr(event, "model_usage", None)
                if usage is not None and current_turn:
                    cost = compute_turn_cost(
                        agent_info.get("model", "claude-haiku-4-5"),
                        input_tokens=getattr(usage, "input_tokens", 0) or 0,
                        output_tokens=getattr(usage, "output_tokens", 0) or 0,
                        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                        cache_creation_input_tokens=getattr(
                            usage, "cache_creation_input_tokens", 0
                        ) or 0,
                    )
                    current_turn.input_tokens += cost["input_tokens"]
                    current_turn.output_tokens += cost["output_tokens"]
                    current_turn.cache_read_input_tokens += cost["cache_read_input_tokens"]
                    current_turn.cache_creation_input_tokens += cost[
                        "cache_creation_input_tokens"
                    ]
                    current_turn.cost_usd += cost["cost_usd"]

            elif etype == "session.status_idle":
                stop = getattr(event, "stop_reason", None)
                stop_type = getattr(stop, "type", None) if stop else None
                if stop_type == "requires_action":
                    event_ids = getattr(stop, "event_ids", None) or []
                    # MA can re-emit the same event id across successive
                    # requires_action pauses if the agent fires off a second
                    # tool_use while we're still dispatching the first. Track
                    # ids we've already answered so we don't send a duplicate
                    # user.custom_tool_result and trigger a 400.
                    pending: list[dict] = []
                    for eid in event_ids:
                        if eid in dispatched_ids:
                            continue
                        tool_event = events_by_id.get(eid)
                        if tool_event is None:
                            continue
                        name = getattr(tool_event, "name", "")
                        payload = getattr(tool_event, "input", {}) or {}
                        r = await dispatch_custom_tool(
                            name,
                            payload,
                            client=client,
                            device_slug=device_slug,
                            session_id=session.id,
                        )
                        dispatched_ids.add(eid)
                        pending.append(
                            {
                                "type": "user.custom_tool_result",
                                "custom_tool_use_id": eid,
                                "content": [
                                    {"type": "text", "text": json.dumps(r, default=str)}
                                ],
                            }
                        )
                    # Batch all fresh tool_results into a single send so the
                    # server validates them together — atomic w.r.t. what it
                    # is currently waiting on.
                    if pending:
                        await client.beta.sessions.events.send(
                            session.id, events=pending
                        )
                elif stop_type == "end_turn":
                    # Turn fully done — advance the user script.
                    close_turn(current_turn)
                    if user_idx < len(user_script):
                        msg = user_script[user_idx]
                        print(f"    > turn {user_idx + 1}: {msg[:90]}…")
                        current_turn = new_turn(msg)
                        await send_user(msg)
                        user_idx += 1
                    else:
                        break
                # Any other status_idle variant (no stop_reason, or intermediate
                # pause between the model call and the tool dispatch) is a
                # transient — do not send a user.message here, the server is
                # still waiting on pending tool_results.

            elif etype == "session.status_terminated":
                close_turn(current_turn)
                break
            elif etype == "session.error":
                err = getattr(event, "error", None)
                msg = getattr(err, "message", None) if err else "unknown"
                print(f"    [session error] {msg}")
                close_turn(current_turn)
                break

    # Best-effort interrupt so a partial tool dance doesn't leave the session
    # stuck when we reopen for repair 2.
    try:
        await client.beta.sessions.events.send(
            session.id, events=[{"type": "user.interrupt"}]
        )
    except Exception:  # noqa: BLE001
        pass

    # Dump the tally of event types so we can spot anything memory-related
    # that rides on a channel other than agent.tool_use / custom_tool_use.
    print(f"    event types: {dict(sorted(seen_event_types.items()))}")
    return result


def _fmt_row(label: str, cost: float, in_tok: int, out_tok: int, mem: int, cus: int) -> str:
    return (
        f"{label:<12} | ${cost:>10.6f} | {in_tok:>10} in | {out_tok:>8} out | "
        f"memory_*: {mem:>3} | custom_*: {cus:>3}"
    )


async def _delete_store(api_key: str, store_id: str) -> int:
    async with httpx.AsyncClient(timeout=30.0) as http:
        resp = await http.delete(
            f"https://api.anthropic.com/v1/memory_stores/{store_id}",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "managed-agents-2026-04-01",
            },
        )
    return resp.status_code


async def _list_store_memories(api_key: str, store_id: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30.0) as http:
        resp = await http.get(
            f"https://api.anthropic.com/v1/memory_stores/{store_id}/memories",
            params={"path_prefix": "/"},
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "managed-agents-2026-04-01",
            },
        )
    if resp.status_code != 200:
        return []
    return resp.json().get("data", []) or []


@dataclass
class RunAggregate:
    run_index: int
    on_cost: float
    off_cost: float
    on_in_tokens: int
    off_in_tokens: int
    on_out_tokens: int
    off_out_tokens: int
    on_memory_calls: int
    off_memory_calls: int
    on_custom_calls: int
    off_custom_calls: int


async def run_one(
    client: AsyncAnthropic,
    *,
    agent_info: dict,
    env_id: str,
    settings: Any,
    run_index: int,
) -> RunAggregate:
    suffix = f"{int(time.time())}-{run_index}"
    slug_a = f"bench-mem-on-{suffix}"
    slug_b = f"bench-mem-off-{suffix}"

    if not PACK_SOURCE.exists():
        raise RuntimeError(
            f"Pack source not found at {PACK_SOURCE}. "
            "This bench needs a populated knowledge pack to run."
        )

    pack_block = build_pack_context_block(PACK_SOURCE)
    print(f"[run{run_index}] pack injected size: {len(pack_block)} chars")

    # ----- Condition A: memory ON, store pre-seeded with pack --------------
    print(f"[run{run_index}] === A: memory ON (slug={slug_a}) ===")
    store_id = await ensure_memory_store(client, slug_a)
    if not store_id:
        raise RuntimeError("ensure_memory_store returned None — aborting")
    print(f"[run{run_index}]     store: {store_id}")

    seed_status = await seed_store_with_pack(
        client, store_id=store_id, pack_dir=PACK_SOURCE
    )
    print(f"[run{run_index}]     seed status: {seed_status}")

    results_a: list[RepairResult] = []
    for idx, script in enumerate(BENCH_REPAIRS, start=1):
        r = await run_repair(
            client,
            agent_info=agent_info,
            environment_id=env_id,
            device_slug=slug_a,
            memory_store_id=store_id,
            user_script=script,
            condition="memory_on",
            repair_index=idx,
        )
        results_a.append(r)
        print(
            f"[run{run_index}]     A repair {idx}/{len(BENCH_REPAIRS)}: "
            f"turns={len(r.turns)} cost=${r.total_cost:.6f} "
            f"memory_*={r.memory_tool_count} custom_*={r.custom_tool_count}"
        )

    # ----- Condition B: memory OFF, pack injected in first user message ----
    print(f"[run{run_index}] === B: memory OFF, pack-in-prompt (slug={slug_b}) ===")
    results_b: list[RepairResult] = []
    for idx, script in enumerate(BENCH_REPAIRS, start=1):
        # Prefix the first user message with the pack context block — this
        # mirrors what the app would do at session start if memory stores
        # weren't available (inject the full pack so the agent has context).
        scripted = list(script)
        scripted[0] = (
            f"{pack_block}\n\n---\n\n{scripted[0]}"
        )
        r = await run_repair(
            client,
            agent_info=agent_info,
            environment_id=env_id,
            device_slug=slug_b,
            memory_store_id=None,
            user_script=scripted,
            condition="memory_off",
            repair_index=idx,
        )
        results_b.append(r)
        print(
            f"[run{run_index}]     B repair {idx}/{len(BENCH_REPAIRS)}: "
            f"turns={len(r.turns)} cost=${r.total_cost:.6f} "
            f"memory_*={r.memory_tool_count} custom_*={r.custom_tool_count}"
        )

    # ----- Aggregate -------------------------------------------------------
    on_cost = sum(r.total_cost for r in results_a)
    off_cost = sum(r.total_cost for r in results_b)
    on_in = sum(r.total_input for r in results_a)
    off_in = sum(r.total_input for r in results_b)
    on_out = sum(r.total_output for r in results_a)
    off_out = sum(r.total_output for r in results_b)
    on_dur = sum(r.duration_sec for r in results_a)
    off_dur = sum(r.duration_sec for r in results_b)
    # Session runtime billing: $0.08 per session-hour on the running duration.
    # Our per-turn durations are a close proxy for the "running" status.
    on_runtime = round(on_dur / 3600.0 * 0.08, 6)
    off_runtime = round(off_dur / 3600.0 * 0.08, 6)
    on_mem = sum(r.memory_tool_count for r in results_a)
    off_mem = sum(r.memory_tool_count for r in results_b)
    on_cus = sum(r.custom_tool_count for r in results_a)
    off_cus = sum(r.custom_tool_count for r in results_b)

    print(f"\n[run{run_index}] ========== PER-RUN SUMMARY ==========\n")
    print(
        f"[run{run_index}] ON  : tokens={on_in}+{on_out}, tokens-cost=${on_cost:.6f}, "
        f"runtime {on_dur:.1f}s → ${on_runtime:.6f}, memory_*={on_mem}, custom_*={on_cus}"
    )
    print(
        f"[run{run_index}] OFF : tokens={off_in}+{off_out}, tokens-cost=${off_cost:.6f}, "
        f"runtime {off_dur:.1f}s → ${off_runtime:.6f}, memory_*={off_mem}, custom_*={off_cus}"
    )
    total_on = round(on_cost + on_runtime, 6)
    total_off = round(off_cost + off_runtime, 6)
    diff = round(total_on - total_off, 6)
    pct = (diff / total_off * 100) if total_off else 0.0
    sign = "+" if diff >= 0 else ""
    print(
        f"[run{run_index}] TOTAL (tokens+runtime): ON=${total_on:.6f} OFF=${total_off:.6f} "
        f"Δ={sign}${diff:.6f} ({sign}{pct:.1f}%)"
    )

    # ----- Cleanup ---------------------------------------------------------
    print(f"[run{run_index}] ========== CLEANUP ==========")
    for label, slug, primary_id in (
        ("A", slug_a, store_id),
        ("B", slug_b, None),
    ):
        sid = primary_id
        if sid is None:
            meta = Path(settings.memory_root) / slug / "managed.json"
            if meta.exists():
                try:
                    sid = json.loads(meta.read_text()).get("memory_store_id")
                except json.JSONDecodeError:
                    sid = None
        if sid:
            code = await _delete_store(settings.anthropic_api_key, sid)
            print(f"[run{run_index}] DELETE memstore {sid} ({label}) → {code}")
    for slug in (slug_a, slug_b):
        p = Path(settings.memory_root) / slug
        if p.exists():
            shutil.rmtree(p)
            print(f"[run{run_index}] rm -rf {p}")

    return RunAggregate(
        run_index=run_index,
        on_cost=round(on_cost + on_runtime, 6),
        off_cost=round(off_cost + off_runtime, 6),
        on_in_tokens=on_in,
        off_in_tokens=off_in,
        on_out_tokens=on_out,
        off_out_tokens=off_out,
        on_memory_calls=on_mem,
        off_memory_calls=off_mem,
        on_custom_calls=on_cus,
        off_custom_calls=off_cus,
    )


async def main() -> None:
    import argparse
    import statistics

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--runs", type=int, default=1, help="Number of full benches to run"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="How many runs execute in parallel (bounded by a semaphore).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=settings.anthropic_max_retries)

    ids = load_managed_ids()
    agent_info = dict(get_agent(ids, "fast"))
    agent_info["model"] = agent_info.get("model", "claude-haiku-4-5")
    env_id = ids["environment_id"]
    print(
        f"Using agent: {agent_info['id']} (model={agent_info['model']}, "
        f"version={agent_info['version']})"
    )
    print(f"Environment: {env_id}")
    print(f"Plan: {args.runs} full run(s), concurrency={args.concurrency}\n")

    semaphore = asyncio.Semaphore(max(1, args.concurrency))

    async def guarded(index: int) -> RunAggregate:
        async with semaphore:
            print(f"\n################## RUN {index}/{args.runs} (started) ##################")
            result = await run_one(
                client,
                agent_info=agent_info,
                env_id=env_id,
                settings=settings,
                run_index=index,
            )
            print(f"\n################## RUN {index}/{args.runs} (done) ##################")
            return result

    aggregates: list[RunAggregate] = await asyncio.gather(
        *(guarded(i) for i in range(1, args.runs + 1))
    )
    aggregates = sorted(aggregates, key=lambda a: a.run_index)

    if args.runs > 1:
        print("\n\n################## AGGREGATE ##################\n")
        print(
            f"{'Run':>4} | {'ON $':>10} | {'OFF $':>10} | "
            f"{'Δ $':>9} | {'Δ %':>8} | {'ON mem*':>7} | {'ON cust*':>8} | {'OFF cust*':>9}"
        )
        print("-" * 90)
        for a in aggregates:
            delta = round(a.on_cost - a.off_cost, 6)
            pct = (delta / a.off_cost * 100.0) if a.off_cost else 0.0
            print(
                f"{a.run_index:>4} | ${a.on_cost:>9.6f} | ${a.off_cost:>9.6f} | "
                f"${delta:>+8.6f} | {pct:>+7.1f}% | {a.on_memory_calls:>7} | "
                f"{a.on_custom_calls:>8} | {a.off_custom_calls:>9}"
            )
        print("-" * 90)
        on_costs = [a.on_cost for a in aggregates]
        off_costs = [a.off_cost for a in aggregates]
        on_mean = statistics.mean(on_costs)
        off_mean = statistics.mean(off_costs)
        on_sd = statistics.stdev(on_costs) if len(on_costs) > 1 else 0.0
        off_sd = statistics.stdev(off_costs) if len(off_costs) > 1 else 0.0
        delta_mean = on_mean - off_mean
        delta_pct = (delta_mean / off_mean * 100) if off_mean else 0.0
        print(
            f"{'mean':>4} | ${on_mean:>9.6f} | ${off_mean:>9.6f} | "
            f"${delta_mean:>+8.6f} | {delta_pct:>+7.1f}% |"
        )
        print(
            f"{'sd':>4} | ${on_sd:>9.6f} | ${off_sd:>9.6f} | "
            f"{'':>9} | {'':>8}"
        )
        on_mem_mean = statistics.mean(a.on_memory_calls for a in aggregates)
        print(
            f"\nmemory_* calls in ON: mean={on_mem_mean:.2f} "
            f"(total across runs: {sum(a.on_memory_calls for a in aggregates)})"
        )
        n = len(aggregates)
        if n > 1:
            # 95% CI via t-distribution approximation (for small n use SD directly)
            sem_on = on_sd / (n ** 0.5)
            ci = 1.96 * sem_on
            print(
                f"\n95% CI on ON cost: ${on_mean:.6f} ± ${ci:.6f}  "
                f"(${on_mean - ci:.6f} to ${on_mean + ci:.6f})"
            )
            # Is delta significant?
            if abs(delta_mean) > ci:
                sign = "cheaper" if delta_mean < 0 else "more expensive"
                print(
                    f"→ memory_ON is statistically {sign} than memory_OFF "
                    f"(delta {delta_pct:+.1f}% exceeds the 95% CI)"
                )
            else:
                print(
                    f"→ delta {delta_pct:+.1f}% is WITHIN the 95% CI — "
                    "no statistically significant cost difference"
                )


if __name__ == "__main__":
    asyncio.run(main())
