# tests/pipeline/test_live_graph_demo.py
from pathlib import Path

from api.pipeline import live_graph


def _demo_pack(tmp_path) -> Path:
    pack = tmp_path / "mnt-reform-motherboard"
    (pack / "schematic_pages").mkdir(parents=True)
    (pack / "schematic_pages" / "page-001.png").write_bytes(b"\x89PNG")
    (pack / "electrical_graph.json").write_text("{}")
    return pack


def test_demo_slug_pages_fall_back_to_root_for_managed_owner(tmp_path):
    pack = _demo_pack(tmp_path)
    # Managed owner, no per-owner pin → demo slug serves the shared root pages.
    pages = live_graph.resolve_pages_dir(pack, "tenant-xyz")
    assert pages == pack / "schematic_pages"


def test_non_demo_slug_pages_have_no_fallback(tmp_path):
    pack = tmp_path / "iphone-12"
    (pack / "schematic_pages").mkdir(parents=True)
    (pack / "schematic_pages" / "p.png").write_bytes(b"\x89PNG")
    assert live_graph.resolve_pages_dir(pack, "tenant-xyz") is None


def test_self_host_root_unchanged(tmp_path):
    pack = _demo_pack(tmp_path)
    assert live_graph.resolve_pages_dir(pack, None) == pack / "schematic_pages"
