# SPDX-License-Identifier: Apache-2.0
"""Ground-truth outcome persisted per repair when the tech clicks « Marquer fix ».

One JSON file per repair at memory/{slug}/repairs/{repair_id}/outcome.json.
Single-valued per repair — subsequent writes overwrite (the latest tech
validation wins). Emitted by `mb_validate_finding` and read by the
field-corpus builder.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("microsolder.agent.validation")


FixMode = Literal["dead", "alive", "anomalous", "hot", "shorted", "passive_swap"]
AgentConfidence = Literal["high", "medium", "low"]


class ValidatedFix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refdes: str
    mode: FixMode
    rationale: str


class RepairOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validated_at: str           # ISO 8601 UTC
    repair_id: str
    device_slug: str
    fixes: list[ValidatedFix]
    tech_note: str | None = None
    agent_confidence: AgentConfidence = "high"


def _outcome_path(memory_root: Path, device_slug: str, repair_id: str) -> Path:
    return memory_root / device_slug / "repairs" / repair_id / "outcome.json"


def write_outcome(*, memory_root: Path, outcome: RepairOutcome) -> bool:
    """Write (or overwrite) the outcome.json for a repair. Returns True on success."""
    path = _outcome_path(memory_root, outcome.device_slug, outcome.repair_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(outcome.model_dump_json(indent=2), encoding="utf-8")
        return True
    except OSError as exc:
        logger.warning(
            "write_outcome: IO error for %s/%s: %s",
            outcome.device_slug, outcome.repair_id, exc,
        )
        return False


def load_outcome(
    *, memory_root: Path, device_slug: str, repair_id: str,
) -> RepairOutcome | None:
    """Return the RepairOutcome for a repair, or None if not yet validated."""
    path = _outcome_path(memory_root, device_slug, repair_id)
    if not path.exists():
        return None
    try:
        return RepairOutcome.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "load_outcome: failed to read %s/%s: %s",
            device_slug, repair_id, exc,
        )
        return None
