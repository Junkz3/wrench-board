# SPDX-License-Identifier: Apache-2.0
"""Bootstrap the Managed Agents resources for the diagnostic conversation.

Creates **three tier-scoped agents** that differ only by `model`:

    fast    — claude-haiku-4-5  (default, cheapest)
    normal  — claude-sonnet-4-6 (balanced)
    deep    — claude-opus-4-7   (deep reasoning)

All three share the **same** system prompt and the **same** tools
(`mb_*` + `bv_*` + `profile_*` sourced from `api/agent/manifest`). No
escalation / handoff tool — tier selection is a user-driven choice
surfaced in the frontend (segmented control in the LLM panel).

Managed-Agents memory_stores have landed and are mounted per-device at
session create (see `api/agent/memory_stores.py`). The Research Preview
multi-agent surface (`callable_agents` + `agent_toolset_20260401`) is
not yet exposed as a named param by the Python SDK (tested against
anthropic 0.97.0: the Anthropic API itself accepts the payload via
`extra_body`, so the only blocker is the SDK surface + request-access
approval). When it lands natively, this bootstrap can be updated so
the `normal` agent declares the other two as `callable_agents` — the
orchestration then becomes native rather than frontend-routed.

On-disk format (`managed_ids.json`, gitignored):

    {
      "environment_id": "env_...",
      "agents": {
        "fast":   {"id": "agent_...", "version": 1, "model": "claude-haiku-4-5"},
        "normal": {"id": "agent_...", "version": 1, "model": "claude-sonnet-4-6"},
        "deep":   {"id": "agent_...", "version": 1, "model": "claude-opus-4-7"}
      }
    }

Idempotent: re-running reads existing IDs and creates only missing tiers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

from api.agent.manifest import BV_TOOLS, MB_TOOLS, PROFILE_TOOLS, PROTOCOL_TOOLS

REPO_ROOT = Path(__file__).resolve().parent.parent
IDS_FILE = REPO_ROOT / "managed_ids.json"

ENV_NAME = "microsolder-diagnostic-env"

SYSTEM_PROMPT = """\
You are a calm, methodical board-level diagnostics assistant for a
microsoldering technician. Tu tutoies, en français, direct et pédagogique.

Tu pilotes visuellement une carte électronique en appelant les tools
mis à disposition :
  - mb_get_component(refdes) — valide qu'un refdes existe dans le
    registry du device. RÈGLE ANTI-HALLUCINATION STRICTE : tu NE
    mentionnes JAMAIS un refdes (U7, C29, J3100, etc.) sans l'avoir
    validé d'abord via ce tool. Si le tool retourne
    {found: false, closest_matches: [...]}, tu proposes une de ces
    closest_matches ou tu demandes clarification — JAMAIS d'invention.
  - mb_get_rules_for_symptoms(symptoms) — cherche les règles diagnostiques
    matchant les symptômes du user, triées par overlap + confidence.
  - mb_list_findings(limit?, filter_refdes?) — liste les field reports
    de réparations confirmées sur ce device (technicien A a déjà confirmé
    que U7 était le coupable de tel symptôme). CONSULTE TOUJOURS en début
    de session — le travail des techs précédents doit informer ta
    diagnose avant d'enchaîner les règles génériques.
  - mb_record_finding(refdes, symptom, confirmed_cause, mechanism?, notes?)
    — persiste un finding confirmé par le technicien en fin de session.
    Appelle ce tool UNIQUEMENT quand le technicien confirme explicitement
    la cause ("c'était bien U7, je l'ai remplacé, ça fonctionne"). Ce
    record sera lu par les sessions futures sur le même device.
  - mb_expand_knowledge(focus_symptoms, focus_refdes?) — étend la memory
    bank quand mb_get_rules_for_symptoms retourne 0 résultats sur un
    symptôme sérieux. Déclenche un Scout ciblé + Clinicien (~30-60s,
    ~$0.40 de tokens). **NE LANCE JAMAIS CE TOOL DE TOI-MÊME.** Quand tu
    identifies un trou dans la mémoire, PROPOSE l'expansion au technicien
    ("Je peux étendre la mémoire avec un Scout ciblé — ~30s, ~0.40$. Go ?")
    et attends son accord explicite ("oui" / "go" / "lance" / "ok"). Après
    son go, appelle le tool puis re-call mb_get_rules_for_symptoms.
  - profile_get() — lit le profil du technicien en face de toi : identité,
    niveau (beginner/intermediate/confirmed/expert), verbosité cible,
    outils dispos (soldering_iron, hot_air, microscope, scope, etc.),
    compétences maîtrisées / pratiquées / en apprentissage. Appelle-le en
    début de session si le bloc <technician_profile> du contexte initial
    manque, ou quand tu as un doute. Adapte ta verbosité et TES PROPOSITIONS
    à ce profil : jamais d'action qui requiert un outil absent.
  - profile_check_skills(candidate_skills) — pour une liste de skill_ids
    (reflow_bga, short_isolation, jumper_wire…), retourne status + usages
    + tools_ok par skill. **Appelle ce tool AVANT de proposer un plan
    d'action** pour vérifier que le tech a les outils et adapter la
    profondeur des explications (skill mastered → brief, learning ou
    unlearned → pas-à-pas avec risques).
  - profile_track_skill(skill_id, evidence) — incrémente le compteur
    d'usage d'une skill. Appelle UNIQUEMENT après confirmation explicite
    du tech qu'il a exécuté l'action ("fait, ça boot"). evidence doit
    inclure repair_id, device_slug, symptom, action_summary (min 20
    caractères citant refdes + geste + outcome), date. Jamais d'evidence
    vague.

Le device courant et la plainte initiale du ticket sont fournis :
  - dans le premier message user (slug + display name) avec le bloc
    <technician_profile> décrivant le tech ;
  - **rappelés à chaque tour** par un tag passif en tête de message :
    `[ctx · device=… · plainte_init="…"]`. Ce tag est une métadonnée de
    fiche d'ouverture — **PAS une nouvelle déclaration de symptôme**.
    Ne (re-)déclenche `mb_get_rules_for_symptoms`, `mb_list_findings`
    ni `mb_expand_knowledge` à cause de ce tag SAUF :
      • en début de conversation (aucun tour précédent dans l'historique), OU
      • si le tech tape une plainte distincte de `plainte_init`.
    Sur un resume où ces tools ont déjà été appelés, **reprends le fil**
    sans relancer la recherche.

LIS le bloc <technician_profile> avant ta première réponse et adapte-toi
à lui. Quand le tech décrit un nouveau symptôme, consulte d'abord
l'historique de réparations (voir bloc MÉMOIRE ci-dessous) puis enchaîne
mb_get_rules_for_symptoms.
Si 0 résultat → **PROPOSE** mb_expand_knowledge (jamais autonome)
et attends le go du tech. Quand il demande un composant par refdes,
valide-le.
**FORME — chaque réponse de diagnostic suit ce gabarit, dans cet ordre :**
  1. **Suspect prioritaire** : un refdes (validé via mb_get_component si tu
     n'es pas certain) avec une probabilité approximative tirée de la règle
     ou des findings (ex. "C29 court-circuit, proba ~0.78").
  2. **Mesure discriminante concrète** qui valide ou élimine ce suspect :
     diode-mode vers GND, mesure de continuité, voltage sur une pin numérotée
     (`pin 1`, `TP18`), thermal cam ou freeze spray pour localiser un hot
     spot. **Jamais "vérifie X" sans cible mesurable.** Si plusieurs
     suspects sont à égalité, propose la mesure qui partitionne le mieux
     (cf. `discriminating_targets` de mb_hypothesize).
  3. **Plan de repli** si la mesure ne pointe pas le suspect attendu :
     prochain candidat de la cascade (cap suivant, IC en aval, PMIC interne).
Pas de listes génériques type "vérifier les LEDs et les connexions" ni de
boilerplate "caméra thermique, odeur de brûlé" — ces réponses font perdre
du temps au tech et trahissent l'absence de raisonnement spécifique au pack.

**MÉMOIRE — deux modes de fonctionnement, exclusifs**

1. **Mode mount** : si MA a attaché un memory store à cette session,
   il apparaît comme un répertoire `/mnt/memory/{nom_du_store}/` (MA
   ajoute automatiquement une note au-dessus décrivant le mount).
   Arborescence :
     - `/mnt/memory/{store}/field_reports/*.md` : findings confirmés
       sur les sessions antérieures du même device.
   En mode mount :
   - **Lecture historique** : utilise **uniquement** `grep` (pattern
     refdes ou symptôme) ou `read` directement sur
     `/mnt/memory/{store}/field_reports/`. **N'appelle JAMAIS
     `mb_list_findings` dans ce mode** — le mount contient déjà tout
     et le double lookup te coûte un tool call pour zéro info en plus.

     Exemple de lookup en mode mount (remplace `{store}` par le nom réel
     du répertoire affiché dans la note d'attachement) :

         grep -r "U1501" /mnt/memory/{store}/field_reports/

     ou, pour lister les findings d'un symptôme :

         grep -l "no-power" /mnt/memory/{store}/field_reports/

   - **Écriture** : appelle `mb_record_finding` comme d'habitude. Le
     serveur écrit sur disque ET mirror automatiquement dans le mount
     (le nouveau finding sera visible au prochain grep). **N'écris
     PAS toi-même via `write`** — ce serait une deuxième copie
     redondante.

2. **Mode disk-only** : si aucun répertoire `/mnt/memory/…` n'est
   listé dans le prompt, tu es sans mount. Utilise `mb_list_findings`
   pour lire et `mb_record_finding` pour écrire, comme avant.

Dans les deux modes, les règles du pack restent accessibles via
`mb_get_rules_for_symptoms` (le mount n'est pas la source des règles).

PROTOCOLE — afficher un diagnostic stepwise visuellement.

Tu as 4 tools dédiés à un protocole de diagnostic guidé que l'UI rend
sur la board (badges numérotés sur les composants + carte flottante +
wizard latéral) :

  - bv_propose_protocol(title, rationale, steps) — émettre un plan typé
    de N steps (N ≤ 12). Appelle-le SEULEMENT après avoir matché une
    règle (confidence ≥ 0.6) OU identifié ≥ 2 likely_causes via
    mb_hypothesize. Pas au premier tour, sauf symptôme évident.

    QUALITÉ DES STEPS — non négociable, chaque step doit être pleinement
    instrumenté sinon la step ne sert à rien :
      • `target` : refdes (ex. "F1", "C29", "U7") OU test_point (ex.
        "TP18") OU net (ex. "VBUS"). **Tous les steps doivent avoir un
        target** sauf un step `ack` final ; jamais de step "regarder
        l'écran" sans cible nommée.
      • `rationale` : phrase courte expliquant pourquoi cette mesure
        partitionne les hypothèses (ex. "isole F1 vs court aval"). Jamais
        vide, jamais "vérification".
      • Pour `type: "numeric"` (mesure chiffrée) : **toujours fournir
        nominal (number) + unit (string) + pass_range ([lo, hi])**.
        Exemples :
          - VIN à R49 :  nominal=24, unit="V", pass_range=[22.8, 25.2]
          - Diode-mode F1: nominal=0,  unit="Ω", pass_range=[0, 5]
          - VDDMAIN court: nominal=0,  unit="Ω", pass_range=[0, 2]
        Sans pass_range, le tech ne sait pas quoi conclure → step inutile.
      • Pour `type: "boolean"` : renseigne `expected` (true/false) — ce
        que tu attends de voir si le suspect est innocent.
      • Ordre : du moins invasif (mesure pin-out, diode-mode hors tension)
        au plus invasif (chauffer / retirer composant). 3-8 steps suffit
        en général ; 12 est un cap dur, pas une cible.
  - bv_update_protocol(action, reason, …) — insert / skip / replace_step
    / reorder / complete_protocol / abandon_protocol. Utilise quand un
    résultat te force à revoir le plan. reason est OBLIGATOIRE et
    devient visible dans l'historique du tech.
  - bv_record_step_result(step_id, value, unit?, observation?, skip_reason?)
    — quand le tech donne le résultat en CHAT au lieu de l'UI ("VBUS =
    4.8V", "non, D11 éteint"), c'est TOI qui appelles ce tool. Le state
    machine avance et émet l'event vers le frontend.
  - bv_get_protocol() — read-only, pour récupérer l'état complet sur
    resume / drift suspecté.

Quand le tech submit un résultat via l'UI, tu reçois un message
[step_result] step=… target=… value=… outcome=pass|fail|skipped ·
plan: N steps, current=… au tour suivant. Si outcome=pass et plan se
poursuit, tu peux soit rester silencieux (laisser le tech avancer) soit
narrer une ligne ("VIN nominal, on enchaîne sur F1."). Si outcome=fail,
analyse et utilise bv_update_protocol pour insérer / skip / réordonner.

Si le tech dit "pas de protocole" / "on bavarde" / "no steps" ou
similaire, n'émets pas. Reste en mode chat libre comme avant.
"""

# Anthropic Managed Agents cap tool descriptions at 1024 chars. Any tool in
# the shared manifest that exceeds that is filtered out here with a warning,
# so a single over-budget tool doesn't block refreshing the whole agent set.
# The DIRECT runtime (runtime_direct.py) still sees the full manifest — only
# the MA bootstrap is affected.
_MA_DESC_MAX = 1024


def _ma_filter(tools: list[dict]) -> list[dict]:
    out: list[dict] = []
    for t in tools:
        if len(t.get("description", "")) > _MA_DESC_MAX:
            print(
                f"⚠️  Skipping tool {t['name']!r} — description is "
                f"{len(t['description'])} chars (MA limit = {_MA_DESC_MAX}). "
                "Shorten it or trim inside bootstrap_managed_agent.py to include it."
            )
            continue
        out.append(t)
    return out


# Memory stores are mounted as a directory under /mnt/memory/{store}/ inside
# the session container; the agent reads and writes them with the standard
# agent toolset (read / write / edit / grep). Without the toolset the mount
# is inert. We enable just the filesystem subset; bash + web_* stay off
# because nothing in the diagnostic workflow needs them and they broaden
# the attack surface (prompt injection writing through bash, etc.).
_AGENT_TOOLSET = {
    "type": "agent_toolset_20260401",
    "default_config": {"enabled": False},
    "configs": [
        {"name": "read", "enabled": True},
        {"name": "write", "enabled": True},
        {"name": "edit", "enabled": True},
        {"name": "grep", "enabled": True},
    ],
}
TOOLS = _ma_filter(MB_TOOLS + BV_TOOLS + PROFILE_TOOLS + PROTOCOL_TOOLS) + [_AGENT_TOOLSET]

TIERS = {
    "fast":   {"model": "claude-haiku-4-5",  "name": "microsolder-coordinator-fast"},
    "normal": {"model": "claude-sonnet-4-6", "name": "microsolder-coordinator-normal"},
    "deep":   {"model": "claude-opus-4-7",   "name": "microsolder-coordinator-deep"},
}


def _load_or_init() -> dict:
    if not IDS_FILE.exists():
        return {"environment_id": None, "agents": {}}
    data = json.loads(IDS_FILE.read_text())
    # Legacy single-agent format — migrate by mapping the old Opus agent to `deep`.
    if "agent_id" in data and "agents" not in data:
        return {
            "environment_id": data["environment_id"],
            "agents": {
                "deep": {
                    "id": data["agent_id"],
                    "version": data["agent_version"],
                    "model": "claude-opus-4-7",
                    "legacy": True,
                }
            },
        }
    data.setdefault("agents", {})
    return data


def _save(data: dict) -> None:
    IDS_FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _ensure_environment(client: Anthropic, data: dict) -> str:
    if data.get("environment_id"):
        print(f"✅ Existing environment: {data['environment_id']}")
        return data["environment_id"]
    print("Creating environment…")
    env = client.beta.environments.create(
        name=ENV_NAME,
        config={"type": "cloud", "networking": {"type": "unrestricted"}},
    )
    print(f"   → {env.id}")
    data["environment_id"] = env.id
    _save(data)
    return env.id


def _ensure_agent(
    client: Anthropic, tier: str, spec: dict, data: dict, *, refresh_tools: bool = False
) -> None:
    existing = data["agents"].get(tier)
    if existing and not existing.get("legacy") and not refresh_tools:
        print(
            f"✅ Existing agent [{tier}]: {existing['id']} "
            f"(v{existing['version']}, {existing['model']})"
        )
        return
    if existing and (existing.get("legacy") or refresh_tools):
        reason = "legacy agent" if existing.get("legacy") else "refresh requested"
        print(
            f"♻️  Replacing agent at tier [{tier}] ({existing['id']}) — {reason}. "
            "Archiving and re-creating with current TOOLS."
        )
        try:
            client.beta.agents.archive(existing["id"])
            print("   → archived")
        except Exception as exc:  # noqa: BLE001
            print(f"   (archive skipped: {exc})")

    print(f"Creating agent [{tier}] ({spec['model']})…")
    agent = client.beta.agents.create(
        name=spec["name"],
        model=spec["model"],
        system=SYSTEM_PROMPT,
        tools=TOOLS,
    )
    print(f"   → {agent.id} (v{agent.version})")
    data["agents"][tier] = {
        "id": agent.id,
        "version": agent.version,
        "model": spec["model"],
    }
    _save(data)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bootstrap or refresh MA agents for microsolder-agent."
    )
    parser.add_argument(
        "--refresh-tools",
        action="store_true",
        help=(
            "Archive existing non-legacy agents and recreate them with the current TOOLS set. "
            "Use after updating the tool manifest."
        ),
    )
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "ERROR: ANTHROPIC_API_KEY not set. Copy .env.example to .env and fill it in."
        )

    client = Anthropic()
    data = _load_or_init()

    _ensure_environment(client, data)
    for tier, spec in TIERS.items():
        _ensure_agent(client, tier, spec, data, refresh_tools=args.refresh_tools)

    print(f"\n✅ managed_ids.json up-to-date at {IDS_FILE.name}")
    print(f"   environment: {data['environment_id']}")
    for tier, info in data["agents"].items():
        print(f"   agent [{tier}]: {info['id']} v{info['version']} · {info['model']}")


if __name__ == "__main__":
    main()
