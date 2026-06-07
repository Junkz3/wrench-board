"""Unit tests for the A/B cost harness metric aggregation.

`scripts/ab_cost_diag.py` drives two real diagnostic runs (managed vs direct)
and compares their token cost. The WS-driving part is real I/O (like
bench_agent_flow, untested here); the metric aggregation is pure and tested
below — a bug there would silently skew the very comparison the harness exists
to make.
"""

from __future__ import annotations

from scripts.ab_cost_diag import aggregate


def _turn_cost(**kw) -> dict:
    base = {
        "type": "turn_cost",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cost_usd": 0.0,
    }
    base.update(kw)
    return base


def test_aggregate_sums_tokens_cost_and_counts_turns() -> None:
    frames = [
        {"type": "message", "role": "assistant", "text": "hi"},
        _turn_cost(input_tokens=1000, output_tokens=200, cost_usd=0.01),
        {"type": "tool_use", "name": "mb_get_component"},
        _turn_cost(
            input_tokens=50, output_tokens=300,
            cache_read_input_tokens=900, cost_usd=0.02,
        ),
    ]
    m = aggregate(frames)
    assert m.turns == 2
    assert m.input_tokens == 1050
    assert m.output_tokens == 500
    assert m.cache_read_input_tokens == 900
    assert m.tool_calls == 1
    assert m.assistant_messages == 1
    assert round(m.cost_usd, 4) == 0.03


def test_aggregate_cache_hit_rate_excludes_output_tokens() -> None:
    # hit rate = cache_read / (input + cache_read + cache_creation); output is
    # not a prompt-cache tier and must not dilute the denominator.
    frames = [
        _turn_cost(
            input_tokens=100, cache_read_input_tokens=900,
            cache_creation_input_tokens=0, output_tokens=9999,
        )
    ]
    m = aggregate(frames)
    assert round(m.cache_hit_rate, 3) == 0.9


def test_aggregate_ignores_replayed_turn_costs() -> None:
    # On a resumed session the runtime replays past turn_cost frames flagged
    # replay=True — counting them would double-bill the comparison.
    frames = [
        _turn_cost(input_tokens=100, cost_usd=0.01),
        {**_turn_cost(input_tokens=999, cost_usd=9.99), "replay": True},
    ]
    m = aggregate(frames)
    assert m.turns == 1
    assert m.input_tokens == 100
    assert round(m.cost_usd, 4) == 0.01


def test_aggregate_counts_errors() -> None:
    frames = [
        {"type": "stream_error", "error": "stream_timeout"},
        {"type": "error", "text": "boom"},
        _turn_cost(input_tokens=10),
    ]
    m = aggregate(frames)
    assert m.errors == 2
    assert m.turns == 1
