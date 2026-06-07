"""Stock search engine — strict matching only, with safety-aware indexing.

v1 contract: only strict matches are returned. A strict match requires
type + package + value_canonical + MPN to be identical, and
voltage_rating ≥ voltage_min (over-spec is fine). No tolerant lane and
no blocked-substitute reporting — relaxing dimensions (voltage downgrade,
value bin, package variants) opens safety holes that need per-role
tolerance windows we don't yet have empirical data to calibrate. The
`safety_class` field on PartsIndexEntry is still computed at index-build
time so a future tolerant lane can re-use it without re-classifying.

See docs/superpowers/specs/2026-05-08-stock-inventory-design.md §6.
"""

from __future__ import annotations

from pathlib import Path

from api.config import get_settings
from api.stock.schemas import (
    PartsIndex,
    PartsIndexEntry,
    StockSearchMatch,
    StockSearchQuery,
    StockSearchResult,
)
from api.stock.store import load_inventory


def _memory_root() -> Path:
    return Path(get_settings().memory_root)


def _load_parts_index(slug: str) -> PartsIndex | None:
    p = _memory_root() / slug / "parts_index.json"
    if not p.exists():
        return None
    return PartsIndex.model_validate_json(p.read_text(encoding="utf-8"))


def _is_strict_match(entry: PartsIndexEntry, q: StockSearchQuery) -> bool:
    """Type, package, value_canonical, MPN must match. voltage_rating ≥ requested."""
    if q.type and entry.type != q.type:
        return False
    if q.package and entry.package != q.package:
        return False
    if q.value_canonical and entry.value_canonical != q.value_canonical:
        return False
    if q.mpn and entry.mpn != q.mpn:
        return False
    if q.voltage_min is not None:
        if entry.voltage_rating is None or entry.voltage_rating < q.voltage_min:
            return False
    return True


def _rank_key(match: StockSearchMatch) -> tuple:
    # Surface the LEAST-critical-on-donor parts first: harvesting a low-
    # criticality cap from a donor preserves more of the donor's residual
    # value than yanking a primary-rail PMIC. Higher criticality sinks
    # toward the bottom of the result list.
    crit_order = {"low": 0, "medium": 1, "high": 2}
    return (crit_order.get(match.criticality_in_donor, 9),)


def stock_search(query: StockSearchQuery, owner_ref: str | None = None) -> StockSearchResult:
    inv = load_inventory(owner_ref)
    if not inv.donors:
        return StockSearchResult(empty_reason="no donors in stock")

    exact: list[StockSearchMatch] = []

    for donor in inv.donors.values():
        if donor.donor_id in query.exclude_donors:
            continue
        idx = _load_parts_index(donor.device_slug)
        if idx is None:
            continue  # donor declared but no schematic ingested — skip silently

        for refdes, entry in idx.entries.items():
            if refdes in donor.consumed:
                continue
            if not _is_strict_match(entry, query):
                continue

            exact.append(StockSearchMatch(
                donor_id=donor.donor_id,
                donor_label=donor.label,
                device_slug=donor.device_slug,
                refdes=refdes,
                value_canonical=entry.value_canonical,
                package=entry.package,
                mpn=entry.mpn,
                voltage_rating=entry.voltage_rating,
                pages=entry.pages,
                criticality_in_donor=entry.criticality_in_design,
            ))

    return StockSearchResult(
        exact_matches=sorted(exact, key=_rank_key),
        empty_reason=None if exact else "no matching parts available",
    )
