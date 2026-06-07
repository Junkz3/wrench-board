"""Slow accuracy gate — verifies stock_search reliably returns matches
when the query is built from real parts in the 3 ingested devices.

Tagged @slow because it depends on memory/{slug}/parts_index.json
artefacts that are populated locally (gitignored). Skipped if the
artefacts are missing.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from api.stock.schemas import (
    DonorEntry,
    StockInventory,
    StockSearchQuery,
)
from api.stock.search import stock_search
from api.stock.store import save_inventory

REAL_DEVICES = ["iphone-x", "mnt-motherboard", "mnt-reform-motherboard"]


@pytest.fixture
def real_stock(tmp_path, monkeypatch):
    """Set up an inventory with one donor per ingested device, pointing at
    the existing memory/{slug}/parts_index.json files in the repo's memory/."""
    repo_memory = Path(__file__).resolve().parents[2] / "memory"
    monkeypatch.setattr("api.stock.store._memory_root", lambda: repo_memory)
    monkeypatch.setattr(
        "api.stock.store._stock_root",
        lambda: tmp_path / "_stock_isolated",
    )
    monkeypatch.setattr("api.stock.search._memory_root", lambda: repo_memory)

    inv = StockInventory(schema_version="1.0", donors={})
    for slug in REAL_DEVICES:
        if not (repo_memory / slug / "parts_index.json").exists():
            pytest.skip(
                f"parts_index.json not found for {slug} — run "
                f"`make tools-inventory && python -m api.pipeline.schematic.cli "
                f"--build-parts-index {slug}` to populate."
            )
        donor_id = f"{slug}-donor-2026-001"
        inv.donors[donor_id] = DonorEntry(
            donor_id=donor_id,
            device_slug=slug,
            label=f"Test {slug}",
            added_at=datetime.now(UTC),
            condition="donor_only",
        )
    save_inventory(inv)
    return repo_memory


@pytest.mark.slow
def test_search_returns_matches_for_random_real_parts(real_stock):
    """For each ingested device, sample 10 random refdes from its parts_index
    and verify a search built from their (type, value_canonical, package)
    returns at least one exact match. When package is missing on the source
    entry (some schematics don't surface it), the query elides package and
    relies on type+value alone — a realistic agent-flow query."""
    rng = random.Random(42)
    failures = []
    for slug in REAL_DEVICES:
        idx = json.loads((real_stock / slug / "parts_index.json").read_text())
        entries = list(idx["entries"].values())
        searchable = [e for e in entries if e["value_canonical"]]
        if len(searchable) < 10:
            pytest.skip(f"{slug} has fewer than 10 entries with a canonical value")
        sample = rng.sample(searchable, 10)
        for e in sample:
            q = StockSearchQuery(
                type=e["type"],
                value_canonical=e["value_canonical"],
                package=e["package"],  # optional — None passes through fine
                requested_role=e["role_in_design"] if e["role_in_design"] else None,
                exclude_donors=[
                    f"{s}-donor-2026-001" for s in REAL_DEVICES if s != slug
                ],
            )
            res = stock_search(q)
            if not res.exact_matches:
                failures.append(
                    f"{slug}/{e['refdes']} ({e['type']} "
                    f"{e['value_canonical']} pkg={e['package']} "
                    f"role={e['role_in_design']})"
                )

    assert not failures, "Queries with no exact match:\n  " + "\n  ".join(failures)
