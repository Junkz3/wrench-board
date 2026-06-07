"""T8 — schémas augmentés : Provenance, WithProvenance mixin, identifiants stricts.

Ce module ne teste QUE les changements de schéma (Pydantic) : aucune I/O, aucun
pipeline. La vraie intégration est testée plus tard (test_expansion_t8.py).
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from api.pipeline.schemas import (
    Cause,
    KnowledgeEdge,
    KnowledgeNode,
    Provenance,
    RegistryComponent,
    RegistrySignal,
    Rule,
    SanitizerAction,
)

# ---- Provenance --------------------------------------------------------


def test_provenance_minimal_valid():
    """Provenance complète avec tous les champs nominaux."""
    p = Provenance(
        expansion_id="E-12345678",
        added_at=datetime.now(UTC),
        added_by_tenant="tenant-uuid-abc",
        confidence=0.7,
        source_kind="agent_expansion",
        sanitizer_actions=[],
        status="staged",
    )
    assert p.expansion_id == "E-12345678"
    assert p.status == "staged"


def test_provenance_baseline_synthetique():
    """Cas spécial : baseline-pre-T8 avec added_by_tenant=None."""
    p = Provenance(
        expansion_id="baseline-pre-T8",
        added_at=datetime.now(UTC),
        added_by_tenant=None,
        confidence=1.0,
        source_kind="baseline",
        sanitizer_actions=[],
        status="baseline",
    )
    assert p.added_by_tenant is None
    assert p.source_kind == "baseline"


def test_provenance_status_invalid_rejected():
    with pytest.raises(ValidationError):
        Provenance(
            expansion_id="E-1",
            added_at=datetime.now(UTC),
            added_by_tenant=None,
            confidence=0.5,
            source_kind="agent_expansion",
            sanitizer_actions=[],
            status="bogus_status",
        )


# ---- Identifiants stricts (mode strict, écritures nouvelles) -----------


def test_registry_component_canonical_name_strict_pattern():
    """canonical_name doit matcher [A-Z0-9_./-]{2,64}."""
    c = RegistryComponent(canonical_name="U1300", kind="IC", aliases=[])
    assert c.canonical_name == "U1300"

    with pytest.raises(ValidationError):
        RegistryComponent(canonical_name="main CPU", kind="IC", aliases=[])

    with pytest.raises(ValidationError):
        RegistryComponent(canonical_name="cpu", kind="IC", aliases=[])


def test_registry_component_kind_enum():
    """kind est un Literal[...] ; tout autre valeur est rejetée."""
    RegistryComponent(canonical_name="U1300", kind="PMIC", aliases=[])
    # Types restaurés en T8 (FUSE/SWITCH/CRYSTAL/COIL) — doivent être acceptés
    RegistryComponent(canonical_name="F1300", kind="FUSE", aliases=[])
    with pytest.raises(ValidationError):
        RegistryComponent(canonical_name="U1300", kind="processeur_principal", aliases=[])
    # Minuscule rejeté — le schéma T8 est strict UPPERCASE
    with pytest.raises(ValidationError):
        RegistryComponent(canonical_name="F1300", kind="fuse", aliases=[])


def test_registry_signal_pattern_and_enum():
    RegistrySignal(canonical_name="PP3V0_TRISTAR", kind="POWER_RAIL")
    with pytest.raises(ValidationError):
        RegistrySignal(canonical_name="alim 3v3", kind="POWER_RAIL")
    with pytest.raises(ValidationError):
        RegistrySignal(canonical_name="PP3V0_TRISTAR", kind="autre_chose")


def test_rule_id_pattern():
    _cause = Cause(refdes="U1", probability=0.8, mechanism="short-to-ground")
    Rule(id="R-NO_CHARGE_001", symptoms=["no charge"], likely_causes=[_cause], diagnostic_steps=[])
    with pytest.raises(ValidationError):
        Rule(id="règle 1", symptoms=["s"], likely_causes=[_cause], diagnostic_steps=[])


def test_knowledge_node_id_and_kind():
    KnowledgeNode(id="N-CPU_MAIN", kind="component", label="Main CPU", properties={})
    with pytest.raises(ValidationError):
        KnowledgeNode(id="cpu", kind="component", label="x", properties={})
    with pytest.raises(ValidationError):
        KnowledgeNode(id="N-X", kind="cpu_thing", label="x", properties={})


def test_knowledge_edge_relation_enum():
    """Toutes les nouvelles relations T8 sont acceptées ; les anciennes sont rejetées."""
    # Smoke test : chaque relation du nouveau vocabulaire doit passer
    for rel in ("powers", "drives", "senses", "grounds", "shares_net", "caused_by", "indicates"):
        KnowledgeEdge(source_id="N-A", target_id="N-B", relation=rel)

    # Valeur inconnue rejetée
    with pytest.raises(ValidationError):
        KnowledgeEdge(source_id="N-A", target_id="N-B", relation="alimente")

    # Anciennes relations supprimées en T8 — doivent maintenant être rejetées
    for old_rel in ("causes", "decouples", "connects", "measured_at", "part_of"):
        with pytest.raises(ValidationError):
            KnowledgeEdge(source_id="N-A", target_id="N-B", relation=old_rel)


# ---- Mixin WithProvenance ----------------------------------------------


def test_component_carries_provenance():
    """RegistryComponent peut transporter un objet Provenance via _provenance."""
    prov = Provenance(
        expansion_id="E-abc",
        added_at=datetime.now(UTC),
        added_by_tenant="t1",
        confidence=0.5,
        source_kind="agent_expansion",
        sanitizer_actions=[],
        status="staged",
    )
    c = RegistryComponent(
        canonical_name="U1300",
        kind="IC",
        aliases=[],
        provenance=prov,
    )
    assert c.provenance.expansion_id == "E-abc"


def test_component_without_provenance_still_valid():
    """Rétro-compat : un composant sans provenance reste lisible."""
    c = RegistryComponent(canonical_name="U1300", kind="IC", aliases=[])
    assert c.provenance is None


# ---- Mode tolérant (lecture baseline migrée) ---------------------------


def test_component_tolerant_mode_allows_legacy_identifiers():
    """Quand on lit un fact issu de la baseline migrée, les patterns stricts
    ne s'appliquent PAS — un canonical_name legacy avec espace doit pouvoir
    être chargé. Activé via Provenance.source_kind == 'baseline'."""
    from api.pipeline.schemas import load_with_tolerant_baseline

    raw = {
        "canonical_name": "main CPU",
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
    c = load_with_tolerant_baseline(RegistryComponent, raw)
    assert c.canonical_name == "main CPU"
    assert c.provenance.source_kind == "baseline"


def test_load_with_tolerant_baseline_does_not_enforce_type():
    """Documente explicitement : load_with_tolerant_baseline bypass tout validation."""
    from api.pipeline.schemas import RegistryComponent, load_with_tolerant_baseline
    raw = {
        "canonical_name": 42,  # type INCORRECT (int au lieu de str) — bypass
        "kind": "invalid_kind",  # n'existe pas dans le Literal — bypass
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
    # Ne raise PAS — c'est volontaire (cf. docstring)
    c = load_with_tolerant_baseline(RegistryComponent, raw)
    assert c.canonical_name == 42  # type pas coercé
    assert c.kind == "invalid_kind"  # Literal pas appliqué


# ---- SanitizerAction ---------------------------------------------------


def test_sanitizer_action_rejects_invalid_action_and_zero_count():
    SanitizerAction(field="description", action="redacted_email", count=1)
    with pytest.raises(ValidationError):
        SanitizerAction(field="description", action="bogus_action", count=1)
    with pytest.raises(ValidationError):
        SanitizerAction(field="description", action="redacted_email", count=0)


# ---- Round-trip JSON via alias -----------------------------------------


def test_provenance_json_roundtrip_via_alias():
    prov = Provenance(
        expansion_id="E-1",
        added_at=datetime.now(UTC),
        added_by_tenant="t1",
        confidence=0.5,
        source_kind="agent_expansion",
        sanitizer_actions=[],
        status="staged",
    )
    comp = RegistryComponent(canonical_name="U1300", kind="IC", aliases=[], provenance=prov)
    raw = comp.model_dump(by_alias=True, mode="json")
    assert "_provenance" in raw
    restored = RegistryComponent.model_validate(raw)
    assert restored.provenance.expansion_id == "E-1"
