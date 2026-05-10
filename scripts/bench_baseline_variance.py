"""Quick variance probe: re-run the production single-shot Opus baseline on
one page and diff against the existing on-disk baseline. Tells us whether
'agentic_only' components on a page are real wins or just inter-run noise.
"""
from __future__ import annotations

import argparse, asyncio, json, logging, os, sys
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

log = logging.getLogger("variance")


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--pdf", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--page", type=int, required=True)
    p.add_argument("--out-dir", default="/tmp/bench_variance/")
    args = p.parse_args()

    out = Path(args.out_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    rendered_dir = out / "rendered"
    rendered_dir.mkdir(exist_ok=True)
    pages = render_pages(Path(args.pdf), rendered_dir, dpi=200)
    target = next(p for p in pages if p.page_number == args.page)
    log.info("rendered page %d at %s", args.page, target.png_path)

    api_key = os.environ["ANTHROPIC_API_KEY"]
    client = AsyncAnthropic(api_key=api_key)

    log.info("running single-shot baseline on page %d (this is run B1)...", args.page)
    new_graph = await extract_page(
        client=client,
        model=os.environ.get("ANTHROPIC_MODEL_MAIN", "claude-opus-4-7"),
        rendered=target,
        total_pages=len(pages),
        device_label=args.slug,
    )
    (out / f"page_{args.page:03d}_b1.json").write_text(new_graph.model_dump_json(indent=2))
    log.info("B1 — %d nodes / %d nets / %d edges / conf=%.2f",
             len(new_graph.nodes), len(new_graph.nets),
             len(new_graph.typed_edges), new_graph.confidence)

    # Compare to disk baseline (B0)
    b0_path = ROOT / "memory" / args.slug / "schematic_pages" / f"page_{args.page:03d}.json"
    b0 = SchematicPageGraph.model_validate(json.loads(b0_path.read_text()))
    log.info("B0 (on disk) — %d nodes / %d nets / %d edges / conf=%.2f",
             len(b0.nodes), len(b0.nets), len(b0.typed_edges), b0.confidence)

    b0_refdes = {n.refdes for n in b0.nodes}
    b1_refdes = {n.refdes for n in new_graph.nodes}
    log.info("baseline-vs-baseline (B0 vs B1) on page %d:", args.page)
    log.info("  components: B0=%d B1=%d shared=%d  (B0_only=%s, B1_only=%s)",
             len(b0_refdes), len(b1_refdes), len(b0_refdes & b1_refdes),
             sorted(b0_refdes - b1_refdes), sorted(b1_refdes - b0_refdes))


if __name__ == "__main__":
    asyncio.run(main())
