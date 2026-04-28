# SPDX-License-Identifier: Apache-2.0
"""CLI: print a one-line JSON scorecard for the schematic vision pipeline.

Mirror of `eval_pipeline.py` but for mode B — scores `page_vision.py` /
`grounding.py` / `renderer.py` edits by RE-RUNNING vision on a fixed set of
test pages per device and comparing the SchematicPageGraph output against
per-page pdfplumber-extracted truth (refdes + nets) plus full-device truth
(for hallucination detection).

The agent edits page_vision.py et al. → in-process import picks up the new
prompt/schema/grounding → vision is invoked → metrics computed → score.

Multi-device, multi-page. Score = mean across pages, weighted across metrics.
Hard-fail invariants on hallucination rate and anti-collapse.

Test pages are configurable via env or CLI:
  PIPELINE_EVOLVE_VISION_PAGES="iphone-x:4,11;mnt-reform-motherboard:2"
  --pages iphone-x:4,11 mnt-reform-motherboard:2

Usage:
  .venv/bin/python -m scripts.eval_pipeline_vision
  .venv/bin/python -m scripts.eval_pipeline_vision --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic

from api.config import get_settings
from api.pipeline.schematic.page_vision import extract_page
from api.pipeline.schematic.renderer import RenderedPage
from api.pipeline.schematic.schemas import SchematicPageGraph

REPO_ROOT = Path(__file__).resolve().parent.parent
ORACLES_DIR = REPO_ROOT / "evolve-pipeline-vision" / "oracles"

# Default test pages — overrideable via env / CLI. Chosen for diversity:
# iphone-x p4 = SoC pinout (200+ pins, dense), p11 = camera/peripheral
# supplies (mid-density). mnt-reform p2 = CPU/memory area.
DEFAULT_TEST_PAGES = {
    "iphone-x": [4, 11],
    "mnt-reform-motherboard": [2],
}

WEIGHTS = {
    "refdes_recall": 0.45,
    "nets_recall": 0.20,
    "typed_edges_density": 0.15,
    "pin_role_classified": 0.10,
    "precision_inv_hallucination": 0.10,
}

HALLUCINATION_HARD_FAIL = 0.20  # >20% hallucinated refdes on any page → score=0
# Threshold tuned for LLM variance: baseline observed 8-18% phantom rate on
# dense pages with current prompt, partly because pdfplumber's text-layer
# refdes truth is incomplete (KiCad multi-section refdes, OCR-fused tokens).
# 5% was too strict — agent had no path to a clean baseline. 20% catches real
# fabrication while leaving room for the agent to drive hallucination DOWN
# via prompt tightening as a real improvement (rewarded by the
# precision_inv_hallucination weight in the score).
ANTI_COLLAPSE_FLOOR = 0.70      # total < 70% baseline → score=0

# I3 — per-page no-drop > 50% from rolling high-water floor. Prevents the
# moyenne-de-Simpson trick where a prompt edit gains on metric A while
# annihilating metric B on one page, with the cross-page average still
# rising. Floor lives in state.json under `per_page_floor` and is updated
# on every clean eval (no violations) via element-wise max. First eval
# after rollout is a calibration: no thresholds to enforce, just seeds
# the floor. Per agent auto-review 2026-04-25-1958 recommendation.
PER_PAGE_DROP_HARD_FAIL = 0.50


def _parse_pages_arg(args_pages: list[str] | None) -> dict[str, list[int]]:
    """Parse --pages slug:N,M slug2:K ... into {slug: [N, M]}."""
    raw = args_pages or []
    env = os.environ.get("PIPELINE_EVOLVE_VISION_PAGES", "")
    if env and not raw:
        raw = env.split(";")
    if not raw:
        return DEFAULT_TEST_PAGES
    out: dict[str, list[int]] = {}
    for tok in raw:
        if ":" not in tok:
            continue
        slug, pages_csv = tok.split(":", 1)
        slug = slug.strip()
        try:
            pages = [int(p.strip()) for p in pages_csv.split(",") if p.strip()]
        except ValueError:
            continue
        if slug and pages:
            out[slug] = pages
    return out or DEFAULT_TEST_PAGES


def _png_path(slug: str, page: int, memory_root: Path) -> Path | None:
    """Locate the rendered PNG for a page on disk (rendered at ingestion)."""
    pages_dir = memory_root / slug / "schematic_pages"
    # pdftoppm pads to width = max(2, len(str(total_pages))); try a few.
    for w in (2, 3, 4):
        cand = pages_dir / f"page-{page:0{w}d}.png"
        if cand.exists():
            return cand
    return None


def _hallucination_rate(
    vision_refdes: set[str], full_device_refdes: set[str]
) -> float:
    """Refdes that vision emitted but pdfplumber's PDF text never sees → fake.

    Excludes the empty case (vision emitted no refdes) → 0.0 by convention.
    """
    if not vision_refdes:
        return 0.0
    fake = vision_refdes - full_device_refdes
    return len(fake) / len(vision_refdes)


def _typed_edges_density(graph: SchematicPageGraph) -> float:
    """Edges per node — proxy for semantic richness of the extraction."""
    if not graph.nodes:
        return 0.0
    return len(graph.typed_edges) / len(graph.nodes)


def _pin_role_classified_ratio(graph: SchematicPageGraph) -> float:
    """Fraction of pins with role != 'unknown' — vision's structural depth."""
    total = 0
    classified = 0
    for node in graph.nodes:
        for pin in node.pins:
            total += 1
            if pin.role and pin.role != "unknown":
                classified += 1
    if total == 0:
        return 0.0
    return classified / total


async def _score_page(
    *,
    client: AsyncAnthropic,
    model: str,
    slug: str,
    page: int,
    oracle: dict,
    memory_root: Path,
) -> dict[str, Any]:
    png = _png_path(slug, page, memory_root)
    if png is None:
        return {
            "slug": slug,
            "page": page,
            "error": f"PNG missing for page {page}",
            "score_contribution": 0.0,
        }
    rendered = RenderedPage(
        page_number=page,
        png_path=png,
        orientation="landscape",
        is_scanned=False,
        width_pt=0.0,
        height_pt=0.0,
    )

    per_page_truth = oracle.get("per_page", {}).get(str(page), {})
    refdes_truth = set(per_page_truth.get("refdes_truth", []))
    nets_truth = set(per_page_truth.get("nets_truth", []))
    full_device_refdes = set(oracle.get("full_device_refdes", []))

    # Bug A — device-aware BGA-coord filter on refdes truth.
    # SoC pinout pages print BGA pin coordinates (A1..Y39) as text in the
    # PDF; pdfplumber catches them and they end up in refdes_truth. On
    # devices with uniformly-long refdes (Apple iPhone: 4-digit C1801,
    # R1900, U2700) the BGA coords are NEVER legitimate refdes — vision
    # correctly refuses to extract them, but they drag down recall.
    # We strip them only on long-refdes-dominant devices (>70% long
    # tokens), preserving short-refdes legitimacy on hobbyist boards
    # like mnt-reform (C0, C1, C10, …). Per agent proposal
    # eval-2026-04-25-1925.md (session 9 propose-eval-fix).
    long_count = sum(1 for r in full_device_refdes if len(r) >= 4)
    long_ratio = long_count / max(len(full_device_refdes), 1)
    if long_ratio >= 0.70:
        import re as _re
        bga_pat = _re.compile(r"^[A-Y]\d{1,2}$")
        full_device_refdes = {r for r in full_device_refdes if not bga_pat.match(r)}
        refdes_truth = {r for r in refdes_truth if not bga_pat.match(r)}

    try:
        graph = await extract_page(
            client=client,
            model=model,
            rendered=rendered,
            total_pages=oracle.get("total_pages", 30),
            device_label=slug,
            grounding=None,
        )
    except Exception as exc:
        return {
            "slug": slug,
            "page": page,
            "error": f"vision crash: {type(exc).__name__}: {exc}",
            "score_contribution": 0.0,
        }

    vision_refdes = {n.refdes for n in graph.nodes if n.refdes}
    vision_nets = {
        n.label for n in graph.nets if n.label
    }

    refdes_recall = (
        len(vision_refdes & refdes_truth) / len(refdes_truth)
        if refdes_truth
        else 0.0
    )
    nets_recall = (
        len(vision_nets & nets_truth) / len(nets_truth) if nets_truth else 0.0
    )
    halluc = _hallucination_rate(vision_refdes, full_device_refdes)
    edges = _typed_edges_density(graph)
    role = _pin_role_classified_ratio(graph)

    metrics = {
        "refdes_recall": refdes_recall,
        "nets_recall": nets_recall,
        "typed_edges_density": min(edges, 1.0),  # cap at 1.0 for weighted sum
        "pin_role_classified": role,
        "precision_inv_hallucination": 1.0 - halluc,
    }

    return {
        "slug": slug,
        "page": page,
        "vision_refdes_count": len(vision_refdes),
        "vision_nets_count": len(vision_nets),
        "vision_nodes_count": len(graph.nodes),
        "hallucination_rate": halluc,
        "refdes_truth_size": len(refdes_truth),
        "nets_truth_size": len(nets_truth),
        "metrics": metrics,
    }


def _check_invariants(
    per_page: list[dict],
    oracle_by_slug: dict,
    per_page_floor: dict | None = None,
) -> list[str]:
    """Return list of violated invariant codes. Empty = all pass."""
    violations: list[str] = []

    # I1 — hallucination rate per page must be < HALLUCINATION_HARD_FAIL
    for r in per_page:
        if "error" in r:
            continue
        hr = r.get("hallucination_rate", 0.0)
        if hr > HALLUCINATION_HARD_FAIL:
            violations.append(
                f"I1_hallucination:{r['slug']}/p{r['page']}={hr:.3f}>{HALLUCINATION_HARD_FAIL}"
            )

    # I2 — total components per device ≥ 70% of bootstrap baseline
    by_slug: dict[str, int] = {}
    for r in per_page:
        if "error" in r:
            continue
        by_slug.setdefault(r["slug"], 0)
        by_slug[r["slug"]] += r.get("vision_nodes_count", 0)
    for slug, count in by_slug.items():
        baseline = oracle_by_slug.get(slug, {}).get("baseline_total_nodes", 0)
        if baseline > 0 and count < ANTI_COLLAPSE_FLOOR * baseline:
            violations.append(
                f"I2_nodes_collapse:{slug}={count}<{ANTI_COLLAPSE_FLOOR * baseline:.0f}"
            )

    # I3 — per-page no-drop > 50% from rolling high-water floor.
    if per_page_floor:
        for r in per_page:
            if "error" in r:
                continue
            page_floor = per_page_floor.get(r["slug"], {}).get(str(r["page"]), {})
            for metric, current in r["metrics"].items():
                base = page_floor.get(metric)
                if base is None or base <= 0:
                    continue  # no floor recorded yet → skip
                if current < (1.0 - PER_PAGE_DROP_HARD_FAIL) * base:
                    violations.append(
                        f"I3_per_page_drop:{r['slug']}/p{r['page']}/{metric}="
                        f"{current:.3f}<{(1.0 - PER_PAGE_DROP_HARD_FAIL) * base:.3f}"
                    )

    return violations


STATE_FILE = REPO_ROOT / "evolve-pipeline-vision" / "state.json"


def _load_per_page_floor() -> dict:
    """Read the rolling high-water per-page floor from state.json."""
    if not STATE_FILE.exists():
        return {}
    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        return {}
    return state.get("per_page_floor", {})


def _update_per_page_floor(per_page: list[dict]) -> None:
    """Write back per_page_floor in state.json with element-wise max(observed).

    Only called on a clean eval (no invariant violations). Preserves all
    other state.json fields untouched.
    """
    if not STATE_FILE.exists():
        return
    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        return
    floor = state.get("per_page_floor", {})
    for r in per_page:
        if "error" in r:
            continue
        slug_floor = floor.setdefault(r["slug"], {})
        page_floor = slug_floor.setdefault(str(r["page"]), {})
        for metric, current in r["metrics"].items():
            prev = page_floor.get(metric, 0.0)
            if current > prev:
                page_floor[metric] = current
    state["per_page_floor"] = floor
    STATE_FILE.write_text(json.dumps(state, indent=2))


async def _run(devices: dict[str, list[int]], verbose: bool) -> dict[str, Any]:
    settings = get_settings()
    memory_root = Path(settings.memory_root)
    model = settings.anthropic_model_main  # Opus 4.7 — vision-grade

    if not settings.anthropic_api_key:
        return {"score": 0.0, "error": "ANTHROPIC_API_KEY missing"}

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    oracle_by_slug: dict[str, dict] = {}
    for slug in devices:
        oracle_path = ORACLES_DIR / f"{slug}.json"
        if not oracle_path.exists():
            return {
                "score": 0.0,
                "error": f"missing oracle: {oracle_path}",
            }
        oracle_by_slug[slug] = json.loads(oracle_path.read_text())

    # Run all (slug, page) pairs in parallel — Anthropic SDK handles
    # concurrency fine, and prompt caching kicks in across pages.
    tasks = [
        _score_page(
            client=client,
            model=model,
            slug=slug,
            page=page,
            oracle=oracle_by_slug[slug],
            memory_root=memory_root,
        )
        for slug, pages in devices.items()
        for page in pages
    ]
    per_page = await asyncio.gather(*tasks)

    valid = [r for r in per_page if "error" not in r]
    if not valid:
        return {
            "score": 0.0,
            "error": "no scorable pages",
            "per_page": per_page,
        }

    per_page_floor = _load_per_page_floor()
    violations = _check_invariants(per_page, oracle_by_slug, per_page_floor)
    if not violations:
        # Clean eval — rolling floor monotonically rises with new highs.
        _update_per_page_floor(per_page)

    # Bug B1 — skip empty-truth pages from per-metric aggregates.
    # When `nets_truth` or `refdes_truth` is empty for a page (oracle
    # extraction failure or genuine connector-only page), the metric
    # mechanically returns 0.0 and unfairly drags the aggregate down.
    # We only average over pages where each metric is well-defined.
    # Per agent proposal eval-2026-04-25-1925.md (session 9
    # propose-eval-fix), option B1.
    def _meaningful_for_metric(r: dict, metric: str) -> bool:
        if metric == "nets_recall":
            return r.get("nets_truth_size", 0) > 0
        if metric == "refdes_recall":
            return r.get("refdes_truth_size", 0) > 0
        # edges_density, pin_role, precision are always defined
        return True

    aggregated: dict[str, float] = {}
    for k in WEIGHTS:
        pages_for_k = [r for r in valid if _meaningful_for_metric(r, k)]
        if pages_for_k:
            aggregated[k] = sum(r["metrics"][k] for r in pages_for_k) / len(pages_for_k)
        else:
            aggregated[k] = 0.0

    if violations:
        score = 0.0
    else:
        score = sum(aggregated[k] * w for k, w in WEIGHTS.items())

    out: dict[str, Any] = {
        "score": score,
        "n_pages": len(valid),
        "any_invariant_violations": len(violations),
        **aggregated,
    }
    if verbose:
        out["per_page"] = per_page
        out["invariant_violations"] = violations
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pages",
        nargs="+",
        default=None,
        help='List of slug:N,M tokens (e.g. "iphone-x:4,11")',
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Per-page breakdown."
    )
    args = parser.parse_args()

    devices = _parse_pages_arg(args.pages)
    out = asyncio.run(_run(devices, args.verbose))
    print(json.dumps(out))
    return 0 if "error" not in out else 2


if __name__ == "__main__":
    sys.exit(main())
