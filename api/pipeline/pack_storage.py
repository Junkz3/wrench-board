"""T8 — I/O de la topologie pack T8 (baseline + promoted + _staged + journal + audit).

Pas de logique métier ici : pas de sanitisation, pas de LLM, pas de génération
de Provenance. Ce module reçoit des facts Pydantic prêts à écrire et les
persiste atomiquement (write-then-rename).

Pourquoi un module dédié : (1) testabilité unitaire sans LLM ; (2) la
topologie est invariante alors que expansion.py change souvent — on isole
les opérations de stockage ici, comme dans un repository en hexagonal.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from api.pipeline.schemas import COMPONENT_KINDS

logger = logging.getLogger("wrench_board.pipeline.pack_storage")

_PACK_FILES = ("registry.json", "knowledge_graph.json", "rules.json", "dictionary.json")


def init_pack_layout(memory_root: Path, slug: str) -> None:
    """Crée la topologie T8 pour un slug. Idempotent."""
    base = memory_root / slug
    for sub in ("baseline", "promoted", "_staged", "expansions", "audit"):
        (base / sub).mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, data: dict) -> None:
    """Écrit `data` en JSON dans `path` de façon atomique (tmp + rename).
    Sur Linux, rename() est atomique sur le même filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=_json_default)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if Path(tmp).exists():
            os.unlink(tmp)
        raise


def _json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def _slug_lock_path(memory_root: Path, slug: str) -> Path:
    return memory_root / slug / ".expansions.lock"


class _SlugLock:
    """fcntl-based exclusive lock, file-scoped. Évite les race append/read
    sur expansions.jsonl + write_staged simultanés sur le même slug."""

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a")
        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()
            self.fh = None


def write_staged(
    memory_root: Path,
    slug: str,
    *,
    owner_ref: str,
    file_name: str,
    new_facts: Iterable[BaseModel],
) -> None:
    """Append des facts dans _staged/{owner_ref}/{file_name}. Atomique."""
    assert file_name in _PACK_FILES, f"unexpected pack file {file_name!r}"
    init_pack_layout(memory_root, slug)
    target = memory_root / slug / "_staged" / owner_ref / file_name
    with _SlugLock(_slug_lock_path(memory_root, slug)):
        existing = _read_items_or_empty(target)
        for f in new_facts:
            existing.append(_model_to_dict(f))
        _atomic_write_json(target, {"items": existing})


def write_promoted_facts(
    memory_root: Path,
    slug: str,
    *,
    file_name: str,
    new_facts: Iterable[BaseModel | dict],
) -> None:
    """Fusionne des facts (Pydantic models OU dicts déjà sérialisés) dans
    promoted/{file_name}, par clé canonique (_key_fn_for). Un fact dont la clé
    existe déjà ÉCRASE l'ancien (override — l'expansion a reconstruit une
    version plus récente). Atomique.

    Utilisé par le flux d'expansion Option C : écriture directe dans la couche
    partagée promoted/, sans passer par staged → promote. Les enrichissements
    sont partagés immédiatement (le moat T6) mais PII-free + tracés + revocables
    (chaque fact porte sa _provenance).
    """
    assert file_name in _PACK_FILES, f"unexpected pack file {file_name!r}"
    init_pack_layout(memory_root, slug)
    target = memory_root / slug / "promoted" / file_name
    key_fn = _key_fn_for(file_name)
    with _SlugLock(_slug_lock_path(memory_root, slug)):
        existing = _read_items_or_empty(target)
        # Index par clé canonique : préserve l'ordre d'insertion (dict ordonné),
        # un même-clé écrase en place sans dupliquer. Les items malformés (sans
        # clé d'identité) sont conservés tels quels en queue (mieux qu'une perte).
        by_key: dict[tuple, dict] = {}
        no_key: list[dict] = []
        for it in existing:
            try:
                by_key[key_fn(it)] = it
            except KeyError:
                no_key.append(it)
        for f in new_facts:
            item = f if isinstance(f, dict) else _model_to_dict(f)
            try:
                by_key[key_fn(item)] = item  # override si la clé existe déjà
            except KeyError:
                no_key.append(item)
        _atomic_write_json(target, {"items": list(by_key.values()) + no_key})


def _read_items_or_empty(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data.get("items") or [])
    except json.JSONDecodeError:
        return []


def _model_to_dict(m: BaseModel) -> dict:
    """Sérialise un BaseModel en dict, avec l'alias _provenance préservé."""
    return m.model_dump(by_alias=True, mode="json")


def load_effective_pack(memory_root: Path, slug: str, *, owner_ref: str | None) -> dict:
    """Retourne le pack effectif vu par owner_ref, fusion des trois couches.

    Résolution staged > promoted > baseline par canonical_name/id.
    """
    base = memory_root / slug
    out: dict[str, dict] = {}
    for fname in _PACK_FILES:
        layers: list[list[dict]] = []
        layers.append(_read_items_or_empty(base / "baseline" / fname))
        layers.append(_read_items_or_empty(base / "promoted" / fname))
        if owner_ref:
            layers.append(_read_items_or_empty(base / "_staged" / owner_ref / fname))
        merged = _merge_layers_by_key(fname, layers)
        kind_key = fname.removesuffix(".json")
        out[kind_key] = {"items": merged}
    return out


def _merge_layers_by_key(file_name: str, layers: list[list[dict]]) -> list[dict]:
    key_fn = _key_fn_for(file_name)
    merged: dict[tuple, dict] = {}
    for layer in layers:
        for item in layer:
            try:
                merged[key_fn(item)] = item
            except KeyError:
                logger.warning(
                    "pack_storage: item sans clé d'identité ignoré dans %s", file_name
                )
                continue
    return list(merged.values())


def _key_fn_for(file_name: str):
    if file_name in ("registry.json", "dictionary.json"):
        return lambda it: ("name", it["canonical_name"])
    if file_name == "rules.json":
        return lambda it: ("id", it["id"])
    if file_name == "knowledge_graph.json":
        def k(it):
            if "relation" in it:
                return ("edge", it["source_id"], it["target_id"], it["relation"])
            return ("node", it["id"])
        return k
    raise AssertionError(f"unhandled file {file_name!r}")


@dataclass
class JournalEntry:
    id: str
    ts: datetime
    owner_ref: str | None
    slug: str
    focus_symptoms: list[str]
    focus_refdes: list[str]
    delta_summary: dict
    scout_dump_range: dict
    status: str
    promoted_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_reason: str | None = None


def _journal_path(memory_root: Path, slug: str) -> Path:
    return memory_root / slug / "expansions" / "expansions.jsonl"


def append_journal(memory_root: Path, slug: str, entry: JournalEntry) -> None:
    path = _journal_path(memory_root, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _SlugLock(_slug_lock_path(memory_root, slug)):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), default=_json_default))
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())


def read_journal(memory_root: Path, slug: str) -> Iterator[JournalEntry]:
    path = _journal_path(memory_root, slug)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        raw = json.loads(line)
        for k in ("ts", "promoted_at", "revoked_at"):
            if raw.get(k):
                raw[k] = datetime.fromisoformat(raw[k])
        yield JournalEntry(**raw)


def _rewrite_journal_entry(memory_root: Path, slug: str, entry_id: str, **updates) -> None:
    path = _journal_path(memory_root, slug)
    if not path.is_file():
        raise ValueError(f"journal vide pour {slug}")
    new_lines: list[str] = []
    found = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        if raw["id"] == entry_id:
            raw.update({k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in updates.items()})
            found = True
        new_lines.append(json.dumps(raw))
    if not found:
        raise ValueError(f"expansion {entry_id!r} absente du journal")
    fd, tmp = tempfile.mkstemp(prefix=".expansions.jsonl.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if Path(tmp).exists():
            os.unlink(tmp)
        raise


def write_promoted(memory_root: Path, slug: str, *, expansion_id: str) -> None:
    """Déplace les facts d'une expansion vers promoted/. Idempotent."""
    with _SlugLock(_slug_lock_path(memory_root, slug)):
        journal = list(read_journal(memory_root, slug))
        entry = next((e for e in journal if e.id == expansion_id), None)
        if entry is None:
            raise ValueError(f"expansion {expansion_id!r} introuvable")
        if entry.status == "promoted":
            return
        if entry.status == "revoked":
            raise ValueError(f"expansion {expansion_id!r} est révoquée — utilise --force pour repromouvoir")
        owner_ref = entry.owner_ref
        if owner_ref is None:
            _rewrite_journal_entry(
                memory_root, slug, expansion_id,
                status="promoted", promoted_at=datetime.now(UTC),
            )
            return

        for fname in _PACK_FILES:
            staged_path = memory_root / slug / "_staged" / owner_ref / fname
            if not staged_path.is_file():
                continue
            staged_items = _read_items_or_empty(staged_path)
            from_this_expansion = [
                it for it in staged_items
                if (it.get("_provenance") or {}).get("expansion_id") == expansion_id
            ]
            if not from_this_expansion:
                continue
            promo_path = memory_root / slug / "promoted" / fname
            promo_items = _read_items_or_empty(promo_path)
            # Dedup : on n'ajoute pas un fact déjà présent dans promoted/
            # (par sa clé canonique). Source unique de vérité : _key_fn_for,
            # même fonction que _merge_layers_by_key (Fix 1 — suppression de
            # _canonical_key_of qui représentait les nœuds graphe différemment).
            key_fn = _key_fn_for(fname)
            existing_keys: set[tuple] = set()
            for it in promo_items:
                try:
                    existing_keys.add(key_fn(it))
                except KeyError:
                    pass  # item malformé — on ne peut pas le dédupliquer, on l'ignore dans l'index
            for it in from_this_expansion:
                if "_provenance" in it:
                    it["_provenance"]["status"] = "promoted"
                try:
                    k = key_fn(it)
                except KeyError:
                    # Item sans clé d'identité : on l'ajoute sans dedup (mieux
                    # qu'une perte silencieuse).
                    promo_items.append(it)
                    continue
                if k not in existing_keys:
                    promo_items.append(it)
                    existing_keys.add(k)
            _atomic_write_json(promo_path, {"items": promo_items})

        _rewrite_journal_entry(
            memory_root, slug, expansion_id,
            status="promoted", promoted_at=datetime.now(UTC),
        )


def revoke_expansion(memory_root: Path, slug: str, *, expansion_id: str, reason: str | None = None) -> None:
    """Retire les facts d'une expansion de staged ET promoted. Refuse baseline-pre-T8."""
    if expansion_id == "baseline-pre-T8":
        raise ValueError("baseline est non-revocable par design")
    with _SlugLock(_slug_lock_path(memory_root, slug)):
        journal = list(read_journal(memory_root, slug))
        entry = next((e for e in journal if e.id == expansion_id), None)
        if entry is None:
            raise ValueError(f"expansion {expansion_id!r} introuvable")
        if entry.status == "revoked":
            return
        owner_ref = entry.owner_ref

        def _remove_matching(path: Path):
            if not path.is_file():
                return
            items = _read_items_or_empty(path)
            kept = [
                it for it in items
                if (it.get("_provenance") or {}).get("expansion_id") != expansion_id
            ]
            _atomic_write_json(path, {"items": kept})

        for fname in _PACK_FILES:
            _remove_matching(memory_root / slug / "promoted" / fname)
            if owner_ref:
                _remove_matching(memory_root / slug / "_staged" / owner_ref / fname)

        _rewrite_journal_entry(
            memory_root, slug, expansion_id,
            status="revoked",
            revoked_at=datetime.now(UTC),
            revoked_reason=reason,
        )


def revoke_fact(memory_root: Path, slug: str, *, fact_id: str, reason: str | None = None) -> None:
    """Retire un seul fact identifié par son fact_id (cherché par hash de la clé canonique).
    L'expansion qui l'a créé conserve son statut (les autres facts restent)."""
    with _SlugLock(_slug_lock_path(memory_root, slug)):
        for fname in _PACK_FILES:
            for layer in ("promoted", "_staged"):
                base = memory_root / slug / layer
                if not base.is_dir():
                    continue
                candidates = [base / fname] if layer == "promoted" else list(base.glob(f"*/{fname}"))
                for path in candidates:
                    if not path.is_file():
                        continue
                    items = _read_items_or_empty(path)
                    kept = [it for it in items if _derive_fact_id(it) != fact_id]
                    if len(kept) != len(items):
                        _atomic_write_json(path, {"items": kept})


def _derive_fact_id(item: dict) -> str:
    """F-{kind}-{short_hash} basé sur la clé canonique de l'item.
    Centralisé ici ; expansion.py l'importe et le réutilise (pas de duplication).

    La discrimination cmp/sig utilise COMPONENT_KINDS (frozenset dérivé de
    schemas._ComponentKind via get_args) — plus de liste dupliquée ni de rot
    silencieux si un kind est ajouté au Literal dans schemas.py.
    """
    import hashlib
    if "canonical_name" in item:
        key = item["canonical_name"]
        kind = "cmp" if item.get("kind") in COMPONENT_KINDS else "sig"
    elif "id" in item and "relation" not in item:
        key = item["id"]
        kind = "rule" if item["id"].startswith("R-") else "node"
    elif "relation" in item:
        key = f"{item['source_id']}->{item['target_id']}:{item['relation']}"
        kind = "edge"
    else:
        return ""
    short = hashlib.sha256(key.encode()).hexdigest()[:8]
    return f"F-{kind}-{short}"
