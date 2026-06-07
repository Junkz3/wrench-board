# tests/scripts/test_build_demo_pack.py
import json
from pathlib import Path

from scripts.build_demo_pack import build_demo_pack

EXAMPLE_REPAIR_ID = "example-mnt-reform"


def _make_source_pack(root: Path) -> Path:
    slug = root / "mnt-reform-motherboard"
    (slug / "baseline").mkdir(parents=True)
    (slug / "audit").mkdir()
    (slug / "promoted").mkdir()
    (slug / "schematic_pages").mkdir()
    (slug / "_sources" / "tenant-abc").mkdir(parents=True)
    # kept files
    (slug / "baseline" / "knowledge_graph.json").write_text('{"nodes": []}')
    (slug / "electrical_graph.json").write_text('{"v": 1}')
    (slug / "schematic_graph.json").write_text('{"v": 1}')
    (slug / "parts_index.json").write_text('{"parts": []}')
    (slug / "schematic_pages" / "page-001.png").write_bytes(b"\x89PNG")
    # excluded / sensitive
    (slug / "audit" / "raw_research_dump.md").write_text("secret research")
    (slug / "promoted" / "expansion.json").write_text('{"owner_ref": "tenant-abc"}')
    (slug / "token_stats.json").write_text('{"cost": 1}')
    # a pre-existing repair carrying owner_ref (must be scrubbed/replaced)
    (slug / "repairs").mkdir()
    (slug / "repairs" / "old.json").write_text(json.dumps({
        "repair_id": "old", "device_slug": "mnt-reform-motherboard",
        "owner_ref": "tenant-abc", "symptom": "x", "status": "open",
    }))
    return slug


def test_build_demo_pack_curates_and_sanitizes(tmp_path):
    src = _make_source_pack(tmp_path / "src")
    dest = tmp_path / "fixtures" / "demo-packs" / "mnt-reform-motherboard"

    build_demo_pack(src, dest, example_repair_id=EXAMPLE_REPAIR_ID,
                    device_label="MNT Reform Motherboard", symptom="No power on")

    # kept
    assert (dest / "baseline" / "knowledge_graph.json").is_file()
    assert (dest / "electrical_graph.json").is_file()
    assert (dest / "schematic_graph.json").is_file()
    assert (dest / "parts_index.json").is_file()
    assert (dest / "schematic_pages" / "page-001.png").is_file()
    # excluded
    assert not (dest / "audit").exists()
    assert not (dest / "promoted").exists()
    assert not (dest / "token_stats.json").exists()
    assert not (dest / "_sources").exists()
    # the only repair is the clean example, no owner_ref anywhere
    repairs = list((dest / "repairs").glob("*.json"))
    assert len(repairs) == 1
    example = json.loads(repairs[0].read_text())
    assert example["repair_id"] == EXAMPLE_REPAIR_ID
    assert example["device_slug"] == "mnt-reform-motherboard"
    assert example["status"] == "open"
    assert "owner_ref" not in example
    # no residual owner_ref in any kept JSON
    for p in dest.rglob("*.json"):
        assert "owner_ref" not in p.read_text()
