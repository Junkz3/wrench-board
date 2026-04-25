#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
"""Diagnostic — what does MA actually emit when a coordinator delegates?

Goal: verify whether the research-preview multi-agent surface is active
on this API key, by observing the live event stream of a coordinator
session attempting to delegate to a single callable sub-agent.

What we look for:
  - `session.thread_created`         — the spawn signal for a sub-thread
  - `agent.tool_use`                 — built-in delegate (vs custom)
  - `agent.custom_tool_use`          — what tool name(s) the coordinator emits
  - `agent.thread_message_sent`      — coordinator → sub-agent inter-thread
  - `agent.thread_message_received`  — sub-agent → coordinator
  - `session_thread_id` on events    — routing field for replies

If `session.thread_created` shows up, multi-agent is wired natively and
our V2 POC was misusing the dispatch (we should NOT custom_tool_result
the coordinator's delegate calls — those are server-managed).

If only `agent.custom_tool_use` shows up with the callable's *name* as
the tool name and no thread_created, the runtime is wrapping callables
as client-side custom tools — and we DO need to dispatch them, returning
the sub-agent's output as the tool result.

Cleans up at the end (interrupt session + archive 2 agents + the env).
Usage: .venv/bin/python scripts/probe_multi_agent_diagnostic.py
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from anthropic import AsyncAnthropic

from api.config import get_settings

logger = logging.getLogger("microsolder.probe.multi_agent")


def _dump(event: Any, prefix: str = "") -> str:
    """Best-effort JSON dump of an SDK event, falling back to dict / repr."""
    try:
        if hasattr(event, "model_dump"):
            d = event.model_dump(mode="json", exclude_none=False)
        elif hasattr(event, "__dict__"):
            d = {k: str(v) for k, v in event.__dict__.items()}
        else:
            d = {"repr": repr(event)}
        return prefix + json.dumps(d, indent=2, default=str)
    except Exception as exc:  # noqa: BLE001
        return prefix + f"<dump failed: {exc}> repr={event!r}"


async def main() -> None:
    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")

    env = await client.beta.environments.create(
        name=f"probe-multiagent-{run_id}",
        config={"type": "cloud", "networking": {"type": "unrestricted"}},
    )
    print(f"env={env.id}")

    callable_agent = await client.beta.agents.create(
        name="probe-callable",
        model="claude-haiku-4-5",
        system=(
            "You are a sub-agent named probe-callable. When invoked by a "
            "coordinator, reply with the literal text 'pong from callable' "
            "then end your turn. No tools needed."
        ),
    )
    print(f"callable={callable_agent.id} v={callable_agent.version}")

    coordinator = await client.beta.agents.create(
        name="probe-coordinator",
        model="claude-haiku-4-5",
        system=(
            "You are a coordinator. You have one callable sub-agent named "
            "'probe-callable'. When the user asks you to ping it, invoke it "
            "via your delegate tool with the message 'ping' and report back "
            "what it returned."
        ),
        tools=[{"type": "agent_toolset_20260401"}],
        extra_body={
            "callable_agents": [
                {"type": "agent", "id": callable_agent.id,
                 "version": callable_agent.version},
            ],
        },
    )
    print(f"coordinator={coordinator.id} v={coordinator.version}")

    session = await client.beta.sessions.create(
        agent={
            "type": "agent",
            "id": coordinator.id,
            "version": coordinator.version,
        },
        environment_id=env.id,
        title="probe-multi-agent",
    )
    print(f"session={session.id}")

    # Stream BEFORE sending the kickoff (doc gotcha #1).
    stream_ctx = await client.beta.sessions.events.stream(session.id)
    saw_thread_created = False
    seen_types: dict[str, int] = {}
    coordinator_tool_uses: list[tuple[str, str | None]] = []  # (name, thread_id)
    n_events = 0
    max_events = 80

    async with stream_ctx as stream:
        await client.beta.sessions.events.send(
            session.id,
            events=[{
                "type": "user.message",
                "content": [{
                    "type": "text",
                    "text": "Please ping probe-callable using your delegate tool.",
                }],
            }],
        )

        async for event in stream:
            n_events += 1
            etype = getattr(event, "type", None) or "unknown"
            seen_types[etype] = seen_types.get(etype, 0) + 1

            # Always show key events in raw form.
            if etype in (
                "session.thread_created",
                "agent.thread_message_sent",
                "agent.thread_message_received",
                "agent.custom_tool_use",
                "agent.tool_use",
                "session.status_idle",
                "session.error",
            ):
                print(f"\n--- event #{n_events} type={etype} ---")
                print(_dump(event))

            if etype == "agent.custom_tool_use":
                name = getattr(event, "name", None) or "?"
                thread_id = (
                    getattr(event, "session_thread_id", None)
                    or (getattr(event, "model_extra", None) or {}).get("session_thread_id")
                )
                coordinator_tool_uses.append((name, thread_id))

            if etype == "session.thread_created":
                saw_thread_created = True

            # Hard stop conditions.
            if etype == "session.status_idle":
                stop = getattr(event, "stop_reason", None)
                stop_type = getattr(stop, "type", None) if stop else None
                if stop_type == "end_turn":
                    print(f"\n[stop] end_turn after {n_events} events")
                    break
                if stop_type == "requires_action":
                    # We don't dispatch — just observe what's pending and stop
                    # so we can analyze the shape without poisoning the run.
                    eids = (
                        getattr(stop, "event_ids", None)
                        or getattr(getattr(stop, "requires_action", None), "event_ids", None)
                        or []
                    )
                    print(f"\n[stop] requires_action with {len(eids)} pending — bailing")
                    break

            if etype == "session.error":
                print(f"\n[stop] session.error")
                break

            if n_events >= max_events:
                print(f"\n[stop] max_events={max_events}")
                break

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"events seen: {n_events}")
    print(f"event types:")
    for t, n in sorted(seen_types.items(), key=lambda x: -x[1]):
        print(f"  {n:>3}× {t}")
    print(f"\nsession.thread_created seen: {saw_thread_created}")
    print(f"\ncoordinator custom_tool_use names:")
    for name, tid in coordinator_tool_uses:
        print(f"  - {name}  (session_thread_id={tid!r})")
    print()
    if saw_thread_created:
        print("VERDICT: research-preview multi-agent is ACTIVE — sub-threads spawn")
        print("natively. The V2 POC just needs to NOT dispatch the delegate calls")
        print("(those are server-managed) and only dispatch sub-agent custom tools")
        print("with session_thread_id routing.")
    elif coordinator_tool_uses:
        print("VERDICT: no thread_created, but the coordinator IS issuing")
        print("custom_tool_use events. Inspect the dumped events above —")
        print("the tool names tell you whether the runtime exposes")
        print("callable_agents as client-side custom tools (we need to")
        print("dispatch and forward to delegate via API), or as something")
        print("else entirely.")
    else:
        print("VERDICT: nothing tool-related happened. The coordinator may have")
        print("just answered with text, suggesting it has no delegate primitive")
        print("at all.")

    # Cleanup
    print("\ncleanup...")
    try:
        await client.beta.sessions.events.send(
            session.id, events=[{"type": "user.interrupt"}],
        )
    except Exception:  # noqa: BLE001
        pass
    for aid in (callable_agent.id, coordinator.id):
        try:
            await client.beta.agents.archive(aid)
        except Exception:  # noqa: BLE001
            pass
    print("done.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    asyncio.run(main())
