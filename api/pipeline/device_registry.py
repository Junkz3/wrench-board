"""Device alias registry — the shared "carnet" (T9a).

Maps every way of naming a board (board# / Apple model / EMC / codename /
marketing) onto ONE canonical identity, plus ``family`` links between sibling
boards (cousins). Storage is pluggable behind :class:`DeviceRegistryStore`:

* :class:`JsonDeviceRegistryStore` — self-host, a local ``memory/_devices/
  registry.json``; each deployment grows its own carnet.
* :class:`CloudDeviceRegistryStore` — managed mode, reads/writes the cloud's
  Postgres via ``/internal/device-registry/*`` (the moat stays the operator's).

Both honor the same contract; identities use the cloud's camelCase wire shape
``{id, canonicalKey, family, facets, provenance, status, mergedInto}`` so the two
adapters are interchangeable. ``facets`` is derived from the aliases (the source
of truth). Strong kinds (board, emc) are board-unique — the anti-poison
invariant; violating it raises :class:`DeviceRegistryConflict`.
"""
from __future__ import annotations

import abc
import contextlib
import fcntl
import json
import os
import tempfile
import uuid
from pathlib import Path

import httpx

from api.config import get_settings
from api.pipeline.device_identity import (
    STRONG_KINDS,
    extract_facets,
    normalize_token,
    slugify_label,
)

_DEVICES_DIR = "_devices"
_REGISTRY_FILE = "registry.json"


class DeviceRegistryConflict(Exception):
    """A strong-alias (board/emc) anti-poison invariant was violated — the caller
    must confirm rather than silently fuse two distinct boards (cloud → 409)."""


def _facets_from_aliases(aliases: list[dict]) -> dict:
    facets: dict[str, list[str]] = {}
    for a in aliases:
        facets.setdefault(a["kind"], [])
        if a["value"] not in facets[a["kind"]]:
            facets[a["kind"]].append(a["value"])
    return facets


class DeviceRegistryStore(abc.ABC):
    """Port: the carnet's storage contract (async; identities are camelCase dicts)."""

    @abc.abstractmethod
    async def lookup(self, tokens: list[str]) -> list[dict]: ...

    @abc.abstractmethod
    async def get_by_canonical_key(self, canonical_key: str) -> dict | None: ...

    @abc.abstractmethod
    async def upsert(self, *, canonical_key: str, family: str | None = None,
                     provenance: dict | None = None, aliases: list[dict]) -> dict: ...

    @abc.abstractmethod
    async def merge(self, *, source_key: str, target_key: str,
                    reason: str | None = None, by: str | None = None) -> dict: ...

    @abc.abstractmethod
    async def revoke(self, *, canonical_key: str | None = None, alias: str | None = None,
                     by: str | None = None, reason: str | None = None) -> dict | None: ...

    @abc.abstractmethod
    async def list_by_family(self, family: str) -> list[dict]: ...

    @abc.abstractmethod
    async def list(self, *, family: str | None = None, affected_by: str | None = None) -> list[dict]: ...


class JsonDeviceRegistryStore(DeviceRegistryStore):
    """Local-file carnet (self-host). Single ``_devices/registry.json``; mutating
    methods take a cross-process flock + write atomically (os.replace)."""

    def __init__(self, memory_root: Path | str):
        self._dir = Path(memory_root) / _DEVICES_DIR
        self._path = self._dir / _REGISTRY_FILE

    # --- file plumbing -----------------------------------------------------
    def _read(self) -> list[dict]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return data.get("identities", []) if isinstance(data, dict) else []

    def _write(self, identities: list[dict]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"identities": identities}, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)

    @contextlib.contextmanager
    def _locked(self):
        self._dir.mkdir(parents=True, exist_ok=True)
        lock = self._dir / ".lock"
        with open(lock, "w", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # --- projection --------------------------------------------------------
    @staticmethod
    def _project(rec: dict) -> dict:
        return {
            "id": rec["id"],
            "canonicalKey": rec["canonicalKey"],
            "family": rec.get("family") or None,
            "facets": _facets_from_aliases(rec.get("aliases", [])),
            "provenance": rec.get("provenance") or None,
            "status": rec.get("status", "active"),
            "mergedInto": rec.get("mergedInto") or None,
        }

    @staticmethod
    def _active(identities: list[dict]) -> list[dict]:
        return [r for r in identities if r.get("status", "active") == "active"]

    @staticmethod
    def _strong_owner(identities: list[dict], norm: str, except_key: str) -> dict | None:
        for r in JsonDeviceRegistryStore._active(identities):
            if r["canonicalKey"] == except_key:
                continue
            if any(a["norm"] == norm and a["kind"] in STRONG_KINDS for a in r.get("aliases", [])):
                return r
        return None

    # --- contract ----------------------------------------------------------
    async def lookup(self, tokens: list[str]) -> list[dict]:
        norms = {normalize_token(t) for t in (tokens or [])}
        norms.discard("")
        if not norms:
            return []
        out = []
        for r in self._active(self._read()):
            if any(a["norm"] in norms for a in r.get("aliases", [])):
                out.append(self._project(r))
        return out

    async def get_by_canonical_key(self, canonical_key: str) -> dict | None:
        for r in self._read():
            if r["canonicalKey"] == canonical_key:
                return self._project(r)
        return None

    async def upsert(self, *, canonical_key, family=None, provenance=None, aliases) -> dict:
        if not canonical_key:
            raise ValueError("canonical_key is required.")
        incoming = [
            {"value": a["value"], "kind": a["kind"], "norm": normalize_token(a["value"])}
            for a in (aliases or [])
        ]
        with self._locked():
            identities = self._read()
            for a in incoming:
                if a["kind"] not in STRONG_KINDS:
                    continue
                owner = self._strong_owner(identities, a["norm"], canonical_key)
                if owner:
                    raise DeviceRegistryConflict(
                        f"Strong alias {a['kind']}:{a['value']} already owned by "
                        f"'{owner['canonicalKey']}'."
                    )
            rec = next((r for r in identities if r["canonicalKey"] == canonical_key), None)
            if rec is None:
                rec = {
                    "id": uuid.uuid4().hex,
                    "canonicalKey": canonical_key,
                    "family": family or None,
                    "provenance": provenance or None,
                    "status": "active",
                    "mergedInto": None,
                    "aliases": [],
                }
                identities.append(rec)
            else:
                rec["status"] = "active"
                rec["mergedInto"] = None
                if family:
                    rec["family"] = family
                if provenance is not None:
                    rec["provenance"] = provenance
            have = {(a["norm"], a["kind"]) for a in rec["aliases"]}
            for a in incoming:
                if (a["norm"], a["kind"]) not in have:
                    rec["aliases"].append(a)
            self._write(identities)
            return self._project(rec)

    async def merge(self, *, source_key, target_key, reason=None, by=None) -> dict:
        if not source_key or not target_key:
            raise ValueError("source_key and target_key are required.")
        if source_key == target_key:
            raise ValueError("Cannot merge an identity into itself.")
        with self._locked():
            identities = self._read()
            source = next((r for r in identities if r["canonicalKey"] == source_key), None)
            target = next((r for r in identities if r["canonicalKey"] == target_key), None)
            if source is None:
                raise KeyError(f"Unknown device identity '{source_key}'.")
            if target is None:
                raise KeyError(f"Unknown device identity '{target_key}'.")
            for a in source["aliases"]:
                if a["kind"] not in STRONG_KINDS:
                    continue
                if any(b["kind"] == a["kind"] and b["norm"] != a["norm"] for b in target["aliases"]):
                    raise DeviceRegistryConflict(
                        f"Refusing to merge: conflicting strong {a['kind']} ids "
                        f"between '{source_key}' and '{target_key}'."
                    )
            have = {(b["norm"], b["kind"]) for b in target["aliases"]}
            for a in source["aliases"]:
                if (a["norm"], a["kind"]) not in have:
                    target["aliases"].append(a)
            if not target.get("family") and source.get("family"):
                target["family"] = source["family"]
            source["status"] = "merged"
            source["mergedInto"] = target_key
            source["aliases"] = []
            source["provenance"] = {**(source.get("provenance") or {}),
                                    "mergedBy": by, "mergeReason": reason}
            self._write(identities)
            return self._project(target)

    async def revoke(self, *, canonical_key=None, alias=None, by=None, reason=None) -> dict | None:
        with self._locked():
            identities = self._read()
            if canonical_key:
                rec = next((r for r in identities if r["canonicalKey"] == canonical_key), None)
                if rec is None:
                    raise KeyError(f"Unknown device identity '{canonical_key}'.")
                rec["status"] = "revoked"
                rec["aliases"] = []
                rec["provenance"] = {**(rec.get("provenance") or {}),
                                     "revokedBy": by, "revokeReason": reason}
                self._write(identities)
                return self._project(rec)
            if alias:
                norm = normalize_token(alias)
                for rec in self._active(identities):
                    before = len(rec["aliases"])
                    rec["aliases"] = [a for a in rec["aliases"] if a["norm"] != norm]
                    if len(rec["aliases"]) != before:
                        self._write(identities)
                        return self._project(rec)
                return None
            raise ValueError("revoke requires canonical_key or alias.")

    async def list_by_family(self, family: str) -> list[dict]:
        if not family:
            return []
        return [self._project(r) for r in self._active(self._read()) if r.get("family") == family]

    async def list(self, *, family=None, affected_by=None) -> list[dict]:
        out = []
        for r in self._active(self._read()):
            if family and r.get("family") != family:
                continue
            if affected_by and (r.get("provenance") or {}).get("addedBy") != affected_by:
                continue
            out.append(self._project(r))
        return out


_REGISTRY_PATH = "/internal/device-registry"
_HTTP_TIMEOUT = 10.0


class CloudDeviceRegistryStore(DeviceRegistryStore):
    """Managed-mode carnet: the cloud's Postgres is the source of truth, reached
    over ``/internal/device-registry/*`` with the shared service token. A 409
    (strong-alias conflict) maps to :class:`DeviceRegistryConflict`; other
    non-2xx raise so the caller (resolve) can degrade gracefully."""

    def __init__(self, base_url: str, token: str, *, timeout: float = _HTTP_TIMEOUT):
        self._base = base_url.rstrip("/") + _REGISTRY_PATH
        self._token = token
        self._timeout = timeout

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    async def _post(self, path: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            resp = await http.post(self._base + path, headers=self._headers, json=payload)
        if resp.status_code == 409:
            raise DeviceRegistryConflict(f"cloud registry conflict: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise RuntimeError(f"device-registry {path} → {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            resp = await http.get(self._base + path, headers=self._headers, params=params)
        if resp.status_code >= 400:
            raise RuntimeError(f"device-registry {path} → {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    async def lookup(self, tokens: list[str]) -> list[dict]:
        body = await self._post("/lookup", {"tokens": list(tokens or [])})
        return body.get("candidates", [])

    async def get_by_canonical_key(self, canonical_key: str) -> dict | None:
        # No dedicated GET-one route; lookup by the key as a token and match exactly.
        for c in await self.lookup([canonical_key]):
            if c.get("canonicalKey") == canonical_key:
                return c
        return None

    async def upsert(self, *, canonical_key, family=None, provenance=None, aliases) -> dict:
        payload = {"canonicalKey": canonical_key, "aliases": list(aliases or [])}
        if family is not None:
            payload["family"] = family
        if provenance is not None:
            payload["provenance"] = provenance
        body = await self._post("/identities", payload)
        return body.get("identity")

    async def merge(self, *, source_key, target_key, reason=None, by=None) -> dict:
        body = await self._post("/merge", {
            "sourceKey": source_key, "targetKey": target_key, "by": by, "reason": reason,
        })
        return body.get("identity")

    async def revoke(self, *, canonical_key=None, alias=None, by=None, reason=None) -> dict | None:
        body = await self._post("/revoke", {
            "canonicalKey": canonical_key, "alias": alias, "by": by, "reason": reason,
        })
        return body.get("identity")

    async def list_by_family(self, family: str) -> list[dict]:
        body = await self._get(f"/family/{family}")
        return body.get("cousins", [])

    async def list(self, *, family=None, affected_by=None) -> list[dict]:
        params = {}
        if family:
            params["family"] = family
        if affected_by:
            params["affectedBy"] = affected_by
        body = await self._get("/identities", params or None)
        return body.get("identities", [])


async def resolve_device(
    text: str,
    store: DeviceRegistryStore,
    *,
    device_slug: str | None = None,
    owner_ref: str | None = None,
) -> dict:
    """Resolve free device text to a canonical identity.

    Extract structured facets → look them up → adopt the owning fiche (a strong
    id, board/emc, is decisive; a single soft match is adopted too) or create a
    new one (canonical = board# if present, else ``device_slug`` or the slug of
    the text). A broad term that fans out to several cousins is **ambiguous** —
    never silently merged; it degrades to a fresh input-derived slug (today's
    behavior) and is registered as another cousin for the UI to disambiguate.

    Returns ``{canonical_slug, identity, candidates, created, ambiguous}``.
    Best-effort: a strong-alias conflict on register degrades instead of raising.
    """
    facets = extract_facets(text)
    tokens = [f["value"] for f in facets]
    candidates = await store.lookup(tokens) if tokens else []

    strong_norms = {normalize_token(f["value"]) for f in facets if f["kind"] in STRONG_KINDS}
    chosen = None
    if strong_norms:
        for c in candidates:
            owned = {
                normalize_token(v)
                for k in STRONG_KINDS
                for v in (c.get("facets", {}).get(k, []))
            }
            if strong_norms & owned:
                chosen = c
                break
    ambiguous = False
    if chosen is None:
        if len(candidates) == 1:
            chosen = candidates[0]
        elif len(candidates) > 1:
            ambiguous = True  # soft fan-out (cousins) — do not merge

    if chosen is not None:
        canonical = chosen["canonicalKey"]
    else:
        board = next((f["value"] for f in facets if f["kind"] == "board"), None)
        canonical = board or device_slug or slugify_label(text)

    identity = chosen
    # Don't persist a GUESS for an ambiguous term — we don't know which board the
    # tech means; the caller disambiguates and re-resolves with an explicit slug.
    if not ambiguous:
        try:
            identity = await store.upsert(
                canonical_key=canonical,
                aliases=facets,
                provenance={"source": "resolve", "addedBy": owner_ref},
            )
        except DeviceRegistryConflict:
            # Two distinct strong ids collided — keep the resolved candidate (or
            # look it up) rather than poisoning. The cloud/operator reconciles later.
            if identity is None:
                with contextlib.suppress(Exception):
                    identity = await store.get_by_canonical_key(canonical)
        except Exception:  # noqa: BLE001 - registry must never break a repair
            pass

    return {
        "canonical_slug": canonical,
        "identity": identity,
        "candidates": candidates,
        "created": chosen is None and not ambiguous,
        "ambiguous": ambiguous,
    }


def _registry_facets(registry: dict) -> tuple[list[dict], str | None]:
    """Map a built Registry (device_label + DeviceTaxonomy) to alias facets +
    a family key. Pulls structured ids (board/model/EMC) from the combined text
    and adds clean marketing strings."""
    label = (registry.get("device_label") or "").strip()
    tax = registry.get("taxonomy") or {}
    brand = (tax.get("brand") or "").strip()
    model = (tax.get("model") or "").strip()
    version = (tax.get("version") or "").strip()

    blob = " ".join(x for x in [label, brand, model, version] if x)
    facets = [f for f in extract_facets(blob) if f["kind"] != "marketing"]
    seen = set()
    for mk in (label, f"{brand} {model}".strip() if brand and model else "",
               f"{brand} {model} {version}".strip() if brand and model and version else ""):
        if mk and mk not in seen:
            seen.add(mk)
            facets.append({"value": mk, "kind": "marketing"})
    family = slugify_label(f"{brand} {model}") if brand and model else None
    return facets, family


async def register_from_registry(
    store: DeviceRegistryStore,
    canonical_slug: str,
    registry: dict,
    *,
    owner_ref: str | None = None,
) -> dict | None:
    """Enrich a pack's fiche with the facets Scout/Registry discovered (board#,
    Apple model, EMC, marketing, family). This is the cross-facet bridge: after
    this, an input by any discovered id resolves to the same pack. Best-effort —
    a strong-id conflict degrades (never breaks the pipeline)."""
    facets, family = _registry_facets(registry)
    if not facets:
        return None
    try:
        return await store.upsert(
            canonical_key=canonical_slug,
            family=family,
            aliases=facets,
            provenance={"source": "registry", "addedBy": owner_ref},
        )
    except DeviceRegistryConflict:
        with contextlib.suppress(Exception):
            return await store.get_by_canonical_key(canonical_slug)
        return None
    except Exception:  # noqa: BLE001 - enrichment must never break a build
        return None


def _pack_data_flags(pack_dir: Path) -> tuple[bool, bool]:
    """(has_data, has_graph) for a pack on disk. A built electrical graph is the
    most useful fallback; a registry (T8 baseline or legacy root) still carries
    knowledge worth borrowing."""
    if (pack_dir / "electrical_graph.json").is_file():
        return True, True
    for reg in (pack_dir / "baseline" / "registry.json", pack_dir / "registry.json"):
        if reg.is_file():
            return True, False
    return False, False


async def find_cousin_packs(
    store: DeviceRegistryStore,
    memory_root: Path | str,
    slug: str,
) -> list[dict]:
    """Sibling packs (same family, NOT the same board) that carry usable data —
    the agent's fallback when no exact graph exists for ``slug``. Returns
    ``[{slug, family, has_graph, facets}]``. Excludes self and dataless cousins;
    the caller decides when to surface them (typically only when ``slug`` itself
    has no graph). The boards stay DISTINCT — this is a suggestion, never a merge."""
    fiche = await store.get_by_canonical_key(slug)
    if not fiche or not fiche.get("family"):
        return []
    root = Path(memory_root)
    out = []
    for c in await store.list_by_family(fiche["family"]):
        if c["canonicalKey"] == slug:
            continue
        has_data, has_graph = _pack_data_flags(root / c["canonicalKey"])
        if has_data:
            out.append({
                "slug": c["canonicalKey"],
                "family": c["family"],
                "has_graph": has_graph,
                "facets": c.get("facets", {}),
            })
    return out


def get_device_registry_store(memory_root: Path | str) -> DeviceRegistryStore:
    """Pick the carnet backend: the cloud's Postgres when both
    ``cloud_device_registry_url`` + token are set (managed mode), else the local
    JSON store (self-host). Mirrors :func:`cloud_metering.cloud_metering_enabled`."""
    settings = get_settings()
    url = getattr(settings, "cloud_device_registry_url", "")
    token = getattr(settings, "cloud_device_registry_token", "")
    if url and token:
        return CloudDeviceRegistryStore(url, token)
    return JsonDeviceRegistryStore(memory_root)
