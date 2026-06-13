"""RulesSet — coercion tolérante d'un argument `rules` stringifié par le LLM.

Bug observé en PROD (log serveur, pipeline Clinicien-Expand) : sous forced
`tool_choice`, le modèle Anthropic renvoie parfois l'argument `rules` du tool
`submit_rules` comme une STRING JSON au lieu d'une vraie liste d'objets :

    WARNING [Clinicien-Expand] attempt=1 validation failed: 1 validation error
      rules  Input should be a valid list [type=list_type,
             input_value='{\n  "rules": [\n   ...}', input_type=str]
    WARNING [Clinicien-Expand] attempt=2 validation failed: ...
             input_value='{"schema_version":"1.0",...}'
    ERROR   [API] expand_pack failed ... Failed to produce a valid submit_rules
            output after 2 attempts.

Deux formes observées :
  A. `rules` = la LISTE sérialisée   →  '[{...}, {...}]'
  B. `rules` = le RulesSet ENTIER sérialisé (double-encodage) →
                                          '{"schema_version":"1.0","rules":[...]}'

Une récupération existait déjà côté appelant (`tool_call._try_unwrap`), mais elle
ne protège QUE le chemin `call_with_forced_tool`. Tout autre code qui valide
`RulesSet`/`Rule` directement (p.ex. `expansion._build_delta_facts` re-valide
chaque `Rule`) restait fragile. Le fix central : un `model_validator(mode="before")`
sur `RulesSet` qui décode une `rules` stringifiée AVANT la validation de champ,
de sorte que `RulesSet.model_validate(...)` réussisse partout, du premier coup.
"""

from __future__ import annotations

import json

from api.pipeline.schemas import RulesSet

# Un fact `Rule` minimal valide, réutilisé par les cas ci-dessous.
_ONE_RULE = {
    "id": "R-X-001",
    "symptoms": ["no boot"],
    "likely_causes": [{"refdes": "U1", "probability": 0.5, "mechanism": "short"}],
    "diagnostic_steps": [],
    "confidence": 0.5,
    "sources": [],
}


def test_rules_field_as_json_list_string_is_coerced():
    """Forme A — `rules` arrive comme la liste JSON sérialisée en string."""
    payload = {"schema_version": "1.0", "rules": json.dumps([_ONE_RULE])}
    rs = RulesSet.model_validate(payload)
    assert isinstance(rs, RulesSet)
    assert len(rs.rules) == 1
    assert rs.rules[0].id == "R-X-001"


def test_whole_rulesset_wrapped_in_rules_field_is_coerced():
    """Forme B — le RulesSet entier est wedgé (stringifié) dans le champ `rules`.

    C'est le `attempt=2` exact du log de prod : `{"rules": "<JSON de tout le
    RulesSet>"}`. Le validator doit déballer le wrapper puis lire `rules`."""
    inner = json.dumps({"schema_version": "1.0", "rules": [_ONE_RULE]})
    payload = {"rules": inner}
    rs = RulesSet.model_validate(payload)
    assert len(rs.rules) == 1
    assert rs.rules[0].id == "R-X-001"


def test_top_level_is_a_json_string_of_the_whole_rulesset():
    """Le payload TOUT ENTIER est une string JSON (le modèle a sérialisé l'objet
    racine). `mode="before"` reçoit la string brute → on la json.loads d'abord."""
    payload = json.dumps({"schema_version": "1.0", "rules": [_ONE_RULE]})
    rs = RulesSet.model_validate(payload)
    assert len(rs.rules) == 1


def test_normal_list_payload_is_unchanged():
    """Régression : un payload bien formé (rules = vraie liste) passe inchangé."""
    payload = {"schema_version": "1.0", "rules": [_ONE_RULE]}
    rs = RulesSet.model_validate(payload)
    assert len(rs.rules) == 1
    assert rs.rules[0].id == "R-X-001"


def test_empty_default_still_valid():
    """Régression : RulesSet() / {} reste valide (rules a un default vide)."""
    assert RulesSet.model_validate({}).rules == []
    assert RulesSet().rules == []


def test_non_json_string_left_for_pydantic_to_reject():
    """Garde-fou : une string non-JSON dans `rules` n'est PAS avalée — on la
    laisse telle quelle et Pydantic lève son erreur `list_type` habituelle (on ne
    masque pas un vrai problème en inventant une liste vide)."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RulesSet.model_validate({"schema_version": "1.0", "rules": "just prose"})


# --- Rule id normalization (Clinicien drift fix) ----------------------------
# Found in a real build: the Clinicien writer emits lowercase 'rule-...' ids that
# fail the schema pattern ^R-[A-Z0-9_-]{1,48}$ → forced-tool validation fails →
# retry/degradation. Normalize server-side instead of betting on the LLM's casing.

def test_rule_id_lowercase_rule_prefix_is_normalized():
    from api.pipeline.schemas import Rule
    r = Rule.model_validate({**_ONE_RULE, "id": "rule-cd3217-ldo-short-001"})
    assert r.id == "R-CD3217-LDO-SHORT-001"


def test_rule_id_lowercase_without_prefix_is_uppercased():
    from api.pipeline.schemas import Rule
    r = Rule.model_validate({**_ONE_RULE, "id": "r-pp1v8-short-001"})
    assert r.id == "R-PP1V8-SHORT-001"


def test_rule_id_already_canonical_is_unchanged():
    from api.pipeline.schemas import Rule
    r = Rule.model_validate({**_ONE_RULE, "id": "R-REFORM-001"})
    assert r.id == "R-REFORM-001"
