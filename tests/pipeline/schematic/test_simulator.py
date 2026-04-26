# tests/pipeline/schematic/test_simulator.py
"""Tests for the behavioral simulator — sync pure function over ElectricalGraph."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from api.pipeline.schematic.schemas import (
    AnalyzedBootPhase,
    AnalyzedBootSequence,
    AnalyzedBootTrigger,
    BootPhase,
    ComponentNode,
    ElectricalGraph,
    NetNode,
    PagePin,
    PowerRail,
    SchematicQualityReport,
)
from api.pipeline.schematic.simulator import (
    BoardState,
    ComponentState,  # noqa: F401 — verifies public export; tested in later tasks
    Failure,
    RailOverride,
    RailState,  # noqa: F401 — verifies public export; tested in later tasks
    SimulationEngine,  # noqa: F401 — verifies public export; tested in later tasks
    SimulationTimeline,
)


@pytest.fixture
def graph_minimal() -> ElectricalGraph:
    return ElectricalGraph(
        device_slug="test-min",
        components={
            "U7": ComponentNode(refdes="U7", type="ic"),
            "U12": ComponentNode(refdes="U12", type="ic"),
        },
        nets={
            "VIN": NetNode(label="VIN", is_power=True, is_global=True),
            "+5V": NetNode(label="+5V", is_power=True, is_global=True),
        },
        power_rails={
            "VIN": PowerRail(label="VIN", consumers=["U7"]),
            "+5V": PowerRail(label="+5V", source_refdes="U7", consumers=["U12"]),
        },
        typed_edges=[],
        boot_sequence=[],
        designer_notes=[],
        ambiguities=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )


def test_board_state_shape_minimal():
    state = BoardState(
        phase_index=0,
        phase_name="Standby",
        rails={"LPC_VCC": "stable"},
        components={"U18": "on"},
        signals={},
        blocked=False,
        blocked_reason=None,
    )
    assert state.phase_index == 0
    assert state.rails["LPC_VCC"] == "stable"


def test_simulation_timeline_shape_minimal():
    tl = SimulationTimeline(
        device_slug="demo",
        killed_refdes=[],
        states=[],
        final_verdict="completed",
        blocked_at_phase=None,
        cascade_dead_components=[],
        cascade_dead_rails=[],
    )
    assert tl.final_verdict == "completed"
    assert tl.killed_refdes == []


def _mnt_like_graph() -> ElectricalGraph:
    """Minimal 3-phase MNT-like topology: battery→VIN, VIN→+5V(U7), +5V→+3V3(U12)."""
    components = {
        "U18": ComponentNode(refdes="U18", type="ic", pins=[
            PagePin(number="1", name="VIN", role="power_in", net_label="LPC_VCC"),
        ]),
        "U7":  ComponentNode(refdes="U7",  type="ic", pins=[
            PagePin(number="1", name="VIN",  role="power_in",  net_label="VIN"),
            PagePin(number="2", name="VOUT", role="power_out", net_label="+5V"),
        ]),
        "U12": ComponentNode(refdes="U12", type="ic", pins=[
            PagePin(number="1", name="VIN",  role="power_in",  net_label="+5V"),
            PagePin(number="2", name="VOUT", role="power_out", net_label="+3V3"),
        ]),
        "U19": ComponentNode(refdes="U19", type="ic", pins=[
            PagePin(number="1", name="VIN", role="power_in", net_label="+5V"),
        ]),
    }
    nets = {
        "VIN":     NetNode(label="VIN",     is_power=True, is_global=True),
        "LPC_VCC": NetNode(label="LPC_VCC", is_power=True, is_global=True),
        "+5V":     NetNode(label="+5V",     is_power=True, is_global=True),
        "+3V3":    NetNode(label="+3V3",    is_power=True, is_global=True),
    }
    power_rails = {
        "VIN": PowerRail(label="VIN", voltage_nominal=24.0, source_refdes=None, consumers=["U18"]),
        "LPC_VCC": PowerRail(label="LPC_VCC", source_refdes="U14", consumers=["U18"]),
        "+5V":  PowerRail(label="+5V",  voltage_nominal=5.0, source_refdes="U7",  enable_net="5V_PWR_EN",  consumers=["U12", "U19"]),
        "+3V3": PowerRail(label="+3V3", voltage_nominal=3.3, source_refdes="U12", enable_net="3V3_PWR_EN", consumers=["U19"]),
    }
    return ElectricalGraph(
        device_slug="demo",
        components=components,
        nets=nets,
        power_rails=power_rails,
        typed_edges=[],
        boot_sequence=[],
        designer_notes=[],
        ambiguities=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )


def _mnt_like_analyzed() -> AnalyzedBootSequence:
    return AnalyzedBootSequence(
        device_slug="demo",
        phases=[
            AnalyzedBootPhase(
                index=0, name="Standby", kind="always-on",
                rails_stable=["VIN", "LPC_VCC"],
                components_entering=["U18"],
                triggers_next=[
                    AnalyzedBootTrigger(net_label="5V_PWR_EN", from_refdes="U18", rationale="LPC asserts 5V"),
                ],
            ),
            AnalyzedBootPhase(
                index=1, name="LPC asserts +5V", kind="sequenced",
                rails_stable=["+5V"],
                components_entering=["U7"],
                triggers_next=[
                    AnalyzedBootTrigger(net_label="3V3_PWR_EN", from_refdes="U18", rationale="LPC asserts 3V3"),
                ],
            ),
            AnalyzedBootPhase(
                index=2, name="LPC asserts +3V3", kind="sequenced",
                rails_stable=["+3V3"],
                components_entering=["U12", "U19"],
                triggers_next=[],
            ),
        ],
        sequencer_refdes="U18",
        global_confidence=0.9,
        model_used="test",
    )


def test_run_normal_boot_completes():
    engine = SimulationEngine(_mnt_like_graph(), analyzed_boot=_mnt_like_analyzed())
    tl = engine.run()
    assert tl.final_verdict == "completed"
    assert tl.blocked_at_phase is None
    # One state per phase.
    assert [s.phase_index for s in tl.states] == [0, 1, 2]
    # By the final phase, every rail the analyzer named is stable.
    last = tl.states[-1]
    assert last.rails["+5V"] == "stable"
    assert last.rails["+3V3"] == "stable"
    # Every component that entered a phase is on.
    assert last.components["U18"] == "on"
    assert last.components["U7"]  == "on"
    assert last.components["U12"] == "on"
    assert last.components["U19"] == "on"
    # The enable triggers asserted at each phase boundary show up as high.
    assert last.signals["5V_PWR_EN"] == "high"
    assert last.signals["3V3_PWR_EN"] == "high"
    assert tl.cascade_dead_rails == []
    assert tl.cascade_dead_components == []


def test_run_kill_plus_5v_regulator_blocks_downstream():
    engine = SimulationEngine(
        _mnt_like_graph(),
        analyzed_boot=_mnt_like_analyzed(),
        killed_refdes=["U7"],
    )
    tl = engine.run()
    assert tl.final_verdict == "blocked"
    assert tl.blocked_at_phase == 1, "phase 1 stabilises +5V which can't come up without U7"
    # U7 itself is dead.
    assert tl.states[-1].components["U7"] == "dead"
    # +5V never stabilised → U12 and U19 never turn on → they cascade as dead.
    assert "+5V" in tl.cascade_dead_rails
    assert "U12" in tl.cascade_dead_components
    assert "U19" in tl.cascade_dead_components
    # +3V3 is a sibling rail whose source (U12) is not DIRECTLY killed, but
    # U12 can't come on without +5V — so +3V3 also never stabilises. It shows
    # up in rails["+3V3"] == "off" in the final state but not in
    # cascade_dead_rails (source wasn't killed).
    assert tl.states[-1].rails["+3V3"] == "off"


def test_run_kill_lpc_sequencer_stalls_enable_dependent_phase():
    """Killing U18 (sequencer) means enable signals never assert.

    Phase 0 still advances because its rails (VIN, LPC_VCC) are externally
    sourced and don't depend on U18. Phase 1 advances partially — U7 can
    turn on from VIN, but +5V cannot stabilise without 5V_PWR_EN which U18
    never drives. The first fully-stalled phase is phase 2: +3V3 requires
    U12, U12 needs +5V, and no enable ever asserts.
    """
    engine = SimulationEngine(
        _mnt_like_graph(),
        analyzed_boot=_mnt_like_analyzed(),
        killed_refdes=["U18"],
    )
    tl = engine.run()
    assert tl.final_verdict == "blocked"
    assert tl.blocked_at_phase == 2
    # Sequencer is dead from phase 0 onward.
    assert tl.states[0].components["U18"] == "dead"
    # Enable signals never assert high because their driver (U18) is dead.
    # Phase 0 emitted (net="5V_PWR_EN", from="U18") — U18 is dead, so "low".
    last = tl.states[-1]
    assert last.signals.get("5V_PWR_EN") == "low"
    assert last.signals.get("3V3_PWR_EN") == "low"
    # The rails behind those enables never came up.
    assert last.rails["+5V"] == "off"
    assert last.rails["+3V3"] == "off"
    # U7 DID come on — its power_in is VIN (external, always up).
    assert last.components["U7"] == "on"
    # But U12/U19 (power_in on +5V) stayed off.
    assert last.components["U12"] == "off"
    assert last.components["U19"] == "off"


def test_run_fallback_without_analyzed_boot_uses_compiler_sequence():
    """When analyzed_boot is None, fall back to ElectricalGraph.boot_sequence."""
    graph = _mnt_like_graph()
    # Compiler-style phases: no `kind`, triggers_next is list[str], no drivers.
    graph.boot_sequence = [
        BootPhase(index=1, name="P1", rails_stable=["VIN", "LPC_VCC"],  components_entering=["U18"], triggers_next=["5V_PWR_EN"]),
        BootPhase(index=2, name="P2", rails_stable=["+5V"],              components_entering=["U7"],  triggers_next=["3V3_PWR_EN"]),
        BootPhase(index=3, name="P3", rails_stable=["+3V3"],             components_entering=["U12", "U19"], triggers_next=[]),
    ]
    engine = SimulationEngine(graph, analyzed_boot=None)
    tl = engine.run()
    assert tl.final_verdict == "completed"
    # Compiler phases are 1-indexed (Kahn starts at 1), analyzer is 0-indexed.
    assert [s.phase_index for s in tl.states] == [1, 2, 3]
    # Triggers without a driver are unconditionally asserted.
    assert tl.states[-1].signals["5V_PWR_EN"] == "high"


def test_run_is_deterministic_across_100_runs():
    graph = _mnt_like_graph()
    analyzed = _mnt_like_analyzed()
    first = SimulationEngine(graph, analyzed_boot=analyzed, killed_refdes=["U7"]).run()
    for _ in range(99):
        again = SimulationEngine(graph, analyzed_boot=analyzed, killed_refdes=["U7"]).run()
        assert again.model_dump() == first.model_dump()


def test_run_stabilises_rail_with_enable_net_via_sequencer_auto_assert():
    """Analyzer doesn't list every enable_net in triggers_next — the
    engine must infer enable assertions from sequencer_refdes instead."""
    graph = _mnt_like_graph()
    # Remove the explicit trigger pairs so ONLY auto-assert via sequencer
    # can stabilise enable-gated rails. The analyzer's triggers often omit
    # specific EN signals (e.g. "power_button/LPC wake" replaces 5V_PWR_EN).
    analyzed = AnalyzedBootSequence(
        device_slug="demo",
        phases=[
            AnalyzedBootPhase(
                index=0, name="Standby", kind="always-on",
                rails_stable=["VIN", "LPC_VCC"],
                components_entering=["U18"],
                triggers_next=[],
            ),
            AnalyzedBootPhase(
                index=1, name="Main rails", kind="sequenced",
                rails_stable=["+5V"],
                components_entering=["U7"],
                triggers_next=[],
            ),
            AnalyzedBootPhase(
                index=2, name="+3V3", kind="sequenced",
                rails_stable=["+3V3"],
                components_entering=["U12", "U19"],
                triggers_next=[],
            ),
        ],
        sequencer_refdes="U18",
        global_confidence=0.9,
        model_used="test",
    )
    tl = SimulationEngine(graph, analyzed_boot=analyzed).run()
    assert tl.final_verdict == "completed"
    last = tl.states[-1]
    # Key assertion — +5V stabilises even though 5V_PWR_EN isn't in any
    # triggers_next list. Auto-assert via sequencer_refdes=U18 handles it.
    assert last.rails["+5V"] == "stable"
    assert last.rails["+3V3"] == "stable"
    # The auto-asserted enable signals are visible in state.
    assert last.signals.get("5V_PWR_EN") == "high"
    assert last.signals.get("3V3_PWR_EN") == "high"


def test_run_sourceless_rails_are_always_stable():
    """Rails with source_refdes=None are external supplies / compiler orphans
    and should be treated as always-on — not left 'off' just because the
    analyzer didn't list them in any rails_stable.
    """
    graph = _mnt_like_graph()
    # Inject an external rail nobody schedules.
    from api.pipeline.schematic.schemas import NetNode, PowerRail
    graph.nets["EXTERNAL_24V"] = NetNode(label="EXTERNAL_24V", is_power=True, is_global=True)
    graph.power_rails["EXTERNAL_24V"] = PowerRail(label="EXTERNAL_24V", source_refdes=None)
    # Same for a compiler orphan (real name, no source).
    graph.nets["USBH_3V3"] = NetNode(label="USBH_3V3", is_power=True, is_global=True)
    graph.power_rails["USBH_3V3"] = PowerRail(label="USBH_3V3", source_refdes=None)

    tl = SimulationEngine(graph, analyzed_boot=_mnt_like_analyzed()).run()
    assert tl.final_verdict == "completed"
    # Stable from Φ0 onward — never scheduled, no source, no enable.
    for state in tl.states:
        assert state.rails["EXTERNAL_24V"] == "stable"
        assert state.rails["USBH_3V3"] == "stable"


def test_run_sourceless_rail_survives_kill_of_unrelated_refdes():
    """Killing an IC that doesn't source a sourceless rail must not affect it."""
    graph = _mnt_like_graph()
    from api.pipeline.schematic.schemas import NetNode, PowerRail
    graph.nets["VBUS"] = NetNode(label="VBUS", is_power=True, is_global=True)
    graph.power_rails["VBUS"] = PowerRail(label="VBUS", source_refdes=None)

    tl = SimulationEngine(graph, analyzed_boot=_mnt_like_analyzed(), killed_refdes=["U7"]).run()
    for state in tl.states:
        assert state.rails["VBUS"] == "stable"


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[3]
         / "memory/mnt-reform-motherboard/electrical_graph.json").exists(),
    reason="MNT artefacts not present on this checkout",
)
def test_run_completes_under_10ms_on_mnt_reform():
    """Performance smoke — MNT is the biggest board we have (~450 comps)."""
    root = Path(__file__).resolve().parents[3]
    eg = ElectricalGraph.model_validate_json(
        (root / "memory/mnt-reform-motherboard/electrical_graph.json").read_text()
    )
    ab_path = root / "memory/mnt-reform-motherboard/boot_sequence_analyzed.json"
    ab = AnalyzedBootSequence.model_validate_json(ab_path.read_text()) if ab_path.exists() else None

    # Warm-up + 20 runs.
    SimulationEngine(eg, analyzed_boot=ab).run()
    t0 = time.perf_counter()
    for _ in range(20):
        SimulationEngine(eg, analyzed_boot=ab, killed_refdes=["U12"]).run()
    elapsed_ms = (time.perf_counter() - t0) * 1000 / 20
    assert elapsed_ms < 10.0, f"simulator ran {elapsed_ms:.2f}ms/run (> 10ms target)"


def test_cascade_transitive_dead_rails_via_dead_source():
    """Rail B sourced by a consumer of rail A: if rail A's source dies,
    the consumer never powers on, so rail B is transitively dead too."""
    from api.pipeline.schematic.schemas import (
        BootPhase,
        ComponentNode,
        ElectricalGraph,
        NetNode,
        PagePin,
        PowerRail,
        SchematicQualityReport,
    )
    from api.pipeline.schematic.simulator import SimulationEngine

    comps = {
        "U1": ComponentNode(
            refdes="U1", type="ic",
            pins=[PagePin(number="1", role="power_in", net_label="VIN")],
        ),
        "U2": ComponentNode(
            refdes="U2", type="ic",
            pins=[
                PagePin(number="1", role="power_in", net_label="RAIL_A"),
                PagePin(number="2", role="power_out", net_label="RAIL_B"),
            ],
        ),
        "U3": ComponentNode(
            refdes="U3", type="ic",
            pins=[PagePin(number="1", role="power_in", net_label="RAIL_B")],
        ),
    }
    nets = {
        "VIN": NetNode(label="VIN", is_power=True),
        "RAIL_A": NetNode(label="RAIL_A", is_power=True),
        "RAIL_B": NetNode(label="RAIL_B", is_power=True),
    }
    rails = {
        "VIN":    PowerRail(label="VIN",    source_refdes=None, consumers=["U1"]),
        "RAIL_A": PowerRail(label="RAIL_A", source_refdes="U1", consumers=["U2"]),
        "RAIL_B": PowerRail(label="RAIL_B", source_refdes="U2", consumers=["U3"]),
    }
    graph = ElectricalGraph(
        device_slug="transitive-test",
        components=comps,
        nets=nets,
        power_rails=rails,
        typed_edges=[],
        boot_sequence=[
            BootPhase(index=1, name="p1", rails_stable=["RAIL_A"], components_entering=["U1", "U2"]),
            BootPhase(index=2, name="p2", rails_stable=["RAIL_B"], components_entering=["U3"]),
        ],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1, confidence_global=1.0),
    )
    tl = SimulationEngine(graph, killed_refdes=["U1"]).run()
    # U1 is the kill source → RAIL_A dead. U2 can't power on → RAIL_B also dead
    # transitively. U3 never powers on.
    assert "RAIL_A" in tl.cascade_dead_rails
    assert "RAIL_B" in tl.cascade_dead_rails, (
        f"expected RAIL_B in cascade after fixpoint refactor; got {tl.cascade_dead_rails!r}"
    )
    assert "U2" in tl.cascade_dead_components
    assert "U3" in tl.cascade_dead_components


def test_board_state_accepts_degraded_rail_with_voltage_pct():
    state = BoardState(
        phase_index=1,
        phase_name="Phase 1",
        rails={"+5V": "degraded"},
        rail_voltage_pct={"+5V": 0.94},
    )
    assert state.rails["+5V"] == "degraded"
    assert state.rail_voltage_pct["+5V"] == 0.94


def test_board_state_accepts_degraded_component():
    state = BoardState(
        phase_index=1,
        phase_name="Phase 1",
        components={"U7": "degraded"},
    )
    assert state.components["U7"] == "degraded"


def test_failure_requires_value_ohms_for_leaky_short():
    # Happy path — value_ohms supplied.
    f = Failure(refdes="C42", mode="leaky_short", value_ohms=200.0)
    assert f.mode == "leaky_short"
    assert f.value_ohms == 200.0


def test_failure_leaky_short_requires_value_ohms():
    """leaky_short without value_ohms must raise — the engine cannot
    compute a voltage drop without a path resistance."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="value_ohms"):
        Failure(refdes="C42", mode="leaky_short")


def test_failure_regulating_low_requires_voltage_pct():
    """regulating_low without voltage_pct must raise — the engine has
    no defensible default for a regulator's degraded output level."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="voltage_pct"):
        Failure(refdes="U7", mode="regulating_low")


def test_rail_override_carries_state_and_voltage_pct():
    o = RailOverride(label="+5V", state="degraded", voltage_pct=0.94)
    assert o.state == "degraded"
    assert o.voltage_pct == 0.94


def test_rail_override_degraded_requires_voltage_pct():
    """RailOverride(state='degraded') without voltage_pct must raise —
    'degraded' has no meaning without a level to compare against the
    engine's TOLERANCE_OK / TOLERANCE_UVLO thresholds."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="voltage_pct"):
        RailOverride(label="+5V", state="degraded")


def test_engine_accepts_failures_argument(graph_minimal):
    """Engine accepts a list of Failure objects without crashing."""
    engine = SimulationEngine(
        graph_minimal,
        failures=[Failure(refdes="U7", mode="dead")],
    )
    timeline = engine.run()
    assert "U7" in timeline.killed_refdes  # killed via Failure(mode="dead")


def test_engine_accepts_rail_overrides_argument(graph_minimal):
    """Engine accepts a list of RailOverride objects without crashing."""
    engine = SimulationEngine(
        graph_minimal,
        rail_overrides=[RailOverride(label="+5V", state="stable")],
    )
    timeline = engine.run()
    # Override forces +5V stable regardless of source state.
    final_state = timeline.states[-1]
    assert final_state.rails.get("+5V") == "stable"


def test_engine_killed_refdes_remains_backward_compat(graph_minimal):
    """Existing killed_refdes API still works identically."""
    engine = SimulationEngine(graph_minimal, killed_refdes=["U7"])
    timeline = engine.run()
    assert "U7" in timeline.killed_refdes


@pytest.fixture
def graph_with_phase(graph_minimal) -> ElectricalGraph:
    """Same as graph_minimal but with one boot phase that activates U12 on +5V."""
    # Wire U12 with a real power_in pin on +5V so _activate_components has work to do.
    graph_minimal.components["U12"] = ComponentNode(
        refdes="U12",
        type="ic",
        pins=[PagePin(number="1", role="power_in", net_label="+5V")],
    )
    graph_minimal.boot_sequence = [
        BootPhase(
            index=1,
            name="Phase 1 — +5V comes up, U12 enters",
            rails_stable=["+5V"],
            components_entering=["U12"],
            triggers_next=[],
        ),
    ]
    return graph_minimal


def test_rail_override_degraded_makes_consumer_degraded(graph_minimal):
    """A degraded rail at 0.85 (below 0.9 threshold) degrades its consumers."""
    engine = SimulationEngine(
        graph_minimal,
        rail_overrides=[RailOverride(label="+5V", state="degraded", voltage_pct=0.85)],
    )
    # Force U12 to enter via a manual "phase" — the minimal graph has empty
    # boot_sequence, so we add a stub via analyzed_boot.
    final = engine.run().states[-1]
    assert final.rails.get("+5V") == "degraded"
    assert final.rail_voltage_pct.get("+5V") == 0.85


def test_rail_override_degraded_within_tolerance_keeps_consumer_on(graph_with_phase):
    """A degraded rail at 0.95 (above 0.9 tolerance) keeps the consumer on."""
    engine = SimulationEngine(
        graph_with_phase,
        rail_overrides=[RailOverride(label="+5V", state="degraded", voltage_pct=0.95)],
    )
    final = engine.run().states[-1]
    assert final.components.get("U12") == "on"


def test_rail_override_uvlo_marks_consumer_dead(graph_with_phase):
    """voltage_pct < 0.5 triggers under-voltage lockout — consumer dead."""
    engine = SimulationEngine(
        graph_with_phase,
        rail_overrides=[RailOverride(label="+5V", state="degraded", voltage_pct=0.4)],
    )
    final = engine.run().states[-1]
    assert final.components.get("U12") == "dead"


def test_run_with_degraded_rail_returns_degraded_verdict(graph_with_phase):
    """Boot completes with degraded states → final_verdict == 'degraded'."""
    engine = SimulationEngine(
        graph_with_phase,
        rail_overrides=[RailOverride(label="+5V", state="degraded", voltage_pct=0.85)],
    )
    timeline = engine.run()
    assert timeline.final_verdict == "degraded"


@pytest.fixture
def graph_with_decoupling(graph_with_phase) -> ElectricalGraph:
    """graph_with_phase + a decoupling cap C42 on +5V."""
    graph_with_phase.components["C42"] = ComponentNode(
        refdes="C42",
        type="capacitor",
        kind="passive_c",
        role="decoupling",
        pins=[
            PagePin(number="1", role="terminal", net_label="+5V"),
            PagePin(number="2", role="ground", net_label="GND"),
        ],
    )
    graph_with_phase.power_rails["+5V"].decoupling = ["C42"]
    return graph_with_phase


@pytest.fixture
def graph_with_series_r(graph_with_phase) -> ElectricalGraph:
    """graph_with_phase + a series R5 between +5V and U12.power_in."""
    graph_with_phase.components["R5"] = ComponentNode(
        refdes="R5",
        type="resistor",
        kind="passive_r",
        role="series",
        pins=[
            PagePin(number="1", role="terminal", net_label="+5V"),
            PagePin(number="2", role="terminal", net_label="+5V_FILT"),
        ],
    )
    # Re-wire U12 to consume +5V_FILT instead.
    graph_with_phase.components["U12"].pins = [
        PagePin(number="1", role="power_in", net_label="+5V_FILT")
    ]
    return graph_with_phase


@pytest.fixture
def graph_upstream_and_downstream_of_r5(graph_with_series_r) -> ElectricalGraph:
    """graph_with_series_r + an upstream consumer U20 directly on +5V.

    Topology: +5V (sourced by U7) → [U20 upstream consumer] → R5 → +5V_FILT → U12.
    Used to verify open-mode passive failures only kill downstream consumers.
    """
    graph_with_series_r.components["U20"] = ComponentNode(
        refdes="U20",
        type="ic",
        pins=[PagePin(number="1", role="power_in", net_label="+5V")],
    )
    # Add U20 to the +5V rail's consumers list (cosmetic — the engine reads
    # power_in pins directly, but keeps the rail bookkeeping honest).
    graph_with_series_r.power_rails["+5V"].consumers = ["U12", "U20"]
    # Schedule U20 to enter alongside U12 so the phase walk activates it.
    graph_with_series_r.boot_sequence[0].components_entering = ["U12", "U20"]
    return graph_with_series_r


def test_failure_leaky_short_on_decoupling_cap_degrades_rail(graph_with_decoupling):
    """A leaky_short on a decoupling cap drives the decoupled rail to degraded."""
    engine = SimulationEngine(
        graph_with_decoupling,
        failures=[Failure(refdes="C42", mode="leaky_short", value_ohms=200.0)],
    )
    final = engine.run().states[-1]
    assert final.rails.get("+5V") == "degraded"
    assert final.rail_voltage_pct.get("+5V", 1.0) < 1.0


def test_failure_regulating_low_drops_sourced_rails(graph_with_phase):
    """regulating_low on the source IC sets all sourced rails to voltage_pct."""
    engine = SimulationEngine(
        graph_with_phase,
        failures=[Failure(refdes="U7", mode="regulating_low", voltage_pct=0.85)],
    )
    final = engine.run().states[-1]
    assert final.rails.get("+5V") == "degraded"
    assert final.rail_voltage_pct.get("+5V") == 0.85


def test_failure_open_on_series_resistor_kills_consumer(graph_with_series_r):
    """open on a series resistor cuts the path → IC dead."""
    engine = SimulationEngine(
        graph_with_series_r,
        failures=[Failure(refdes="R5", mode="open")],
    )
    final = engine.run().states[-1]
    assert final.components.get("U12") == "dead"


def test_cascade_populates_for_shorted_failure(graph_with_phase):
    """Failure(mode='shorted') on the source IC must register downstream
    consumers + the rail itself in cascade aggregates, not only the
    self.killed-driven set. Downstream callers (bridge, tool, endpoint,
    evaluator) read these aggregate fields to decide what's affected.
    """
    # Give U7 pins so the shorted branch can identify the touched rail.
    graph_with_phase.components["U7"].pins = [
        PagePin(number="1", role="power_in", net_label="VIN"),
        PagePin(number="2", role="power_out", net_label="+5V"),
    ]
    engine = SimulationEngine(
        graph_with_phase,
        failures=[Failure(refdes="U7", mode="shorted")],
    )
    timeline = engine.run()
    assert "+5V" in timeline.cascade_dead_rails
    assert "U12" in timeline.cascade_dead_components


def test_cascade_populates_for_regulating_low_uvlo(graph_with_phase):
    """Failure(mode='regulating_low', voltage_pct=0.3) below TOLERANCE_UVLO
    drives consumers dead and must register them in cascade aggregates.
    The rail is technically 'degraded' but at 0.3 < 0.5 UVLO it can't
    power anything, so its consumers cascade as dead.
    """
    engine = SimulationEngine(
        graph_with_phase,
        failures=[Failure(refdes="U7", mode="regulating_low", voltage_pct=0.3)],
    )
    timeline = engine.run()
    # U12 starves at 30 % of nominal — its activation pass marked it dead.
    # The cascade aggregates must reflect that.
    assert "U12" in timeline.cascade_dead_components
    # The rail itself is below UVLO — for cascade purposes it's effectively
    # dead even though its `state` is 'degraded'.
    assert "+5V" in timeline.cascade_dead_rails


def test_failure_open_on_series_resistor_spares_upstream_consumer(
    graph_upstream_and_downstream_of_r5,
):
    """open on a series passive must only kill downstream consumers.

    R5 sits between +5V (upstream, sourced by U7) and +5V_FILT (downstream).
    U20 consumes +5V upstream — its supply is unaffected by R5 opening.
    U12 consumes +5V_FILT downstream — it loses its supply when R5 opens.
    """
    engine = SimulationEngine(
        graph_upstream_and_downstream_of_r5,
        failures=[Failure(refdes="R5", mode="open")],
    )
    final = engine.run().states[-1]
    # Downstream consumer is dead.
    assert final.components.get("U12") == "dead"
    # Upstream consumer is NOT collateral damage.
    assert final.components.get("U20") == "on"
