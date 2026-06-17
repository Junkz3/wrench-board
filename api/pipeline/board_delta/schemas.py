from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class SignatureIC(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part: str | None = Field(default=None, description="Part marking if named by sources (e.g. 'ISL9240'). Null when unknown.")
    refdes_hint: str | None = Field(default=None, description="Indicative refdes if a source mentions one (e.g. 'U5200'). NEVER treated as validated.")
    role: str = Field(description="Functional role: 'charger', 'PMIC', 'baseband PMU', 'USB-C CC controller'.")
    source_url: str = Field(description="URL the claim is grounded in.")


class NotableRail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(description="Rail name as written by sources (e.g. 'PP3v8_AON_VDDMAIN').")
    note: str = Field(description="Why it matters for this revision.")
    source_url: str = Field(description="URL the claim is grounded in.")


class RepairPitfall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(description="Short pitfall name (e.g. 'one-orientation USB-C').")
    detail: str = Field(description="Concrete symptom + cause as reported.")
    source_url: str = Field(description="URL the claim is grounded in.")


class KinshipHint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    board_number: str = Field(description="A neighbouring revision's board number mentioned by sources.")
    relation: str = Field(description="How it relates (e.g. 'predecessor Intel variant', 'same family, N6 die-shrink').")
    source_url: str = Field(description="URL the claim is grounded in.")


class DeltaSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(description="Source URL.")
    kind: str = Field(description="Source type: 'forum', 'ifixit', 'teardown', 'vendor', 'datasheet', 'video'.")


class DeltaBoard(BaseModel):
    """Per-revision context overlay derived from web search. Context/knowledge,
    NOT validated refdes. Lives at memory/{slug}/board_deltas/{board}.json.
    coverage='none' means the web had nothing usable: never inject it.
    """

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    device_label: str = Field(description="Commercial device label this delta was generated for.")
    board_number: str = Field(description="Normalized board number key.")
    coverage: Literal["rich", "thin", "none"] = Field(description="How much usable, sourced delta was found.")
    signature_ics: list[SignatureIC] = Field(default_factory=list)
    notable_rails: list[NotableRail] = Field(default_factory=list)
    repair_pitfalls: list[RepairPitfall] = Field(default_factory=list)
    kinship_hints: list[KinshipHint] = Field(default_factory=list)
    sources: list[DeltaSource] = Field(default_factory=list)
    generated_at: str | None = Field(default=None, description="ISO-8601 UTC stamp set at write time.")
    generated_by_tenant: str | None = Field(default=None, description="owner_ref of the tenant who generated it (provenance).")

    def is_empty(self) -> bool:
        return not (self.signature_ics or self.notable_rails or self.repair_pitfalls)
