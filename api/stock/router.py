"""HTTP endpoints for the stock UI.

See docs/superpowers/specs/2026-05-08-stock-inventory-design.md §10.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.config import get_settings
from api.stock.schemas import (
    PartsIndex,
    StockSearchQuery,
    StockSearchResult,
)
from api.stock.search import stock_search
from api.stock.store import (
    consume_part,
    load_inventory,
    mark_donor,
    unconsume_part,
    unmark_donor,
)

router = APIRouter(prefix="/api/stock", tags=["stock"])


class _MarkDonorBody(BaseModel):
    device_slug: str
    label: str
    condition: str = "donor_only"


class _ConsumeBody(BaseModel):
    refdes: str
    repair_id: str | None = None
    notes: str | None = None


def _memory_root() -> Path:
    return Path(get_settings().memory_root)


def _load_parts_index_or_none(slug: str) -> PartsIndex | None:
    p = _memory_root() / slug / "parts_index.json"
    if not p.exists():
        return None
    return PartsIndex.model_validate_json(p.read_text(encoding="utf-8"))


@router.get("/donors")
def list_donors():
    inv = load_inventory()
    out = []
    for donor in inv.donors.values():
        idx = _load_parts_index_or_none(donor.device_slug)
        parts_total = len(idx.entries) if idx else 0
        parts_consumed = len(donor.consumed)
        out.append({
            "donor_id": donor.donor_id,
            "device_slug": donor.device_slug,
            "label": donor.label,
            "condition": donor.condition,
            "added_at": donor.added_at.isoformat(),
            "parts_total": parts_total,
            "parts_available": max(0, parts_total - parts_consumed),
            "parts_consumed": parts_consumed,
            "has_parts_index": idx is not None,
        })
    return {"donors": out}


@router.post("/donors")
def create_donor(body: _MarkDonorBody):
    try:
        donor_id = mark_donor(
            device_slug=body.device_slug,
            label=body.label,
            condition=body.condition,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    has_pi = _load_parts_index_or_none(body.device_slug) is not None
    return {"donor_id": donor_id, "has_parts_index": has_pi}


@router.delete("/donors/{donor_id}")
def delete_donor(donor_id: str):
    if not unmark_donor(donor_id):
        raise HTTPException(status_code=404, detail="donor_id not found")
    return {"ok": True}


@router.post("/donors/{donor_id}/consume")
def consume_endpoint(donor_id: str, body: _ConsumeBody):
    if not consume_part(donor_id=donor_id, refdes=body.refdes,
                        repair_id=body.repair_id, notes=body.notes):
        raise HTTPException(status_code=404, detail="donor_id not found")
    return {"ok": True}


@router.delete("/donors/{donor_id}/consume/{refdes}")
def unconsume_endpoint(donor_id: str, refdes: str):
    if not unconsume_part(donor_id=donor_id, refdes=refdes):
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}


@router.post("/search", response_model=StockSearchResult)
def search_endpoint(query: StockSearchQuery) -> StockSearchResult:
    return stock_search(query)


@router.get("/donors/{donor_id}/parts")
def list_donor_parts(donor_id: str):
    inv = load_inventory()
    if donor_id not in inv.donors:
        raise HTTPException(status_code=404, detail="donor_id not found")
    donor = inv.donors[donor_id]
    idx = _load_parts_index_or_none(donor.device_slug)
    if idx is None:
        return {"donor_id": donor_id, "has_parts_index": False, "parts": []}
    parts = []
    for refdes, entry in idx.entries.items():
        parts.append({
            "refdes": refdes,
            "type": entry.type,
            "value_canonical": entry.value_canonical,
            "package": entry.package,
            "role_in_design": entry.role_in_design,
            "safety_class": entry.safety_class,
            "criticality_in_design": entry.criticality_in_design,
            "available": refdes not in donor.consumed,
        })
    return {"donor_id": donor_id, "has_parts_index": True, "parts": parts}
