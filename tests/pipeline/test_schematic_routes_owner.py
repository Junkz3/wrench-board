"""T9 — les routes HTTP du graphe schématique résolvent per-owner via X-Owner-Ref.

Le cloud proxy injecte `X-Owner-Ref` (= tenant_id) sur les lectures proxifiées.
Deux tenants épinglés sur deux PDF distincts (hA / hB) du MÊME slug doivent lire
deux graphes distincts ; absence d'en-tête (self-host) lit la racine du slug.

Mirror du patron de tests dans `test_schematic_api.py` (fixtures memory_root +
client de conftest).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api import config as config_mod
from api.pipeline import live_graph, sources


@pytest.fixture
def memory_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    yield tmp_path
    monkeypatch.setattr(config_mod, "_settings", None)


def _seed_cache(pack: Path, pdf_hash: str, device_slug: str, rail_label: str,
                comp_refdes: str | None = None) -> None:
    """Seed a hashed cache dir with a schema-complete electrical graph.

    Optionally embeds one IC component `comp_refdes` so callers can assert which
    graph a per-owner reader actually loaded (a refdes present in graph A but not
    graph/root B is `known` only if the right graph was read)."""
    cdir = sources.cache_dir_for(pack, pdf_hash)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "schematic.pdf").write_bytes(b"%PDF")
    (cdir / "schematic_graph.json").write_text("{}")
    components: dict = {}
    if comp_refdes is not None:
        components[comp_refdes] = {
            "refdes": comp_refdes,
            "type": "ic",
            "kind": "ic",
            "pages": [],
            "pins": [],
        }
    graph = {
        "schema_version": "1.0",
        "device_slug": device_slug,
        "components": components,
        "nets": {},
        "power_rails": {
            rail_label: {
                "label": rail_label,
                "voltage_nominal": 5.0,
                "source_refdes": "U7",
                "source_type": "buck",
                "enable_net": None,
                "consumers": [],
                "decoupling": [],
            }
        },
        "typed_edges": [],
        "boot_sequence": [],
        "quality": {"total_pages": 1, "pages_parsed": 1},
    }
    (cdir / "electrical_graph.json").write_text(json.dumps(graph))


def test_get_schematic_resolves_per_owner(memory_root, client):
    slug = "iphone-x"
    pack = memory_root / slug
    pack.mkdir()
    _seed_cache(pack, "hA", slug, "+5V_A")
    _seed_cache(pack, "hB", slug, "+5V_B")
    live_graph.write_owner_active(pack, "tenant-A", "schematic_pdf", "a.pdf", "hA")
    live_graph.write_owner_active(pack, "tenant-B", "schematic_pdf", "b.pdf", "hB")

    ra = client.get(f"/pipeline/packs/{slug}/schematic", headers={"X-Owner-Ref": "tenant-A"})
    rb = client.get(f"/pipeline/packs/{slug}/schematic", headers={"X-Owner-Ref": "tenant-B"})

    assert ra.status_code == 200, ra.text
    assert rb.status_code == 200, rb.text
    assert list(ra.json()["power_rails"].keys()) == ["+5V_A"]
    assert list(rb.json()["power_rails"].keys()) == ["+5V_B"]


def test_get_schematic_managed_no_pin_falls_back_to_canonical(memory_root, client):
    """Tenant managé SANS pin → lit le graphe CANONIQUE partagé du slug (racine
    owner=None) — le moat (T6). Le graphe analysé est de la connaissance device
    PARTAGÉE : un tenant peut diaguer une carte connue sans uploader son PDF.
    (Un uploader, lui, a son graphe per-owner — cf. test_resolves_per_owner.)"""
    slug = "iphone-x"
    pack = memory_root / slug
    pack.mkdir()
    # Racine = le graphe canonique partagé (rail distinctif +CANON).
    (pack / "electrical_graph.json").write_text(json.dumps({
        "schema_version": "1.0", "device_slug": slug, "components": {}, "nets": {},
        "power_rails": {"+CANON": {"label": "+CANON", "voltage_nominal": 3.3,
                                   "source_refdes": "U1", "source_type": "ldo",
                                   "enable_net": None, "consumers": [], "decoupling": []}},
        "typed_edges": [], "boot_sequence": [],
    }))

    r = client.get(f"/pipeline/packs/{slug}/schematic", headers={"X-Owner-Ref": "tenant-Z"})
    assert r.status_code == 200, r.text
    assert list(r.json()["power_rails"].keys()) == ["+CANON"]


def test_get_schematic_managed_no_canonical_404(memory_root, client):
    """Pas de pin ET pas de graphe canonique → 404 (le moat n'existe pas encore)."""
    slug = "iphone-x"
    pack = memory_root / slug
    pack.mkdir()
    r = client.get(f"/pipeline/packs/{slug}/schematic", headers={"X-Owner-Ref": "tenant-Z"})
    assert r.status_code == 404, r.text


def test_get_schematic_pages_managed_no_pin_stays_404(memory_root, client):
    """Les PAGES PNG (rendu du PDF brut) restent PRIVÉES : un non-uploader → 404,
    MÊME si la racine a des pages canoniques (contrairement au graphe-données)."""
    slug = "iphone-x"
    pack = memory_root / slug
    pack.mkdir()
    _seed_cache(pack, "hCanon", slug, "+CANON")
    # Pages canoniques à la racine (rendu) — ne doivent PAS fuiter au non-uploader.
    pages = pack / "schematic_pages"
    pages.mkdir()
    (pages / "page-001.png").write_bytes(b"\x89PNG canonical")

    r = client.get(f"/pipeline/packs/{slug}/schematic/pages", headers={"X-Owner-Ref": "tenant-Z"})
    assert r.status_code == 404, r.text


def test_get_schematic_self_host_reads_root(memory_root, client):
    """Pas d'en-tête (self-host) → racine du slug, inchangé."""
    slug = "iphone-x"
    pack = memory_root / slug
    pack.mkdir()
    root_graph = {
        "schema_version": "1.0",
        "device_slug": slug,
        "components": {},
        "nets": {},
        "power_rails": {"+ROOT": {"label": "+ROOT", "voltage_nominal": 1.0,
                                  "source_refdes": "U1", "source_type": "ldo",
                                  "enable_net": None, "consumers": [], "decoupling": []}},
        "typed_edges": [],
        "boot_sequence": [],
    }
    (pack / "electrical_graph.json").write_text(json.dumps(root_graph))

    r = client.get(f"/pipeline/packs/{slug}/schematic")
    assert r.status_code == 200, r.text
    assert list(r.json()["power_rails"].keys()) == ["+ROOT"]


# --- hypothesize (T9 residual reader) ---------------------------------------
#
# `mb_hypothesize` validates the caller's `state_comps` refdes against the
# electrical graph it loads. Seed graph A with IC `U_A` and graph B with `U_B`
# on the SAME slug: a per-owner read makes `U_A` known for tenant-A only. If the
# reader fell back to the slug ROOT (the bug), `U_A` would be unknown → the call
# would 400 / report `unknown_refdes`.


def test_hypothesize_http_route_resolves_per_owner(memory_root, client):
    slug = "iphone-x"
    pack = memory_root / slug
    pack.mkdir()
    _seed_cache(pack, "hA", slug, "+5V_A", comp_refdes="U_A")
    _seed_cache(pack, "hB", slug, "+5V_B", comp_refdes="U_B")
    live_graph.write_owner_active(pack, "tenant-A", "schematic_pdf", "a.pdf", "hA")
    live_graph.write_owner_active(pack, "tenant-B", "schematic_pdf", "b.pdf", "hB")

    # tenant-A names U_A (present only in graph A) → accepted (graph A loaded).
    ra = client.post(
        f"/pipeline/packs/{slug}/schematic/hypothesize",
        json={"state_comps": {"U_A": "dead"}},
        headers={"X-Owner-Ref": "tenant-A"},
    )
    assert ra.status_code == 200, ra.text

    # tenant-A naming U_B (graph B's IC) → unknown for tenant-A → 400.
    ra_wrong = client.post(
        f"/pipeline/packs/{slug}/schematic/hypothesize",
        json={"state_comps": {"U_B": "dead"}},
        headers={"X-Owner-Ref": "tenant-A"},
    )
    assert ra_wrong.status_code == 400, ra_wrong.text

    # tenant-B mirror: U_B known, U_A unknown.
    rb = client.post(
        f"/pipeline/packs/{slug}/schematic/hypothesize",
        json={"state_comps": {"U_B": "dead"}},
        headers={"X-Owner-Ref": "tenant-B"},
    )
    assert rb.status_code == 200, rb.text
    rb_wrong = client.post(
        f"/pipeline/packs/{slug}/schematic/hypothesize",
        json={"state_comps": {"U_A": "dead"}},
        headers={"X-Owner-Ref": "tenant-B"},
    )
    assert rb_wrong.status_code == 400, rb_wrong.text


def test_hypothesize_http_managed_no_pin_uses_canonical(memory_root, client):
    """Managed tenant sans pin → hypothesize tourne sur le graphe CANONIQUE partagé
    (le moat) : U_ROOT y est connu → 200. (Avant T6-graph-shared : 404.)"""
    slug = "iphone-x"
    pack = memory_root / slug
    pack.mkdir()
    # Racine = graphe canonique partagé contenant U_ROOT.
    (pack / "electrical_graph.json").write_text(json.dumps({
        "schema_version": "1.0", "device_slug": slug,
        "components": {"U_ROOT": {"refdes": "U_ROOT", "type": "ic", "kind": "ic",
                                  "pages": [], "pins": []}},
        "nets": {}, "power_rails": {}, "typed_edges": [], "boot_sequence": [],
        "quality": {"total_pages": 1, "pages_parsed": 1},
    }))

    r = client.post(
        f"/pipeline/packs/{slug}/schematic/hypothesize",
        json={"state_comps": {"U_ROOT": "dead"}},
        headers={"X-Owner-Ref": "tenant-Z"},
    )
    assert r.status_code == 200, r.text


def test_hypothesize_tool_resolves_per_owner_via_contextvar(memory_root):
    """Agent-tool path: owner comes from the ContextVar (current_owner_ref)."""
    from api.agent.owner_ref import set_owner_ref
    from api.tools.hypothesize import mb_hypothesize

    slug = "iphone-x"
    pack = memory_root / slug
    pack.mkdir()
    _seed_cache(pack, "hA", slug, "+5V_A", comp_refdes="U_A")
    _seed_cache(pack, "hB", slug, "+5V_B", comp_refdes="U_B")
    live_graph.write_owner_active(pack, "tenant-A", "schematic_pdf", "a.pdf", "hA")
    live_graph.write_owner_active(pack, "tenant-B", "schematic_pdf", "b.pdf", "hB")

    try:
        set_owner_ref("tenant-A")
        ok = mb_hypothesize(device_slug=slug, memory_root=memory_root,
                            state_comps={"U_A": "dead"})
        assert ok.get("found") is True, ok
        wrong = mb_hypothesize(device_slug=slug, memory_root=memory_root,
                               state_comps={"U_B": "dead"})
        assert wrong.get("found") is False
        assert wrong.get("reason") == "unknown_refdes", wrong
    finally:
        set_owner_ref(None)


def test_hypothesize_self_host_reads_root(memory_root):
    """No owner (self-host) → slug root graph, unchanged."""
    from api.agent.owner_ref import set_owner_ref
    from api.tools.hypothesize import mb_hypothesize

    slug = "iphone-x"
    pack = memory_root / slug
    pack.mkdir()
    (pack / "electrical_graph.json").write_text(json.dumps({
        "schema_version": "1.0", "device_slug": slug,
        "components": {"U_ROOT": {"refdes": "U_ROOT", "type": "ic", "kind": "ic",
                                  "pages": [], "pins": []}},
        "nets": {}, "power_rails": {}, "typed_edges": [], "boot_sequence": [],
        "quality": {"total_pages": 1, "pages_parsed": 1},
    }))

    set_owner_ref(None)
    res = mb_hypothesize(device_slug=slug, memory_root=memory_root,
                         state_comps={"U_ROOT": "dead"})
    assert res.get("found") is True, res
