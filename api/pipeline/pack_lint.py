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
# A bare voltage value (3V, 0V, 12V, 2V7) cited as an expected MEASUREMENT is a
# reading, not a rail label — the rail itself is named in the step's action.
_BARE_VOLTAGE = re.compile(r"\d+V\d*\Z")


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
        # Underscore-insensitive view of the graph rails: a writer that drops the
        # underscore (PP3V3G3H) still names the real PP3V3_G3H rail.
        rails_squashed = {r.replace("_", "") for r in graph_rails}
        # Only treat tokens that *look* like rails (contain a digit or 'V') and
        # are absent from the graph as phantom — avoids flagging prose words.
        for tok in sorted(cited):
            if tok in graph_rails:
                continue
            if not (_DIGIT.search(tok) and _RAIL_KEYWORD.search(tok)):
                continue
            # A bare voltage reading (3V, 0V) is a measurement, not a rail.
            if _BARE_VOLTAGE.match(tok):
                continue
            # Family shorthand (PPBUS → PPBUS_G3H) names a real rail family.
            if any(r.startswith(tok + "_") for r in graph_rails):
                continue
            # Underscore-dropped spelling of a real rail (PP3V3G3H → PP3V3_G3H).
            if tok.replace("_", "") in rails_squashed:
                continue
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
