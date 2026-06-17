"""Lot 2 + Lot 3 — orchestrator finalize: a private build is staged per-owner.

`_stage_if_private` decides, after a build completes, whether the freshly built
pack stays SHARED (schematic-backed + PASS/WARN, or self-host) or is relocated to
the per-owner private staging layer (managed build with no electrical graph, OR a
schematic build that FAILED the graph↔boardview coverage gate). Pure + fast — no
LLM, no full pipeline run.
"""

import json
from pathlib import Path

from api.pipeline.orchestrator import _stage_if_private

SLUG = "device-x"


def _root_writer_files(memory_root: Path) -> Path:
    pack = memory_root / SLUG
    pack.mkdir(parents=True)
    (pack / "registry.json").write_text(json.dumps({
        "components": [{"canonical_name": "U1", "kind": "IC", "aliases": []}],
        "signals": [],
    }))
    (pack / "rules.json").write_text(json.dumps({"rules": []}))
    (pack / "knowledge_graph.json").write_text(json.dumps({"nodes": [], "edges": []}))
    (pack / "dictionary.json").write_text(json.dumps({"entries": []}))
    return pack


def test_self_host_build_stays_shared(tmp_path):
    pack = _root_writer_files(tmp_path)
    staged = _stage_if_private(tmp_path, pack, SLUG, owner_ref=None)
    assert staged is False
    assert (pack / "registry.json").is_file()  # untouched — root, shared


def test_schematic_backed_passing_build_stays_shared(tmp_path):
    pack = _root_writer_files(tmp_path)
    (pack / "electrical_graph.json").write_text(json.dumps({"nodes": [], "edges": []}))
    staged = _stage_if_private(tmp_path, pack, SLUG, owner_ref="tenant-A", coverage_verdict="PASS")
    assert staged is False
    assert (pack / "registry.json").is_file()  # graph + PASS → shared commons


def test_web_only_managed_build_is_staged_private(tmp_path):
    pack = _root_writer_files(tmp_path)  # no electrical_graph.json
    staged = _stage_if_private(tmp_path, pack, SLUG, owner_ref="tenant-A")
    assert staged is True
    assert not (pack / "registry.json").exists()  # relocated
    assert (pack / "_staged" / "tenant-A" / "registry.json").is_file()
    assert not (pack / "baseline" / "registry.json").exists()  # never shared


def test_schematic_build_failing_coverage_is_staged_private(tmp_path):
    pack = _root_writer_files(tmp_path)
    (pack / "electrical_graph.json").write_text(json.dumps({"nodes": [], "edges": []}))
    staged = _stage_if_private(tmp_path, pack, SLUG, owner_ref="tenant-A", coverage_verdict="FAIL")
    assert staged is True  # incomplete source PDF must not become the shared pack
    assert (pack / "_staged" / "tenant-A" / "registry.json").is_file()
    assert not (pack / "registry.json").exists()


def test_schematic_build_warn_coverage_stays_shared(tmp_path):
    pack = _root_writer_files(tmp_path)
    (pack / "electrical_graph.json").write_text(json.dumps({"nodes": [], "edges": []}))
    staged = _stage_if_private(tmp_path, pack, SLUG, owner_ref="tenant-A", coverage_verdict="WARN")
    assert staged is False  # WARN = operator review, still shared
    assert (pack / "registry.json").is_file()
