#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
"""Generate benchable scenarios from a device's knowledge pack.

Reads memory/{slug}/ and writes benchmark/auto_proposals/{slug}-YYYY-MM-DD.*
+ memory/{slug}/simulator_reliability.json. See spec §6 for full CLI
surface and exit codes.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from anthropic import AsyncAnthropic

from api.config import get_settings
from api.pipeline.bench_generator.errors import (
    BenchGeneratorLLMError,
    BenchGeneratorPreconditionError,
)
from api.pipeline.bench_generator.orchestrator import generate_from_pack

logger = logging.getLogger("microsolder.bench_generator.cli")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate benchable scenarios from a device knowledge pack.",
    )
    p.add_argument("--slug", required=True, help="Device slug (memory/{slug}/)")
    p.add_argument(
        "--model",
        default=None,
        help=(
            "Sonnet model id. Defaults to settings.anthropic_model_sonnet "
            "(from .env) or 'claude-sonnet-4-6' if unset."
        ),
    )
    p.add_argument(
        "--escalate-rejects",
        action="store_true",
        help="Re-propose rejected scenarios via Opus (claude-opus-4-7).",
    )
    p.add_argument(
        "--output-dir",
        default="benchmark/auto_proposals",
        help="Proposals destination (default: benchmark/auto_proposals).",
    )
    p.add_argument(
        "--memory-root",
        default="memory",
        help="Device memory root (default: memory/).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print summary, do not write. (Currently still hits LLM — budget the tokens.)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


async def main_async(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    model = args.model or (getattr(settings, "anthropic_model_sonnet", None) or "claude-sonnet-4-6")
    client = AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        max_retries=5,
    )
    output_dir = Path(args.output_dir)
    latest_path = output_dir / "_latest.json"
    run_date = datetime.now(UTC).date().isoformat()
    memory_root = Path(args.memory_root)

    try:
        summary = await generate_from_pack(
            device_slug=args.slug,
            client=client,
            model=model,
            memory_root=memory_root,
            output_dir=output_dir,
            latest_path=latest_path,
            run_date=run_date,
            escalate_rejects=args.escalate_rejects,
        )
    except BenchGeneratorPreconditionError as exc:
        logger.error("Precondition failed: %s", exc)
        return 2
    except BenchGeneratorLLMError as exc:
        logger.error("LLM failure after retries: %s", exc)
        return 3

    print(
        f"slug={args.slug} n_proposed={summary['n_proposed']} "
        f"accepted={summary['n_accepted']} rejected={summary['n_rejected']} "
        f"score={summary['score']:.3f} "
        f"(self_mrr={summary['self_mrr']:.3f}, "
        f"cascade_recall={summary['cascade_recall']:.3f})"
    )
    if args.dry_run:
        logger.warning(
            "--dry-run noted: files were STILL written (dry-run only skips "
            "the Opus escalate pass). Use --help for details.",
        )
    if summary["n_accepted"] == 0:
        return 1
    return 0


def main() -> int:
    return asyncio.run(main_async(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
