#!/usr/bin/env python3
"""End-to-end benchmark of the diagnostic agent on a frozen scenario set.

Plays each scenario in `benchmark/agent_scenarios.jsonl` against a live
backend in MANAGED mode (the runtime that ships in the demo + counts
toward the Managed-Agents hackathon track). For each scenario the harness:

  1. Creates a repair via POST /pipeline/repairs (re-uses existing pack).
  2. Opens a WS /ws/diagnostic/{slug}?tier=…&repair=… and plays every
     scripted user turn.
  3. Captures every agent message (post-sanitizer text), tool_use, memory
     tool, and turn_cost event.
  4. Scores binary checks against the scenario's `checks` block.
  5. Asks an Opus 4.7 judge for a 0-10 quality rating against the rubric.
  6. Computes scenario_score = 0.7 * mean(binary) + 0.3 * judge/10.
  7. Cleans the ephemeral repair dir (no home-view pollution).

Aggregate score = mean(scenario_score). Output JSON one-line, same shape
convention as scripts/eval_simulator.py so the evolve loop can parse it.

Usage:
    .venv/bin/python -m scripts.eval_diagnostic_agent --tier normal
    .venv/bin/python -m scripts.eval_diagnostic_agent --tier deep --verbose
    .venv/bin/python -m scripts.eval_diagnostic_agent \
        --tier fast --bench benchmark/agent_scenarios.jsonl

Requires: dev server on localhost:8000, ANTHROPIC_API_KEY in env.
The dev server MUST run with DIAGNOSTIC_MODE=managed (the default) and
managed_ids.json must exist (run scripts/bootstrap_managed_agent.py first).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import websockets
from anthropic import Anthropic
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / ".env")

DEFAULT_HOST = "http://localhost:8000"
DEFAULT_BENCH = REPO / "benchmark" / "agent_scenarios.jsonl"
DEFAULT_TIER = "normal"

JUDGE_MODEL = "claude-opus-4-7"
JUDGE_MAX_TOKENS = 800
JUDGE_TIMEOUT_S = 60.0

# Refdes wrapped by the post-hoc sanitizer when the agent invents one
# (api/agent/sanitize.py). We detect them in the post-sanitization text the
# WS sends to the client.
INVENTED_REFDES_RE = re.compile(r"⟨\?[A-Z]{1,3}\d{1,4}⟩")

# Patterns that count as a "specific pin / test point / protocol" mention.
# Liberal on purpose — the rubric values *any* concrete probe target.
SPECIFIC_PROBE_RE = re.compile(
    r"(?:\bpin\s*\d+\b|\bTP\d+\b|\bbv_propose_protocol\b|\bbv_show_pin\b"
    r"|\bdiode[- ]?mode\b|\bcontinuit[éy]\b)",
    re.IGNORECASE,
)

# User-consent tokens for mb_expand_knowledge.
CONSENT_RE = re.compile(r"\b(oui|go|lance|ok|d['’]?accord|vas[- ]?y|yes)\b", re.IGNORECASE)


@dataclass
class TurnRecord:
    """One agent turn: every event captured between two user messages."""
    stage: str
    user_text: str
    assistant_messages: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # {name, input}
    # MA-native fs tools (read / write / grep / glob from agent_toolset_20260401);
    # captured with input so post-processing can classify the touched mount.
    memory_tool_calls: list[dict[str, Any]] = field(default_factory=list)  # {name, input}
    turn_costs: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    wall_seconds: float = 0.0


@dataclass
class ScenarioResult:
    id: str
    device_slug: str
    tier: str
    turns: list[TurnRecord] = field(default_factory=list)
    binary_checks: dict[str, bool] = field(default_factory=dict)
    binary_score: float = 0.0  # mean of binary_checks
    judge_score: float = 0.0   # 0..1 (judge raw 0..10 / 10)
    judge_reasoning: str = ""
    judge_cost_usd: float = 0.0
    final_score: float = 0.0   # 0.7 * binary + 0.3 * judge
    cost_usd: float = 0.0       # token + session-runtime sum
    runtime_seconds: float = 0.0
    error: str | None = None


def _is_tty() -> bool:
    return sys.stdout.isatty()


def _c(s: str, code: str) -> str:
    return f"\x1b[{code}m{s}\x1b[0m" if _is_tty() else s


def _green(s): return _c(s, "32")
def _red(s):   return _c(s, "31")
def _yellow(s):return _c(s, "33")
def _dim(s):   return _c(s, "2")


def _load_scenarios(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class ScenarioFilterError(ValueError):
    """Raised when --scenario-id / --bench-subset / --max-scenarios produce
    no runnable scenarios or reference an unknown id."""


def filter_scenarios(
    scenarios: list[dict],
    *,
    scenario_ids: list[str] | None = None,
    bench_subset: int | None = None,
    max_scenarios: int | None = None,
) -> list[dict]:
    """Filter a loaded scenario list by id / prefix-slice / hard cap.

    Pure function — no IO, no globals, deterministic. Extracted so the CLI
    filter wiring is unit-testable without touching the WS / API layer.

    Rules:
      - `scenario_ids` keeps scenarios whose `id` is in the set, in **source
        file order** (not in argument order — stable bench output even when
        a caller passes ids in arbitrary order). Unknown ids raise
        `ScenarioFilterError`.
      - `bench_subset` keeps the first N scenarios from the file. Must be a
        positive int. Mutually exclusive with `scenario_ids`.
      - `max_scenarios` is a hard cap applied after the above filters. Must
        be a positive int.
      - At least one scenario must remain after filtering, else
        `ScenarioFilterError`.
    """
    if scenario_ids and bench_subset is not None:
        raise ScenarioFilterError(
            "--scenario-id and --bench-subset are mutually exclusive"
        )
    if bench_subset is not None and bench_subset <= 0:
        raise ScenarioFilterError(
            f"--bench-subset must be a positive integer, got {bench_subset}"
        )
    if max_scenarios is not None and max_scenarios <= 0:
        raise ScenarioFilterError(
            f"--max-scenarios must be a positive integer, got {max_scenarios}"
        )

    out = list(scenarios)

    if scenario_ids:
        wanted = list(scenario_ids)
        known = {sc["id"] for sc in scenarios}
        unknown = [sid for sid in wanted if sid not in known]
        if unknown:
            raise ScenarioFilterError(
                f"unknown scenario id(s): {', '.join(unknown)} "
                f"(known: {', '.join(sorted(known))})"
            )
        wanted_set = set(wanted)
        out = [sc for sc in scenarios if sc["id"] in wanted_set]

    if bench_subset is not None:
        out = out[:bench_subset]

    if max_scenarios is not None:
        out = out[:max_scenarios]

    if not out:
        raise ScenarioFilterError(
            "scenario filter produced an empty list — nothing to run"
        )

    return out


async def _create_repair(host: str, slug: str, label: str, complaint: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{host}/pipeline/repairs",
            json={
                "device_label": label,
                "device_slug": slug,
                "symptom": f"eval_diagnostic_agent — {complaint[:80]}",
                "force_rebuild": False,
            },
        )
        resp.raise_for_status()
        return resp.json()["repair_id"]


def _cleanup_repair(slug: str, repair_id: str) -> None:
    """Remove the ephemeral repair dir + metadata file."""
    base = REPO / "memory" / slug / "repairs"
    target = base / repair_id
    meta = base / f"{repair_id}.json"
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    if meta.exists():
        try:
            meta.unlink()
        except OSError:
            pass


async def _drain_until_turn_complete(
    ws, record: TurnRecord, timeout_s: float,
) -> None:
    """Consume WS events for the current turn until `turn_complete` arrives.

    Resilient to history_replay_* events on resume (we shouldn't see any
    on a fresh repair, but the harness tolerates them anyway).
    """
    t0 = time.perf_counter()
    while True:
        if time.perf_counter() - t0 > timeout_s:
            record.error = f"turn_complete timeout after {timeout_s}s"
            raise TimeoutError(record.error)
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
        ev = json.loads(raw)
        t = ev.get("type")
        if t == "message" and ev.get("role") == "assistant":
            text = ev.get("text", "")
            if text:
                record.assistant_messages.append(text)
        elif t == "tool_use":
            record.tool_calls.append(
                {"name": ev.get("name", "?"), "input": ev.get("input", {})}
            )
        elif t == "memory_tool_use":
            record.memory_tool_calls.append(
                {"name": ev.get("name", "?"), "input": ev.get("input", {})}
            )
        elif t == "turn_cost":
            record.turn_costs.append(
                {
                    "model": ev.get("model"),
                    "input_tokens": ev.get("input_tokens", 0),
                    "output_tokens": ev.get("output_tokens", 0),
                    "cache_read_input_tokens": ev.get("cache_read_input_tokens", 0),
                    "cache_creation_input_tokens": ev.get(
                        "cache_creation_input_tokens", 0
                    ),
                    "cost_usd": ev.get("cost_usd", 0.0),
                }
            )
        elif t == "error":
            record.error = ev.get("text") or "unknown WS error"
            return
        elif t == "turn_complete":
            return
        # Ignore protocol_proposed / protocol_updated / etc — they don't
        # affect scoring, but they aren't terminal either.


async def _wait_session_ready(ws, timeout_s: float = 30.0) -> dict:
    t0 = time.perf_counter()
    while True:
        if time.perf_counter() - t0 > timeout_s:
            raise TimeoutError("session_ready timeout")
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
        ev = json.loads(raw)
        if ev.get("type") == "session_ready":
            return ev
        if ev.get("type") in {"history_replay_start", "history_replay_end", "context_loaded"}:
            continue
        if ev.get("type") == "error":
            raise RuntimeError(f"session error: {ev.get('text')}")


def _all_assistant_text(result: ScenarioResult) -> str:
    return "\n".join(
        msg for turn in result.turns for msg in turn.assistant_messages
    )


def _all_tool_names(result: ScenarioResult) -> list[str]:
    return [c["name"] for turn in result.turns for c in turn.tool_calls]


def _all_user_texts(result: ScenarioResult) -> str:
    return "\n".join(turn.user_text for turn in result.turns)


def _compute_binary_checks(result: ScenarioResult, scenario: dict) -> dict[str, bool]:
    checks_spec = scenario.get("checks", {})
    text = _all_assistant_text(result)
    tool_names = set(_all_tool_names(result))
    user_text = _all_user_texts(result)

    out: dict[str, bool] = {}

    # 1. tools_required: every listed tool must appear at least once
    required = set(checks_spec.get("tools_required", []))
    if required:
        out["tools_required_called"] = required.issubset(tool_names)

    # 2. refdes_must_mention_one_of: at least one canonical refdes appears
    expected_refs = checks_spec.get("refdes_must_mention_one_of", [])
    if expected_refs:
        # Substring match in lowercase to be tolerant to formatting (markdown,
        # wrapping, code-fences). Refdes are unique-enough strings that this
        # is safe for our 5 scenarios.
        text_lc = text.lower()
        out["refdes_mentioned"] = any(r.lower() in text_lc for r in expected_refs)

    # 3. no_invented_refdes: the sanitizer wraps invented refdes in ⟨?…⟩
    if checks_spec.get("no_invented_refdes"):
        out["no_invented_refdes"] = INVENTED_REFDES_RE.search(text) is None

    # 4. specific probe target (pin number / TP / protocol tool)
    if checks_spec.get("must_propose_specific_pin_or_protocol_or_test_point"):
        has_protocol_tool = any(
            n in {"bv_propose_protocol", "bv_show_pin"} for n in tool_names
        )
        out["specific_probe_target"] = bool(
            has_protocol_tool or SPECIFIC_PROBE_RE.search(text)
        )

    # 5. forbidden tools without user consent (typically mb_expand_knowledge)
    forbidden = checks_spec.get("tools_forbidden_without_user_consent", [])
    if forbidden:
        # If forbidden tool was called, we need at least one consent token in
        # the user-side text to pass the check. Otherwise (no call), pass.
        called_forbidden = [n for n in tool_names if n in forbidden]
        if not called_forbidden:
            out["consent_respected"] = True
        else:
            out["consent_respected"] = bool(CONSENT_RE.search(user_text))

    # 6. protocol_quality — only applied when scenario declares the block.
    #    Parses the LATEST bv_propose_protocol call's `steps` payload.
    pq = checks_spec.get("protocol_quality")
    if pq is not None:
        protocol_calls = [
            c for turn in result.turns for c in turn.tool_calls
            if c["name"] == "bv_propose_protocol"
        ]
        if not protocol_calls:
            # No protocol → all 6 quality checks fail (high penalty signal)
            out["protocol_proposed"] = False
            out["protocol_step_count_ok"] = False
            out["protocol_has_numeric_step"] = False
            out["protocol_numeric_complete"] = False
            out["protocol_all_targets"] = False
            out["protocol_all_rationales"] = False
        else:
            steps = protocol_calls[-1].get("input", {}).get("steps", []) or []
            n = len(steps)
            min_s = pq.get("min_steps", 3)
            max_s = pq.get("max_steps", 12)
            out["protocol_proposed"] = True
            out["protocol_step_count_ok"] = (min_s <= n <= max_s)
            numeric_steps = [s for s in steps if s.get("type") == "numeric"]
            out["protocol_has_numeric_step"] = len(numeric_steps) >= 1
            # numeric_complete: each numeric step has nominal + unit AND
            # (pass_range OR a tolerance you could derive). We're strict on
            # nominal+unit; pass_range is highly recommended.
            def _numeric_ok(s: dict) -> bool:
                return (
                    s.get("nominal") is not None
                    and s.get("unit")
                    and s.get("pass_range") is not None
                    and isinstance(s.get("pass_range"), list)
                    and len(s.get("pass_range")) == 2
                )
            out["protocol_numeric_complete"] = (
                bool(numeric_steps) and all(_numeric_ok(s) for s in numeric_steps)
            )
            # ≥ 80% of steps must have a non-empty target (refdes / TP / net)
            with_target = sum(1 for s in steps if s.get("target"))
            out["protocol_all_targets"] = (n > 0 and (with_target / n) >= 0.8)
            # Every step must have a non-empty rationale string
            out["protocol_all_rationales"] = bool(steps) and all(
                isinstance(s.get("rationale"), str) and s.get("rationale", "").strip()
                for s in steps
            )

    return out


def _compose_judge_prompt(scenario: dict, result: ScenarioResult) -> str:
    transcript_blocks: list[str] = []
    for i, turn in enumerate(result.turns):
        transcript_blocks.append(f"### Turn {i+1} — user")
        transcript_blocks.append(turn.user_text)
        for j, call in enumerate(turn.tool_calls):
            args = json.dumps(call.get("input", {}), ensure_ascii=False)
            # Don't truncate bv_propose_protocol — the judge needs the full
            # steps payload to evaluate protocol coherence.
            cap = 4000 if call["name"] == "bv_propose_protocol" else 280
            if len(args) > cap:
                args = args[:cap] + "…"
            transcript_blocks.append(
                f"[tool_use {j+1}] {call['name']}({args})"
            )
        for k, msg in enumerate(turn.assistant_messages):
            transcript_blocks.append(f"### Turn {i+1} — agent message {k+1}")
            transcript_blocks.append(msg)
    transcript = "\n\n".join(transcript_blocks) or "(no transcript)"

    return f"""You are a senior board-level repair expert grading the quality of a diagnostic agent's session against a known-good rubric.

## Scenario
- Device: {scenario['device_slug']}
- Initial complaint: {scenario['initial_complaint']}

## Rubric (what a great agent does for this scenario)
{scenario['judge_rubric']}

## Full transcript (every user turn + every tool call + every agent reply, post-sanitizer)
{transcript}

## Your task
Rate the agent's diagnostic quality on a 0-10 integer scale where:
- 0-2 = mostly wrong (invented refdes, generic boilerplate, missed the obvious)
- 3-5 = on the right track but vague (no specific probe target, missed nuance)
- 6-8 = correct identification + concrete next step (probe / measurement / cap candidate)
- 9-10 = textbook: identified the prime suspect, proposed a precise discriminating measurement, used the right tools

Rate based ONLY on the rubric — do not penalize for missing tools the agent did not have, do not reward for unnecessary verbosity.

Output strictly in this JSON shape, nothing else:

{{"score": <integer 0-10>, "reasoning": "<one sentence>"}}"""


def _judge(client: Anthropic, scenario: dict, result: ScenarioResult) -> tuple[float, str, float]:
    """Return (judge_0_to_1, reasoning, cost_usd)."""
    prompt = _compose_judge_prompt(scenario, result)
    try:
        resp = client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=JUDGE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
            timeout=JUDGE_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        return 0.0, f"judge_call_failed: {type(exc).__name__}: {exc}", 0.0

    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    # Strip optional ```json fences
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(raw)
        score = float(parsed.get("score", 0))
        score = max(0.0, min(10.0, score))
        reasoning = str(parsed.get("reasoning", ""))[:400]
    except Exception:
        # Defensive fallback: try to grep an integer 0-10 from the response
        m = re.search(r'"score"\s*:\s*(\d+)', raw) or re.search(r"\b([0-9]|10)\b", raw)
        score = float(m.group(1)) if m else 0.0
        reasoning = f"parse_fallback: {raw[:200]}"

    # Cost: usage is on the response object
    usage = resp.usage
    from api.agent.pricing import compute_turn_cost
    cost = compute_turn_cost(
        JUDGE_MODEL,
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )
    return score / 10.0, reasoning, cost["cost_usd"]


async def _play_scenario(
    host: str, tier: str, scenario: dict, judge_client: Anthropic, verbose: bool,
) -> ScenarioResult:
    slug = scenario["device_slug"]
    label = scenario.get("device_label", slug)
    sid = scenario["id"]
    result = ScenarioResult(id=sid, device_slug=slug, tier=tier)

    repair_id: str | None = None
    t_scenario_start = time.perf_counter()
    try:
        repair_id = await _create_repair(host, slug, label, scenario["initial_complaint"])
        if verbose:
            print(_dim(f"  · repair {repair_id}"))

        ws_url = (
            host.replace("http://", "ws://").replace("https://", "wss://")
            + f"/ws/diagnostic/{slug}?tier={tier}&repair={repair_id}"
        )

        async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
            await _wait_session_ready(ws)

            all_turns = [{"text": scenario["initial_complaint"], "stage": "initial"}]
            all_turns.extend(
                {"text": t["text"], "stage": t.get("stage", f"turn_{i+1}")}
                for i, t in enumerate(scenario.get("turns", []))
            )

            for turn_def in all_turns:
                rec = TurnRecord(stage=turn_def["stage"], user_text=turn_def["text"])
                t0 = time.perf_counter()
                await ws.send(json.dumps({"type": "message", "text": turn_def["text"]}))
                try:
                    await _drain_until_turn_complete(ws, rec, timeout_s=180.0)
                except TimeoutError as exc:
                    rec.error = str(exc)
                rec.wall_seconds = time.perf_counter() - t0
                result.turns.append(rec)
                if rec.error:
                    result.error = f"turn[{rec.stage}]: {rec.error}"
                    break
                if verbose:
                    n_tools = len(rec.tool_calls)
                    n_msgs = len(rec.assistant_messages)
                    cost = sum(c["cost_usd"] for c in rec.turn_costs)
                    print(
                        _dim(
                            f"  · {rec.stage}: {rec.wall_seconds:.1f}s "
                            f"· {n_tools} tools · {n_msgs} msgs · ${cost:.4f}"
                        )
                    )
    except Exception as exc:  # noqa: BLE001
        result.error = result.error or f"{type(exc).__name__}: {exc}"

    result.runtime_seconds = time.perf_counter() - t_scenario_start

    # Cleanup ephemeral repair dir
    if repair_id:
        _cleanup_repair(slug, repair_id)

    if result.error:
        # Skip judge entirely on hard errors — score stays 0
        result.binary_checks = _compute_binary_checks(result, scenario)
        if result.binary_checks:
            result.binary_score = sum(result.binary_checks.values()) / len(result.binary_checks)
        result.final_score = 0.0
        return result

    # Binary checks
    result.binary_checks = _compute_binary_checks(result, scenario)
    if result.binary_checks:
        result.binary_score = sum(result.binary_checks.values()) / len(result.binary_checks)

    # LLM judge
    j_score, j_reasoning, j_cost = _judge(judge_client, scenario, result)
    result.judge_score = j_score
    result.judge_reasoning = j_reasoning
    result.judge_cost_usd = j_cost

    # Final score: 0.7 binary + 0.3 judge
    result.final_score = 0.7 * result.binary_score + 0.3 * result.judge_score

    # Aggregate cost: token cost from agent + judge + session-runtime estimate
    agent_token_cost = sum(
        c["cost_usd"] for turn in result.turns for c in turn.turn_costs
    )
    # Session runtime: $0.08/h × wall_seconds-ish.
    # MA bills only `running` state; idle (waiting on user) is free. We have
    # no exact runtime_ms here, so we approximate as the sum of per-turn
    # wall_seconds (each turn = active running + tool-dispatch idle,
    # over-estimates slightly — fine, runtime cost is dwarfed by tokens).
    active_seconds = sum(t.wall_seconds for t in result.turns)
    session_runtime_cost = 0.08 * active_seconds / 3600.0

    result.cost_usd = round(agent_token_cost + j_cost + session_runtime_cost, 6)

    return result


async def run_bench(args) -> dict:
    all_scenarios = _load_scenarios(Path(args.bench))
    if not all_scenarios:
        return {"error": f"no scenarios at {args.bench}"}

    n_scenarios_total = len(all_scenarios)
    scenarios = filter_scenarios(
        all_scenarios,
        scenario_ids=getattr(args, "scenario_id", None) or None,
        bench_subset=getattr(args, "bench_subset", None),
        max_scenarios=getattr(args, "max_scenarios", None),
    )

    judge_client = Anthropic()
    results: list[ScenarioResult] = []

    if len(scenarios) < n_scenarios_total:
        print(
            _yellow(
                f"==> Running {len(scenarios)}/{n_scenarios_total} scenarios "
                f"on tier={args.tier} (filtered)"
            )
        )
    else:
        print(_yellow(f"==> Running {len(scenarios)} scenarios on tier={args.tier}"))
    for i, sc in enumerate(scenarios, 1):
        print(f"\n[{i}/{len(scenarios)}] {sc['id']} ({sc['device_slug']})")
        result = await _play_scenario(args.host, args.tier, sc, judge_client, args.verbose)
        results.append(result)
        marker = _green("✓") if result.error is None else _red("✗")
        print(
            f"  {marker} score={result.final_score:.3f} "
            f"(binary={result.binary_score:.2f} · judge={result.judge_score:.2f}) "
            f"· ${result.cost_usd:.4f} · {result.runtime_seconds:.1f}s"
        )
        if result.error:
            print(_red(f"    error: {result.error}"))
        elif args.verbose:
            for k, v in result.binary_checks.items():
                ico = _green("✓") if v else _red("✗")
                print(f"    {ico} {k}")
            print(_dim(f"    judge: {result.judge_reasoning}"))

    aggregate_score = sum(r.final_score for r in results) / len(results)
    total_cost = sum(r.cost_usd for r in results)

    payload = {
        "score": round(aggregate_score, 6),
        "tier_under_test": args.tier,
        "n_scenarios": len(results),
        "n_scenarios_total": n_scenarios_total,
        "binary_score_mean": round(
            sum(r.binary_score for r in results) / len(results), 6
        ),
        "judge_score_mean": round(
            sum(r.judge_score for r in results) / len(results), 6
        ),
        "cost_usd_total": round(total_cost, 6),
        "n_errors": sum(1 for r in results if r.error),
    }
    if args.verbose:
        payload["per_scenario"] = [
            {
                "id": r.id,
                "device_slug": r.device_slug,
                "final_score": r.final_score,
                "binary_score": r.binary_score,
                "binary_checks": r.binary_checks,
                "judge_score": r.judge_score,
                "judge_reasoning": r.judge_reasoning,
                "cost_usd": r.cost_usd,
                "runtime_seconds": r.runtime_seconds,
                "n_turns": len(r.turns),
                "tool_calls": _all_tool_names(r),
                # Enriched per-turn capture so analyzers can classify
                # filesystem touches by mount (patterns / playbooks /
                # device / repair). Custom tool_calls keep their `input`
                # too so we can verify e.g. mb_get_component refdes.
                "turns": [
                    {
                        "stage": t.stage,
                        "tool_calls": t.tool_calls,
                        "memory_tool_calls": t.memory_tool_calls,
                    }
                    for t in r.turns
                ],
                "error": r.error,
            }
            for r in results
        ]
    return payload


def build_parser() -> argparse.ArgumentParser:
    """Argparse for the runner — exposed for unit testing."""
    p = argparse.ArgumentParser(description="Diagnostic-agent benchmark + scoring.")
    p.add_argument("--tier", default=DEFAULT_TIER, choices=["fast", "normal", "deep"])
    p.add_argument("--bench", default=str(DEFAULT_BENCH))
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument(
        "--output",
        default=None,
        help="If given, write the JSON payload to this path (default: stdout only).",
    )
    filter_grp = p.add_mutually_exclusive_group()
    filter_grp.add_argument(
        "--scenario-id",
        action="append",
        default=None,
        metavar="ID",
        help=(
            "Run only the scenario with id ID. May be repeated to select "
            "multiple ids (e.g. --scenario-id A --scenario-id B). Unknown "
            "ids exit with code 2."
        ),
    )
    filter_grp.add_argument(
        "--bench-subset",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Run only the first N scenarios from the bench file (smoke "
            "mode). Mutually exclusive with --scenario-id."
        ),
    )
    p.add_argument(
        "--max-scenarios",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Hard cap on the number of scenarios run. Combinable with "
            "--scenario-id or --bench-subset (applied last)."
        ),
    )
    return p


def main() -> int:
    p = build_parser()
    args = p.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(_red("ERROR: ANTHROPIC_API_KEY missing — set it in .env"), file=sys.stderr)
        return 2

    try:
        payload = asyncio.run(run_bench(args))
    except ScenarioFilterError as exc:
        print(_red(f"ERROR: {exc}"), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(_red("\n  ! interrupted"), file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(_red(f"FATAL: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return 2

    # JSON one-line on stdout (parseable by the evolve loop)
    print(json.dumps(payload))
    if args.output:
        Path(args.output).write_text(json.dumps(payload, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
