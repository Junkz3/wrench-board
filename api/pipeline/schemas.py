"""Pydantic V2 schemas for the knowledge generation pipeline.

Every structured output of Phases 2–4 is declared here. These classes double as:
- Runtime validators for tool outputs (via `Class.model_validate(...)`)
- JSON Schema sources for the forced-tool definitions (via `Class.model_json_schema()`)
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal, TypeVar, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ======================================================================
# T8 — Provenance & identifiants stricts
# ======================================================================

# --- Patterns regex pour les identifiants canoniques ---

_CANONICAL_NAME_PATTERN = r"^[A-Z0-9_./-]{2,64}$"
_REFDES_PATTERN = r"^[A-Z]{1,3}[0-9]{1,5}[A-Z]?$"
_RULE_ID_PATTERN = r"^R-[A-Z0-9_-]{1,48}$"
_NODE_ID_PATTERN = r"^N-[A-Z0-9_-]{1,48}$"

# --- Enums fermés pour les champs kind/relation ---

_ComponentKind = Literal[
    "MOSFET", "IC", "PMIC", "CAPACITOR", "RESISTOR", "CONNECTOR",
    "INDUCTOR", "DIODE", "FUSE", "SWITCH", "CRYSTAL", "COIL", "OTHER",
]

# Exporté pour les modules qui doivent discriminer composant vs signal sans
# dupliquer la liste (cf. pack_storage._derive_fact_id).
# Dérivé de _ComponentKind via get_args : si un kind est ajouté au Literal,
# COMPONENT_KINDS se met à jour automatiquement — pas de rot silencieux.
COMPONENT_KINDS: frozenset[str] = frozenset(get_args(_ComponentKind))

_SignalKind = Literal[
    "POWER_RAIL", "CONTROL", "DATA", "CLOCK", "ANALOG", "REFERENCE", "OTHER",
]
_DeviceKind = Literal[
    "gpu_card", "laptop_logic_board", "phone_logic_board",
    "desktop_motherboard", "sbc_board", "power_charging_board",
    "other", "unknown",
]
# Exported so other modules discriminate without duplicating the list.
DEVICE_KINDS: frozenset[str] = frozenset(get_args(_DeviceKind))


# ======================================================================
# PHASE 1.5 — Device-kind classifier
# ======================================================================


class KindVerdict(BaseModel):
    """Phase 1.5 classifier output — device class inferred from the graph summary."""

    model_config = ConfigDict(extra="forbid")

    device_kind: _DeviceKind = Field(
        description="The single best device class for this board's topology."
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="0–1 self-assessed confidence. <0.6 routes to user confirmation.",
    )
    evidence: str = Field(
        description="One sentence citing the rails/families that decided it (no refdes)."
    )


_NodeKind = Literal["component", "symptom", "net", "test_point"]
_EdgeRelation = Literal["powers", "drives", "senses", "grounds", "shares_net", "caused_by", "indicates"]


class SanitizerAction(BaseModel):
    """Une action loguée par le sanitizer PII sur un champ libre."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(..., description="Nom du champ sanitisé (e.g. 'description').")
    action: Literal[
        "redacted_email",
        "redacted_phone",
        "redacted_serial",
        "redacted_iban",
        "redacted_ip",
        "redacted_customer_mention",
        "dropped_invalid_identifier",
    ]
    count: int = Field(..., ge=1, description="Nombre d'occurrences redactées dans ce champ.")


class Provenance(BaseModel):
    """Métadonnées attachées à chaque fact post-T8 (composant, règle, node, …)."""

    model_config = ConfigDict(extra="forbid")

    expansion_id: str = Field(..., description="ID de l'expansion qui a produit ce fact, ou 'baseline-pre-T8'.")
    added_at: datetime
    added_by_tenant: str | None = Field(default=None, description="Tenant ID (null pour baseline / self-host anonyme).")
    confidence: float = Field(..., ge=0.0, le=1.0)
    source_kind: Literal["baseline", "agent_expansion", "operator_seed"]
    sanitizer_actions: list[SanitizerAction] = Field(default_factory=list)
    status: Literal["baseline", "staged", "promoted", "revoked"] = "staged"


class WithProvenance(BaseModel):
    """Mixin Pydantic : ajoute un champ provenance optionnel aux schémas existants.

    Optionnel pour rétro-compat lecture des packs pré-T8 (la migration attachera
    une provenance synthétique 'baseline-pre-T8', cf. pack_migrate.py — Task 4).

    Toutes les sous-classes héritent de `populate_by_name=True` dans leur
    model_config : ce réglage permet d'utiliser `provenance=` en plus de l'alias
    `_provenance=` lors de la construction Python (les deux formes sont acceptées).
    """

    provenance: Provenance | None = Field(default=None, alias="_provenance")


_T = TypeVar("_T", bound=BaseModel)


def load_with_tolerant_baseline(model_cls: type[_T], raw: dict) -> _T:
    """Charge un fact en mode tolérant si sa provenance dit source_kind=='baseline'.

    Le but : on a durci les patterns d'identifiants en T8, mais les packs
    legacy migrés contiennent des facts qui ne matchent pas. On ne veut pas
    les rejeter en lecture.

    ⚠ CAVEAT IMPORTANT : `model_construct` bypass TOUTE la validation Pydantic
    (patterns regex, appartenance aux Literal, coercion de type, présence des
    champs requis). L'objet retourné peut donc avoir des valeurs qui violent
    le schéma — y compris des types incorrects ou des champs manquants. Le
    caller (typiquement la migration Task 4 ou le loader de baseline) NE DOIT
    PAS supposer la correctness de type/format de l'objet retourné.

    Cette fonction est conçue spécifiquement pour la lecture défensive d'une
    baseline pré-T8 dont on assume que les données sont historiquement saines
    même si elles ne matchent pas les patterns durcis. NE PAS l'utiliser pour
    valider du contenu agent ou tenant.
    """
    prov_raw = raw.get("_provenance") or raw.get("provenance")
    if prov_raw and prov_raw.get("source_kind") == "baseline":
        prov = Provenance.model_validate(prov_raw)
        data = {k: v for k, v in raw.items() if k not in ("_provenance", "provenance")}
        obj = model_cls.model_construct(**data)
        obj.provenance = prov
        return obj
    return model_cls.model_validate(raw)

# ======================================================================
# PHASE 2.5 — Device taxonomy (brand > model > version hierarchy)
# ======================================================================


class DeviceTaxonomy(BaseModel):
    """Hierarchical classification extracted from the raw dump by the taxonomist.

    Every field is nullable — the extractor MUST output null rather than
    invent when a source doesn't state the fact (hard rule #4). Populated
    after the Registry Builder so the writers see the final taxonomy, and
    used by the UI to group devices by brand > model > version.
    """

    model_config = ConfigDict(extra="forbid")

    brand: str | None = Field(
        default=None,
        description=(
            "Manufacturer name as spelled in the sources — 'Apple', 'MNT', "
            "'Raspberry Pi', 'Samsung'. Null when the sources don't name one."
        ),
    )
    model: str | None = Field(
        default=None,
        description=(
            "Product line / model name — 'iPhone X', 'Reform', 'Model B', "
            "'Galaxy S21'. Null when genuinely unspecified."
        ),
    )
    version: str | None = Field(
        default=None,
        description=(
            "Free-form revision or variant: a model-id (A1901), a PCB rev "
            "(Rev 2.0), a generation (Gen 11), or a year (2021). Null otherwise."
        ),
    )
    form_factor: str | None = Field(
        default=None,
        description=(
            "The physical board being worked on — 'motherboard', 'logic board', "
            "'mainboard', 'daughterboard', 'charging board'. Use the term the "
            "community uses most often in the dump."
        ),
    )
    device_kind: _DeviceKind | None = Field(
        default=None,
        description=(
            "Closed device class. Set by the graph-arbitrated classifier (Phase "
            "1.5), reconciled with the technician's declared kind. Null only when "
            "no graph and no declaration exist."
        ),
    )


# ======================================================================
# PHASE 2 — Registry (the canonical glossary)
# ======================================================================


class RefdesCandidate(BaseModel):
    """A graph refdes proposed as a match for a registry canonical_name.

    Emitted only when the Registry Builder is given an `ElectricalGraph`
    at phase-2 time (technician supplied a schematic). Each candidate
    must justify its mapping in `evidence` — either by quoting a source
    that ties the canonical to the refdes (via MPN / datasheet) or by
    citing an inference from a technician-supplied BOM. Never fabricate.
    """

    model_config = ConfigDict(extra="forbid")

    refdes: Annotated[str, Field(pattern=_REFDES_PATTERN, description="Refdes from the supplied ElectricalGraph (e.g. U7, C29, J1).")]
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Subjective confidence in this canonical→refdes mapping.",
    )
    evidence: str = Field(
        description=(
            "One sentence justifying the mapping. Either a paraphrased quote "
            "from the dump (with URL when available) or 'inference from BOM "
            "MPN match' / 'inference from schematic MPN match'. Never empty."
        ),
        min_length=4,
    )


class RegistryComponent(WithProvenance):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    canonical_name: Annotated[str, Field(
        pattern=_CANONICAL_NAME_PATTERN,
        description=(
            "The primary identifier. Must be an uppercase refdes or signal name "
            "(e.g. U7, C29, PP3V3_MAIN). Pattern: [A-Z0-9_./-]{2,64}."
        ),
    )]
    logical_alias: str | None = Field(
        default=None,
        description=(
            "A human-readable logical name, used when canonical_name is a cryptic refdes. "
            "Null if canonical_name is already human-readable."
        ),
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="Other names by which this component is known in the sources.",
    )
    kind: _ComponentKind = "OTHER"
    description: str = Field(
        default="",
        description="One sentence describing the role of the component.",
    )
    refdes_candidates: list[RefdesCandidate] | None = Field(
        default=None,
        description=(
            "Graph refdes candidates that match this canonical_name, emitted "
            "only when an ElectricalGraph is supplied at registry time. Each "
            "candidate carries its own evidence. Null on legacy packs and on "
            "any pipeline run where the technician did not supply a schematic."
        ),
    )


class RegistrySignal(WithProvenance):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    canonical_name: Annotated[str, Field(
        pattern=_CANONICAL_NAME_PATTERN,
        description="Canonical name of the signal/net/rail (e.g. PP3V3_MAIN, VDD_CORE, USB_DP1). Pattern: [A-Z0-9_./-]{2,64}.",
    )]
    aliases: list[str] = Field(default_factory=list)
    kind: _SignalKind = "OTHER"
    nominal_voltage: float | None = Field(
        default=None,
        description="Nominal voltage in V if applicable (e.g. 3.3 for 3V3_RAIL). Null otherwise.",
    )


class Registry(BaseModel):
    """Phase 2 output — the canonical vocabulary all downstream writers must respect."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    device_label: str = Field(
        description="Human-readable device identifier (e.g. 'MNT Reform motherboard')."
    )
    taxonomy: DeviceTaxonomy = Field(
        default_factory=DeviceTaxonomy,
        description=(
            "Hierarchical classification (brand > model > version > form_factor). "
            "Fields are individually nullable — leave null when the sources don't "
            "state the fact rather than guessing (hard rule #4)."
        ),
    )
    components: list[RegistryComponent] = Field(default_factory=list)
    signals: list[RegistrySignal] = Field(default_factory=list)


# ======================================================================
# PHASE 2.5 — Refdes Mapper (canonical_name → graph refdes attribution)
# ======================================================================
#
# Runs only when an ElectricalGraph is loaded for the device. Output is
# server-side-validated against three deterministic rules before persist:
#   1. evidence_quote is a literal substring of the raw dump,
#   2. for literal_refdes_in_quote: refdes appears literally in evidence_quote,
#   3. for mpn_match_in_quote: graph.components[refdes].value.mpn appears
#      literally in evidence_quote (MPN comes only from the graph — the
#      LLM cannot invent it).
# Failed attributions are dropped, not retried. An empty mapping is a
# valid output. See docs/superpowers/specs/2026-04-25-refdes-mapper-agent.md.


EvidenceKind = Literal[
    "literal_refdes_in_quote",
    "mpn_match_in_quote",
]


class RefdesAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_name: str = Field(
        description=(
            "Must match a `canonical_name` of a component in the registry "
            "supplied to the Mapper. Otherwise the attribution is dropped."
        ),
    )
    refdes: str = Field(
        description=(
            "Must exist in `graph.components`. Otherwise dropped. The mapper "
            "MUST NOT invent a refdes that is not in the supplied graph."
        ),
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Subjective confidence in this attribution. Use ~0.95 for direct "
            "literal refdes mentions, ~0.85 for MPN matches, lower as evidence "
            "thins."
        ),
    )
    evidence_kind: EvidenceKind = Field(
        description=(
            "How the attribution is justified. Closed enum — the only two "
            "legitimate kinds are direct literal refdes mention OR MPN match. "
            "Topology / rail-overlap / functional similarity are NOT valid."
        ),
    )
    evidence_quote: str = Field(
        min_length=30,
        max_length=600,
        description=(
            "A literal substring of the raw research dump (≥30 chars) that "
            "supports the attribution. For `literal_refdes_in_quote` the "
            "refdes must appear in this quote (case-insensitive). For "
            "`mpn_match_in_quote` the graph's MPN for this refdes must appear "
            "in this quote (case-sensitive). Server validates both literally."
        ),
    )
    reasoning: str = Field(
        max_length=240,
        description=(
            "One sentence explaining why this canonical→refdes mapping holds. "
            "E.g. 'dump quote mentions the LM2677 buck; graph U7.value.mpn is "
            "LM2677SX-5'."
        ),
    )


class RefdesMappings(BaseModel):
    """Phase 2.5 output — typed canonical→refdes attributions, persisted as
    `memory/{slug}/refdes_attributions.json`."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    device_slug: str
    attributions: list[RefdesAttribution] = Field(default_factory=list)


# ======================================================================
# Symptom coverage checker — Haiku classifier that decides whether a
# newly-reported repair symptom is already covered by an existing rule
# in the device pack. When it is (confidence ≥ threshold), the pipeline
# skips the expand-pack round-trip and the UI can surface the matched
# rule immediately instead of waiting on an LLM call.
# ======================================================================


class CoverageCheck(BaseModel):
    """Output of `api.pipeline.coverage.check_symptom_coverage`.

    Emitted via a forced-tool Haiku call — the classifier reads the
    technician's new symptom and the `rules.json` symptoms, returns a
    typed verdict. Failed / empty / no-rules pack returns a default
    `covered=False, confidence=0.0` result so the caller can treat it
    as "not covered, proceed with expand"."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    covered: bool = Field(
        description=(
            "True iff the new symptom effectively duplicates or narrows "
            "an existing rule's symptom — paraphrases count. False when "
            "the new symptom describes a distinct failure mode the pack "
            "has never captured."
        ),
    )
    matched_rule_id: str | None = Field(
        default=None,
        description=(
            "Stable id of the best-matching existing rule, e.g. "
            "'rule-tristar-no-charge-001'. Set only when covered=True "
            "AND confidence ≥ 0.7 — otherwise null."
        ),
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "0.0 for unrelated, 0.5-0.7 for partial overlap, 0.7-0.9 for "
            "paraphrase, 0.9+ for exact / near-exact match."
        ),
    )
    reason: str = Field(
        description=(
            "One sentence explaining the match or its absence — e.g. "
            "'matches rule-tristar-001, both describe no-charge on adapter "
            "connect'. Surface to the technician in the UI."
        ),
        max_length=400,
    )


# ======================================================================
# PHASE 3 — Writer outputs
# ======================================================================


# --- Writer 1 — Cartographe -----------------------------------------------------


class KnowledgeNode(WithProvenance):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: Annotated[str, Field(pattern=_NODE_ID_PATTERN, description="Stable identifier for this node (e.g. 'N-U7', 'N-3V3-DEAD'). Pattern: N-[A-Z0-9_-]{1,48}.")]
    kind: _NodeKind
    label: str
    properties: dict[str, str] = Field(
        default_factory=dict,
        description="Free-form key/value properties. Values must be strings.",
    )


class KnowledgeEdge(WithProvenance):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source_id: Annotated[str, Field(pattern=_NODE_ID_PATTERN)]
    target_id: Annotated[str, Field(pattern=_NODE_ID_PATTERN)]
    relation: _EdgeRelation


class KnowledgeGraph(BaseModel):
    """Phase 3 Writer 1 (Cartographe) output — typed graph of the device domain."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    nodes: list[KnowledgeNode] = Field(default_factory=list)
    edges: list[KnowledgeEdge] = Field(default_factory=list)


# --- Writer 2 — Clinicien ------------------------------------------------------


class Cause(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refdes: str = Field(description="canonical_name from the registry. Must match exactly.")
    probability: float = Field(ge=0.0, le=1.0)
    mechanism: str = Field(
        description="Short phrase describing how this component fails (e.g. 'short-to-ground')."
    )


class DiagnosticStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(description="Concrete action, e.g. 'measure 3V3_RAIL at TP18'.")
    expected: str | None = Field(
        default=None,
        description="Expected value or range, e.g. '3.3V ± 5%'. Null if informational.",
    )


class Rule(WithProvenance):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: Annotated[str, Field(pattern=_RULE_ID_PATTERN, description="Stable identifier, e.g. 'R-REFORM-001'. Pattern: R-[A-Z0-9_-]{1,48}.")]
    symptoms: list[str] = Field(min_length=1)

    @field_validator("id", mode="before")
    @classmethod
    def _normalize_id(cls, v: object) -> object:
        """Normalize LLM drift on the rule id BEFORE the pattern check: the
        Clinicien writer often emits `rule-cd3217-...` (lowercase, `rule-` prefix)
        instead of the canonical `R-...`. Uppercase + coerce a `RULE-`/`RULE_`
        prefix to `R-` so a reparable casing slip doesn't fail the whole build."""
        if not isinstance(v, str):
            return v
        s = v.strip().upper()
        if s.startswith(("RULE-", "RULE_")):
            s = "R-" + s[5:]
        return s
    likely_causes: list[Cause] = Field(min_length=1)
    diagnostic_steps: list[DiagnosticStep] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    sources: list[str] = Field(
        default_factory=list,
        description="URLs or citation markers supporting this rule.",
    )


class RulesSet(BaseModel):
    """Phase 3 Writer 2 (Clinicien) output — diagnostic decision tree."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    rules: list[Rule] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_stringified_rules(cls, data: object) -> object:
        """Tolère un `submit_rules` mal formé par le LLM (forced tool_choice).

        Bug PROD (pipeline Clinicien-Expand) : sous `tool_choice` forcé, Opus
        renvoie parfois l'argument `rules` du tool comme une STRING JSON au lieu
        d'une vraie liste — pydantic lève alors `list_type` ("Input should be a
        valid list", input_type=str), l'expansion échoue après 2 retries, et
        `expand_pack` plante. Trois formes ont été observées :

          A. `rules` = la liste sérialisée        →  '[{...}, {...}]'
          B. le RulesSet ENTIER wedgé dans `rules` →  '{"schema_version":...,"rules":[...]}'
          C. le payload racine tout entier stringifié → la string arrive ici directement.

        On décode AVANT la validation de champ (mode="before") pour que
        `RulesSet.model_validate(...)` réussisse partout (pas seulement via le
        `_try_unwrap` de tool_call.py) et du premier coup — ce validator est le
        point central : toute re-validation d'un RulesSet en bénéficie.

        Garde-fou : on ne décode QUE des strings JSON-like (commençant par
        '{'/'['). Une string non-JSON est laissée telle quelle → pydantic lève
        son erreur habituelle (on ne masque pas un vrai problème en inventant
        une liste vide).
        """
        # Forme C : tout le payload est une string JSON (le modèle a sérialisé
        # l'objet racine). On la json.loads pour retrouver un dict à valider.
        if isinstance(data, str):
            stripped = data.strip()
            if stripped[:1] in "{[":
                try:
                    data = json.loads(stripped)
                except (json.JSONDecodeError, ValueError):
                    return data  # non-JSON → laisse pydantic rejeter

        if not isinstance(data, dict):
            return data

        rules = data.get("rules")
        if not isinstance(rules, str):
            return data  # cas nominal (vraie liste) ou absent → rien à faire

        stripped = rules.strip()
        if stripped[:1] not in "{[":
            return data  # string non-JSON → laisse pydantic lever list_type

        try:
            decoded = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return data

        # Forme A : `rules` était la liste elle-même.
        if isinstance(decoded, list):
            return {**data, "rules": decoded}

        # Forme B : `rules` enveloppait le RulesSet entier ({schema_version, rules}).
        # On remonte la vraie liste interne et on conserve le schema_version
        # interne s'il existe (plus fiable que le wrapper externe vide).
        if isinstance(decoded, dict) and isinstance(decoded.get("rules"), list):
            merged = {**data, **decoded}
            return merged

        return data


# --- Writer 3 — Lexicographe ---------------------------------------------------


class ComponentSheet(WithProvenance):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    canonical_name: Annotated[str, Field(pattern=_CANONICAL_NAME_PATTERN, description="Must match a canonical_name in the registry. Pattern: [A-Z0-9_./-]{2,64}.")]
    role: str | None = None
    package: str | None = None
    typical_failure_modes: list[str] = Field(default_factory=list)
    notes: str | None = None


class Dictionary(BaseModel):
    """Phase 3 Writer 3 (Lexicographe) output — per-component technical sheets."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    entries: list[ComponentSheet] = Field(default_factory=list)


# ======================================================================
# PHASE 3.5 — Reviser patches (surgical revision deltas)
# ======================================================================
#
# A reviser no longer re-emits an entire writer artefact. It emits a typed
# DELTA of operations addressed by stable identifier, which the deterministic
# applicator in `api.pipeline.patch` applies to the current artefact. Records
# the reviser does not name are preserved verbatim — that removes the full
# re-emit's collateral-regression surface (a large graph re-emitted to change
# four orphan edges used to drop unflagged nodes and tank the consistency
# score). Every list defaults empty, so an empty patch is a valid no-op.


class KnowledgeGraphPatch(BaseModel):
    """Surgical delta over a `KnowledgeGraph`. Nodes are addressed by `id`,
    edges by their (source_id, target_id, relation) triple."""

    model_config = ConfigDict(extra="forbid")

    add_nodes: list[KnowledgeNode] = Field(
        default_factory=list,
        description="New nodes to insert. Each `id` must NOT already exist.",
    )
    update_nodes: list[KnowledgeNode] = Field(
        default_factory=list,
        description=(
            "Full replacements of existing nodes, matched by `id`. The `id` "
            "must already exist. Carries the complete corrected node, not a diff."
        ),
    )
    remove_node_ids: list[str] = Field(
        default_factory=list,
        description="`id`s of nodes to drop. Unknown ids are skipped.",
    )
    add_edges: list[KnowledgeEdge] = Field(
        default_factory=list,
        description=(
            "New edges. Both endpoints must reference a node that exists once "
            "the patch is applied. Re-adding an identical edge is a no-op."
        ),
    )
    remove_edges: list[KnowledgeEdge] = Field(
        default_factory=list,
        description=(
            "Edges to drop, matched on (source_id, target_id, relation). "
            "Unknown edges are skipped."
        ),
    )


class RulesPatch(BaseModel):
    """Surgical delta over a `RulesSet`. Rules are addressed by `id`."""

    model_config = ConfigDict(extra="forbid")

    add_rules: list[Rule] = Field(
        default_factory=list,
        description="New rules. Each `id` must NOT already exist.",
    )
    update_rules: list[Rule] = Field(
        default_factory=list,
        description=(
            "Full replacements of existing rules, matched by `id`. The `id` "
            "must already exist. Carries the complete corrected rule, not a diff."
        ),
    )
    remove_rule_ids: list[str] = Field(
        default_factory=list,
        description="`id`s of rules to drop. Unknown ids are skipped.",
    )


class DictionaryPatch(BaseModel):
    """Surgical delta over a `Dictionary`. Entries are addressed by `canonical_name`."""

    model_config = ConfigDict(extra="forbid")

    add_entries: list[ComponentSheet] = Field(
        default_factory=list,
        description="New component sheets. Each `canonical_name` must NOT already exist.",
    )
    update_entries: list[ComponentSheet] = Field(
        default_factory=list,
        description=(
            "Full replacements of existing sheets, matched by `canonical_name`. "
            "Must already exist. Carries the complete corrected sheet, not a diff."
        ),
    )
    remove_entry_names: list[str] = Field(
        default_factory=list,
        description="`canonical_name`s of entries to drop. Unknown names are skipped.",
    )


# ======================================================================
# PHASE 4 — Audit verdict
# ======================================================================


FileName = Literal["knowledge_graph", "rules", "dictionary"]


class DriftItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: FileName
    mentions: list[str] = Field(
        description="The strings (refdes or names) that failed validation against the registry."
    )
    reason: str


class AuditVerdict(BaseModel):
    """Phase 4 output — structured QA result driving the self-healing loop."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    overall_status: Literal["APPROVED", "NEEDS_REVISION", "REJECTED"]
    consistency_score: float = Field(ge=0.0, le=1.0)
    files_to_rewrite: list[FileName] = Field(default_factory=list)
    drift_report: list[DriftItem] = Field(default_factory=list)
    revision_brief: str = Field(
        default="",
        description="Actionable description of what the Reviser must change. Empty when APPROVED.",
    )


# ======================================================================
# Orchestrator return type
# ======================================================================


class PipelineResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_slug: str
    disk_path: str
    # "COMPLETED" — the full Scout→Auditor chain ran and `verdict` is set.
    # "NEEDS_KIND_CONFIRMATION" — the Phase 1.5 device-kind gate paused the
    #   run before any Scout spend; the technician must confirm the device
    #   class (see `pending_kind.json`) and re-run with `confirmed_device_kind`.
    #   `verdict` is None in this case.
    status: Literal["COMPLETED", "NEEDS_KIND_CONFIRMATION"] = "COMPLETED"
    verdict: AuditVerdict | None = None
    revise_rounds_used: int = 0
    tokens_used_total: int = 0
    cache_read_tokens_total: int = 0
    cache_write_tokens_total: int = 0
