"""Phase 1.5 — graph-arbitrated device-kind classification + reconciliation.

The classifier never sees refdes (the 2026-04-24 audit found refdes fabrication
when research stages were given component identities). It sees only the device
LABEL and a topology SUMMARY: power-rail names + a component-family histogram.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from api.pipeline.schemas import KindVerdict
from api.pipeline.tool_call import call_with_forced_tool

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

    from api.pipeline.schematic.schemas import ElectricalGraph
    from api.pipeline.telemetry.token_stats import PhaseTokenStats

logger = logging.getLogger("wrench_board.pipeline.device_kind")

CONFIRM_THRESHOLD = 0.6  # below this the verdict routes to user confirmation

SUBMIT_KIND_TOOL_NAME = "submit_device_kind"


def summarize_graph_for_kind(graph: ElectricalGraph) -> str:
    """Topology summary for the classifier: rail names + component-family counts.

    Deliberately excludes refdes and component values — only the *shape* of the
    power tree and the kinds of parts present, which is what distinguishes a GPU
    card from a phone logic board from a charging board.
    """
    rails = sorted(graph.power_rails.keys())
    fam = Counter(node.kind for node in graph.components.values())
    fam_str = ", ".join(f"{k}×{n}" for k, n in fam.most_common())
    return (
        f"Power rails ({len(rails)}): {', '.join(rails) if rails else 'none'}\n"
        f"Component families: {fam_str or 'none'}"
    )


_CLASSIFIER_SYSTEM = (
    "You are a hardware-repair triage classifier. Given a device label and a "
    "summary of its schematic's power rails and component families, output the "
    "single best device class. Weigh the rails as primary evidence (e.g. a GPU "
    "core rail + GDDR memory rails + a PCIe 12V input ⇒ gpu_card; a battery rail "
    "+ CPU/SoC rails ⇒ laptop_logic_board or phone_logic_board by scale; a single "
    "USB/charger input with no compute rails ⇒ power_charging_board). The device "
    "label may be a PCB code that is ambiguous or misleading — trust the topology "
    "over the label. Use 'unknown' only when the summary is genuinely uninformative. "
    "Set confidence honestly; <0.6 means a human should confirm."
)

_CLASSIFIER_USER_TEMPLATE = (
    "Device label (may be unreliable): {device_label}\n\n"
    "Schematic topology summary:\n{summary}\n\n"
    "Classify the device."
)


def _submit_kind_tool() -> dict:
    """Build the forced-tool definition whose `input_schema` matches `KindVerdict`."""
    return {
        "name": SUBMIT_KIND_TOOL_NAME,
        "description": "Submit the device class. Your only valid output.",
        "input_schema": KindVerdict.model_json_schema(),
    }


async def classify_device_kind(
    *,
    client: AsyncAnthropic,
    model: str,
    device_label: str,
    graph: ElectricalGraph,
    stats: PhaseTokenStats | None = None,
) -> KindVerdict:
    """Infer the device class from the graph summary (never refdes)."""
    logger.info("[DeviceKind] Classifying device_label=%r", device_label)
    summary = summarize_graph_for_kind(graph)
    user = _CLASSIFIER_USER_TEMPLATE.format(device_label=device_label, summary=summary)
    verdict = await call_with_forced_tool(
        client=client,
        model=model,
        system=_CLASSIFIER_SYSTEM,
        messages=[{"role": "user", "content": user}],
        tools=[_submit_kind_tool()],
        forced_tool_name=SUBMIT_KIND_TOOL_NAME,
        output_schema=KindVerdict,
        max_attempts=2,
        log_label="DeviceKind",
        stats=stats,
    )
    logger.info(
        "[DeviceKind] %r → %s (conf=%.2f) · %s",
        device_label,
        verdict.device_kind,
        verdict.confidence,
        verdict.evidence,
    )
    return verdict


@dataclass(frozen=True)
class KindResolution:
    resolved_kind: str | None  # set unless status == needs_confirmation
    status: Literal["user_only", "confirmed", "needs_confirmation"]
    user_declared: str | None
    graph_inferred: str | None
    confidence: float | None
    evidence: str | None


def reconcile_kind(
    *,
    user_declared: str | None,
    verdict: KindVerdict | None,
) -> KindResolution:
    """Combine the user's declared kind with the graph verdict.

    - no graph verdict          → trust the user (or 'unknown'), status user_only
    - verdict.confidence < gate → needs_confirmation (graph unsure)
    - user silent, conf ok      → take the graph, status confirmed
    - user == graph             → confirmed
    - user != graph             → needs_confirmation (human resolves)
    """
    if verdict is None:
        return KindResolution(
            user_declared or "unknown", "user_only", user_declared, None, None, None
        )
    base = dict(
        user_declared=user_declared,
        graph_inferred=verdict.device_kind,
        confidence=verdict.confidence,
        evidence=verdict.evidence,
    )
    if verdict.confidence < CONFIRM_THRESHOLD:
        return KindResolution(None, "needs_confirmation", **base)
    if user_declared is None or user_declared == "unknown":
        return KindResolution(verdict.device_kind, "confirmed", **base)
    if user_declared == verdict.device_kind:
        return KindResolution(verdict.device_kind, "confirmed", **base)
    return KindResolution(None, "needs_confirmation", **base)


_PENDING_FILE = "pending_kind.json"
_PROVENANCE_FILE = "device_kind.json"


def _to_dict(r: KindResolution) -> dict:
    return {
        "resolved_kind": r.resolved_kind,
        "status": r.status,
        "user_declared": r.user_declared,
        "graph_inferred": r.graph_inferred,
        "confidence": r.confidence,
        "evidence": r.evidence,
    }


def write_pending_kind(pack_dir: Path, r: KindResolution) -> None:
    (pack_dir / _PENDING_FILE).write_text(
        json.dumps(_to_dict(r), indent=2, ensure_ascii=False), encoding="utf-8")


def read_pending_kind(pack_dir: Path) -> dict | None:
    p = pack_dir / _PENDING_FILE
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("[DeviceKind] failed to read %s, treating as absent", p, exc_info=True)
        return None


def clear_pending_kind(pack_dir: Path) -> None:
    (pack_dir / _PENDING_FILE).unlink(missing_ok=True)


def write_kind_provenance(pack_dir: Path, r: KindResolution, *, resolved_by: str) -> None:
    data = _to_dict(r) | {"resolved_by": resolved_by}
    (pack_dir / _PROVENANCE_FILE).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
