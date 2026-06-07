"""T8 Option C — expand_pack écrit le DELTA dans promoted/ (couche partagée),
avec provenance + owner_ref + sanitisation PII + journal.

On ne teste PAS les appels LLM : on mocke `_run_targeted_scout` (Scout),
`run_registry_builder` (Registry) et `_run_clinicien_on_full_dump` (Clinicien)
exactement comme test_expansion.py. Les objets Registry/RulesSet retournés sont
fixés ; on vérifie la cible d'écriture (promoted/, pas la racine), la provenance,
la sanitisation, le journal et le drop des identifiants invalides.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from api import config as config_mod
from api.pipeline import expansion
from api.pipeline.pack_storage import load_effective_pack, read_journal
from api.pipeline.schemas import (
    Cause,
    DeviceTaxonomy,
    Registry,
    RegistryComponent,
    RegistrySignal,
    Rule,
    RulesSet,
)


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch):
    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    yield
    monkeypatch.setattr(config_mod, "_settings", None)


def _seed_legacy_pack(tmp_path: Path, slug: str) -> Path:
    """Pose un pack LEGACY (fichiers racine) — expand_pack migre vers baseline/
    au premier appel, comme en prod."""
    pack = tmp_path / slug
    pack.mkdir()
    (pack / "raw_research_dump.md").write_text(
        "# Research Dump — test device\n\n## Device overview\ntest\n",
        encoding="utf-8",
    )
    seed_registry = Registry(
        device_label="Test Device",
        taxonomy=DeviceTaxonomy(brand="TestCo", model="Thing"),
        components=[RegistryComponent(canonical_name="U1", kind="IC")],
        signals=[RegistrySignal(canonical_name="VCC", kind="POWER_RAIL")],
    )
    (pack / "registry.json").write_text(seed_registry.model_dump_json(indent=2), encoding="utf-8")
    seed_rules = RulesSet(rules=[
        Rule(
            id="R-EXISTING-001",
            symptoms=["prior symptom"],
            likely_causes=[Cause(refdes="U1", probability=0.5, mechanism="short")],
            confidence=0.6,
        ),
    ])
    (pack / "rules.json").write_text(seed_rules.model_dump_json(indent=2), encoding="utf-8")
    return pack


async def _run_expand(tmp_path, slug, *, registry, rules, owner_ref=None,
                      focus_symptoms=None, scout_chunk=None):
    """Helper : monkeypatch des 3 seams LLM + appel expand_pack."""
    focus_symptoms = focus_symptoms or ["no sound"]

    async def fake_scout(**_kwargs):
        return scout_chunk or "\n\n- **Symptom:** no sound\n  - **mentioned:** U3101\n"

    with patch.object(expansion, "_run_targeted_scout", new=fake_scout), patch(
        "api.pipeline.expansion.run_registry_builder",
        new=AsyncMock(return_value=registry),
    ), patch(
        "api.pipeline.expansion._run_clinicien_on_full_dump",
        new=AsyncMock(return_value=rules),
    ):
        return await expansion.expand_pack(
            device_slug=slug,
            focus_symptoms=focus_symptoms,
            focus_refdes=[],
            client=object(),
            memory_root=tmp_path,
            owner_ref=owner_ref,
        )


def _new_registry_with(components, signals=None):
    return Registry(
        device_label="Test Device",
        taxonomy=DeviceTaxonomy(brand="TestCo", model="Thing"),
        components=components,
        signals=signals or [RegistrySignal(canonical_name="VCC", kind="POWER_RAIL")],
    )


def _existing_plus(extra_rules):
    return RulesSet(rules=[
        Rule(
            id="R-EXISTING-001",
            symptoms=["prior symptom"],
            likely_causes=[Cause(refdes="U1", probability=0.5, mechanism="short")],
            confidence=0.6,
        ),
        *extra_rules,
    ])


# ---------------------------------------------------------------------------


async def test_expand_writes_delta_to_promoted_not_root(tmp_path):
    pack = _seed_legacy_pack(tmp_path, "dev")
    reg = _new_registry_with(
        components=[
            RegistryComponent(canonical_name="U1", kind="IC"),
            RegistryComponent(canonical_name="U3101", kind="IC"),
        ],
        signals=[
            RegistrySignal(canonical_name="VCC", kind="POWER_RAIL"),
            RegistrySignal(canonical_name="VCC_AUDIO", kind="POWER_RAIL"),
        ],
    )
    rules = _existing_plus([
        Rule(id="R-AUDIO-002", symptoms=["no sound"],
             likely_causes=[Cause(refdes="U3101", probability=0.7, mechanism="cold joint")],
             confidence=0.8),
    ])
    await _run_expand(tmp_path, "dev", registry=reg, rules=rules)

    # Le delta est dans promoted/, PAS à la racine.
    promo_reg = json.loads((pack / "promoted" / "registry.json").read_text())
    names = {it["canonical_name"] for it in promo_reg["items"]}
    assert "U3101" in names
    assert "VCC_AUDIO" in names
    # U1/VCC (inchangés vs baseline) ne sont PAS réécrits dans promoted.
    assert "U1" not in names
    assert "VCC" not in names

    promo_rules = json.loads((pack / "promoted" / "rules.json").read_text())
    assert {it["id"] for it in promo_rules["items"]} == {"R-AUDIO-002"}

    # La racine n'est PAS recréée (migration → baseline/).
    assert not (pack / "registry.json").exists()
    assert not (pack / "rules.json").exists()
    # baseline intacte.
    base_reg = json.loads((pack / "baseline" / "registry.json").read_text())
    base_names = {it["canonical_name"] for it in base_reg["items"]}
    assert base_names == {"U1", "VCC"}


async def test_expand_attaches_provenance_with_owner_ref(tmp_path):
    pack = _seed_legacy_pack(tmp_path, "dev")
    reg = _new_registry_with(components=[
        RegistryComponent(canonical_name="U1", kind="IC"),
        RegistryComponent(canonical_name="U3101", kind="IC"),
    ])
    summary = await _run_expand(
        tmp_path, "dev", registry=reg, rules=_existing_plus([]), owner_ref="tenant-A"
    )
    promo = json.loads((pack / "promoted" / "registry.json").read_text())
    fact = next(it for it in promo["items"] if it["canonical_name"] == "U3101")
    prov = fact["_provenance"]
    assert prov["added_by_tenant"] == "tenant-A"
    assert prov["status"] == "promoted"
    assert prov["source_kind"] == "agent_expansion"
    assert prov["expansion_id"].startswith("E-")
    assert prov["expansion_id"] == summary["expansion_id"]


async def test_expand_pii_sanitized_in_promoted(tmp_path):
    """Le critique : une description contenant un email est redactée dans
    promoted ET dans la vue effective de N'IMPORTE QUEL owner."""
    pack = _seed_legacy_pack(tmp_path, "dev")
    reg = _new_registry_with(components=[
        RegistryComponent(canonical_name="U1", kind="IC"),
        RegistryComponent(
            canonical_name="U3101", kind="IC",
            description="audio amp, contact alice@apple.com for docs",
        ),
    ])
    await _run_expand(tmp_path, "dev", registry=reg, rules=_existing_plus([]),
                      owner_ref="tenant-A")

    promo = json.loads((pack / "promoted" / "registry.json").read_text())
    fact = next(it for it in promo["items"] if it["canonical_name"] == "U3101")
    assert "alice@apple.com" not in fact["description"]
    assert "[REDACTED:email]" in fact["description"]
    actions = fact["_provenance"]["sanitizer_actions"]
    assert any(a["action"] == "redacted_email" for a in actions)

    # Aucun tenant ne peut lire la PII via load_effective_pack.
    eff = load_effective_pack(tmp_path, "dev", owner_ref="tenant-B")
    eff_fact = next(
        it for it in eff["registry"]["items"] if it["canonical_name"] == "U3101"
    )
    assert "alice@apple.com" not in json.dumps(eff_fact)


async def test_expand_focus_symptoms_sanitized_in_journal(tmp_path):
    _seed_legacy_pack(tmp_path, "dev")
    reg = _new_registry_with(components=[RegistryComponent(canonical_name="U1", kind="IC")])
    await _run_expand(
        tmp_path, "dev", registry=reg, rules=_existing_plus([]),
        focus_symptoms=["le client Dupont à Lyon dit no charge."],
    )
    entries = [e for e in read_journal(tmp_path, "dev") if e.id != "baseline-pre-T8"]
    assert len(entries) == 1
    joined = " ".join(entries[0].focus_symptoms)
    assert "Dupont" not in joined


async def test_expand_invalid_identifier_dropped_not_crash(tmp_path):
    pack = _seed_legacy_pack(tmp_path, "dev")
    # "main CPU" viole le pattern canonical_name → doit être droppé, pas crash.
    # Pydantic refuse de construire l'objet invalide, donc on bypasse la
    # validation via model_construct (le Registry Builder pourrait sortir ça).
    bad = RegistryComponent.model_construct(
        canonical_name="main CPU", kind="IC", aliases=[], description="bad",
        refdes_candidates=None, logical_alias=None, provenance=None,
    )
    good = RegistryComponent(canonical_name="U1300", kind="IC", description="ok")
    reg = _new_registry_with(components=[
        RegistryComponent(canonical_name="U1", kind="IC"), bad, good,
    ])
    await _run_expand(tmp_path, "dev", registry=reg, rules=_existing_plus([]))

    promo = json.loads((pack / "promoted" / "registry.json").read_text())
    names = {it["canonical_name"] for it in promo["items"]}
    assert "U1300" in names
    assert "main CPU" not in names

    entries = [e for e in read_journal(tmp_path, "dev") if e.id != "baseline-pre-T8"]
    dropped = entries[0].delta_summary.get("dropped", [])
    assert any("main CPU" in json.dumps(d) for d in dropped)


async def test_expand_journal_entry_status_promoted(tmp_path):
    _seed_legacy_pack(tmp_path, "dev")
    reg = _new_registry_with(components=[
        RegistryComponent(canonical_name="U1", kind="IC"),
        RegistryComponent(canonical_name="U3101", kind="IC"),
    ])
    rules = _existing_plus([
        Rule(id="R-AUDIO-002", symptoms=["no sound"],
             likely_causes=[Cause(refdes="U3101", probability=0.7, mechanism="cj")],
             confidence=0.8),
    ])
    await _run_expand(tmp_path, "dev", registry=reg, rules=rules, owner_ref="tenant-A")
    entries = [e for e in read_journal(tmp_path, "dev") if e.id != "baseline-pre-T8"]
    assert len(entries) == 1
    e = entries[0]
    assert e.owner_ref == "tenant-A"
    assert e.status == "promoted"
    assert e.delta_summary["new_components"]   # liste de fact_ids non-vide
    assert e.delta_summary["new_rules"]


async def test_expand_owner_ref_none_self_host(tmp_path):
    pack = _seed_legacy_pack(tmp_path, "dev")
    reg = _new_registry_with(components=[
        RegistryComponent(canonical_name="U1", kind="IC"),
        RegistryComponent(canonical_name="U3101", kind="IC"),
    ])
    await _run_expand(tmp_path, "dev", registry=reg, rules=_existing_plus([]),
                      owner_ref=None)
    promo = json.loads((pack / "promoted" / "registry.json").read_text())
    fact = next(it for it in promo["items"] if it["canonical_name"] == "U3101")
    assert fact["_provenance"]["added_by_tenant"] is None
    assert fact["_provenance"]["status"] == "promoted"
