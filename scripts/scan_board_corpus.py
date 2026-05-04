#!/usr/bin/env python3
"""Scan a directory tree of boardview fixtures through `parser_for` dispatch
and report per-format pass/fail rates plus aggregate parts/pins/nets totals.

Useful for measuring the impact of a parser change before/after, and for
spotting regressions when a parser is updated. Defaults to scanning the
local `XZZ Graphics card` and `XZZ LCD&TV` corpora that Alexis uses for
manual validation.

Usage:
    .venv/bin/python scripts/scan_board_corpus.py
    .venv/bin/python scripts/scan_board_corpus.py /path/to/boards/
"""
from __future__ import annotations

import collections
import logging
import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)
logging.disable(logging.CRITICAL)

DEFAULT_ROOTS = [
    "/home/alex/Documents/Boardview XZZ/XZZ Graphics card/",
    "/home/alex/Documents/Boardview XZZ/XZZ LCD&TV/",
]
SUPPORTED_EXT = {
    ".pcb", ".cad", ".fz", ".tvw", ".brd", ".bv", ".bvr",
    ".asc", ".bdv", ".cst", ".f2b", ".gr", ".kicad_pcb",
}


def _has_cjk(s: str) -> bool:
    return any(0x4e00 <= ord(c) <= 0x9fff for c in s)


def _find_files(roots: list[str]) -> list[str]:
    out: list[str] = []
    for root in roots:
        for dirpath, _, files in os.walk(root):
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in SUPPORTED_EXT:
                    out.append(os.path.join(dirpath, f))
    return out


def main() -> int:
    from api.board.parser import parser_for

    roots = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_ROOTS
    missing = [r for r in roots if not os.path.isdir(r)]
    if missing:
        print(f"ERROR: roots not found: {missing}", file=sys.stderr)
        return 2

    files = _find_files(roots)
    by_ext: dict[str, list[str]] = collections.defaultdict(list)
    for p in files:
        by_ext[os.path.splitext(p)[1].lower()].append(p)

    print(f"Scanning {len(files)} files across {len(by_ext)} formats")
    for ext, lst in sorted(by_ext.items()):
        print(f"  {ext:12s} {len(lst)}")
    print()

    stats: dict[str, dict] = {}
    for ext in by_ext:
        stats[ext] = dict(
            ok=0, fail=0, parts=0, pins=0, unique_xy=0, nets=0,
            cjk_files=0, time_ms=0.0, errors=[],
        )

    for i, path in enumerate(files, 1):
        ext = os.path.splitext(path)[1].lower()
        s = stats[ext]
        ppath = Path(path)
        try:
            parser_inst = parser_for(ppath)
        except Exception as e:
            s["fail"] += 1
            s["errors"].append((path, f"dispatch: {type(e).__name__}: {e}"))
            continue
        try:
            t0 = time.perf_counter()
            board = parser_inst.parse_file(ppath)
            dt = (time.perf_counter() - t0) * 1000
            s["time_ms"] += dt
            s["ok"] += 1
            s["parts"] += len(board.parts)
            s["pins"] += len(board.pins)
            # Unique-coordinate pad count — collapses multi-layer via
            # records (one per layer the via traverses) into a single
            # tally. Useful for comparing formats apples-to-apples
            # since some emit records per (X, Y, layer) and others
            # per physical pad.
            s["unique_xy"] += len({(p.pos.x, p.pos.y) for p in board.pins})
            s["nets"] += len(board.nets)
            cjk_found = False
            for part in board.parts:
                for attr in ("refdes", "footprint", "value", "category"):
                    v = getattr(part, attr, None)
                    if isinstance(v, str) and _has_cjk(v):
                        cjk_found = True
                        break
                if cjk_found:
                    break
            if not cjk_found:
                for net in board.nets:
                    if isinstance(net.name, str) and _has_cjk(net.name):
                        cjk_found = True
                        break
            if cjk_found:
                s["cjk_files"] += 1
        except Exception as e:
            s["fail"] += 1
            s["errors"].append((path, f"{type(e).__name__}: {e}"))
        if i % 50 == 0:
            print(f"  ... {i}/{len(files)}")

    print()
    print(f"{'EXT':6s} {'OK':>5s} {'FAIL':>5s} {'PASS%':>6s} "
          f"{'PARTS':>8s} {'PINS':>9s} {'UNIQ_XY':>9s} {'NETS':>8s} "
          f"{'CJK':>5s} {'avg_ms':>8s}")
    for ext, s in sorted(stats.items()):
        total = s["ok"] + s["fail"]
        pct = 100.0 * s["ok"] / total if total else 0
        avg = s["time_ms"] / s["ok"] if s["ok"] else 0
        print(f"{ext:6s} {s['ok']:5d} {s['fail']:5d} {pct:6.1f} "
              f"{s['parts']:8d} {s['pins']:9d} {s['unique_xy']:9d} "
              f"{s['nets']:8d} {s['cjk_files']:5d} {avg:8.1f}")
    print()
    print("PINS    = total per-record pad count (one per layer the pad")
    print("          appears on — vias on multi-layer boards count N×).")
    print("UNIQ_XY = unique (X, Y) pad coordinates summed across files.")

    print()
    print("=== Sample errors (top 5 distinct per format) ===")
    for ext, s in sorted(stats.items()):
        if not s["errors"]:
            continue
        print(f"\n--- {ext} ({len(s['errors'])} failures) ---")
        seen: dict[str, list] = {}
        for path, msg in s["errors"]:
            kind = msg.split(":", 1)[0]
            seen.setdefault(kind, []).append((path, msg))
        for _kind, items in list(seen.items())[:5]:
            sample_path, sample_msg = items[0]
            print(f"  [{len(items):3d}×] {sample_msg[:140]}")
            print(f"         e.g. {os.path.basename(sample_path)[:90]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
