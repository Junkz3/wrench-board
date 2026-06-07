"""Thin wrapper around `evaluator.compute_score`.

The evaluator accepts `list[dict]` scenarios. Our accepted scenarios are
typed `ProposedScenario`; this module just converts and delegates.
"""

from __future__ import annotations

from api.pipeline.bench_generator.schemas import ProposedScenario
from api.pipeline.schematic.evaluator import Scorecard, compute_score
from api.pipeline.schematic.schemas import ElectricalGraph


def score_accepted(
    graph: ElectricalGraph,
    scenarios: list[ProposedScenario],
) -> Scorecard:
    """Feed accepted scenarios to the evaluator in its native dict shape."""
    dicts: list[dict] = []
    for s in scenarios:
        entry = {
            "id": s.id,
            "device_slug": s.device_slug,
            "cause": s.cause.model_dump(exclude_none=True),
            "expected_dead_rails": s.expected_dead_rails,
            "expected_dead_components": s.expected_dead_components,
        }
        dicts.append(entry)
    return compute_score(graph, dicts)
