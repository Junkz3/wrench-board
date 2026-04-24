# SPDX-License-Identifier: Apache-2.0
"""Phase 2.5 — Refdes Mapper.

Forced-tool sub-agent that maps registry canonical names to graph refdes
with evidence. Runs only when an ElectricalGraph is loaded. Output is
server-side-validated against three deterministic rules before persist:

1. evidence_quote must be a literal substring of the raw dump,
2. for literal_refdes_in_quote: refdes appears literally in evidence_quote
   (case-insensitive),
3. for mpn_match_in_quote: graph.components[refdes].value.mpn appears
   literally in evidence_quote (case-sensitive).

Failed attributions are dropped, not retried. An empty mapping is a
legitimate output — when the dump's vocabulary is purely functional, no
mapping is the correct answer. See spec
docs/superpowers/specs/2026-04-25-refdes-mapper-agent.md.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from anthropic import AsyncAnthropic

from api.pipeline.prompts import MAPPER_SYSTEM, MAPPER_USER_TEMPLATE
from api.pipeline.schemas import (
    RefdesAttribution,
    RefdesMappings,
    Registry,
)
from api.pipeline.tool_call import call_with_forced_tool

if TYPE_CHECKING:
    from api.pipeline.schematic.schemas import ElectricalGraph
    from api.pipeline.telemetry.token_stats import PhaseTokenStats

logger = logging.getLogger("microsolder.pipeline.mapper")


SUBMIT_REFDES_MAPPINGS_TOOL_NAME = "submit_refdes_mappings"


def _submit_mappings_tool() -> dict:
    """Forced-tool definition. Pydantic schema doubles as input_schema."""
    return {
        "name": SUBMIT_REFDES_MAPPINGS_TOOL_NAME,
        "description": (
            "Submit the canonical→refdes attributions for this device. "
            "An empty list is a valid, expected answer when the research "
            "dump is purely functional with no literal refdes / MPN match."
        ),
        "input_schema": RefdesMappings.model_json_schema(),
    }


def _build_graph_block(graph: ElectricalGraph) -> str:
    """Compact projection of the graph for the mapper user message.

    Per-component MPN/kind/role and power rails. No pin-level detail —
    the mapper does not infer topology, only matches MPN strings to dump
    quotes.
    """
    lines: list[str] = []
    lines.append("## Components (refdes → MPN, kind, role)")
    for refdes in sorted(graph.components):
        comp = graph.components[refdes]
        mpn = (comp.value.mpn if comp.value is not None else None) or "—"
        kind = comp.kind or "—"
        role = comp.role or "—"
        lines.append(f"- {refdes}: mpn={mpn} kind={kind} role={role}")

    lines.append("")
    lines.append("## Power rails (refdes that source / decouple them)")
    for rail_key in sorted(graph.power_rails):
        rail = graph.power_rails[rail_key]
        v = (
            f"{rail.voltage_nominal:.2f}V"
            if rail.voltage_nominal is not None
            else "?"
        )
        src = rail.source_refdes or "—"
        lines.append(f"- {rail.label}: voltage={v} source={src}")
    return "\n".join(lines)


def _validate_attributions(
    mappings: RefdesMappings,
    *,
    raw_dump: str,
    registry: Registry,
    graph: ElectricalGraph,
) -> RefdesMappings:
    """Server-side validator. Drop every attribution that fails any rule.

    Returns a new RefdesMappings carrying only the surviving subset.
    Logs each drop with the reason. The pipeline never retries — this is
    a hard, terminal filter."""
    canonical_set = {c.canonical_name for c in registry.components}
    survivors: list[RefdesAttribution] = []
    for a in mappings.attributions:
        # Rule 1: canonical_name in registry.
        if a.canonical_name not in canonical_set:
            logger.warning(
                "[Mapper] dropping attribution canonical=%r — not in registry",
                a.canonical_name,
            )
            continue

        # Rule 2: refdes in graph.
        graph_comp = graph.components.get(a.refdes)
        if graph_comp is None:
            logger.warning(
                "[Mapper] dropping attribution refdes=%r canonical=%r — not in graph",
                a.refdes,
                a.canonical_name,
            )
            continue

        # Rule 3: evidence_quote ⊂ dump (case-sensitive literal).
        if a.evidence_quote not in raw_dump:
            logger.warning(
                "[Mapper] dropping attribution refdes=%r canonical=%r — "
                "evidence_quote not literal substring of dump",
                a.refdes,
                a.canonical_name,
            )
            continue

        # Rule 4 / 5: kind-specific literal contract.
        if a.evidence_kind == "literal_refdes_in_quote":
            if a.refdes.lower() not in a.evidence_quote.lower():
                logger.warning(
                    "[Mapper] dropping attribution refdes=%r canonical=%r — "
                    "evidence_kind=literal_refdes_in_quote but refdes not in quote",
                    a.refdes,
                    a.canonical_name,
                )
                continue
        elif a.evidence_kind == "mpn_match_in_quote":
            mpn = graph_comp.value.mpn if graph_comp.value is not None else None
            if not mpn:
                logger.warning(
                    "[Mapper] dropping attribution refdes=%r canonical=%r — "
                    "evidence_kind=mpn_match_in_quote but graph has no MPN for this refdes",
                    a.refdes,
                    a.canonical_name,
                )
                continue
            if mpn not in a.evidence_quote:
                logger.warning(
                    "[Mapper] dropping attribution refdes=%r canonical=%r — "
                    "graph MPN %r not in evidence_quote",
                    a.refdes,
                    a.canonical_name,
                    mpn,
                )
                continue
        else:  # pragma: no cover — Pydantic Literal already constrains
            logger.warning(
                "[Mapper] dropping attribution refdes=%r — unknown evidence_kind=%r",
                a.refdes,
                a.evidence_kind,
            )
            continue

        survivors.append(a)

    n_dropped = len(mappings.attributions) - len(survivors)
    logger.info(
        "[Mapper] validation kept=%d dropped=%d / %d total",
        len(survivors),
        n_dropped,
        len(mappings.attributions),
    )
    return RefdesMappings(
        device_slug=mappings.device_slug,
        attributions=survivors,
    )


async def run_mapper(
    *,
    client: AsyncAnthropic,
    model: str,
    device_label: str,
    device_slug: str,
    raw_dump: str,
    registry: Registry,
    graph: ElectricalGraph,
    stats: PhaseTokenStats | None = None,
) -> RefdesMappings:
    """Execute Phase 2.5 — return server-validated `RefdesMappings`.

    Always returns a `RefdesMappings`; callers persist whatever survives.
    An empty mapping is legitimate (and frequent on functional-language
    dumps).
    """
    logger.info(
        "[Mapper] Mapping canonicals to refdes for device=%r · components=%d · graph_refdes=%d",
        device_label,
        len(registry.components),
        len(graph.components),
    )

    user_prompt = MAPPER_USER_TEMPLATE.format(
        device_label=device_label,
        raw_dump=raw_dump,
        registry_json=registry.model_dump_json(indent=2),
        graph_block=_build_graph_block(graph),
    )

    raw_mappings = await call_with_forced_tool(
        client=client,
        model=model,
        system=MAPPER_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        tools=[_submit_mappings_tool()],
        forced_tool_name=SUBMIT_REFDES_MAPPINGS_TOOL_NAME,
        output_schema=RefdesMappings,
        max_attempts=2,
        log_label="Mapper",
        stats=stats,
    )

    # Force device_slug to match the orchestrator's view — the model is
    # asked but we own the canonical value.
    raw_mappings = RefdesMappings(
        device_slug=device_slug,
        attributions=raw_mappings.attributions,
    )

    validated = _validate_attributions(
        raw_mappings,
        raw_dump=raw_dump,
        registry=registry,
        graph=graph,
    )
    logger.info(
        "[Mapper] Final attributions=%d (proposed=%d)",
        len(validated.attributions),
        len(raw_mappings.attributions),
    )
    # Light JSON probe — assertion that the survivor set serializes cleanly.
    json.loads(validated.model_dump_json())
    return validated
