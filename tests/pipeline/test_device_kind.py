import json
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from api.pipeline.device_kind import (
    classify_device_kind,
    clear_pending_kind,
    read_pending_kind,
    reconcile_kind,
    summarize_graph_for_kind,
    write_kind_provenance,
    write_pending_kind,
)
from api.pipeline.schemas import DEVICE_KINDS, DeviceTaxonomy, KindVerdict
from api.pipeline.schematic.schemas import (
    ComponentNode,
    ElectricalGraph,
    PowerRail,
    SchematicQualityReport,
)


def test_device_kind_enum_values():
    assert DEVICE_KINDS == frozenset({
        "gpu_card", "laptop_logic_board", "phone_logic_board",
        "desktop_motherboard", "sbc_board", "power_charging_board",
        "other", "unknown",
    })


def test_taxonomy_accepts_device_kind_and_defaults_null():
    assert DeviceTaxonomy().device_kind is None
    assert DeviceTaxonomy(device_kind="gpu_card").device_kind == "gpu_card"


def test_kind_verdict_validates_confidence_bounds():
    v = KindVerdict(device_kind="gpu_card", confidence=0.9, evidence="NVVDD + GDDR rails")
    assert v.device_kind == "gpu_card"
    with pytest.raises(ValidationError):
        KindVerdict(device_kind="gpu_card", confidence=1.4, evidence="x")


def _graph(rails, kinds):
    return ElectricalGraph(
        device_slug="dev-x",
        components={
            f"R{i}": ComponentNode(refdes=f"R{i}", type="resistor", kind=k)
            for i, k in enumerate(kinds)
        },
        power_rails={name: PowerRail(label=name) for name in rails},
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )


def test_summary_lists_rails_and_kind_histogram_no_refdes():
    g = _graph(["NVVDD", "12V_PEX", "FBVDDQ"], ["ic", "ic", "passive_c"])
    s = summarize_graph_for_kind(g)
    assert "NVVDD" in s and "12V_PEX" in s and "FBVDDQ" in s
    assert "ic" in s.lower()
    # never leak refdes
    assert "R0" not in s and "R1" not in s


@pytest.mark.asyncio
async def test_classifier_passes_summary_and_label_no_refdes():
    g = _graph(["NVVDD", "FBVDDQ"], ["ic"])
    fake = KindVerdict(device_kind="gpu_card", confidence=0.92, evidence="NVVDD core + GDDR")
    with patch("api.pipeline.device_kind.call_with_forced_tool", new=AsyncMock(return_value=fake)) as m:
        v = await classify_device_kind(
            client=object(), model="m", device_label="MSI V311_11", graph=g,
        )
    assert v.device_kind == "gpu_card"
    sent = m.call_args.kwargs["messages"][0]["content"]
    assert "NVVDD" in sent and "MSI V311_11" in sent
    assert "R0" not in sent  # no refdes leaked
    system_sent = m.call_args.kwargs["system"]
    assert "topology" in system_sent


def _v(kind, conf):
    return KindVerdict(device_kind=kind, confidence=conf, evidence="e")


def test_reconcile_no_graph_uses_user():
    r = reconcile_kind(user_declared="laptop_logic_board", verdict=None)
    assert (r.resolved_kind, r.status) == ("laptop_logic_board", "user_only")


def test_reconcile_no_graph_no_user_is_unknown():
    r = reconcile_kind(user_declared=None, verdict=None)
    assert (r.resolved_kind, r.status) == ("unknown", "user_only")


def test_reconcile_agreement_confirmed():
    r = reconcile_kind(user_declared="gpu_card", verdict=_v("gpu_card", 0.9))
    assert (r.resolved_kind, r.status) == ("gpu_card", "confirmed")


def test_reconcile_user_silent_high_conf_takes_graph():
    r = reconcile_kind(user_declared=None, verdict=_v("gpu_card", 0.9))
    assert (r.resolved_kind, r.status) == ("gpu_card", "confirmed")


def test_reconcile_disagreement_needs_confirmation():
    r = reconcile_kind(user_declared="laptop_logic_board", verdict=_v("gpu_card", 0.9))
    assert r.status == "needs_confirmation" and r.resolved_kind is None
    assert r.user_declared == "laptop_logic_board" and r.graph_inferred == "gpu_card"


def test_reconcile_low_confidence_needs_confirmation():
    r = reconcile_kind(user_declared=None, verdict=_v("gpu_card", 0.4))
    assert r.status == "needs_confirmation" and r.resolved_kind is None


def test_reconcile_confidence_at_threshold_is_confirmed():
    # confidence == CONFIRM_THRESHOLD must pass the gate (operator is <, not <=)
    r = reconcile_kind(user_declared=None, verdict=_v("gpu_card", 0.6))
    assert r.status == "confirmed"
    assert r.resolved_kind == "gpu_card"


def test_pending_kind_roundtrip(tmp_path):
    r = reconcile_kind(user_declared="laptop_logic_board", verdict=_v("gpu_card", 0.9))
    write_pending_kind(tmp_path, r)
    loaded = read_pending_kind(tmp_path)
    assert loaded["user_declared"] == "laptop_logic_board"
    assert loaded["graph_inferred"] == "gpu_card"
    assert loaded["resolved_kind"] is None
    assert loaded["status"] == "needs_confirmation"
    clear_pending_kind(tmp_path)
    assert read_pending_kind(tmp_path) is None


def test_provenance_written(tmp_path):
    r = reconcile_kind(user_declared="gpu_card", verdict=_v("gpu_card", 0.9))
    write_kind_provenance(tmp_path, r, resolved_by="graph")
    data = json.loads((tmp_path / "device_kind.json").read_text())
    assert data["resolved_kind"] == "gpu_card" and data["resolved_by"] == "graph"
