# SPDX-License-Identifier: Apache-2.0
"""Seed a device's Managed-Agents memory store from its on-disk knowledge pack.

Called from the pipeline orchestrator right after an APPROVED verdict. The
diagnostic conversation for this device can then read the canonical knowledge
(registry, rules, dictionary, knowledge graph) natively via memory_search /
memory_read instead of re-hydrating it from disk on every tool call.

Feature-gated behind `settings.ma_memory_store_enabled` — off by default until
Anthropic grants the memory_stores Research Preview to our workspace. Every
error path degrades to a log warning: the pipeline must never fail because
memory seeding failed.
"""

from __future__ import annotations

import logging
from pathlib import Path

from anthropic import AsyncAnthropic

from api.agent.memory_stores import ensure_memory_store
from api.config import get_settings

logger = logging.getLogger("microsolder.agent.memory_seed")


# Files we push into the store and the memory path they land on. Path scheme
# `/knowledge/*` is reserved for pipeline-authored memories; `/field_reports/*`
# is for write-backs from diagnostic sessions (see record_field_report).
_SEED_FILES = (
    ("registry.json", "/knowledge/registry.json"),
    ("knowledge_graph.json", "/knowledge/knowledge_graph.json"),
    ("rules.json", "/knowledge/rules.json"),
    ("dictionary.json", "/knowledge/dictionary.json"),
)


async def seed_memory_store_from_pack(
    *,
    client: AsyncAnthropic,
    device_slug: str,
    pack_dir: Path,
) -> dict[str, str]:
    """Upsert the pack's JSON artefacts into the device's memory store.

    Returns a mapping `{memory_path: "seeded"|"skipped"|"error:<reason>"}` —
    useful for tests and telemetry. Never raises; missing files, missing
    store, or SDK failures all become log warnings plus a per-file status
    in the returned dict.
    """
    settings = get_settings()
    status: dict[str, str] = {path: "pending" for _, path in _SEED_FILES}

    if not settings.ma_memory_store_enabled:
        for path in status:
            status[path] = "skipped:flag_disabled"
        logger.debug(
            "[MemorySeed] ma_memory_store_enabled=False — no-op for slug=%s",
            device_slug,
        )
        return status

    store_id = await ensure_memory_store(client, device_slug)
    if store_id is None:
        for path in status:
            status[path] = "skipped:no_store"
        return status

    try:
        memories_api = client.beta.memory_stores.memories  # type: ignore[attr-defined]
    except AttributeError:
        logger.warning(
            "[MemorySeed] anthropic SDK has no beta.memory_stores.memories surface; "
            "cannot seed slug=%s",
            device_slug,
        )
        for path in status:
            status[path] = "skipped:no_sdk_surface"
        return status

    for file_name, memory_path in _SEED_FILES:
        on_disk = pack_dir / file_name
        if not on_disk.exists():
            status[memory_path] = "skipped:missing_file"
            logger.info(
                "[MemorySeed] Skip %s for slug=%s (no file on disk)",
                memory_path,
                device_slug,
            )
            continue
        try:
            content = on_disk.read_text(encoding="utf-8")
            await memories_api.create(
                memory_store_id=store_id,
                path=memory_path,
                content=content,
            )
        except Exception as exc:  # noqa: BLE001 — beta surface, want single catch
            logger.warning(
                "[MemorySeed] create failed for slug=%s path=%s: %s",
                device_slug,
                memory_path,
                exc,
            )
            status[memory_path] = f"error:{type(exc).__name__}"
            continue
        status[memory_path] = "seeded"
        logger.info(
            "[MemorySeed] Seeded slug=%s path=%s bytes=%d",
            device_slug,
            memory_path,
            len(content),
        )

    return status
