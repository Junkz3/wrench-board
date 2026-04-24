"""Unit tests for the intent classifier (offline, mocked)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.pipeline.intent_classifier import IntentCandidate, IntentClassification


def test_intent_candidate_requires_slug():
    with pytest.raises(ValidationError):
        IntentCandidate(label="ok", confidence=0.5, pack_exists=True)


def test_intent_candidate_confidence_bounds():
    with pytest.raises(ValidationError):
        IntentCandidate(slug="x", label="x", confidence=1.5, pack_exists=True)
    with pytest.raises(ValidationError):
        IntentCandidate(slug="x", label="x", confidence=-0.1, pack_exists=True)


def test_intent_classification_max_three_candidates():
    cands = [
        IntentCandidate(slug=f"d{i}", label=f"D{i}", confidence=0.5, pack_exists=True)
        for i in range(4)
    ]
    with pytest.raises(ValidationError):
        IntentClassification(symptoms="x", candidates=cands)


def test_intent_classification_empty_candidates_ok():
    obj = IntentClassification(symptoms="rien de connu", candidates=[])
    assert obj.candidates == []
