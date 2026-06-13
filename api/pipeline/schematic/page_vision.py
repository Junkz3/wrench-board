"""Per-page Claude Opus vision call — RenderedPage → SchematicPageGraph.

Forced tool use with the full SchematicPageGraph schema as `input_schema`.
No grounding dump is injected into the prompt: Claude 4.8 vision is strong
enough on clean KiCad-style PDFs to extract refdes, values, topology, and
typed edges directly from the rendered page. pdfplumber only provides the
scan-detection hint passed as context.

Runs one page at a time; the orchestrator parallelises calls with
`asyncio.gather`, relying on Anthropic's automatic prompt caching on the
large `tools` array + the system prompt.
"""

from __future__ import annotations

import base64
import logging

from anthropic import AsyncAnthropic

from api.pipeline.schematic.renderer import RenderedPage
from api.pipeline.schematic.schemas import SchematicPageGraph
from api.pipeline.tool_call import call_with_forced_tool, effort_for_model

logger = logging.getLogger("wrench_board.pipeline.schematic.page_vision")


SUBMIT_PAGE_TOOL_NAME = "submit_schematic_page"

# Token budget: 128k total (Opus 4.8 max output) = up to 24k thinking +
# ~104k visible. Dense Apple pages (hundreds of nets) genuinely exceed the
# old 64k cap → they hit stop_reason=max_tokens and TRUNCATED, silently
# dropping real refdes/nets from the extraction (measured on a 92-page Mac
# build: 14 pages pegged out=64000). On a diagnostic graph, truncation is
# worse than the extra tokens — the cost is data we actually need. (V2:
# also trim the SchematicPageGraph schema to cut redundant verbosity.)
PAGE_MAX_TOKENS = 128000

# Extended thinking: the model reasons before emitting the structured tool
# call. Opus 4.7/4.8 only accept `adaptive` (the deprecated `enabled` type
# returns 400), and the default `display` is "omitted" (silent) — opt back
# into summarized blocks so observers see progress. Shared verbatim by the
# direct path (via tool_call.py) and the batch-vision twin.
PAGE_THINKING_PARAM = {"type": "adaptive", "display": "summarized"}


SYSTEM_PROMPT = """You are an expert electronics technician and schematic analyst.

You will receive one rendered page of a board-level schematic PDF. Your job is
to emit a single `submit_schematic_page` tool call whose payload matches the
SchematicPageGraph schema precisely.

Hard rules — NEVER violate:
1. Never invent a refdes, net label, pin number, value, or MPN. When a field
   cannot be determined from the image, use null or omit the entry. Empty or
   null is always preferable to a fabricated value.
2. Populate `typed_edges` whenever you can infer a semantic relationship from
   the page: `powers` / `powered_by` for regulator outputs / inputs, `enables`
   for EN/ON/OFF signals, `resets` for RESET pins, `decouples` for bypass caps
   placed next to a power pin, `filters` for series inductors on a rail.
3. For every off-page connector or hierarchical port visible on the page,
   emit a `CrossPageRef` with its label (the text printed next to the symbol).
   Set `direction` to `in`, `out`, or `bidir` based on the arrow direction,
   or `subsheet` for KiCad-style sub-sheet references.
4. Classify each pin's `role` from pin name + component context. Canonical
   patterns (commit, then fall back to `unknown` only when none fits):
   - Power: `VIN`/`VDD`/`VCC`/`AVDD`/`VBAT` → `power_in`; `VOUT`/`VBUS_OUT`
     → `power_out`; `SW`/`LX`/`PHASE` → `switch_node`; `GND`/`VSS`/`AGND`/
     `DGND` → `ground`.
   - Control: `EN`/`SHDN`/`ON_OFF` → `enable_in`; `PG`/`PGOOD` →
     `power_good_out`; `RESET`/`RSTn`/`POR` → `reset_in`/`reset_out` by
     direction; `FB`/`SENSE`/`VFB` → `feedback_in`; `CLK`/`XTAL` →
     `clock_in`/`clock_out`.
   - Digital bus: `Dn`/`DQn` (memory data lanes), `An`/`BA`/`RAS`/`CAS`/`WE`
     (memory address/control), `D+`/`D-`/`TX_P`/`TX_N`/`RX_P`/`RX_N` (diff
     pairs) → `bus_pin`.
   - Generic IO: `GPIOn`/`IO_n` → `signal_inout`; `IRQ`/`INT`/`ALERT`/`DREQ`
     (driven by the chip) → `signal_out`; named uni-directional logic →
     `signal_in` or `signal_out` from the page's arrow / functional context.
   - Misc: `NC`/`N.C.`/`No Connect` → `no_connect`; unlabelled pins on a
     connector / header symbol → `terminal`.
   When no canonical pattern fits, use `unknown` — never invent a role to
   look more thorough.
5. Mark components annotated as "NOSTUFF" / "DNP" / "DNI" with `populated=False`
   (this field lives on the PageNode itself, not inside `value`).
6. Capture designer annotations (magenta/italic text attached to a component
   or net) as `designer_notes`, attaching the refdes or net when the visual
   association is unambiguous.

SCHEMA PLACEMENT — common confusions to avoid:
- `populated` (bool) is ONLY on the PageNode (top level of a node).
- `polarity_marker` (bool) is ONLY inside the nested `value` object (i.e.
  `node.value.polarity_marker`), never at the top level of a node. Set
  it when a pin-1 dot or polarity band is visible on the symbol.
- `primary`, `package`, `mpn`, `tolerance`, `voltage_rating`, `temp_coef`,
  `description` all live inside `value`. When you read a chip like
  "LM2677SX-5", put it in BOTH `value.raw` AND `value.mpn`
  (it's the manufacturer part number and we want it searchable).
7. Use `confidence` honestly in [0.0, 1.0]: 1.0 when every visible element is
   clearly legible, lower when parts of the page are blurry, rotated, or dense
   beyond reliable reading.
8. Use `ambiguities` to flag anything you *see* but cannot *resolve* (e.g.
   "component at top-right has an unreadable refdes", "off-page connector
   lacks a legible label").

The page image is the sole source of truth — treat anything you can't see
as genuinely unknown, and emit null/empty rather than fabricate.
"""


def _submit_page_tool() -> dict:
    return {
        "name": SUBMIT_PAGE_TOOL_NAME,
        "description": (
            "Submit the structured analysis of one schematic page as a "
            "SchematicPageGraph payload."
        ),
        "input_schema": SchematicPageGraph.model_json_schema(),
        # Cache the (large) tool definition — identical across every page call
        # in a batch. On a warm hit Anthropic reports these tokens as
        # `cache_read_input_tokens`, cutting input cost 50-90%.
        "cache_control": {"type": "ephemeral"},
    }


def build_page_user_content(
    *,
    rendered: RenderedPage,
    total_pages: int,
    device_label: str | None = None,
    grounding: str | None = None,
) -> list[dict]:
    """Build the user-message content blocks for one page's vision call.

    Shared by the direct path (`extract_page`) and the batch twin
    (`batch_vision.build_page_request`) so the two passes extract with the
    byte-identical prompt — any drift here would mean the -50% batch pass
    silently produces different-quality graphs.

    When `grounding` is provided, it is inlined into the user message as a
    truth set. The system prompt tells the model to only emit refdes, net
    labels and values from the grounding — collapsing the fabrication failure
    mode observed on cheaper models running nu.
    """
    png_bytes = rendered.png_path.read_bytes()
    b64 = base64.standard_b64encode(png_bytes).decode("ascii")

    context_line = (
        f"Device: {device_label or 'unknown'}. "
        f"Page {rendered.page_number} of {total_pages}. "
        f"Orientation: {rendered.orientation}."
    )
    if rendered.is_scanned:
        context_line += (
            " This page looks rasterised (no extractable text or vectors) — "
            "expect lower legibility and set `confidence` accordingly."
        )

    user_content: list[dict] = [{"type": "text", "text": context_line}]
    if grounding:
        user_content.append({"type": "text", "text": grounding})
    user_content.append(
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": b64,
            },
        }
    )
    instruction = (
        "Analyse this page and call the submit_schematic_page tool with a "
        "complete SchematicPageGraph payload. Respect all hard rules from the "
        "system prompt. Null / empty over fabrication. A real schematic page "
        "is expected to populate ALL of: `nodes`, `nets`, `typed_edges`, and "
        "`designer_notes` — empty arrays are a red flag, not the goal. On "
        "pinout / fanout pages where a single component carries 100+ pins, "
        "expect 30-50+ distinct net labels: enumerate each as its own "
        "PageNet, including index-suffix replicas (e.g. base names ending "
        "in _0/_1/_2/_3 are distinct nets, not aliases of the bare base). "
        "Small-print supply rails in corners or near auxiliary functional "
        "blocks are the most commonly missed labels — read the whole image "
        "systematically, not just the visually dominant region. To assign "
        "pins to nets, trace each printed wire from the pin along the "
        "visible conductor to its terminal label, off-page connector or "
        "power symbol; do not infer net membership from spatial proximity "
        "of a pin to a nearby label.\n\n"
        "Separately, on a board-level power-distribution page — recognisable "
        "by multiple regulators (buck / LDO / load-switch ICs) feeding "
        "multiple downstream loads through ferrites, inductors, fuses and "
        "decoupling capacitors — every visible topological relationship "
        "should produce a `typed_edges` entry. Concretely, expect: each "
        "ceramic cap clustered next to a power/GND pin pair on an IC → a "
        "`decouples` edge from the cap to the parent IC; each series "
        "ferrite or inductor on a rail between source and load → a "
        "`filters` edge on that rail; each fuse or series resistor at a "
        "rail entry → a `powers` edge from source to sink along the rail; "
        "each regulator output pin (VOUT / SW post-LC / LDO output) → a "
        "`powers` edge to every load it feeds on the page. A power-tree "
        "page with 80+ components typically supports 40-80 such edges; "
        "<15 edges on such a page means the topology was not actually "
        "traced. CRITICAL anti-fabrication guard: an edge endpoint "
        "(`src` / `dst`) MUST already appear either in your `nodes` "
        "(refdes) or `nets` list — never invent an endpoint to satisfy "
        "edge completeness; if you cannot confidently identify both "
        "endpoints from the image, omit the edge."
    )
    if grounding:
        instruction += (
            " Use the grounding block as a spelling / existence check: refdes "
            "and net labels you emit SHOULD come from those sets (reject your "
            "own reading of a refdes or net if it contradicts the grounding). "
            "The grounding isn't necessarily complete on dense pages — if the "
            "image clearly shows a labelled net that's missing from the list, "
            "you may emit it AND add an entry in `ambiguities` noting the "
            "discrepancy. Trace wires from each pin to its destination label "
            "rather than guessing from adjacency alone."
        )
    user_content.append({"type": "text", "text": instruction})
    return user_content


def _system_cached() -> list[dict]:
    # Pass the system prompt as a cached content block so the burst of 12
    # page calls reuses the same 1.5k-token preamble via Anthropic's prompt
    # cache. The tool definition carries its own cache marker — together
    # they cover ~5-6k tokens of shared preamble.
    return [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def build_page_vision_params(
    *,
    model: str,
    rendered: RenderedPage,
    total_pages: int,
    device_label: str | None = None,
    grounding: str | None = None,
) -> dict:
    """Full Messages-API params for one page's vision call — the batch twin.

    Mirrors `call_with_forced_tool`'s FIRST attempt with thinking active
    (tool_choice "auto": the API rejects thinking + forced tool; the system
    prompt tells the model to always emit the tool). The retry tail of the
    direct path (validation suffix, thinking→forced fallback) has no batch
    equivalent — a failed batch page falls back to the direct path instead.
    Guarded by a parity test that diffs these params against the kwargs the
    direct path actually sends.
    """
    return {
        "model": model,
        "max_tokens": PAGE_MAX_TOKENS,
        "system": _system_cached(),
        "messages": [
            {
                "role": "user",
                "content": build_page_user_content(
                    rendered=rendered,
                    total_pages=total_pages,
                    device_label=device_label,
                    grounding=grounding,
                ),
            }
        ],
        "tools": [_submit_page_tool()],
        "tool_choice": {"type": "auto"},
        "thinking": dict(PAGE_THINKING_PARAM),
        "output_config": {"effort": effort_for_model(model)},
    }


def ensure_canonical_page(
    graph: SchematicPageGraph, page_number: int
) -> SchematicPageGraph:
    """Force `graph.page` to the canonical page number.

    The model occasionally fills `page` from its own prompt context; overwrite
    with the canonical value to guarantee downstream identity.
    """
    if graph.page != page_number:
        logger.info(
            "Model emitted page=%d, overriding with canonical page=%d",
            graph.page,
            page_number,
        )
        graph = graph.model_copy(update={"page": page_number})
    return graph


async def extract_page(
    *,
    client: AsyncAnthropic,
    model: str,
    rendered: RenderedPage,
    total_pages: int,
    device_label: str | None = None,
    grounding: str | None = None,
) -> SchematicPageGraph:
    """Run the per-page vision call and return a validated SchematicPageGraph.

    See `build_page_user_content` for the grounding semantics; the prompt and
    knobs are shared with the batch pass via the builders above.
    """
    graph = await call_with_forced_tool(
        client=client,
        model=model,
        system=_system_cached(),
        messages=[
            {
                "role": "user",
                "content": build_page_user_content(
                    rendered=rendered,
                    total_pages=total_pages,
                    device_label=device_label,
                    grounding=grounding,
                ),
            }
        ],
        tools=[_submit_page_tool()],
        forced_tool_name=SUBMIT_PAGE_TOOL_NAME,
        output_schema=SchematicPageGraph,
        max_tokens=PAGE_MAX_TOKENS,
        # The integer budget is unused under adaptive thinking (see
        # tool_call.py) — non-None simply switches thinking on.
        thinking_budget=24000,
        log_label=f"page_vision:page_{rendered.page_number}",
    )
    return ensure_canonical_page(graph, rendered.page_number)
