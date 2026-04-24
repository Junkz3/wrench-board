# SPDX-License-Identifier: Apache-2.0
"""Integration test for POST /pipeline/classify-intent."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from api.main import app
from api.pipeline.intent_classifier import IntentCandidate, IntentClassification

client = TestClient(app)


def test_classify_intent_returns_classification():
    fake = IntentClassification(
        symptoms="MNT Reform — pas de boot",
        candidates=[
            IntentCandidate(slug="mnt-reform-motherboard", label="MNT Reform — carte mère", confidence=0.92, pack_exists=True),
        ],
    )
    with patch("api.pipeline.classify_intent", new=AsyncMock(return_value=fake)):
        res = client.post("/pipeline/classify-intent", json={"text": "MNT Reform ne démarre pas"})
    assert res.status_code == 200
    body = res.json()
    assert body["symptoms"] == "MNT Reform — pas de boot"
    assert body["candidates"][0]["slug"] == "mnt-reform-motherboard"
    assert body["candidates"][0]["pack_exists"] is True


def test_classify_intent_rejects_empty_text():
    res = client.post("/pipeline/classify-intent", json={"text": "   "})
    assert res.status_code == 422


def test_classify_intent_returns_503_on_anthropic_failure():
    from anthropic import APIConnectionError

    async def raise_anthropic(*_a, **_k):
        # APIConnectionError requires a `request` kwarg in the SDK constructor.
        # We use a minimal MagicMock so the test stays self-contained.
        from unittest.mock import MagicMock
        raise APIConnectionError(request=MagicMock())

    with patch("api.pipeline.classify_intent", new=raise_anthropic):
        res = client.post("/pipeline/classify-intent", json={"text": "rien"})
    assert res.status_code == 503
