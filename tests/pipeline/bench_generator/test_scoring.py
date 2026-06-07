from api.pipeline.bench_generator.schemas import (
    Cause,
    ProposedScenario,
)
from api.pipeline.bench_generator.scoring import score_accepted
from api.pipeline.schematic.evaluator import Scorecard


def _scenario(local_id: str = "s1") -> ProposedScenario:
    return ProposedScenario(
        id=local_id,
        device_slug="toy-board",
        cause=Cause(refdes="C19", mode="shorted"),
        expected_dead_rails=["+3V3"],
        expected_dead_components=[],
        source_url="https://example.com/x",
        source_quote="x" * 60,
        source_archive="benchmark/auto_proposals/sources/s1.txt",
        confidence=0.9,
        generated_by="bench-gen-sonnet-4-6",
        generated_at="2026-04-24T21:00:00Z",
    )


def test_score_accepted_wraps_evaluator(toy_graph):
    scorecard = score_accepted(toy_graph, [_scenario()])
    assert isinstance(scorecard, Scorecard)
    assert scorecard.n_scenarios == 1
    assert 0.0 <= scorecard.score <= 1.0


def test_score_accepted_empty_is_zero(toy_graph):
    scorecard = score_accepted(toy_graph, [])
    assert scorecard.n_scenarios == 0
    # self_mrr can be non-zero (depends on graph); cascade_recall is 0
    assert scorecard.cascade_recall == 0.0
