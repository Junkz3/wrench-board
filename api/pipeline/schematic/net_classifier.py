# SPDX-License-Identifier: Apache-2.0
"""Net classifier — tags every net with a functional domain.

Two layers of intelligence, same pattern as the boot sequence analyzer:

1. `classify_nets_regex(graph)` — deterministic fallback. A regex ruleset
   recognises the common naming conventions (HDMI_*, USB_*, PCIE_*, …).
   Fast, free, always available. Used when Opus isn't reachable or when
   the user explicitly wants a reproducible baseline.

2. `classify_nets_llm(graph, client, model)` — Opus post-pass. One call
   that sees every net + its connected components + relevant designer
   notes, emits rich metadata (domain + description + voltage_level +
   confidence). Budget: ~$0.80 / device, graceful on failure.

Both paths return the same `NetClassification` shape so callers don't
care which tier ran. The orchestrator calls the LLM version in parallel
with the boot analyzer via `asyncio.gather`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter

from anthropic import AsyncAnthropic

from api.pipeline.schematic.schemas import (
    ClassifiedNet,
    ElectricalGraph,
    NetClassification,
    SchematicGraph,
)
from api.pipeline.tool_call import call_with_forced_tool

# Batch size for parallel LLM calls. 100 nets → ~10k output tokens per
# batch → well within the model's max_tokens, allowing parallel dispatch
# of 5-6 batches (~30s wall clock on MNT-sized boards vs 3+ min single-call).
_BATCH_SIZE = 100

# We use Sonnet by default for classification — the reasoning needed here
# (domain tagging + one-sentence description) doesn't justify Opus's cost.
# Callers can override with model= to use anything. The default is read
# lazily from settings.anthropic_model_sonnet at call time.

logger = logging.getLogger("wrench_board.pipeline.schematic.net_classifier")


SUBMIT_TOOL_NAME = "submit_net_classification"


# ----------------------------------------------------------------------
# Deterministic regex-based classifier (fallback / baseline)
# ----------------------------------------------------------------------

# Ordered rules — first match wins. Upper-cased label is matched.
# Patterns are anchored/fenced to avoid greedy false-positives (e.g.
# "USB" substring in "BUS_*" nets).
# Order matters — first match wins. Most specific buckets first (bus names,
# functional prefixes), then generic power / sequencing patterns, then
# clock / misc last.
_REGEX_RULES: list[tuple[str, re.Pattern]] = [
    # Exact ground first — unambiguous anchor.
    ("ground",     re.compile(r"^(?:GND|AGND|DGND|PGND|SGND)(?:_[A-Z0-9]+)?$")),

    # Bus-specific prefixes — placed BEFORE clock so PCIE1_CLK_P stays pcie
    # and EMMC_CLK stays storage rather than matching the generic clock rule.
    ("hdmi",       re.compile(r"(?:^HDMI_|^TMDS_|_CEC$|_DDC_|^DDC_)")),
    ("usb",        re.compile(r"(?:^USB_|USB_DP|USB_DM|USB_OC|USB_VBUS)")),
    ("pcie",       re.compile(r"(?:^PCIE_|^PCIE\d+_)")),
    ("ethernet",   re.compile(r"(?:^ETH_|^RGMII_|^MII_|^MDIO_|^PHY_)")),
    ("display",    re.compile(r"(?:^EDP_|^DSI_|_DSI_|^LCD_|LCD_|BACKLIGHT_|_BL_|_BL$|LVDS_)")),
    ("storage",    re.compile(r"(?:^SD_|_SD_|^EMMC_|^SDHC_|^MMC_|_MMC_)")),
    # Audio comes after display so EDP_BL_EN doesn't match via AUDIO_ hits.
    # AVDD / DBVDD / DCVDD etc. are now in power_rail (they are rails).
    ("audio",      re.compile(r"(?:^DAC_|^ADC_|^I2S_|_I2S_|^SPDIF|SPDIF_|MICBIAS|AUDIO_|^LRCLK|_LRCLK|^BCLK|_BCLK)")),

    # Debug before control — JTAG/UART/SWD take precedence over generic I2C/SPI.
    ("debug",      re.compile(r"(?:^JTAG_|^SWD_|^UART_|_UART_|^DEBUG_|^TDO$|^TDI$|^TCK$|^TMS$|^SWDIO|^SWCLK)")),
    ("control",    re.compile(r"(?:^I2C_|_I2C_|^SPI_|_SPI_|^SDA$|^SCL$|_SDA$|_SCL$)")),

    # Power-sequencing signals — MUST land before power_rail, otherwise
    # "5V_PWR_EN" matches rail first. Strict suffixes / prefixes only.
    ("power_seq",  re.compile(r"(?:_PWR_EN$|_PG$|_EN$|POWER_GOOD|^PG_|^EN_)")),

    # Reset lines.
    ("reset",      re.compile(r"(?:^RESET$|^RESET_|_RST$|^XRESET$|^POR_|^POR$|RESET_N$|NRESET$)")),

    # Power rails — strict: entire label must be a known rail name with an
    # allowed rail-ish suffix (STANDBY, AUX, IN, OUT, SUPPLY, PWR) or nothing
    # at all. Prevents "5V_PWR_EN" / "+3V3_PG" from matching here.
    ("power_rail", re.compile(
        r"^(?:"
        r"\+?\d+V\d*"
        r"|VIN|VOUT|VCC|VDD|VSS|VBUS"
        r"|AVDD|DBVDD|DCVDD|SPKVDD|INTVCC|LPC_VCC"
        r"|BAT\d*|PVIN"
        r")"
        r"(?:_(?:STANDBY|AUX|IN|OUT|SUPPLY|PWR|PWR_AUX|\d+))?$"
    )),

    # Clock — generic pattern, placed near the end to avoid catching
    # bus-specific CLK fields.
    ("clock",      re.compile(r"(?:^CLK_|_CLK$|XTAL_|OSC_|REFCLK)")),
]


def classify_net_regex(label: str) -> str:
    """Return a domain string for `label` via regex matching. 'misc' on no hit."""
    if not label:
        return "misc"
    up = label.upper()
    for domain, pat in _REGEX_RULES:
        if pat.search(up):
            return domain
    return "misc"


def classify_nets_regex(graph: ElectricalGraph) -> NetClassification:
    """Classify every net in the graph with the regex ruleset only.

    No LLM call, no description/voltage_level — plain domain assignment
    with confidence 0.6 (we know the regex captures the common cases
    well but misses custom conventions).
    """
    classified: dict[str, ClassifiedNet] = {}
    for label in graph.nets.keys():
        domain = classify_net_regex(label)
        classified[label] = ClassifiedNet(
            label=label,
            domain=domain,
            description="",
            voltage_level=None,
            confidence=0.6,
        )
    counts = dict(Counter(c.domain for c in classified.values()))
    return NetClassification(
        device_slug=graph.device_slug,
        nets=classified,
        domain_summary=counts,
        ambiguities=[],
        model_used="regex",
    )


# ----------------------------------------------------------------------
# LLM classifier — Opus post-pass
# ----------------------------------------------------------------------


SYSTEM_PROMPT = """You classify every net in a board's electrical graph
by its functional domain, producing diagnostic metadata the technician
uses to narrow a symptom down to a few probe points.

Canonical domains (use these strings as written, 'misc' only when truly
no hint exists):

  - hdmi          — HDMI_*, TMDS_*, CEC, DDC_SDA/SCL
  - usb           — USB_*, USB_DP/DM, VBUS (when used as USB data path)
  - pcie          — PCIE_*, PCIE1_CLK_P/N, REFCLK of PCIe
  - ethernet      — ETH_*, RGMII_*, MII_*, MDIO_*, PHY_*
  - audio         — DAC/ADC lines, I2S bus, SPDIF, MICBIAS, AVDD/DBVDD/
                     DCVDD/SPKVDD rails of the codec
  - display       — EDP_*, DSI_*, LCD_*, BACKLIGHT_*
  - storage       — SD_*, EMMC_*, MMC_*
  - debug         — JTAG_*, SWD_*, UART_*, BOOT_UART
  - power_seq     — enable / power_good / sequencing signals. Names
                     usually end with _PWR_EN, _PG, _EN, or contain
                     POWER_GOOD / EN_ / PG_. NOT for rail voltages.
  - power_rail    — actual DC rails: +5V, +3V3, VIN, VOUT, VCC, VDD,
                     AVDD, LPC_VCC, battery stacks
  - ground        — GND and its variants (AGND/DGND/PGND)
  - clock         — oscillator outputs, XTAL, REFCLK
  - reset         — reset lines (RESET, *_RST, XRESET, POR_*)
  - control       — I2C / SPI / control UART that isn't a dedicated
                     debug port. Use debug for JTAG/boot UART.
  - misc          — use only if no hint whatsoever

For EACH net in the input list, emit:
  - label         — exact net label (copy it verbatim)
  - domain        — one of the above
  - description   — ONE short sentence (< 140 chars) on the net's role
  - voltage_level — typical electrical characteristic when healthy
                     ('3V3 logic', 'differential ±500mV', 'rail 5V',
                     'open-drain pull-up'). Null if ambiguous.
  - confidence    — 0..1, lower it when the name is cryptic or the
                     connected components don't narrow it down

Then emit global fields:
  - nets               — map of label → classified net
  - domain_summary     — count per domain
  - ambiguities        — short notes for nets you couldn't classify
                         confidently

Rely on the connected-refdes list + designer notes when the label alone
is ambiguous. Never invent a net that wasn't in the input list.
"""


def _format_nets_for_prompt(
    graph: ElectricalGraph,
    labels: list[str] | None = None,
    max_net_hints: int = 20,
) -> str:
    """Render the subset of nets for the prompt (defaults to all nets)."""
    lines: list[str] = []
    selected = labels if labels is not None else sorted(graph.nets.keys())
    for label in selected:
        net = graph.nets.get(label)
        if net is None:
            continue
        hint_parts = []
        if net.is_power:
            hint_parts.append("is_power")
        if net.is_global:
            hint_parts.append("is_global")
        pages = ",".join(str(p) for p in (net.pages or []))
        if pages:
            hint_parts.append(f"pages={pages}")
        connects = (net.connects or [])[:max_net_hints]
        hint = f" [{', '.join(hint_parts)}]" if hint_parts else ""
        conn_str = f" connects={connects}" if connects else ""
        lines.append(f"- {label}{hint}{conn_str}")
    return "\n".join(lines)


def _format_design_notes_for_prompt(graph: ElectricalGraph, limit: int = 40) -> str:
    """Keep designer notes that mention a net label or functional keyword."""
    if not graph.designer_notes:
        return "(no designer notes)"
    keywords = (
        "hdmi", "usb", "pcie", "ethernet", "audio", "codec", "dac",
        "i2s", "lvds", "dsi", "edp", "backlight", "sd", "emmc",
        "jtag", "uart", "boot", "pg_", "_pg", "enable", "reset",
    )
    filtered: list[str] = []
    for n in graph.designer_notes:
        t = (n.text or "").strip().lower()
        if not t:
            continue
        if any(k in t for k in keywords):
            attach = n.attached_to_refdes or n.attached_to_net or "-"
            filtered.append(f"- p{n.page} [{attach}] {n.text[:200]}")
            if len(filtered) >= limit:
                break
    return "\n".join(filtered) or "(no relevant notes)"


def build_context(
    graph: ElectricalGraph,
    labels: list[str] | None = None,
) -> str:
    count = len(labels) if labels is not None else len(graph.nets)
    return f"""\
DEVICE: {graph.device_slug}

NETS TO CLASSIFY ({count} nets in this batch):
{_format_nets_for_prompt(graph, labels=labels)}

DESIGNER NOTES relevant to net domains:
{_format_design_notes_for_prompt(graph)}

Produce the NetClassification via the forced tool. You MUST emit EVERY
net from the list above — all {count} of them — in the `nets` field.
Never skip a net; use domain='misc' with low confidence rather than
omitting. The `domain_summary` will be recomputed downstream, emit a
best-effort count.
"""


def _tool_definition() -> dict:
    return {
        "name": SUBMIT_TOOL_NAME,
        "description": (
            "Submit the net classification. Every net from the input list MUST "
            "appear in `nets`. Never invent a net that wasn't listed."
        ),
        "input_schema": NetClassification.model_json_schema(),
    }


async def _classify_batch(
    graph: ElectricalGraph,
    labels: list[str],
    *,
    client: AsyncAnthropic,
    model: str,
    batch_idx: int,
) -> NetClassification:
    """Classify one batch of nets. Single LLM call, ~10k output tokens."""
    result = await call_with_forced_tool(
        client=client,
        model=model,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": build_context(graph, labels=labels),
        }],
        tools=[_tool_definition()],
        forced_tool_name=SUBMIT_TOOL_NAME,
        output_schema=NetClassification,
        max_attempts=2,
        max_tokens=16000,
        log_label=f"net_classifier({graph.device_slug})[batch{batch_idx}]",
    )
    return result


async def classify_nets_llm(
    graph: ElectricalGraph,
    *,
    client: AsyncAnthropic,
    model: str | None = None,
) -> NetClassification:
    """Classify every net in the graph using the LLM.

    Strategy: batch the labels into groups of `_BATCH_SIZE` and dispatch
    them in parallel. Each batch stays well under max_tokens and the
    wall-clock is dominated by the slowest batch (~30s on MNT sized boards
    vs 3+ min for a single mega-call).
    """
    from api.config import get_settings
    model = model or get_settings().anthropic_model_sonnet
    all_labels = sorted(graph.nets.keys())
    batches = [all_labels[i:i + _BATCH_SIZE] for i in range(0, len(all_labels), _BATCH_SIZE)]
    logger.info(
        "net_classifier starting (model=%s slug=%s nets=%d batches=%d notes=%d)",
        model, graph.device_slug, len(all_labels), len(batches), len(graph.designer_notes),
    )

    tasks = [
        _classify_batch(graph, batch, client=client, model=model, batch_idx=i)
        for i, batch in enumerate(batches)
    ]
    partial_results = await asyncio.gather(*tasks)

    # Merge all batches.
    merged_nets: dict[str, ClassifiedNet] = {}
    merged_ambiguities: list[str] = []
    for r in partial_results:
        merged_nets.update(r.nets)
        merged_ambiguities.extend(r.ambiguities)

    missing = [label for label in all_labels if label not in merged_nets]
    if missing:
        logger.warning(
            "net_classifier: %d nets missing after merge — filling with "
            "regex fallback (first 5: %s)",
            len(missing), missing[:5],
        )
        # Fill holes with regex classification so every input net has an entry.
        for label in missing:
            merged_nets[label] = ClassifiedNet(
                label=label,
                domain=classify_net_regex(label),
                description="",
                voltage_level=None,
                confidence=0.5,  # signal that this came from the regex fallback
            )

    counts = dict(Counter(c.domain for c in merged_nets.values()))
    result = NetClassification(
        device_slug=graph.device_slug,
        nets=merged_nets,
        domain_summary=counts,
        ambiguities=merged_ambiguities,
        model_used=model,
    )
    logger.info(
        "net_classifier done (slug=%s classified=%d domains=%d)",
        graph.device_slug, len(result.nets), len(result.domain_summary),
    )
    return result


def apply_power_rail_classification(
    schematic_graph: SchematicGraph,
    classification: NetClassification,
    *,
    min_confidence: float = 0.7,
) -> list[str]:
    """Promote nets classified `domain=power_rail` to `is_power=True`.

    The vision pass emits `NetNode.is_power` heuristically and misses rails
    that don't match a well-known label pattern (e.g. PVIN fed by a
    load-switch on MNT Reform). The Opus net classifier identifies those
    nets as `domain=power_rail` with high confidence — this helper lifts
    that decision back into `SchematicGraph.nets[label].is_power` so a
    subsequent `compile_electrical_graph` call registers them in
    `electrical.power_rails` and unlocks the downstream cascades.

    Returns the list of labels whose `is_power` flipped False → True.
    Nets already marked `is_power=True` are skipped (no-op).
    `min_confidence` gates promotion — defaults to 0.7 to exclude the
    regex-fallback 0.6 floor and accept only LLM-confident classifications.
    """
    promoted: list[str] = []
    for label, classified in classification.nets.items():
        if classified.domain != "power_rail":
            continue
        if classified.confidence < min_confidence:
            continue
        node = schematic_graph.nets.get(label)
        if node is None or node.is_power:
            continue
        node.is_power = True
        promoted.append(label)
    return promoted


async def classify_nets(
    graph: ElectricalGraph,
    *,
    client: AsyncAnthropic | None = None,
    model: str | None = None,
) -> NetClassification:
    """Public entry point used by the orchestrator.

    When a client is provided, call Opus; on any exception, fall back to
    the deterministic regex classifier. When no client, go straight to
    the regex path.
    """
    if client is None:
        return classify_nets_regex(graph)
    try:
        return await classify_nets_llm(graph, client=client, model=model)
    except Exception:
        logger.warning(
            "LLM net classifier failed, falling back to regex for slug=%s",
            graph.device_slug, exc_info=True,
        )
        return classify_nets_regex(graph)
