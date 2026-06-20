"""Phase 4 convergence policy — early-stop on regression, best-of snapshot,
acceptance floor (Tasks 9 + 10).

These tests drive `generate_knowledge_pack` with every network phase mocked
(same harness as test_orchestrator_lint_wiring.py / test_orchestrator_events.py)
to exercise ONLY the Phase 4 loop-control restructure and the graph wiring.

The policy under test (why it exists):
  * EARLY-STOP on score regression — on the real macbook-air-m1 build, a
    regressing trajectory (consistency 0.78 → 0.42) never recovered, and each
    extra revise round costs real $ (Opus writers + Opus auditor). So when the
    score drops below the previous round we stop revising immediately.
  * BEST-OF snapshot — we never ship an artefact worse than one we already had.
    The loop tracks the highest-scoring round's (kg, rules, dictionary, verdict)
    and, when it stops without an APPROVED, falls back to that best snapshot
    instead of the last (possibly-regressed) rewrite.
  * ACCEPTANCE FLOOR — a 0.78 pack whose DETERMINISTIC drift is empty beats a
    hard ~$100 build failure. When the best snapshot clears
    `pipeline_accept_score` AND has no deterministic drift, the pack is accepted
    WITH WARNINGS (the residual brief persisted to pack_quality for audit)
    rather than rejected. floor=0 restores the legacy hard-fail.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from api.config import Settings
from api.pipeline import orchestrator
from api.pipeline.schemas import (
    AuditVerdict,
    Cause,
    Dictionary,
    KnowledgeEdge,
    KnowledgeGraph,
    KnowledgeNode,
    Registry,
    RegistryComponent,
    Rule,
    RulesSet,
)
from api.pipeline.schematic.schemas import (
    ComponentNode,
    ElectricalGraph,
    NetNode,
    PowerRail,
    SchematicQualityReport,
    TypedEdge,
)

# ----------------------------------------------------------------------
# Fixtures / helpers
# ----------------------------------------------------------------------


def _verdict(status: str, score: float, *, brief: str = "", drift=None) -> AuditVerdict:
    return AuditVerdict(
        overall_status=status,
        consistency_score=score,
        files_to_rewrite=["rules"] if status == "NEEDS_REVISION" else [],
        drift_report=drift or [],
        revision_brief=brief,
    )


def _mini_graph() -> ElectricalGraph:
    """Tiny graph mirroring tests/pipeline/test_graph_truth.py — U8100 sources
    PP1V2_S2, with C0042 on the rail. Enough to (a) make GraphTruth non-None and
    (b) let Phase 2.6 enrich the registry from a description that cites PP1V2_S2."""
    return ElectricalGraph(
        device_slug="mini",
        components={
            "U8100": ComponentNode(refdes="U8100", type="ic", kind="ic", role="pmic", pages=[3]),
            "SWV011": ComponentNode(refdes="SWV011", type="switch", role="load_switch", pages=[5]),
            "C0042": ComponentNode(refdes="C0042", type="capacitor", kind="passive_c", pages=[3]),
        },
        nets={
            "PP1V2_S2": NetNode(label="PP1V2_S2", is_power=True),
            "SIG_EN": NetNode(label="SIG_EN"),
        },
        power_rails={
            "PP1V2_S2": PowerRail(
                label="PP1V2_S2",
                voltage_nominal=1.2,
                source_refdes="U8100",
                consumers=["C0042"],
            ),
        },
        typed_edges=[
            TypedEdge(src="U8100", dst="PP1V2_S2", kind="powers"),
            TypedEdge(src="PP1V2_S2", dst="C0042", kind="powers"),
        ],
        quality=SchematicQualityReport(total_pages=5, pages_parsed=5),
    )


def _patch_settings(monkeypatch, **overrides):
    """Patch orchestrator.get_settings to return a Settings with the given
    overrides. The orchestrator reads `settings = get_settings()` once at the
    top of the function, then reads the policy knobs off it, so patching the
    factory is the clean seam (no global cache mutation that leaks across tests)."""
    base = Settings()
    patched = base.model_copy(update=overrides)
    monkeypatch.setattr(orchestrator, "get_settings", lambda: patched)
    return patched


async def _drive(
    tmp_path,
    *,
    registry,
    verdicts,
    revisions=None,
    on_event=None,
    writers_outputs=None,
    write_graph=False,
    graph=None,
    captured_auditor=None,
):
    """Run the pipeline with all phases mocked. `verdicts` is the scripted list
    of auditor verdicts (one per round); `revisions` is the side_effect list for
    run_single_writer_revision (the per-file reviser). `writers_outputs` lets a
    test supply the round-0 (kg, rules, dictionary) so it can assert the best
    snapshot is the round-0 artefacts."""
    if writers_outputs is None:
        kg = KnowledgeGraph(nodes=[], edges=[])
        dictionary = Dictionary(entries=[])
        rules = RulesSet(rules=[])
        writers_outputs = (kg, rules, dictionary)

    if write_graph:
        pack = tmp_path / "demo"
        pack.mkdir(parents=True, exist_ok=True)
        (pack / "electrical_graph.json").write_text(
            (graph or _mini_graph()).model_dump_json(), encoding="utf-8"
        )

    auditor_mock = AsyncMock(side_effect=list(verdicts))
    revision_mock = AsyncMock(side_effect=list(revisions or []))

    if captured_auditor is not None:
        async def _capture(**kwargs):
            captured_auditor.append(kwargs)
            return verdicts[len(captured_auditor) - 1]
        auditor_mock = AsyncMock(side_effect=_capture)

    with (
        patch("api.pipeline.orchestrator.run_scout", new=AsyncMock(return_value="# dump")),
        patch(
            "api.pipeline.orchestrator.run_registry_builder",
            new=AsyncMock(return_value=registry),
        ),
        patch(
            "api.pipeline.orchestrator.run_mapper",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "api.pipeline.orchestrator.run_writers_parallel",
            new=AsyncMock(return_value=writers_outputs),
        ),
        patch("api.pipeline.orchestrator.run_auditor", new=auditor_mock),
        patch(
            "api.pipeline.orchestrator.run_single_writer_revision",
            new=revision_mock,
        ),
    ):
        result = await orchestrator.generate_knowledge_pack(
            "Demo",
            client=object(),
            memory_root=tmp_path,
            on_event=on_event,
        )
    return result, auditor_mock


# ----------------------------------------------------------------------
# 1. early-stop on regression (floor disabled)
# ----------------------------------------------------------------------


async def test_early_stop_on_regression(tmp_path, monkeypatch):
    """0.78 → 0.42 (both NEEDS_REVISION), floor disabled → exactly 2 auditor
    calls (no 3rd round), build fails. The regression itself terminates the
    loop — we don't burn the remaining rounds on a trajectory that's getting
    worse."""
    _patch_settings(monkeypatch, pipeline_accept_score=0.0, pipeline_max_revise_rounds=3)
    registry = Registry(device_label="Demo", components=[], signals=[])
    verdicts = [
        _verdict("NEEDS_REVISION", 0.78, brief="drift A"),
        _verdict("NEEDS_REVISION", 0.42, brief="drift B"),
    ]
    revisions = [RulesSet(rules=[])]  # one revise round happens (after round 0)

    with pytest.raises(RuntimeError):
        await _drive(tmp_path, registry=registry, verdicts=verdicts, revisions=revisions)


async def test_early_stop_only_two_auditor_calls(tmp_path, monkeypatch):
    """Same regression scenario — assert run_auditor was awaited exactly twice
    (round 0 + round 1), proving the regression short-circuited round 2/3."""
    _patch_settings(monkeypatch, pipeline_accept_score=0.0, pipeline_max_revise_rounds=3)
    registry = Registry(device_label="Demo", components=[], signals=[])
    verdicts = [
        _verdict("NEEDS_REVISION", 0.78),
        _verdict("NEEDS_REVISION", 0.42),
    ]
    revisions = [RulesSet(rules=[])]

    # captured_auditor survit au raise (la destructuration du retour, elle, ne se
    # produit jamais quand _drive lève) — c'est LE compteur qui prouve l'early-stop.
    calls: list[dict] = []
    with pytest.raises(RuntimeError):
        await _drive(
            tmp_path,
            registry=registry,
            verdicts=verdicts,
            revisions=revisions,
            captured_auditor=calls,
        )
    assert len(calls) == 2  # round 0 + round 1, pas de 3e tour sur une trajectoire qui régresse


# ----------------------------------------------------------------------
# 2. best-of snapshot accepted with warnings
# ----------------------------------------------------------------------


async def test_best_snapshot_accepted_with_warnings(tmp_path, monkeypatch):
    """0.78 → 0.42, floor 0.70, deterministic drift empty → pipeline COMPLETES
    with the ROUND-0 artefacts (the best snapshot), pack_quality carries the
    0.78 verdict's brief under audit_warnings, the audit phase_finished event
    is APPROVED_WITH_WARNINGS, and build_state is complete."""
    _patch_settings(monkeypatch, pipeline_accept_score=0.70, pipeline_max_revise_rounds=3)
    registry = Registry(device_label="Demo", components=[], signals=[])

    # Round-0 artefacts: a kg with a single, distinctive node so we can prove the
    # accepted-on-disk kg is the round-0 one, not the round-1 rewrite.
    round0_kg = KnowledgeGraph(nodes=[], edges=[])
    round0_rules = RulesSet(rules=[])
    round0_dict = Dictionary(entries=[])

    # The round-1 reviser returns a DIFFERENT rules object (an extra rule) so a
    # naive "ship the last rewrite" would leave it on disk — the test asserts it
    # does NOT.
    revised_rules = RulesSet(
        rules=[
            Rule(
                id="R-REVISED-001",
                symptoms=["a regression-introduced symptom"],
                likely_causes=[Cause(refdes="U1", probability=0.5, mechanism="x")],
            )
        ]
    )

    verdicts = [
        _verdict("NEEDS_REVISION", 0.78, brief="best brief — U99 residual"),
        _verdict("NEEDS_REVISION", 0.42, brief="worse brief"),
    ]

    events = []

    async def collect(ev):
        events.append(ev)

    result, _ = await _drive(
        tmp_path,
        registry=registry,
        verdicts=verdicts,
        revisions=[revised_rules],
        writers_outputs=(round0_kg, round0_rules, round0_dict),
        on_event=collect,
    )

    # Pipeline completed (no raise).
    assert result.verdict.consistency_score == 0.78

    pack = tmp_path / "demo"
    # The best snapshot (round-0 rules, empty) is what's on disk — NOT the
    # revised rules with R-REVISED-001.
    on_disk = json.loads((pack / "rules.json").read_text(encoding="utf-8"))
    assert on_disk["rules"] == []

    # pack_quality carries the residual brief from the 0.78 verdict.
    pq = json.loads((pack / "pack_quality.json").read_text(encoding="utf-8"))
    assert "audit_warnings" in pq
    assert pq["audit_warnings"]["consistency_score"] == 0.78
    assert pq["audit_warnings"]["revision_brief"] == "best brief — U99 residual"

    # The audit phase_finished event is APPROVED_WITH_WARNINGS.
    audit_finished = [
        e for e in events
        if e["type"] == "phase_finished" and e.get("phase") == "audit"
    ]
    assert len(audit_finished) == 1
    assert audit_finished[0]["status"] == "APPROVED_WITH_WARNINGS"

    # Le verdict PERSISTÉ porte le statut décidé, pas l'avis du round.
    av = json.loads((pack / "audit_verdict.json").read_text(encoding="utf-8"))
    assert av["overall_status"] == "APPROVED_WITH_WARNINGS"
    assert av["revision_brief"] == "best brief — U99 residual"

    # … et le pipeline_finished FINAL aussi : un abonné qui lit `status` ne doit
    # jamais voir NEEDS_REVISION sur un build accepté et complet.
    pipeline_finished = [e for e in events if e["type"] == "pipeline_finished"]
    assert len(pipeline_finished) == 1
    assert pipeline_finished[0]["status"] == "APPROVED_WITH_WARNINGS"

    # build_state complete.
    from api.pipeline import build_state
    assert build_state.read_build_state(pack)["status"] == "complete"


# ----------------------------------------------------------------------
# 3. below floor still fails
# ----------------------------------------------------------------------


async def test_below_floor_still_fails(tmp_path, monkeypatch):
    """0.50 → 0.42, floor 0.70 → the best (0.50) is below the floor, so the
    legacy hard-fail still fires: RuntimeError, build_state failed,
    pipeline_failed emitted."""
    _patch_settings(monkeypatch, pipeline_accept_score=0.70, pipeline_max_revise_rounds=3)
    registry = Registry(device_label="Demo", components=[], signals=[])
    verdicts = [
        _verdict("NEEDS_REVISION", 0.50),
        _verdict("NEEDS_REVISION", 0.42),
    ]
    events = []

    async def collect(ev):
        events.append(ev)

    with pytest.raises(RuntimeError):
        await _drive(
            tmp_path,
            registry=registry,
            verdicts=verdicts,
            revisions=[RulesSet(rules=[])],
            on_event=collect,
        )

    failures = [e for e in events if e["type"] == "pipeline_failed"]
    assert len(failures) == 1

    from api.pipeline import build_state
    assert build_state.read_build_state(tmp_path / "demo")["status"] == "failed"


# ----------------------------------------------------------------------
# 4. best snapshot with non-empty deterministic drift is not accepted
# ----------------------------------------------------------------------


async def test_best_with_drift_not_accepted(tmp_path, monkeypatch):
    """Best snapshot scores 0.80 (above floor) but its DETERMINISTIC drift is
    non-empty (a rules Cause.refdes the registry doesn't back) → not acceptable;
    the build fails despite the high score. A floor pass is necessary but NOT
    sufficient — empty deterministic drift is the hard gate."""
    _patch_settings(monkeypatch, pipeline_accept_score=0.70, pipeline_max_revise_rounds=3)
    # Registry knows U1 but NOT UNKNOWN1.
    registry = Registry(
        device_label="Demo",
        components=[RegistryComponent(canonical_name="U1", description="known")],
        signals=[],
    )
    # Round-0 rules reference UNKNOWN1 → compute_drift will flag it.
    drifting_rules = RulesSet(
        rules=[
            Rule(
                id="R-DRIFT-001",
                symptoms=["x"],
                likely_causes=[Cause(refdes="UNKNOWN1", probability=0.5, mechanism="y")],
            )
        ]
    )
    verdicts = [
        _verdict("NEEDS_REVISION", 0.80, brief="high but drifting"),
        _verdict("NEEDS_REVISION", 0.42),
    ]

    with pytest.raises(RuntimeError):
        await _drive(
            tmp_path,
            registry=registry,
            verdicts=verdicts,
            revisions=[drifting_rules],  # revision keeps the drift
            writers_outputs=(
                KnowledgeGraph(nodes=[], edges=[]),
                drifting_rules,
                Dictionary(entries=[]),
            ),
        )


# ----------------------------------------------------------------------
# 5. accept_score == 0 disables the floor (legacy hard-fail)
# ----------------------------------------------------------------------


async def test_accept_score_zero_disables(tmp_path, monkeypatch):
    """floor=0, a single high-score NEEDS_REVISION (0.95) with rounds exhausted
    → still fails. Disabling the floor restores the legacy behaviour: a
    NEEDS_REVISION that never clears is fatal regardless of how good the score
    looks."""
    _patch_settings(monkeypatch, pipeline_accept_score=0.0, pipeline_max_revise_rounds=0)
    registry = Registry(device_label="Demo", components=[], signals=[])
    verdicts = [_verdict("NEEDS_REVISION", 0.95)]

    with pytest.raises(RuntimeError):
        await _drive(tmp_path, registry=registry, verdicts=verdicts)


# ----------------------------------------------------------------------
# 6. explicit REJECTED at round 0 is terminal — no acceptance path
# ----------------------------------------------------------------------


async def test_explicit_rejected_still_terminal(tmp_path, monkeypatch):
    """A REJECTED verdict at round 0 fails immediately — the acceptance floor
    never applies (REJECTED is the auditor's hard veto, not a near-miss)."""
    _patch_settings(monkeypatch, pipeline_accept_score=0.70, pipeline_max_revise_rounds=3)
    registry = Registry(device_label="Demo", components=[], signals=[])
    verdicts = [_verdict("REJECTED", 0.99, brief="hopeless")]

    with pytest.raises(RuntimeError):
        await _drive(tmp_path, registry=registry, verdicts=verdicts)


# ----------------------------------------------------------------------
# 7. graph wiring — Task 9
# ----------------------------------------------------------------------


async def test_graph_wired_into_auditor_and_registry_enriched(tmp_path, monkeypatch):
    """With a graph on disk: (a) run_auditor receives a non-None graph_truth +
    a ground-truth report naming a mentioned identifier, and (b) registry.json
    on disk gains the description-cited rail (Phase 2.6 enrichment)."""
    _patch_settings(monkeypatch, pipeline_accept_score=0.70)
    # A component whose description cites PP1V2_S2 (real in the mini-graph) but
    # which is NOT in registry.signals → Phase 2.6 must add it.
    registry = Registry(
        device_label="Demo",
        components=[
            RegistryComponent(
                canonical_name="U8100",
                description="PMIC that generates PP1V2_S2 for the SoC.",
            )
        ],
        signals=[],
    )
    # Writers mention U8100 so the ground-truth report has something to report.
    rules = RulesSet(
        rules=[
            Rule(
                id="R-GT-001",
                symptoms=["no PP1V2_S2 from U8100"],
                likely_causes=[Cause(refdes="U8100", probability=0.5, mechanism="dead pmic")],
            )
        ]
    )
    captured = []
    verdicts = [_verdict("APPROVED", 1.0)]

    await _drive(
        tmp_path,
        registry=registry,
        verdicts=verdicts,
        write_graph=True,
        writers_outputs=(KnowledgeGraph(nodes=[], edges=[]), rules, Dictionary(entries=[])),
        captured_auditor=captured,
    )

    # (a) auditor got a non-None graph_truth + a report naming a mention.
    assert len(captured) == 1
    assert captured[0]["graph_truth"] is not None
    report = captured[0]["ground_truth_report"]
    assert report is not None
    assert "U8100" in report

    # (b) registry.json gained PP1V2_S2 as a signal (Phase 2.6 enrichment).
    pack = tmp_path / "demo"
    reg_on_disk = json.loads((pack / "registry.json").read_text(encoding="utf-8"))
    signal_names = {s["canonical_name"] for s in reg_on_disk["signals"]}
    assert "PP1V2_S2" in signal_names


async def test_no_graph_means_no_graph_truth(tmp_path, monkeypatch):
    """Without a graph: run_auditor receives graph_truth=None + ground_truth
    report=None, and the registry is NOT enriched (no signals added)."""
    _patch_settings(monkeypatch, pipeline_accept_score=0.70)
    registry = Registry(
        device_label="Demo",
        components=[
            RegistryComponent(
                canonical_name="U8100",
                description="PMIC that generates PP1V2_S2 for the SoC.",
            )
        ],
        signals=[],
    )
    captured = []
    verdicts = [_verdict("APPROVED", 1.0)]

    await _drive(
        tmp_path,
        registry=registry,
        verdicts=verdicts,
        write_graph=False,
        captured_auditor=captured,
    )

    assert captured[0]["graph_truth"] is None
    assert captured[0]["ground_truth_report"] is None

    pack = tmp_path / "demo"
    reg_on_disk = json.loads((pack / "registry.json").read_text(encoding="utf-8"))
    assert reg_on_disk["signals"] == []


# ----------------------------------------------------------------------
# 4. edge-contradiction backstop (graph-contradicted power edges)
# ----------------------------------------------------------------------


def _two_ic_graph() -> ElectricalGraph:
    """Rail PP1V8_X is sourced by the dedicated regulator U200; U100 is a real IC
    that does NOT produce it — the over-attribution shape (a kg `U100 powers
    PP1V8_X` edge is graph-contradicted)."""
    return ElectricalGraph(
        device_slug="demo",
        components={
            "U100": ComponentNode(refdes="U100", type="ic", kind="ic", pages=[1]),
            "U200": ComponentNode(refdes="U200", type="ic", kind="ic", pages=[2]),
        },
        nets={"PP1V8_X": NetNode(label="PP1V8_X", is_power=True)},
        power_rails={
            "PP1V8_X": PowerRail(label="PP1V8_X", voltage_nominal=1.8, source_refdes="U200"),
        },
        typed_edges=[TypedEdge(src="U200", dst="PP1V8_X", kind="powers")],
        quality=SchematicQualityReport(total_pages=2, pages_parsed=2),
    )


def _contradicted_kg() -> KnowledgeGraph:
    return KnowledgeGraph(
        nodes=[
            KnowledgeNode(id="N-U100", kind="component", label="pmic"),
            KnowledgeNode(id="N-NET_PP1V8_X", kind="net", label="1.8V"),
        ],
        edges=[KnowledgeEdge(source_id="N-U100", target_id="N-NET_PP1V8_X", relation="powers")],
    )


def _no_contra_edge(pack_dir) -> bool:
    kg = json.loads((pack_dir / "knowledge_graph.json").read_text())
    return not any(
        e["source_id"] == "N-U100" and e["target_id"] == "N-NET_PP1V8_X"
        for e in kg["edges"]
    )


async def test_backstop_prunes_contradicted_edge_on_floor_path(tmp_path, monkeypatch):
    """A graph-contradicted edge survives the LLM revise-loop. Without the
    backstop it reads as residual drift at the acceptance gate and sinks an
    otherwise-shippable 0.78 pack to REJECTED. WITH it, the edge is pruned before
    the gate → the pack ships APPROVED_WITH_WARNINGS and the on-disk kg is clean."""
    _patch_settings(monkeypatch, pipeline_accept_score=0.70, pipeline_max_revise_rounds=1)
    registry = Registry(device_label="Demo", components=[], signals=[])
    kg = _contradicted_kg()
    verdicts = [
        _verdict("NEEDS_REVISION", 0.78, brief="fix edges"),
        _verdict("NEEDS_REVISION", 0.78, brief="fix edges"),
    ]
    revisions = [RulesSet(rules=[])]  # reviser touches rules; the bad kg edge persists
    await _drive(
        tmp_path,
        registry=registry,
        verdicts=verdicts,
        revisions=revisions,
        writers_outputs=(kg, RulesSet(rules=[]), Dictionary(entries=[])),
        write_graph=True,
        graph=_two_ic_graph(),
    )
    av = json.loads((tmp_path / "demo" / "audit_verdict.json").read_text())
    assert av["overall_status"] == "APPROVED_WITH_WARNINGS"
    assert _no_contra_edge(tmp_path / "demo")


async def test_backstop_prunes_contradicted_edge_on_approved_path(tmp_path, monkeypatch):
    """Even if the auditor returns APPROVED despite the contradiction, the
    post-loop backstop guarantees the shipped kg carries no graph-contradicted
    edge."""
    _patch_settings(monkeypatch, pipeline_accept_score=0.70, pipeline_max_revise_rounds=2)
    registry = Registry(device_label="Demo", components=[], signals=[])
    kg = _contradicted_kg()
    verdicts = [_verdict("APPROVED", 0.95)]
    await _drive(
        tmp_path,
        registry=registry,
        verdicts=verdicts,
        writers_outputs=(kg, RulesSet(rules=[]), Dictionary(entries=[])),
        write_graph=True,
        graph=_two_ic_graph(),
    )
    av = json.loads((tmp_path / "demo" / "audit_verdict.json").read_text())
    assert av["overall_status"] == "APPROVED"
    assert _no_contra_edge(tmp_path / "demo")
