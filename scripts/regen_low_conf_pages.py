"""Multi-sample regen of low-confidence baseline pages.

Re-runs page_vision.extract_page() N times on each target page and picks
the best run by (confidence, node count). Writes results to a sibling
directory; does NOT overwrite the canonical baseline unless --commit is
passed.

Use when:
- Bench data shows a baseline page has high inter-run variance and low
  confidence (e.g. iPhone X page 31: conf 0.60, recovered to conf 0.86
  on a single re-run with +3 components).
- You want to regenerate just the weakest pages of a pack without paying
  for a full re-ingest.

Usage:
    .venv/bin/python -u scripts/regen_low_conf_pages.py \
        --pdf memory/iphone-x/schematic.pdf \
        --slug iphone-x \
        --pages 31,35,37 \
        --samples 3

    # Override pages with an auto threshold:
    .venv/bin/python -u scripts/regen_low_conf_pages.py \
        --pdf memory/iphone-x/schematic.pdf \
        --slug iphone-x \
        --conf-threshold 0.85 \
        --samples 3

    # After review, write the winners back into the canonical pack:
    .venv/bin/python -u scripts/regen_low_conf_pages.py ... --commit
"""
from __future__ import annotations

import argparse, asyncio, json, logging, os, re, shutil, sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)
logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s [%(levelname)s] %(name)s | %(message)s")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

from anthropic import AsyncAnthropic  # noqa: E402
from api.pipeline.schematic.page_vision import extract_page  # noqa: E402
from api.pipeline.schematic.renderer import render_pages  # noqa: E402
from api.pipeline.schematic.schemas import SchematicPageGraph  # noqa: E402

log = logging.getLogger("regen")


def _autodetect_low_conf(slug: str, threshold: float) -> list[int]:
    """Scan memory/{slug}/schematic_pages/page_NNN.json and return page
    numbers below the confidence threshold (with at least one node)."""
    mem = ROOT / "memory" / slug / "schematic_pages"
    pages: list[int] = []
    for p in sorted(mem.iterdir()):
        if not re.fullmatch(r"page_\d{3}\.json", p.name):
            continue
        d = json.loads(p.read_text())
        if d.get("confidence", 1.0) < threshold and len(d.get("nodes", [])) > 0:
            pages.append(int(p.stem.split("_")[-1]))
    return pages


async def _sample_once(
    client: AsyncAnthropic, *, model: str, rendered, total_pages: int,
    slug: str, sample_idx: int,
) -> tuple[int, SchematicPageGraph]:
    log.info("[page %d] sample %d starting", rendered.page_number, sample_idx)
    g = await extract_page(
        client=client, model=model, rendered=rendered,
        total_pages=total_pages, device_label=slug,
    )
    log.info(
        "[page %d] sample %d done — %d nodes / %d nets / %d edges / conf=%.2f",
        rendered.page_number, sample_idx,
        len(g.nodes), len(g.nets), len(g.typed_edges), g.confidence,
    )
    return sample_idx, g


def _score(g: SchematicPageGraph) -> tuple[float, int, int]:
    # Higher is better. Confidence dominates; ties broken by node + edge count.
    return (g.confidence, len(g.nodes), len(g.typed_edges))


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--pdf", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--pages", help="Comma-separated page numbers (overrides --conf-threshold)")
    p.add_argument("--conf-threshold", type=float, default=0.85,
                   help="Auto-pick pages with confidence below this (default 0.85)")
    p.add_argument("--samples", type=int, default=3)
    p.add_argument("--out-dir", default=None,
                   help="Defaults to memory/{slug}/schematic_pages_regen/")
    p.add_argument("--commit", action="store_true",
                   help="Overwrite the canonical baseline page with the winner")
    p.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL_MAIN", "claude-opus-4-8"))
    args = p.parse_args()

    pdf_path = Path(args.pdf).expanduser().resolve()
    if not pdf_path.exists():
        raise SystemExit(f"PDF not found: {pdf_path}")

    baseline_dir = ROOT / "memory" / args.slug / "schematic_pages"
    out_dir = (
        Path(args.out_dir).expanduser().resolve()
        if args.out_dir
        else ROOT / "memory" / args.slug / "schematic_pages_regen"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.pages:
        target_pages = [int(x) for x in args.pages.split(",") if x.strip()]
    else:
        target_pages = _autodetect_low_conf(args.slug, args.conf_threshold)
        log.info("Auto-detected %d pages below conf %.2f: %s",
                 len(target_pages), args.conf_threshold, target_pages)

    if not target_pages:
        log.info("No target pages — exiting")
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY not set")
    client = AsyncAnthropic(api_key=api_key)

    log.info("Rendering PDF (this may take a minute)...")
    rendered_pages = render_pages(pdf_path, out_dir / "_rendered", dpi=200)
    by_num = {r.page_number: r for r in rendered_pages}
    total_pages = len(rendered_pages)

    summary = []
    for page_num in target_pages:
        if page_num not in by_num:
            log.warning("page %d not in rendered set — skipping", page_num)
            continue
        rendered = by_num[page_num]

        # Load baseline for comparison
        b_path = baseline_dir / f"page_{page_num:03d}.json"
        baseline = SchematicPageGraph.model_validate(json.loads(b_path.read_text()))

        log.info(
            "[page %d] baseline disk: %d nodes / %d nets / %d edges / conf=%.2f",
            page_num, len(baseline.nodes), len(baseline.nets),
            len(baseline.typed_edges), baseline.confidence,
        )

        # Fan out N samples in parallel for this page
        tasks = [
            _sample_once(
                client, model=args.model, rendered=rendered,
                total_pages=total_pages, slug=args.slug, sample_idx=i,
            )
            for i in range(args.samples)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        good = [(idx, g) for r in results if not isinstance(r, BaseException) for idx, g in [r]]
        errors = [r for r in results if isinstance(r, BaseException)]
        for e in errors:
            log.warning("[page %d] one sample failed: %s", page_num, e)

        if not good:
            log.error("[page %d] ALL samples failed", page_num)
            summary.append({"page": page_num, "status": "all_failed"})
            continue

        # Pick the best
        good.sort(key=lambda kv: _score(kv[1]), reverse=True)
        best_idx, best = good[0]
        log.info(
            "[page %d] winner = sample %d  (conf=%.2f, %d nodes vs baseline %.2f, %d nodes)",
            page_num, best_idx, best.confidence, len(best.nodes),
            baseline.confidence, len(baseline.nodes),
        )

        # Persist all samples + winner
        for idx, g in good:
            (out_dir / f"page_{page_num:03d}_sample{idx}.json").write_text(
                g.model_dump_json(indent=2)
            )
        winner_path = out_dir / f"page_{page_num:03d}.json"
        winner_path.write_text(best.model_dump_json(indent=2))

        # Diff
        b_refdes = {n.refdes for n in baseline.nodes}
        w_refdes = {n.refdes for n in best.nodes}
        improvement = {
            "page": page_num,
            "baseline": {
                "conf": baseline.confidence,
                "nodes": len(baseline.nodes),
                "nets": len(baseline.nets),
                "edges": len(baseline.typed_edges),
            },
            "winner": {
                "conf": best.confidence,
                "nodes": len(best.nodes),
                "nets": len(best.nets),
                "edges": len(best.typed_edges),
                "sample_idx": best_idx,
            },
            "delta_conf": best.confidence - baseline.confidence,
            "delta_nodes": len(best.nodes) - len(baseline.nodes),
            "new_refdes": sorted(w_refdes - b_refdes),
            "dropped_refdes": sorted(b_refdes - w_refdes),
            "sample_scores": [
                {"idx": idx, "conf": g.confidence, "nodes": len(g.nodes)}
                for idx, g in good
            ],
        }
        summary.append(improvement)
        log.info(
            "[page %d] Δconf=%+.2f  Δnodes=%+d  new=%s  dropped=%s",
            page_num,
            improvement["delta_conf"], improvement["delta_nodes"],
            improvement["new_refdes"][:5],
            improvement["dropped_refdes"][:5],
        )

        if args.commit and (
            best.confidence > baseline.confidence
            or (best.confidence == baseline.confidence and len(best.nodes) > len(baseline.nodes))
        ):
            shutil.copy(winner_path, b_path)
            log.info("[page %d] COMMITTED — overwrote %s", page_num, b_path)

    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    log.info("=" * 60)
    log.info("=== SUMMARY (%d pages) ===", len(summary))
    log.info("=" * 60)
    pos = sum(1 for s in summary if s.get("delta_conf", 0) > 0)
    neg = sum(1 for s in summary if s.get("delta_conf", 0) < 0)
    zero = sum(1 for s in summary if s.get("delta_conf", 0) == 0)
    log.info("  conf improved: %d", pos)
    log.info("  conf unchanged: %d", zero)
    log.info("  conf regressed: %d", neg)
    log.info("Wrote per-page outputs + summary to %s", out_dir)
    if not args.commit:
        log.info("(dry-run — re-run with --commit to write winners back into the baseline)")


if __name__ == "__main__":
    asyncio.run(main())
