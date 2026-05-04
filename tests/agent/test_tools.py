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
    from api.agent.tools import mb_get_component
    from api.session.state import SessionState

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


def test_mb_get_component_lru_skips_pack_reload(tmp_path: Path, monkeypatch):
    from api.agent.tools import mb_get_component
    from api.session.state import SessionState

    slug = "demo"
    pack_dir = tmp_path / slug
    pack_dir.mkdir()
    (pack_dir / "registry.json").write_text('{"components": [{"canonical_name": "U5", "kind": "ic"}], "signals": []}')
    (pack_dir / "dictionary.json").write_text('{"entries": [{"canonical_name": "U5", "role": "pmic"}]}')
    (pack_dir / "rules.json").write_text('{"rules": []}')

    session = SessionState()
    calls: list[tuple[str, str]] = []
    from api.agent import tools as tools_mod
    orig_load_pack = tools_mod._load_pack
    def spy(slug_arg, root, session=None):
        calls.append((slug_arg, "pack"))
        return orig_load_pack(slug_arg, root, session=session)
    monkeypatch.setattr(tools_mod, "_load_pack", spy)

    mb_get_component(device_slug=slug, refdes="U5", memory_root=tmp_path, session=session)
    mb_get_component(device_slug=slug, refdes="U5", memory_root=tmp_path, session=session)

    # R1 means the second call hits cached pack; R2 means it never invokes _load_pack at all.
    assert len(calls) == 1, f"expected 1 _load_pack call, got {len(calls)}"


def test_mb_get_component_lru_evicts_oldest_when_full(tmp_path: Path):
    """Exceeding COMPONENT_CACHE_MAX entries must evict the oldest (LRU) entry."""
    from api.agent.tools import mb_get_component
    from api.session.state import SessionState

    slug = "demo"
    pack_dir = tmp_path / slug
    pack_dir.mkdir()
    # Build a registry with enough refdes to force eviction.
    cap = SessionState.COMPONENT_CACHE_MAX
    components = [
        {"canonical_name": f"U{i}", "kind": "ic"} for i in range(cap + 2)
    ]
    (pack_dir / "registry.json").write_text(
        '{"components": ' + str(components).replace("'", '"') + ', "signals": []}'
    )
    (pack_dir / "dictionary.json").write_text('{"entries": []}')
    (pack_dir / "rules.json").write_text('{"rules": []}')

    session = SessionState()
    # Fill the cache exactly to cap — oldest entry is U0.
    for i in range(cap):
        mb_get_component(
            device_slug=slug, refdes=f"U{i}", memory_root=tmp_path, session=session,
        )
    assert len(session.component_cache) == cap
    assert (slug, "U0") in session.component_cache

    # One more query past the cap — U0 (oldest) must be evicted.
    mb_get_component(
        device_slug=slug, refdes=f"U{cap}", memory_root=tmp_path, session=session,
    )
    assert len(session.component_cache) == cap
    assert (slug, "U0") not in session.component_cache
    assert (slug, f"U{cap}") in session.component_cache


def test_mb_get_component_caches_not_found(tmp_path: Path, monkeypatch):
    """Not-found results must also be cached — second query for unknown refdes skips _load_pack."""
    from api.agent import tools as tools_mod
    from api.agent.tools import mb_get_component
    from api.session.state import SessionState

    slug = "demo"
    pack_dir = tmp_path / slug
    pack_dir.mkdir()
    (pack_dir / "registry.json").write_text('{"components": [], "signals": []}')
    (pack_dir / "dictionary.json").write_text('{"entries": []}')
    (pack_dir / "rules.json").write_text('{"rules": []}')

    calls: list[str] = []
    orig_load_pack = tools_mod._load_pack
    def spy(slug_arg, root, session=None):
        calls.append(slug_arg)
        return orig_load_pack(slug_arg, root, session=session)
    monkeypatch.setattr(tools_mod, "_load_pack", spy)

    session = SessionState()
    first = mb_get_component(
        device_slug=slug, refdes="U999", memory_root=tmp_path, session=session,
    )
    assert first["found"] is False

    second = mb_get_component(
        device_slug=slug, refdes="U999", memory_root=tmp_path, session=session,
    )
    assert second["found"] is False
    assert len(calls) == 1, f"expected 1 _load_pack call (first lookup), got {len(calls)}"


def test_invalidate_pack_cache_drops_component_entries(tmp_path: Path):
    """After invalidate_pack_cache, no component_cache entry for that slug survives."""
    from api.agent.tools import mb_get_component
    from api.session.state import SessionState

    slug = "demo"
    pack_dir = tmp_path / slug
    pack_dir.mkdir()
    (pack_dir / "registry.json").write_text(
        '{"components": [{"canonical_name": "U1", "kind": "ic"}], "signals": []}'
    )
    (pack_dir / "dictionary.json").write_text('{"entries": []}')
    (pack_dir / "rules.json").write_text('{"rules": []}')

    session = SessionState()
    mb_get_component(device_slug=slug, refdes="U1", memory_root=tmp_path, session=session)
    mb_get_component(device_slug=slug, refdes="U2", memory_root=tmp_path, session=session)
    assert (slug, "U1") in session.component_cache
    assert (slug, "U2") in session.component_cache

    session.invalidate_pack_cache(slug)

    assert slug not in session.pack_cache
    assert (slug, "U1") not in session.component_cache
    assert (slug, "U2") not in session.component_cache
