from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from api.stock.schemas import (
    ConsumedEvent,
    DonorEntry,
    PartsIndex,
    PartsIndexEntry,
    StockInventory,
    StockSearchQuery,
    StockSearchResult,
)


def test_parts_index_entry_round_trip():
    entry = PartsIndexEntry(
        refdes="C42",
        type="capacitor",
        kind="passive_c",
        value_canonical="0.1uF",
        value_raw="100nF",
        package="0402",
        mpn=None,
        voltage_rating=25.0,
        tolerance="±10%",
        role_in_design="decoupling",
        safety_class="tolerant_with_warning",
        criticality_in_design="low",
        pages=[3, 4],
    )
    payload = entry.model_dump_json()
    reloaded = PartsIndexEntry.model_validate_json(payload)
    assert reloaded == entry


def test_parts_index_extra_field_forbidden():
    with pytest.raises(ValidationError):
        PartsIndexEntry.model_validate(
            {
                "refdes": "C1",
                "type": "capacitor",
                "kind": "passive_c",
                "value_canonical": "1uF",
                "value_raw": "1uF",
                "package": "0402",
                "mpn": None,
                "voltage_rating": None,
                "tolerance": None,
                "role_in_design": "decoupling",
                "safety_class": "tolerant_with_warning",
                "criticality_in_design": "low",
                "pages": [1],
                "rogue_field": "should_fail",
            }
        )


def test_donor_entry_consumed_dict():
    donor = DonorEntry(
        donor_id="iphone-x-donor-2026-001",
        device_slug="iphone-x",
        label="iPhone X HS écran cassé lot 2024-001",
        added_at=datetime(2026, 5, 8, 10, 0, tzinfo=UTC),
        condition="donor_only",
        consumed={
            "U7": ConsumedEvent(
                refdes="U7",
                consumed_at=datetime(2026, 5, 9, 14, 30, tzinfo=UTC),
                repair_id="repair-abc",
                notes="remplacement PMIC sur iphone-13-...",
            )
        },
    )
    payload = donor.model_dump_json()
    reloaded = DonorEntry.model_validate_json(payload)
    assert reloaded == donor


def test_stock_inventory_empty_round_trip():
    inv = StockInventory(schema_version="1.0", donors={})
    payload = inv.model_dump_json()
    reloaded = StockInventory.model_validate_json(payload)
    assert reloaded == inv
    assert reloaded.donors == {}


def test_parts_index_entries_keyed_by_refdes():
    idx = PartsIndex(
        schema_version="1.0",
        device_slug="iphone-x",
        generated_at=datetime(2026, 5, 8, tzinfo=UTC),
        source_electrical_graph_hash="deadbeef" * 8,
        entries={
            "C42": PartsIndexEntry(
                refdes="C42",
                type="capacitor",
                kind="passive_c",
                value_canonical="0.1uF",
                value_raw="0.1uF",
                package="0402",
                mpn=None,
                voltage_rating=25.0,
                tolerance=None,
                role_in_design="decoupling",
                safety_class="tolerant_with_warning",
                criticality_in_design="low",
                pages=[3],
            ),
        },
    )
    reloaded = PartsIndex.model_validate_json(idx.model_dump_json())
    assert reloaded.entries["C42"].value_canonical == "0.1uF"


def test_stock_search_query_minimal():
    q = StockSearchQuery(type="capacitor")
    assert q.value_canonical is None
    assert q.exclude_donors == []


def test_stock_search_result_empty():
    res = StockSearchResult(
        exact_matches=[],
        tolerant_matches=[],
        blocked_substitutes=[],
        empty_reason="no donors in stock",
    )
    assert res.empty_reason == "no donors in stock"
