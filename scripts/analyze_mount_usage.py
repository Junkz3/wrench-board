#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Post-process eval_diagnostic_agent JSON output for layered MA mount usage.

Reads the harness's JSON payload, walks every captured `tool_call` per
turn, and classifies filesystem tools (read/write/grep/glob/edit/ls)
against the four MA memory mounts (global-patterns, global-playbooks,
device-{slug}, repair-{repair_id}). Surfaces the breakdown per scenario
and an aggregate so a glance answers:

  - Does the agent reach all 4 mounts in practice (not just my smoke)?
  - Does it write to the per-repair scribe mount when it should?
  - Distribution of fs tools vs custom mb_* / bv_* tools.
  - Final scoring (binary + judge) per scenario.

Usage:
  .venv/bin/python -m scripts.analyze_mount_usage /tmp/bench_3_result.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

FS_TOOLS = {"read", "write", "edit", "grep", "glob", "ls"}


def classify_mount(path_or_pattern: str) -> str:
    """Map a filesystem path/pattern to one of the 4 layered mounts.

    Returns 'patterns' / 'playbooks' / 'device' / 'repair' / 'other'.
    'device' covers any wrench-board-{slug} that ISN'T a repair mount.
    """
    if not path_or_pattern:
        return "other"
    p = str(path_or_pattern)
    if "/wrench-board-global-patterns/" in p or "wrench-board-global-patterns" in p:
        return "patterns"
    if "/wrench-board-global-playbooks/" in p or "wrench-board-global-playbooks" in p:
        return "playbooks"
    if "/wrench-board-repair-" in p or "wrench-board-repair-" in p:
        return "repair"
    if "/wrench-board-" in p or "wrench-board-" in p:
        return "device"
    return "other"


def extract_path(tool_input: dict) -> str:
    """Pull a path/pattern from a fs-tool input, regardless of which key holds it."""
    if not isinstance(tool_input, dict):
        return ""
    for key in ("file_path", "path", "pattern", "glob", "directory", "cwd"):
        v = tool_input.get(key)
        if isinstance(v, str) and v:
            return v
    # Some tools embed the target inside `command` (e.g. shell-style usage)
    cmd = tool_input.get("command")
    if isinstance(cmd, str):
        return cmd
    return ""


def analyze_scenario(scenario: dict) -> dict[str, Any]:
    """Walk one ScenarioResult dict, return per-mount and per-tool breakdown."""
    fs_breakdown: dict[str, Counter] = defaultdict(Counter)  # tool → mount → count
    custom_counter: Counter = Counter()
    other_tools: Counter = Counter()
    fs_total = 0

    for turn in scenario.get("turns", []) or []:
        # Custom tools (mb_* / bv_* / profile_*) come from `tool_calls`,
        # one entry per agent.tool_use forwarded by the runtime.
        for call in turn.get("tool_calls", []) or []:
            name = call.get("name", "?")
            inp = call.get("input", {}) or {}
            if name in FS_TOOLS:
                fs_total += 1
                mount = classify_mount(extract_path(inp))
                fs_breakdown[name][mount] += 1
            elif name.startswith(("mb_", "bv_", "profile_")):
                custom_counter[name] += 1
            else:
                other_tools[name] += 1
        # MA-native fs / memory tools (read/write/grep/glob from
        # agent_toolset_20260401) come from a separate stream — runtime
        # forwards them under WS event type `memory_tool_use`.
        for call in turn.get("memory_tool_calls", []) or []:
            name = call.get("name", "?")
            inp = call.get("input", {}) or {}
            if name in FS_TOOLS:
                fs_total += 1
                mount = classify_mount(extract_path(inp))
                fs_breakdown[name][mount] += 1
            else:
                # Some MA tools (memory_search, memory_list) are not
                # path-anchored — bucket them as "other_fs" under a
                # synthetic mount key so they show up.
                fs_total += 1
                fs_breakdown[name]["server-side"] += 1

    # Reduce to a flat mount distribution (sum over fs tool kinds)
    mount_totals: Counter = Counter()
    for tool_counts in fs_breakdown.values():
        for mount, n in tool_counts.items():
            mount_totals[mount] += n

    return {
        "id": scenario.get("id"),
        "tier": scenario.get("tier"),
        "device_slug": scenario.get("device_slug"),
        "binary_score": scenario.get("binary_score", 0.0),
        "judge_score": scenario.get("judge_score", 0.0),
        "final_score": scenario.get("final_score", 0.0),
        "cost_usd": scenario.get("cost_usd", 0.0),
        "runtime_seconds": scenario.get("runtime_seconds", 0.0),
        "turn_count": len(scenario.get("turns", []) or []),
        "fs_total": fs_total,
        "fs_by_tool_mount": {
            tool: dict(mounts) for tool, mounts in fs_breakdown.items()
        },
        "mount_totals": dict(mount_totals),
        "custom_tools": dict(custom_counter),
        "other_tools": dict(other_tools),
        "error": scenario.get("error"),
    }


def render_table(rows: list[dict[str, Any]]) -> str:
    """ASCII breakdown table — one column per scenario, totals on the right."""
    if not rows:
        return "(no scenarios)"

    headers = [r["id"][:24] for r in rows] + ["TOTAL"]
    lines: list[str] = []

    def fmt_row(label: str, vals: list[Any], width: int = 12) -> str:
        cells = [str(v).rjust(width) for v in vals]
        return f"{label:<32}" + " ".join(cells)

    # --- FS tools by mount ---
    lines.append("=" * 100)
    lines.append("FILESYSTEM TOOLS PER MOUNT (read/write/grep/glob/edit/ls)")
    lines.append("=" * 100)
    lines.append(fmt_row("", headers))
    lines.append("-" * 100)

    all_mounts = ["patterns", "playbooks", "device", "repair", "other"]
    all_fs = sorted({t for r in rows for t in r["fs_by_tool_mount"]})
    for tool in all_fs:
        for mount in all_mounts:
            counts = [
                r["fs_by_tool_mount"].get(tool, {}).get(mount, 0)
                for r in rows
            ]
            total = sum(counts)
            if total == 0:
                continue
            lines.append(fmt_row(f"{tool:<6} → {mount}", counts + [total]))

    # --- Custom tools ---
    lines.append("")
    lines.append("=" * 100)
    lines.append("CUSTOM TOOLS (mb_* / bv_* / profile_*)")
    lines.append("=" * 100)
    all_custom = sorted({t for r in rows for t in r["custom_tools"]})
    for tool in all_custom:
        counts = [r["custom_tools"].get(tool, 0) for r in rows]
        lines.append(fmt_row(tool, counts + [sum(counts)]))

    # --- Aggregates ---
    lines.append("")
    lines.append("=" * 100)
    lines.append("PER-SCENARIO AGGREGATES")
    lines.append("=" * 100)
    lines.append(fmt_row("turns", [r["turn_count"] for r in rows] + [sum(r["turn_count"] for r in rows)]))
    lines.append(fmt_row("fs tool calls (total)", [r["fs_total"] for r in rows] + [sum(r["fs_total"] for r in rows)]))
    lines.append(fmt_row("custom tool calls (total)", [sum(r["custom_tools"].values()) for r in rows] + [sum(sum(r["custom_tools"].values()) for r in rows)]))
    lines.append(fmt_row("runtime (s)", [f"{r['runtime_seconds']:.1f}" for r in rows] + [f"{sum(r['runtime_seconds'] for r in rows):.1f}"]))
    lines.append(fmt_row("cost (USD)", [f"{r['cost_usd']:.4f}" for r in rows] + [f"{sum(r['cost_usd'] for r in rows):.4f}"]))
    lines.append(fmt_row("binary score", [f"{r['binary_score']:.2f}" for r in rows] + [f"{sum(r['binary_score'] for r in rows)/len(rows):.2f}"]))
    lines.append(fmt_row("judge score", [f"{r['judge_score']:.2f}" for r in rows] + [f"{sum(r['judge_score'] for r in rows)/len(rows):.2f}"]))
    lines.append(fmt_row("final score", [f"{r['final_score']:.2f}" for r in rows] + [f"{sum(r['final_score'] for r in rows)/len(rows):.2f}"]))

    # --- Mount coverage health ---
    lines.append("")
    lines.append("=" * 100)
    lines.append("LAYERED MA MEMORY HEALTH CHECK")
    lines.append("=" * 100)
    aggregate_mounts: Counter = Counter()
    for r in rows:
        for m, n in r["mount_totals"].items():
            aggregate_mounts[m] += n
    expected_mounts = ["patterns", "playbooks", "device", "repair"]
    for m in expected_mounts:
        n = aggregate_mounts.get(m, 0)
        sym = "✅" if n > 0 else "⚠️ "
        lines.append(f"  {sym}  /mnt/memory/wrench-board-{m if m != 'device' else '{slug}'}/  →  {n} fs tool calls across all scenarios")
    if aggregate_mounts.get("repair", 0) > 0:
        # Did write happen on the repair mount? (scribe pattern in action)
        wrote_repair = sum(
            r["fs_by_tool_mount"].get("write", {}).get("repair", 0)
            + r["fs_by_tool_mount"].get("edit", {}).get("repair", 0)
            for r in rows
        )
        lines.append(f"  📝 scribe writes to repair mount: {wrote_repair}")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", type=Path, help="JSON output from eval_diagnostic_agent")
    args = ap.parse_args()

    if not args.path.exists():
        sys.exit(f"file not found: {args.path}")

    payload = json.loads(args.path.read_text())
    scenarios = (
        payload.get("per_scenario")
        or payload.get("scenarios")
        or payload.get("results")
        or []
    )
    if not scenarios and isinstance(payload, list):
        scenarios = payload
    if not scenarios:
        sys.exit("no scenarios found in JSON payload (expected 'scenarios' or 'results' key)")

    rows = [analyze_scenario(s) for s in scenarios]
    print(render_table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
