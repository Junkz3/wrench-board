"""Phase 1.5 device-kind gate — orchestrator short-circuit + provenance.

These tests drive `generate_knowledge_pack` only as far as the Phase 1.5
classification gate. Scout is stubbed so the short-circuit (and the
no-spend guarantee) can be asserted without touching the network.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from api.pipeline import orchestrator as orch
from api.pipeline.schemas import KindVerdict


@pytest.mark.asyncio
async def test_pipeline_short_circuits_on_kind_disagreement(tmp_path):
    """User declares one kind, graph confidently infers another → gate pauses.

    The pipeline must write `pending_kind.json`, return status
    NEEDS_KIND_CONFIRMATION, and never spend a Scout call.
    """
    pack = tmp_path / "dev-x"
    pack.mkdir(parents=True)
    (pack / "electrical_graph.json").write_text("{}", encoding="utf-8")

    verdict = KindVerdict(
        device_kind="gpu_card",
        confidence=0.9,
        evidence="GPU core rail + GDDR rails",
    )

    with (
        # Graph loader must return a non-None graph so the gate's
        # `if graph is not None` branch runs. The object is opaque — the
        # classifier is stubbed, so its shape is never inspected.
        patch.object(orch, "_load_existing_electrical_graph", return_value=object()),
        # classify_device_kind is called as `device_kind.classify_device_kind`
        # in the orchestrator, so patch it at the device_kind module path.
        patch(
            "api.pipeline.device_kind.classify_device_kind",
            new=AsyncMock(return_value=verdict),
        ),
        patch.object(orch, "run_scout", new=AsyncMock()) as scout,
    ):
        result = await orch.generate_knowledge_pack(
            "dev-x",
            client=object(),  # never used — Scout is stubbed before any client call
            memory_root=tmp_path,
            user_device_kind="laptop_logic_board",
        )

    scout.assert_not_awaited()
    assert result.status == "NEEDS_KIND_CONFIRMATION"
    pending = pack / "pending_kind.json"
    assert pending.is_file()
    data = json.loads(pending.read_text(encoding="utf-8"))
    assert data["status"] == "needs_confirmation"
    assert data["user_declared"] == "laptop_logic_board"
    assert data["graph_inferred"] == "gpu_card"


@pytest.mark.asyncio
async def test_pipeline_short_circuits_on_low_confidence(tmp_path):
    """Graph verdict below CONFIRM_THRESHOLD also pauses for confirmation."""
    pack = tmp_path / "dev-y"
    pack.mkdir(parents=True)
    (pack / "electrical_graph.json").write_text("{}", encoding="utf-8")

    verdict = KindVerdict(device_kind="gpu_card", confidence=0.3, evidence="unsure")

    with (
        patch.object(orch, "_load_existing_electrical_graph", return_value=object()),
        patch(
            "api.pipeline.device_kind.classify_device_kind",
            new=AsyncMock(return_value=verdict),
        ),
        patch.object(orch, "run_scout", new=AsyncMock()) as scout,
    ):
        result = await orch.generate_knowledge_pack(
            "dev-y",
            client=object(),
            memory_root=tmp_path,
            user_device_kind="gpu_card",
        )

    scout.assert_not_awaited()
    assert result.status == "NEEDS_KIND_CONFIRMATION"
    assert (pack / "pending_kind.json").is_file()


@pytest.mark.asyncio
async def test_confirmed_kind_rerun_clears_pending_and_writes_provenance(tmp_path):
    """A re-run with confirmed_device_kind resolves the gate and proceeds.

    We let it reach Scout (stubbed to raise) to prove the gate did NOT
    short-circuit: classify is never called, pending is cleared, provenance
    is written with resolved_by=user, and resolved_kind is threaded into
    run_scout.
    """
    pack = tmp_path / "dev-z"
    pack.mkdir(parents=True)
    (pack / "electrical_graph.json").write_text("{}", encoding="utf-8")
    # Pre-seed a pending file from a prior disagreement.
    (pack / "pending_kind.json").write_text("{}", encoding="utf-8")

    classify = AsyncMock()
    # Scout raises to abort the run cleanly right after the gate.
    scout = AsyncMock(side_effect=RuntimeError("stop-after-gate"))

    with (
        patch.object(orch, "_load_existing_electrical_graph", return_value=object()),
        patch("api.pipeline.device_kind.classify_device_kind", new=classify),
        patch.object(orch, "run_scout", new=scout),
    ):
        with pytest.raises(RuntimeError, match="stop-after-gate"):
            await orch.generate_knowledge_pack(
                "dev-z",
                client=object(),
                memory_root=tmp_path,
                user_device_kind="laptop_logic_board",
                confirmed_device_kind="gpu_card",
            )

    classify.assert_not_awaited()  # confirmation path skips classification
    assert not (pack / "pending_kind.json").exists()  # cleared
    prov = pack / "device_kind.json"
    assert prov.is_file()
    data = json.loads(prov.read_text(encoding="utf-8"))
    assert data["resolved_kind"] == "gpu_card"
    assert data["resolved_by"] == "user"
    # resolved_kind was threaded into Scout.
    scout.assert_awaited_once()
    assert scout.await_args.kwargs["device_kind"] == "gpu_card"
