# api/pipeline/schematic/simulator.py
# SPDX-License-Identifier: Apache-2.0
"""Behavioral event-driven simulator over the compiled ElectricalGraph.

Sync, pure, deterministic. Progresses a board state phase-by-phase using the
Opus-refined boot sequence when present (graph_analyzed phases with triggers
carrying `from_refdes`), else the compiler's topological boot_sequence.

No SPICE, no analog modelling — rail/component/signal states are closed
enums. The output is a list of discrete `BoardState` snapshots the UI can
scrub through and the agent can reason about ("kill U12 → blocked at Φ2").
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph

RailState = Literal["off", "rising", "stable"]
ComponentState = Literal["off", "on", "dead"]
SignalState = Literal["low", "high", "floating"]
FinalVerdict = Literal["completed", "blocked", "cascade"]


class BoardState(BaseModel):
    """Snapshot of the board at the end of one phase."""

    model_config = ConfigDict(extra="forbid")

    phase_index: int
    phase_name: str
    rails: dict[str, RailState] = Field(default_factory=dict)
    components: dict[str, ComponentState] = Field(default_factory=dict)
    signals: dict[str, SignalState] = Field(default_factory=dict)
    blocked: bool = False
    blocked_reason: str | None = None


class SimulationTimeline(BaseModel):
    """Full playback of one scenario."""

    model_config = ConfigDict(extra="forbid")

    device_slug: str
    killed_refdes: list[str] = Field(default_factory=list)
    states: list[BoardState] = Field(default_factory=list)
    final_verdict: FinalVerdict = "completed"
    blocked_at_phase: int | None = None
    cascade_dead_components: list[str] = Field(default_factory=list)
    cascade_dead_rails: list[str] = Field(default_factory=list)


class SimulationEngine:
    """Stub — run() will be implemented in the next tasks."""

    def __init__(
        self,
        electrical: ElectricalGraph,
        *,
        analyzed_boot: AnalyzedBootSequence | None = None,
        killed_refdes: list[str] | None = None,
    ) -> None:
        self.electrical = electrical
        self.analyzed_boot = analyzed_boot
        self.killed: frozenset[str] = frozenset(killed_refdes or ())

    def run(self) -> SimulationTimeline:
        raise NotImplementedError
