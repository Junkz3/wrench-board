#!/usr/bin/env python3
"""One-off backfill: classify `device_kind` for existing knowledge packs.

For every pack under `memory/{slug}/` that has a compiled `electrical_graph.json`
but no resolved `device_kind` in its taxonomy, run the graph-arbitrated
`classify_device_kind` classifier and stamp the verdict into the pack's taxonomy
*where `GET /pipeline/taxonomy` reads it* — so the UI readiness chip lights up.

The stamp location is migration-aware (mirrors `api/pipeline/routes/packs.py`):
  - Migrated pack (`.migrated_t8` present): get_taxonomy → `_effective_registry`
    → `_baseline_meta(pack_dir, "registry.json")` → so device_kind lives in
    `baseline/registry.json` under `_meta.taxonomy.device_kind`.
  - Non-migrated pack: get_taxonomy reads root `registry.json` → `taxonomy.device_kind`.

Usage:
    python scripts/backfill_device_kind.py --all          # all eligible packs
    python scripts/backfill_device_kind.py <slug>         # one pack
    python scripts/backfill_device_kind.py --all --force  # re-classify even if set

Streams progress live per the repo's long-running-script rules.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Long-running-script rules: stream output live.
sys.stdout.reconfigure(line_buffering=True)
logging.basicConfig(level=logging.INFO, stream=sys.stderr)

# Allow `python scripts/backfill_device_kind.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anthropic import AsyncAnthropic  # noqa: E402

from api.config import get_settings  # noqa: E402
from api.pipeline.device_kind import classify_device_kind  # noqa: E402
from api.pipeline.orchestrator import _load_existing_electrical_graph  # noqa: E402

logger = logging.getLogger("wrench_board.backfill.device_kind")

_SKIP_DIRS = {"_stock", "_profile"}


def _is_migrated(pack_dir: Path) -> bool:
    return (pack_dir / ".migrated_t8").is_file()


def _read_taxonomy_source(pack_dir: Path) -> tuple[dict, str | None]:
    """Return (taxonomy_dict, device_label) from the source get_taxonomy reads.

    Migrated → baseline/registry.json `_meta.{taxonomy,device_label}`.
    Non-migrated → root registry.json `{taxonomy, device_label}`.
    """
    if _is_migrated(pack_dir):
        path = pack_dir / "baseline" / "registry.json"
        if not path.is_file():
            return {}, None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}, None
        meta = data.get("_meta") or {}
        return (meta.get("taxonomy") or {}), meta.get("device_label")
    path = pack_dir / "registry.json"
    if not path.is_file():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, None
    return (data.get("taxonomy") or {}), data.get("device_label")


def _stamp_device_kind(pack_dir: Path, device_kind: str) -> Path:
    """Write taxonomy.device_kind into the file get_taxonomy reads. Returns path.

    Reads the whole JSON, mutates only the device_kind field, writes it back —
    never clobbers the `items` array or other `_meta`/registry keys.
    """
    if _is_migrated(pack_dir):
        path = pack_dir / "baseline" / "registry.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        meta = data.setdefault("_meta", {})
        taxonomy = meta.setdefault("taxonomy", {})
        taxonomy["device_kind"] = device_kind
    else:
        path = pack_dir / "registry.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        taxonomy = data.setdefault("taxonomy", {})
        taxonomy["device_kind"] = device_kind
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _eligible_slugs(memory_root: Path, only: str | None) -> list[str]:
    if only:
        return [only]
    out: list[str] = []
    for d in sorted(memory_root.iterdir(), key=lambda p: p.name):
        if not d.is_dir():
            continue
        if d.name in _SKIP_DIRS or d.name.startswith("."):
            continue
        out.append(d.name)
    return out


async def _backfill_one(
    *,
    client: AsyncAnthropic,
    model: str,
    memory_root: Path,
    slug: str,
    force: bool,
) -> tuple[str, str | None]:
    """Classify + stamp one pack. Returns (status, device_kind)."""
    pack_dir = memory_root / slug
    if not (pack_dir / "electrical_graph.json").exists():
        logger.info("[backfill] %s: skip — no electrical_graph.json", slug)
        return "skip_no_graph", None

    taxonomy, device_label = _read_taxonomy_source(pack_dir)
    current = taxonomy.get("device_kind")
    if current and current != "unknown" and not force:
        logger.info("[backfill] %s: skip — device_kind already set (%s)", slug, current)
        return "skip_already_set", current

    graph = _load_existing_electrical_graph(pack_dir)
    if graph is None:
        logger.warning("[backfill] %s: skip — graph load failed/None", slug)
        return "skip_graph_load_failed", None

    label = device_label or slug
    try:
        verdict = await classify_device_kind(
            client=client,
            model=model,
            device_label=label,
            graph=graph,
        )
    except Exception:  # noqa: BLE001 — report which pack failed, don't fake a result
        logger.exception("[backfill] %s: classify_device_kind FAILED", slug)
        return "classify_failed", None

    path = _stamp_device_kind(pack_dir, verdict.device_kind)
    logger.info(
        "[backfill] %s: %s (conf=%.2f) — %s [stamped → %s]",
        slug,
        verdict.device_kind,
        verdict.confidence,
        verdict.evidence,
        path,
    )
    return "stamped", verdict.device_kind


async def _main_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.anthropic_api_key:
        logger.error("[backfill] ANTHROPIC_API_KEY missing — cannot classify.")
        return 2

    memory_root = Path(settings.memory_root)
    if not memory_root.is_dir():
        logger.error("[backfill] memory_root %s is not a directory.", memory_root)
        return 2

    # Registry-role model (Sonnet) — see orchestrator models_by_role["registry"].
    model = settings.anthropic_model_sonnet
    logger.info("[backfill] memory_root=%s · model=%s · force=%s", memory_root, model, args.force)

    client = AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        max_retries=settings.anthropic_max_retries,
    )

    slugs = _eligible_slugs(memory_root, args.slug)
    results: dict[str, str | None] = {}
    for slug in slugs:
        status, kind = await _backfill_one(
            client=client,
            model=model,
            memory_root=memory_root,
            slug=slug,
            force=args.force,
        )
        if status == "stamped" or status == "skip_already_set":
            results[slug] = kind

    print("\n=== Backfill summary (slug → device_kind) ===", flush=True)
    if not results:
        print("(no packs stamped or already-set)", flush=True)
    else:
        width = max(len(s) for s in results)
        for slug in sorted(results):
            print(f"  {slug.ljust(width)}  ->  {results[slug]}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("slug", nargs="?", default=None, help="Backfill only this slug.")
    group.add_argument("--all", action="store_true", help="Backfill all eligible packs (default).")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-classify even when device_kind is already set.",
    )
    args = parser.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
