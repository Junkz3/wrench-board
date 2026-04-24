"""Unit tests for the intent classifier (offline, mocked)."""
from __future__ import annotations
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from api.pipeline.intent_classifier import IntentCandidate, IntentClassification, classify_intent


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


def _make_anthropic_response(payload: dict):
    """Build a fake Anthropic Messages response wrapping a tool_use block."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "report_intent"
    block.input = payload
    block.id = "toolu_test"
    resp = MagicMock()
    resp.content = [block]
    resp.stop_reason = "tool_use"
    resp.usage = MagicMock(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return resp


@pytest.mark.asyncio
async def test_classify_intent_single_high_confidence(tmp_path: Path):
    pack_dir = tmp_path / "mnt-reform-motherboard"
    pack_dir.mkdir()
    (pack_dir / "registry.json").write_text('{"device_label": "MNT Reform — carte mère"}')

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(
        return_value=_make_anthropic_response({
            "symptoms": "MNT Reform — pas de boot",
            "candidates": [
                {"slug": "mnt-reform-motherboard", "label": "MNT Reform — carte mère", "confidence": 0.92},
            ],
        })
    )

    with patch("api.pipeline.intent_classifier._get_memory_root", return_value=tmp_path):
        result = await classify_intent("MNT Reform ne démarre pas, écran noir", client=fake_client)

    assert result.symptoms.startswith("MNT Reform")
    assert len(result.candidates) == 1
    assert result.candidates[0].slug == "mnt-reform-motherboard"
    assert result.candidates[0].pack_exists is True
    assert result.candidates[0].confidence == pytest.approx(0.92)


@pytest.mark.asyncio
async def test_classify_intent_unknown_pack_marked_false(tmp_path: Path):
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(
        return_value=_make_anthropic_response({
            "symptoms": "iPhone 11 — charge",
            "candidates": [
                {"slug": "iphone-11", "label": "iPhone 11", "confidence": 0.8},
            ],
        })
    )
    with patch("api.pipeline.intent_classifier._get_memory_root", return_value=tmp_path):
        result = await classify_intent("iPhone 11 charge plus", client=fake_client)
    assert result.candidates[0].pack_exists is False


@pytest.mark.asyncio
async def test_classify_intent_truncates_to_three(tmp_path: Path):
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(
        return_value=_make_anthropic_response({
            "symptoms": "vague",
            "candidates": [
                {"slug": f"d{i}", "label": f"D{i}", "confidence": 0.5 - i * 0.1}
                for i in range(5)
            ],
        })
    )
    with patch("api.pipeline.intent_classifier._get_memory_root", return_value=tmp_path):
        result = await classify_intent("ordinateur en panne", client=fake_client)
    assert len(result.candidates) == 3
    # sorted desc by confidence
    confs = [c.confidence for c in result.candidates]
    assert confs == sorted(confs, reverse=True)


@pytest.mark.asyncio
async def test_classify_intent_no_tool_use_returns_empty(tmp_path: Path):
    """If the model refuses the forced tool (returns end_turn / text only), we get an empty classification."""
    fake_client = MagicMock()
    text_block = MagicMock()
    text_block.type = "text"
    resp = MagicMock()
    resp.content = [text_block]
    resp.stop_reason = "end_turn"
    fake_client.messages.create = AsyncMock(return_value=resp)

    with patch("api.pipeline.intent_classifier._get_memory_root", return_value=tmp_path):
        result = await classify_intent("anything", client=fake_client)

    assert result.symptoms == ""
    assert result.candidates == []
