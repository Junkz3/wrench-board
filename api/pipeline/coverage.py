# SPDX-License-Identifier: Apache-2.0
"""Symptom-coverage classifier.

At repair-creation time, the pipeline compares the technician's newly-
reported symptom against the pack's existing `rules.json` to decide
whether a fresh expand-pack round-trip is worth the cost. When Haiku
classifies the symptom as already covered (confidence ≥ threshold),
the caller skips the LLM expand and surfaces the matched rule
immediately — the technician gets the known diagnostic flow in under a
second instead of waiting ~30-60 s for a redundant expand.

Only one Haiku forced-tool call per `check_symptom_coverage`. Cost is
~$0.001 per check — negligible compared to a $0.5 expand-pack.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from anthropic import AsyncAnthropic

from api.pipeline.schemas import CoverageCheck
from api.pipeline.tool_call import call_with_forced_tool

if TYPE_CHECKING:
    from api.pipeline.telemetry.token_stats import PhaseTokenStats

logger = logging.getLogger("microsolder.pipeline.coverage")


SUBMIT_COVERAGE_TOOL = "submit_coverage_check"


COVERAGE_SYSTEM = """\
You are a diagnostic-coverage checker for a microsoldering repair
workbench. Given a NEW symptom a technician just reported and a list of
symptoms already captured by the pack's rules, decide whether the new
symptom is effectively covered by any existing rule.

## What counts as covered

- **Exact / near-exact match** — word-for-word or trivial reordering.
  confidence 0.9-1.0.
- **Paraphrase of the same observable failure** — "screen dark" ≡
  "no image on internal display" ≡ "backlight off". confidence 0.75-0.9.
- **New symptom narrows an existing one** — "USB-A port 1 dead on cold
  boot" against existing "USB ports dead": covered, confidence 0.7-0.8.
  The existing rule's diagnostic flow still applies.

## What does NOT count as covered

- **Same surface hardware, different failure mode** — "USB port dead" vs
  "USB audio dropout": not covered. Different failure class.
- **Same subsystem at a different stage** — "LCD flickers during boot"
  vs "no backlight ever": not covered.
- When the new symptom is a superset spanning multiple existing rules,
  treat as NOT covered so the expand pass can add a distinct rule.

## Be conservative

When in doubt, return covered=false. False negatives (we run an expand
we didn't strictly need) cost $0.5. False positives (we skip an expand
that would have captured a genuinely new rule) cost the tech an entire
diagnostic session. Prefer the cheap miss.

## Output contract

Return via the `submit_coverage_check` tool. No prose.

- `covered` — boolean
- `matched_rule_id` — set ONLY when covered=true AND confidence ≥ 0.7;
  null otherwise
- `confidence` — 0.0 to 1.0 per the scale above
- `reason` — one sentence the UI can surface to the tech, e.g. "matches
  rule-tristar-001, both describe no-charge on adapter connect"
"""


COVERAGE_USER_TEMPLATE = """\
New symptom reported by the technician:

{symptom}

Symptoms already captured by the pack's {n_rules} rules:

{rules_summary}

Is the new symptom already covered? Emit the `submit_coverage_check` tool call now.
"""


def _submit_coverage_tool() -> dict:
    return {
        "name": SUBMIT_COVERAGE_TOOL,
        "description": (
            "Submit the coverage verdict for the new symptom. Exactly one call."
        ),
        "input_schema": CoverageCheck.model_json_schema(),
    }


def _load_rules(memory_root: Path, device_slug: str) -> list[dict] | None:
    """Return the `rules` array from `memory/{slug}/rules.json` or None.

    Missing file, malformed JSON, and empty rules list all yield None —
    caller treats it as "not covered, proceed with full pipeline / expand"."""
    rules_path = memory_root / device_slug / "rules.json"
    if not rules_path.exists():
        return None
    try:
        payload = json.loads(rules_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning(
            "[coverage] malformed rules.json at %s — treating as empty pack",
            rules_path,
        )
        return None
    rules = payload.get("rules") or []
    if not rules:
        return None
    return rules


def _build_rules_summary(rules: list[dict]) -> str:
    """One line per (rule_id, symptom). Keeps the prompt small even for
    packs with dozens of rules — each line is ~80 chars, 100 rules ≈ 8k
    tokens, still well inside Haiku's budget."""
    lines: list[str] = []
    for rule in rules:
        rid = rule.get("id") or "rule-unknown"
        symptoms = rule.get("symptoms") or []
        for sym in symptoms:
            lines.append(f"- rule_id={rid} · symptom: {sym}")
    return "\n".join(lines)


async def check_symptom_coverage(
    *,
    client: AsyncAnthropic,
    model: str,
    device_slug: str,
    symptom: str,
    memory_root: Path,
    stats: PhaseTokenStats | None = None,
) -> CoverageCheck:
    """Classify whether the new `symptom` is already covered by the pack.

    Returns a `CoverageCheck` the caller can act on directly. When the
    pack has no rules.json or an empty rules list, returns
    `covered=False, confidence=0.0, reason="no prior rules"` without
    calling the LLM — that saves the Haiku round-trip on fresh packs.
    """
    rules = _load_rules(memory_root, device_slug)
    if rules is None:
        logger.info(
            "[coverage] no prior rules for slug=%s — cold cache, skip LLM",
            device_slug,
        )
        return CoverageCheck(
            covered=False,
            matched_rule_id=None,
            confidence=0.0,
            reason="no prior rules in pack — fresh diagnostic required",
        )

    rules_summary = _build_rules_summary(rules)
    n_rules = len(rules)

    logger.info(
        "[coverage] checking symptom against n_rules=%d for slug=%s",
        n_rules,
        device_slug,
    )

    user_prompt = COVERAGE_USER_TEMPLATE.format(
        symptom=symptom.strip(),
        rules_summary=rules_summary,
        n_rules=n_rules,
    )

    verdict = await call_with_forced_tool(
        client=client,
        model=model,
        system=COVERAGE_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        tools=[_submit_coverage_tool()],
        forced_tool_name=SUBMIT_COVERAGE_TOOL,
        output_schema=CoverageCheck,
        max_attempts=2,
        log_label="coverage",
        stats=stats,
    )

    # Guard against the LLM setting matched_rule_id below threshold.
    if verdict.confidence < 0.7 and verdict.matched_rule_id is not None:
        logger.info(
            "[coverage] stripping matched_rule_id — confidence=%.2f < 0.7",
            verdict.confidence,
        )
        verdict = verdict.model_copy(update={"matched_rule_id": None})

    logger.info(
        "[coverage] covered=%s confidence=%.2f matched=%s",
        verdict.covered,
        verdict.confidence,
        verdict.matched_rule_id,
    )
    return verdict
