"""Unit tests for the defensive unwrap in call_with_forced_tool.

Opus 4.7 under forced tool_choice occasionally stringifies nested structures.
_try_unwrap must recover from both observed pathologies before we give up
and retry (which is expensive and usually reproduces the same failure).
"""

from __future__ import annotations

from api.pipeline.schemas import KnowledgeGraph, RulesSet
from api.pipeline.tool_call import _try_unwrap


def test_unwrap_recovers_stringified_nested_list():
    """Case A — one field stringifies the nested list."""
    payload = {
        "schema_version": "1.0",
        "rules": (
            '[{"id":"r1","symptoms":["x"],'
            '"likely_causes":[{"refdes":"U7","probability":0.5,"mechanism":"short"}],'
            '"diagnostic_steps":[],"confidence":0.6,"sources":[]}]'
        ),
    }
    recovered = _try_unwrap(payload, RulesSet)
    assert recovered is not None
    assert isinstance(recovered, RulesSet)
    assert len(recovered.rules) == 1
    assert recovered.rules[0].id == "r1"


def test_unwrap_recovers_collapsed_payload():
    """Case B — the whole target is wedged into one stringified field."""
    inner = (
        '{"schema_version":"1.0","rules":['
        '{"id":"r1","symptoms":["x"],'
        '"likely_causes":[{"refdes":"U7","probability":0.5,"mechanism":"short"}],'
        '"diagnostic_steps":[],"confidence":0.6,"sources":[]}'
        "]}"
    )
    payload = {"rules": inner}
    recovered = _try_unwrap(payload, RulesSet)
    assert recovered is not None
    assert len(recovered.rules) == 1


def test_unwrap_returns_none_when_nothing_matches():
    payload = {"rules": "this is just prose"}
    assert _try_unwrap(payload, RulesSet) is None


def test_unwrap_returns_none_on_non_dict():
    assert _try_unwrap(["not", "a", "dict"], RulesSet) is None
    assert _try_unwrap("prose", RulesSet) is None


def test_unwrap_leaves_good_payload_alone_then_fails():
    """A correct payload should validate at the top level — _try_unwrap isn't
    called in that path. But when called directly with a good payload, it still
    works because it re-validates after the no-op unwrap."""
    payload = {"schema_version": "1.0", "nodes": [], "edges": []}
    recovered = _try_unwrap(payload, KnowledgeGraph)
    # No strings to unwrap, so changed=False → falls through to the value-level
    # check which also won't find anything. Expected: None.
    assert recovered is None
