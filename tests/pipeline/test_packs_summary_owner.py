"""Le résumé `GET /pipeline/packs/{slug}` doit refléter l'état PER-OWNER pour les
artefacts PRIVÉS (graphe électrique, boardview, schematic PDF) tout en gardant
SHARED les flags du pack commun (registry / knowledge_graph / rules / dictionary /
parts_index — le moat).

Avant le fix, `has_electrical_graph` / `has_boardview` / `has_schematic_pdf` étaient
lus à la RACINE → un tenant qui n'a rien uploadé voyait son dashboard « tout vert »
(plainte initiale). Miroir du patron T9 (`test_schematic_routes_owner.py`).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from api import config as config_mod
from api.pipeline import live_graph, sources

FIXTURE_BRD = Path(__file__).parents[1] / "board" / "fixtures" / "minimal.brd"


@pytest.fixture
def memory_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    yield tmp_path
    monkeypatch.setattr(config_mod, "_settings", None)


_GRAPH = json.dumps({
    "schema_version": "1.0", "device_slug": "x", "components": {}, "nets": {},
    "power_rails": {}, "typed_edges": [], "boot_sequence": [],
    "quality": {"total_pages": 1, "pages_parsed": 1},
})


def _seed_shared_and_root(pack: Path) -> None:
    """État RACINE complet : un graphe + un boardup + le pack partagé (registry…)."""
    pack.mkdir()
    # SHARED (le moat) — doit rester vert pour tout tenant.
    (pack / "registry.json").write_text("{}")
    # PRIVÉ à la racine (self-host / legacy).
    (pack / "electrical_graph.json").write_text(_GRAPH)
    uploads = pack / "uploads"
    uploads.mkdir()
    shutil.copyfile(FIXTURE_BRD, uploads / "20260529T000000Z-boardview-minimal.brd")


def test_summary_managed_no_upload_hides_private_renders_keeps_shared_graph(memory_root, client):
    """Tenant sans upload : RENDUS privés (boardview, schematic PDF brut) FALSE,
    mais le GRAPHE analysé (moat PARTAGÉ, canonique racine) + le pack commun TRUE."""
    slug = "iphone-x"
    pack = memory_root / slug
    _seed_shared_and_root(pack)   # racine = electrical_graph canonique + registry partagé
    # tenant-A a uploadé (pin per-owner boardview), tenant-Z n'a RIEN.
    live_graph.write_owner_active(pack, "tenant-A", sources.BOARDVIEW_KIND,
                                  "20260529T000000Z-boardview-minimal.brd", None)

    r = client.get(f"/pipeline/packs/{slug}", headers={"X-Owner-Ref": "tenant-Z"})
    assert r.status_code == 200, r.text
    body = r.json()
    # RENDUS privés du fichier brut → false (tenant-Z n'a rien uploadé)
    assert body["has_boardview"] is False
    assert body["has_schematic_pdf"] is False
    # Graphe analysé = moat PARTAGÉ → true (canonique du slug, dispo à tout tenant)
    assert body["has_electrical_graph"] is True
    # Pack commun partagé → true
    assert body["has_registry"] is True


def test_summary_managed_no_canonical_graph_false(memory_root, client):
    """Pas d'upload ET pas de graphe canonique → has_electrical_graph FALSE."""
    slug = "iphone-x"
    pack = memory_root / slug
    pack.mkdir()
    (pack / "registry.json").write_text("{}")   # pack partagé présent, mais aucun graphe
    r = client.get(f"/pipeline/packs/{slug}", headers={"X-Owner-Ref": "tenant-Z"})
    assert r.status_code == 200, r.text
    assert r.json()["has_electrical_graph"] is False
    assert r.json()["has_registry"] is True


def test_summary_managed_uploader_sees_own_private(memory_root, client):
    """Tenant qui a uploadé : son boardview + son graphe apparaissent."""
    slug = "iphone-x"
    pack = memory_root / slug
    _seed_shared_and_root(pack)
    # tenant-A épingle un boardview ET un schematic (cache hA seedé avec un graphe).
    live_graph.write_owner_active(pack, "tenant-A", sources.BOARDVIEW_KIND,
                                  "20260529T000000Z-boardview-minimal.brd", None)
    cdir = sources.cache_dir_for(pack, "hA")
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "electrical_graph.json").write_text(_GRAPH)
    live_graph.write_owner_active(pack, "tenant-A", sources.SCHEMATIC_KIND, "a.pdf", "hA")

    r = client.get(f"/pipeline/packs/{slug}", headers={"X-Owner-Ref": "tenant-A"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_boardview"] is True
    assert body["has_electrical_graph"] is True
    assert body["has_schematic_pdf"] is True


def test_summary_self_host_reads_root(memory_root, client):
    """Pas d'en-tête (self-host) → racine inchangée : privés racine TRUE."""
    slug = "iphone-x"
    pack = memory_root / slug
    _seed_shared_and_root(pack)

    r = client.get(f"/pipeline/packs/{slug}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_electrical_graph"] is True
    assert body["has_boardview"] is True
    assert body["has_registry"] is True
