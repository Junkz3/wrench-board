"""T8 — les endpoints pack lisent encore correctement APRÈS migration.

La migration T8 déplace registry.json/rules.json/knowledge_graph.json/
dictionary.json de la racine vers baseline/. Les readers de routes/packs.py
(taxonomy, /full, /graph, presence bitmask) doivent suivre — sinon un pack
expandé/touché par l'agent renvoie vide.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from api.pipeline.pack_migrate import migrate_pack_if_needed

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "demo-pack"


def _seed_and_migrate(memory_root: Path, slug: str = "demo-pi") -> Path:
    dst = memory_root / slug
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("registry.json", "knowledge_graph.json", "rules.json", "dictionary.json"):
        shutil.copy(FIXTURE_ROOT / name, dst / name)
    migrate_pack_if_needed(memory_root, slug)
    # Sanity : la racine a bien été migrée.
    assert not (dst / "registry.json").exists()
    assert (dst / "baseline" / "registry.json").exists()
    return dst


def test_full_pack_after_migration(memory_root, client):
    _seed_and_migrate(memory_root)
    res = client.get("/pipeline/packs/demo-pi/full")
    assert res.status_code == 200
    body = res.json()
    assert body["device_slug"] == "demo-pi"
    assert body["device_label"] == "Demo Pi"
    names = {c["canonical_name"] for c in body["registry"]["components"]}
    assert "U7" in names
    assert {r["id"] for r in body["rules"]["rules"]} == {"rule-demo-001"}
    assert {e["canonical_name"] for e in body["dictionary"]["entries"]} == {"U7", "C29"}
    assert {n["id"] for n in body["knowledge_graph"]["nodes"]} >= {"cmp_U7"}


def test_taxonomy_after_migration(memory_root, client):
    _seed_and_migrate(memory_root)
    res = client.get("/pipeline/taxonomy")
    assert res.status_code == 200
    tree = res.json()
    # demo-pi n'a pas de taxonomy → uncategorized, mais doit apparaître + complete.
    slugs = {e["device_slug"] for e in tree["uncategorized"]}
    for brand in tree["brands"].values():
        for entries in brand.values():
            slugs |= {e["device_slug"] for e in entries}
    assert "demo-pi" in slugs


def test_graph_after_migration(memory_root, client):
    _seed_and_migrate(memory_root)
    res = client.get("/pipeline/packs/demo-pi/graph")
    assert res.status_code == 200
    body = res.json()
    assert "nodes" in body and "edges" in body
    assert any(n.get("id") == "cmp_U7" for n in body["nodes"])


def test_summary_after_migration_reports_complete(memory_root, client):
    _seed_and_migrate(memory_root)
    res = client.get("/pipeline/packs/demo-pi")
    assert res.status_code == 200
    s = res.json()
    assert s["has_registry"] is True
    assert s["has_rules"] is True
    assert s["has_dictionary"] is True
    assert s["has_knowledge_graph"] is True


# ---------------------------------------------------------------------------
# Lot 2 — /full owner-aware: a staged web-only pack shows for its owner only.
# ---------------------------------------------------------------------------

def test_full_pack_staged_visible_only_to_owner(memory_root, client):
    from api.pipeline.pack_migrate import stage_web_only_pack

    dst = memory_root / "webonly-x"
    dst.mkdir(parents=True)
    for name in ("registry.json", "knowledge_graph.json", "rules.json", "dictionary.json"):
        shutil.copy(FIXTURE_ROOT / name, dst / name)
    stage_web_only_pack(memory_root, "webonly-x", owner_ref="tenant-A")
    from api.pipeline import build_state
    build_state.mark_complete(dst)

    # Owner sees the pack content.
    res_owner = client.get("/pipeline/packs/webonly-x/full", headers={"X-Owner-Ref": "tenant-A"})
    assert res_owner.status_code == 200
    assert res_owner.json()["registry"] is not None
    assert {c["canonical_name"] for c in res_owner.json()["registry"]["components"]} >= {"U7"}

    # Commons (no header) sees nothing — the staged pack is private.
    res_commons = client.get("/pipeline/packs/webonly-x/full")
    assert res_commons.status_code == 200
    assert res_commons.json()["registry"] is None
