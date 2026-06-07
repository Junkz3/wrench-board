"""Pydantic shapes for the bench generator.

Every model is `extra='forbid'`. Shapes come in three groups:

1. LLM input/output contract — `EvidenceSpan`, `Cause`, `ProposedScenarioDraft`,
   `ProposalsPayload`. Their JSON schemas are used as `input_schema` for the
   forced tool call in `extractor.py`.
2. Accepted / rejected outputs — `ProposedScenario` (full scenario ready for
   `auto_proposals/*.jsonl`), `Rejection` (with a controlled-vocabulary motive).
3. Run artefacts — `RunManifest`, `ReliabilityCard` (consumed by
   `api/agent/reliability.py`).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FailureMode = Literal["dead", "shorted", "open", "leaky_short", "regulating_low"]


class Cause(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refdes: str
    mode: FailureMode
    value_ohms: float | None = None
    voltage_pct: float | None = None

    @model_validator(mode="after")
    def _mode_specific_fields(self) -> Cause:
        if self.mode == "leaky_short" and self.value_ohms is None:
            raise ValueError("leaky_short mode requires value_ohms")
        if self.mode == "regulating_low" and self.voltage_pct is None:
            raise ValueError("regulating_low mode requires voltage_pct")
        return self


EvidenceField = Literal[
    "cause.refdes",
    "cause.mode",
    "cause.value_ohms",
    "cause.voltage_pct",
    "expected_dead_rails",
    "expected_dead_components",
]


class EvidenceSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: EvidenceField
    source_quote_substring: str = Field(
        ...,
        min_length=1,
        description=(
            "Sub-string copied verbatim from the scenario's source_quote. "
            "Will be checked literally (case-sensitive `in` test); any "
            "deviation yields rejection."
        ),
    )
    reasoning: str = Field(..., max_length=400)


class ProposedScenarioDraft(BaseModel):
    """The shape the LLM must produce per scenario."""

    model_config = ConfigDict(extra="forbid")

    local_id: str = Field(..., min_length=3, max_length=48)
    cause: Cause
    expected_dead_rails: list[str] = Field(default_factory=list)
    expected_dead_components: list[str] = Field(default_factory=list)
    source_url: str = Field(..., pattern=r"^https?://.+")
    source_quote: str = Field(..., min_length=50)
    confidence: float = Field(..., ge=0.0, le=1.0)
    evidence: list[EvidenceSpan] = Field(..., min_length=1)
    reasoning_summary: str = Field(..., max_length=800)


class ProposalsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenarios: list[ProposedScenarioDraft] = Field(default_factory=list, max_length=50)


class ProposedScenario(BaseModel):
    """Scenario promoted after V1-V5 validation. Landing shape in
    auto_proposals/{slug}-{date}.jsonl."""

    model_config = ConfigDict(extra="forbid")

    id: str
    device_slug: str
    cause: Cause
    expected_dead_rails: list[str] = Field(default_factory=list)
    expected_dead_components: list[str] = Field(default_factory=list)
    source_url: str
    source_quote: str
    source_archive: str
    confidence: float
    generated_by: str
    generated_at: str
    validated_by_human: bool = False
    evidence: list[EvidenceSpan] = Field(default_factory=list)


RejectionMotive = Literal[
    "unknown_mode",
    "value_ohms_missing",
    "voltage_pct_missing",
    "source_url_malformed",
    "source_quote_too_short",
    "evidence_missing",
    "evidence_field_empty",
    "evidence_span_not_literal",
    "refdes_not_in_graph",
    "rail_name_not_in_graph",
    "component_not_in_graph",
    "mode_not_pertinent",
    "duplicate_in_run",
    "opus_rescue_failed",
    # V2b semantic guardrails — attribution must be grounded in the source
    "refdes_not_mentioned_in_quote",
    "rail_not_mentioned_in_quote",
    "cause_not_connected_to_rail",
]


class Rejection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_id: str
    motive: RejectionMotive
    detail: str = ""
    original_draft: ProposedScenarioDraft | None = None


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_slug: str
    run_date: str  # YYYY-MM-DD
    run_timestamp: str  # ISO UTC
    model: str
    n_proposed: int
    n_accepted: int
    n_rejected: int
    input_mtimes: dict[str, float]
    escalated_rejects: bool


class ReliabilityCard(BaseModel):
    """Consumed by api/agent/reliability.py. Written at
    memory/{slug}/simulator_reliability.json."""

    model_config = ConfigDict(extra="forbid")

    device_slug: str
    score: float
    self_mrr: float
    cascade_recall: float
    n_scenarios: int
    generated_at: str
    source_run_date: str
    notes: list[str] = Field(default_factory=list)
