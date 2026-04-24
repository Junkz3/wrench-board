# SPDX-License-Identifier: Apache-2.0
"""Tool manifest + system prompt builders for the diagnostic agent.

- MB_TOOLS: the always-on memory-bank family (4 tools).
- BV_TOOLS: the boardview control family (12 tools), exposed only when
  a board is loaded in the session.
- build_tools_manifest(session): produces the per-session manifest
  passed to Anthropic's messages.create or the Managed Agent definition.
- render_system_prompt(session, device_slug): DIRECT-runtime only; the
  Managed-runtime prompt is carried by the agent server-side.
"""

from __future__ import annotations

from pathlib import Path

from api.config import get_settings
from api.profile.prompt import render_technician_block
from api.profile.store import load_profile
from api.session.state import SessionState

MB_TOOLS: list[dict] = [
    {
        "type": "custom",
        "name": "mb_get_component",
        "description": (
            "Look up a component by refdes on the current device. Returns "
            "aggregated info: {found, canonical_name, memory_bank: {...}|null, "
            "board: {...}|null} when found. For unknown refdes returns "
            "{found: false, closest_matches: [...]}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {"type": "string", "description": "e.g. U7, C29, J3100"},
            },
            "required": ["refdes"],
        },
    },
    {
        "type": "custom",
        "name": "mb_get_rules_for_symptoms",
        "description": (
            "Find diagnostic rules matching a list of symptoms, ranked by "
            "symptom overlap + rule confidence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symptoms": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["symptoms"],
        },
    },
    {
        "type": "custom",
        "name": "mb_list_findings",
        "description": (
            "Return prior confirmed findings (field reports) for the current "
            "device, newest first. Cross-session memory — check on open."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
                "filter_refdes": {"type": "string"},
            },
        },
    },
    {
        "type": "custom",
        "name": "mb_record_finding",
        "description": (
            "Persist a confirmed repair finding so future sessions see it. "
            "Only when the technician explicitly confirms the cause."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {"type": "string"},
                "symptom": {"type": "string"},
                "confirmed_cause": {"type": "string"},
                "mechanism": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["refdes", "symptom", "confirmed_cause"],
        },
    },
    {
        "type": "custom",
        "name": "mb_schematic_graph",
        "description": (
            "Interrogate the compiled electrical graph for this device "
            "(rails, source ICs, enable signals, consumers, boot sequence). "
            "Deterministic disk read — no LLM cost, no side-effects. Use it "
            "BEFORE speculating on power topology. Queries: "
            "query='rail' with label (e.g. '+5V') returns source_refdes, "
            "enable_net, consumers, decoupling, voltage_nominal, boot_phase, "
            "pages; "
            "query='component' with refdes returns type, value, pins, pages, "
            "rails_produced, rails_consumed, populated, boot_phase; "
            "query='downstream' with refdes returns the transitive "
            "loss-of-power set if that component dies (rails_direct, "
            "components_direct, rails_transitive, components_transitive); "
            "query='boot_phase' with index returns that phase's rails and "
            "components; "
            "query='list_rails' returns a brief catalogue of every rail; "
            "query='list_boot' returns a brief catalogue of boot phases; "
            "query='critical_path' returns the board's Single-Points-Of-Failure "
            "ranked by blast_radius (how many nodes die if X fails) plus the "
            "critical gate at each boot phase. Use BEFORE telling the tech "
            "which component to measure first when a rail is absent — the "
            "top SPOF is usually the highest-leverage probe point; "
            "query='net' with label returns the net's classified domain + "
            "description + touching components; "
            "query='net_domain' with domain (e.g. 'hdmi', 'usb', 'audio') "
            "returns every net in that functional domain + top-3 suspect "
            "components ranked by touch_count + blast_radius. Use when the "
            "technician describes a symptom by function ('HDMI écran noir', "
            "'USB-C dead') — it surfaces the exact refdes to probe first. "
            "Returns {found: false, reason: 'no_schematic_graph'} if the "
            "schematic hasn't been ingested yet — don't retry, just proceed "
            "without rail context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "enum": [
                        "rail",
                        "component",
                        "downstream",
                        "boot_phase",
                        "list_rails",
                        "list_boot",
                        "critical_path",
                        "net",
                        "net_domain",
                    ],
                },
                "label": {
                    "type": "string",
                    "description": "Rail or net label, e.g. '+5V', '+3V3', '24V_IN', 'HDMI_HPD'. Required for query=rail or query=net.",
                },
                "refdes": {
                    "type": "string",
                    "description": "Component refdes, e.g. 'U7'. Required for query=component or query=downstream.",
                },
                "domain": {
                    "type": "string",
                    "description": "Functional domain for query=net_domain. Canonical values: hdmi, usb, pcie, ethernet, audio, display, storage, debug, power_seq, power_rail, clock, reset, control, ground. Free-form accepted.",
                },
                "index": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "1-based phase index. Required for query=boot_phase.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "type": "custom",
        "name": "mb_hypothesize",
        "description": (
            "Propose des hypothèses (refdes, mode) qui expliquent les observations. "
            "Modes IC (actifs) : dead (inerte), alive (fonctionne), anomalous (actif "
            "mais output incorrect — IC DSI bridge, codec audio, sensor), hot (chauffe "
            "anormalement). Modes PASSIVES (R/C/D/FB) : open (circuit coupé, typique "
            "ferrite brûlée ou R cassée), short (court plaque-à-plaque pour un cap, "
            "wire pour R). Modes RAILS : dead, alive, shorted (court vers GND ou "
            "overvoltage). Passer au moins une observation via state_comps/state_rails "
            "OU fournir repair_id pour synthétiser depuis le journal. La réponse "
            "contient `discriminating_targets` (list[str]) : quand les top-N candidats "
            "sont à égalité de score, ce sont les refdes/rails dont la mesure "
            "suivante partitionne le mieux les suspects — à suggérer au tech."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "state_comps": {
                    "type": "object",
                    "description": (
                        "Map refdes → mode. Pour un IC : 'dead', 'alive', 'anomalous', "
                        "'hot'. Pour un passive (R/C/D/FB) : 'open', 'short', 'alive'. "
                        "Le moteur rejette un IC en mode passive (et vice-versa)."
                    ),
                    "additionalProperties": {
                        "type": "string",
                        "enum": ["dead", "alive", "anomalous", "hot", "open", "short"],
                    },
                },
                "state_rails": {
                    "type": "object",
                    "description": "Map rail label → mode. Modes: 'dead', 'alive', 'shorted'.",
                    "additionalProperties": {
                        "type": "string",
                        "enum": ["dead", "alive", "shorted"],
                    },
                },
                "metrics_comps": {
                    "type": "object",
                    "description": "Optional numeric measurements on components, refdes → {measured, unit, nominal?}.",
                    "additionalProperties": {"type": "object"},
                },
                "metrics_rails": {
                    "type": "object",
                    "description": "Optional numeric measurements on rails.",
                    "additionalProperties": {"type": "object"},
                },
                "max_results": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                "repair_id": {
                    "type": "string",
                    "description": "If set AND state/metrics dicts are empty, synthesise observations from the repair's measurement journal.",
                },
            },
            "required": [],
        },
    },
    {
        "type": "custom",
        "name": "mb_record_measurement",
        "description": (
            "Enregistre une mesure électrique du tech dans le journal de la "
            "repair session. Cible au format 'rail:<label>' | 'comp:<refdes>' | "
            "'pin:<refdes>:<pin>'. Unit ∈ {V, A, W, °C, Ω, mV}. Si nominal est "
            "fourni, le mode est auto-classifié (alive/anomalous/dead/shorted/hot)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "value": {"type": "number"},
                "unit": {"type": "string", "enum": ["V", "A", "W", "°C", "Ω", "mV"]},
                "nominal": {"type": ["number", "null"]},
                "note": {"type": ["string", "null"]},
            },
            "required": ["target", "value", "unit"],
        },
    },
    {
        "type": "custom",
        "name": "mb_list_measurements",
        "description": "Relit le journal de mesures de la repair session, filtré par target et/ou timestamp.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": ["string", "null"]},
                "since": {"type": ["string", "null"]},
            },
            "required": [],
        },
    },
    {
        "type": "custom",
        "name": "mb_compare_measurements",
        "description": "Diff avant/après d'une cible donnée (mesure la plus ancienne vs la plus récente par défaut).",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "before_ts": {"type": ["string", "null"]},
                "after_ts": {"type": ["string", "null"]},
            },
            "required": ["target"],
        },
    },
    {
        "type": "custom",
        "name": "mb_observations_from_measurements",
        "description": "Synthétise un payload Observations (state + metrics) depuis le journal de mesures — dernier événement par cible.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "custom",
        "name": "mb_set_observation",
        "description": "Force un mode d'observation pour une cible sans enregistrer de valeur (utile quand le tech dit 'U7 est mort' sans mesure). Émet l'event WS pour l'UI.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "mode": {"type": "string", "enum": ["dead", "alive", "anomalous", "hot", "shorted"]},
            },
            "required": ["target", "mode"],
        },
    },
    {
        "type": "custom",
        "name": "mb_clear_observations",
        "description": "Efface l'état visuel des observations côté UI (le journal est préservé).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "custom",
        "name": "mb_validate_finding",
        "description": (
            "Enregistre le(s) composant(s) coupable(s) confirmé(s) par le tech à la "
            "fin d'une repair. À appeler UNIQUEMENT quand un trigger 'Marquer fix' "
            "a été reçu ET que les fixes sont confirmés (pas d'auto-validation sur "
            "contexte ambigu). `fixes` est une liste d'objets "
            "{refdes, mode ∈ (dead|alive|anomalous|hot|shorted|passive_swap), rationale}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fixes": {
                    "type": "array",
                    "description": "Liste des composants fixés lors de la repair.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "refdes": {"type": "string"},
                            "mode": {
                                "type": "string",
                                "enum": ["dead", "alive", "anomalous", "hot", "shorted", "passive_swap"],
                            },
                            "rationale": {"type": "string"},
                        },
                        "required": ["refdes", "mode", "rationale"],
                    },
                    "minItems": 1,
                },
                "tech_note": {"type": ["string", "null"]},
                "agent_confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "default": "high",
                },
            },
            "required": ["fixes"],
        },
    },
    {
        "type": "custom",
        "name": "mb_expand_knowledge",
        "description": (
            "Grow this device's memory bank around a focus symptom area. "
            "COSTS ~$0.40 AND 30-60s of wall clock. NEVER call autonomously — "
            "the technician MUST explicitly authorize this call (e.g. reply "
            "'oui', 'go', 'lance'). When mb_get_rules_for_symptoms returns "
            "zero matches, PROPOSE the expansion and wait for the tech's "
            "confirmation. Only then invoke this tool. After it succeeds, "
            "re-call mb_get_rules_for_symptoms to pick up the new rules."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "focus_symptoms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Symptom phrases to target, e.g. ['no sound', 'earpiece dead'].",
                },
                "focus_refdes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional refdes to probe specifically (e.g. ['U3101', 'U3200']).",
                },
            },
            "required": ["focus_symptoms"],
        },
    },
]


BV_TOOLS: list[dict] = [
    {
        "type": "custom",
        "name": "bv_highlight",
        "description": "Highlight one or more components on the PCB canvas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                },
                "color": {"type": "string", "enum": ["accent", "warn", "mute"], "default": "accent"},
                "additive": {"type": "boolean", "default": False},
            },
            "required": ["refdes"],
        },
    },
    {
        "type": "custom",
        "name": "bv_focus",
        "description": "Pan/zoom the PCB canvas to a specific component. Auto-flips the board if the component is on the hidden side.",
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {"type": "string"},
                "zoom": {"type": "number", "default": 2.5},
            },
            "required": ["refdes"],
        },
    },
    {
        "type": "custom",
        "name": "bv_reset_view",
        "description": "Reset the PCB canvas: clear all highlights, annotations, arrows, dim, filter. The technician's manual selection is preserved.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "type": "custom",
        "name": "bv_flip",
        "description": "Flip the visible PCB side (top ↔ bottom).",
        "input_schema": {
            "type": "object",
            "properties": {"preserve_cursor": {"type": "boolean", "default": False}},
        },
    },
    {
        "type": "custom",
        "name": "bv_annotate",
        "description": "Attach a text label to a component on the canvas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {"type": "string"},
                "label": {"type": "string"},
            },
            "required": ["refdes", "label"],
        },
    },
    {
        "type": "custom",
        "name": "bv_dim_unrelated",
        "description": "Visually dim all components not currently highlighted — focuses the technician's attention.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "type": "custom",
        "name": "bv_highlight_net",
        "description": "Highlight every pin on a given net (rail/signal tracing).",
        "input_schema": {
            "type": "object",
            "properties": {"net": {"type": "string"}},
            "required": ["net"],
        },
    },
    {
        "type": "custom",
        "name": "bv_show_pin",
        "description": "Point to a specific pin of a component (e.g. for a probe instruction).",
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes": {"type": "string"},
                "pin": {"type": "integer", "minimum": 1},
            },
            "required": ["refdes", "pin"],
        },
    },
    {
        "type": "custom",
        "name": "bv_draw_arrow",
        "description": "Draw an arrow between two components (e.g. to show a signal path).",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_refdes": {"type": "string"},
                "to_refdes": {"type": "string"},
            },
            "required": ["from_refdes", "to_refdes"],
        },
    },
    {
        "type": "custom",
        "name": "bv_measure",
        "description": "Return the physical distance (mm) between two components' centers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "refdes_a": {"type": "string"},
                "refdes_b": {"type": "string"},
            },
            "required": ["refdes_a", "refdes_b"],
        },
    },
    {
        "type": "custom",
        "name": "bv_filter_by_type",
        "description": "Show only components whose refdes starts with a given prefix. The prefix must be the letter(s) used in the refdes convention (e.g. 'C' for capacitors, 'U' for ICs, 'R' for resistors), not a category name like 'capacitor'.",
        "input_schema": {
            "type": "object",
            "properties": {"prefix": {"type": "string"}},
            "required": ["prefix"],
        },
    },
    {
        "type": "custom",
        "name": "bv_layer_visibility",
        "description": "Toggle visibility of a PCB layer (top or bottom).",
        "input_schema": {
            "type": "object",
            "properties": {
                "layer": {"type": "string", "enum": ["top", "bottom"]},
                "visible": {"type": "boolean"},
            },
            "required": ["layer", "visible"],
        },
    },
]


PROFILE_TOOLS: list[dict] = [
    {
        "type": "custom",
        "name": "profile_get",
        "description": (
            "Read the technician's profile: identity, current level, "
            "verbosity preference, list of available and missing tools, and "
            "summary of mastered/practiced/learning skills with usage counts. "
            "Call once at session start if the system prompt context is stale, "
            "or when the tech reports having updated their profile."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "type": "custom",
        "name": "profile_check_skills",
        "description": (
            "Given a list of candidate skill ids from the catalogue (e.g. "
            "reflow_bga, short_isolation), return for each: the tech's status "
            "(unlearned|learning|practiced|mastered), usage count, whether the "
            "required tools are available, and if not the missing tool ids. "
            "Use BEFORE proposing an action plan so you can adapt depth per step "
            "and skip actions with missing tools."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "candidate_skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
            },
            "required": ["candidate_skills"],
        },
    },
    {
        "type": "custom",
        "name": "profile_track_skill",
        "description": (
            "Record that the technician has executed an action requiring this "
            "skill, with evidence. Call ONLY after explicit confirmation from "
            "the tech that the action was performed. action_summary must be at "
            "least 20 characters and quote the actual fix (refdes, symptom, "
            "outcome) — the backend rejects thin evidence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_id": {"type": "string"},
                "evidence": {
                    "type": "object",
                    "properties": {
                        "repair_id": {"type": "string"},
                        "device_slug": {"type": "string"},
                        "symptom": {"type": "string"},
                        "action_summary": {"type": "string", "minLength": 20},
                        "date": {"type": "string"},
                    },
                    "required": ["repair_id", "device_slug", "symptom", "action_summary", "date"],
                },
            },
            "required": ["skill_id", "evidence"],
        },
    },
]


def build_tools_manifest(session: SessionState) -> list[dict]:
    """Return the tools list for `session`. `profile_*` always present; `bv_*`
    only when a board is loaded. Future: `sch_*` when a schematic is attached."""
    manifest: list[dict] = list(MB_TOOLS) + list(PROFILE_TOOLS)
    if session.board is not None:
        manifest.extend(BV_TOOLS)
    return manifest


def _has_electrical_graph(device_slug: str) -> bool:
    root = Path(get_settings().memory_root)
    return (root / device_slug / "electrical_graph.json").exists()


def render_system_prompt(session: SessionState, *, device_slug: str) -> str:
    """Build the system prompt for the DIRECT runtime only.

    The Managed runtime carries its prompt server-side via managed_ids.json
    and doesn't call this function.
    """
    boardview_status = "✅" if session.board is not None else "❌ (no board file loaded)"
    schematic_status = (
        "✅ (mb_schematic_graph)"
        if _has_electrical_graph(device_slug)
        else "❌ (not yet parsed)"
    )
    technician_block = render_technician_block(load_profile())
    return f"""\
You are a calm, methodical board-level diagnostics assistant for a
microsoldering technician. Tu tutoies, en français, direct et pédagogique.

Device courant : {device_slug}.

{technician_block}

Capabilities for this session:
  - memory bank ✅ (mb_get_component, mb_get_rules_for_symptoms, mb_list_findings, mb_record_finding, mb_expand_knowledge)
  - profile ✅ (profile_get, profile_check_skills, profile_track_skill)
  - boardview {boardview_status}
  - schematic {schematic_status}

RÈGLE ANTI-HALLUCINATION : tu NE mentionnes JAMAIS un refdes (U7, C29,
J3100…) sans l'avoir validé via mb_get_component. Si le tool retourne
{{found: false, closest_matches: [...]}}, tu proposes une des
closest_matches ou tu demandes clarification — JAMAIS d'invention. Les
refdes non validés seront automatiquement wrapped ⟨?U999⟩ dans la
réponse finale (sanitizer post-hoc) — signal de debug, pas d'excuse.

Quand l'utilisateur décrit des symptômes, consulte d'abord mb_list_findings
(historique cross-session de ce device), puis mb_get_rules_for_symptoms.
Avant de proposer un plan d'action, appelle profile_check_skills avec les
compétences que ton plan mobilise — adapte ton niveau de détail et évite
les actions dont les outils ne sont pas dispo. Quand le tech confirme
avoir exécuté une étape avec succès, appelle profile_track_skill (evidence
concrète — refdes, symptôme, geste — JAMAIS un résumé vague).

Si mb_get_rules_for_symptoms retourne 0 matches sur un symptôme sérieux,
PROPOSE mb_expand_knowledge ("Je peux lancer un Scout ciblé sur ces symptômes
— ~30s, ~0.40$ de tokens. Go ?"). NE LANCE PAS tant que le tech n'a pas dit
oui. Après son go, invoque le tool, patiente, puis re-call
mb_get_rules_for_symptoms. Quand il demande un composant, appelle
mb_get_component. Si la boardview est disponible, enchaîne bv_focus +
bv_highlight pour MONTRER le suspect. Quand le tech confirme la cause,
appelle mb_record_finding. Ne réponds JAMAIS depuis ta mémoire de formation
pour des refdes ou des symptômes — utilise toujours les tools ci-dessus.

STYLE. Tu écris comme un ingé de bench qui tape vite : phrases courtes,
pas d'emoji, pas d'ouverture polie (« Excellent. » / « Parfait. »), pas
de bullet list verbeuse quand 2 lignes suffisent. Jargon pro autorisé
(PMIC, BGA reball, cold joint, reflow), pas de vulgarisation gratuite.
Quand tu cites un refdes, toujours en majuscules monospace-style (U7,
C156). Les modes failure se lisent en français technique : « claqué »
(short), « brûlée » (fusible/ferrite open), « HS » / « morte » (dead),
« dégazée » (electrolytic bulging). Pas d'anglicisme gratuit comme
« let me check » — dis « je regarde ».

HYPOTHESIZE — lire la réponse.
Le tool `mb_hypothesize` retourne `hypotheses` triées par score
décroissant + `discriminating_targets` (list).

  - Top-1 détaché (score ≥ 2× le suivant) → présente-le direct, cite
    physiquement le mode (pas juste « C156 short » mais « C156 claqué
    plaque-à-plaque »), puis chaîne MESURE-CIBLE (§ suivant) pour
    valider avant remplacement.
  - Top-N à égalité → ne liste pas les 5 candidats, prends
    `discriminating_targets` et chaîne MESURE-CIBLE sur chacun.
  - `discriminating_targets=[]` → pas d'ambiguïté, top-1.

Modes passives (Phase 4) :
  - `short` sur un passive_c = claquage plaque-à-plaque, rail shorted
  - `open` sur un passive_fb = ferrite brûlée, rail downstream dead
  - `open` sur un passive_r role=feedback = divider ouvert, rail part
    en overvoltage
  - `open` sur un passive_r role=pull_up/pull_down = signal floats
  - `short` sur un passive_c role=filter/decoupling = même pattern que
    decoupling short

Le scoring passive a un multiplicateur 0.5× par design sur les cascades
topologiquement faibles (decoupling/bulk/filter open, pull_up/down
open). Un score 0.5 sur une passive = candidat LÉGITIME, pas faible.

MESURE-CIBLE — jamais « mesure U1 » vague.
Quand tu suggères une mesure (discriminateur ou validation top-1), tu
DOIS d'abord appeler `mb_get_component(refdes)` pour récupérer la
liste de pins avec leurs `role` et `net_label`. Puis tu sélectionnes
UNE pin utile :

  - Si le refdes est un IC/PMIC et on cherche si le rail arrive : pin
    avec role=`power_in` sur le rail en question. Dis au tech
    « ohmmètre entre pin N (power_in +5V) et GND sur U1, attendu ~9-
    50kΩ alim coupée. Résistance quasi-zéro = court confirmé ».
  - Si le refdes est soupçonné hot/shorted : pin `power_in` d'entrée
    d'alim, tech fait main-sur-boîtier sous PSU limitée 500mA, repère
    lequel chauffe sous 5-10s.
  - Si on veut valider un signal (anomalous) : pin `signal_out` ou
    `clock_out`, scope ou multi en AC.
  - Si pin introuvable ou toutes BGA (inaccessible) : dis-le au tech,
    propose d'injecter du courant limité via l'entrée du rail et de
    faire thermal/toucher pour localiser, OU dis qu'on doit passer à
    une autre piste.

Si la boardview est chargée, enchaîne `bv_show_pin(refdes=..., pin=N)`
pour la surligner visuellement. Pas de boardview = pas grave, le tech
lit le refdes + pin number.

Format de suggestion de mesure typique :
  « ohmmètre, pin 3 de U1 (power_in +5V) vers GND. Attendu hors alim :
  quelques kΩ. Court franc (<1Ω) = U1 ou son découplage en cause. »

ANTI-GÉNÉRIQUE. Évite le boilerplate « caméra thermique, décoloration,
odeur de brûlé ». Propose UN test précis à la fois, pas une liste de
trois options au tech. Le tech n'a pas forcément de thermal camera —
demande-lui ce qu'il a avant de supposer. Si le scope par défaut est
un multimètre + une PSU limitée + une main, reste là.

TIER. Quand tu tournes sur tier=fast (Haiku), tu es sous-dimensionné
pour le diagnostic complexe (long tail, schéma touffu). Si tu ressens
que la piste devient touffue (3+ hypothèses de scores proches, nets
ambigus, designer notes à interpréter), signale-le : « ce diag bénéficie
d'un tier plus riche, bascule sur normal ou deep ». Le tech reconnectera
son WS avec un tier supérieur.
"""
