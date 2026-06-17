"""Cousin-fallback hint for the diagnostic agent (T9a Phase B).

When the current board has no schematic of its own, the agent can still lean on a
sibling pack (same device family, a nearby board revision) as an INDICATIVE
reference — similar power rails and topology. This builds the one-line hint
injected into the system prompt; the caller gates it on "no graph for this board".

Best-effort: any registry/filesystem hiccup returns None — a missing hint must
never disturb a session. The sibling boards stay DISTINCT (suggestion, never a
merge): the line tells the agent to cross-check, not to trust blindly.
"""
from __future__ import annotations

import logging
from pathlib import Path

from api.config import get_settings
from api.pipeline.device_registry import find_cousin_packs, get_device_registry_store

logger = logging.getLogger("wrench_board.agent.cousin_hint")

# How many siblings to name (graph-bearing first); keep the line short.
_MAX_COUSINS = 3


async def build_cousin_line(device_slug: str) -> str | None:
    """One-line sibling-fallback hint for ``device_slug``, or None when there is
    no same-family cousin pack with usable data."""
    try:
        memory_root = Path(get_settings().memory_root)
        store = get_device_registry_store(memory_root)
        cousins = await find_cousin_packs(store, memory_root, device_slug)
    except Exception:  # noqa: BLE001 - best-effort UX, never disturb the session
        logger.warning("[CousinHint] lookup failed for %r", device_slug, exc_info=True)
        return None
    if not cousins:
        return None

    # Graph-bearing siblings are the most useful — list them first.
    cousins.sort(key=lambda c: not c.get("has_graph"))
    named = []
    for c in cousins[:_MAX_COUSINS]:
        kind = "schematic graph" if c.get("has_graph") else "knowledge pack"
        named.append(f"{c['slug']} ({kind})")
    family = cousins[0].get("family")
    return (
        f"No schematic is loaded for this exact board. Sibling boards in the same "
        f"family ({family}) DO have data you can lean on as an INDICATIVE reference: "
        f"{', '.join(named)}. Their rails/topology are often similar, but the boards "
        f"differ — cross-check every refdes/pin against this board, never assume."
    )
