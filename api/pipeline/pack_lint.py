"""Deterministic pre-persist lint — cheap sanity checks on a generated pack.

Catches the failure modes a graph-blind Scout produces: rules that hedge across
two device kinds, rules citing rails absent from the schematic, and packs left
unclassified despite a graph. Findings feed the pack-quality signal; `reject`
severity should block auto-publish, `warn` should surface a badge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from api.pipeline.schemas import Registry

# Word-boundary markers that, co-occurring in one pack's rules, signal a
# hybrid laptop/GPU/phone dump.
_LAPTOP_MARK = re.compile(r"\b(laptop|barrel[- ]?jack|19\s?v)\b", re.I)
_GPU_MARK = re.compile(r"\b(gpu|pcie|graphics card|12\s?v\s?pex)\b", re.I)
_RAIL_TOKEN = re.compile(r"\b([0-9]?[A-Z]{2,}[A-Z0-9_]*|[0-9]V[0-9]?[A-Z0-9_]*)\b")
_DIGIT = re.compile(r"\d")
_RAIL_KEYWORD = re.compile(r"V|VDD|RAIL|PEX|VBAT|VIN", re.I)


@dataclass(frozen=True)
class LintFinding:
    code: Literal["mixed_kind_rule", "phantom_rail", "unknown_kind_with_graph"]
    severity: Literal["warn", "reject"]
    detail: str


def lint_pack(
    *, registry: Registry, rules_text: str, graph_rails: set[str] | None,
) -> list[LintFinding]:
    findings: list[LintFinding] = []

    if _LAPTOP_MARK.search(rules_text) and _GPU_MARK.search(rules_text):
        findings.append(LintFinding(
            "mixed_kind_rule", "reject",
            "Rules mix laptop and GPU device markers in the same pack.",
        ))

    if graph_rails:
        cited = set(_RAIL_TOKEN.findall(rules_text))
        # Only treat tokens that *look* like rails (contain a digit or 'V') and
        # are absent from the graph as phantom — avoids flagging prose words.
        for tok in sorted(cited):
            if tok in graph_rails:
                continue
            if _DIGIT.search(tok) and _RAIL_KEYWORD.search(tok):
                findings.append(LintFinding(
                    "phantom_rail", "warn",
                    f"Rule cites rail {tok!r} absent from the schematic graph.",
                ))

    if graph_rails and registry.taxonomy.device_kind in (None, "unknown"):
        findings.append(LintFinding(
            "unknown_kind_with_graph", "warn",
            "device_kind unresolved although a schematic graph exists.",
        ))

    return findings
