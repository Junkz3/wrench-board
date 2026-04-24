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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph

RailState = Literal["off", "rising", "stable", "degraded", "shorted"]
ComponentState = Literal["off", "on", "degraded", "dead"]
SignalState = Literal["low", "high", "floating"]
FinalVerdict = Literal["completed", "blocked", "cascade", "degraded"]

# Voltage tolerance thresholds, fraction of nominal.
# Above 0.9 → consumer treated as fully on.
# Between 0.5 and 0.9 → consumer enters degraded state.
# Below 0.5 → under-voltage lockout, consumer marked dead.
TOLERANCE_OK = 0.9
TOLERANCE_UVLO = 0.5

# Estimated nominal current draw per consumer when computing leaky_short
# voltage drop. Chosen for order-of-magnitude correctness — tests pin
# behaviour, not the exact curve. Override per-rail later if needed.
LEAKY_SHORT_PER_CONSUMER_MA = 50.0


class BoardState(BaseModel):
    """Snapshot of the board at the end of one phase."""

    model_config = ConfigDict(extra="forbid")

    phase_index: int
    phase_name: str
    rails: dict[str, RailState] = Field(default_factory=dict)
    rail_voltage_pct: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Optional per-rail voltage as a fraction of nominal. Present "
            "only when the rail is `degraded`/`shorted` (with finite R) "
            "or was explicitly observed via rail_overrides."
        ),
    )
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


class Failure(BaseModel):
    """A cause prescribed by the caller — the simulator computes the
    consequences (which rails sag, which components degrade)."""

    model_config = ConfigDict(extra="forbid")

    refdes: str
    mode: Literal[
        "dead",
        "shorted",
        "leaky_short",
        "regulating_low",
        "open",
    ]
    value_ohms: float | None = Field(
        default=None,
        description="Required for `leaky_short`. Path resistance to GND (Ω).",
    )
    voltage_pct: float | None = Field(
        default=None,
        description="Required for `regulating_low`. Output as fraction of nominal.",
    )

    @model_validator(mode="after")
    def _check_mode_specific_required(self) -> "Failure":
        if self.mode == "leaky_short" and self.value_ohms is None:
            raise ValueError(
                "Failure(mode='leaky_short') requires value_ohms — "
                "the engine cannot compute a voltage drop without a "
                "path resistance."
            )
        if self.mode == "regulating_low" and self.voltage_pct is None:
            raise ValueError(
                "Failure(mode='regulating_low') requires voltage_pct — "
                "no defensible default exists for a regulator's "
                "degraded output level."
            )
        return self


class RailOverride(BaseModel):
    """An observation supplied by the caller — forces a rail to a state."""

    model_config = ConfigDict(extra="forbid")

    label: str
    state: RailState
    voltage_pct: float | None = Field(
        default=None,
        description="Required when state is `degraded`.",
    )

    @model_validator(mode="after")
    def _check_state_specific_required(self) -> "RailOverride":
        if self.state == "degraded" and self.voltage_pct is None:
            raise ValueError(
                "RailOverride(state='degraded') requires voltage_pct — "
                "'degraded' is meaningless without a level to compare "
                "against TOLERANCE_OK / TOLERANCE_UVLO."
            )
        return self


class SimulationEngine:
    """Phase-by-phase behavioral simulator over an ElectricalGraph."""

    def __init__(
        self,
        electrical: ElectricalGraph,
        *,
        analyzed_boot: AnalyzedBootSequence | None = None,
        killed_refdes: list[str] | None = None,
        failures: list[Failure] | None = None,
        rail_overrides: list[RailOverride] | None = None,
    ) -> None:
        self.electrical = electrical
        self.analyzed_boot = analyzed_boot
        # killed_refdes is sugar for Failure(mode="dead").
        synth_failures = [Failure(refdes=r, mode="dead") for r in (killed_refdes or [])]
        self.failures: list[Failure] = list(failures or []) + synth_failures
        self.rail_overrides: list[RailOverride] = list(rail_overrides or [])
        # Derived view used by the existing cascade pass.
        self.killed: frozenset[str] = frozenset(
            f.refdes for f in self.failures if f.mode == "dead"
        )
        # Rails locked by an explicit observation — _stabilise_rails leaves
        # these alone so the override holds across phase iteration.
        self._overridden_rails: frozenset[str] = frozenset(
            o.label for o in self.rail_overrides
        )

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
        # External supplies / compiler-orphaned rails (no source IC) are presumed
        # always-on. They're either physical inputs (VIN connector, battery, USB
        # VBUS) or vision-missed sources the analyzer never scheduled. Marking
        # them stable from Φ0 matches technician intuition — a killed IC can't
        # turn off a rail it doesn't drive.
        rails: dict[str, RailState] = {
            label: ("stable" if rail.source_refdes is None else "off")
            for label, rail in self.electrical.power_rails.items()
        }
        components: dict[str, ComponentState] = {}
        signals: dict[str, SignalState] = {}
        # Pre-seed every component as off; kills override immediately.
        for refdes in self.electrical.components:
            components[refdes] = "dead" if refdes in self.killed else "off"

        rail_voltage: dict[str, float] = {}
        # Apply causes first; then observations override anything.
        failure_locked = self._apply_failures_at_init(rails, rail_voltage, components)
        for ovr in self.rail_overrides:
            rails[ovr.label] = ovr.state
            if ovr.voltage_pct is not None:
                rail_voltage[ovr.label] = ovr.voltage_pct
        # Lock rails touched by failures so the phase walk doesn't overwrite
        # them. Combined with the override-locked set built in __init__.
        self._locked_rails: frozenset[str] = self._overridden_rails | failure_locked

        states: list[BoardState] = []
        phases = self._phases()
        blocked_at: int | None = None

        for (idx, name, rails_stable, comps_entering, triggers) in phases:
            self._stabilise_rails(rails, components, rails_stable, signals)
            self._activate_components(rails, rail_voltage, components, comps_entering)
            self._assert_triggers(components, signals, triggers)
            blocked, reason = self._phase_blocked(rails_stable, rails, comps_entering, components)
            if blocked and blocked_at is None:
                blocked_at = idx
            states.append(BoardState(
                phase_index=idx,
                phase_name=name,
                rails=dict(rails),
                rail_voltage_pct=dict(rail_voltage),
                components=dict(components),
                signals=dict(signals),
                blocked=blocked,
                blocked_reason=reason,
            ))
            if blocked:
                break  # halt at first blockage — cascade below is computed post-loop

        # Emit a Φ0 baseline snapshot when no phases ran — keeps `states[-1]`
        # meaningful for callers driving the engine purely via overrides on a
        # graph without a compiled boot_sequence.
        if not states:
            states.append(BoardState(
                phase_index=0,
                phase_name="Φ0 — initial state",
                rails=dict(rails),
                rail_voltage_pct=dict(rail_voltage),
                components=dict(components),
                signals=dict(signals),
                blocked=False,
                blocked_reason=None,
            ))

        cascade_components, cascade_rails = self._cascade(rails, components, rail_voltage)
        verdict: FinalVerdict
        if blocked_at is not None:
            verdict = "blocked"
        elif cascade_components or cascade_rails:
            verdict = "cascade"
        elif any(s == "degraded" for s in rails.values()) or any(
            s == "degraded" for s in components.values()
        ):
            verdict = "degraded"
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
    def _apply_failures_at_init(
        self,
        rails: dict[str, RailState],
        rail_voltage: dict[str, float],
        components: dict[str, ComponentState],
    ) -> frozenset[str]:
        """Mutate initial state from each Failure. Order:
        dead/open/regulating_low/shorted/leaky_short — last writer wins
        on the same rail, but failures rarely overlap in practice.

        Returns the set of rails this pass touched, so the caller can lock
        them against the phase walk's `_stabilise_rails` rewrites.
        """
        touched_rails: set[str] = set()
        for f in self.failures:
            if f.mode == "dead":
                components[f.refdes] = "dead"
                continue

            if f.mode == "regulating_low":
                pct = f.voltage_pct if f.voltage_pct is not None else 0.85
                for label, rail in self.electrical.power_rails.items():
                    if rail.source_refdes == f.refdes:
                        rails[label] = "degraded"
                        rail_voltage[label] = pct
                        touched_rails.add(label)
                continue

            if f.mode == "shorted":
                comp = self.electrical.components.get(f.refdes)
                if comp is None:
                    continue
                # Find the rail this component touches (through any pin).
                touched = {
                    pin.net_label for pin in comp.pins if pin.net_label
                    and pin.net_label in self.electrical.power_rails
                    and pin.net_label.upper() not in {"GND", "VSS", "0V"}
                }
                for label in touched:
                    rails[label] = "shorted"
                    rail_voltage[label] = 0.0
                    touched_rails.add(label)
                continue

            if f.mode == "leaky_short":
                comp = self.electrical.components.get(f.refdes)
                if comp is None or f.value_ohms is None:
                    continue
                # The cap decouples a rail — find which.
                target_rail: str | None = None
                for label, rail in self.electrical.power_rails.items():
                    if f.refdes in rail.decoupling:
                        target_rail = label
                        break
                if target_rail is None:
                    continue
                # Voltage divider model: leak draws extra I = V_nom / R_leak;
                # consumers also draw I_nom_total = N × per-consumer estimate.
                # Without a source resistance we approximate the resulting
                # voltage as V_nom × (R_leak / (R_leak + R_eff_consumers)),
                # where R_eff_consumers ≈ V_nom / I_nom_total.
                rail = self.electrical.power_rails[target_rail]
                v_nom = rail.voltage_nominal or 5.0
                n_consumers = max(1, len(rail.consumers))
                i_nom_a = (LEAKY_SHORT_PER_CONSUMER_MA * n_consumers) / 1000.0
                r_eff = v_nom / i_nom_a
                v_drop_pct = f.value_ohms / (f.value_ohms + r_eff)
                rails[target_rail] = "degraded"
                rail_voltage[target_rail] = max(0.0, min(1.0, v_drop_pct))
                touched_rails.add(target_rail)
                continue

            if f.mode == "open":
                comp = self.electrical.components.get(f.refdes)
                if comp is None:
                    continue
                # An open passive in series cuts power to consumers on the
                # DOWNSTREAM side only — upstream consumers still see the
                # supply rail. Identify which of the two touched nets is
                # upstream by looking for a registered `power_rail` whose
                # source is either an IC (`source_refdes` set) or an
                # external supply (`source_refdes is None` — the always-on
                # baseline applied in `run()`). The OTHER net's consumers
                # are the downstream casualties.
                touched_nets = {pin.net_label for pin in comp.pins if pin.net_label}
                upstream_candidates = {
                    n for n in touched_nets
                    if n in self.electrical.power_rails
                }
                if len(upstream_candidates) == 1:
                    upstream = next(iter(upstream_candidates))
                    downstream_nets = touched_nets - {upstream}
                    for refdes, c in self.electrical.components.items():
                        ins = {p.net_label for p in c.pins if p.role == "power_in" and p.net_label}
                        if ins & downstream_nets:
                            components[refdes] = "dead"
                # Ambiguous topology (both touched nets are registered
                # power rails, or neither is) — under-kill rather than
                # over-kill. A foundation simulator should never fabricate
                # a dead set; downstream agents/operators can refine via
                # explicit `Failure(mode="dead")` on the affected IC.
                continue

        return frozenset(touched_rails)

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
            # Honour caller-supplied observations and failure-driven states —
            # locked rails are not rewritten by the phase walk.
            if label in self._locked_rails:
                continue
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
        rail_voltage: dict[str, float],
        components: dict[str, ComponentState],
        comps_entering: list[str],
    ) -> None:
        for refdes in comps_entering:
            if refdes in self.killed:
                components[refdes] = "dead"
                continue
            # A failure already marked this component dead (e.g. an `open`
            # on a series passive cut its supply path). Keep it dead — the
            # phase walk doesn't resurrect it.
            if components.get(refdes) == "dead":
                continue
            comp = self.electrical.components.get(refdes)
            if comp is None:
                components[refdes] = "on"  # unknown — trust the phase
                continue
            ins = [
                pin.net_label for pin in comp.pins
                if pin.role == "power_in" and pin.net_label
            ]
            if not ins:
                components[refdes] = "on"
                continue
            # Compute the worst-case state across all power_in rails.
            worst: ComponentState = "on"
            for net in ins:
                state = rails.get(net)
                if state == "stable":
                    continue
                if state == "degraded":
                    pct = rail_voltage.get(net, 1.0)
                    if pct < TOLERANCE_UVLO:
                        worst = "dead"
                        break
                    if pct < TOLERANCE_OK and worst != "dead":
                        worst = "degraded"
                    continue
                # off, rising, shorted → component cannot turn on.
                worst = "off"
                break
            components[refdes] = worst

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
        # A degraded rail / degraded component counts as progress: the boot kept
        # moving, just out of spec.
        if not rails_stable and not comps_entering:
            return False, None
        live_rail_states = {"stable", "degraded"}
        live_comp_states = {"on", "degraded"}
        no_rails = (
            all(rails.get(r) not in live_rail_states for r in rails_stable)
            if rails_stable else True
        )
        no_comps = (
            all(components.get(c) not in live_comp_states for c in comps_entering)
            if comps_entering else True
        )
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
        rail_voltage: dict[str, float],
    ) -> tuple[list[str], list[str]]:
        """Compute dead components + dead rails with one transitive rail pass.

        Semantics:
          1. `effective_dead` = `self.killed` ∪ every component whose
             post-failure-application state is already 'dead' (shorted ICs
             marking their own rails, open-mode passive kills, regulating_low
             UVLO consequences propagated by `_activate_components`). This
             unions the by-construction kills with the cause-driven kills so
             cascade aggregates reflect the full failure surface.
          2. `dead_components` = `effective_dead` plus every component whose
             `power_in` pin sits on a rail whose original source is in
             `effective_dead`.
          3. `dead_rails` = every rail whose source is dead after step 2,
             plus every final-state rail that is `shorted` or that is
             `degraded` with a voltage below TOLERANCE_UVLO (its consumers
             cannot power on, so for cascade purposes the rail itself is dead).
          4. `dead_components` is then extended to include consumers of the
             dead rails — closes the "if a rail is dead its consumers are too"
             invariant in a single linear pass.

        Downstream callers (bridge, tool, endpoint, evaluator) read
        `cascade_dead_*` to decide what's affected; relying only on
        `self.killed` previously hid every cause-driven failure.
        """
        # Step 1 — fold post-failure dead state into the kill set.
        effective_dead: set[str] = set(self.killed) | {
            refdes for refdes, state in components.items() if state == "dead"
        }

        # Step 2 — propagate to consumers of rails sourced by an effective-dead IC.
        dead_components: set[str] = set(effective_dead)
        for refdes, comp in self.electrical.components.items():
            if refdes in dead_components:
                continue
            if components.get(refdes) == "on":
                continue
            ins = [p.net_label for p in comp.pins if p.role == "power_in" and p.net_label]
            if not ins:
                continue
            if any(
                rails.get(n) != "stable"
                and self.electrical.power_rails.get(n) is not None
                and self.electrical.power_rails[n].source_refdes in effective_dead
                for n in ins
            ):
                dead_components.add(refdes)

        # Step 3 — dead rails: source dead, OR rail itself is shorted, OR
        # degraded with explicit voltage under the UVLO threshold (consumers
        # can't run on it). A degraded rail without a voltage entry is
        # treated as "near nominal" and not UVLO-cascaded.
        dead_rails: set[str] = set()
        for label, rail in self.electrical.power_rails.items():
            final_state = rails.get(label)
            if final_state == "stable":
                continue
            if rail.source_refdes and rail.source_refdes in dead_components:
                dead_rails.add(label)
                continue
            if final_state == "shorted":
                dead_rails.add(label)
                continue
            if final_state == "degraded":
                voltage = rail_voltage.get(label)
                if voltage is not None and voltage < TOLERANCE_UVLO:
                    dead_rails.add(label)

        # Step 4 — extend dead_components to consumers of any dead rail.
        for refdes, comp in self.electrical.components.items():
            if refdes in dead_components:
                continue
            if components.get(refdes) == "on":
                continue
            ins = [p.net_label for p in comp.pins if p.role == "power_in" and p.net_label]
            if ins and any(n in dead_rails for n in ins):
                dead_components.add(refdes)

        return sorted(dead_components), sorted(dead_rails)
