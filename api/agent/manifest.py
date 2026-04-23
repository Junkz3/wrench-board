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
            "top SPOF is usually the highest-leverage probe point. "
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
                    ],
                },
                "label": {
                    "type": "string",
                    "description": "Rail label, e.g. '+5V', '+3V3', '24V_IN'. Required for query=rail.",
                },
                "refdes": {
                    "type": "string",
                    "description": "Component refdes, e.g. 'U7'. Required for query=component or query=downstream.",
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
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
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
    return f"""\
You are a calm, methodical board-level diagnostics assistant for a
microsoldering technician. Tu tutoies, en français, direct et pédagogique.

Device courant : {device_slug}.

Capabilities for this session:
  - memory bank ✅ (mb_get_component, mb_get_rules_for_symptoms,
    mb_list_findings, mb_record_finding, mb_expand_knowledge)
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
**Si mb_get_rules_for_symptoms retourne 0 matches** sur un symptôme sérieux,
**PROPOSE** au tech d'étendre la memory bank via mb_expand_knowledge
("Je peux lancer un Scout ciblé sur ces symptômes — ~30s, ~0.40$ de tokens.
Go ?"). **NE LANCE PAS mb_expand_knowledge tant que le tech n'a pas
explicitement dit oui** (oui / go / lance / ok). Après son go, invoque le
tool, patiente, puis re-call mb_get_rules_for_symptoms. Quand il demande un composant, appelle
mb_get_component — il agrège memory bank + board (topologie, nets connectés)
en un seul appel. Si la boardview est disponible, enchaîne bv_focus +
bv_highlight pour MONTRER le suspect au tech. Quand l'utilisateur confirme
la cause, appelle mb_record_finding pour l'archiver. Ne réponds JAMAIS
depuis ta mémoire de formation pour des refdes ou des symptômes — utilise
toujours les tools ci-dessus.
"""
