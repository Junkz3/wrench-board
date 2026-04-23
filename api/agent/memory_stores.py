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


async def _upsert_memory_via_http(
    *, api_key: str, store_id: str, path: str, content: str
) -> str | None:
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
    if resp.status_code != 200:
        logger.warning(
            "[MemoryStore] HTTP upsert returned %d for path=%s: %s",
            resp.status_code,
            path,
            resp.text[:300],
        )
        return None
    try:
        return resp.json().get("content_sha256") or "ok"
    except ValueError:
        return "ok"


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
