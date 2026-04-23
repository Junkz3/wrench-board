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
    """Phase-by-phase behavioral simulator over an ElectricalGraph."""

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

    # ------------------------------------------------------------------
    # Phase source — prefer analyzer (phases + triggers carry `from_refdes`),
    # fall back to compiler (topological boot_sequence without triggers).
    # ------------------------------------------------------------------
    def _phases(self) -> list[tuple[int, str, list[str], list[str], list[tuple[str, str | None]]]]:
        """Return (index, name, rails_stable, components_entering, trigger_pairs)."""
        if self.analyzed_boot is not None and self.analyzed_boot.phases:
            out = []
            for p in self.analyzed_boot.phases:
                triggers = [(t.net_label, t.from_refdes) for t in p.triggers_next]
                out.append((p.index, p.name, list(p.rails_stable), list(p.components_entering), triggers))
            return out
        # Compiler fallback — triggers_next is list[str] of signal names, no driver.
        out = []
        for p in self.electrical.boot_sequence:
            triggers = [(net, None) for net in p.triggers_next]
            out.append((p.index, p.name, list(p.rails_stable), list(p.components_entering), triggers))
        return out

    def run(self) -> SimulationTimeline:
        rails: dict[str, RailState] = {label: "off" for label in self.electrical.power_rails}
        components: dict[str, ComponentState] = {}
        signals: dict[str, SignalState] = {}
        # Pre-seed every component as off; kills override immediately.
        for refdes in self.electrical.components:
            components[refdes] = "dead" if refdes in self.killed else "off"

        states: list[BoardState] = []
        phases = self._phases()
        blocked_at: int | None = None

        for (idx, name, rails_stable, comps_entering, triggers) in phases:
            self._stabilise_rails(rails, components, rails_stable, signals)
            self._activate_components(rails, components, comps_entering)
            self._assert_triggers(components, signals, triggers)
            blocked, reason = self._phase_blocked(rails_stable, rails, comps_entering, components)
            if blocked and blocked_at is None:
                blocked_at = idx
            states.append(BoardState(
                phase_index=idx,
                phase_name=name,
                rails=dict(rails),
                components=dict(components),
                signals=dict(signals),
                blocked=blocked,
                blocked_reason=reason,
            ))
            if blocked:
                break  # halt at first blockage — cascade below is computed post-loop

        cascade_components, cascade_rails = self._cascade(rails, components)
        verdict: FinalVerdict
        if blocked_at is not None:
            verdict = "blocked"
        elif cascade_components or cascade_rails:
            verdict = "cascade"
        else:
            verdict = "completed"

        return SimulationTimeline(
            device_slug=self.electrical.device_slug,
            killed_refdes=sorted(self.killed),
            states=states,
            final_verdict=verdict,
            blocked_at_phase=blocked_at,
            cascade_dead_components=cascade_components,
            cascade_dead_rails=cascade_rails,
        )

    # ------------------------------------------------------------------
    # Private transitions
    # ------------------------------------------------------------------
    def _stabilise_rails(
        self,
        rails: dict[str, RailState],
        components: dict[str, ComponentState],
        rails_stable: list[str],
        signals: dict[str, SignalState],
    ) -> None:
        # First pass — auto-assert every enable_net driven by the analyzer's
        # declared sequencer for rails entering this phase. The analyzer's
        # `triggers_next` often describes phase boundaries semantically
        # ("LPC wake") rather than listing every EN signal, so we fill the gap
        # by propagating sequencer state to all enable signals referenced here.
        sequencer = (
            self.analyzed_boot.sequencer_refdes if self.analyzed_boot else None
        )
        for label in rails_stable:
            rail = self.electrical.power_rails.get(label)
            if rail is None or not rail.enable_net:
                continue
            if sequencer is None:
                # No sequencer known → trust the analyzer: enable floats high.
                signals[rail.enable_net] = "high"
                continue
            seq_state = components.get(sequencer)
            if seq_state == "dead":
                signals[rail.enable_net] = "low"
            elif seq_state == "on":
                signals[rail.enable_net] = "high"
            # else: sequencer still "off" at this point — leave the signal
            # untouched so downstream logic can evaluate it as floating.

        # Second pass — actually decide rail state.
        for label in rails_stable:
            rail = self.electrical.power_rails.get(label)
            if rail is None:
                rails[label] = "stable"  # unknown rail — trust the phase
                continue
            # Dead source ⇒ rail stays off.
            if rail.source_refdes and components.get(rail.source_refdes) == "dead":
                rails[label] = "off"
                continue
            # Gated enable with a POSITIVE low ⇒ rail off. Floating / unknown
            # signals don't reject — the analyzer placed the rail in this
            # phase on purpose; only deny when we have evidence to the contrary.
            if rail.enable_net and signals.get(rail.enable_net) == "low":
                rails[label] = "off"
                continue
            rails[label] = "stable"

    def _activate_components(
        self,
        rails: dict[str, RailState],
        components: dict[str, ComponentState],
        comps_entering: list[str],
    ) -> None:
        for refdes in comps_entering:
            if refdes in self.killed:
                components[refdes] = "dead"
                continue
            comp = self.electrical.components.get(refdes)
            if comp is None:
                components[refdes] = "on"  # unknown — trust the phase
                continue
            ins = [
                pin.net_label for pin in comp.pins
                if pin.role == "power_in" and pin.net_label
            ]
            if ins and not all(rails.get(n) == "stable" for n in ins):
                components[refdes] = "off"
                continue
            components[refdes] = "on"

    def _assert_triggers(
        self,
        components: dict[str, ComponentState],
        signals: dict[str, SignalState],
        triggers: list[tuple[str, str | None]],
    ) -> None:
        for net_label, driver in triggers:
            if driver is None:
                signals[net_label] = "high"
                continue
            driver_state = components.get(driver)
            if driver_state == "on":
                signals[net_label] = "high"
            elif driver_state == "dead":
                signals[net_label] = "low"
            # else: driver is "off" (passive not in phases, or not yet entered)
            # — leave the signal absent/unchanged so the rail logic treats it
            # as floating rather than positively "low".

    def _phase_blocked(
        self,
        rails_stable: list[str],
        rails: dict[str, RailState],
        comps_entering: list[str],
        components: dict[str, ComponentState],
    ) -> tuple[bool, str | None]:
        # Only flag the phase as blocked when NOTHING advanced — all expected
        # rails stayed off AND no expected component came on. Partial progress
        # is not a blockage (a phase can have a dead consumer alongside live ones).
        if not rails_stable and not comps_entering:
            return False, None
        no_rails = all(rails.get(r) != "stable" for r in rails_stable) if rails_stable else True
        no_comps = all(components.get(c) != "on" for c in comps_entering) if comps_entering else True
        if rails_stable and no_rails and (not comps_entering or no_comps):
            missing = next((r for r in rails_stable if rails.get(r) != "stable"), rails_stable[0])
            rail = self.electrical.power_rails.get(missing)
            reason = f"Rail {missing} never stabilised"
            if rail and rail.source_refdes in self.killed:
                reason += f" — source {rail.source_refdes} is dead"
            return True, reason
        if not rails_stable and comps_entering and no_comps:
            return True, f"No component in {comps_entering} activated"
        return False, None

    def _cascade(
        self,
        rails: dict[str, RailState],
        components: dict[str, ComponentState],
    ) -> tuple[list[str], list[str]]:
        dead_rails = sorted(
            label for label, rail in self.electrical.power_rails.items()
            if rail.source_refdes in self.killed and rails.get(label) != "stable"
        )
        # Components that never turned on because they were waiting on a rail
        # whose source was killed, OR because the component itself was killed.
        dead_components: list[str] = []
        for refdes, comp in self.electrical.components.items():
            if refdes in self.killed:
                dead_components.append(refdes)
                continue
            if components.get(refdes) == "on":
                continue
            ins = [pin.net_label for pin in comp.pins if pin.role == "power_in" and pin.net_label]
            if not ins:
                continue
            if any(rails.get(n) != "stable" and
                   self.electrical.power_rails.get(n) is not None and
                   self.electrical.power_rails[n].source_refdes in self.killed
                   for n in ins):
                dead_components.append(refdes)
        return sorted(set(dead_components)), dead_rails
