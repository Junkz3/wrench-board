"""Build-state marker (`_build_state.json`) — the pack's truthfulness contract.

A FAILED pipeline used to leave a PARTIAL pack behind (registry/rules written
before the failing phase) that `_pack_is_complete` mistook for a real pack →
phantom rule coverage → a retry never rebuilt. The marker records the build
outcome so an unfinished pack can never masquerade as complete, while packs
built before the marker existed (no file) stay complete — full self-host
back-compat.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from api.pipeline import build_state
from api.pipeline.routes.packs import _pack_is_complete

# ---------------------------------------------------------------------------
# build_state helper unit tests
# ---------------------------------------------------------------------------


def test_read_build_state_absent_returns_none(tmp_path):
    assert build_state.read_build_state(tmp_path) is None


def test_read_build_state_corrupted_returns_none(tmp_path):
    (tmp_path / build_state.BUILD_STATE_FILE).write_text("{not json", encoding="utf-8")
    assert build_state.read_build_state(tmp_path) is None


def test_mark_building_then_read(tmp_path):
    build_state.mark_building(tmp_path)
    state = build_state.read_build_state(tmp_path)
    assert state["status"] == "building"
    assert state["started_at"]  # ISO timestamp present


def test_mark_complete_overwrites_building(tmp_path):
    build_state.mark_building(tmp_path)
    build_state.mark_complete(tmp_path)
    state = build_state.read_build_state(tmp_path)
    assert state["status"] == "complete"
    assert state["finished_at"]


def test_mark_failed_records_stage_and_error(tmp_path):
    build_state.mark_building(tmp_path)
    build_state.mark_failed(tmp_path, stage="audit", error="boom")
    state = build_state.read_build_state(tmp_path)
    assert state["status"] == "failed"
    assert state["stage"] == "audit"
    assert state["error"] == "boom"


def test_mark_paused_distinct_from_failed(tmp_path):
    build_state.mark_building(tmp_path)
    build_state.mark_paused(tmp_path, reason="needs_kind_confirmation")
    state = build_state.read_build_state(tmp_path)
    assert state["status"] == "paused"
    assert state["reason"] == "needs_kind_confirmation"


def test_finalize_failed_if_building_transitions_building(tmp_path):
    """The orchestrator's finally-hook: any exit that left the marker on
    'building' (exception, cancellation, hard crash recovery) becomes 'failed'."""
    build_state.mark_building(tmp_path)
    build_state.finalize_failed_if_building(tmp_path, error="cancelled")
    assert build_state.read_build_state(tmp_path)["status"] == "failed"


def test_finalize_failed_if_building_leaves_complete_untouched(tmp_path):
    build_state.mark_complete(tmp_path)
    build_state.finalize_failed_if_building(tmp_path, error="late")
    assert build_state.read_build_state(tmp_path)["status"] == "complete"


def test_finalize_failed_if_building_noop_when_absent(tmp_path):
    build_state.finalize_failed_if_building(tmp_path, error="x")
    assert build_state.read_build_state(tmp_path) is None


def test_marker_writes_never_raise_on_unwritable_dir(tmp_path):
    """Best-effort contract: a marker hiccup must never crash a build."""
    missing = tmp_path / "does" / "not" / "exist"
    build_state.mark_failed(missing, stage="x", error="y")  # must not raise
    # mkdir'd lazily or swallowed — either way read on the parent stays None
    assert build_state.read_build_state(tmp_path) is None


# ---------------------------------------------------------------------------
# _pack_is_complete × marker matrix
# ---------------------------------------------------------------------------

_PACK_FILES = ("registry.json", "knowledge_graph.json", "rules.json", "dictionary.json")


def _seed_pack_files(pack_dir, *, omit: str | None = None):
    pack_dir.mkdir(parents=True, exist_ok=True)
    for name in _PACK_FILES:
        if name != omit:
            (pack_dir / name).write_text("{}", encoding="utf-8")


def test_pack_complete_legacy_no_marker(tmp_path):
    """Packs built before the marker existed (self-host) stay complete."""
    _seed_pack_files(tmp_path / "p")
    assert _pack_is_complete(tmp_path / "p") is True


def test_pack_complete_with_complete_marker(tmp_path):
    _seed_pack_files(tmp_path / "p")
    build_state.mark_complete(tmp_path / "p")
    assert _pack_is_complete(tmp_path / "p") is True


@pytest.mark.parametrize("status_setter, kwargs", [
    (build_state.mark_failed, {"stage": "audit", "error": "boom"}),
    (build_state.mark_building, {}),
    (build_state.mark_paused, {"reason": "needs_kind_confirmation"}),
])
def test_partial_pack_marker_blocks_completeness(tmp_path, status_setter, kwargs):
    """The whole point: all 4 files on disk but the build did NOT succeed →
    the pack must NOT count as complete (no phantom coverage, retry rebuilds)."""
    _seed_pack_files(tmp_path / "p")
    status_setter(tmp_path / "p", **kwargs)
    assert _pack_is_complete(tmp_path / "p") is False


def test_complete_marker_does_not_override_missing_files(tmp_path):
    _seed_pack_files(tmp_path / "p", omit="rules.json")
    build_state.mark_complete(tmp_path / "p")
    assert _pack_is_complete(tmp_path / "p") is False


# ---------------------------------------------------------------------------
# Lot 2 — owner-aware completeness: a web-only pack staged under one owner is
# complete FOR THAT OWNER but never for the shared commons / other tenants.
# ---------------------------------------------------------------------------

def _seed_staged_pack(pack_dir, owner):
    staged = pack_dir / "_staged" / owner
    staged.mkdir(parents=True, exist_ok=True)
    for name in _PACK_FILES:
        (staged / name).write_text('{"items": []}', encoding="utf-8")
    build_state.mark_complete(pack_dir)


def test_staged_pack_complete_for_its_owner(tmp_path):
    _seed_staged_pack(tmp_path / "p", "tenant-A")
    assert _pack_is_complete(tmp_path / "p", owner_ref="tenant-A") is True


def test_staged_pack_not_complete_for_commons_or_other_tenant(tmp_path):
    _seed_staged_pack(tmp_path / "p", "tenant-A")
    assert _pack_is_complete(tmp_path / "p") is False  # commons (owner_ref=None)
    assert _pack_is_complete(tmp_path / "p", owner_ref="tenant-B") is False


# ---------------------------------------------------------------------------
# Orchestrator wiring — the marker follows the real pipeline outcomes
# ---------------------------------------------------------------------------

from api.pipeline import orchestrator  # noqa: E402
from api.pipeline.schemas import (  # noqa: E402
    AuditVerdict,
    Dictionary,
    KnowledgeGraph,
    Registry,
    RulesSet,
)


def _phase_patches(verdict: AuditVerdict):
    registry = Registry(device_label="Demo", components=[], signals=[])
    outputs = (KnowledgeGraph(nodes=[], edges=[]), RulesSet(rules=[]), Dictionary(entries=[]))
    return (
        patch("api.pipeline.orchestrator.run_scout", new=AsyncMock(return_value="# dump")),
        patch("api.pipeline.orchestrator.run_registry_builder", new=AsyncMock(return_value=registry)),
        patch("api.pipeline.orchestrator.run_writers_parallel", new=AsyncMock(return_value=outputs)),
        patch("api.pipeline.orchestrator.run_auditor", new=AsyncMock(return_value=verdict)),
    )


def _approved() -> AuditVerdict:
    return AuditVerdict(
        overall_status="APPROVED",
        consistency_score=1.0,
        files_to_rewrite=[],
        drift_report=[],
        revision_brief="",
    )


@pytest.mark.asyncio
async def test_orchestrator_success_marks_complete(tmp_path):
    p1, p2, p3, p4 = _phase_patches(_approved())
    with p1, p2, p3, p4:
        await orchestrator.generate_knowledge_pack(
            "Demo", client=object(), memory_root=tmp_path,
        )
    state = build_state.read_build_state(tmp_path / "demo")
    assert state["status"] == "complete"
    assert _pack_is_complete(tmp_path / "demo") is True


@pytest.mark.asyncio
async def test_orchestrator_failure_marks_failed_and_pack_incomplete(tmp_path):
    """Mid-pipeline exception (the live-test shape: writers wrote the 4 files,
    audit blew up) → marker says failed → the partial pack stops lying."""
    async def _boom(*_a: Any, **_k: Any):
        raise RuntimeError("audit exploded")

    p1, p2, p3, _ = _phase_patches(_approved())
    with p1, p2, p3, patch("api.pipeline.orchestrator.run_auditor", new=AsyncMock(side_effect=_boom)):
        with pytest.raises(RuntimeError):
            await orchestrator.generate_knowledge_pack(
                "Demo", client=object(), memory_root=tmp_path,
            )
    state = build_state.read_build_state(tmp_path / "demo")
    assert state["status"] == "failed"
    assert _pack_is_complete(tmp_path / "demo") is False


@pytest.mark.asyncio
async def test_orchestrator_kind_pause_marks_paused_not_failed(tmp_path):
    """The Phase-1.5 confirmation pause is a legitimate early return — it must
    not be recorded as a failure (but the pack is still not complete)."""
    from api.pipeline.schemas import KindVerdict

    pack = tmp_path / "dev-x"
    pack.mkdir(parents=True)
    (pack / "electrical_graph.json").write_text("{}", encoding="utf-8")
    verdict = KindVerdict(device_kind="gpu_card", confidence=0.9, evidence="GPU rails")

    with (
        patch.object(orchestrator, "_load_existing_electrical_graph", return_value=object()),
        patch("api.pipeline.device_kind.classify_device_kind", new=AsyncMock(return_value=verdict)),
        patch.object(orchestrator, "run_scout", new=AsyncMock()),
    ):
        result = await orchestrator.generate_knowledge_pack(
            "dev-x", client=object(), memory_root=tmp_path,
            user_device_kind="laptop_logic_board",
        )

    assert result.status == "NEEDS_KIND_CONFIRMATION"
    state = build_state.read_build_state(pack)
    assert state["status"] == "paused"
    assert _pack_is_complete(pack) is False
