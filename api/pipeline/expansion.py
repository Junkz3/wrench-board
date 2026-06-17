"""Targeted pack expansion — grow an existing device's memory bank in place.

When the diagnostic agent calls `mb_get_rules_for_symptoms` and comes back
empty-handed, it can call `mb_expand_knowledge(focus_symptoms, focus_refdes)`
to trigger THIS pipeline. We don't rebuild the whole pack; we:

1. Append a targeted Scout run to the existing `raw_research_dump.md`
   (cumulative — the dump grows with every expansion).
2. Re-run the Registry Builder on the enriched dump to absorb any new
   components/signals the Scout surfaced.
3. Re-run the Clinicien on the enriched dump + merged registry to produce
   an updated rule set focused on the new symptom area.
4. Persist — registry.json and rules.json get overwritten with the merged
   result. Any new canonical names and rule_ids are counted for telemetry.

Cost per expansion: ~$0.40 (Scout Sonnet + Registry Sonnet + Clinicien Opus)
vs ~$1.5 for a full rebuild. Cartographe/Lexicographe/Auditor are skipped —
the graph can drift slightly between expansions and the full audit waits
for the next rebuild. Acceptable trade-off for the "living memory bank" UX.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import ValidationError

from api.config import get_settings
from api.pipeline import pack_storage
from api.pipeline.expand_metering import report_expand_phases
from api.pipeline.pack_migrate import migrate_pack_if_needed
from api.pipeline.pack_sanitizer import PackSanitizer
from api.pipeline.pack_storage import (
    JournalEntry,
    append_journal,
    load_effective_pack,
    write_promoted_facts,
)
from api.pipeline.prompts import (
    CLINICIEN_TASK,
    SCOUT_RETRY_SUFFIX,
    SCOUT_SYSTEM,
    WRITER_SHARED_USER_PREFIX_TEMPLATE,
    WRITER_SYSTEM,
    device_kind_constraint,
)
from api.pipeline.registry import run_registry_builder
from api.pipeline.schemas import (
    COMPONENT_KINDS,
    Provenance,
    Registry,
    RegistryComponent,
    RegistrySignal,
    Rule,
    RulesSet,
    SanitizerAction,
)
from api.pipeline.telemetry.token_stats import PhaseTokenStats
from api.pipeline.tool_call import call_with_forced_tool
from api.pipeline.writers import SUBMIT_RULES_TOOL_NAME, _submit_rules_tool

logger = logging.getLogger("wrench_board.pipeline.expansion")

# Signature for the optional "chunk provider" injected by the MA runtime
# when it wants to spawn a KnowledgeCurator sub-agent instead of running
# the inline `_run_targeted_scout` ourselves. Returns the same Markdown
# chunk shape so the rest of the pipeline (Registry + Clinicien) is
# oblivious to who produced it.
ChunkProvider = Callable[..., Awaitable[str]]


TARGETED_SCOUT_TEMPLATE = """\
Research the following device with a FOCUSED scope on specific symptoms the
technician is hitting right now. Produce the same Markdown dump format defined
in your system prompt, BUT dedicate your searches strictly to the focus areas
below. The existing knowledge pack already covers other failure modes — your
job is to fill the gap for THESE symptoms.

Device: {device_label}

Focus symptoms (target these):
{focus_block}

{refdes_block}

Search plan for this expansion:
- 4–8 searches total, each scoped to one focus symptom + the device.
- Probe the specialized microsoldering families first (r/boardrepair, Rossmann,
  NorthridgeFix, iPadRehab, badcaps, EEVblog). These are where component-level
  audio / RF / charging threads live in detail.
- For each focus refdes if any, search the refdes + device together
  (e.g. "iPhone X U3101 no sound", "iPhone X Meson audio codec").
- Do NOT re-cover power-rail / boot / charge topics already in the pack —
  we already have those; new material only.

Stop when you have 3–6 concrete symptom bullets with traceable sources.
"""


def _format_focus_block(focus_symptoms: list[str]) -> str:
    return "\n".join(f"  - {s}" for s in focus_symptoms)


def _format_refdes_block(focus_refdes: list[str]) -> str:
    if not focus_refdes:
        return ""
    return (
        "Focus refdes (look up these specific component identifiers):\n"
        + "\n".join(f"  - {r}" for r in focus_refdes)
    )


async def _run_targeted_scout(
    *,
    client: AsyncAnthropic,
    model: str,
    device_label: str,
    focus_symptoms: list[str],
    focus_refdes: list[str],
    device_kind: str | None = None,
    stats: PhaseTokenStats | None = None,
) -> str:
    """Run a Scout turn focused on specific symptoms, return the Markdown chunk.

    Reuses SCOUT_SYSTEM so the output shape is identical to the main Scout —
    same headings, same bullet format — which means the downstream Registry
    + Clinicien parse it without adaptation. When `device_kind` is a resolved
    class, appends the same authoritative class constraint the main Scout uses
    (returns '' for None/unknown, so the prompt is byte-identical otherwise).
    """
    user_prompt = TARGETED_SCOUT_TEMPLATE.format(
        device_label=device_label,
        focus_block=_format_focus_block(focus_symptoms),
        refdes_block=_format_refdes_block(focus_refdes),
    )
    user_prompt = user_prompt + device_kind_constraint(device_kind)
    messages: list[dict] = [{"role": "user", "content": user_prompt}]
    web_search_tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": 8}

    logger.info(
        "[Expand·Scout] targeting device=%r symptoms=%s refdes=%s",
        device_label, focus_symptoms, focus_refdes,
    )

    def _record(resp) -> None:
        # Best-effort token capture for cloud metering (kind='expand'). The Scout
        # runs its own messages.create (web_search), outside the shared tool_call
        # helper, so we record here what registry/clinicien record via `stats`.
        if stats is None or resp is None:
            return
        u = resp.usage
        stats.record(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
            model=model,
        )

    # Single pass; we don't expect long pause_turn chains on a narrow scope.
    attempt = 0
    response = None
    effort = "xhigh" if str(model).startswith("claude-opus-4-") else "high"
    while attempt < 2:
        response = await client.messages.create(
            model=model,
            max_tokens=8000,
            system=SCOUT_SYSTEM,
            messages=messages,
            tools=[web_search_tool],
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": effort},
        )
        _record(response)
        if response.stop_reason == "pause_turn":
            messages = [
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": response.content},
            ]
            attempt += 1
            continue
        break

    if response is None:
        raise RuntimeError("targeted scout did not run")

    text_parts = [block.text for block in response.content if block.type == "text"]
    chunk = "\n\n".join(t for t in text_parts if t.strip())
    if not chunk:
        # Try one retry with the broadening suffix before giving up.
        logger.warning(
            "[Expand·Scout] empty first pass for focus=%s · retrying broader",
            focus_symptoms,
        )
        response = await client.messages.create(
            model=model,
            max_tokens=8000,
            system=SCOUT_SYSTEM,
            messages=[{"role": "user", "content": user_prompt + SCOUT_RETRY_SUFFIX}],
            tools=[web_search_tool],
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": effort},
        )
        _record(response)
        text_parts = [block.text for block in response.content if block.type == "text"]
        chunk = "\n\n".join(t for t in text_parts if t.strip())
        if not chunk:
            raise RuntimeError("targeted scout produced no output after retry")

    logger.info("[Expand·Scout] produced chunk length=%d chars", len(chunk))
    return chunk


def _audit_dump_path(pack_dir: Path) -> Path:
    """Chemin du dump cumulatif après migration T8 → audit/raw_research_dump.md.

    Le dump est privé/audit (jamais re-servi aux tenants) : la PII y est tolérée
    — le point de contrôle est la sanitisation des FACTS à la sortie (promoted/).
    """
    return pack_dir / "audit" / "raw_research_dump.md"


def _append_scout_chunk(
    pack_dir: Path, chunk: str, focus_symptoms: list[str]
) -> tuple[int, int]:
    """Append the new chunk to audit/raw_research_dump.md with a separator header.

    The cumulative dump is the durable raw memory — every expansion leaves a
    traceable footprint. Registry + rules are re-derived on the FULL cumulative
    dump on the next Clinicien call. Retourne (start, end) en octets du segment
    ajouté (header + chunk) pour scout_dump_range dans le journal.
    """
    path = _audit_dump_path(pack_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    start = path.stat().st_size if path.is_file() else 0
    header = (
        "\n\n---\n"
        f"## Expansion {time.strftime('%Y-%m-%dT%H:%M:%S')} — focus: "
        f"{', '.join(focus_symptoms)}\n\n"
    )
    with path.open("a", encoding="utf-8") as f:
        f.write(header)
        f.write(chunk)
        f.write("\n")
    end = path.stat().st_size
    return start, end


async def _run_clinicien_on_full_dump(
    *,
    client: AsyncAnthropic,
    model: str,
    device_label: str,
    raw_dump: str,
    registry: Registry,
    stats: PhaseTokenStats | None = None,
) -> RulesSet:
    """Re-run the Clinicien on the FULL (cumulative) dump + merged registry.

    The output REPLACES the existing rules.json — the new ruleset covers
    both previously-known failure modes and the expansion focus area. Ids
    may be renumbered between runs; rule content stays stable because the
    same prompt + same sources are used.
    """
    shared_prefix = WRITER_SHARED_USER_PREFIX_TEMPLATE.format(
        device_label=device_label,
        raw_dump=raw_dump,
        registry_json=registry.model_dump_json(indent=2),
    )
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": shared_prefix,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": CLINICIEN_TASK},
            ],
        }
    ]
    return await call_with_forced_tool(
        client=client,
        model=model,
        system=WRITER_SYSTEM,
        messages=messages,
        tools=[_submit_rules_tool()],
        forced_tool_name=SUBMIT_RULES_TOOL_NAME,
        output_schema=RulesSet,
        max_attempts=2,
        log_label="Clinicien-Expand",
        stats=stats,
    )


# --- Option C : sanitisation + provenance + delta ----------------------------

_SANITIZER = PackSanitizer()


def _prior_taxonomy(memory_root: Path, slug: str):
    """Lit la taxonomy registry-level préservée dans baseline/registry.json _meta
    (la migration y range les clés non-liste). Renvoie un DeviceTaxonomy ou None."""
    from api.pipeline.schemas import DeviceTaxonomy
    for layer in ("baseline", "promoted"):
        path = memory_root / slug / layer / "registry.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        tax = (data.get("_meta") or {}).get("taxonomy")
        if tax:
            try:
                return DeviceTaxonomy.model_validate(tax)
            except Exception:  # noqa: BLE001
                return None
    return None


def _device_label_from(memory_root: Path, slug: str) -> str | None:
    """device_label après migration : préservé dans baseline/registry.json _meta.
    Fallback : un éventuel promoted/registry.json _meta. Renvoie None si absent."""
    for layer in ("baseline", "promoted"):
        path = memory_root / slug / layer / "registry.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        meta = data.get("_meta") or {}
        if meta.get("device_label"):
            return meta["device_label"]
    return None


def _to_pydantic_actions(actions) -> list[SanitizerAction]:
    """Convertit les SanitizerAction (dataclass, pack_sanitizer) en SanitizerAction
    (Pydantic, schemas) — agrégées par (field, action)."""
    agg: dict[tuple[str, str], int] = {}
    for a in actions:
        agg[(a.field, a.action)] = agg.get((a.field, a.action), 0) + a.count
    return [
        SanitizerAction(field=f, action=act, count=n)
        for (f, act), n in agg.items()
    ]


def _make_provenance(
    *, expansion_id: str, owner_ref: str | None, actions: list[SanitizerAction]
) -> Provenance:
    return Provenance(
        expansion_id=expansion_id,
        added_at=datetime.now(UTC),
        added_by_tenant=owner_ref,
        confidence=0.5,
        source_kind="agent_expansion",
        sanitizer_actions=actions,
        status="promoted",
    )


def _sanitize_component(comp: RegistryComponent) -> tuple[dict, list]:
    """Sanitise les champs libres d'un composant. Retourne (dict_sain, actions)."""
    data = comp.model_dump(mode="json", exclude={"provenance"})
    acts: list = []
    desc, a = _SANITIZER.sanitize_text(data.get("description"), field_name="description")
    data["description"] = desc
    acts += a
    if data.get("logical_alias"):
        la, a = _SANITIZER.sanitize_text(data["logical_alias"], field_name="logical_alias")
        data["logical_alias"] = la
        acts += a
    if data.get("aliases"):
        outs, a = _SANITIZER.sanitize_many(data["aliases"], field_name="aliases")
        data["aliases"] = outs
        acts += a
    for cand in data.get("refdes_candidates") or []:
        ev, a = _SANITIZER.sanitize_text(cand.get("evidence"), field_name="refdes_candidate.evidence")
        cand["evidence"] = ev
        acts += a
    return data, acts


def _sanitize_signal(sig: RegistrySignal) -> tuple[dict, list]:
    data = sig.model_dump(mode="json", exclude={"provenance"})
    acts: list = []
    if data.get("aliases"):
        outs, a = _SANITIZER.sanitize_many(data["aliases"], field_name="aliases")
        data["aliases"] = outs
        acts += a
    return data, acts


def _sanitize_rule(rule: Rule) -> tuple[dict, list]:
    data = rule.model_dump(mode="json", exclude={"provenance"})
    acts: list = []
    outs, a = _SANITIZER.sanitize_many(data.get("symptoms") or [], field_name="symptoms")
    data["symptoms"] = outs
    acts += a
    for step in data.get("diagnostic_steps") or []:
        act_txt, a = _SANITIZER.sanitize_text(step.get("action"), field_name="diagnostic_step.action")
        step["action"] = act_txt
        acts += a
        if step.get("expected"):
            exp, a = _SANITIZER.sanitize_text(step["expected"], field_name="diagnostic_step.expected")
            step["expected"] = exp
            acts += a
    if data.get("sources"):
        outs, a = _SANITIZER.sanitize_many(data["sources"], field_name="sources")
        data["sources"] = outs
        acts += a
    return data, acts


def _build_delta_facts(
    *,
    items,
    before_by_key: dict[str, dict],
    key_attr: str,
    sanitize_fn,
    model_cls,
    expansion_id: str,
    owner_ref: str | None,
    delta_dropped: list[dict],
) -> tuple[list[dict], list[str], list[str]]:
    """Pour une liste de facts (composants/signaux/règles), garde ceux qui sont
    NOUVEAUX ou MODIFIÉS vs `before`, les sanitise, attache la provenance,
    re-valide via le modèle Pydantic strict. Un fact invalide est DROPPÉ
    (ajouté à delta_dropped), pas levé. Retourne (facts_sains, ids_new, ids_modified)."""
    out: list[dict] = []
    ids_new: list[str] = []
    ids_modified: list[str] = []
    for item in items:
        key = getattr(item, key_attr, None)
        if key is None:
            # canonical_name/id invalide (model_construct bypass) → drop.
            delta_dropped.append({
                "kind": model_cls.__name__,
                "reason": "invalid_identifier",
                "field": key_attr,
                "value_preview": str(getattr(item, key_attr, ""))[:60],
                "error": f"missing/invalid {key_attr}",
            })
            continue

        prior = before_by_key.get(key)
        sane_data, raw_actions = sanitize_fn(item)
        # Comparaison NEW/MODIFIED faite sur le contenu sanitisé vs prior
        # (prior est déjà sanitisé/baseline). On compare hors provenance.
        if prior is not None:
            # Strip les DEUX formes de provenance (alias _provenance + champ
            # `provenance` que model_dump_json sans by_alias produit sur les
            # packs migrés legacy). Compare le contenu métier uniquement.
            prior_cmp = {
                k: v for k, v in prior.items()
                if k not in ("_provenance", "provenance")
            }
            if prior_cmp == sane_data:
                continue  # inchangé → promoted reste additif, on ne réécrit pas.

        actions = _to_pydantic_actions(raw_actions)
        prov = _make_provenance(expansion_id=expansion_id, owner_ref=owner_ref, actions=actions)
        candidate = dict(sane_data)
        candidate["_provenance"] = prov.model_dump(by_alias=True, mode="json")
        # Re-validation stricte : un identifiant invalide (pattern) lève ici.
        try:
            model_cls.model_validate(candidate)
        except ValidationError as exc:
            delta_dropped.append({
                "kind": model_cls.__name__,
                "reason": "validation_failed",
                "field": key_attr,
                "value_preview": str(key)[:60],
                "error": str(exc)[:200],
            })
            continue

        out.append(candidate)
        fid = pack_storage._derive_fact_id(candidate)
        if prior is None:
            ids_new.append(fid)
        else:
            ids_modified.append(fid)
    return out, ids_new, ids_modified


async def expand_pack(
    *,
    device_slug: str,
    focus_symptoms: list[str],
    focus_refdes: list[str] | None = None,
    client: AsyncAnthropic | None = None,
    memory_root: Path | None = None,
    chunk_provider: ChunkProvider | None = None,
    owner_ref: str | None = None,
) -> dict[str, Any]:
    """Grow the on-disk pack for `device_slug` around a focus symptom area.

    Option C (T8) : au lieu d'écraser registry.json/rules.json à la racine,
    on écrit le DELTA (facts nouveaux ou modifiés vs le pack effectif courant)
    dans la couche partagée `promoted/`, avec attribution owner_ref + sanitisation
    PII + provenance + journal. Pas de staging tenant-local en V1 (les
    enrichissements sont partagés immédiatement — le moat T6 — mais PII-free,
    tracés et revocables).

    Trade-off documenté : une "amélioration" du Registry-Builder à la description
    d'un fact baseline est captée comme MODIFIED et écrite dans promoted/, où elle
    écrasera la baseline au merge (load_effective_pack : promoted > baseline).

    Returns a summary dict (rétro-compatible avec les 4 callers historiques,
    + `expansion_id`) :
        {
          "expanded": True, "expansion_id": "E-xxxxxxxx",
          "focus_symptoms": [...], "focus_refdes": [...],
          "new_rules_count": int, "new_components_count": int,
          "new_signals_count": int, "total_rules_after": int,
          "dump_bytes_added": int,
        }

    Raises RuntimeError on hard failures (missing pack, empty Scout).
    """
    settings = get_settings()
    memory_root = memory_root or Path(settings.memory_root)
    pack_dir = memory_root / device_slug
    if not pack_dir.exists():
        raise RuntimeError(f"no pack on disk for slug={device_slug!r}")

    focus_refdes = focus_refdes or []
    if not focus_symptoms:
        raise RuntimeError("expand_pack requires at least one focus symptom")

    if client is None:
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        client = AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=settings.anthropic_max_retries)

    # 0. Migration idempotente legacy → T8 (baseline/ + audit/). Après ça, les
    #    fichiers racine n'existent plus : on lit l'état via le pack effectif.
    migrate_pack_if_needed(memory_root, device_slug)

    # 1. État "avant" = pack effectif partagé (baseline + promoted). owner_ref=None
    #    ici car Option C n'a pas de couche staged ; le delta est calculé vs le
    #    partagé que tout le monde voit.
    before = load_effective_pack(memory_root, device_slug, owner_ref=None)
    before_reg_items = before["registry"]["items"]
    before_rules_items = before["rules"]["items"]
    # On sépare composants/signaux par kind : COMPONENT_KINDS sinon signal.
    before_comp_by_key: dict[str, dict] = {}
    before_sig_by_key: dict[str, dict] = {}
    for it in before_reg_items:
        name = it.get("canonical_name")
        if name is None:
            continue
        # Case-insensitive : les packs legacy ont des kinds en minuscules
        # (pmic/power_rail) alors que la convention T8 est UPPERCASE. Doit
        # matcher _unflatten_effective (tools.py) + _effective_registry
        # (routes/packs.py) sinon un composant legacy mal classe serait vu
        # comme NEW et re-promu (write amplification + mauvaise provenance).
        if str(it.get("kind", "")).upper() in COMPONENT_KINDS:
            before_comp_by_key[name] = it
        else:
            before_sig_by_key[name] = it
    before_rule_by_key = {it["id"]: it for it in before_rules_items if "id" in it}

    # device_label : baseline/_meta le préserve ; fallback sur le slug.
    device_label = _device_label_from(memory_root, device_slug) or device_slug

    # 2. Targeted Scout → new chunk (appended au dump audit/).
    #    On lit la taxonomy pré-existante AVANT le Scout pour réinjecter le
    #    device_kind connu comme contrainte de classe (même helper que la
    #    création — Task 6), évitant qu'un focus mono-symptôme dérive sur une
    #    autre utilisation du même board code.
    model_sonnet = settings.anthropic_model_sonnet
    model_main = settings.anthropic_model_main
    prior_tax = _prior_taxonomy(memory_root, device_slug)
    prior_kind = prior_tax.device_kind if prior_tax else None

    # T13 metering (kind='expand'): one PhaseTokenStats per LLM-calling phase.
    # expansion_id is minted HERE (not at step 4) so the finally can key the
    # cloud report even when a later phase raises. The report fires in `finally`
    # so the spend lands for partial expansions too (success AND failure), like
    # the build path — expand_pack makes its own paid calls outside the agent turn.
    expansion_id = f"E-{uuid.uuid4().hex[:8]}"
    scout_stats = PhaseTokenStats(phase="scout", model=model_sonnet)
    registry_stats = PhaseTokenStats(phase="registry", model=model_sonnet)
    clinicien_stats = PhaseTokenStats(phase="clinicien", model=model_main)

    try:
        if chunk_provider is not None:
            logger.info("[Expand] using injected chunk_provider (MA curator)")
            chunk = await chunk_provider(
                device_label=device_label,
                focus_symptoms=focus_symptoms,
                focus_refdes=focus_refdes,
            )
        else:
            chunk = await _run_targeted_scout(
                client=client,
                model=model_sonnet,
                device_label=device_label,
                focus_symptoms=focus_symptoms,
                focus_refdes=focus_refdes,
                device_kind=prior_kind,
                stats=scout_stats,
            )
        dump_start, dump_end = _append_scout_chunk(pack_dir, chunk, focus_symptoms)
        dump_bytes_added = len(chunk)

        # 3. Re-run Registry + Clinicien sur le dump cumulatif COMPLET → objets entiers.
        #    prior_tax / prior_kind sont déjà lus en étape 2 (réinjectés au Registry
        #    Builder comme contrainte — même mécanisme que la création, Task 6 —
        #    évitant qu'un focus mono-symptôme reclasse le board).
        full_dump = _audit_dump_path(pack_dir).read_text(encoding="utf-8")
        new_registry = await run_registry_builder(
            client=client, model=model_sonnet,
            device_label=device_label, raw_dump=full_dump,
            device_kind=prior_kind,
            stats=registry_stats,
        )
        # Préserve la taxonomy pré-existante si la re-run a régressé à tout-null
        # (un focus mono-symptôme peut affamer le signal de marque). La taxonomy est
        # une métadonnée registry-level, persistée dans baseline/registry.json _meta —
        # Option C ne la réécrit jamais, mais on la réinjecte ici pour que le Clinicien
        # la voie. Cf. test_expand_pack_preserves_taxonomy.
        if prior_tax and not any(
            getattr(new_registry.taxonomy, field)
            for field in ("brand", "model", "version", "form_factor")
        ):
            new_registry.taxonomy = prior_tax.model_copy()
        if prior_tax:
            # device_kind est une dimension orthogonale (la re-run peut peupler
            # brand/model mais perdre la classe résolue) : on la reporte toujours
            # depuis le prior si la re-run l'a laissée nulle.
            new_registry.taxonomy.device_kind = (
                new_registry.taxonomy.device_kind or prior_tax.device_kind
            )
        new_rules = await _run_clinicien_on_full_dump(
            client=client, model=model_main,
            device_label=device_label, raw_dump=full_dump, registry=new_registry,
            stats=clinicien_stats,
        )
    finally:
        # Best-effort, no-op on self-host (cloud metering unconfigured). Reports
        # whatever spend was captured even if a later phase raised.
        report_expand_phases(
            owner_ref=owner_ref,
            device_slug=device_slug,
            stats=[scout_stats, registry_stats, clinicien_stats],
            expansion_id=expansion_id,
        )

    # 4. Calcul du DELTA + sanitisation + provenance + re-validation stricte.
    delta_dropped: list[dict] = []

    comp_facts, comp_new, comp_mod = _build_delta_facts(
        items=new_registry.components, before_by_key=before_comp_by_key,
        key_attr="canonical_name", sanitize_fn=_sanitize_component,
        model_cls=RegistryComponent, expansion_id=expansion_id,
        owner_ref=owner_ref, delta_dropped=delta_dropped,
    )
    sig_facts, sig_new, sig_mod = _build_delta_facts(
        items=new_registry.signals, before_by_key=before_sig_by_key,
        key_attr="canonical_name", sanitize_fn=_sanitize_signal,
        model_cls=RegistrySignal, expansion_id=expansion_id,
        owner_ref=owner_ref, delta_dropped=delta_dropped,
    )
    rule_facts, rule_new, rule_mod = _build_delta_facts(
        items=new_rules.rules, before_by_key=before_rule_by_key,
        key_attr="id", sanitize_fn=_sanitize_rule,
        model_cls=Rule, expansion_id=expansion_id,
        owner_ref=owner_ref, delta_dropped=delta_dropped,
    )

    # 5. Écriture du delta dans promoted/ (couche partagée, jamais la racine).
    registry_delta = comp_facts + sig_facts
    if registry_delta:
        write_promoted_facts(
            memory_root, device_slug, file_name="registry.json", new_facts=registry_delta
        )
    if rule_facts:
        write_promoted_facts(
            memory_root, device_slug, file_name="rules.json", new_facts=rule_facts
        )

    # 6. Sanitisation des focus_symptoms pour le journal (jamais de PII en clair).
    sane_focus, _ = _SANITIZER.sanitize_many(focus_symptoms, field_name="focus_symptom")

    delta_summary = {
        "new_components": comp_new,
        "new_signals": sig_new,
        "new_rules": rule_new,
        "modified": comp_mod + sig_mod + rule_mod,
        "dropped": delta_dropped,
    }

    append_journal(
        memory_root, device_slug,
        JournalEntry(
            id=expansion_id,
            ts=datetime.now(UTC),
            owner_ref=owner_ref,
            slug=device_slug,
            focus_symptoms=sane_focus,
            focus_refdes=focus_refdes,
            delta_summary=delta_summary,
            scout_dump_range={"start": dump_start, "end": dump_end},
            status="promoted",
        ),
    )

    # 7. Résumé rétro-compatible + expansion_id.
    total_rules_after = len(before_rule_by_key) + len(rule_new)
    summary = {
        "expanded": True,
        "expansion_id": expansion_id,
        "focus_symptoms": focus_symptoms,
        "focus_refdes": focus_refdes,
        "new_rules_count": len(rule_new),
        "new_components_count": len(comp_new),
        "new_signals_count": len(sig_new),
        "total_rules_after": total_rules_after,
        "dump_bytes_added": dump_bytes_added,
    }
    logger.info("[Expand] done · %s · dropped=%d", summary, len(delta_dropped))
    return summary
