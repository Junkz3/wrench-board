#!/usr/bin/env python3
"""Build the field-calibrated benchmark corpus from already-persisted data.

Two canonical sources — NO hand-curated seed file:

1. **Live validated outcomes** (primary, post-FT chantier)
     `memory/*/repairs/*/outcome.json`
   Joined with each repair's `measurements.jsonl` to synthesise the
   `Observations` snapshot. One scenario per outcome, source="live".

2. **Historical field reports** (fallback, pre-FT chantier)
     `memory/*/field_reports/*.md`
   YAML frontmatter (`refdes`, `symptom`, `confirmed_cause`) becomes a
   minimal scenario — no structured observations, just the free-form
   symptom as description and a single-fix ground truth. source="report".

Writes `tests/pipeline/schematic/fixtures/hypothesize_field_scenarios.json`
for the accuracy-gate test to consume.

Usage:
    .venv/bin/python scripts/build_benchmark_corpus.py
    .venv/bin/python scripts/build_benchmark_corpus.py --out <path>
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from api.agent.measurement_memory import synthesise_observations
from api.agent.validation import RepairOutcome

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / "tests/pipeline/schematic/fixtures/hypothesize_field_scenarios.json"
MEMORY_ROOT = REPO / "memory"


# Very light YAML frontmatter reader — field_reports always use a simple
# `key: value` or `key: "quoted value"` shape, no nested keys.
_FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _parse_frontmatter(markdown: str) -> dict[str, str]:
    m = _FM_RE.match(markdown)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        # Strip surrounding quotes — tolerant of both styles.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        out[key] = value
    return out


def build_live_outcomes() -> list[dict]:
    scenarios: list[dict] = []
    if not MEMORY_ROOT.exists():
        return scenarios
    for outcome_path in MEMORY_ROOT.glob("*/repairs/*/outcome.json"):
        try:
            outcome = RepairOutcome.model_validate_json(outcome_path.read_text())
        except (OSError, ValueError) as exc:
            print(f"  skip outcome {outcome_path.name}: {exc}")
            continue
        observations = synthesise_observations(
            memory_root=MEMORY_ROOT,
            device_slug=outcome.device_slug,
            repair_id=outcome.repair_id,
        )
        scenarios.append({
            "id": f"live-{outcome.device_slug}-{outcome.repair_id}",
            "slug": outcome.device_slug,
            "source": "live",
            "observations": observations.model_dump(),
            "ground_truth_kill": [f.refdes for f in outcome.fixes],
            "ground_truth_modes": [f.mode for f in outcome.fixes],
            "rationales": [f.rationale for f in outcome.fixes],
            "validated_at": outcome.validated_at,
        })
    return scenarios


def build_field_report_cases() -> list[dict]:
    scenarios: list[dict] = []
    if not MEMORY_ROOT.exists():
        return scenarios
    for report_path in MEMORY_ROOT.glob("*/field_reports/*.md"):
        try:
            markdown = report_path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = _parse_frontmatter(markdown)
        refdes = fm.get("refdes") or ""
        symptom = fm.get("symptom") or ""
        confirmed = fm.get("confirmed_cause") or ""
        device_slug = fm.get("device_slug") or ""
        report_id = fm.get("report_id") or report_path.stem
        if not refdes or not device_slug:
            continue
        # Field reports predate the structured observations shape — they
        # carry the symptom as free text. Leave state_* empty; the
        # description carries the information for corpus analysis.
        scenarios.append({
            "id": f"report-{report_id}",
            "slug": device_slug,
            "source": "field_report",
            "observations": {
                "state_comps": {},
                "state_rails": {},
                "metrics_comps": {},
                "metrics_rails": {},
            },
            "ground_truth_kill": [refdes],
            "ground_truth_modes": ["dead"],   # pre-FT chantier defaults
            "rationales": [confirmed] if confirmed else [],
            "symptom": symptom,
        })
    return scenarios


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    live = build_live_outcomes()
    reports = build_field_report_cases()
    # Historical field reports first so the file stays diff-stable as live
    # entries accumulate at the tail.
    all_scenarios = reports + live

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(all_scenarios, indent=2))

    by_source: dict[str, int] = {}
    by_mode: dict[str, int] = {}
    for sc in all_scenarios:
        by_source[sc["source"]] = by_source.get(sc["source"], 0) + 1
        if sc["ground_truth_modes"]:
            m = sc["ground_truth_modes"][0]
            by_mode[m] = by_mode.get(m, 0) + 1

    print(f"wrote {len(all_scenarios)} field scenarios to {out.relative_to(REPO)}")
    print(f"  by source: {by_source}")
    print(f"  by mode:   {by_mode}")


if __name__ == "__main__":
    main()
