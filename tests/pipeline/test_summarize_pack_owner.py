"""Lot 2 — _summarize_pack owner-aware: a staged web-only pack is 'present' for
its owner (so the cloud's getPack → complete=True for that tenant) but absent for
the commons / other tenants (free gate stays closed, no shared serving)."""

from pathlib import Path

from api.pipeline import build_state
from api.pipeline.routes.packs import _summarize_pack

SLUG = "device-y"
_FILES = ("registry.json", "knowledge_graph.json", "rules.json", "dictionary.json")


def _staged_only_pack(tmp_path: Path, owner: str) -> Path:
    pack = tmp_path / SLUG
    staged = pack / "_staged" / owner
    staged.mkdir(parents=True)
    for name in _FILES:
        (staged / name).write_text('{"items": []}')
    build_state.mark_complete(pack)
    return pack


def test_summary_present_for_owner(tmp_path):
    pack = _staged_only_pack(tmp_path, "tenant-A")
    s = _summarize_pack(pack, "tenant-A")
    assert s.has_registry and s.has_knowledge_graph and s.has_rules and s.has_dictionary


def test_summary_absent_for_commons_and_other_tenant(tmp_path):
    pack = _staged_only_pack(tmp_path, "tenant-A")
    commons = _summarize_pack(pack, None)
    assert not (commons.has_registry or commons.has_knowledge_graph or commons.has_rules or commons.has_dictionary)
    other = _summarize_pack(pack, "tenant-B")
    assert not (other.has_registry or other.has_knowledge_graph or other.has_rules or other.has_dictionary)
