"""End-to-end smoke for the curator-driven expand_pack flow.

Calls `api.pipeline.expansion.expand_pack` with `chunk_provider` wired to
the MA KnowledgeCurator sub-agent. Full chain:

    curator (web_search + web_fetch) -> raw_dump append
                                     -> Registry (Sonnet)
                                     -> Clinicien (Opus)
                                     -> rules.json merge

Backs the pack up before, prints the deltas after, then by default
restores the backup so the pack stays clean. Pass --keep to commit the
expansion to disk.

Usage:
    .venv/bin/python scripts/smoke_expand_pack.py [--slug iphone-x]
                                                 [--symptom "earpiece dead"]
                                                 [--keep]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

load_dotenv(REPO_ROOT / ".env")

# Force line-buffered stdout so the script streams progress live when
# its stdout isn't a TTY (eg piped to a file or backgrounded). Without
# this, Python buffers the whole run and only flushes at exit.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:  # noqa: BLE001
    pass

# Surface the pipeline's INFO logs (Scout, Registry, Clinicien, Curator)
# on stderr so we see what's actually happening during the 2-3 min run.
import logging  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    stream=sys.stderr,
)

from api.agent.managed_ids import load_managed_ids  # noqa: E402
from api.agent.runtime_managed import _run_knowledge_curator  # noqa: E402
from api.config import get_settings  # noqa: E402
from api.pipeline.expansion import expand_pack  # noqa: E402

BACKUP_FILES = ("rules.json", "registry.json", "raw_research_dump.md")


def _backup(pack_dir: Path) -> dict[str, bytes | None]:
    snap: dict[str, bytes | None] = {}
    for name in BACKUP_FILES:
        p = pack_dir / name
        snap[name] = p.read_bytes() if p.exists() else None
    return snap


def _restore(pack_dir: Path, snap: dict[str, bytes | None]) -> None:
    for name, data in snap.items():
        p = pack_dir / name
        if data is None:
            if p.exists():
                p.unlink()
        else:
            p.write_bytes(data)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", default="iphone-x")
    parser.add_argument(
        "--symptom",
        default="earpiece dead but loudspeaker works",
    )
    parser.add_argument(
        "--refdes",
        default="",
        help="Comma-separated refdes to focus on (optional)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Commit the expansion to disk; default restores the backup",
    )
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 2

    settings = get_settings()
    pack_dir = Path(settings.memory_root) / args.slug
    if not pack_dir.exists():
        print(f"ERROR: no pack at {pack_dir}", file=sys.stderr)
        return 2

    rules_before = json.loads((pack_dir / "rules.json").read_text())["rules"]
    rule_ids_before = {r["id"] for r in rules_before}
    print(f"[smoke] device={args.slug}")
    print(f"[smoke] symptom={args.symptom!r}")
    print(f"[smoke] rules before: {len(rule_ids_before)}")

    ids = load_managed_ids()
    env_id = ids["environment_id"]
    if "curator" not in ids["agents"]:
        print("ERROR: 'curator' agent not bootstrapped", file=sys.stderr)
        return 2

    client = AsyncAnthropic()

    async def curator_provider(
        *,
        device_label: str,
        focus_symptoms: list[str],
        focus_refdes: list[str],
    ) -> str:
        return await _run_knowledge_curator(
            client=client,
            device_label=device_label,
            focus_symptoms=focus_symptoms,
            focus_refdes=focus_refdes,
            environment_id=env_id,
            parent_session_id="smoke-expand-pack",
            ws=None,
            timeout_s=240.0,
        )

    snap = _backup(pack_dir)
    started = time.monotonic()

    try:
        focus_refdes = (
            [r.strip() for r in args.refdes.split(",") if r.strip()]
            if args.refdes
            else []
        )
        result = await expand_pack(
            device_slug=args.slug,
            focus_symptoms=[args.symptom],
            focus_refdes=focus_refdes,
            client=client,
            memory_root=Path(settings.memory_root),
            chunk_provider=curator_provider,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"\n[smoke] FAIL: {type(exc).__name__}: {exc}")
        if not args.keep:
            _restore(pack_dir, snap)
            print("[smoke] backup restored")
        return 1

    elapsed = time.monotonic() - started
    print(f"\n[smoke] elapsed={elapsed:.1f}s")
    print(f"[smoke] result: {json.dumps(result, indent=2, default=str)}")

    rules_after = json.loads((pack_dir / "rules.json").read_text())["rules"]
    rule_ids_after = {r["id"] for r in rules_after}
    new_ids = rule_ids_after - rule_ids_before
    dropped_ids = rule_ids_before - rule_ids_after

    print(f"\n[smoke] rules after: {len(rule_ids_after)}")
    print(f"[smoke] +{len(new_ids)} new rule ids: {sorted(new_ids)[:8]}")
    if dropped_ids:
        print(f"[smoke] −{len(dropped_ids)} dropped rule ids: {sorted(dropped_ids)[:8]}")

    if new_ids:
        print("\n[smoke] new rules preview:")
        new_set = {r["id"]: r for r in rules_after if r["id"] in new_ids}
        for rid, r in list(new_set.items())[:3]:
            symptom = r.get("symptom", r.get("when", "?"))
            comp = r.get("components", r.get("refdes", "?"))
            print(f"   • {rid}: {symptom!r} → {comp}")

    if not args.keep:
        _restore(pack_dir, snap)
        print("\n[smoke] backup restored (rules.json/registry.json/raw_dump back to original)")
    else:
        print("\n[smoke] --keep: pack on disk now contains the expansion")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
