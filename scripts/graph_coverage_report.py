#!/usr/bin/env python3
"""Post-build QA gate — graph↔boardview coverage report for one pack.

Usage:
    python scripts/graph_coverage_report.py <pack_slug_or_dir> <boardview.pcb> [--json out.json]

Compares memory/{slug}/electrical_graph.json against the physical boardview
(every part/net actually on the PCB, parsed by the engine's own board
parsers) and prints a PASS/WARN/FAIL verdict + the missing-critical list.
Writes the full report to memory/{slug}/coverage_report.json (or --json).

Run it after every catalogue pre-build BEFORE seeding prod:
    PASS → seed; WARN → review the missing list (often an incomplete source
    PDF — find a better scan); FAIL → do not seed, investigate.

Requires the boardview cipher keys in the environment / .env
(WRENCH_BOARD_XZZ_KEY / WRENCH_BOARD_FZ_KEY for .pcb / .fz).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.board.parser import parser_for  # noqa: E402
from api.pipeline.qa.graph_coverage import compare_graph_to_board  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pack", help="pack slug (under memory/) or pack directory")
    ap.add_argument("boardview", help="boardview file (.pcb/.brd/.fz/.tvw/...)")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="where to write the JSON report (default: <pack>/coverage_report.json)")
    args = ap.parse_args()

    pack_dir = Path(args.pack)
    if not pack_dir.is_dir():
        pack_dir = Path("memory") / args.pack
    graph_path = pack_dir / "electrical_graph.json"
    if not graph_path.is_file():
        print(f"ERROR: {graph_path} not found", file=sys.stderr)
        return 2

    bv_path = Path(args.boardview)
    if not bv_path.is_file():
        print(f"ERROR: {bv_path} not found", file=sys.stderr)
        return 2

    graph = json.loads(graph_path.read_text())
    board = parser_for(bv_path).parse(
        bv_path.read_bytes(),
        file_hash="coverage-check",
        board_id=pack_dir.name,
    )

    report = compare_graph_to_board(
        graph=graph,
        board_refdes=[p.refdes for p in board.parts],
        board_nets=[n.name for n in board.nets],
    )

    out = Path(args.json_out) if args.json_out else pack_dir / "coverage_report.json"
    out.write_text(json.dumps(report.to_dict(), indent=2))

    print(f"\n===== coverage {pack_dir.name} vs {bv_path.name} =====")
    print(report.summary())
    print(f"\nreport: {out}")
    return 0 if report.verdict == "PASS" else (1 if report.verdict == "WARN" else 3)


if __name__ == "__main__":
    raise SystemExit(main())
