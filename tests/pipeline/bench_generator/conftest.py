# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from api.pipeline.bench_generator.schemas import (
    Cause,
    EvidenceSpan,
    ProposedScenarioDraft,
)
from api.pipeline.schematic.schemas import (
    ComponentNode,
    ElectricalGraph,
    PowerRail,
    SchematicQualityReport,
)


@pytest.fixture
def toy_graph() -> ElectricalGraph:
    """6 components + 2 rails + one decoupling relationship."""
    components = {
        "U7": ComponentNode(refdes="U7", type="ic", kind="ic", role="buck_regulator"),
        "U13": ComponentNode(refdes="U13", type="ic", kind="ic", role="buck_regulator"),
        "U1": ComponentNode(refdes="U1", type="ic", kind="ic", role="cpu"),
        "C19": ComponentNode(refdes="C19", type="capacitor", kind="passive_c", role="decoupling"),
        "R100": ComponentNode(refdes="R100", type="resistor", kind="passive_r", role="pullup"),
        "R200": ComponentNode(refdes="R200", type="resistor", kind="passive_r", role="series"),
    }
    rails = {
        "+5V": PowerRail(label="+5V", voltage_nominal=5.0, source_refdes="U7"),
        "+3V3": PowerRail(
            label="+3V3",
            voltage_nominal=3.3,
            source_refdes="U13",
            decoupling=["C19"],
        ),
    }
    return ElectricalGraph(
        device_slug="toy-board",
        components=components,
        power_rails=rails,
        typed_edges=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )


@pytest.fixture
def sample_draft() -> ProposedScenarioDraft:
    """A clean draft that passes V1-V5 against toy_graph. Tests derive
    invalid variants from this by mutating one field."""
    return ProposedScenarioDraft(
        local_id="c19-short",
        cause=Cause(refdes="C19", mode="shorted"),
        expected_dead_rails=["+3V3"],
        expected_dead_components=[],
        source_url="https://example.com/forum/c19",
        source_quote=(
            "C19 is a decoupling capacitor on the +3V3 rail. A hard short "
            "collapses the rail to ground and prevents the regulator from "
            "maintaining regulation."
        ),
        confidence=0.85,
        evidence=[
            EvidenceSpan(
                field="cause.refdes",
                source_quote_substring="C19",
                reasoning="explicitly named",
            ),
            EvidenceSpan(
                field="cause.mode",
                source_quote_substring="hard short",
                reasoning="shorted mode",
            ),
            EvidenceSpan(
                field="expected_dead_rails",
                source_quote_substring="collapses the rail to ground",
                reasoning="rail death asserted verbatim",
            ),
        ],
        reasoning_summary="C19 short on +3V3 collapses the rail.",
    )


@pytest.fixture
def now_iso() -> str:
    # Deterministic enough for tests that assert format, not value.
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@pytest.fixture
def pack_dir(tmp_path: Path) -> Path:
    """A minimal on-disk pack. Tests that need specific content write it
    themselves on top of the baseline here."""
    d = tmp_path / "memory" / "toy-board"
    d.mkdir(parents=True)
    (d / "raw_research_dump.md").write_text(
        "# Research Dump\n\n"
        "Symptom: rail +3V3 collapses when C19 shorts.\n"
        "Source: https://example.com/forum/c19\n"
        "...\n" * 20,  # ≥ 500 chars
        encoding="utf-8",
    )
    (d / "rules.json").write_text(
        '{"schema_version": "1.0", "rules": []}',
        encoding="utf-8",
    )
    (d / "registry.json").write_text(
        '{"schema_version": "1.0", "components": [], "signals": []}',
        encoding="utf-8",
    )
    return d
