"""T9 — le write path écrit un pointeur per-owner en mode managé, sans clobber
de la racine entre deux tenants. Owner None = comportement inchangé.

Inclut aussi les tests du chemin de suppression managée (Task 2 — T9) :
DELETE /packs/{slug}/sources/{kind}/versions/{filename} avec X-Owner-Ref.
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from api.pipeline import live_graph, sources
from api.pipeline.routes import documents as docs


def _seed_cached_upload(pack: Path, filename: str, content: bytes) -> str:
    """Upload + son cache (PDF déjà ingéré → is_cached True)."""
    (pack / "uploads").mkdir(parents=True, exist_ok=True)
    (pack / "uploads" / filename).write_bytes(content)
    h = sources.hash_pdf(pack / "uploads" / filename)
    cdir = sources.cache_dir_for(pack, h)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "schematic.pdf").write_bytes(content)
    (cdir / "schematic_graph.json").write_text("{}")
    (cdir / "electrical_graph.json").write_text(json.dumps({"src": filename}))
    return h


def test_managed_pin_writes_owner_pointer_no_root_clobber(tmp_path):
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    _seed_cached_upload(pack, "20260529-schematic_pdf-A.pdf", b"%PDF A")
    _seed_cached_upload(pack, "20260529-schematic_pdf-B.pdf", b"%PDF B")

    docs._apply_schematic_pin("iphone-x", pack, "20260529-schematic_pdf-A.pdf", owner_ref="tenant-A")
    docs._apply_schematic_pin("iphone-x", pack, "20260529-schematic_pdf-B.pdf", owner_ref="tenant-B")

    ga = live_graph.resolve_graph_path(pack, "tenant-A")
    gb = live_graph.resolve_graph_path(pack, "tenant-B")
    assert json.loads(ga.read_text())["src"] == "20260529-schematic_pdf-A.pdf"
    assert json.loads(gb.read_text())["src"] == "20260529-schematic_pdf-B.pdf"
    # En managé : la racine n'est PAS matérialisée.
    assert not (pack / "electrical_graph.json").exists()


def test_self_host_pin_materialises_root_unchanged(tmp_path):
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    _seed_cached_upload(pack, "20260529-schematic_pdf-A.pdf", b"%PDF A")
    status, _, _ = docs._apply_schematic_pin("iphone-x", pack, "20260529-schematic_pdf-A.pdf", owner_ref=None)
    assert status == "cached"
    assert (pack / "electrical_graph.json").is_file()  # racine matérialisée (inchangé)


# ── Managed DELETE — chemin de suppression per-owner ───────────────────────
#
# Route : DELETE /pipeline/packs/{slug}/sources/{kind}/versions/{filename}
#         avec en-tête X-Owner-Ref pour le mode managé.
#
# Cas couverts :
#   1. Supprimer le fichier qui EST le pin actif du tenant → status "cleared",
#      pointeur per-owner retiré pour ce kind.
#   2. Supprimer un fichier qui N'EST PAS le pin actif du tenant → status
#      "deleted", pin per-owner inchangé.
#   3. Self-host (pas de X-Owner-Ref) → chemin racine inchangé (status "cleared"
#      si aucun remplacement disponible).


_SLUG = "demo-board"
_KIND = "schematic_pdf"
_FILE_A = "20260529T000001Z-schematic_pdf-revA.pdf"
_FILE_B = "20260529T000002Z-schematic_pdf-revB.pdf"
_OWNER = "tenant-A"


def _setup_two_uploads(memory_root: Path) -> Path:
    """Crée deux uploads schematic_pdf pour demo-board, pas de pin racine."""
    pack = memory_root / _SLUG
    (pack / "uploads").mkdir(parents=True, exist_ok=True)
    (pack / "uploads" / _FILE_A).write_bytes(b"%PDF-1.4 revA")
    (pack / "uploads" / _FILE_B).write_bytes(b"%PDF-1.4 revB")
    return pack


def test_managed_delete_active_clears_owner_pin(memory_root: Path, client: TestClient) -> None:
    """Supprimer le fichier actif du tenant → pin per-owner retiré, status 'cleared'."""
    pack = _setup_two_uploads(memory_root)
    # Écrire un pin per-owner pour _FILE_A (simuler un upload déjà pinné).
    live_graph.write_owner_active(pack, _OWNER, _KIND, _FILE_A, None)

    res = client.delete(
        f"/pipeline/packs/{_SLUG}/sources/{_KIND}/versions/{_FILE_A}",
        headers={"X-Owner-Ref": _OWNER},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "cleared", f"attendu 'cleared', obtenu: {body['status']!r}"
    assert body["new_active"] is None

    # Le pointeur per-owner ne doit plus contenir schematic_pdf.
    active = live_graph.read_owner_active(pack, _OWNER)
    assert _KIND not in active, f"le pin per-owner '{_KIND}' doit avoir été effacé"

    # Le fichier physique doit avoir été supprimé.
    assert not (pack / "uploads" / _FILE_A).exists()


def test_managed_delete_non_active_leaves_pin_intact(memory_root: Path, client: TestClient) -> None:
    """Supprimer un fichier qui n'est PAS le pin actif → pin per-owner inchangé, status 'deleted'."""
    pack = _setup_two_uploads(memory_root)
    # Pincer _FILE_B comme actif ; supprimer _FILE_A (non actif).
    live_graph.write_owner_active(pack, _OWNER, _KIND, _FILE_B, None)

    res = client.delete(
        f"/pipeline/packs/{_SLUG}/sources/{_KIND}/versions/{_FILE_A}",
        headers={"X-Owner-Ref": _OWNER},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "deleted", f"attendu 'deleted', obtenu: {body['status']!r}"

    # Le pin per-owner doit pointer toujours vers _FILE_B.
    active = live_graph.read_owner_active(pack, _OWNER)
    assert active.get(_KIND, {}).get("filename") == _FILE_B, \
        "le pin per-owner doit toujours pointer vers _FILE_B"

    # Le fichier non-actif doit avoir été supprimé ; _FILE_B doit rester.
    assert not (pack / "uploads" / _FILE_A).exists()
    assert (pack / "uploads" / _FILE_B).exists()


def test_selfhost_delete_active_no_replacement_clears_root_pin(
    memory_root: Path, client: TestClient
) -> None:
    """Self-host (pas de X-Owner-Ref) : supprimer la seule version → pin racine effacé,
    status 'cleared' — le chemin self-host reste inchangé."""
    pack = memory_root / _SLUG
    (pack / "uploads").mkdir(parents=True, exist_ok=True)
    (pack / "uploads" / _FILE_A).write_bytes(b"%PDF-1.4 revA")
    # Écrire un pin racine (self-host).
    sources.write_active(pack, {_KIND: _FILE_A})

    res = client.delete(
        f"/pipeline/packs/{_SLUG}/sources/{_KIND}/versions/{_FILE_A}",
        # Pas de X-Owner-Ref → chemin self-host.
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "cleared"
    assert body["new_active"] is None

    # Le pin racine doit être vidé.
    pins = sources.read_active(pack)
    assert pins.get(_KIND) is None
