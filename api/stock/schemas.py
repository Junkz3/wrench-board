"""Pydantic schemas for the stock subsystem.

See docs/superpowers/specs/2026-05-08-stock-inventory-design.md §4 for the
authoritative data shape definitions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from api.pipeline.schematic.schemas import ComponentKind, ComponentType


class PartsIndexEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refdes: str
    type: ComponentType
    kind: ComponentKind
    value_canonical: str | None
    value_raw: str | None
    package: str | None
    mpn: str | None
    voltage_rating: float | None
    tolerance: str | None
    role_in_design: str | None
    safety_class: Literal["exact_only", "tolerant_with_warning", "blocked"]
    criticality_in_design: Literal["low", "medium", "high"]
    pages: list[int] = Field(default_factory=list)


class PartsIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    device_slug: str
    generated_at: datetime
    source_electrical_graph_hash: str
    entries: dict[str, PartsIndexEntry] = Field(default_factory=dict)


class ConsumedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refdes: str
    consumed_at: datetime
    repair_id: str | None = None
    notes: str | None = None


class DonorEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    donor_id: str
    device_slug: str
    label: str
    added_at: datetime
    condition: Literal["donor_only", "potentially_repairable"] = "donor_only"
    consumed: dict[str, ConsumedEvent] = Field(default_factory=dict)


class StockInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    donors: dict[str, DonorEntry] = Field(default_factory=dict)


class StockSearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ComponentType | None = None
    value_canonical: str | None = None
    package: str | None = None
    mpn: str | None = None
    voltage_min: float | None = None
    requested_role: str | None = None
    exclude_donors: list[str] = Field(default_factory=list)


class StockSearchMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    donor_id: str
    donor_label: str
    device_slug: str
    refdes: str
    value_canonical: str | None
    package: str | None
    mpn: str | None
    voltage_rating: float | None
    pages: list[int] = Field(default_factory=list)
    criticality_in_donor: Literal["low", "medium", "high"]


class StockSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exact_matches: list[StockSearchMatch] = Field(default_factory=list)
    empty_reason: str | None = None
