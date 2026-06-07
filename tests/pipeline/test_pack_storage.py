"""T8 — pack_storage : I/O sur la nouvelle topologie disque.

Pas de LLM, pas d'agent. Toute la machinerie load_effective_pack + write_staged
+ append_journal + write_promoted + revoke est testée ici avec tmp_path.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from api.pipeline.pack_storage import (
    JournalEntry,
    append_journal,
    init_pack_layout,
    load_effective_pack,
    read_journal,
    revoke_expansion,
    revoke_fact,
    write_promoted,
    write_promoted_facts,
    write_staged,
)
from api.pipeline.schemas import Provenance, RegistryComponent

SLUG = "iphone-12-test"


@pytest.fixture
def memory_root(tmp_path: Path) -> Path:
    """Racine memory/ vide pour chaque test."""
    return tmp_path


@pytest.fixture
def pack_dir(memory_root: Path) -> Path:
    """Pack initialisé avec la nouvelle topologie T8."""
    init_pack_layout(memory_root, SLUG)
    return memory_root / SLUG


def _make_provenance(expansion_id="E-test", owner="t1", status="staged") -> Provenance:
    return Provenance(
        expansion_id=expansion_id,
        added_at=datetime.now(UTC),
        added_by_tenant=owner,
        confidence=0.5,
        source_kind="agent_expansion",
        sanitizer_actions=[],
        status=status,
    )


# ---- Layout initialization ---------------------------------------------


def test_init_pack_layout_creates_directories(memory_root: Path):
    init_pack_layout(memory_root, SLUG)
    for sub in ("baseline", "promoted", "_staged", "expansions", "audit"):
        assert (memory_root / SLUG / sub).is_dir()


def test_init_pack_layout_idempotent(memory_root: Path):
    init_pack_layout(memory_root, SLUG)
    init_pack_layout(memory_root, SLUG)
    assert (memory_root / SLUG / "baseline").is_dir()


# ---- Write staged ------------------------------------------------------


def test_write_staged_creates_owner_dir_and_file(pack_dir: Path, memory_root: Path):
    comp = RegistryComponent(canonical_name="U1300", kind="IC", aliases=[], provenance=_make_provenance())
    write_staged(memory_root, SLUG, owner_ref="t1", file_name="registry.json", new_facts=[comp])

    staged_file = memory_root / SLUG / "_staged" / "t1" / "registry.json"
    assert staged_file.is_file()
    data = json.loads(staged_file.read_text())
    assert "items" in data
    assert any(item["canonical_name"] == "U1300" for item in data["items"])


def test_write_staged_atomic_via_rename(memory_root: Path):
    init_pack_layout(memory_root, SLUG)
    comp = RegistryComponent(canonical_name="X1", kind="IC", aliases=[], provenance=_make_provenance())
    write_staged(memory_root, SLUG, owner_ref="t1", file_name="registry.json", new_facts=[comp])
    staged_dir = memory_root / SLUG / "_staged" / "t1"
    assert not any(p.suffix == ".tmp" for p in staged_dir.iterdir())


def test_write_staged_merges_with_existing(pack_dir: Path, memory_root: Path):
    c1 = RegistryComponent(canonical_name="U1300", kind="IC", aliases=[], provenance=_make_provenance())
    c2 = RegistryComponent(canonical_name="Q9100", kind="MOSFET", aliases=[], provenance=_make_provenance())
    write_staged(memory_root, SLUG, owner_ref="t1", file_name="registry.json", new_facts=[c1])
    write_staged(memory_root, SLUG, owner_ref="t1", file_name="registry.json", new_facts=[c2])
    data = json.loads((memory_root / SLUG / "_staged" / "t1" / "registry.json").read_text())
    names = {item["canonical_name"] for item in data["items"]}
    assert names == {"U1300", "Q9100"}


# ---- Effective pack merging (baseline + promoted + staged) ------------


def test_load_effective_pack_baseline_only(pack_dir: Path, memory_root: Path):
    baseline = {
        "items": [
            {
                "canonical_name": "U1300",
                "kind": "IC",
                "aliases": [],
                "_provenance": {
                    "expansion_id": "baseline-pre-T8",
                    "added_at": "2025-01-01T00:00:00Z",
                    "added_by_tenant": None,
                    "confidence": 1.0,
                    "source_kind": "baseline",
                    "sanitizer_actions": [],
                    "status": "baseline",
                },
            }
        ]
    }
    (memory_root / SLUG / "baseline" / "registry.json").write_text(json.dumps(baseline))
    eff = load_effective_pack(memory_root, SLUG, owner_ref="t1")
    assert len(eff["registry"]["items"]) == 1
    assert eff["registry"]["items"][0]["canonical_name"] == "U1300"


def test_load_effective_pack_staged_overrides_promoted_overrides_baseline(memory_root: Path):
    init_pack_layout(memory_root, SLUG)
    pack = memory_root / SLUG

    def _fact(name, layer):
        return {
            "canonical_name": name,
            "kind": "IC",
            "aliases": [],
            "_provenance": {
                "expansion_id": f"E-{layer}",
                "added_at": "2025-01-01T00:00:00Z",
                "added_by_tenant": "t1" if layer == "staged" else None,
                "confidence": 0.5,
                "source_kind": "agent_expansion" if layer != "baseline" else "baseline",
                "sanitizer_actions": [],
                "status": layer if layer in ("staged", "promoted", "baseline") else "staged",
            },
        }

    (pack / "baseline" / "registry.json").write_text(
        json.dumps({"items": [_fact("U1300", "baseline")]})
    )
    (pack / "promoted" / "registry.json").write_text(
        json.dumps({"items": [_fact("U1300", "promoted")]})
    )
    (pack / "_staged" / "t1").mkdir(parents=True)
    (pack / "_staged" / "t1" / "registry.json").write_text(
        json.dumps({"items": [_fact("U1300", "staged")]})
    )

    eff = load_effective_pack(memory_root, SLUG, owner_ref="t1")
    items = eff["registry"]["items"]
    assert len(items) == 1
    assert items[0]["_provenance"]["expansion_id"] == "E-staged"


def test_load_effective_pack_isolation_between_tenants(memory_root: Path):
    init_pack_layout(memory_root, SLUG)
    pack = memory_root / SLUG
    (pack / "_staged" / "t2").mkdir(parents=True)
    (pack / "_staged" / "t2" / "registry.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "canonical_name": "SECRET_COMP",
                        "kind": "IC",
                        "aliases": [],
                        "_provenance": {
                            "expansion_id": "E-t2",
                            "added_at": "2025-01-01T00:00:00Z",
                            "added_by_tenant": "t2",
                            "confidence": 0.5,
                            "source_kind": "agent_expansion",
                            "sanitizer_actions": [],
                            "status": "staged",
                        },
                    }
                ]
            }
        )
    )
    eff = load_effective_pack(memory_root, SLUG, owner_ref="t1")
    names = {it["canonical_name"] for it in eff["registry"]["items"]}
    assert "SECRET_COMP" not in names


# ---- Journal -----------------------------------------------------------


def test_append_and_read_journal(memory_root: Path):
    init_pack_layout(memory_root, SLUG)
    entry = JournalEntry(
        id="E-abc",
        ts=datetime.now(UTC),
        owner_ref="t1",
        slug=SLUG,
        focus_symptoms=["no charge"],
        focus_refdes=[],
        delta_summary={"new_components": ["F-cmp-001"], "new_rules": []},
        scout_dump_range={"start": 0, "end": 1024},
        status="staged",
    )
    append_journal(memory_root, SLUG, entry)
    journal = list(read_journal(memory_root, SLUG))
    assert len(journal) == 1
    assert journal[0].id == "E-abc"


def test_journal_is_append_only_in_normal_writes(memory_root: Path):
    init_pack_layout(memory_root, SLUG)
    for i in range(2):
        append_journal(
            memory_root,
            SLUG,
            JournalEntry(
                id=f"E-{i}",
                ts=datetime.now(UTC),
                owner_ref="t1",
                slug=SLUG,
                focus_symptoms=[],
                focus_refdes=[],
                delta_summary={"new_components": [], "new_rules": []},
                scout_dump_range={"start": 0, "end": 0},
                status="staged",
            ),
        )
    raw = (memory_root / SLUG / "expansions" / "expansions.jsonl").read_text()
    assert raw.count("\n") == 2


# ---- Promote -----------------------------------------------------------


def test_write_promoted_moves_fact_from_staged_journal_updated(memory_root: Path):
    init_pack_layout(memory_root, SLUG)
    comp = RegistryComponent(canonical_name="U1300", kind="IC", aliases=[], provenance=_make_provenance(expansion_id="E-promo"))
    write_staged(memory_root, SLUG, owner_ref="t1", file_name="registry.json", new_facts=[comp])
    append_journal(
        memory_root,
        SLUG,
        JournalEntry(
            id="E-promo", ts=datetime.now(UTC), owner_ref="t1", slug=SLUG,
            focus_symptoms=[], focus_refdes=[],
            delta_summary={"new_components": ["F-cmp-001"], "new_rules": []},
            scout_dump_range={"start": 0, "end": 0}, status="staged",
        ),
    )
    write_promoted(memory_root, SLUG, expansion_id="E-promo")

    promoted_file = memory_root / SLUG / "promoted" / "registry.json"
    assert promoted_file.is_file()
    data = json.loads(promoted_file.read_text())
    assert any(item["canonical_name"] == "U1300" for item in data["items"])

    journal = list(read_journal(memory_root, SLUG))
    promo_entry = next(e for e in journal if e.id == "E-promo")
    assert promo_entry.status == "promoted"
    assert promo_entry.promoted_at is not None


def test_write_promoted_idempotent(memory_root: Path):
    init_pack_layout(memory_root, SLUG)
    comp = RegistryComponent(canonical_name="U1", kind="IC", aliases=[], provenance=_make_provenance(expansion_id="E-x"))
    write_staged(memory_root, SLUG, owner_ref="t1", file_name="registry.json", new_facts=[comp])
    append_journal(
        memory_root, SLUG,
        JournalEntry(
            id="E-x", ts=datetime.now(UTC), owner_ref="t1", slug=SLUG,
            focus_symptoms=[], focus_refdes=[],
            delta_summary={"new_components": ["F-cmp-001"], "new_rules": []},
            scout_dump_range={"start": 0, "end": 0}, status="staged",
        ),
    )
    write_promoted(memory_root, SLUG, expansion_id="E-x")
    write_promoted(memory_root, SLUG, expansion_id="E-x")
    promo_file = memory_root / SLUG / "promoted" / "registry.json"
    data = json.loads(promo_file.read_text())
    assert sum(1 for it in data["items"] if it["canonical_name"] == "U1") == 1


# ---- Revoke ------------------------------------------------------------


def test_revoke_expansion_removes_from_staged_and_promoted(memory_root: Path):
    init_pack_layout(memory_root, SLUG)
    comp = RegistryComponent(canonical_name="U1300", kind="IC", aliases=[], provenance=_make_provenance(expansion_id="E-rev"))
    write_staged(memory_root, SLUG, owner_ref="t1", file_name="registry.json", new_facts=[comp])
    append_journal(
        memory_root, SLUG,
        JournalEntry(
            id="E-rev", ts=datetime.now(UTC), owner_ref="t1", slug=SLUG,
            focus_symptoms=[], focus_refdes=[],
            delta_summary={"new_components": ["F-cmp-001"], "new_rules": []},
            scout_dump_range={"start": 0, "end": 0}, status="staged",
        ),
    )
    write_promoted(memory_root, SLUG, expansion_id="E-rev")
    revoke_expansion(memory_root, SLUG, expansion_id="E-rev", reason="halluciné")

    promo_file = memory_root / SLUG / "promoted" / "registry.json"
    data = json.loads(promo_file.read_text())
    assert not any(it["canonical_name"] == "U1300" for it in data["items"])

    staged_file = memory_root / SLUG / "_staged" / "t1" / "registry.json"
    data = json.loads(staged_file.read_text())
    assert not any(it["canonical_name"] == "U1300" for it in data["items"])

    journal = list(read_journal(memory_root, SLUG))
    rev_entry = next(e for e in journal if e.id == "E-rev")
    assert rev_entry.status == "revoked"
    assert rev_entry.revoked_reason == "halluciné"


def test_revoke_baseline_refused(memory_root: Path):
    init_pack_layout(memory_root, SLUG)
    append_journal(
        memory_root, SLUG,
        JournalEntry(
            id="baseline-pre-T8", ts=datetime.now(UTC), owner_ref=None, slug=SLUG,
            focus_symptoms=[], focus_refdes=[],
            delta_summary={}, scout_dump_range={"start": 0, "end": 0}, status="baseline",
        ),
    )
    with pytest.raises(ValueError, match="baseline"):
        revoke_expansion(memory_root, SLUG, expansion_id="baseline-pre-T8", reason="test")


def test_revoke_fact_removes_single_fact(memory_root: Path):
    """revoke_fact retire UN fact par son fact_id, laisse les autres."""
    init_pack_layout(memory_root, SLUG)
    c1 = RegistryComponent(canonical_name="U1300", kind="IC", aliases=[], provenance=_make_provenance(expansion_id="E-a"))
    c2 = RegistryComponent(canonical_name="Q9100", kind="MOSFET", aliases=[], provenance=_make_provenance(expansion_id="E-a"))
    write_staged(memory_root, SLUG, owner_ref="t1", file_name="registry.json", new_facts=[c1, c2])

    # Dérive le fact_id de U1300 de la même façon que pack_storage le fait.
    from api.pipeline.pack_storage import _derive_fact_id
    fid_u1300 = _derive_fact_id({"canonical_name": "U1300", "kind": "IC"})

    revoke_fact(memory_root, SLUG, fact_id=fid_u1300, reason="bad")

    data = json.loads((memory_root / SLUG / "_staged" / "t1" / "registry.json").read_text())
    names = {it["canonical_name"] for it in data["items"]}
    assert "U1300" not in names
    assert "Q9100" in names


def test_revoke_expansion_idempotent(memory_root: Path):
    """Révoquer deux fois une expansion = no-op la 2e fois."""
    init_pack_layout(memory_root, SLUG)
    comp = RegistryComponent(canonical_name="U1300", kind="IC", aliases=[], provenance=_make_provenance(expansion_id="E-rev2"))
    write_staged(memory_root, SLUG, owner_ref="t1", file_name="registry.json", new_facts=[comp])
    append_journal(memory_root, SLUG, JournalEntry(
        id="E-rev2", ts=datetime.now(UTC), owner_ref="t1", slug=SLUG,
        focus_symptoms=[], focus_refdes=[],
        delta_summary={"new_components": ["F-cmp-001"], "new_rules": []},
        scout_dump_range={"start": 0, "end": 0}, status="staged",
    ))
    revoke_expansion(memory_root, SLUG, expansion_id="E-rev2", reason="first")
    # 2e appel ne doit pas crasher
    revoke_expansion(memory_root, SLUG, expansion_id="E-rev2", reason="second")
    journal = list(read_journal(memory_root, SLUG))
    rev = next(e for e in journal if e.id == "E-rev2")
    assert rev.status == "revoked"


def test_write_promoted_raises_on_revoked(memory_root: Path):
    """Promouvoir une expansion révoquée lève ValueError."""
    init_pack_layout(memory_root, SLUG)
    comp = RegistryComponent(canonical_name="U1300", kind="IC", aliases=[], provenance=_make_provenance(expansion_id="E-rr"))
    write_staged(memory_root, SLUG, owner_ref="t1", file_name="registry.json", new_facts=[comp])
    append_journal(memory_root, SLUG, JournalEntry(
        id="E-rr", ts=datetime.now(UTC), owner_ref="t1", slug=SLUG,
        focus_symptoms=[], focus_refdes=[],
        delta_summary={"new_components": ["F-cmp-001"], "new_rules": []},
        scout_dump_range={"start": 0, "end": 0}, status="staged",
    ))
    revoke_expansion(memory_root, SLUG, expansion_id="E-rr", reason="x")
    with pytest.raises(ValueError, match="révoquée|revoked"):
        write_promoted(memory_root, SLUG, expansion_id="E-rr")


# ---- write_promoted_facts (Option C — écriture directe dans promoted/) ----


def test_write_promoted_facts_merges_by_key(memory_root: Path, pack_dir: Path):
    """Un fact nouveau s'ajoute ; un fact de même clé canonique écrase l'ancien."""
    # Seed promoted/ avec U1300 (description "old").
    c1 = RegistryComponent(
        canonical_name="U1300", kind="IC", description="old",
        provenance=_make_provenance(expansion_id="E-1", status="promoted"),
    )
    write_promoted_facts(
        memory_root, SLUG, file_name="registry.json", new_facts=[c1]
    )
    promo = json.loads((pack_dir / "promoted" / "registry.json").read_text())
    assert len(promo["items"]) == 1
    assert promo["items"][0]["description"] == "old"

    # Override U1300 (même clé) + ajout U1400 (nouveau).
    c1b = RegistryComponent(
        canonical_name="U1300", kind="IC", description="new",
        provenance=_make_provenance(expansion_id="E-2", status="promoted"),
    )
    c2 = RegistryComponent(
        canonical_name="U1400", kind="IC", description="brand new",
        provenance=_make_provenance(expansion_id="E-2", status="promoted"),
    )
    write_promoted_facts(
        memory_root, SLUG, file_name="registry.json", new_facts=[c1b, c2]
    )
    promo = json.loads((pack_dir / "promoted" / "registry.json").read_text())
    by_name = {it["canonical_name"]: it for it in promo["items"]}
    assert set(by_name) == {"U1300", "U1400"}
    assert by_name["U1300"]["description"] == "new"        # écrasé
    assert by_name["U1300"]["_provenance"]["expansion_id"] == "E-2"
    assert by_name["U1400"]["description"] == "brand new"  # ajouté


def test_write_promoted_facts_accepts_dicts(memory_root: Path, pack_dir: Path):
    """Accepte aussi des dicts déjà sérialisés (pas seulement des BaseModel)."""
    fact = {
        "canonical_name": "U2000", "kind": "IC", "aliases": [], "description": "d",
        "_provenance": _make_provenance(status="promoted").model_dump(mode="json"),
    }
    write_promoted_facts(
        memory_root, SLUG, file_name="registry.json", new_facts=[fact]
    )
    promo = json.loads((pack_dir / "promoted" / "registry.json").read_text())
    assert promo["items"][0]["canonical_name"] == "U2000"
