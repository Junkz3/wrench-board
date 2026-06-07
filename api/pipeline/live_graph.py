"""T9 — résolution du graphe vif (live) par (slug, owner_ref).

Mode managé (owner set) : un pointeur per-owner _sources/{owner_ref}/active_sources.json
mappe kind → {filename, hash} ; les lecteurs résolvent owner→hash→cache partagé
.cache_schematic/{hash}/. Self-host (owner None) : chemin racine actuel, inchangé.

Le cache .cache_schematic/{hash}/ reste PARTAGÉ par hash de PDF (le moat) : deux tenants
avec le même PDF lisent les mêmes fichiers, zéro duplication. Calque les patrons
owner-scoping de stock/store.py + conversation_log.py.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from api.pipeline import sources

_SAFE_OWNER = re.compile(r"^[A-Za-z0-9_-]+$")

# Slugs we deliberately publish as demo devices: their rendered pages + cache
# are SHARED (read-only) with every tenant, so the first-run example tour shows
# full schematic pages without a per-owner upload. The moat is unaffected — only
# these explicitly-listed slugs get the page fallback.
PUBLIC_DEMO_SLUGS = {"mnt-reform-motherboard"}


def _check_owner(owner_ref: str) -> str:
    if not _SAFE_OWNER.match(owner_ref):
        raise ValueError(f"invalid owner_ref: {owner_ref!r}")
    return owner_ref


def _owner_sources_dir(pack_dir: Path, owner_ref: str) -> Path:
    return pack_dir / "_sources" / _check_owner(owner_ref)


def read_owner_active(pack_dir: Path, owner_ref: str | None) -> dict:
    """Pointeur actif. Owner None → format historique racine normalisé
    {kind:{filename,hash:None}} ; owner set → _sources/{owner}/active_sources.json."""
    if owner_ref is None:
        return {k: {"filename": v, "hash": None} for k, v in sources.read_active(pack_dir).items() if v}
    path = _owner_sources_dir(pack_dir, owner_ref) / sources.ACTIVE_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_owner_active(pack_dir: Path, owner_ref: str | None, kind: str, filename: str, pdf_hash: str | None) -> None:
    """Pin per-owner (managé) ou racine (self-host, délègue au write_active historique).
    Écriture atomique (tmp + replace)."""
    if kind not in sources.KNOWN_KINDS:
        raise ValueError(f"unknown kind: {kind!r}")
    if owner_ref is None:
        pins = sources.read_active(pack_dir)
        pins[kind] = filename
        sources.write_active(pack_dir, pins)
        return
    d = _owner_sources_dir(pack_dir, owner_ref)
    d.mkdir(parents=True, exist_ok=True)
    path = d / sources.ACTIVE_FILE
    current: dict = {}
    if path.is_file():
        try:
            current = json.loads(path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            current = {}
    current[kind] = {"filename": filename, "hash": pdf_hash}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def clear_owner_active(pack_dir: Path, owner_ref: str, kind: str) -> None:
    """Retire un kind du pointeur per-owner (managé). No-op si absent.
    Écriture atomique (tmp + replace). Owner None n'a pas de sens ici
    (le self-host gère son pin racine via sources.write_active)."""
    if kind not in sources.KNOWN_KINDS:
        raise ValueError(f"unknown kind: {kind!r}")
    path = _owner_sources_dir(pack_dir, owner_ref) / sources.ACTIVE_FILE
    if not path.is_file():
        return
    try:
        current = json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        current = {}
    if kind not in current:
        return
    current.pop(kind, None)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def resolve_cache_dir(pack_dir: Path, owner_ref: str | None) -> Path | None:
    """Répertoire des artefacts du graphe vif pour (slug, owner_ref), ou None.

    Self-host (owner None) : la racine du slug (matérialisation en place, INCHANGÉ).
    Managé (owner set) : _sources/{owner}/active → hash → .cache_schematic/{hash}/.
    Primitive unique : fichiers (electrical_graph.json…) ET répertoire pages s'y résolvent.
    """
    if owner_ref is None:
        return pack_dir
    active = read_owner_active(pack_dir, owner_ref)
    sch = active.get(sources.SCHEMATIC_KIND)
    if not sch or not sch.get("hash"):
        # No per-owner pin. For a public demo slug, fall back to the shared
        # root (pages are intentionally public there); otherwise stay strict
        # (private renders never leak across owners).
        if pack_dir.name in PUBLIC_DEMO_SLUGS and (pack_dir / "schematic_pages").is_dir():
            return pack_dir
        return None
    cache = sources.cache_dir_for(pack_dir, sch["hash"])
    return cache if cache.is_dir() else None


def resolve_graph_dir(pack_dir: Path, owner_ref: str | None) -> Path | None:
    """Répertoire d'où lire les DONNÉES analysées du graphe — le moat PARTAGÉ (T6).

    - Owner avec pin actif → sa base per-owner (override : son propre PDF).
    - Managé SANS pin → fallback CANONIQUE = la racine du slug (owner=None), qui
      est le graphe partagé/curé du device → un tenant peut diaguer une carte
      connue sans uploader son PDF (le moat). N'est servi que si un graphe
      canonique existe réellement (sinon None → 404).
    - Self-host (owner None) → racine (resolve_cache_dir == pack_dir), inchangé.

    RÉSERVÉ aux DONNÉES analysées (electrical_graph.json & co.). Les RENDUS du
    fichier brut (pages PNG du PDF, boardview) restent PRIVÉS → ils passent par
    resolve_cache_dir (strict, AUCUN fallback) / resolve_owner_boardview.
    """
    base = resolve_cache_dir(pack_dir, owner_ref)
    if base is not None:
        return base
    if owner_ref is not None and (pack_dir / "electrical_graph.json").is_file():
        return pack_dir
    return None


def resolve_graph_path(pack_dir: Path, owner_ref: str | None, artifact: str = "electrical_graph.json") -> Path | None:
    base = resolve_graph_dir(pack_dir, owner_ref)
    if base is None:
        return None
    p = base / artifact
    return p if p.is_file() else None


def resolve_pages_dir(pack_dir: Path, owner_ref: str | None) -> Path | None:
    base = resolve_cache_dir(pack_dir, owner_ref)
    if base is None:
        return None
    pages = base / "schematic_pages"
    return pages if pages.is_dir() else None
