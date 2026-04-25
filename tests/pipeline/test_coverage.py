# SPDX-License-Identifier: Apache-2.0
"""Tests for the symptom-coverage classifier.

The LLM call itself is exercised end-to-end by the tool_call helper;
here we assert the shape contracts, the "no rules" short-circuit, and
the confidence / matched_rule_id guard.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.pipeline.coverage import (
    _build_rules_summary,
    _load_rules,
    check_symptom_coverage,
)
from api.pipeline.schemas import CoverageCheck


# --- Schema round-trip -----------------------------------------------------


def test_coverage_check_shape() -> None:
    c = CoverageCheck(
        covered=True,
        matched_rule_id="rule-tristar-001",
        confidence=0.92,
        reason="matches rule-tristar-001, both describe no-charge",
    )
    j = c.model_dump_json()
    back = CoverageCheck.model_validate_json(j)
    assert back == c


def test_coverage_check_rejects_out_of_range_confidence() -> None:
    with pytest.raises(ValueError):
        CoverageCheck(
            covered=False, matched_rule_id=None, confidence=1.5, reason="x"
        )


def test_coverage_check_null_matched_rule_id_allowed() -> None:
    c = CoverageCheck(
        covered=False, matched_rule_id=None, confidence=0.2, reason="unrelated"
    )
    assert c.matched_rule_id is None


# --- _load_rules helper ----------------------------------------------------


def test_load_rules_missing_file_returns_none(tmp_path: Path) -> None:
    assert _load_rules(tmp_path, "never-seen-device") is None


def test_load_rules_empty_rules_returns_none(tmp_path: Path) -> None:
    slug = "demo"
    (tmp_path / slug).mkdir()
    (tmp_path / slug / "rules.json").write_text('{"rules": []}')
    assert _load_rules(tmp_path, slug) is None


def test_load_rules_malformed_json_returns_none(tmp_path: Path) -> None:
    slug = "demo"
    (tmp_path / slug).mkdir()
    (tmp_path / slug / "rules.json").write_text("{not json at all}")
    assert _load_rules(tmp_path, slug) is None


def test_load_rules_valid_returns_list(tmp_path: Path) -> None:
    slug = "demo"
    (tmp_path / slug).mkdir()
    payload = {
        "schema_version": "1.0",
        "rules": [
            {"id": "rule-a", "symptoms": ["no charge"]},
            {"id": "rule-b", "symptoms": ["screen dark"]},
        ],
    }
    (tmp_path / slug / "rules.json").write_text(json.dumps(payload))
    rules = _load_rules(tmp_path, slug)
    assert rules is not None
    assert len(rules) == 2
    assert rules[0]["id"] == "rule-a"


# --- _build_rules_summary --------------------------------------------------


def test_build_rules_summary_one_line_per_symptom() -> None:
    rules = [
        {"id": "rule-a", "symptoms": ["no charge", "iPhone won't power on"]},
        {"id": "rule-b", "symptoms": ["screen dark"]},
    ]
    summary = _build_rules_summary(rules)
    lines = summary.splitlines()
    assert len(lines) == 3
    assert "rule_id=rule-a · symptom: no charge" in lines[0]
    assert "rule_id=rule-a · symptom: iPhone won't power on" in lines[1]
    assert "rule_id=rule-b · symptom: screen dark" in lines[2]


def test_build_rules_summary_missing_id_graceful() -> None:
    rules = [{"symptoms": ["x"]}]
    summary = _build_rules_summary(rules)
    assert "rule-unknown" in summary


# --- check_symptom_coverage cold-cache short-circuit ----------------------


@pytest.mark.asyncio
async def test_check_skips_llm_when_no_rules(tmp_path: Path) -> None:
    """No rules.json → return {covered=False, confidence=0} without LLM."""
    client = MagicMock()
    # If the code called the LLM at all, the mock would fail because
    # we didn't configure messages.create.
    out = await check_symptom_coverage(
        client=client,
        model="claude-haiku-4-5",
        device_slug="never-seen",
        symptom="USB port dead",
        memory_root=tmp_path,
    )
    assert out.covered is False
    assert out.confidence == 0.0
    assert out.matched_rule_id is None
    assert "no prior rules" in out.reason.lower()


@pytest.mark.asyncio
async def test_check_skips_llm_when_rules_empty(tmp_path: Path) -> None:
    slug = "demo"
    (tmp_path / slug).mkdir()
    (tmp_path / slug / "rules.json").write_text('{"rules": []}')
    client = MagicMock()
    out = await check_symptom_coverage(
        client=client,
        model="claude-haiku-4-5",
        device_slug=slug,
        symptom="USB port dead",
        memory_root=tmp_path,
    )
    assert out.covered is False
    assert out.confidence == 0.0


# --- confidence threshold guard -------------------------------------------


@pytest.mark.asyncio
async def test_matched_rule_id_stripped_when_below_threshold(
    tmp_path: Path, monkeypatch
) -> None:
    """If the LLM returns matched_rule_id with low confidence, we strip it."""
    slug = "demo"
    (tmp_path / slug).mkdir()
    (tmp_path / slug / "rules.json").write_text(
        '{"rules": [{"id": "rule-a", "symptoms": ["no charge"]}]}'
    )

    async def _fake_call_with_forced_tool(**kwargs):
        return CoverageCheck(
            covered=True,
            matched_rule_id="rule-a",
            confidence=0.5,  # below 0.7 threshold
            reason="weak match",
        )

    monkeypatch.setattr(
        "api.pipeline.coverage.call_with_forced_tool",
        _fake_call_with_forced_tool,
    )

    out = await check_symptom_coverage(
        client=MagicMock(),
        model="claude-haiku-4-5",
        device_slug=slug,
        symptom="screen dark",
        memory_root=tmp_path,
    )
    # Guard: matched_rule_id stripped to None when confidence < 0.7.
    assert out.matched_rule_id is None
    assert out.confidence == 0.5


@pytest.mark.asyncio
async def test_matched_rule_id_kept_when_above_threshold(
    tmp_path: Path, monkeypatch
) -> None:
    slug = "demo"
    (tmp_path / slug).mkdir()
    (tmp_path / slug / "rules.json").write_text(
        '{"rules": [{"id": "rule-a", "symptoms": ["no charge"]}]}'
    )

    async def _fake_call_with_forced_tool(**kwargs):
        return CoverageCheck(
            covered=True,
            matched_rule_id="rule-a",
            confidence=0.9,
            reason="paraphrase match",
        )

    monkeypatch.setattr(
        "api.pipeline.coverage.call_with_forced_tool",
        _fake_call_with_forced_tool,
    )

    out = await check_symptom_coverage(
        client=MagicMock(),
        model="claude-haiku-4-5",
        device_slug=slug,
        symptom="won't charge",
        memory_root=tmp_path,
    )
    assert out.matched_rule_id == "rule-a"
    assert out.confidence == 0.9
