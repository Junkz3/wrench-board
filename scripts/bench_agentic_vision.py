"""Bench: agentic vision loop with request_zoom tool, vs the single-shot baseline.

Tests whether allowing Opus to request high-res crops improves extraction
quality on dense schematic pages. Reuses the production system prompt and
schema from page_vision.py — only the tool surface changes.

Usage:
    .venv/bin/python -u scripts/bench_agentic_vision.py \
        --pdf memory/iphone-x/schematic.pdf \
        --slug iphone-x \
        --pages 6 \
        --out-dir /tmp/bench_agentic/

Compares each agentic page output against the baseline at
memory/{slug}/schematic_pages/page_NNN.json.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import pdfplumber  # noqa: E402
from anthropic import AsyncAnthropic  # noqa: E402
from PIL import Image  # noqa: E402

from api.pipeline.schematic.page_vision import SYSTEM_PROMPT  # noqa: E402
from api.pipeline.schematic.schemas import SchematicPageGraph  # noqa: E402

log = logging.getLogger("bench_agentic")


REQUEST_ZOOM_TOOL = {
    "name": "request_zoom",
    "description": (
        "Request a higher-resolution crop of a specific region of the schematic page. "
        "Use this when a region looks dense and you can't read small text reliably "
        "(values tables, IC pinout boxes, rail labels, BOM-style annotations). "
        "Coordinates are in pixels of the source image you were shown — the harness "
        "re-renders that region at the source PDF's full resolution and sends it "
        "back as another image. Make a few targeted zooms rather than many tiny "
        "ones; each crop costs an API round trip. After you've zoomed enough to "
        "read every component confidently, call submit_schematic_page."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "Top-left x in source-image pixels (0 = left edge)"},
            "y": {"type": "integer", "description": "Top-left y in source-image pixels (0 = top edge)"},
            "width": {"type": "integer", "description": "Crop width in source-image pixels"},
            "height": {"type": "integer", "description": "Crop height in source-image pixels"},
            "reason": {
                "type": "string",
                "description": "One short sentence on what you expect to see in this crop and why the full-page resolution wasn't enough.",
            },
        },
        "required": ["x", "y", "width", "height", "reason"],
        "additionalProperties": False,
    },
}


def _submit_page_tool() -> dict:
    return {
        "name": "submit_schematic_page",
        "description": (
            "Submit the structured analysis of one schematic page as a "
            "SchematicPageGraph payload. Call this once you have read every "
            "component you need, including any zooms you requested."
        ),
        "input_schema": SchematicPageGraph.model_json_schema(),
    }


AGENTIC_SUFFIX = (
    "\n\nYou have access to two tools:\n"
    "  - `request_zoom(x, y, width, height, reason)` — get a high-res crop of a region\n"
    "  - `submit_schematic_page(...)` — final structured submission\n\n"
    "Workflow: skim the full page first. For each region that looks dense or "
    "small (values tables, BOM rows, IC pinout boxes), call `request_zoom` once "
    "with generous bounds. Read the crop, then either zoom further or move on. "
    "When you have read every component confidently, call `submit_schematic_page`. "
    "Keep zooms targeted — 3 to 6 well-chosen crops is usually enough. Always "
    "end with `submit_schematic_page`; do not return a text-only response."
)


def _render_full_page(pdf_path: Path, page_num: int, dpi: int) -> Image.Image:
    """Render one PDF page at the requested DPI, return a PIL RGB image."""
    with pdfplumber.open(str(pdf_path)) as pdf:
        if page_num < 1 or page_num > len(pdf.pages):
            raise ValueError(f"page {page_num} out of range (1..{len(pdf.pages)})")
        page = pdf.pages[page_num - 1]
        return page.to_image(resolution=dpi).original.convert("RGB")


def _png_b64(img: Image.Image, *, max_dim: int = 1568) -> tuple[str, tuple[int, int]]:
    """Return (base64-PNG, (sent_w, sent_h)). Resizes to max_dim on long edge."""
    if max(img.size) > max_dim:
        ratio = max_dim / max(img.size)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii"), img.size


def _crop_image(full: Image.Image, x: int, y: int, w: int, h: int) -> Image.Image:
    """Crop, clamping to image bounds. Returns RGB."""
    x = max(0, min(x, full.width - 1))
    y = max(0, min(y, full.height - 1))
    w = max(1, min(w, full.width - x))
    h = max(1, min(h, full.height - y))
    return full.crop((x, y, x + w, y + h))


async def extract_page_agentic(
    *,
    client: AsyncAnthropic,
    model: str,
    pdf_path: Path,
    page_num: int,
    total_pages: int,
    device_label: str,
    dpi: int = 200,
    max_iterations: int = 12,
) -> tuple[SchematicPageGraph, dict]:
    """Run the agentic vision loop on one page. Returns (graph, stats)."""
    full_image = _render_full_page(pdf_path, page_num, dpi)
    log.info(
        "[page %d] rendered at %d dpi → %dx%d px",
        page_num, dpi, full_image.width, full_image.height,
    )

    initial_b64, sent_size = _png_b64(full_image, max_dim=1568)
    log.info("[page %d] full-page sent at %dx%d (downsampled from %dx%d)",
             page_num, sent_size[0], sent_size[1],
             full_image.width, full_image.height)

    # Translate model→source coords on every zoom
    sx_ratio = full_image.width / sent_size[0]
    sy_ratio = full_image.height / sent_size[1]

    context_line = (
        f"Device: {device_label}. Page {page_num} of {total_pages}. "
        f"Source image dimensions: {sent_size[0]} x {sent_size[1]} pixels."
    )
    initial_user_content = [
        {"type": "text", "text": context_line},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": initial_b64},
        },
        {
            "type": "text",
            "text": (
                "Begin. Triage the page, request high-res zooms for any dense "
                "regions, then submit a complete SchematicPageGraph via "
                "submit_schematic_page."
            ),
        },
    ]

    messages: list[dict] = [{"role": "user", "content": initial_user_content}]
    tools = [REQUEST_ZOOM_TOOL, _submit_page_tool()]
    system_cached = [
        {
            "type": "text",
            "text": SYSTEM_PROMPT + AGENTIC_SUFFIX,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    stats = {
        "iterations": 0,
        "zooms": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "elapsed_s": 0.0,
        "submitted": False,
        "stop_reason": None,
        "zoom_log": [],
    }

    t0 = time.monotonic()
    for it in range(max_iterations):
        stats["iterations"] = it + 1
        log.info("[page %d] iteration %d, history=%d msgs", page_num, it + 1, len(messages))

        response = await client.messages.create(
            model=model,
            max_tokens=16000,
            system=system_cached,
            tools=tools,
            messages=messages,
        )

        u = response.usage
        stats["input_tokens"] += u.input_tokens
        stats["output_tokens"] += u.output_tokens
        stats["cache_read_input_tokens"] += u.cache_read_input_tokens or 0
        stats["cache_creation_input_tokens"] += u.cache_creation_input_tokens or 0
        stats["stop_reason"] = response.stop_reason

        log.info(
            "[page %d] iter %d: stop=%s in=%d out=%d cache_read=%d",
            page_num, it + 1, response.stop_reason,
            u.input_tokens, u.output_tokens, u.cache_read_input_tokens or 0,
        )

        # Append assistant message verbatim (preserves tool_use blocks)
        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            log.warning("[page %d] no tool_use in response, ending loop", page_num)
            break

        tool_results: list[dict] = []
        submission = None
        for tu in tool_uses:
            if tu.name == "submit_schematic_page":
                submission = tu.input
                # The harness must send a tool_result back even on submission
                # to keep messages well-formed, but we'll exit before next call.
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": "submitted",
                })
            elif tu.name == "request_zoom":
                inp = tu.input
                # Translate from sent-image coords back to source-image coords
                src_x = int(inp["x"] * sx_ratio)
                src_y = int(inp["y"] * sy_ratio)
                src_w = int(inp["width"] * sx_ratio)
                src_h = int(inp["height"] * sy_ratio)
                stats["zooms"] += 1
                stats["zoom_log"].append({
                    "iter": it + 1,
                    "model_coords": {"x": inp["x"], "y": inp["y"], "w": inp["width"], "h": inp["height"]},
                    "source_coords": {"x": src_x, "y": src_y, "w": src_w, "h": src_h},
                    "reason": inp.get("reason", ""),
                })
                crop = _crop_image(full_image, src_x, src_y, src_w, src_h)
                log.info(
                    "[page %d] zoom %d: model_box=(%d,%d %dx%d) src_box=(%d,%d %dx%d) → crop %dx%d — %s",
                    page_num, stats["zooms"],
                    inp["x"], inp["y"], inp["width"], inp["height"],
                    src_x, src_y, src_w, src_h, crop.width, crop.height,
                    inp.get("reason", "")[:80],
                )
                crop_b64, _ = _png_b64(crop, max_dim=1568)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/png", "data": crop_b64},
                        },
                        {
                            "type": "text",
                            "text": f"Crop of region ({inp['x']}, {inp['y']}, {inp['width']}x{inp['height']}) at full source resolution.",
                        },
                    ],
                })
            else:
                log.warning("[page %d] unexpected tool: %s", page_num, tu.name)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": f"unknown tool {tu.name}",
                    "is_error": True,
                })

        messages.append({"role": "user", "content": tool_results})

        if submission is not None:
            stats["submitted"] = True
            stats["elapsed_s"] = time.monotonic() - t0
            stats["raw_submission_keys"] = sorted(list(submission.keys())) if isinstance(submission, dict) else None
            # Defensive unwrap: Opus 4.7 occasionally wraps the tool input under
            # a single `$PARAMETER_NAME` key (schema-template literal that the
            # model interpreted as a parameter name). Observed on dense pages
            # with long `description` fields. Unwrap once if the shape matches.
            if (
                isinstance(submission, dict)
                and len(submission) == 1
                and "$PARAMETER_NAME" in submission
                and isinstance(submission["$PARAMETER_NAME"], dict)
            ):
                log.warning(
                    "[page %d] unwrapping $PARAMETER_NAME wrapper (Opus template-literal quirk)",
                    page_num,
                )
                submission = submission["$PARAMETER_NAME"]
                stats["unwrapped_parameter_name"] = True

            stats["raw_submission_dump"] = submission
            try:
                graph = SchematicPageGraph.model_validate(submission)
            except Exception:
                log.error("[page %d] submission keys: %s",
                          page_num, sorted(list(submission.keys())) if isinstance(submission, dict) else None)
                log.error("[page %d] submission preview: %r", page_num, str(submission)[:600])
                raise
            return graph, stats

    stats["elapsed_s"] = time.monotonic() - t0
    raise RuntimeError(
        f"page {page_num}: agent did not submit after {max_iterations} iterations"
    )


def _diff_against_baseline(
    agentic: SchematicPageGraph, baseline_path: Path
) -> dict:
    if not baseline_path.exists():
        return {"baseline_missing": True}

    base_raw = json.loads(baseline_path.read_text())
    base = SchematicPageGraph.model_validate(base_raw)

    a_refdes = {n.refdes for n in agentic.nodes}
    b_refdes = {n.refdes for n in base.nodes}
    a_nets = {n.label for n in agentic.nets}
    b_nets = {n.label for n in base.nets}
    a_edges = {(e.src, e.dst, e.kind) for e in agentic.typed_edges}
    b_edges = {(e.src, e.dst, e.kind) for e in base.typed_edges}

    return {
        "components": {
            "agentic": len(a_refdes),
            "baseline": len(b_refdes),
            "shared": len(a_refdes & b_refdes),
            "agentic_only": sorted(a_refdes - b_refdes),
            "baseline_only": sorted(b_refdes - a_refdes),
        },
        "nets": {
            "agentic": len(a_nets),
            "baseline": len(b_nets),
            "shared": len(a_nets & b_nets),
            "agentic_only_count": len(a_nets - b_nets),
            "baseline_only_count": len(b_nets - a_nets),
        },
        "typed_edges": {
            "agentic": len(a_edges),
            "baseline": len(b_edges),
            "shared": len(a_edges & b_edges),
        },
        "confidence": {"agentic": agentic.confidence, "baseline": base.confidence},
    }


def _cost_estimate(stats: dict, *, input_per_m: float = 5.0, output_per_m: float = 25.0) -> float:
    """Rough $ estimate. Cache reads cost ~10% of normal input — approximate that."""
    billable_input = stats["input_tokens"]
    cache_read = stats["cache_read_input_tokens"]
    cache_create = stats["cache_creation_input_tokens"]
    # input_tokens excludes cache. Cache reads at ~0.1x, cache create at ~1.25x.
    cost = (
        billable_input * input_per_m
        + cache_read * input_per_m * 0.1
        + cache_create * input_per_m * 1.25
        + stats["output_tokens"] * output_per_m
    ) / 1_000_000
    return cost


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument(
        "--pages",
        required=True,
        help="Comma-separated 1-based page numbers (e.g. 6 or 6,17,31)",
    )
    parser.add_argument("--out-dir", default="/tmp/bench_agentic/")
    parser.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL_MAIN", "claude-opus-4-7"))
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--max-iterations", type=int, default=12)
    args = parser.parse_args()

    pdf_path = Path(args.pdf).expanduser().resolve()
    if not pdf_path.exists():
        raise SystemExit(f"PDF not found: {pdf_path}")

    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    with pdfplumber.open(str(pdf_path)) as pdf:
        total_pages = len(pdf.pages)

    pages = [int(p) for p in args.pages.split(",") if p.strip()]

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY not set")
    client = AsyncAnthropic(api_key=api_key)

    baseline_dir = ROOT / "memory" / args.slug / "schematic_pages"

    summary = []
    for page_num in pages:
        log.info("=" * 60)
        log.info("=== PAGE %d ===", page_num)
        log.info("=" * 60)
        try:
            graph, stats = await extract_page_agentic(
                client=client,
                model=args.model,
                pdf_path=pdf_path,
                page_num=page_num,
                total_pages=total_pages,
                device_label=args.slug,
                dpi=args.dpi,
                max_iterations=args.max_iterations,
            )
        except Exception as exc:
            log.error("[page %d] FAILED: %s", page_num, exc)
            summary.append({"page": page_num, "error": str(exc)})
            continue

        # Persist agentic output
        (out_dir / f"page_{page_num:03d}_agentic.json").write_text(graph.model_dump_json(indent=2))
        (out_dir / f"page_{page_num:03d}_stats.json").write_text(json.dumps(stats, indent=2))

        baseline_path = baseline_dir / f"page_{page_num:03d}.json"
        diff = _diff_against_baseline(graph, baseline_path)
        cost = _cost_estimate(stats)

        log.info("[page %d] DONE — %d nodes / %d nets / %d edges / conf=%.2f",
                 page_num, len(graph.nodes), len(graph.nets),
                 len(graph.typed_edges), graph.confidence)
        log.info("[page %d] zooms=%d iter=%d elapsed=%.1fs cost≈$%.4f",
                 page_num, stats["zooms"], stats["iterations"],
                 stats["elapsed_s"], cost)

        if "components" in diff:
            log.info(
                "[page %d] vs baseline — components: agentic=%d / baseline=%d / shared=%d (+%d / -%d)",
                page_num,
                diff["components"]["agentic"], diff["components"]["baseline"],
                diff["components"]["shared"],
                len(diff["components"]["agentic_only"]),
                len(diff["components"]["baseline_only"]),
            )
            log.info(
                "[page %d] vs baseline — nets: agentic=%d / baseline=%d / shared=%d",
                page_num,
                diff["nets"]["agentic"], diff["nets"]["baseline"], diff["nets"]["shared"],
            )
            log.info(
                "[page %d] vs baseline — edges: agentic=%d / baseline=%d / shared=%d",
                page_num,
                diff["typed_edges"]["agentic"], diff["typed_edges"]["baseline"],
                diff["typed_edges"]["shared"],
            )

        summary.append({"page": page_num, "diff": diff, "stats": stats, "cost_usd": cost})

    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    log.info("=== Wrote %s ===", out_dir / "summary.json")


if __name__ == "__main__":
    asyncio.run(main())
