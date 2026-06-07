"""Coverage for api/pipeline/schematic/evaluator.py."""

from __future__ import annotations

import pytest

from api.pipeline.schematic.evaluator import (
    Scorecard,
    compute_cascade_recall,
    compute_score,
    compute_self_mrr,
)
from api.pipeline.schematic.schemas import (
    ComponentNode,
    ElectricalGraph,
    NetNode,
    PagePin,
    PowerRail,
    SchematicQualityReport,
)


@pytest.fixture
def trivial_graph() -> ElectricalGraph:
    return ElectricalGraph(
        device_slug="trivial",
        components={
            "U7": ComponentNode(refdes="U7", type="ic"),
            "U12": ComponentNode(
                refdes="U12",
                type="ic",
                pins=[PagePin(number="1", role="power_in", net_label="+5V")],
            ),
        },
        nets={"+5V": NetNode(label="+5V", is_power=True)},
        power_rails={
            "+5V": PowerRail(label="+5V", source_refdes="U7", consumers=["U12"])
        },
        typed_edges=[],
        boot_sequence=[],
        designer_notes=[],
        ambiguities=[],
        quality=SchematicQualityReport(total_pages=1, pages_parsed=1),
    )


def test_compute_self_mrr_returns_float_in_range(trivial_graph):
    score = compute_self_mrr(trivial_graph)
    assert 0.0 <= score <= 1.0


def test_compute_self_mrr_returns_zero_when_no_pairs_sampled():
    empty = ElectricalGraph(
        device_slug="empty",
        components={},
        nets={},
        power_rails={},
        typed_edges=[],
        boot_sequence=[],
        designer_notes=[],
        ambiguities=[],
        quality=SchematicQualityReport(total_pages=0, pages_parsed=0),
    )
    assert compute_self_mrr(empty) == 0.0


def test_compute_cascade_recall_perfect_match(trivial_graph):
    scenarios = [
        {
            "id": "kill_u7",
            "device_slug": "trivial",
            "cause": {"refdes": "U7", "mode": "dead"},
            "expected_dead_rails": ["+5V"],
            "expected_dead_components": ["U12"],
        }
    ]
    recall, breakdown = compute_cascade_recall(trivial_graph, scenarios)
    assert recall == 1.0
    assert len(breakdown) == 1


def test_compute_cascade_recall_zero_when_predictions_disjoint(trivial_graph):
    scenarios = [
        {
            "id": "phantom",
            "device_slug": "trivial",
            "cause": {"refdes": "U7", "mode": "dead"},
            "expected_dead_rails": ["NONEXISTENT"],
            "expected_dead_components": ["NOPE"],
        }
    ]
    recall, _ = compute_cascade_recall(trivial_graph, scenarios)
    assert recall == 0.0


def test_compute_score_honours_60_40_weighting(trivial_graph):
    sc: Scorecard = compute_score(trivial_graph, scenarios=[])
    # Empty scenarios → cascade_recall = 0.0 → score = 0.6 × self_mrr.
    assert pytest.approx(sc.score, abs=1e-6) == 0.6 * sc.self_mrr
    assert sc.cascade_recall == 0.0


def test_compute_self_mrr_is_deterministic(trivial_graph):
    a = compute_self_mrr(trivial_graph)
    b = compute_self_mrr(trivial_graph)
    assert a == b
