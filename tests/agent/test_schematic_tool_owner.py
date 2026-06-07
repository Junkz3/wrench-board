"""T9 — l'outil agent résout le graphe du bon owner (current_owner_ref ContextVar).

Le READ path per-owner : `_load_graph` doit résoudre owner→hash→cache partagé
via `current_owner_ref()`, pas lire la racine du slug. Deux tenants épinglés sur
deux PDF distincts (hA / hB) du MÊME slug lisent deux graphes distincts.
Self-host (owner None) lit la racine du slug, inchangé.
"""

from __future__ import annotations

import json
from pathlib import Path

from api.agent.owner_ref import set_owner_ref
from api.pipeline import live_graph, sources
from api.tools import schematic as sch_tool


def _seed(pack: Path, pdf_hash: str, nodes: list[str]) -> None:
    cdir = sources.cache_dir_for(pack, pdf_hash)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "schematic.pdf").write_bytes(b"%PDF")
    (cdir / "schematic_graph.json").write_text("{}")
    (cdir / "electrical_graph.json").write_text(json.dumps({"nodes": nodes}))


def test_load_graph_resolves_per_owner(tmp_path: Path) -> None:
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    _seed(pack, "hA", ["A"])
    _seed(pack, "hB", ["B"])
    live_graph.write_owner_active(pack, "tenant-A", "schematic_pdf", "a.pdf", "hA")
    live_graph.write_owner_active(pack, "tenant-B", "schematic_pdf", "b.pdf", "hB")

    set_owner_ref("tenant-A")
    try:
        ga, err_a = sch_tool._load_graph("iphone-x", tmp_path)
    finally:
        set_owner_ref(None)

    set_owner_ref("tenant-B")
    try:
        gb, err_b = sch_tool._load_graph("iphone-x", tmp_path)
    finally:
        set_owner_ref(None)

    assert err_a is None and err_b is None
    assert ga is not None and gb is not None
    assert ga["nodes"] == ["A"]
    assert gb["nodes"] == ["B"]
    assert ga != gb


def test_load_graph_managed_no_pin_falls_back_to_canonical(tmp_path: Path) -> None:
    """Tenant managé sans pin → graphe CANONIQUE partagé du slug (moat T6) :
    _load_graph renvoie le graphe racine, pas 'no_schematic_graph'. (Un uploader
    a son graphe per-owner — cf. test_load_graph_resolves_per_owner.)"""
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    (pack / "electrical_graph.json").write_text(json.dumps({"nodes": ["ROOT"]}))

    set_owner_ref("tenant-Z")
    try:
        graph, err = sch_tool._load_graph("iphone-x", tmp_path)
    finally:
        set_owner_ref(None)

    assert err is None
    assert graph is not None and graph.get("nodes") == ["ROOT"]


def test_load_graph_self_host_reads_root(tmp_path: Path) -> None:
    """owner None (self-host) lit la racine du slug — comportement historique."""
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    (pack / "electrical_graph.json").write_text(json.dumps({"nodes": ["ROOT"]}))

    graph, err = sch_tool._load_graph("iphone-x", tmp_path)

    assert err is None
    assert graph is not None
    assert graph["nodes"] == ["ROOT"]
