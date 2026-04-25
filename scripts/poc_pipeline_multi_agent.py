#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
"""POC v2 — Pipeline via Managed-Agents multi-agent (callable_agents).

V1 (`scripts/poc_pipeline_managed_agents.py`) ran the 3 writers + auditor
as four SEPARATE MA sessions each attaching the same memory store. That
came out 2.3× more expensive than the legacy pipeline because each session
had its own cache — zero sharing between cartographe / clinicien /
lexicographe.

V2 puts all four writers under ONE session owned by a coordinator agent
declaring them as `callable_agents` (research-preview surface on
`managed-agents-2026-04-01`). Sub-agents run as session THREADS sharing
the same container filesystem; their model calls go through the session's
shared cache. The hypothesis: once cartographe warms the cache with its
read of `/inputs/`, clinicien and lexicographe hit it at 0.10×.

What's tested:
  coordinator (Opus)
    ├─ delegate → POC-Cartographe (Opus)   — emits submit_knowledge_graph
    ├─ delegate → POC-Clinicien  (Opus)    — emits submit_rules
    ├─ delegate → POC-Lexicographe (Sonnet)— emits submit_dictionary
    ├─ (code seeds /writers/ + /auditor/drift.json to the store mid-session)
    └─ delegate → POC-Auditor (Opus)       — emits submit_audit_verdict

The coordinator signals the two phase transitions via two custom tools
(`phase2_trigger`, `pipeline_done`) that we dispatch to drive the
code-side seeding and the final termination.

Session_thread_id routing: when a sub-agent emits `agent.custom_tool_use`,
the session-level stream carries `session_thread_id` on the event. We
must echo that id on the corresponding `user.custom_tool_result` so the
platform routes the reply back to the right thread. Absent field → reply
came from the coordinator (primary thread).

Usage:
  .venv/bin/python scripts/poc_pipeline_multi_agent.py --slug mnt-reform-motherboard

Artefacts → benchmark/poc_multi_agent_v2/{slug}_{ts}/ — same shape as V1
so `run.json` is directly comparable.

---
## Status on 2026-04-25 — research preview NOT effective yet

First live run hung at Phase 1 with the coordinator hallucinating seven
different delegate-tool names in 30 s (`POC-Cartographe`, `Cartographe`,
`Task`, `delegate`, `invoke_subagent`, `poc_cartographe`, ...). None of
those are real — `agent_toolset_20260401` did not surface a delegate
tool, meaning the callable_agents runtime is not activated on this API
key yet.

An earlier `client.beta.agents.create(..., extra_body={"callable_agents":
[...]})` probe succeeded (HTTP 200) — but that only proves the request
body was accepted at schema level. Runtime activation is a separate flag
that we can only detect behaviorally once a session is running.

Lesson: future research-preview access checks should open a live session
with a coordinator + callable and look for `session.thread_created`
events — not just a passing `agents.create` call.

When Anthropic approves the research-preview request and the delegate
tool actually appears on the stream:
  1. The session will emit `session.thread_created` early in Phase 1.
  2. `agent.custom_tool_use` events for submit_* tools will carry a
     non-null `session_thread_id` — the extractor at
     `_extract_session_thread_id()` already handles both SDK shapes.
  3. `user.custom_tool_result` replies will echo that thread id.
Nothing in this script needs to change once that happens — the code is
structured around the thread-id routing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import BaseModel

from api.config import get_settings
from api.pipeline.auditor import SUBMIT_AUDIT_TOOL_NAME
from api.pipeline.drift import compute_drift
from api.pipeline.prompts import (
    AUDITOR_SYSTEM,
    CARTOGRAPHE_TASK,
    CLINICIEN_TASK,
    LEXICOGRAPHE_TASK,
    WRITER_SYSTEM,
)
from api.pipeline.schemas import (
    AuditVerdict,
    Dictionary,
    DriftItem,
    KnowledgeGraph,
    Registry,
    RulesSet,
)
from api.pipeline.writers import (
    SUBMIT_DICT_TOOL_NAME,
    SUBMIT_KG_TOOL_NAME,
    SUBMIT_RULES_TOOL_NAME,
)

logger = logging.getLogger("microsolder.poc.pipeline_ma_v2")


# ----------------------------------------------------------------------
# Constants, pricing table, cost dataclasses — duplicated from V1 so this
# script stays launchable as a standalone (Python can't cross-import
# `scripts.*` when the target is invoked directly from the filesystem).
# ----------------------------------------------------------------------

MODEL_OPUS = "claude-opus-4-7"
MODEL_SONNET = "claude-sonnet-4-6"

PRICING: dict[str, dict[str, float]] = {
    MODEL_OPUS: {
        "input": 15.0, "output": 75.0,
        "cache_read": 1.5, "cache_write": 18.75,
    },
    MODEL_SONNET: {
        "input": 3.0, "output": 15.0,
        "cache_read": 0.3, "cache_write": 3.75,
    },
}

_MA_ROOT_ALLOWED = frozenset({"type", "properties", "required"})
_MA_NESTED_STRIP = frozenset({"$defs", "additionalProperties"})


def _inline_defs(schema: dict) -> dict:
    """Normalize a Pydantic JSON schema for MA's `tools[*].input_schema`.

    Inline every `$ref` into a copy of its `$defs` target (cycle-safe),
    strip `additionalProperties` at every depth, and at the ROOT keep only
    `type` / `properties` / `required` — MA rejects `description`, `title`,
    or any other metadata at the top level with "Extra inputs are not
    permitted". Identical to V1's helper; kept here so this script is
    self-contained when invoked directly via `python scripts/...`.
    """
    defs = dict(schema.pop("$defs", {}))

    def _walk(node: object, in_flight: frozenset[str]) -> object:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.split("/", 3)[-1]
                if name in in_flight:
                    return {}
                target = defs.get(name)
                if target is None:
                    return {}
                return _walk(dict(target), in_flight | {name})
            return {
                k: _walk(v, in_flight)
                for k, v in node.items()
                if k not in _MA_NESTED_STRIP
            }
        if isinstance(node, list):
            return [_walk(x, in_flight) for x in node]
        return node

    inlined = _walk(schema, frozenset())
    if isinstance(inlined, dict):
        return {k: v for k, v in inlined.items() if k in _MA_ROOT_ALLOWED}
    return inlined  # type: ignore[return-value]


@dataclass
class PhaseCost:
    phase: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    duration_seconds: float = 0.0

    def estimate_usd(self) -> float:
        p = PRICING.get(self.model)
        if not p:
            return 0.0
        return (
            self.input_tokens * p["input"]
            + self.output_tokens * p["output"]
            + self.cache_read_tokens * p["cache_read"]
            + self.cache_creation_tokens * p["cache_write"]
        ) / 1_000_000.0


@dataclass
class RunCosts:
    device_slug: str
    started_at: str
    ended_at: str = ""
    phases: dict[str, PhaseCost] = field(default_factory=dict)

    @property
    def total_input(self) -> int:
        return sum(p.input_tokens for p in self.phases.values())

    @property
    def total_output(self) -> int:
        return sum(p.output_tokens for p in self.phases.values())

    @property
    def total_cache_read(self) -> int:
        return sum(p.cache_read_tokens for p in self.phases.values())

    @property
    def total_cache_write(self) -> int:
        return sum(p.cache_creation_tokens for p in self.phases.values())

    @property
    def total_usd(self) -> float:
        return sum(p.estimate_usd() for p in self.phases.values())


PHASE2_TRIGGER_TOOL = "phase2_trigger"
PIPELINE_DONE_TOOL = "pipeline_done"


# ----------------------------------------------------------------------
# Prompts
# ----------------------------------------------------------------------

_MA_SUB_AGENT_FORCING = """

---
## Running as a Managed Agents sub-agent (callable by a coordinator)

Inputs live under `/mnt/memory/<store_dir>/`. Rules:
  1. Read the files you need with the `read` tool.
  2. Your ONLY valid output is ONE call to the submit_* custom tool below.
     No free-form text before the call.
  3. After submit_* returns "OK", emit a single final message with the
     literal text "DONE" and end the turn. This signals completion to the
     coordinator.
"""


COORDINATOR_SYSTEM = """You coordinate a knowledge-pack pipeline for an electronic-repair workbench.

You have four callable sub-agents in this session:
  - POC-Cartographe (Opus)   — emits knowledge_graph.json via submit_knowledge_graph
  - POC-Clinicien  (Opus)    — emits rules.json           via submit_rules
  - POC-Lexicographe (Sonnet)— emits dictionary.json      via submit_dictionary
  - POC-Auditor    (Opus)    — emits audit_verdict.json   via submit_audit_verdict

A read_write memory store is attached at `/mnt/memory/<store_dir>/`:
  /inputs/raw_research_dump.md   — free-markdown research dump
  /inputs/registry.json          — canonical vocabulary (components + signals)

## Your phases

### Phase 1 — run the three writers in parallel

Delegate to POC-Cartographe, POC-Clinicien, POC-Lexicographe **in parallel**
(fire all three delegate calls in a single assistant turn — do NOT wait for
one to finish before launching the others). Each writer's instruction:
"Read `/mnt/memory/<store_dir>/inputs/raw_research_dump.md` and
`/mnt/memory/<store_dir>/inputs/registry.json`, then emit your schema via
your single submit_* tool. Finish with the literal text DONE."

Wait for all three sub-agents to return "DONE".

### Phase 2 — call phase2_trigger

Once all three writers are done, call the `phase2_trigger` custom tool.
Code outside the session will seed the writer outputs at
`/mnt/memory/<store_dir>/writers/{knowledge_graph,rules,dictionary}.json`
and the deterministic drift report at
`/mnt/memory/<store_dir>/auditor/drift.json`, then reply with "READY".

### Phase 3 — run the auditor

Delegate to POC-Auditor with the instruction: "Read
`/mnt/memory/<store_dir>/inputs/raw_research_dump.md`,
`/mnt/memory/<store_dir>/inputs/registry.json`,
`/mnt/memory/<store_dir>/writers/knowledge_graph.json`,
`/mnt/memory/<store_dir>/writers/rules.json`,
`/mnt/memory/<store_dir>/writers/dictionary.json`,
`/mnt/memory/<store_dir>/auditor/drift.json`, then emit submit_audit_verdict.
Include drift items verbatim under `drift_detected`. Finish with DONE."

Wait for the Auditor to return "DONE".

### Phase 4 — finalize

Call `pipeline_done`, then end the turn with no text.

## Hard rules

- Do NOT produce summaries, commentary, or planning text. Your output
  besides tool calls is minimal ("Phase 1 complete.", "Phase 3 complete.").
- Do NOT delegate more than once to the same sub-agent.
- Do NOT read the input or writer files yourself — only sub-agents do.
- If any sub-agent fails to say DONE, proceed anyway once you see a
  `submit_*` tool call from it (the custom tool dispatch is what actually
  captures the payload).
"""


# ----------------------------------------------------------------------
# Resource helpers (share most of V1's but create() them here so we can
# tweak the surface for sub-agents without mutating V1).
# ----------------------------------------------------------------------


async def ensure_environment(client: AsyncAnthropic, run_id: str) -> Any:
    env = await client.beta.environments.create(
        name=f"poc-pipeline-v2-{run_id}",
        config={"type": "cloud", "networking": {"type": "unrestricted"}},
    )
    logger.info("environment created: %s", env.id)
    return env


async def create_memory_store(
    client: AsyncAnthropic, slug: str, run_id: str
) -> Any:
    store = await client.beta.memory_stores.create(
        name=f"poc-pipeline-v2-{slug}-{run_id}",
        description=(
            "POC v2 per-run store. /inputs/ = raw_dump + registry, "
            "/writers/ = writer outputs (seeded mid-session), "
            "/auditor/drift.json = deterministic drift report."
        ),
    )
    logger.info("memory_store created: %s", store.id)
    return store


async def seed_inputs(client, store_id, *, raw_dump: str, registry: Registry):
    await client.beta.memory_stores.memories.create(
        store_id, path="/inputs/raw_research_dump.md", content=raw_dump,
    )
    await client.beta.memory_stores.memories.create(
        store_id, path="/inputs/registry.json",
        content=registry.model_dump_json(indent=2),
    )


async def seed_writers_and_drift(
    client, store_id, *,
    kg: KnowledgeGraph, rules: RulesSet, dictionary: Dictionary,
    drifts: list[DriftItem],
):
    await asyncio.gather(
        client.beta.memory_stores.memories.create(
            store_id, path="/writers/knowledge_graph.json",
            content=kg.model_dump_json(indent=2),
        ),
        client.beta.memory_stores.memories.create(
            store_id, path="/writers/rules.json",
            content=rules.model_dump_json(indent=2),
        ),
        client.beta.memory_stores.memories.create(
            store_id, path="/writers/dictionary.json",
            content=dictionary.model_dump_json(indent=2),
        ),
        client.beta.memory_stores.memories.create(
            store_id, path="/auditor/drift.json",
            content=json.dumps([d.model_dump() for d in drifts], indent=2),
        ),
    )


def _read_only_toolset() -> dict:
    return {
        "type": "agent_toolset_20260401",
        "default_config": {"enabled": False},
        "configs": [
            {"name": "read", "enabled": True},
            {"name": "grep", "enabled": True},
            {"name": "glob", "enabled": True},
        ],
    }


def _submit_tool(name: str, desc: str, schema: type[BaseModel]) -> dict:
    return {
        "type": "custom",
        "name": name,
        "description": desc,
        "input_schema": _inline_defs(schema.model_json_schema()),
    }


async def create_writer_agent(
    client, *, name: str, model: str, task_suffix: str,
    tool_name: str, tool_description: str, schema_model: type[BaseModel],
) -> Any:
    system = (
        f"{WRITER_SYSTEM}\n\n## Your specific task\n\n{task_suffix}"
        f"{_MA_SUB_AGENT_FORCING}"
    )
    agent = await client.beta.agents.create(
        name=name, model=model, system=system,
        tools=[
            _read_only_toolset(),
            _submit_tool(tool_name, tool_description, schema_model),
        ],
    )
    logger.info("sub-agent %s created (id=%s v=%d)", name, agent.id, agent.version)
    return agent


async def create_auditor_agent(client) -> Any:
    system = AUDITOR_SYSTEM + _MA_SUB_AGENT_FORCING
    agent = await client.beta.agents.create(
        name="POC-Auditor", model=MODEL_OPUS, system=system,
        tools=[
            _read_only_toolset(),
            _submit_tool(
                SUBMIT_AUDIT_TOOL_NAME,
                "Submit the structured audit verdict.",
                AuditVerdict,
            ),
        ],
    )
    logger.info("auditor sub-agent created (id=%s v=%d)", agent.id, agent.version)
    return agent


async def create_coordinator_agent(
    client, *, callables: list[Any],
) -> Any:
    """Create the coordinator. `callable_agents` goes through extra_body
    because anthropic 0.97.0 doesn't expose it as a named kwarg yet.

    Two custom tools are declared beyond the toolset:
      - phase2_trigger: signals "writers done; please seed outputs"
      - pipeline_done:  signals final termination
    """
    callable_payload = [
        {"type": "agent", "id": a.id, "version": a.version}
        for a in callables
    ]
    coord = await client.beta.agents.create(
        name="POC-Coordinator",
        model=MODEL_OPUS,
        system=COORDINATOR_SYSTEM,
        tools=[
            {"type": "agent_toolset_20260401"},
            {
                "type": "custom",
                "name": PHASE2_TRIGGER_TOOL,
                "description": (
                    "Signal that all three writer sub-agents have returned. "
                    "The response will be 'READY' once the writer outputs + "
                    "drift report have been seeded into the memory store."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "type": "custom",
                "name": PIPELINE_DONE_TOOL,
                "description": (
                    "Signal final pipeline termination. After this call, end "
                    "the turn with no further text or tool calls."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        ],
        extra_body={"callable_agents": callable_payload},
    )
    logger.info(
        "coordinator created (id=%s v=%d, %d callables)",
        coord.id, coord.version, len(callable_payload),
    )
    return coord


# ----------------------------------------------------------------------
# Orchestration — one session, custom-tool dispatch with session_thread_id
# routing.
# ----------------------------------------------------------------------


@dataclass
class _Collector:
    """Captures the validated sub-agent outputs as they stream in."""
    kg: KnowledgeGraph | None = None
    rules: RulesSet | None = None
    dictionary: Dictionary | None = None
    verdict: AuditVerdict | None = None

    def complete(self) -> bool:
        return all([self.kg, self.rules, self.dictionary, self.verdict])


async def run_session(
    client: AsyncAnthropic,
    *,
    coordinator: Any,
    environment_id: str,
    memory_store_id: str,
    registry: Registry,
    costs: RunCosts,
) -> tuple[KnowledgeGraph, RulesSet, Dictionary, AuditVerdict, list[DriftItem]]:
    """Drive the whole pipeline through one coordinator session.

    Token accounting goes per `agent_name` reported on
    `span.model_request_end` events (when the SDK exposes it) so we can
    attribute cache reads to cartographe vs. clinicien etc. Absent fallback:
    all usage goes under a single `coordinator_or_unknown` bucket.
    """
    session = await client.beta.sessions.create(
        agent={
            "type": "agent",
            "id": coordinator.id,
            "version": coordinator.version,
        },
        environment_id=environment_id,
        title="poc-v2-coordinator",
        resources=[{
            "type": "memory_store",
            "memory_store_id": memory_store_id,
            "access": "read_only",
            "instructions": (
                "Pipeline knowledge-pack context. /inputs/ = raw dump + "
                "registry. /writers/ gets seeded mid-session by the "
                "orchestrating code. /auditor/drift.json is the drift report."
            ),
        }],
    )
    logger.info("session %s opened (coordinator=%s)", session.id, coordinator.id)

    collector = _Collector()
    events_by_id: dict[str, Any] = {}
    responded: set[str] = set()
    seeding_started = False
    seeded = asyncio.Event()
    seed_task: asyncio.Task | None = None

    # Map sub-agent submit_* tool names to schema + attribute name.
    submit_targets = {
        SUBMIT_KG_TOOL_NAME:    (KnowledgeGraph, "kg"),
        SUBMIT_RULES_TOOL_NAME: (RulesSet, "rules"),
        SUBMIT_DICT_TOOL_NAME:  (Dictionary, "dictionary"),
        SUBMIT_AUDIT_TOOL_NAME: (AuditVerdict, "verdict"),
    }

    async def _seed_on_phase2():
        """Triggered by phase2_trigger — seed writers + drift, then reply."""
        drifts = compute_drift(
            registry=registry,
            knowledge_graph=collector.kg,
            rules=collector.rules,
            dictionary=collector.dictionary,
        )
        logger.info("[phase2] drift items: %d — seeding /writers/", len(drifts))
        await seed_writers_and_drift(
            client, memory_store_id,
            kg=collector.kg, rules=collector.rules,
            dictionary=collector.dictionary, drifts=drifts,
        )
        return drifts

    t_start = time.monotonic()
    drifts_final: list[DriftItem] = []

    coord_cost = PhaseCost(phase="coordinator", model=MODEL_OPUS)
    sub_costs: dict[str, PhaseCost] = {}
    costs.phases["coordinator"] = coord_cost

    def _attribute_span(event) -> None:
        usage = getattr(event, "model_usage", None)
        if usage is None:
            return
        # session_thread_id present → sub-agent; absent → coordinator.
        thread_id = _extract_session_thread_id(event)
        bucket = coord_cost
        if thread_id:
            # Bucket per thread so we can tell cartographe from clinicien
            # once we correlate thread_id to sub-agent by the earliest
            # custom_tool_use that came from it.
            bucket_key = f"subthread_{thread_id[-6:]}"
            bucket = sub_costs.setdefault(
                bucket_key,
                PhaseCost(phase=bucket_key, model="unknown"),
            )
        bucket.input_tokens += getattr(usage, "input_tokens", 0) or 0
        bucket.output_tokens += getattr(usage, "output_tokens", 0) or 0
        bucket.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        bucket.cache_creation_tokens += (
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        )

    stream_ctx = await client.beta.sessions.events.stream(session.id)
    async with stream_ctx as stream:
        await client.beta.sessions.events.send(
            session.id,
            events=[{
                "type": "user.message",
                "content": [{
                    "type": "text",
                    "text": (
                        "Kickoff — run the pipeline as described in your "
                        "system prompt. Start with Phase 1."
                    ),
                }],
            }],
        )

        async for event in stream:
            etype = getattr(event, "type", None)

            if etype == "span.model_request_end":
                _attribute_span(event)

            elif etype == "agent.custom_tool_use":
                events_by_id[event.id] = event
                # Log thread-aware which sub-agent emitted what.
                tid = _extract_session_thread_id(event)
                logger.info(
                    "custom_tool_use id=%s name=%s thread=%s",
                    event.id, getattr(event, "name", "?"),
                    (tid or "primary")[-10:],
                )

            elif etype == "session.status_idle":
                stop = getattr(event, "stop_reason", None)
                stop_type = getattr(stop, "type", None) if stop else None

                if stop_type == "requires_action":
                    event_ids = (
                        getattr(stop, "event_ids", None)
                        or getattr(
                            getattr(stop, "requires_action", None),
                            "event_ids", None,
                        )
                        or []
                    )
                    for eid in event_ids:
                        if eid in responded:
                            continue
                        tool_ev = events_by_id.get(eid)
                        if tool_ev is None:
                            logger.warning("requires_action for uncached eid=%s", eid)
                            continue
                        name = getattr(tool_ev, "name", "")
                        payload = getattr(tool_ev, "input", {}) or {}
                        thread_id = _extract_session_thread_id(tool_ev)

                        ack_text = "OK"
                        if name in submit_targets:
                            schema_cls, attr = submit_targets[name]
                            try:
                                validated = schema_cls.model_validate(payload)
                            except Exception as exc:
                                raise RuntimeError(
                                    f"{name} payload failed validation: {exc}"
                                ) from exc
                            setattr(collector, attr, validated)
                            ack_text = "OK — turn end."
                            logger.info(
                                "captured %s from thread=%s",
                                name, (thread_id or "primary")[-10:],
                            )
                        elif name == PHASE2_TRIGGER_TOOL:
                            # Kick off seeding off-thread so we keep the
                            # stream flowing; reply "READY" as soon as seeded.
                            if not seeding_started:
                                seeding_started = True

                                async def _do_seed():
                                    nonlocal drifts_final
                                    drifts_final = await _seed_on_phase2()
                                    seeded.set()

                                seed_task = asyncio.create_task(_do_seed())
                                await seeded.wait()
                            ack_text = "READY"
                        elif name == PIPELINE_DONE_TOOL:
                            ack_text = "OK — end session."
                        else:
                            logger.warning("unexpected custom tool: %r", name)

                        reply_event = {
                            "type": "user.custom_tool_result",
                            "custom_tool_use_id": eid,
                            "content": [{"type": "text", "text": ack_text}],
                        }
                        # Echo session_thread_id if the originating request
                        # had one (multi-agent routing requirement).
                        if thread_id:
                            reply_event["session_thread_id"] = thread_id
                        await client.beta.sessions.events.send(
                            session.id, events=[reply_event],
                        )
                        responded.add(eid)

                elif stop_type == "end_turn":
                    # Coordinator ended the turn — session is done once the
                    # collector is full. If we get end_turn before we have
                    # everything, keep the stream open (could be waiting on
                    # an earlier sub-agent idle).
                    if collector.complete():
                        break

            elif etype == "session.status_terminated":
                break

            elif etype == "session.error":
                err = getattr(event, "error", None)
                msg = getattr(err, "message", "unknown") if err else "unknown"
                retry = getattr(err, "retry_status", None) if err else None
                retry_type = getattr(retry, "type", None) if retry else None
                if retry_type == "retrying":
                    # Transient — MA auto-retries. Log and keep streaming;
                    # the next span.model_request_end will attribute usage
                    # to the retried inference.
                    logger.warning("session.error (retrying): %s", msg)
                    continue
                raise RuntimeError(f"session.error: {msg}")

            elif etype == "session.status_rescheduled":
                logger.warning("session rescheduled (transient) — waiting")
                continue

    if seed_task is not None:
        await seed_task

    coord_cost.duration_seconds = time.monotonic() - t_start
    # Flush sub-thread buckets into the main cost map.
    for name, pc in sub_costs.items():
        costs.phases[name] = pc

    if not collector.complete():
        raise RuntimeError(
            f"session ended incomplete: kg={bool(collector.kg)} "
            f"rules={bool(collector.rules)} dict={bool(collector.dictionary)} "
            f"verdict={bool(collector.verdict)}"
        )

    return (
        collector.kg, collector.rules, collector.dictionary,
        collector.verdict, drifts_final,
    )


def _extract_session_thread_id(event: Any) -> str | None:
    """SDK 0.97 doesn't model `session_thread_id` as a first-class attribute.

    Look it up on the event object directly (some versions), or peek into
    `model_extra` (Pydantic's bucket for unknown fields).
    """
    direct = getattr(event, "session_thread_id", None)
    if isinstance(direct, str) and direct:
        return direct
    extra = getattr(event, "model_extra", None) or {}
    if isinstance(extra, dict):
        v = extra.get("session_thread_id")
        if isinstance(v, str) and v:
            return v
    return None


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------


def print_report(costs: RunCosts, out_dir: Path, slug: str, legacy_dir: Path) -> None:
    lines = [
        "",
        "=" * 90,
        f"POC v2 (multi-agent) pipeline — {slug}",
        "=" * 90,
        f"Artefacts: {out_dir}",
        "",
        f"{'Phase':<24} {'Model':<20} {'in':>8} {'out':>8} "
        f"{'cacheR':>8} {'cacheW':>8} {'dur':>7}  USD",
        "-" * 90,
    ]
    for name, p in costs.phases.items():
        lines.append(
            f"{name:<24} {p.model:<20} {p.input_tokens:>8} "
            f"{p.output_tokens:>8} {p.cache_read_tokens:>8} "
            f"{p.cache_creation_tokens:>8} {p.duration_seconds:>6.1f}s  "
            f"${p.estimate_usd():.4f}"
        )
    lines.append("-" * 90)
    lines.append(
        f"{'TOTAL':<24} {'':<20} {costs.total_input:>8} "
        f"{costs.total_output:>8} {costs.total_cache_read:>8} "
        f"{costs.total_cache_write:>8}  {'':>6}  ${costs.total_usd:.4f}"
    )
    legacy_stats = legacy_dir / "token_stats.json"
    if legacy_stats.exists():
        try:
            data = json.loads(legacy_stats.read_text())
            lines.append("")
            lines.append(f"Legacy for comparison (memory/{slug}/token_stats.json):")
            lines.append(json.dumps(data, indent=2)[:700])
        except Exception:
            pass
    lines.append("")
    print("\n".join(lines))


async def _cleanup(client, *, agent_ids: list[str], store_id: str | None):
    for aid in agent_ids:
        try:
            await client.beta.agents.archive(aid)
        except Exception as exc:
            logger.warning("archive agent %s: %s", aid, exc)
    if store_id:
        try:
            await client.beta.memory_stores.archive(store_id)
        except Exception as exc:
            logger.warning("archive store %s: %s", store_id, exc)


async def run(slug: str, memory_root: Path) -> None:
    settings = get_settings()
    client = AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        max_retries=settings.anthropic_max_retries,
    )

    pack_dir = memory_root / slug
    raw_dump = (pack_dir / "raw_research_dump.md").read_text()
    registry = Registry.model_validate_json(
        (pack_dir / "registry.json").read_text()
    )

    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out_dir = Path("benchmark/poc_multi_agent_v2") / f"{slug}_{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    costs = RunCosts(device_slug=slug, started_at=datetime.now(UTC).isoformat())

    env = await ensure_environment(client, run_id)
    store = await create_memory_store(client, slug, run_id)
    agent_ids: list[str] = []

    try:
        await seed_inputs(client, store.id, raw_dump=raw_dump, registry=registry)

        cart, clin, lex, auditor = await asyncio.gather(
            create_writer_agent(
                client, name="POC-Cartographe", model=MODEL_OPUS,
                task_suffix=CARTOGRAPHE_TASK,
                tool_name=SUBMIT_KG_TOOL_NAME,
                tool_description="Cartographe output — typed knowledge graph.",
                schema_model=KnowledgeGraph,
            ),
            create_writer_agent(
                client, name="POC-Clinicien", model=MODEL_OPUS,
                task_suffix=CLINICIEN_TASK,
                tool_name=SUBMIT_RULES_TOOL_NAME,
                tool_description="Clinicien output — diagnostic rules.",
                schema_model=RulesSet,
            ),
            create_writer_agent(
                client, name="POC-Lexicographe", model=MODEL_SONNET,
                task_suffix=LEXICOGRAPHE_TASK,
                tool_name=SUBMIT_DICT_TOOL_NAME,
                tool_description="Lexicographe output — component sheets.",
                schema_model=Dictionary,
            ),
            create_auditor_agent(client),
        )
        agent_ids.extend([cart.id, clin.id, lex.id, auditor.id])

        coordinator = await create_coordinator_agent(
            client, callables=[cart, clin, lex, auditor],
        )
        agent_ids.append(coordinator.id)

        kg, rules, dictionary, verdict, drifts = await run_session(
            client, coordinator=coordinator,
            environment_id=env.id, memory_store_id=store.id,
            registry=registry, costs=costs,
        )

        (out_dir / "knowledge_graph.json").write_text(kg.model_dump_json(indent=2))
        (out_dir / "rules.json").write_text(rules.model_dump_json(indent=2))
        (out_dir / "dictionary.json").write_text(dictionary.model_dump_json(indent=2))
        (out_dir / "audit_verdict.json").write_text(verdict.model_dump_json(indent=2))

        costs.ended_at = datetime.now(UTC).isoformat()
        (out_dir / "run.json").write_text(json.dumps(
            {
                "device_slug": slug,
                "run_id": run_id,
                "started_at": costs.started_at,
                "ended_at": costs.ended_at,
                "verdict_status": verdict.overall_status,
                "verdict_consistency": verdict.consistency_score,
                "drift_items": len(drifts),
                "totals": {
                    "input_tokens": costs.total_input,
                    "output_tokens": costs.total_output,
                    "cache_read_tokens": costs.total_cache_read,
                    "cache_creation_tokens": costs.total_cache_write,
                    "usd_estimate": round(costs.total_usd, 4),
                },
                "phases": {k: asdict(v) for k, v in costs.phases.items()},
                "resources": {
                    "environment_id": env.id,
                    "memory_store_id": store.id,
                    "agent_ids": agent_ids,
                },
            },
            indent=2,
        ))
        print_report(costs, out_dir, slug, pack_dir)
    finally:
        await _cleanup(client, agent_ids=agent_ids, store_id=store.id)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--slug", required=True)
    p.add_argument("--memory-root", default=None)
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    memory_root = (
        Path(args.memory_root) if args.memory_root
        else Path(get_settings().memory_root)
    )
    asyncio.run(run(args.slug, memory_root))


if __name__ == "__main__":
    main()
