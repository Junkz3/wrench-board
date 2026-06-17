"""Fuite tenant : `GET /api/board/render` doit résoudre le boardview PER-OWNER.

Avant le fix, `render_board` appelait `_find_boardview(slug, pack_dir)` qui scanne le
pin GLOBAL / `board_assets/` / `memory/{slug}/uploads/` de la RACINE → un tenant qui
n'a rien uploadé récupérait le board d'un autre tenant du même slug (fuite confirmée
empiriquement : 200 + payload board complet). Miroir du patron T9
(`test_schematic_routes_owner.py`) : owner set → pin per-owner via `X-Owner-Ref` ;
pas de pin → 404 (PAS la racine) ; pas d'en-tête (self-host) → racine inchangée.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from api import config as config_mod
from api.pipeline import live_graph, sources

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.brd"


@pytest.fixture
def memory_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    yield tmp_path
    monkeypatch.setattr(config_mod, "_settings", None)


def _seed_owner_boardview(pack: Path, owner: str, filename: str) -> None:
    """Dépose un boardview dans uploads/ + épingle le pin per-owner du tenant."""
    uploads = pack / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE, uploads / filename)
    live_graph.write_owner_active(pack, owner, sources.BOARDVIEW_KIND, filename, None)


def test_render_resolves_per_owner(memory_root, client):
    """Le tenant qui a uploadé voit SON board."""
    slug = "iphone-x"
    pack = memory_root / slug
    pack.mkdir()
    _seed_owner_boardview(pack, "tenant-A", "20260529T000000Z-boardview-minimal.brd")

    r = client.get(f"/api/board/render?slug={slug}", headers={"X-Owner-Ref": "tenant-A"})
    assert r.status_code == 200, r.text
    assert "board_width" in r.json()


def test_render_managed_no_pin_404(memory_root, client):
    """Le tenant qui n'a PAS uploadé NE DOIT PAS voir le boardview d'un autre (fuite)."""
    slug = "iphone-x"
    pack = memory_root / slug
    pack.mkdir()
    # tenant-A a uploadé (fichier présent dans uploads/ + pin per-owner pour A).
    _seed_owner_boardview(pack, "tenant-A", "20260529T000000Z-boardview-minimal.brd")

    # tenant-Z n'a aucun pin → 404, et surtout PAS le board de tenant-A.
    r = client.get(f"/api/board/render?slug={slug}", headers={"X-Owner-Ref": "tenant-Z"})
    assert r.status_code == 404, r.text


def test_render_self_host_reads_root(memory_root, client):
    """Pas d'en-tête (self-host) → chaîne racine inchangée (pin global / uploads scan)."""
    slug = "iphone-x"
    pack = memory_root / slug
    pack.mkdir()
    uploads = pack / "uploads"
    uploads.mkdir(parents=True)
    shutil.copyfile(FIXTURE, uploads / "20260529T000000Z-boardview-minimal.brd")
    sources.write_active(pack, {sources.BOARDVIEW_KIND: "20260529T000000Z-boardview-minimal.brd"})

    r = client.get(f"/api/board/render?slug={slug}")
    assert r.status_code == 200, r.text
    assert "board_width" in r.json()
