"""Boot sequence analyzer — Opus-refined, post-compile pass.

The deterministic compiler produces a `boot_sequence` via Kahn topological
sort on the rail-dependency DAG. That gives the *minimum causal order* but
not the *real* boot sequence of the board, because it ignores:

- Enable signals driven by an MCU / PMIC sequencer (often the LPC on MNT
  Reform, a PMIC like TPS65218 on Pi-class boards). A rail can be
  "power available" but not "enabled".
- Power-good chains (`PG_5V` → `EN_3V3` → …) that are in the schematic as
  plain `enables` edges but that the compiler treats no differently from
  a direct rail-consume.
- Designer notes that explicitly describe the sequencing (e.g.
  "Main system power converters, enabled by LPC", "Standby always-on").
- Always-on classification vs on-demand vs sequenced.

This module runs **one Opus call** per device, receives the compiled
`ElectricalGraph` as context, and returns an `AnalyzedBootSequence` that
classifies phases by kind (`always-on` / `sequenced` / `on-demand`),
identifies the sequencer, and cites evidence for each placement.

Budget: ~$0.25 per device run, 1-run per ingestion by default (graceful
on failure), re-runnable in isolation via CLI or HTTP.
"""

from __future__ import annotations

import logging

from anthropic import AsyncAnthropic

from api.config import get_settings
from api.pipeline.schematic.schemas import (
    AnalyzedBootSequence,
    ElectricalGraph,
)
from api.pipeline.tool_call import call_with_forced_tool

logger = logging.getLogger("wrench_board.pipeline.schematic.boot_analyzer")


SUBMIT_TOOL_NAME = "submit_analyzed_boot_sequence"


SYSTEM_PROMPT = """You are an expert in board-level power sequencing analysis.

You receive the compiled electrical graph of a device (rails with their
source ICs, enable nets, consumers; enable edges; designer notes). The
graph already has a `boot_sequence` field that was derived by topological
sort — that ordering is a *minimum causal order* only, not the real
sequence of the board.

Your job is to reconstruct the REAL boot sequence by leveraging:

  1. `enable_net` on each rail — the signal that gates the rail's producer.
  2. `typed_edges` with kind=enables — who drives each EN.
  3. `designer_notes` — often explicitly describe the sequencing
     ("Main system power converters, enabled by LPC", "Standby always-on",
     "Inrush Current Limiter", "Phase 2 locked by PG_5V").
  4. Refdes naming conventions (R*/C* = passives, U* = ICs,
     U-names like "LPC" / "PMIC" / "supervisor" are sequencers).

Classify every phase into one of three kinds:

  - `always-on`: lives whenever physical power is present (standby rails,
    battery chargers, supervisors) — no EN gating beyond a resistor divider
    from VIN or battery.
  - `sequenced`: gated by an enable signal from the sequencer (LPC/PMIC/EC
    drives `*_PWR_EN` lines). Needs an upstream phase to be stable first.
  - `on-demand`: user / OS triggers asynchronously (USB plug event, PCIe
    power-up after kernel handshake, audio codec enabled by driver, etc.).

For each phase, emit:
- `rails_stable`: rails up by the END of this phase.
- `components_entering`: refdes becoming active during this phase.
- `triggers_next`: the specific signals that assert at the phase boundary.
  Each trigger MUST name the net and its driver (when known).
- `evidence`: short quoted excerpts from designer_notes or named enable
  edges. NEVER invent evidence — if nothing supports the placement, say so
  in `ambiguities` and lower the confidence.
- `confidence`: 0..1. Lower it when evidence is indirect.

Identify the `sequencer_refdes` when one is visibly orchestrating the
sequence (MCU referenced in notes, the refdes driving 3+ EN lines).

Output every field of the AnalyzedBootSequence schema. Stay concise in
narrative fields (one sentence per rationale, one quote per evidence).
"""


def _format_rails(graph: ElectricalGraph) -> str:
    lines: list[str] = []
    for label, rail in sorted(graph.power_rails.items()):
        parts = [f"- {label}"]
        if rail.voltage_nominal is not None:
            parts.append(f"({rail.voltage_nominal} V)")
        if rail.source_refdes:
            parts.append(f"source={rail.source_refdes}")
            if rail.source_type:
                parts.append(f"[{rail.source_type}]")
        else:
            parts.append("source=external")
        if rail.enable_net:
            parts.append(f"enable={rail.enable_net}")
        if rail.consumers:
            parts.append(f"consumers=[{', '.join(rail.consumers[:8])}]")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _format_enable_edges(graph: ElectricalGraph) -> str:
    lines: list[str] = []
    for e in graph.typed_edges:
        kind = e.kind.lower()
        if kind not in {"enables", "resets", "produces_signal"}:
            continue
        page = f"p{e.page}" if e.page is not None else "p?"
        lines.append(f"- {e.src} --{kind}--> {e.dst}  ({page})")
    return "\n".join(lines) or "(none)"


def _format_designer_notes(graph: ElectricalGraph, limit: int = 60) -> str:
    """Keep only notes relevant to sequencing; trim to a budget."""
    keywords = (
        "phase", "boot", "sequence", "enable", "pg_", "_pg", "power_good",
        "start", "order", "always-on", "standby", "after", "before",
        "lpc", "pmic", "supervisor", "inrush", "charger", "soft-start",
        "main ", "aux ", "on-demand",
    )
    filtered: list[str] = []
    for n in graph.designer_notes:
        t = (n.text or "").strip()
        if not t:
            continue
        low = t.lower()
        if not any(k in low for k in keywords):
            continue
        attach = n.attached_to_refdes or n.attached_to_net or "-"
        page = n.page if n.page is not None else "?"
        filtered.append(f"- p{page} [{attach}] {t[:220]}")
        if len(filtered) >= limit:
            break
    return "\n".join(filtered) or "(no sequencing-relevant notes)"


def _format_compiler_sequence(graph: ElectricalGraph) -> str:
    if not graph.boot_sequence:
        return "(empty)"
    lines: list[str] = []
    for p in graph.boot_sequence:
        lines.append(
            f"- Phase {p.index} ({p.name}): rails={p.rails_stable} "
            f"comps={p.components_entering}"
        )
    return "\n".join(lines)


def build_context(graph: ElectricalGraph) -> str:
    """Build a compact user-message payload for Opus.

    Designed to fit ~5k tokens on a board the size of MNT Reform (27 rails,
    449 components, 108 designer notes). We pre-filter notes to sequencing
    keywords so Opus doesn't waste tokens on cosmetic annotations.
    """
    return f"""\
DEVICE: {graph.device_slug}

COMPILED BOOT SEQUENCE (topological, needs refinement):
{_format_compiler_sequence(graph)}

POWER RAILS ({len(graph.power_rails)}):
{_format_rails(graph)}

ENABLE / RESET / SIGNAL EDGES (from typed_edges):
{_format_enable_edges(graph)}

DESIGNER NOTES relevant to sequencing:
{_format_designer_notes(graph)}

Produce the AnalyzedBootSequence for this device via the forced tool call.
"""


def _tool_definition() -> dict:
    return {
        "name": SUBMIT_TOOL_NAME,
        "description": (
            "Submit the analyzed boot sequence. Every phase must have evidence "
            "rooted in designer_notes or explicit enable edges — never fabricate."
        ),
        "input_schema": AnalyzedBootSequence.model_json_schema(),
    }


async def analyze_boot_sequence(
    graph: ElectricalGraph,
    *,
    client: AsyncAnthropic,
    model: str | None = None,
) -> AnalyzedBootSequence:
    """Run the Opus analysis pass on `graph` and return the validated result.

    `model` defaults to `ANTHROPIC_MODEL_MAIN` (Opus) from settings.
    Raises if the tool call fails validation after the usual retry budget.
    """
    model = model or get_settings().anthropic_model_main
    user_content = build_context(graph)
    logger.info(
        "boot_analyzer starting (model=%s slug=%s rails=%d notes=%d)",
        model, graph.device_slug, len(graph.power_rails), len(graph.designer_notes),
    )
    result = await call_with_forced_tool(
        client=client,
        model=model,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
        tools=[_tool_definition()],
        forced_tool_name=SUBMIT_TOOL_NAME,
        output_schema=AnalyzedBootSequence,
        max_attempts=2,
        max_tokens=8000,
        log_label=f"boot_analyzer({graph.device_slug})",
    )
    # Stamp the model used — the schema requires it and we know it authoritatively here.
    result = result.model_copy(update={"model_used": model})
    logger.info(
        "boot_analyzer done (slug=%s phases=%d confidence=%.2f sequencer=%s)",
        graph.device_slug, len(result.phases), result.global_confidence,
        result.sequencer_refdes or "none",
    )
    return result
