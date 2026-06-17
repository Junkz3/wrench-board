"""Graph↔boardview coverage gate — the post-build completeness check.

The vision-built electrical graph extracts what the SCHEMATIC shows; the
boardview (.pcb/.brd/.tvw) lists what is PHYSICALLY on the board. Comparing
the two gives an objective, $0 measure of pack completeness — independent of
the vision pipeline AND of the PDF (it also flags incomplete source PDFs,
which no self-check can see).

Calibrated on the three real pilots (2026-06-12):

  pack        nets   components(raw)  missing-critical
  A2338       97.9%  83.6%            4 RF inductors      → PASS
  iPhone 8    98.9%  91.4%            5 RF inductors      → PASS
  iPhone 11   93.8%  84.6%            12 (source-PDF gap) → WARN (review)

Two systematic artefacts the comparison must neutralise:
  - the boardview carries test pads / straps (TPU, TP, PP, XW, FID, MP) the
    schematic legitimately never draws as components (A2338: 310 TPU);
  - the vision suffixes refdes on region-marked pages (C300_K, L7700_W) —
    base-name matching credits them.

Verdict thresholds (net coverage is the diagnostic backbone):
  PASS: nets ≥ 0.90 AND missing-critical ≤ 8
  FAIL: nets < 0.75 OR missing-critical > 25
  WARN: everything in between → human review before seeding.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("wrench_board.pipeline.qa.graph_coverage")

# Physical artefacts present on the PCB but legitimately absent from a
# schematic's component set: test pads (TP/TPU), bare power pads (PP),
# solder straps/jumpers (XW, W when standalone), fiducials, mounting points.
EXCLUDED_FAMILIES = ("TPU", "TP", "PP", "XW", "FID", "MP")

# Families whose absence breaks diagnostic chains: ICs, mosfets, connectors,
# inductors (rail filters), fuses, diodes, transformers.
CRITICAL_PREFIX = re.compile(r"^(U|Q|J|L|F|D|T)\d")

PASS_NET_FLOOR = 0.90
FAIL_NET_FLOOR = 0.75
PASS_CRITICAL_MAX = 8
FAIL_CRITICAL_MAX = 25


def _family(refdes: str) -> str:
    m = re.match(r"[A-Z]+", refdes)
    return m.group(0) if m else ""


def _excluded(refdes: str) -> bool:
    fam = _family(refdes)
    # Longest-prefix semantics: TPU beats TP; PP only when followed by digits
    # (PP0500 pad) — a refdes like PPX would still be excluded by family PP.
    return any(fam == f or (f != "PP" and fam.startswith(f) and fam != f)
               for f in EXCLUDED_FAMILIES) or fam in EXCLUDED_FAMILIES


def _base(refdes: str) -> str:
    """Strip the vision's page-region suffix (C300_K → C300)."""
    return refdes.split("_")[0]


@dataclass
class CoverageReport:
    component_coverage: float
    net_coverage: float
    board_components: int
    board_components_considered: int
    board_nets: int
    graph_components: int
    graph_nets: int
    missing_components: list[str]
    missing_critical: list[str]
    missing_nets: list[str]
    ghosts: list[str]
    excluded_families: dict[str, int] = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        crit = len(self.missing_critical)
        if self.net_coverage < FAIL_NET_FLOOR or crit > FAIL_CRITICAL_MAX:
            return "FAIL"
        if self.net_coverage >= PASS_NET_FLOOR and crit <= PASS_CRITICAL_MAX:
            return "PASS"
        return "WARN"

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "component_coverage": round(self.component_coverage, 4),
            "net_coverage": round(self.net_coverage, 4),
            "board_components": self.board_components,
            "board_components_considered": self.board_components_considered,
            "board_nets": self.board_nets,
            "graph_components": self.graph_components,
            "graph_nets": self.graph_nets,
            "missing_components": self.missing_components,
            "missing_critical": self.missing_critical,
            "missing_nets": self.missing_nets,
            "ghosts": self.ghosts,
            "excluded_families": self.excluded_families,
        }

    def summary(self) -> str:
        lines = [
            f"verdict: {self.verdict}",
            f"nets:        {self.net_coverage:.1%} "
            f"({len(self.missing_nets)} missing of {self.board_nets})",
            f"components:  {self.component_coverage:.1%} "
            f"({len(self.missing_components)} missing of "
            f"{self.board_components_considered} considered, "
            f"{self.board_components} on board)",
            f"critical missing ({len(self.missing_critical)}): "
            + ", ".join(self.missing_critical[:15])
            + ("…" if len(self.missing_critical) > 15 else ""),
            f"ghosts (graph-only, DNP/rev-skew?): {len(self.ghosts)}",
        ]
        return "\n".join(lines)


def compare_graph_to_board(
    *,
    graph: dict,
    board_refdes: list[str],
    board_nets: list[str],
) -> CoverageReport:
    """Compare an electrical_graph dict to the boardview's parts/nets lists."""
    graph_refdes = {str(k).upper() for k in graph.get("components", {})}
    graph_bases = {_base(r) for r in graph_refdes}
    graph_nets = {str(k).upper() for k in graph.get("nets", {})}

    bv_all = {r.upper() for r in board_refdes if r}
    excluded_counts: dict[str, int] = {}
    bv_considered = set()
    for r in bv_all:
        if _excluded(r):
            fam = _family(r)
            excluded_counts[fam] = excluded_counts.get(fam, 0) + 1
        else:
            bv_considered.add(r)

    # Suffixes exist on BOTH sides (vision region-suffixes C300_K; some
    # boardviews ship suffixed refdes too, e.g. L7700_W) — match on bases.
    missing = sorted(
        r for r in bv_considered
        if r not in graph_refdes
        and r not in graph_bases
        and _base(r) not in graph_refdes
        and _base(r) not in graph_bases
    )
    missing_critical = [m for m in missing if CRITICAL_PREFIX.match(m)]

    bv_nets = {n.upper() for n in board_nets if n}
    missing_nets = sorted(n for n in bv_nets if n not in graph_nets)

    ghosts = sorted(
        r for r in graph_refdes
        if r not in bv_all and _base(r) not in bv_all
    )

    return CoverageReport(
        component_coverage=1 - len(missing) / max(1, len(bv_considered)),
        net_coverage=1 - len(missing_nets) / max(1, len(bv_nets)),
        board_components=len(bv_all),
        board_components_considered=len(bv_considered),
        board_nets=len(bv_nets),
        graph_components=len(graph_refdes),
        graph_nets=len(graph_nets),
        missing_components=missing,
        missing_critical=missing_critical,
        missing_nets=missing_nets,
        ghosts=ghosts,
        excluded_families=excluded_counts,
    )


def run_coverage_gate(pack_dir: Path, boardview_path: Path | None) -> str | None:
    """Lot 3 — post-build QA gate, wired into the orchestrator.

    When a build produced an electrical graph AND the technician supplied a
    boardview, compare the two (the boardview is the independent physical ground
    truth) and write `coverage_report.json` to the pack. Returns the verdict
    (PASS / WARN / FAIL) so the orchestrator can keep a FAIL pack out of the
    shared commons (an incomplete source PDF must not become the authoritative
    pack). Returns None when there is nothing to compare (no graph or no
    boardview). Best-effort: any parse/IO error logs and returns None — the QA
    gate must NEVER crash a build.
    """
    pack_dir = Path(pack_dir)
    graph_path = pack_dir / "electrical_graph.json"
    if not graph_path.is_file():
        return None
    if boardview_path is None or not Path(boardview_path).is_file():
        return None
    try:
        from api.board.parser import parser_for  # local — heavy registry import

        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        bv_path = Path(boardview_path)
        board = parser_for(bv_path).parse(
            bv_path.read_bytes(), file_hash="coverage-gate", board_id=pack_dir.name
        )
        report = compare_graph_to_board(
            graph=graph,
            board_refdes=[p.refdes for p in board.parts],
            board_nets=[n.name for n in board.nets],
        )
        (pack_dir / "coverage_report.json").write_text(
            json.dumps(report.to_dict(), indent=2), encoding="utf-8"
        )
        logger.info(
            "[Coverage] %s vs %s → %s (nets %.1f%%, missing-critical %d)",
            pack_dir.name,
            bv_path.name,
            report.verdict,
            report.net_coverage * 100,
            len(report.missing_critical),
        )
        return report.verdict
    except Exception:  # noqa: BLE001 — the QA gate must never crash a build
        logger.exception("[Coverage] gate failed for %s — skipping", pack_dir.name)
        return None
