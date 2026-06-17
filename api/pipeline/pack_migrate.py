"""T8 — Migration in-place idempotente du layout legacy vers T8.

Déclenchée au premier accès à un slug (cf. _load_pack dans api/agent/tools.py
après T8 — Task 5) ou explicitement par la CLI admin.

Stratégie :
1. Si .migrated_t8 existe → no-op
2. Si registry.json existe à la racine (layout pré-T8 détecté) → on migre :
   - init_pack_layout crée baseline/promoted/_staged/expansions/audit
   - les 4 JSON pack sont déplacés vers baseline/ en attachant une Provenance
     synthétique baseline-pre-T8 à chaque fact ({items: [...], _meta: {...}})
   - raw_research_dump.md est déplacé vers audit/ (privé / audit moteur)
   - une ligne 'baseline-pre-T8' est ajoutée au journal (status=baseline,
     non-revocable par design — cf. revoke_expansion dans pack_storage.py)
3. touch .migrated_t8

Idempotent : un crash en cours peut laisser un pack à moitié migré ; le
prochain appel détectera l'absence du flag et reprendra. Chaque fichier est
traité avec write-then-rename (_atomic_write_json) ; si la destination existe
déjà (reprise), on supprime juste la source legacy.

Formats legacy hétérogènes normalisés en {items: [...]} T8.
Clés portant les listes, vérifiées contre les packs réels sur disque :
  registry.json       : {"components": [...], "signals": [...]} → concaténés
  rules.json          : {"rules": [...]}
  knowledge_graph.json: {"nodes": [...], "edges": [...]} → concaténés
  dictionary.json     : {"entries": [...]}  ← 'entries', PAS 'components'

Les clés non-liste (schema_version, device_label, taxonomy, …) sont
préservées sous une clé _meta dans le fichier baseline migré — zéro perte.
load_effective_pack ignore _meta (il ne lit que items) ; Task 5 câblera
taxonomy/device_label à partir de _meta quand le loader en aura besoin.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

from api.pipeline.pack_storage import (
    JournalEntry,
    _atomic_write_json,
    append_journal,
    init_pack_layout,
    read_journal,
)

# Mapping: nom du fichier legacy → (nom cible dans baseline/, clé d'items dans le legacy JSON)
_LEGACY_FILE_TO_T8: dict[str, tuple[str, str]] = {
    "registry.json":       ("registry.json",       "registry"),
    "rules.json":          ("rules.json",           "rules"),
    "knowledge_graph.json":("knowledge_graph.json", "knowledge_graph"),
    "dictionary.json":     ("dictionary.json",      "dictionary"),
}


def migrate_pack_if_needed(memory_root: Path, slug: str) -> None:
    """Point d'entrée idempotent. Coûte un stat() si déjà migré.

    - Si le répertoire slug n'existe pas → no-op silencieux.
    - Si .migrated_t8 est présent → no-op (pack déjà au format T8).
    - Si aucun fichier legacy n'est détecté (répertoire vide, ou déjà partiellement
      migré sans flag) → crée le layout + pose le flag sans écriture de journal.
    """
    pack = memory_root / slug
    if not pack.is_dir():
        return

    flag = pack / ".migrated_t8"
    if flag.is_file():
        return

    # Crée les sous-répertoires T8 (idempotent).
    init_pack_layout(memory_root, slug)

    legacy_present = any((pack / fname).is_file() for fname in _LEGACY_FILE_TO_T8)

    if legacy_present:
        _migrate_legacy_files(pack)
        _migrate_raw_dump(pack)
        _create_baseline_journal_entry(memory_root, slug, pack)

    # Le flag est posé inconditionnellement — même pour un répertoire vide.
    # Hypothèse valide : les packs new-pipeline (post-T8) écrivent directement
    # au format T8 natif ; les fichiers legacy, s'il y en a, sont TOUJOURS
    # déjà présents avant le premier appel à migrate_pack_if_needed (qui est
    # déclenché depuis _load_pack, après que le build du pack ait terminé).
    # Un répertoire slug vide = pack new-pipeline, aucune migration nécessaire.
    flag.touch()


def _migrate_legacy_files(pack: Path) -> None:
    """Déplace les 4 JSON legacy vers baseline/ en wrappant chaque entrée
    avec une Provenance synthétique baseline-pre-T8.

    Pré-condition : au moins un fichier legacy est présent (vérifiée par
    l'appelant via `legacy_present`), donc max() sur un itérateur non-vide.
    """
    # Utilise le mtime du fichier le plus récent comme timestamp de provenance
    # (approximation de la date de création du pack original).
    file_mtime = max(
        (pack / fname).stat().st_mtime
        for fname in _LEGACY_FILE_TO_T8
        if (pack / fname).is_file()
    )
    file_mtime_iso = datetime.fromtimestamp(file_mtime, tz=UTC).isoformat()

    base_prov = {
        "expansion_id": "baseline-pre-T8",
        "added_at": file_mtime_iso,
        "added_by_tenant": None,
        "confidence": 1.0,
        "source_kind": "baseline",
        "sanitizer_actions": [],
        "status": "baseline",
    }

    for legacy_name, (t8_name, items_field) in _LEGACY_FILE_TO_T8.items():
        src = pack / legacy_name
        dst = pack / "baseline" / t8_name
        if not src.is_file():
            continue
        # Reprise d'un crash : la destination existe déjà → supprime la source.
        if dst.is_file():
            src.unlink(missing_ok=True)
            continue
        try:
            legacy_data = json.loads(src.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # JSON corrompu : on supprime et on laisse baseline vide pour ce fichier.
            src.unlink(missing_ok=True)
            continue

        items = _flatten_legacy_payload(legacy_data, items_field)
        # Clés non-liste à conserver (schema_version, device_label, taxonomy, …).
        # _meta est préservé-mais-pas-encore-consommé par load_effective_pack ;
        # Task 5 câblera taxonomy/device_label à partir de _meta.
        list_keys = _LIST_KEYS[items_field]
        meta = {k: v for k, v in legacy_data.items() if k not in list_keys}

        for it in items:
            # Ne pas écraser une provenance déjà présente (pack semi-migré).
            # deep-copy : chaque fact a sa propre liste sanitizer_actions —
            # pas d'alias partagé entre facts (évite les mutations silencieuses).
            it.setdefault("_provenance", copy.deepcopy(base_prov))

        payload: dict = {"items": items}
        if meta:
            payload["_meta"] = meta
        _atomic_write_json(dst, payload)
        src.unlink()


# Clés portant les listes pour chaque type de fichier.
# Vérifiées contre les packs réels sur disque (STEP 0 — 2026-05-28).
# Toute clé absente de ce tuple est considérée métadonnée et préservée dans _meta.
_LIST_KEYS: dict[str, tuple[str, ...]] = {
    "registry":       ("components", "signals"),
    "rules":          ("rules",),
    "knowledge_graph": ("nodes", "edges"),
    "dictionary":     ("entries",),   # 'entries' — PAS 'components' (bug corrigé)
}


def _flatten_legacy_payload(legacy_data: dict, kind: str) -> list[dict]:
    """Normalise les formats hétérogènes du pack legacy en une liste plate d'items.

    Clés portant les listes (vérifiées contre les packs réels) :
      registry.json       → components + signals concaténés
      rules.json          → rules
      knowledge_graph.json→ nodes + edges concaténés
      dictionary.json     → entries  (PAS 'components' comme supposé à tort dans le plan)
    """
    if kind == "registry":
        return (
            list(legacy_data.get("components") or [])
            + list(legacy_data.get("signals") or [])
        )
    if kind == "rules":
        return list(legacy_data.get("rules") or [])
    if kind == "knowledge_graph":
        return (
            list(legacy_data.get("nodes") or [])
            + list(legacy_data.get("edges") or [])
        )
    if kind == "dictionary":
        # BUG CORRIGÉ : le schéma Dictionary et tous les packs réels utilisent
        # 'entries', pas 'components'. Lire 'components' produisait {items: []}
        # (liste vide) et déliait la source → perte définitive des fiches composants.
        return list(legacy_data.get("entries") or [])
    # Cas exhaustif — ne devrait jamais arriver vu _LEGACY_FILE_TO_T8.
    return []


def stage_web_only_pack(memory_root: Path, slug: str, *, owner_ref: str) -> None:
    """Lot 2 — isole un build WEB-ONLY (sans schéma) dans _staged/{owner_ref}/.

    Un build sans schématique produit les 4 fichiers writers à la RACINE comme
    un build normal (le pipeline les lit en interne pendant l'audit/revise).
    Appelée à la FIN d'un tel build en contexte managé (owner_ref présent), elle
    relocalise ces fichiers vers la couche privée `_staged/{owner_ref}/` au lieu
    de les laisser migrer vers `baseline/` (la couche PARTAGÉE par slug).

    Effet (décision Alex Q1) : le tenant qui a demandé le diagnostic voit son
    pack via la vue effective (`load_effective_pack(owner_ref)`), mais le commons
    reste byte-clean — un pack web-only de qualité non vérifiée n'est jamais
    servi aux autres réparateurs ni verrouille le slug pour un futur build
    schématique (la racine vidée n'est plus migrable vers baseline/, et
    `_pack_is_complete` du pack partagé reste faux).

    Chaque fact reçoit une provenance owner-scopée (`source_kind=web_only_build`,
    `status=staged`, `expansion_id=web-only-{owner_ref}`) → traçable et révocable
    par le CLI opérateur (`revoke_expansion`), comme une expansion.
    """
    pack = memory_root / slug
    if not pack.is_dir():
        return
    init_pack_layout(memory_root, slug)

    file_mtimes = [
        (pack / fname).stat().st_mtime
        for fname in _LEGACY_FILE_TO_T8
        if (pack / fname).is_file()
    ]
    added_at = (
        datetime.fromtimestamp(max(file_mtimes), tz=UTC).isoformat()
        if file_mtimes
        else datetime.now(UTC).isoformat()
    )
    prov = {
        "expansion_id": f"web-only-{owner_ref}",
        "added_at": added_at,
        "added_by_tenant": owner_ref,
        "confidence": 0.5,
        "source_kind": "web_only_build",
        "sanitizer_actions": [],
        "status": "staged",
    }

    staged_dir = pack / "_staged" / owner_ref
    staged_dir.mkdir(parents=True, exist_ok=True)
    for legacy_name, (t8_name, items_field) in _LEGACY_FILE_TO_T8.items():
        src = pack / legacy_name
        if not src.is_file():
            continue
        try:
            legacy_data = json.loads(src.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            src.unlink(missing_ok=True)
            continue
        items = _flatten_legacy_payload(legacy_data, items_field)
        meta = {k: v for k, v in legacy_data.items() if k not in _LIST_KEYS[items_field]}
        for it in items:
            it.setdefault("_provenance", copy.deepcopy(prov))
        payload: dict = {"items": items}
        if meta:
            payload["_meta"] = meta
        _atomic_write_json(staged_dir / t8_name, payload)
        src.unlink()

    _migrate_raw_dump(pack)


def _migrate_raw_dump(pack: Path) -> None:
    """Déplace raw_research_dump.md vers audit/ (données brutes, privé moteur)."""
    src = pack / "raw_research_dump.md"
    if not src.is_file():
        return
    dst = pack / "audit" / "raw_research_dump.md"
    if dst.is_file():
        # Reprise : destination déjà présente, source à nettoyer.
        src.unlink(missing_ok=True)
        return
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    src.unlink()


def _create_baseline_journal_entry(memory_root: Path, slug: str, pack: Path) -> None:
    """Ajoute la ligne baseline-pre-T8 au journal si elle n'y est pas encore.

    Cette entrée est non-revocable par design (voir revoke_expansion dans
    pack_storage.py qui refuse explicitement expansion_id == 'baseline-pre-T8').
    """
    existing = list(read_journal(memory_root, slug))
    if any(e.id == "baseline-pre-T8" for e in existing):
        return

    # Le mtime des fichiers baseline est notre meilleure approximation du
    # moment de création du pack original.
    baseline_dir = pack / "baseline"
    ts = datetime.now(UTC)
    for fname in _LEGACY_FILE_TO_T8:
        candidate = baseline_dir / fname
        if candidate.is_file():
            ts = datetime.fromtimestamp(candidate.stat().st_mtime, tz=UTC)
            break

    append_journal(
        memory_root,
        slug,
        JournalEntry(
            id="baseline-pre-T8",
            ts=ts,
            owner_ref=None,
            slug=slug,
            focus_symptoms=[],
            focus_refdes=[],
            delta_summary={
                "new_components": [],
                "new_rules": [],
                "new_nodes": [],
                "new_edges": [],
            },
            scout_dump_range={"start": 0, "end": 0},
            status="baseline",
        ),
    )
