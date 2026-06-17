"""Pack-level read endpoints — listings, summaries, taxonomy, graph payload.

Also hosts the shared on-disk presence helpers (`_find_boardview`,
`_detect_boardview`, `_detect_schematic_pdf`, `_pack_is_complete`,
`_read_optional_json`) reused by the documents/repairs/schematic route
modules. Keeping them here avoids a circular dependency: documents.py
imports `_find_boardview`, repairs.py imports `_pack_is_complete`, and
neither of those modules has a more natural home for the helper than the
"pack composition" concern owned by this file.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import api.pipeline as _pkg  # noqa: PLC0415 — module-attribute lookups for patchability
from api.agent.field_reports import list_field_reports
from api.pipeline import device_kind as device_kind_module
from api.pipeline import sources
from api.pipeline.build_state import read_build_state
from api.pipeline.graph_transform import pack_to_graph_payload
from api.pipeline.models import (
    ExpandRequest,
    PackSummary,
    TaxonomyPackEntry,
    TaxonomyTree,
)
from api.pipeline.orchestrator import _slugify
from api.pipeline.pack_storage import load_effective_pack
from api.pipeline.routes._helpers import _validate_slug
from api.pipeline.schemas import COMPONENT_KINDS, _DeviceKind

logger = logging.getLogger("wrench_board.pipeline.api")

router = APIRouter()


# --- T8 — lecture migration-aware du pack ------------------------------------
#
# La migration T8 (pack_migrate) déplace registry.json/rules.json/
# knowledge_graph.json/dictionary.json de la racine vers baseline/ (format
# {items:[...]} + _meta pour les clés non-liste). Les endpoints ci-dessous
# doivent suivre : sur un pack migré (.migrated_t8 présent), on lit la vue
# effective (baseline + promoted) et on reconstruit la forme legacy attendue
# par les consommateurs (UI / graph_transform). Sur un pack non-migré (tests
# qui écrivent encore la racine, self-host pré-migration), on garde le reader
# racine historique.


def _is_migrated(pack_dir: Path) -> bool:
    return (pack_dir / ".migrated_t8").is_file()


def _baseline_meta(pack_dir: Path, file_name: str) -> dict:
    """_meta préservé par la migration dans baseline/{file_name} (device_label,
    taxonomy, schema_version, …). {} si absent."""
    path = pack_dir / "baseline" / file_name
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return data.get("_meta") or {}


def _effective_registry(memory_root: Path, slug: str, pack_dir: Path, owner_ref: str | None = None) -> dict:
    """Registry legacy-shape {schema_version, device_label, taxonomy, components,
    signals} reconstruit depuis la vue effective d'un pack migré.

    Lot 2 : owner_ref inclut la couche privée _staged/{owner} (build web-only)."""
    eff = load_effective_pack(memory_root, slug, owner_ref=owner_ref)
    items = eff["registry"]["items"]
    components = [it for it in items if str(it.get("kind", "")).upper() in COMPONENT_KINDS]
    signals = [it for it in items if str(it.get("kind", "")).upper() not in COMPONENT_KINDS]
    meta = _baseline_meta(pack_dir, "registry.json")
    out = {"components": components, "signals": signals}
    out.update(meta)  # device_label, taxonomy, schema_version
    return out


def _pack_file_present(pack_dir: Path, file_name: str, owner_ref: str | None = None) -> bool:
    """Un fichier pack est "présent" sur un pack migré ssi il existe dans
    baseline/ ou promoted/ (la migration crée baseline/{fname} pour chaque
    fichier legacy qui existait).

    Lot 2 : avec un owner_ref, la couche PRIVÉE `_staged/{owner}/` compte aussi —
    un build web-only mis en staging pour ce tenant est "présent" POUR LUI sans
    jamais l'être pour le commons (owner None) ni pour un autre tenant."""
    if (
        (pack_dir / "baseline" / file_name).is_file()
        or (pack_dir / "promoted" / file_name).is_file()
    ):
        return True
    return bool(owner_ref) and (pack_dir / "_staged" / owner_ref / file_name).is_file()


def _writer_present(pack_dir: Path, file_name: str, migrated: bool, owner_ref: str | None) -> bool:
    """Presence d'un fichier writer pour le bitmask du résumé, owner-aware (Lot 2).

    Migré : baseline/promoted (+ _staged/{owner}). Non-migré : fichier racine
    historique (+ _staged/{owner} pour un build web-only mis en staging, qui ne
    pose pas le flag .migrated_t8)."""
    if migrated:
        return _pack_file_present(pack_dir, file_name, owner_ref)
    if (pack_dir / file_name).exists():
        return True
    return bool(owner_ref) and (pack_dir / "_staged" / owner_ref / file_name).is_file()


def _effective_pack_files(memory_root: Path, slug: str, pack_dir: Path, owner_ref: str | None = None) -> dict:
    """Reconstruit les 4 fichiers pack en forme legacy depuis la vue effective.
    Renvoie {registry, knowledge_graph, rules, dictionary} (dicts), avec None
    pour un fichier absent (ni baseline ni promoted) — fidèle à la sémantique
    hard-rule #5 de get_pack_full.

    Lot 2 : owner_ref inclut la couche privée _staged/{owner} (build web-only)
    pour que le tenant demandeur voie son pack dans la Memory Bank."""
    eff = load_effective_pack(memory_root, slug, owner_ref=owner_ref)

    out: dict = {}

    if _pack_file_present(pack_dir, "registry.json", owner_ref):
        out["registry"] = _effective_registry(memory_root, slug, pack_dir, owner_ref)
    else:
        out["registry"] = None

    if _pack_file_present(pack_dir, "knowledge_graph.json", owner_ref):
        kg_items = eff["knowledge_graph"]["items"]
        out["knowledge_graph"] = {
            "nodes": [it for it in kg_items if "relation" not in it],
            "edges": [it for it in kg_items if "relation" in it],
            **_baseline_meta(pack_dir, "knowledge_graph.json"),
        }
    else:
        out["knowledge_graph"] = None

    if _pack_file_present(pack_dir, "rules.json", owner_ref):
        out["rules"] = {"rules": eff["rules"]["items"], **_baseline_meta(pack_dir, "rules.json")}
    else:
        out["rules"] = None

    if _pack_file_present(pack_dir, "dictionary.json", owner_ref):
        out["dictionary"] = {
            "entries": eff["dictionary"]["items"],
            **_baseline_meta(pack_dir, "dictionary.json"),
        }
    else:
        out["dictionary"] = None

    return out


# Boardview parsers — the dispatch registry is the source of truth, but we
# materialise the supported extension list once for filesystem scans (the
# registry is keyed on extension, not on file path glob).
_BOARDVIEW_EXTENSIONS = (
    ".kicad_pcb",
    ".brd",
    ".brd2",
    ".asc",
    ".bdv",
    ".bv",
    ".cad",
    ".cst",
    ".f2b",
    ".fz",
    ".gr",
    ".tvw",
)


def _find_boardview(slug: str, pack_dir: Path) -> Path | None:
    """Return the absolute path of the active boardview for this slug, or None.

    Lookup order — same priority chain as `_detect_boardview`:
        1. The active pin from `active_sources.json` (if present).
        2. `board_assets/{slug}.<ext>` — canonical, in-repo demo boards.
        3. `memory/{slug}/uploads/*-boardview-*` — technician-uploaded
           (alphabetical first match).
    Used by both `_detect_boardview` (for the on-disk presence bitmask in
    `PackSummary`) and by `GET /pipeline/packs/{slug}/boardview` (which
    needs the actual path to stream the file).
    """
    pinned = sources.resolve_path(pack_dir, sources.BOARDVIEW_KIND)
    if pinned is not None:
        return pinned

    assets_root = Path.cwd() / "board_assets"
    for ext in _BOARDVIEW_EXTENSIONS:
        candidate = assets_root / f"{slug}{ext}"
        if candidate.exists() and candidate.is_file():
            return candidate

    uploads_dir = pack_dir / "uploads"
    if uploads_dir.exists():
        for path in sorted(uploads_dir.iterdir()):
            if not path.is_file():
                continue
            if "-boardview-" not in path.name:
                continue
            return path
    return None


def _find_owner_boardview(slug: str, pack_dir: Path, owner_ref: str | None) -> Path | None:
    """Per-owner boardview path (T9 — clôt la fuite `/api/board/render`).

    Self-host (owner None) → chaîne globale historique `_find_boardview` (inchangé).
    Managé (owner set) → STRICTEMENT le pin per-owner du tenant
    (`_sources/{owner}/active_sources.json` → `uploads/{filename}`) ; aucun fallback
    racine / `board_assets` / scan `uploads/` → un tenant sans boardview actif
    obtient None (la route répond 404, jamais le board d'un autre tenant).
    """
    if owner_ref is None:
        return _find_boardview(slug, pack_dir)
    from api.pipeline import live_graph  # local — évite tout cycle au chargement du package

    active = live_graph.read_owner_active(pack_dir, owner_ref)
    bv = active.get(sources.BOARDVIEW_KIND)
    if not bv or not bv.get("filename"):
        # Public demo slug: its board is intentionally shared (read-only) — fall
        # back to the global chain (board_assets/{slug}.*) so the example tour
        # renders a board for EVERY tenant. Every other slug stays strict (a
        # tenant without its own pin never sees another tenant's board).
        if slug in live_graph.PUBLIC_DEMO_SLUGS:
            return _find_boardview(slug, pack_dir)
        return None
    candidate = pack_dir / "uploads" / bv["filename"]
    return candidate if candidate.is_file() else None


def _detect_boardview(slug: str, pack_dir: Path, owner_ref: str | None = None) -> tuple[bool, str | None]:
    """Return (present, extension) for a slug's boardview — bitmask helper.

    Per-owner (T9) : managé → le boardview épinglé par CE tenant ; self-host
    (owner None) → chaîne globale. Returns the dotted extension (e.g. ".kicad_pcb")
    so the UI can label the format on the boardview card.
    """
    path = _find_owner_boardview(slug, pack_dir, owner_ref)
    if path is None:
        return False, None
    return True, path.suffix.lower() or None


def _detect_schematic_pdf(slug: str, pack_dir: Path, owner_ref: str | None = None) -> bool:
    """True when a source schematic PDF exists for this slug.

    Per-owner (T9) : managé (owner set) → True ssi CE tenant a un pin schematic
    actif (`_sources/{owner}/active_sources.json`) ; pas de fallback racine/
    board_assets → un tenant sans upload voit `has_schematic_pdf=false`.
    Self-host (owner None) → chaîne globale historique :
      1. Active pin from `active_sources.json`.
      2. `memory/{slug}/schematic.pdf` (canonical post-ingest copy).
      3. `board_assets/{slug}.pdf`.
      4. Any technician-uploaded `*-schematic_pdf-*`.
    """
    if owner_ref is not None:
        from api.pipeline import live_graph  # local — évite tout cycle au chargement

        pin = live_graph.read_owner_active(pack_dir, owner_ref).get(sources.SCHEMATIC_KIND)
        return bool(pin and pin.get("filename"))
    if sources.resolve_path(pack_dir, sources.SCHEMATIC_KIND) is not None:
        return True
    if (pack_dir / "schematic.pdf").exists():
        return True
    if (Path.cwd() / "board_assets" / f"{slug}.pdf").exists():
        return True
    uploads_dir = pack_dir / "uploads"
    if uploads_dir.exists():
        for path in uploads_dir.iterdir():
            if path.is_file() and "-schematic_pdf-" in path.name:
                return True
    return False


def _summarize_pack(pack_dir: Path, owner_ref: str | None = None) -> PackSummary:
    slug = pack_dir.name
    bv_present, bv_ext = _detect_boardview(slug, pack_dir, owner_ref)
    migrated = _is_migrated(pack_dir)
    # T9 : les artefacts PRIVÉS (graphe électrique, boardview, schematic PDF) se
    # résolvent per-owner ; le pack partagé (registry/kg/rules/dictionary/parts_index)
    # reste SHARED. Un tenant sans upload voit donc les cartes privées en « absent ».
    if owner_ref is not None:
        from api.pipeline import live_graph  # local — évite tout cycle au chargement

        has_graph = live_graph.resolve_graph_path(pack_dir, owner_ref) is not None
    else:
        has_graph = (pack_dir / "electrical_graph.json").exists()
    # T8 : sur un pack migré, le bitmask de présence lit baseline/+promoted/ ;
    # le dump est sous audit/. Sinon, les fichiers racine historiques.
    return PackSummary(
        device_slug=slug,
        disk_path=str(pack_dir),
        has_raw_dump=(
            (pack_dir / "audit" / "raw_research_dump.md").exists()
            if migrated
            else (pack_dir / "raw_research_dump.md").exists()
        ),
        has_registry=_writer_present(pack_dir, "registry.json", migrated, owner_ref),
        has_knowledge_graph=_writer_present(pack_dir, "knowledge_graph.json", migrated, owner_ref),
        has_rules=_writer_present(pack_dir, "rules.json", migrated, owner_ref),
        has_dictionary=_writer_present(pack_dir, "dictionary.json", migrated, owner_ref),
        has_audit_verdict=(pack_dir / "audit_verdict.json").exists(),
        has_boardview=bv_present,
        boardview_format=bv_ext,
        has_schematic_pdf=_detect_schematic_pdf(slug, pack_dir, owner_ref),
        has_electrical_graph=has_graph,
        has_parts_index=(pack_dir / "parts_index.json").exists(),
        build_state=(read_build_state(pack_dir) or {}).get("status"),
    )


def _read_optional_json(path: Path) -> dict | None:
    """Return the parsed JSON at path, or None if the file is absent.

    Raises HTTPException(422) if the file exists but is not valid JSON.
    """
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid JSON in {path.name}: {exc}",
        ) from exc


def _pack_is_complete(pack_dir: Path, owner_ref: str | None = None) -> bool:
    """A pack is 'complete' when the 4 writer files are present — audit is optional.

    T8 : sur un pack migré, "présent" = baseline/ ou promoted/ (cf.
    _pack_file_present) ; sinon, fichier racine historique.

    Lot 2 : avec un owner_ref, la couche PRIVÉE `_staged/{owner}/` compte aussi.
    Un build web-only mis en staging pour ce tenant est donc 'complet' POUR LUI
    (il ré-ouvre son repair sans relancer un build) mais reste INCOMPLET pour le
    commons (owner None → free gate fermé, autres tenants → rebuild propre, pas
    de pack web-only servi ni de verrou de slug).

    Build-state veto: the orchestrator writes the files incrementally, so a
    failed build leaves a partial-but-plausible pack behind (surviving rules →
    phantom symptom coverage → a retry never rebuilt). A marker whose status is
    not "complete" (failed/building/paused) vetoes completeness regardless of
    which files survived. NO marker = pack built before the marker existed
    (every self-host pack) = trusted on file presence alone, as before.
    """
    state = read_build_state(pack_dir)
    if state is not None and state.get("status") != "complete":
        return False
    files = ("registry.json", "knowledge_graph.json", "rules.json", "dictionary.json")
    if _is_migrated(pack_dir):
        return all(_pack_file_present(pack_dir, name, owner_ref) for name in files)
    # Non-migré : fichiers racine historiques OU la couche privée de ce tenant
    # (un build web-only mis en staging n'écrit pas la racine et ne pose pas le
    # flag .migrated_t8).
    return all(
        (pack_dir / name).exists()
        or (bool(owner_ref) and (pack_dir / "_staged" / owner_ref / name).is_file())
        for name in files
    )


@router.get("/packs", response_model=list[PackSummary])
async def list_packs(
    x_owner_ref: str | None = Header(default=None, alias="X-Owner-Ref"),
) -> list[PackSummary]:
    settings = _pkg.get_settings()
    root = Path(settings.memory_root)
    if not root.exists():
        return []
    return sorted(
        (_summarize_pack(d, x_owner_ref) for d in root.iterdir() if d.is_dir()),
        key=lambda s: s.device_slug,
    )


@router.get("/taxonomy", response_model=TaxonomyTree)
async def get_taxonomy() -> TaxonomyTree:
    """Scan every pack's registry.json and group by taxonomy.

    A pack lands in `brands[brand][model]` when both `taxonomy.brand` and
    `taxonomy.model` are present; otherwise it falls to `uncategorized`. The UI
    uses this to populate the 'New repair' modal's accordion by manufacturer
    and the home section headers.
    """
    settings = _pkg.get_settings()
    root = Path(settings.memory_root)
    tree = TaxonomyTree()
    if not root.exists():
        return tree

    # T9a: one carnet read → map each device's aliases so the autocomplete can
    # match by board#/model/EMC. Best-effort: a registry hiccup just leaves
    # aliases empty (the label-based filter still works).
    aliases_by_slug: dict[str, list[str]] = {}
    try:
        from api.pipeline.device_registry import get_device_registry_store

        for ident in await get_device_registry_store(root).list():
            vals: list[str] = []
            for items in (ident.get("facets") or {}).values():
                vals.extend(items)
            aliases_by_slug[ident["canonicalKey"]] = vals
    except Exception:  # noqa: BLE001 - autocomplete enrichment must never 500 the list
        logger.warning("[Taxonomy] carnet alias enrichment failed", exc_info=True)

    for pack_dir in sorted(root.iterdir(), key=lambda p: p.name):
        if not pack_dir.is_dir():
            continue
        # T8 : sur un pack migré, taxonomy/device_label vivent dans la registry
        # reconstruite depuis baseline/_meta + vue effective.
        if _is_migrated(pack_dir):
            if not _pack_file_present(pack_dir, "registry.json"):
                continue
            registry = _effective_registry(root, pack_dir.name, pack_dir)
        else:
            registry = _read_optional_json(pack_dir / "registry.json")
            if registry is None:
                continue

        taxonomy = registry.get("taxonomy") or {}
        brand = taxonomy.get("brand")
        model = taxonomy.get("model")

        entry = TaxonomyPackEntry(
            device_slug=pack_dir.name,
            device_label=registry.get("device_label") or pack_dir.name,
            version=taxonomy.get("version"),
            form_factor=taxonomy.get("form_factor"),
            complete=_pack_is_complete(pack_dir),
            # Taxonomy is a global (non-owner-scoped) view; mirrors _summarize_pack's self-host branch.
            has_electrical_graph=(pack_dir / "electrical_graph.json").exists(),
            has_parts_index=(pack_dir / "parts_index.json").exists(),
            device_kind=taxonomy.get("device_kind"),
            aliases=aliases_by_slug.get(pack_dir.name, []),
        )

        if brand and model:
            tree.brands.setdefault(brand, {}).setdefault(model, []).append(entry)
        else:
            tree.uncategorized.append(entry)

    return tree


@router.get("/packs/{device_slug}", response_model=PackSummary)
async def get_pack(
    device_slug: str,
    x_owner_ref: str | None = Header(default=None, alias="X-Owner-Ref"),
) -> PackSummary:
    settings = _pkg.get_settings()
    root = Path(settings.memory_root)
    # Normalize: accept either a raw slug or a device_label.
    slug = _slugify(device_slug)
    pack_dir = root / slug
    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")
    return _summarize_pack(pack_dir, x_owner_ref)


@router.get("/packs/{device_slug}/full")
async def get_pack_full(
    device_slug: str,
    x_owner_ref: str | None = Header(default=None, alias="X-Owner-Ref"),
) -> dict:
    """Return every JSON artefact of a pack in a single payload.

    Missing files become `null` — never fabricated (hard rule #4). Consumed by
    the Memory Bank UI so it can render all five sections in one fetch.

    Lot 2 : owner-aware. Un build web-only mis en staging pour ce tenant
    (`_staged/{owner}`) est servi via la vue effective ; le commons (sans header)
    ne voit rien (pack privé).
    """
    settings = _pkg.get_settings()
    slug = _slugify(device_slug)
    pack_dir = Path(settings.memory_root) / slug
    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")

    owner_has_staged = bool(x_owner_ref) and (pack_dir / "_staged" / x_owner_ref).is_dir()
    if _is_migrated(pack_dir) or owner_has_staged:
        files = _effective_pack_files(Path(settings.memory_root), slug, pack_dir, x_owner_ref)
        registry = files["registry"]
        knowledge_graph = files["knowledge_graph"]
        rules = files["rules"]
        dictionary = files["dictionary"]
    else:
        registry = _read_optional_json(pack_dir / "registry.json")
        knowledge_graph = _read_optional_json(pack_dir / "knowledge_graph.json")
        rules = _read_optional_json(pack_dir / "rules.json")
        dictionary = _read_optional_json(pack_dir / "dictionary.json")
    audit_verdict = _read_optional_json(pack_dir / "audit_verdict.json")

    device_label = (registry or {}).get("device_label") or slug

    return {
        "device_slug": slug,
        "device_label": device_label,
        "registry": registry,
        "knowledge_graph": knowledge_graph,
        "rules": rules,
        "dictionary": dictionary,
        "audit_verdict": audit_verdict,
    }


@router.get("/packs/{device_slug}/findings")
async def list_device_findings(device_slug: str, limit: int = 50) -> list[dict]:
    """Return every field report recorded for this device, newest first.

    Same content the agent reads via grep on the FUSE mount, exposed to
    the web UI so the Journal dashboard can render cross-session memory
    without a WS round-trip. Strictly JSON-on-disk — no MA memory-store.
    """
    return list_field_reports(device_slug=_validate_slug(device_slug), limit=limit)


@router.post("/packs/{device_slug}/expand")
async def expand_device_pack(
    device_slug: str,
    request: ExpandRequest,
    x_owner_ref: str | None = Header(default=None, alias="X-Owner-Ref"),
) -> dict:
    """Grow an existing pack's memory bank around a focus symptom area.

    Called by the diagnostic agent via the `mb_expand_knowledge` tool when
    the current ruleset comes up empty for a live symptom. Runs a targeted
    Scout + Registry + Clinicien mini-pipeline and merges the output into
    the existing pack. See api/pipeline/expansion.py for the mechanics.

    T8 : l'en-tête X-Owner-Ref (injecté par le cloud, opaque côté moteur) scope
    l'enrichissement au tenant (added_by_tenant dans la provenance). Absent →
    None (self-host).
    """
    slug = _slugify(device_slug)
    logger.info(
        "[API] /packs/%s/expand · focus=%s · refdes=%s · owner=%s",
        slug,
        request.focus_symptoms,
        request.focus_refdes,
        x_owner_ref,
    )
    try:
        return await _pkg.expand_pack(
            device_slug=slug,
            focus_symptoms=request.focus_symptoms,
            focus_refdes=request.focus_refdes,
            owner_ref=x_owner_ref,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/packs/{device_slug}/graph")
async def get_pack_graph(device_slug: str) -> dict:
    """Return the combined graph payload ({nodes, edges}) consumed by web/index.html."""
    settings = _pkg.get_settings()
    slug = _slugify(device_slug)
    pack_dir = Path(settings.memory_root) / slug
    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")

    if _is_migrated(pack_dir):
        files = _effective_pack_files(Path(settings.memory_root), slug, pack_dir)
        missing = [k for k in ("registry", "knowledge_graph", "rules", "dictionary") if files[k] is None]
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"Pack for {slug!r} is incomplete: {missing[0]}.json",
            )
        registry = files["registry"]
        knowledge_graph = files["knowledge_graph"]
        rules = files["rules"]
        dictionary = files["dictionary"]
    else:
        try:
            registry = json.loads((pack_dir / "registry.json").read_text())
            knowledge_graph = json.loads((pack_dir / "knowledge_graph.json").read_text())
            rules = json.loads((pack_dir / "rules.json").read_text())
            dictionary = json.loads((pack_dir / "dictionary.json").read_text())
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Pack for {slug!r} is incomplete: {exc.filename}",
            ) from exc

    return pack_to_graph_payload(
        registry=registry,
        knowledge_graph=knowledge_graph,
        rules=rules,
        dictionary=dictionary,
    )


class ConfirmKindRequest(BaseModel):
    """Body for POST /packs/{slug}/confirm-kind — the technician's resolved kind.

    `device_kind` is validated against the `_DeviceKind` Literal, so an unknown
    value (e.g. "toaster") is rejected with HTTP 422 by Pydantic before the
    endpoint body runs.
    """

    device_kind: _DeviceKind


@router.post("/packs/{device_slug}/confirm-kind")
async def confirm_pack_kind(
    device_slug: str,
    payload: ConfirmKindRequest,
    x_owner_ref: str | None = Header(default=None, alias="X-Owner-Ref"),
) -> dict:
    """Record the technician's resolved device kind and re-run the pipeline.

    When the pipeline detects a mismatch between the user-declared kind and the
    kind inferred from the (partial) graph, it short-circuits with
    NEEDS_KIND_CONFIRMATION and writes `pending_kind.json`. The UI surfaces the
    two candidates; the technician picks one and POSTs it here. We clear the
    pending state and fire a fresh build with `confirmed_device_kind` so the
    orchestrator trusts the human verdict instead of re-detecting.

    The pipeline is kicked off as a background task — the build takes 30–120 s,
    so the endpoint returns `status="rebuilding"` immediately. Mirrors the
    fire-and-forget pattern of POST /repairs.
    """
    settings = _pkg.get_settings()
    memory_root = Path(settings.memory_root)
    slug = _validate_slug(device_slug)
    pack_dir = memory_root / slug

    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")

    # Local import: repairs.py imports packs.py at module load (it pulls in
    # `_pack_is_complete`), so importing repairs at packs' module top would be
    # a circular import. Deferring it to the function body breaks the cycle.
    from api.pipeline import events
    from api.pipeline.routes.repairs import (
        _builds_at_capacity,
        _enqueue_build,
        _queue_position,
        _register_build,
        _run_pipeline_with_events,
        _slug_is_building,
        _slug_queued,
    )

    # Clear the pending marker first — even if the rerun fails to start the
    # tech's verdict is recorded and the stale "needs confirmation" state is gone.
    device_kind_module.clear_pending_kind(pack_dir)

    # Recover the original device_label so the rerun targets the same pack dir.
    # `generate_knowledge_pack` re-slugifies its first arg internally; the slug
    # round-trips to itself, so the slug is a safe fallback when no registry
    # device_label is on disk. T8 : sur un pack migré, device_label vit dans la
    # registry reconstruite (baseline/_meta + vue effective), pas à la racine.
    if _is_migrated(pack_dir):
        registry = _effective_registry(memory_root, slug, pack_dir)
    else:
        registry = _read_optional_json(pack_dir / "registry.json")
    device_label = (registry or {}).get("device_label") or slug

    # Stampede guard: a build/expand for this shared-by-slug pack is already in
    # flight → don't launch a duplicate (same rationale as create_repair). The
    # tech's verdict is already persisted (pending cleared above); the in-flight
    # build streams on /pipeline/progress/{slug}.
    if _slug_is_building(slug):
        logger.info(
            "[API] /packs/%s/confirm-kind · slug already building — joining in-flight build",
            slug,
        )
        return {
            "device_slug": slug,
            "confirmed_kind": payload.device_kind,
            "status": "rebuilding",
        }

    # Already queued for this slug → join it (no duplicate), return its position.
    if _slug_queued(slug):
        return {
            "device_slug": slug,
            "confirmed_kind": payload.device_kind,
            "status": "queued",
            "queue_position": _queue_position(slug),
        }

    logger.info(
        "[API] /packs/%s/confirm-kind · confirmed_kind=%s — clearing pending, rebuilding",
        slug,
        payload.device_kind,
    )

    # Reuse the repairs.py pipeline-run wrapper so background exceptions publish a
    # `pipeline_failed` event on the bus, and register the task (counted against
    # the build cap) so POST /repairs/{slug}/cancel can cancel it cooperatively.
    def _launch():
        # T13: the rebuild's spend is attributed to the confirming tenant (the
        # cloud injects X-Owner-Ref on all proxied traffic); no single repair_id
        # backs a kind-confirmation rebuild → build_metering keys on the slug.
        return _run_pipeline_with_events(
            device_label, slug,
            confirmed_device_kind=payload.device_kind,
            owner_ref=x_owner_ref,
        )

    # Same concurrent-build cap as create_repair: at capacity, ENQUEUE (visible
    # position) instead of 503 — the rebuild starts when a slot frees.
    if _builds_at_capacity():
        pos = _enqueue_build(slug, _launch)
        await events.publish(slug, {"type": "queued", "position": pos, "ahead": pos - 1})
        logger.info("[API] /packs/%s/confirm-kind · rebuild queued at position %d", slug, pos)
        return {
            "device_slug": slug,
            "confirmed_kind": payload.device_kind,
            "status": "queued",
            "queue_position": pos,
        }

    _register_build(slug, asyncio.create_task(_launch()))
    return {
        "device_slug": slug,
        "confirmed_kind": payload.device_kind,
        "status": "rebuilding",
    }


@router.get("/packs/{device_slug}/pending-kind")
async def get_pending_kind(device_slug: str) -> dict:
    """Return the pending device-kind disagreement for this pack, or 404.

    When the pipeline pauses on a kind mismatch it writes `pending_kind.json`
    and emits a one-shot `pipeline_paused` event. A page reload misses that
    event, so the UI calls this to rebuild the inline confirmation panel from
    the persisted state (user_declared / graph_inferred / confidence / evidence).
    """
    settings = _pkg.get_settings()
    slug = _validate_slug(device_slug)
    pack_dir = Path(settings.memory_root) / slug
    if not pack_dir.exists():
        raise HTTPException(status_code=404, detail=f"No pack for device_slug={slug!r}")
    pending = device_kind_module.read_pending_kind(pack_dir)
    if pending is None:
        raise HTTPException(status_code=404, detail=f"No pending kind for {slug!r}")
    return pending
