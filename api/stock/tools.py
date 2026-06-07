"""Agent tool implementations dispatched from api.agent.tool_dispatch.

Each function takes a dict payload and returns a dict (JSON-serializable).
See spec §8.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from api.agent.owner_ref import current_owner_ref
from api.config import get_settings
from api.stock.schemas import PartsIndex, StockSearchQuery
from api.stock.search import stock_search as _search_impl
from api.stock.store import (
    consume_part,
    load_inventory,
    mark_donor,
    unmark_donor,
)


def _memory_root() -> Path:
    return Path(get_settings().memory_root)


# Every agent stock tool scopes to the session's tenant (current_owner_ref):
# the cloud front-door set it from X-Owner-Ref, so a tenant's agent only ever
# touches that tenant's private inventory. None (standalone) → the global store.
def stock_search(payload: dict[str, Any]) -> dict[str, Any]:
    query = StockSearchQuery.model_validate(payload)
    res = _search_impl(query, current_owner_ref())
    return res.model_dump(mode="json")


def stock_consume(payload: dict[str, Any]) -> dict[str, Any]:
    donor_id = payload["donor_id"]
    refdes = payload["refdes"]
    notes = payload.get("notes")
    repair_id = payload.get("repair_id")
    ok = consume_part(donor_id=donor_id, refdes=refdes, repair_id=repair_id, notes=notes,
                      owner_ref=current_owner_ref())
    if not ok:
        return {"ok": False, "reason": f"donor_id {donor_id!r} not found in stock"}
    return {"ok": True, "donor_id": donor_id, "refdes": refdes}


def stock_mark_donor(payload: dict[str, Any]) -> dict[str, Any]:
    device_slug = payload["device_slug"]
    label = payload["label"]
    condition = payload.get("condition", "donor_only")

    try:
        donor_id = mark_donor(device_slug=device_slug, label=label, condition=condition,
                              owner_ref=current_owner_ref())
    except FileNotFoundError:
        return {"created": False, "reason": f"device_slug {device_slug!r} not found in memory/"}

    has_pi = (_memory_root() / device_slug / "parts_index.json").exists()
    out: dict[str, Any] = {"created": True, "donor_id": donor_id}
    if not has_pi:
        out["warning"] = (
            "no parts_index for this device — value-search will skip this donor "
            "until the schematic PDF is ingested"
        )
    return out


def stock_unmark_donor(payload: dict[str, Any]) -> dict[str, Any]:
    donor_id = payload["donor_id"]
    if not unmark_donor(donor_id, owner_ref=current_owner_ref()):
        return {"ok": False, "reason": f"donor_id {donor_id!r} not found"}
    return {"ok": True}


def stock_list_donors(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    inv = load_inventory(current_owner_ref())
    out = []
    memory_dir = _memory_root()
    for donor in inv.donors.values():
        pi_path = memory_dir / donor.device_slug / "parts_index.json"
        if pi_path.exists():
            idx = PartsIndex.model_validate_json(pi_path.read_text(encoding="utf-8"))
            parts_total = len(idx.entries)
            has_pi = True
        else:
            parts_total = 0
            has_pi = False
        out.append({
            "donor_id": donor.donor_id,
            "device_slug": donor.device_slug,
            "label": donor.label,
            "condition": donor.condition,
            "parts_total": parts_total,
            "parts_available": max(0, parts_total - len(donor.consumed)),
            "parts_consumed": len(donor.consumed),
            "has_parts_index": has_pi,
        })
    return {"donors": out}
