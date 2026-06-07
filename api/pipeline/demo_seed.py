# api/pipeline/demo_seed.py
"""Seed shipped demo packs into the runtime memory root at boot.

memory/ is the mutable runtime store (gitignored, written by the agent), so the
demo pack ships as a pristine fixture under fixtures/demo-packs/ and is copied
in ONLY when the slug is absent. Idempotent and non-destructive: an existing
pack (a self-hoster's real device, or a prior seed the agent has since enriched)
is never overwritten. Self-host and cloud both call this; the copy lands at the
pack root (owner None path), which the per-owner readers fall back to.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_FIXTURES_ROOT = Path(__file__).resolve().parents[2] / "fixtures"


def seed_demo_packs(memory_root: Path, *, fixtures_root: Path | None = None) -> int:
    """Copy each fixtures/demo-packs/{slug} into memory/{slug} if absent.

    Returns the count of packs newly seeded. Never raises on a single-pack
    failure (logs + continues) — a broken demo pack must not block startup.
    """
    fixtures_root = fixtures_root or _DEFAULT_FIXTURES_ROOT
    demo_dir = fixtures_root / "demo-packs"
    if not demo_dir.is_dir():
        return 0
    memory_root.mkdir(parents=True, exist_ok=True)

    seeded = 0
    for pack in sorted(demo_dir.iterdir()):
        if not pack.is_dir():
            continue
        target = memory_root / pack.name
        try:
            if target.exists():
                # Pack already present (a self-hoster's real device, or a prior
                # seed). Don't clobber it — but BACKFILL any demo file that's
                # missing, notably repairs/example-*.json, so the example tour's
                # repair resolves even when the analyzed pack predates this seed.
                added = _backfill_missing(pack, target)
                if added:
                    logger.info("backfilled %d demo file(s) into existing pack %s", added, pack.name)
                continue
            shutil.copytree(pack, target)
            seeded += 1
            logger.info("seeded demo pack %s", pack.name)
        except OSError as exc:  # noqa: BLE001 — one bad pack must not block boot
            logger.warning("demo-pack seed failed for %s: %s", pack.name, exc)
    return seeded


def _backfill_missing(src: Path, dst: Path) -> int:
    """Copy files present in `src` but absent in `dst` (never overwrite). Returns
    the count added. Same-slug ⇒ same device, so the fixture's files are the
    right ones to fill gaps with."""
    added = 0
    for s in src.rglob("*"):
        rel = s.relative_to(src)
        d = dst / rel
        if s.is_dir():
            d.mkdir(parents=True, exist_ok=True)
        elif not d.exists():
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
            added += 1
    return added
