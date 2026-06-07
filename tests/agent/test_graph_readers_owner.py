"""T9 — résiduels : _known_refdes (validation) + _lookup_comp_kind (measurements)
lisent le graphe du bon owner (current_owner_ref ContextVar), pas la racine du slug.

Deux tenants épinglés sur deux PDF distincts (hA / hB) du MÊME slug : chacun voit
SON refdes/composant et pas celui de l'autre. Self-host (owner None) lit la racine,
inchangé. Mirror du seed helper de test_schematic_tool_owner.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from api.agent.owner_ref import set_owner_ref
from api.pipeline import live_graph, sources
from api.tools.measurements import _lookup_comp_kind
from api.tools.validation import _known_refdes


def _graph(refdes: str, kind: str) -> dict:
    """ElectricalGraph minimal valide avec UN composant `refdes` de `kind`
    (les deux lecteurs valident le graphe entier via ElectricalGraph)."""
    # `type` et `kind` sont deux Literals distincts dans ElectricalGraph :
    # type ∈ {resistor, ic, …}, kind ∈ {ic, passive_r, …}. _lookup_comp_kind
    # lit `kind`, _known_refdes la clé refdes.
    comp_type = "ic" if kind == "ic" else "resistor"
    return {
        "device_slug": "iphone-x",
        "quality": {"total_pages": 1, "pages_parsed": 1},
        "components": {
            refdes: {"refdes": refdes, "type": comp_type, "kind": kind, "pins": []}
        },
    }


def _seed(pack: Path, pdf_hash: str, refdes: str, kind: str) -> None:
    """Épingle un graphe per-hash dans .cache_schematic/{hash}/ (mirror du
    seed de test_schematic_tool_owner.py)."""
    cdir = sources.cache_dir_for(pack, pdf_hash)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "schematic.pdf").write_bytes(b"%PDF")
    (cdir / "schematic_graph.json").write_text("{}")
    (cdir / "electrical_graph.json").write_text(json.dumps(_graph(refdes, kind)))


def test_known_refdes_resolves_per_owner(tmp_path: Path) -> None:
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    _seed(pack, "hA", "U_A", "ic")
    _seed(pack, "hB", "U_B", "ic")
    live_graph.write_owner_active(pack, "tenant-A", "schematic_pdf", "a.pdf", "hA")
    live_graph.write_owner_active(pack, "tenant-B", "schematic_pdf", "b.pdf", "hB")

    set_owner_ref("tenant-A")
    try:
        known_a = _known_refdes(tmp_path, "iphone-x")
    finally:
        set_owner_ref(None)

    set_owner_ref("tenant-B")
    try:
        known_b = _known_refdes(tmp_path, "iphone-x")
    finally:
        set_owner_ref(None)

    assert known_a == {"U_A"}
    assert known_b == {"U_B"}
    # Croisé : A ne voit pas le composant de B et inversement.
    assert "U_B" not in known_a
    assert "U_A" not in known_b


def test_known_refdes_managed_no_pin_falls_back_to_canonical(tmp_path: Path) -> None:
    """Tenant managé sans pin → graphe CANONIQUE partagé du slug (moat T6) : les
    refdes du graphe racine sont connus (diag d'une carte connue sans upload). Un
    uploader garde son graphe per-owner (cf. test_lookup_comp_kind_resolves_per_owner)."""
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    (pack / "electrical_graph.json").write_text(json.dumps(_graph("U_ROOT", "ic")))

    set_owner_ref("tenant-Z")
    try:
        known = _known_refdes(tmp_path, "iphone-x")
    finally:
        set_owner_ref(None)

    assert known == {"U_ROOT"}


def test_known_refdes_self_host_reads_root(tmp_path: Path) -> None:
    """owner None (self-host) lit la racine du slug — inchangé."""
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    (pack / "electrical_graph.json").write_text(json.dumps(_graph("U_ROOT", "ic")))

    known = _known_refdes(tmp_path, "iphone-x")

    assert known == {"U_ROOT"}


def test_lookup_comp_kind_resolves_per_owner(tmp_path: Path) -> None:
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    _seed(pack, "hA", "U_A", "ic")
    _seed(pack, "hB", "U_B", "passive_r")
    live_graph.write_owner_active(pack, "tenant-A", "schematic_pdf", "a.pdf", "hA")
    live_graph.write_owner_active(pack, "tenant-B", "schematic_pdf", "b.pdf", "hB")

    set_owner_ref("tenant-A")
    try:
        kind_a_own = _lookup_comp_kind(tmp_path, "iphone-x", "U_A")
        kind_a_other = _lookup_comp_kind(tmp_path, "iphone-x", "U_B")
    finally:
        set_owner_ref(None)

    set_owner_ref("tenant-B")
    try:
        kind_b_own = _lookup_comp_kind(tmp_path, "iphone-x", "U_B")
        kind_b_other = _lookup_comp_kind(tmp_path, "iphone-x", "U_A")
    finally:
        set_owner_ref(None)

    assert kind_a_own == "ic"
    assert kind_a_other is None  # le composant de B est invisible pour A
    assert kind_b_own == "passive_r"
    assert kind_b_other is None


def test_lookup_comp_kind_managed_no_pin_falls_back_to_canonical(tmp_path: Path) -> None:
    """Sans pin → lit le graphe canonique partagé (moat) : U_ROOT y est connu."""
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    (pack / "electrical_graph.json").write_text(json.dumps(_graph("U_ROOT", "ic")))

    set_owner_ref("tenant-Z")
    try:
        kind = _lookup_comp_kind(tmp_path, "iphone-x", "U_ROOT")
    finally:
        set_owner_ref(None)

    assert kind == "ic"


def test_lookup_comp_kind_self_host_reads_root(tmp_path: Path) -> None:
    pack = tmp_path / "iphone-x"
    pack.mkdir()
    (pack / "electrical_graph.json").write_text(json.dumps(_graph("U_ROOT", "ic")))

    kind = _lookup_comp_kind(tmp_path, "iphone-x", "U_ROOT")

    assert kind == "ic"
