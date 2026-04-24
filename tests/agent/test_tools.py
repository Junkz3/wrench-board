# SPDX-License-Identifier: Apache-2.0
"""Tests for api.agent.tools (the 2 mb_* tools exposed in v1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from api.agent.tools import mb_get_component, mb_get_rules_for_symptoms

FIXTURE_DIR = Path(__file__).parent.parent / "pipeline" / "fixtures" / "demo-pack"


@pytest.fixture
def seeded_memory_root(tmp_path):
    dest = tmp_path / "demo-pi"
    dest.mkdir()
    for name in ("registry.json", "dictionary.json", "knowledge_graph.json", "rules.json"):
        (dest / name).write_text((FIXTURE_DIR / name).read_text())
    return tmp_path


def test_mb_get_component_found(seeded_memory_root):
    result = mb_get_component(
        device_slug="demo-pi", refdes="U7", memory_root=seeded_memory_root,
    )
    assert result["found"] is True
    assert result["canonical_name"] == "U7"
    assert result["memory_bank"] is not None
    assert result["memory_bank"]["role"] == "PMIC"
    assert result["memory_bank"]["package"] == "QFN-24"
    assert result["memory_bank"]["kind"] == "pmic"
    assert result["board"] is None  # no session passed


def test_mb_get_component_not_found_suggests_closest(seeded_memory_root):
    result = mb_get_component(
        device_slug="demo-pi", refdes="U999", memory_root=seeded_memory_root,
    )
    assert result["found"] is False
    assert result["error"] == "not_found"
    assert "closest_matches" in result
    assert "U7" in result["closest_matches"]
    assert "memory_bank" not in result
    assert "board" not in result


def test_mb_get_component_empty_refdes_returns_not_found(seeded_memory_root):
    result = mb_get_component(
        device_slug="demo-pi", refdes="", memory_root=seeded_memory_root,
    )
    assert result["found"] is False
    assert result["error"] == "not_found"


def test_mb_get_rules_for_symptoms_returns_matches(seeded_memory_root):
    result = mb_get_rules_for_symptoms(
        device_slug="demo-pi",
        symptoms=["3V3 rail dead"],
        memory_root=seeded_memory_root,
    )
    assert isinstance(result["matches"], list)
    assert len(result["matches"]) >= 1
    assert result["matches"][0]["rule_id"] == "rule-demo-001"
    assert result["matches"][0]["overlap_count"] == 1
    assert result["matches"][0]["confidence"] == 0.82
    assert result["total_available_rules"] == 1


def test_mb_get_rules_for_symptoms_case_insensitive(seeded_memory_root):
    result = mb_get_rules_for_symptoms(
        device_slug="demo-pi",
        symptoms=["3V3 RAIL DEAD"],
        memory_root=seeded_memory_root,
    )
    assert len(result["matches"]) == 1


def test_mb_get_rules_for_symptoms_no_overlap_empty(seeded_memory_root):
    result = mb_get_rules_for_symptoms(
        device_slug="demo-pi",
        symptoms=["completely unrelated symptom"],
        memory_root=seeded_memory_root,
    )
    assert result["matches"] == []
    assert result["total_available_rules"] == 1


def test_mb_get_rules_for_symptoms_max_results(seeded_memory_root):
    result = mb_get_rules_for_symptoms(
        device_slug="demo-pi",
        symptoms=["3V3 rail dead", "device doesn't boot"],
        memory_root=seeded_memory_root,
        max_results=0,
    )
    assert result["matches"] == []


def test_pack_cache_hits_on_repeated_calls(tmp_path: Path, monkeypatch):
    """Second mb_get_component call on same slug must not re-read pack files."""
    from api.session.state import SessionState
    from api.agent.tools import mb_get_component

    slug = "demo"
    pack_dir = tmp_path / slug
    pack_dir.mkdir()
    (pack_dir / "registry.json").write_text('{"components": [{"canonical_name": "U1", "kind": "ic"}], "signals": []}')
    (pack_dir / "dictionary.json").write_text('{"entries": [{"canonical_name": "U1", "role": "cpu"}]}')
    (pack_dir / "rules.json").write_text('{"rules": []}')

    session = SessionState()
    reads: list[Path] = []
    orig_read_text = Path.read_text
    def counting_read(self, *args, **kwargs):
        if self.suffix == ".json" and self.parent == pack_dir:
            reads.append(self)
        return orig_read_text(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", counting_read)

    mb_get_component(device_slug=slug, refdes="U1", memory_root=tmp_path, session=session)
    first_call_reads = len(reads)
    assert first_call_reads >= 3  # registry + dictionary + rules

    mb_get_component(device_slug=slug, refdes="U1", memory_root=tmp_path, session=session)
    assert len(reads) == first_call_reads, "second call hit disk — cache did not work"
