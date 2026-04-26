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

from api.agent.reliability import load_reliability_line
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
        "name": "mb_record_session_log",
        "description": (
            "Write a narrative summary of THIS conversation to the device's "
            "cross-repair log so future sessions on the same device can grep "
            "what was tested / hypothesised / concluded. Distinct from "
            "mb_record_finding (component-grain, only on confirmed cause): "
            "this is conversation-grain and should be called when wrapping "
            "up — user pauses, fix is confirmed, escalates, or session ends "
            "without conclusion. Idempotent on (repair_id, conv_id): "
            "re-calls overwrite. Mirrored to "
            "/mnt/memory/wrench-board-{slug}/conversation_log/."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symptom": {
                    "type": "string",
                    "description": "1-line restatement of the user-reported symptom that drove this session.",
                },
                "outcome": {
                    "type": "string",
                    "enum": ["resolved", "unresolved", "paused", "escalated"],
                    "description": (
                        "resolved = fix confirmed; unresolved = ended without "
                        "conclusion; paused = user will resume; escalated = "
                        "beyond bench scope (board-replace, vendor RMA)."
                    ),
                },
                "tested": {
                    "type": "array",
                    "description": "Probes/inspections done. Empty list OK.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "target": {
                                "type": "string",
                                "description": "'rail:<label>' | 'comp:<refdes>' | 'pin:<refdes>:<pin>'",
                            },
                            "result": {
                                "type": "string",
                                "description": "Free-form short verdict: 'normal', 'dead', '0V', 'shorted', 'open', 'hot', 'noisy', '3.27V (nom 3.30V)', …",
                            },
                        },
                        "required": ["target", "result"],
                    },
                },
                "hypotheses": {
                    "type": "array",
                    "description": "Suspect refdes considered during the session, with verdict.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "refdes": {"type": "string"},
                            "verdict": {
                                "type": "string",
                                "enum": ["confirmed", "rejected", "inconclusive"],
                            },
                            "evidence": {
                                "type": "string",
                                "description": "One short sentence — the measurement or reasoning that drove the verdict.",
                            },
                        },
                        "required": ["refdes", "verdict"],
                    },
                },
                "findings": {
                    "type": "array",
                    "description": "report_id values returned by mb_record_finding during this session — link them so the narrative cross-references the canonical findings.",
                    "items": {"type": "string"},
                },
                "next_steps": {
                    "type": "string",
                    "description": "If outcome=unresolved or paused: what the next session should pick up.",
                },
                "lesson": {
                    "type": "string",
                    "description": "One-line takeaway for future repairs on this device. Most useful field for grep-based recall.",
                },
            },
            "required": ["symptom", "outcome"],
        },
    },
    {
        "type": "custom",
        "name": "mb_schematic_graph",
        "description": (
            "Interrogate the compiled electrical graph (rails, ICs, enable "
            "signals, boot sequence). Deterministic, no LLM cost. Use BEFORE "
            "speculating on power topology. Queries: "
            "'rail'+label → source_refdes, enable_net, consumers, boot_phase; "
            "'component'+refdes → pins, rails_produced, rails_consumed, "
            "boot_phase; "
            "'downstream'+refdes → transitive loss-of-power set if that "
            "component dies; "
            "'boot_phase'+index → that phase's rails+components; "
            "'list_rails'/'list_boot' → brief catalogues; "
            "'critical_path' → SPOFs ranked by blast_radius + critical gate "
            "per boot phase. Use BEFORE picking the first probe point on a "
            "dead rail; "
            "'net'+label → domain + touching components; "
            "'net_domain'+domain ('hdmi','usb','audio'…) → nets in that "
            "domain + top-3 suspect refdes. Use when the tech describes a "
            "symptom by function (e.g. 'HDMI black screen', 'USB-C dead'). "
            "Returns {found:false, reason:'no_schematic_graph'} if not "
            "ingested — don't retry."
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
            "Propose hypotheses (refdes, mode) that explain the "
            "observations. IC modes (active): dead (inert), alive "
            "(working), anomalous (powered but wrong output — DSI "
            "bridge IC, audio codec, sensor), hot (running abnormally "
            "warm). PASSIVE modes (R/C/D/FB): open (broken circuit, "
            "typically burnt ferrite or cracked R), short (plate-to-"
            "plate for a cap, wire for an R). Q modes (MOSFET/BJT): "
            "open / short (physical), stuck_on / stuck_off "
            "(behavioural: always conducting / never conducting). RAIL "
            "modes: dead, alive, shorted, stuck_on (rail powered when "
            "it should be off). Pass at least one observation via "
            "state_comps / state_rails OR provide repair_id to "
            "synthesise from the journal. The response contains "
            "`discriminating_targets` (list[str]): when top-N "
            "candidates tie on score, these are the refdes/rails whose "
            "next measurement best partitions the suspects — surface "
            "them to the tech."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "state_comps": {
                    "type": "object",
                    "description": (
                        "Map refdes → mode. For an IC: 'dead', 'alive', "
                        "'anomalous', 'hot'. For a passive (R/C/D/FB): "
                        "'open', 'short', 'alive'. For a passive_q "
                        "(MOSFET/BJT): 'open', 'short', 'stuck_on', "
                        "'stuck_off', 'alive'. The engine rejects an IC "
                        "in passive mode (and vice versa)."
                    ),
                    "additionalProperties": {
                        "type": "string",
                        "enum": [
                            "dead", "alive", "anomalous", "hot",
                            "open", "short",
                            "stuck_on", "stuck_off",
                        ],
                    },
                },
                "state_rails": {
                    "type": "object",
                    "description": (
                        "Map rail label → mode. Modes: 'dead' (0V), "
                        "'alive' (nominal), 'shorted' (short to GND or "
                        "overvolt), 'stuck_on' (powered when it should "
                        "be off — blown load switch downstream)."
                    ),
                    "additionalProperties": {
                        "type": "string",
                        "enum": ["dead", "alive", "shorted", "stuck_on"],
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
            "Record an electrical measurement from the tech into the "
            "repair-session journal. Target format 'rail:<label>' | "
            "'comp:<refdes>' | 'pin:<refdes>:<pin>'. Unit ∈ "
            "{V, A, W, °C, Ω, mV}. If nominal is provided, the mode is "
            "auto-classified (alive/anomalous/dead/shorted/hot)."
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
        "description": "Re-read the repair-session measurement journal, optionally filtered by target and/or timestamp.",
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
        "description": "Before/after diff of a given target (oldest measurement vs latest by default).",
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
        "description": "Synthesise an Observations payload (state + metrics) from the measurement journal — latest event per target.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "custom",
        "name": "mb_set_observation",
        "description": (
            "Force an observation mode for a target without recording a "
            "value (useful when the tech says 'U7 is dead' without a "
            "measurement). Emits the WS event for the UI. "
            "Modes per kind: rail ∈ {dead, alive, shorted, stuck_on}. "
            "IC ∈ {dead, alive, anomalous, hot}. Passive (R/C/D/FB) ∈ "
            "{open, short, alive}. Passive_q (MOSFET/BJT) ∈ {open, short, "
            "stuck_on, stuck_off, alive}. The server rejects a mode that "
            "is inconsistent with the target's kind."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": [
                        "dead", "alive", "anomalous", "hot", "shorted",
                        "stuck_on", "stuck_off", "open", "short",
                    ],
                },
            },
            "required": ["target", "mode"],
        },
    },
    {
        "type": "custom",
        "name": "mb_clear_observations",
        "description": "Clear the visual observation state on the UI side (the journal is preserved).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "custom",
        "name": "mb_validate_finding",
        "description": (
            "Record the culprit component(s) confirmed by the tech at "
            "the end of a repair. Call ONLY after a 'Marquer fix' "
            "trigger has been received AND the fixes are confirmed "
            "(no auto-validation on ambiguous context). `fixes` is a "
            "list of objects "
            "{refdes, mode ∈ (dead|alive|anomalous|hot|shorted|passive_swap), rationale}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fixes": {
                    "type": "array",
                    "description": "List of components fixed during the repair.",
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
        "name": "bv_scene",
        "description": (
            "Compose a diagnostic scene on the board in ONE call: "
            "reset, highlights, annotations, arrows, focus, dim. "
            "PREFER this tool whenever you want to show several "
            "elements tied to the same hypothesis (e.g. highlight 3 "
            "PMICs + annotate their role + draw an arrow from the "
            "suspect to its rail). Sub-ops run in the order reset → "
            "highlights → annotations → arrows → focus → dim and emit "
            "a single group of events. The atomic tools "
            "(bv_highlight, bv_annotate, bv_draw_arrow…) remain for "
            "isolated actions (one refdes, one gesture). Soft cap: "
            "~10 highlights, 10 annotations, 5 arrows per scene."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reset": {
                    "type": "boolean",
                    "default": False,
                    "description": "Clear all overlays before applying the scene.",
                },
                "highlights": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "refdes": {
                                "oneOf": [
                                    {"type": "string"},
                                    {"type": "array", "items": {"type": "string"}},
                                ],
                            },
                            "color": {
                                "type": "string",
                                "enum": ["accent", "warn", "mute"],
                                "default": "accent",
                            },
                        },
                        "required": ["refdes"],
                    },
                },
                "annotations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "refdes": {"type": "string"},
                            "label": {"type": "string"},
                        },
                        "required": ["refdes", "label"],
                    },
                },
                "arrows": {
                    "type": "array",
                    "description": (
                        "Directional arrows refdes→refdes. Include them "
                        "EVERY TIME the scene describes a directed relation: "
                        "boot order, signal path, power propagation, fault "
                        "cascade, upstream→downstream causation. One arrow "
                        "per hop (e.g. boot PMIC→SoC→DRAM = 2 arrows). A "
                        "scene without arrows is a static highlight; a scene "
                        "WITH arrows tells the story the tech needs to see."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "from_refdes": {"type": "string"},
                            "to_refdes": {"type": "string"},
                        },
                        "required": ["from_refdes", "to_refdes"],
                    },
                },
                "focus": {
                    "type": "object",
                    "properties": {
                        "refdes": {"type": "string"},
                        "zoom": {"type": "number", "default": 1.4},
                    },
                    "required": ["refdes"],
                },
                "dim_unrelated": {"type": "boolean", "default": False},
            },
        },
    },
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
                "zoom": {"type": "number", "default": 1.4},
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
        "description": (
            "Draw a directional arrow on the PCB from one refdes to another. "
            "Materializes a directed relation the tech needs to SEE: signal "
            "path, power propagation, boot dependency, causation chain, "
            "upstream→downstream link. RULE: every time your reply describes "
            "such a relation in words (\"the rail comes from U2\", \"U7 "
            "drives the SoC\", \"boot order: PMIC → SoC → DRAM\"), draw the "
            "matching arrow(s) — one per hop. Words alone aren't enough on "
            "this UI; the diagram is the explanation. For multi-hop chains "
            "or arrows combined with highlights/annotations, prefer "
            "bv_scene.arrows in ONE call. Use bv_draw_arrow only for an "
            "isolated single-hop gesture."
        ),
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


PROTOCOL_TOOLS: list[dict] = [
    {
        "type": "custom",
        "name": "bv_propose_protocol",
        "description": (
            "Emit an ordered, typed diagnostic protocol that the UI "
            "renders visually (floating cards on the board + side "
            "wizard, or inline cards when no board). Each step has a "
            "type (numeric/boolean/observation/ack), a target refdes, "
            "an instruction and a rationale. Call this tool ONLY after "
            "matching a rule (confidence ≥ 0.6) or identifying ≥ 2 "
            "likely_causes. Typical trigger: when the tech, suspect "
            "already identified, asks 'how do I find / locate / test / "
            "track it down', emit the hunt protocol (3-6 steps) rather "
            "than explaining in prose. ONE active protocol at a time — "
            "re-emitting replaces the previous one. Cap: 12 steps. "
            "REQUIRES TECH CONFIRMATION: the proposal is shown in a "
            "modal first; the tech accepts or rejects before the "
            "protocol is materialised. On reject, the tool call returns "
            "is_error with the tech's reason — do not re-emit the same "
            "plan; ask a clarifying question or propose a different "
            "approach."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "rationale": {"type": "string"},
                "rule_inspirations": {
                    "type": "array", "items": {"type": "string"},
                },
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["numeric", "boolean", "observation", "ack"],
                            },
                            "target": {"type": ["string", "null"]},
                            "test_point": {"type": ["string", "null"]},
                            "instruction": {"type": "string"},
                            "rationale": {"type": "string"},
                            "unit": {"type": ["string", "null"]},
                            "nominal": {"type": ["number", "null"]},
                            "pass_range": {
                                "type": ["array", "null"],
                                "items": {"type": "number"},
                                "minItems": 2, "maxItems": 2,
                            },
                            "expected": {"type": ["boolean", "null"]},
                        },
                        "required": ["type", "instruction", "rationale"],
                    },
                },
            },
            "required": ["title", "rationale", "steps"],
        },
    },
    {
        "type": "custom",
        "name": "bv_update_protocol",
        "description": (
            "Modify the active protocol: insert (new step after an "
            "anchor), skip (the tech lacks the tool or you decide to "
            "pass), replace_step (a pending step that no longer makes "
            "sense), reorder (the pending steps — the active step "
            "stays first), complete_protocol (everything done, give a "
            "1-sentence verdict), abandon_protocol (the tech declines). "
            "reason is REQUIRED and will be logged in the history."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "insert", "skip", "replace_step", "reorder",
                        "complete_protocol", "abandon_protocol",
                    ],
                },
                "reason": {"type": "string"},
                "step_id": {"type": ["string", "null"]},
                "after": {"type": ["string", "null"]},
                "new_step": {"type": ["object", "null"]},
                "new_order": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                },
                "verdict": {"type": ["string", "null"]},
            },
            "required": ["action", "reason"],
        },
    },
    {
        "type": "custom",
        "name": "bv_record_step_result",
        "description": (
            "Persist a step result yourself (useful when the tech "
            "reports the value in chat rather than via the UI: "
            "'VBUS = 4.8V'). For numeric, value is a number + unit. "
            "For boolean, value is true/false. For observation, value "
            "is text. For ack, value=null. skip_reason set = step "
            "marked skipped without a measurement. The state machine "
            "then auto-advances to the next pending step."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "step_id": {"type": "string"},
                "value": {},
                "unit": {"type": ["string", "null"]},
                "observation": {"type": ["string", "null"]},
                "skip_reason": {"type": ["string", "null"]},
            },
            "required": ["step_id"],
        },
    },
    {
        "type": "custom",
        "name": "bv_get_protocol",
        "description": (
            "Read the full active protocol (steps, statuses, results, "
            "history). Use when resuming a session or when you suspect "
            "state drift after a disconnect. Returns {active: false} "
            "if no protocol is active."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


CAM_TOOLS: list[dict] = [
    {
        "type": "custom",
        "name": "cam_capture",
        "description": (
            "Acquire a still frame from the technician's selected camera "
            "(microscope, webcam, etc.). Use when you need a fresh visual "
            "on a specific component or anomaly. The tech has already "
            "framed and focused — no parameters needed beyond an optional "
            "reason for traceability. Returns the captured image as a "
            "tool_result the model can read directly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Brief reason for the capture (logged, not shown to the tech).",
                }
            },
        },
    },
]


CONSULT_TOOLS: list[dict] = [
    {
        "type": "custom",
        "name": "consult_specialist",
        "description": (
            "Delegate a focused question to a specialist sub-agent on a "
            "different model tier. Use this when your tier is too shallow "
            "(Haiku on multi-step electrical reasoning) or you want a quick "
            "second opinion (Opus consulting Sonnet for a sanity check). "
            "The sub-agent runs as an isolated Managed-Agent session, "
            "returns only its final text answer, and CANNOT call any tool — "
            "tell it everything it needs in `context`. The call costs a "
            "separate session-hour on the chosen tier. Avoid recursive "
            "consultations. Good triggers: 'this needs Opus-grade reasoning', "
            "'check my hypothesis with a second model'. Bad triggers: "
            "'classify this string' (too cheap), 'continue the conversation' "
            "(use your own turn)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tier": {
                    "type": "string",
                    "enum": ["fast", "normal", "deep"],
                    "description": (
                        "Specialist tier. `fast`=Haiku 4.5 (cheap quick "
                        "lookups), `normal`=Sonnet 4.6 (balanced), "
                        "`deep`=Opus 4.7 (best multi-step reasoning). "
                        "Don't pick your own tier — the dispatcher will reject "
                        "self-consultation."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": "The focused question for the specialist.",
                },
                "context": {
                    "type": "string",
                    "description": (
                        "Self-contained briefing: device, symptoms, prior "
                        "measurements, hypotheses already ruled out. The "
                        "sub-agent has no access to your tools or memory."
                    ),
                },
            },
            "required": ["tier", "query"],
        },
    },
]


def build_tools_manifest(session: SessionState) -> list[dict]:
    """Return the tools list for `session` in DIRECT mode. `profile_*` and
    `protocol_*` always present; `bv_*` only when a board is loaded; `cam_*`
    only when the frontend reported a camera available.

    `consult_specialist` is intentionally absent here — escalation between
    tiers requires the Managed Agents control plane (separate agent IDs per
    tier, persisted in `managed_ids.json`). Direct mode runs a single
    `messages.create` loop with no peer tiers to consult, so exposing the
    tool would let the agent call something with no dispatcher behind it.
    The MA runtime bakes CONSULT_TOOLS into each tier-scoped agent at
    bootstrap time (see `scripts/bootstrap_managed_agent.py`)."""
    manifest: list[dict] = list(MB_TOOLS) + list(PROFILE_TOOLS) + list(PROTOCOL_TOOLS)
    if session.board is not None:
        manifest.extend(BV_TOOLS)
    if session.has_camera:
        manifest.extend(CAM_TOOLS)
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
    reliability_line = load_reliability_line(device_slug)
    reliability_block = (
        f"\n{reliability_line}\n"
        if reliability_line
        else ""
    )
    return f"""\
You are a calm, methodical board-level diagnostics assistant for a
microsoldering technician. Address the technician directly, in a
direct and pedagogical tone.

Current device: {device_slug}.
{reliability_block}
{technician_block}

Capabilities for this session:
  - memory bank ✅ (mb_get_component, mb_get_rules_for_symptoms, mb_record_finding, mb_record_session_log, mb_expand_knowledge)
  - profile ✅ (profile_get, profile_check_skills, profile_track_skill)
  - filesystem ✅ (read, write, edit, grep, glob — for the /mnt/memory/ mounts)
  - boardview {boardview_status}
  - schematic {schematic_status}

ANTI-HALLUCINATION RULE: NEVER mention a refdes (U7, C29, J3100…)
without validating it via mb_get_component. If the tool returns
{{found: false, closest_matches: [...]}}, propose one of those
closest_matches or ask for clarification — NEVER invent. Unvalidated
refdes are automatically wrapped ⟨?U999⟩ in the final reply by the
post-hoc sanitizer — debug signal, not an excuse.

Every user message in this conversation is prefixed by a passive tag
`[ctx · device=… · initial_complaint="…"]` — that is intake-form
metadata, **NOT a fresh symptom declaration**. Do NOT (re-)trigger
mb_get_rules_for_symptoms / mb_expand_knowledge because of this tag
EXCEPT at the very start of the conversation (no prior turn in the
history) or when the tech types a complaint distinct from
initial_complaint. On a resume where context is established, **pick
up the thread** without re-running the search.

When the tech describes a new symptom, call
mb_get_rules_for_symptoms to fetch the applicable rules.
Before proposing an action plan, call profile_check_skills with the
skills the plan involves — adapt your level of detail and avoid
actions whose tools are not available. When the tech confirms they
performed a step successfully, call profile_track_skill (concrete
evidence — refdes, symptom, gesture — NEVER a vague summary).

If mb_get_rules_for_symptoms returns 0 matches on a serious symptom,
PROPOSE mb_expand_knowledge to the tech (briefly: target a focused
Scout pass, ~30s, ~$0.40 in tokens, ask for go-ahead). DO NOT launch
until the tech agrees. After their go-ahead, invoke the tool, wait,
then re-call mb_get_rules_for_symptoms. When they ask about a
component, call mb_get_component. If the boardview is available and
you want to show several elements at once (highlights + annotations
+ arrows for the same hypothesis), use `bv_scene` in ONE call —
atomic tools (bv_highlight, bv_focus, bv_annotate alone) only for an
isolated action. When the tech confirms the cause, call
mb_record_finding. NEVER answer from your training memory for
refdes or symptoms — always use the tools above.

ARROWS — draw causation, do not just describe it. The boardview is
the demo surface; words alone don't earn it. Whenever your reply
describes a directed relation — boot order, signal path, power
propagation, fault cascade, upstream→downstream dependency — you
MUST draw the matching arrows on the board, one per hop. Examples:
  - "Boot order: PMIC U1 → SoC U2 → DRAM U3" → 2 arrows (U1→U2,
    U2→U3) inside a `bv_scene` that also highlights the three.
  - "VBUS comes from J1, filtered by L4, sinks into U7" → 2 arrows
    (J1→L4, L4→U7).
  - "C29 short on the 3V3 rail collapses U2's supply" → arrow
    C29→U2.
A scene without arrows when you described a flow IS a regression;
do not skip them to save tokens. Use bv_scene.arrows for any
multi-hop / combined gesture, bv_draw_arrow for one isolated hop.

STYLE. Write like a bench engineer typing fast: short sentences, no
emoji, no polite opener, no verbose bullet list when 2 lines
suffice. Pro jargon allowed (PMIC, BGA reball, cold joint, reflow),
no gratuitous beginner-talk. When you cite a refdes, always in
monospace-style uppercase (U7, C156).

HYPOTHESIZE — reading the response.
The `mb_hypothesize` tool returns `hypotheses` sorted by descending
score + `discriminating_targets` (list).

  - Top-1 detached (score ≥ 2× the next) → present it directly, cite
    the mode physically (not just "C156 short" but explicitly
    plate-to-plate breakdown of C156), then chain MEASURE-TARGET
    (§ next) to validate before replacement.
  - Top-N tie → don't list 5 candidates, take
    `discriminating_targets` and chain MEASURE-TARGET on each one.
  - `discriminating_targets=[]` → no ambiguity, top-1.

Passive modes (Phase 4):
  - `short` on a passive_c = plate-to-plate breakdown, rail shorted
  - `open` on a passive_fb = burnt ferrite, downstream rail dead
  - `open` on a passive_r role=feedback = open divider, rail goes
    overvoltage
  - `open` on a passive_r role=pull_up/pull_down = signal floats
  - `short` on a passive_c role=filter/decoupling = same pattern as
    decoupling short

The passive scoring has a 0.5× multiplier by design on
topologically weak cascades (decoupling/bulk/filter open,
pull_up/down open). A 0.5 score on a passive = LEGITIMATE candidate,
not weak.

Q modes (Phase 4.5):
  - `open` or `stuck_off` on a Q = broken channel (never conducts).
    On a load_switch = downstream rail dead.
    On an inrush_limiter = rail never comes up.
  - `short` or `stuck_on` on a Q = stuck channel (conducts always).
    On a load_switch = downstream rail always powered, even in
    standby (typical standby-current fault).
    On a level_shifter = bus stuck at a logic level.
  - On a flyback_switch (main Q of a buck/boost SMPS, pin on SW1/SW2):
    `open` = SMPS no longer switches → downstream rail dead;
    `short` / `stuck_on` = D-S stuck = continuous current through the
    inductor → input PVIN rail stressed and source hot.
  - On a cell_protection (Q in series with a cell / pack, pins on
    BATn / BATnFUSED): `open` / `stuck_off` = cell disconnected →
    fused rail on the pack side dead; `short` / `stuck_on` = no
    protection (observable only on overload / cell imbalance, not
    directly on a rail).
  - On a cell_balancer (Q + R for passive balancing, pins on BATn
    repeated): modes not observable from a rail. Useful as a
    physical inspection target when one cell drifts alone in BMS
    telemetry.
  - `stuck_on` on a rail = direct observation: "+3V3_USB at 3.3V in
    standby when it should be off". Engine proposes an upstream Q
    stuck_on as suspect.

The vocabulary open/short and stuck_on/stuck_off overlaps on Q's:
the two pairs describe the same cascade (open/stuck_off = broken
channel, short/stuck_on = stuck channel). Use the word that matches
the tech's observation: if they ohmmeter'd D-S and saw 0Ω, say
"short". If they observed the rail still on in standby, say
"stuck_on". The engine treats them equivalently.

MEASURE-TARGET — never a vague "measure U1".
When you suggest a measurement (discriminator or top-1 validation),
you MUST first call `mb_get_component(refdes)` to fetch the pin
list with their `role` and `net_label`. Then select ONE useful pin:

  - If the refdes is an IC/PMIC and you are checking whether the
    rail arrives: pin with role=`power_in` on that rail. Tell the
    tech to ohm-meter between pin N (power_in +5V) and GND on U1,
    expected ~9-50kΩ with power off; near-zero resistance =
    confirmed short.
  - If the refdes is suspected hot/shorted: `power_in` supply pin,
    tech holds a hand on the package under a current-limited PSU
    (500mA), spots which one warms up in 5-10s.
  - If validating a signal (anomalous): `signal_out` or
    `clock_out` pin, scope or multimeter in AC.
  - If pin not found or all-BGA (inaccessible): say so, propose
    injecting limited current through the rail's input and use
    thermal / touch to locate, OR say we move to another lead.

If the boardview is loaded, chain
`bv_show_pin(refdes=..., pin=N)` to highlight it visually. No
boardview = no problem, the tech reads the refdes + pin number.

Typical measurement-suggestion format:
  ohm-meter, pin 3 of U1 (power_in +5V) to GND. Expected with
  power off: a few kΩ. Hard short (<1Ω) = U1 or its decoupling is
  the cause.

ANTI-GENERIC. Avoid boilerplate (thermal camera, discoloration,
burnt smell). Propose ONE precise test at a time, not a list of
three options. The tech may not have a thermal camera — ask what
they have before assuming. If the default scope is a multimeter +
a current-limited PSU + a hand, stay there.

PROTOCOL — display a stepwise diagnostic visually.

You have 4 tools dedicated to a guided diagnostic protocol that
the UI renders on the board (numbered badges on the components +
floating card + side wizard):

  - bv_propose_protocol(title, rationale, steps) — emit a typed
    plan of N steps (N ≤ 12). Call it ONLY after matching a rule
    (confidence ≥ 0.6) OR identifying ≥ 2 likely_causes via
    mb_hypothesize. Not on the first turn, except for an obvious
    symptom.
  - bv_update_protocol(action, reason, …) — insert / skip /
    replace_step / reorder / complete_protocol /
    abandon_protocol. Use when a result forces you to revise the
    plan. reason is REQUIRED and becomes visible in the tech's
    history.
  - bv_record_step_result(step_id, value, unit?, observation?, skip_reason?)
    — when the tech reports the result in CHAT instead of the UI
    ("VBUS = 4.8V", "no, D11 off"), YOU call this tool. The state
    machine advances and emits the event to the frontend.
  - bv_get_protocol() — read-only, to fetch full state on resume /
    suspected drift.

When the tech submits a result via the UI you receive a message
[step_result] step=… target=… value=… outcome=pass|fail|skipped ·
plan: N steps, current=… on the next turn. If outcome=pass and
the plan continues you may stay silent (let the tech move on) or
narrate one line summarising the pass and naming the next
target. If outcome=fail, analyse and use bv_update_protocol to
insert / skip / reorder.

If the tech says "no protocol" / "let's chat" / "no steps" or
similar, do not emit. Stay in free chat mode as before.

TIER. When you are running on tier=fast (Haiku), you are
under-sized for complex diagnostics (long tail, dense schematic).
If you sense the lead is getting dense (3+ near-tied hypotheses,
ambiguous nets, designer notes to interpret), flag it to the
tech and recommend they switch to a higher tier (normal or
deep). The tech will reconnect their WS with the new tier.
"""
