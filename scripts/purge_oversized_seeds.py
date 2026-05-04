#!/usr/bin/env python3
"""One-shot purge of memory entries that are now served by mb_schematic_graph.

Background — 2026-04-28: `electrical_graph.json` and `nets_classified.json`
are no longer seeded into MA memory stores (they exceed the 102_400-byte
per-memory cap). They are served by the `mb_schematic_graph` tool instead.
This script removes the orphan entries left behind on already-seeded
devices and trims the on-disk `managed.json` marker so the seeder won't
see them as "stale" on the next pipeline pass.

Usage:
    .venv/bin/python scripts/purge_oversized_seeds.py [--dry-run]

Walks every `memory/{slug}/managed.json`, deletes the two paths from the
referenced store, and rewrites the marker. Idempotent.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / ".env")

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
API_BASE = "https://api.anthropic.com/v1"
BETA_HEADER = "managed-agents-2026-04-01"

PURGE_PATHS = (
    "/knowledge/electrical_graph.json",
    "/knowledge/nets_classified.json",
)
PURGE_FILES = ("electrical_graph.json", "nets_classified.json")


def _hdrs() -> dict[str, str]:
    return {
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": BETA_HEADER,
    }


def _list_paths_to_ids(client: httpx.Client, store_id: str) -> dict[str, str]:
    """Return {path: memory_id} for every leaf memory in the store."""
    out: dict[str, str] = {}
    cursor: str | None = None
    while True:
        params: dict = {"limit": 100, "view": "basic"}
        if cursor:
            params["after_id"] = cursor
        resp = client.get(
            f"{API_BASE}/memory_stores/{store_id}/memories",
            headers=_hdrs(),
            params=params,
        )
        resp.raise_for_status()
        body = resp.json()
        page = body.get("data", [])
        for item in page:
            if item.get("type") != "memory":
                continue
            mid = item.get("id")
            mpath = item.get("path")
            if isinstance(mid, str) and isinstance(mpath, str):
                out[mpath] = mid
        if not body.get("has_more"):
            break
        cursor = page[-1]["id"] if page else None
        if not cursor:
            break
    return out


def _delete(client: httpx.Client, store_id: str, memory_id: str) -> bool:
    resp = client.delete(
        f"{API_BASE}/memory_stores/{store_id}/memories/{memory_id}",
        headers=_hdrs(),
    )
    return resp.status_code in (200, 204, 404)


def _trim_marker(marker_path: Path) -> bool:
    """Drop the purged filenames from the marker's `files` map. Returns True
    if the marker was rewritten."""
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    files = data.get("files")
    if not isinstance(files, dict):
        return False
    removed = [k for k in PURGE_FILES if k in files]
    if not removed:
        return False
    for k in removed:
        files.pop(k, None)
    marker_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not API_KEY:
        print("ERROR: ANTHROPIC_API_KEY missing", file=sys.stderr)
        return 2

    markers = sorted((REPO / "memory").glob("*/managed.json"))
    if not markers:
        print("No memory/{slug}/managed.json found — nothing to do.")
        return 0

    print(f"Scanning {len(markers)} device(s)…")
    total_deleted = 0
    total_trimmed = 0
    with httpx.Client(timeout=30.0) as client:
        for marker_path in markers:
            slug = marker_path.parent.name
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                print(f"  [{slug}] marker unreadable: {exc}")
                continue
            store_id = marker.get("memory_store_id") or marker.get("store_id")
            if not store_id:
                print(f"  [{slug}] no store_id in marker — skip")
                continue

            try:
                paths = _list_paths_to_ids(client, store_id)
            except httpx.HTTPStatusError as exc:
                print(
                    f"  [{slug}] list failed {exc.response.status_code}: "
                    f"{exc.response.text[:120]}"
                )
                continue
            except httpx.HTTPError as exc:
                print(f"  [{slug}] list raised: {exc}")
                continue

            targets = [(p, paths[p]) for p in PURGE_PATHS if p in paths]
            if not targets:
                print(f"  [{slug}] {store_id}: clean already")
            else:
                for path, mid in targets:
                    if args.dry_run:
                        print(f"  [{slug}] would delete {path} (id={mid})")
                    else:
                        ok = _delete(client, store_id, mid)
                        flag = "✓" if ok else "✗"
                        print(f"  [{slug}] {flag} delete {path}")
                        if ok:
                            total_deleted += 1

            if not args.dry_run and _trim_marker(marker_path):
                total_trimmed += 1
                print(f"  [{slug}] marker trimmed")

    if args.dry_run:
        print("\nDRY RUN — re-run without --dry-run to apply.")
    else:
        print(f"\nDeleted {total_deleted} memories, trimmed {total_trimmed} markers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
