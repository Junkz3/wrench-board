# SPDX-License-Identifier: Apache-2.0
"""Static prompts + user-message assembly for the bench generator LLM call.

Kept in one module so the system prompt can be version-controlled in
isolation. The graph summary deliberately omits edges + pins — the LLM
only needs to know WHICH refdes and rails exist, not their connectivity.
"""

from __future__ import annotations

from api.pipeline.schematic.schemas import ElectricalGraph

FORCED_TOOL_NAME = "propose_scenarios"


SYSTEM_PROMPT = """\
You are a diagnostic-scenario extractor for a board-level electronics
simulator benchmark. Given a device's research dump (forums, datasheets,
community posts — all web-search sourced with URLs) and the device's
compiled electrical graph (refdes, power rails), you propose a set of
failure scenarios that can be run against a physics-lite simulator.

Your output MUST satisfy these contracts — failures at any of them will
be discarded downstream:

1. GROUNDING. For every structured field you fill (cause.refdes,
   cause.mode, cause.value_ohms, cause.voltage_pct, expected_dead_rails,
   expected_dead_components), emit an `evidence` entry whose
   `source_quote_substring` is a LITERAL, VERBATIM substring of
   `source_quote`. Case-sensitive, no paraphrase, no normalisation.
   If you cannot find a literal substring that justifies a field, do
   NOT emit that field.

2. TOPOLOGY. Every refdes (cause + expected_dead_components) and every
   rail name you emit must exist in the provided graph. If the research
   says "LPC controller" and no such refdes is in the graph, skip that
   scenario — do not guess.

3. PROVENANCE. source_url must be an http(s) URL from the dump. source_quote
   is verbatim from the dump (≥ 50 chars). If the dump is vague, emit
   fewer scenarios with high confidence; do not pad.

4. FAILURE MODES. Exactly one of: dead | shorted | open | leaky_short |
   regulating_low. leaky_short requires value_ohms (typical 100-500 Ω),
   regulating_low requires voltage_pct (typical 0.75-0.95).

5. DEDUP. Do not emit two scenarios with the same (refdes, mode, rails,
   components) tuple.

6. ZERO CASCADE IS VALID. If the source describes a silent / local
   failure, emit empty expected_dead_rails AND expected_dead_components.
   This is a legitimate anti-pattern scenario the bench needs.

Return the scenarios via the `propose_scenarios` tool. No prose output.
"""


def graph_summary(graph: ElectricalGraph) -> str:
    """Compact projection of ElectricalGraph for the user prompt. Drops
    edges and pin-level detail; keeps refdes + kind + role + rails.

    NB — PowerRail field is `voltage_nominal` (not `nominal_voltage`) and
    its label field is `label` (not `id`). These match the current
    schematic schema; tests assert on these names.
    """
    lines = [f"Device slug: {graph.device_slug}"]
    lines.append(f"\n## Components ({len(graph.components)})")
    for refdes in sorted(graph.components):
        c = graph.components[refdes]
        role = c.role or "-"
        kind = c.kind or "-"
        lines.append(f"  {refdes} kind={kind} role={role}")
    lines.append(f"\n## Power rails ({len(graph.power_rails)})")
    for rail_key in sorted(graph.power_rails):
        r = graph.power_rails[rail_key]
        src = r.source_refdes or "-"
        dec = ",".join(r.decoupling or []) or "-"
        voltage = r.voltage_nominal if r.voltage_nominal is not None else float("nan")
        lines.append(
            f"  {r.label} voltage_nominal={voltage:.2f} source_refdes={src} decoupling={dec}"
        )
    return "\n".join(lines)


def build_user_message(
    *,
    raw_dump: str,
    rules_json: str,
    registry_json: str,
    graph: ElectricalGraph,
) -> str:
    """Concatenate the 4 input blocks in a stable order for caching."""
    return (
        "# Research dump (Scout)\n"
        f"{raw_dump}\n"
        "\n# Rules (Clinicien)\n"
        f"{rules_json}\n"
        "\n# Registry (canonical vocabulary)\n"
        f"{registry_json}\n"
        "\n# Electrical graph summary\n"
        f"{graph_summary(graph)}\n"
        "\nEmit the propose_scenarios tool call now."
    )
