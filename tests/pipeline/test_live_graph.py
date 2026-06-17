"""T9 — résolution du graphe vif per-owner. Isolé : pas de pipeline, pas de LLM."""

import json
from pathlib import Path

import pytest

from api.pipeline import live_graph, sources


def _seed_cache(pack_dir: Path, pdf_hash: str, label: str) -> None:
    """Crée un .cache_schematic/{hash}/ minimal (les fichiers que is_cached exige)."""
    cdir = sources.cache_dir_for(pack_dir, pdf_hash)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "schematic.pdf").write_bytes(b"%PDF-1.7 " + label.encode())
    (cdir / "schematic_graph.json").write_text(json.dumps({"label": label}))
    (cdir / "electrical_graph.json").write_text(json.dumps({"nodes": [label]}))
    (cdir / "schematic_pages").mkdir(exist_ok=True)
    (cdir / "schematic_pages" / "p1.json").write_text(json.dumps({"page": label}))


def test_write_then_read_owner_active(tmp_path):
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    live_graph.write_owner_active(pack, "tenant-A", "schematic_pdf", "20260529-schematic_pdf-x.pdf", "abc123")
    active = live_graph.read_owner_active(pack, "tenant-A")
    assert active["schematic_pdf"] == {"filename": "20260529-schematic_pdf-x.pdf", "hash": "abc123"}
    assert (pack / "_sources" / "tenant-A" / sources.ACTIVE_FILE).is_file()


def test_resolve_cache_dir_managed(tmp_path):
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    _seed_cache(pack, "hashA", "A")
    live_graph.write_owner_active(pack, "tenant-A", "schematic_pdf", "fileA.pdf", "hashA")
    cdir = live_graph.resolve_cache_dir(pack, "tenant-A")
    assert cdir == sources.cache_dir_for(pack, "hashA")
    assert (cdir / "electrical_graph.json").is_file()


def test_anti_leak_two_tenants_different_pdfs(tmp_path):
    """LE test critique : A et B sur le même slug, PDF différents → graphes différents."""
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    _seed_cache(pack, "hashA", "A")
    _seed_cache(pack, "hashB", "B")
    live_graph.write_owner_active(pack, "tenant-A", "schematic_pdf", "fileA.pdf", "hashA")
    live_graph.write_owner_active(pack, "tenant-B", "schematic_pdf", "fileB.pdf", "hashB")
    ga = live_graph.resolve_graph_path(pack, "tenant-A")
    gb = live_graph.resolve_graph_path(pack, "tenant-B")
    assert ga != gb
    assert json.loads(ga.read_text())["nodes"] == ["A"]
    assert json.loads(gb.read_text())["nodes"] == ["B"]


def test_moat_same_pdf_same_cache(tmp_path):
    """Deux tenants, MÊME PDF (même hash) → littéralement les mêmes fichiers."""
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    _seed_cache(pack, "hashSAME", "S")
    live_graph.write_owner_active(pack, "tenant-A", "schematic_pdf", "fileA.pdf", "hashSAME")
    live_graph.write_owner_active(pack, "tenant-B", "schematic_pdf", "fileB.pdf", "hashSAME")
    assert live_graph.resolve_graph_path(pack, "tenant-A") == live_graph.resolve_graph_path(pack, "tenant-B")


def test_self_host_reads_root(tmp_path):
    """Owner None → racine du slug, inchangé."""
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    (pack / "electrical_graph.json").write_text(json.dumps({"nodes": ["root"]}))
    g = live_graph.resolve_graph_path(pack, None)
    assert g == pack / "electrical_graph.json"
    assert json.loads(g.read_text())["nodes"] == ["root"]


def test_no_pin_returns_none(tmp_path):
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    assert live_graph.resolve_cache_dir(pack, "tenant-A") is None
    assert live_graph.resolve_graph_path(pack, "tenant-A") is None
    assert live_graph.resolve_pages_dir(pack, "tenant-A") is None


def test_invalid_owner_rejected(tmp_path):
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    with pytest.raises(ValueError):
        live_graph.write_owner_active(pack, "../evil", "schematic_pdf", "f.pdf", "h")
    with pytest.raises(ValueError):
        live_graph.read_owner_active(pack, "bad owner")


def test_resolve_pages_dir(tmp_path):
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    _seed_cache(pack, "hashP", "P")
    live_graph.write_owner_active(pack, "tenant-A", "schematic_pdf", "f.pdf", "hashP")
    pages = live_graph.resolve_pages_dir(pack, "tenant-A")
    assert pages == sources.cache_dir_for(pack, "hashP") / "schematic_pages"
    assert (pages / "p1.json").is_file()


# ── clear_owner_active ──────────────────────────────────────────────────────


def test_clear_owner_active_removes_kind(tmp_path):
    """Retire uniquement le kind ciblé ; les autres kinds restent intacts."""
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    live_graph.write_owner_active(pack, "tenant-A", "schematic_pdf", "rev1.pdf", "h1")
    live_graph.write_owner_active(pack, "tenant-A", "boardview", "board.brd", None)

    live_graph.clear_owner_active(pack, "tenant-A", "schematic_pdf")

    active = live_graph.read_owner_active(pack, "tenant-A")
    assert "schematic_pdf" not in active, "schematic_pdf doit avoir été retiré"
    assert active.get("boardview") == {"filename": "board.brd", "hash": None}, \
        "boardview doit rester intact"


def test_clear_owner_active_noop_when_absent(tmp_path):
    """No-op si le pointeur n'existe pas encore — pas d'erreur, pas de fichier créé."""
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    # Appel sur un slug sans aucun pointeur per-owner.
    live_graph.clear_owner_active(pack, "tenant-A", "schematic_pdf")
    # Le fichier pointeur ne doit PAS avoir été créé.
    pointer = pack / "_sources" / "tenant-A" / sources.ACTIVE_FILE
    assert not pointer.exists(), "aucun pointeur ne doit être créé pour un no-op"


def test_clear_owner_active_noop_when_kind_absent(tmp_path):
    """No-op si le pointeur existe mais ne contient pas le kind ciblé."""
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    live_graph.write_owner_active(pack, "tenant-A", "boardview", "board.brd", None)

    live_graph.clear_owner_active(pack, "tenant-A", "schematic_pdf")  # pas encore présent

    active = live_graph.read_owner_active(pack, "tenant-A")
    assert "boardview" in active, "boardview doit rester présent après un no-op sur schematic_pdf"
    assert "schematic_pdf" not in active


def test_clear_owner_active_invalid_owner(tmp_path):
    """owner_ref invalide → ValueError (même validation que write/read)."""
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    with pytest.raises(ValueError, match="invalid owner_ref"):
        live_graph.clear_owner_active(pack, "../evil", "schematic_pdf")
    with pytest.raises(ValueError, match="invalid owner_ref"):
        live_graph.clear_owner_active(pack, "bad owner", "schematic_pdf")


def test_clear_owner_active_unknown_kind(tmp_path):
    """kind inconnu → ValueError (KNOWN_KINDS vérifié avant toute lecture disque)."""
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    with pytest.raises(ValueError, match="unknown kind"):
        live_graph.clear_owner_active(pack, "tenant-A", "datasheet")
