# tests/pipeline/schematic/test_simulator_spof_correlation.py
"""Cross-check critical_path SPOF ranking with simulator blockage depth.

The `mb_schematic_graph(query="critical_path")` tool ranks nodes by raw
cascade size in the power DAG. The simulator is the richer oracle — it
also respects enable gating and phase ordering. We don't require exact
ranking equality (the two metrics differ by design) but we DO require
that every top-5 compiler-side SPOF, when killed in the simulator,
produces a non-empty cascade.

Gated on MNT artefacts; skipped otherwise so CI stays hermetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph
from api.pipeline.schematic.simulator import SimulationEngine
from api.tools.schematic import mb_schematic_graph

MEMORY_ROOT = Path(__file__).resolve().parents[3] / "memory"


@pytest.mark.skipif(
    not (MEMORY_ROOT / "mnt-reform-motherboard/electrical_graph.json").exists(),
    reason="MNT artefacts not present",
)
def test_top_5_spof_refdes_produce_simulator_cascade():
    slug = "mnt-reform-motherboard"
    eg = ElectricalGraph.model_validate_json(
        (MEMORY_ROOT / slug / "electrical_graph.json").read_text()
    )
    ab_path = MEMORY_ROOT / slug / "boot_sequence_analyzed.json"
    ab = AnalyzedBootSequence.model_validate_json(ab_path.read_text()) if ab_path.exists() else None

    cp = mb_schematic_graph(
        device_slug=slug, memory_root=MEMORY_ROOT, query="critical_path"
    )
    assert cp["found"] is True

    component_spofs = [s for s in cp["top_spofs"] if s["kind"] == "component"][:5]
    assert len(component_spofs) >= 3, "expected at least 3 component SPOFs"

    for s in component_spofs:
        refdes = s["label"]
        tl = SimulationEngine(eg, analyzed_boot=ab, killed_refdes=[refdes]).run()
        # Killing a real SPOF must produce measurable cascade — either a
        # blocked phase OR a non-empty cascade_dead_components set.
        nonzero = (
            tl.final_verdict in ("blocked", "cascade")
            or tl.cascade_dead_components
            or tl.cascade_dead_rails
        )
        assert nonzero, f"killing top-SPOF {refdes} produced no simulator cascade"
