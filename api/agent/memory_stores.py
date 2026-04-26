# SPDX-License-Identifier: Apache-2.0
"""Per-device memory store cache for Managed Agents sessions.

Anthropic **memory stores** entered public beta on 2026-04-23
(`anthropic-beta: managed-agents-2026-04-01`). The first session for a
given device slug creates a store via the API and persists its id in
`memory/{slug}/managed.json`. Subsequent sessions reuse it so the agent
retains learnings across repairs without re-seeding.

Dual path — SDK first, raw HTTP fallback:
  - When `client.beta.memory_stores` is exposed by the SDK, we call it
    directly (typed, nicer errors).
  - Otherwise we POST/GET the REST endpoints. SDK 0.96.0 has not yet
    shipped the surface, so today every call takes the HTTP path; the
    code auto-promotes to the SDK path once it lands, no migration.

All failures (missing key, network, API rejection) degrade to a WARNING
log + `None`/empty return — the diagnostic session runs without memory
rather than crashing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
from anthropic import AsyncAnthropic

from api.config import get_settings

logger = logging.getLogger("microsolder.agent.memory_stores")

_BETA_HEADER = "managed-agents-2026-04-01"
_API_BASE = "https://api.anthropic.com/v1"


def _store_description(device_slug: str) -> str:
    return (
        f"Repair history and learned facts for device {device_slug}. "
        "Contains previous diagnostic sessions, confirmed component "
        "failures, and patterns observed across multiple repairs. "
        "Knowledge pack artefacts are pre-seeded under /knowledge/*; "
        "field reports from past repairs land under /field_reports/*."
    )


def _http_headers(api_key: str, *, content_json: bool = False) -> dict[str, str]:
    hdrs = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": _BETA_HEADER,
    }
    if content_json:
        hdrs["content-type"] = "application/json"
    return hdrs


async def _create_store_via_http(
    *, api_key: str, name: str, description: str
) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.post(
                f"{_API_BASE}/memory_stores",
                headers=_http_headers(api_key, content_json=True),
                json={"name": name, "description": description},
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[MemoryStore] HTTP create raised: %s", exc)
        return None
    if resp.status_code != 200:
        logger.warning(
            "[MemoryStore] HTTP create returned %d: %s",
            resp.status_code,
            resp.text[:300],
        )
        return None
    try:
        return resp.json().get("id")
    except ValueError:
        return None


async def _update_memory_via_http(
    *, api_key: str, store_id: str, memory_id: str, content: str
) -> str | None:
    """POST an update to an existing memory by id. Returns content_sha256 on success.

    Note: the public Managed Agents API docs list this as PATCH, but the
    live endpoint only accepts POST (verified 2026-04-26 — PATCH/PUT both
    return 405 Method Not Allowed). The shape stays
    `{"content": "..."}` either way.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.post(
                f"{_API_BASE}/memory_stores/{store_id}/memories/{memory_id}",
                headers=_http_headers(api_key, content_json=True),
                json={"content": content},
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[MemoryStore] HTTP update raised for memory_id=%s: %s",
            memory_id,
            exc,
        )
        return None
    if resp.status_code != 200:
        logger.warning(
            "[MemoryStore] HTTP update returned %d for memory_id=%s: %s",
            resp.status_code,
            memory_id,
            resp.text[:300],
        )
        return None
    try:
        return resp.json().get("content_sha256") or "ok"
    except ValueError:
        return "ok"


async def _upsert_memory_via_http(
    *, api_key: str, store_id: str, path: str, content: str
) -> str | None:
    """True upsert: try create, on 409 path conflict fall back to update.

    The Anthropic Memory API addresses memories by `mem_...` id for
    mutations and returns `409 memory_path_conflict_error` when a create
    is attempted at a path that already has a memory. This helper extracts
    the `conflicting_memory_id` from the error body and PATCHes that
    memory instead, giving callers true upsert semantics.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.post(
                f"{_API_BASE}/memory_stores/{store_id}/memories",
                headers=_http_headers(api_key, content_json=True),
                json={"path": path, "content": content},
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[MemoryStore] HTTP upsert raised for path=%s: %s", path, exc
        )
        return None
    if resp.status_code == 200:
        try:
            return resp.json().get("content_sha256") or "ok"
        except ValueError:
            return "ok"

    # 409 path conflict → switch to PATCH on the conflicting memory id.
    if resp.status_code == 409:
        try:
            body = resp.json()
            err = body.get("error", {}) if isinstance(body, dict) else {}
            if err.get("type") == "memory_path_conflict_error":
                memory_id = err.get("conflicting_memory_id")
                if memory_id:
                    return await _update_memory_via_http(
                        api_key=api_key,
                        store_id=store_id,
                        memory_id=memory_id,
                        content=content,
                    )
        except (ValueError, KeyError):
            pass

    logger.warning(
        "[MemoryStore] HTTP upsert returned %d for path=%s: %s",
        resp.status_code,
        path,
        resp.text[:300],
    )
    return None


async def ensure_memory_store(
    client: AsyncAnthropic, device_slug: str
) -> str | None:
    """Return the `memstore_...` id for this device, creating one on first call.

    The id is cached in `memory/{slug}/managed.json` so subsequent calls on the
    same device reuse it (no network round-trip, no duplicate stores).
    """
    settings = get_settings()
    pack_dir = Path(settings.memory_root) / device_slug
    pack_dir.mkdir(parents=True, exist_ok=True)
    meta_path = pack_dir / "managed.json"

    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            meta = {}
        store_id = meta.get("memory_store_id")
        if store_id:
            return store_id

    name = f"microsolder-{device_slug}"
    description = _store_description(device_slug)
    store_id: str | None = None

    sdk_beta = getattr(client, "beta", None)
    sdk_surface = getattr(sdk_beta, "memory_stores", None) if sdk_beta else None
    if sdk_surface is not None:
        try:
            store = await sdk_surface.create(name=name, description=description)
            store_id = getattr(store, "id", None)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[MemoryStore] SDK create failed for device=%s: %s — "
                "falling back to HTTP",
                device_slug,
                exc,
            )
            store_id = None

    if store_id is None:
        if not settings.anthropic_api_key:
            logger.warning(
                "[MemoryStore] no API key; running device=%s without memory",
                device_slug,
            )
            return None
        store_id = await _create_store_via_http(
            api_key=settings.anthropic_api_key,
            name=name,
            description=description,
        )

    if not store_id:
        return None

    meta_path.write_text(
        json.dumps(
            {"memory_store_id": store_id, "device_slug": device_slug},
            indent=2,
        )
        + "\n"
    )
    logger.info("[MemoryStore] Created id=%s for device=%s", store_id, device_slug)
    return store_id


GLOBAL_REGISTRY_DIR = "_managed"
GLOBAL_REGISTRY_FILE = "global.json"

# Allowed kinds for the global singleton registry. Each maps to a single
# store created at most once per workspace; the id is cached locally so
# subsequent sessions reuse it. See
# docs/superpowers/plans/2026-04-26-ma-memory-layered-architecture.md
# for the layered MA memory architecture (4 stores per session).
_GLOBAL_KINDS = {"patterns", "playbooks"}


def _global_registry_path() -> Path:
    settings = get_settings()
    root = Path(settings.memory_root) / GLOBAL_REGISTRY_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root / GLOBAL_REGISTRY_FILE


def _read_global_registry() -> dict:
    path = _global_registry_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        logger.warning("[MemoryStore] global registry at %s unreadable", path)
        return {}


def _write_global_registry(data: dict) -> None:
    path = _global_registry_path()
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


async def ensure_global_store(
    client: AsyncAnthropic,
    *,
    kind: str,
    description: str,
) -> str | None:
    """Return the singleton memstore id for `kind` ∈ {patterns, playbooks}.

    Created on first call, cached in `memory/_managed/global.json` for
    re-use across all sessions and devices. The store hosts cross-device
    knowledge (failure taxonomy, diagnostic playbook templates) attached
    read-only to every diagnostic session.
    """
    if kind not in _GLOBAL_KINDS:
        raise ValueError(f"Unknown global store kind: {kind!r}")

    registry = _read_global_registry()
    cached = registry.get(kind, {})
    cached_id = cached.get("memory_store_id")
    if cached_id:
        return cached_id

    name = f"microsolder-global-{kind}"
    store_id: str | None = None

    sdk_beta = getattr(client, "beta", None)
    sdk_surface = (
        getattr(sdk_beta, "memory_stores", None) if sdk_beta else None
    )
    if sdk_surface is not None:
        try:
            store = await sdk_surface.create(name=name, description=description)
            store_id = getattr(store, "id", None)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[MemoryStore] SDK create failed for global %s: %s — "
                "falling back to HTTP",
                kind,
                exc,
            )

    if store_id is None:
        settings = get_settings()
        if not settings.anthropic_api_key:
            logger.warning(
                "[MemoryStore] no API key; running without global %s store",
                kind,
            )
            return None
        store_id = await _create_store_via_http(
            api_key=settings.anthropic_api_key,
            name=name,
            description=description,
        )

    if not store_id:
        return None

    registry[kind] = {
        "memory_store_id": store_id,
        "name": name,
        "description": description,
    }
    _write_global_registry(registry)
    logger.info(
        "[MemoryStore] Created global %s store id=%s", kind, store_id
    )
    return store_id


def _repair_marker_path(device_slug: str, repair_id: str) -> Path:
    settings = get_settings()
    return (
        Path(settings.memory_root)
        / device_slug
        / "repairs"
        / repair_id
        / "managed.json"
    )


def _repair_store_description(device_slug: str, repair_id: str) -> str:
    return (
        f"Scratch notebook for repair {repair_id} on device {device_slug}. "
        "Read-write scribe layer for the agent's own working notes across "
        "sessions of THIS specific repair: state.md (latest snapshot), "
        "decisions/{ts}.md (validated/refuted hypotheses), "
        "measurements/{rail}.md (time series of probed values), "
        "open_questions.md (unresolved threads to revisit)."
    )


async def ensure_repair_store(
    client: AsyncAnthropic,
    *,
    device_slug: str,
    repair_id: str,
) -> str | None:
    """Return the per-repair RW memstore id, creating one on first session.

    Persisted at `memory/{slug}/repairs/{repair_id}/managed.json`. Backbone
    of the "agent-as-its-own-librarian" pattern that replaces the LLM-driven
    session resume summary.
    """
    marker = _repair_marker_path(device_slug, repair_id)
    marker.parent.mkdir(parents=True, exist_ok=True)

    if marker.exists():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            existing = data.get("memory_store_id")
            if existing:
                return existing
        except (json.JSONDecodeError, OSError):
            logger.warning(
                "[MemoryStore] repair marker %s unreadable", marker
            )

    name = f"microsolder-repair-{device_slug}-{repair_id}"
    description = _repair_store_description(device_slug, repair_id)
    store_id: str | None = None

    sdk_beta = getattr(client, "beta", None)
    sdk_surface = (
        getattr(sdk_beta, "memory_stores", None) if sdk_beta else None
    )
    if sdk_surface is not None:
        try:
            store = await sdk_surface.create(name=name, description=description)
            store_id = getattr(store, "id", None)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[MemoryStore] SDK create failed for repair=%s: %s — "
                "falling back to HTTP",
                repair_id,
                exc,
            )

    if store_id is None:
        settings = get_settings()
        if not settings.anthropic_api_key:
            logger.warning(
                "[MemoryStore] no API key; running repair=%s/%s without scribe store",
                device_slug,
                repair_id,
            )
            return None
        store_id = await _create_store_via_http(
            api_key=settings.anthropic_api_key,
            name=name,
            description=description,
        )

    if not store_id:
        return None

    marker.write_text(
        json.dumps(
            {
                "memory_store_id": store_id,
                "device_slug": device_slug,
                "repair_id": repair_id,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    logger.info(
        "[MemoryStore] Created repair store id=%s for %s/%s",
        store_id,
        device_slug,
        repair_id,
    )
    return store_id


async def upsert_memory(
    client: AsyncAnthropic,
    *,
    store_id: str,
    path: str,
    content: str,
) -> str | None:
    """Upsert a memory by path into `store_id`, returning its `content_sha256`.

    `path` is the logical address (e.g. `/knowledge/rules.json`) — the
    server creates on first write and replaces content thereafter. See
    `docs/en/managed-agents/memory` for the contract. Returns `None` on
    failure so callers can track per-memory status without raising.
    """
    sdk_beta = getattr(client, "beta", None)
    sdk_stores = getattr(sdk_beta, "memory_stores", None) if sdk_beta else None
    sdk_surface = getattr(sdk_stores, "memories", None) if sdk_stores else None
    if sdk_surface is not None:
        # Prefer `write` (public beta name). `create` kept as a courtesy
        # for older SDK builds that may still expose the research-preview
        # spelling.
        call = getattr(sdk_surface, "write", None) or getattr(
            sdk_surface, "create", None
        )
        if call is not None:
            try:
                result = await call(
                    memory_store_id=store_id,
                    path=path,
                    content=content,
                )
                return getattr(result, "content_sha256", None) or "ok"
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[MemoryStore] SDK upsert failed for store=%s path=%s: %s — "
                    "falling back to HTTP",
                    store_id,
                    path,
                    exc,
                )

    settings = get_settings()
    if not settings.anthropic_api_key:
        return None
    return await _upsert_memory_via_http(
        api_key=settings.anthropic_api_key,
        store_id=store_id,
        path=path,
        content=content,
    )
