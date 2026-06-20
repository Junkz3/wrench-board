"""Deterministic existence ground-truth over the compiled electrical graph.

WHY THIS MODULE EXISTS
----------------------
On real builds the pack pipeline (Scout → Registry → Writers → Auditor
revise-loop) fails to converge. The web-derived Registry covers only ~2 % of
a board's identifiers, so the LLM Auditor flags REAL schematic identifiers as
"fabricated" — e.g. switch `SWV011`, or the `PP1V2_S2` rail at 1.2 V sourced by
`U8100`. Those are genuine, they just never made it into the thin web glossary.

The fix is to make `electrical_graph.json` queryable as the EXISTENCE ground
truth: "does U8100 exist?", "what powers PP1V2_S2?", "at what voltage?". Every
answer is a deterministic lookup against the compiled graph — no LLM, no guess.

CRITICAL DESIGN CONSTRAINT — QUERIES, NOT DUMPS
-----------------------------------------------
This module NEVER dumps the graph into a generative context. The 2026-04-24
lesson (see the Phase 1 comment in api/pipeline/orchestrator.py): when Scout was
handed the graph as context, a URL-by-URL audit found **23/23 fabricated refdes
attributions** — the model confidently invented connectivity that "fit". So the
only surfaces here are:
  * deterministic boolean / list lookups (GraphTruth),
  * a COMPILED, mention-scoped report that lists ONLY identifiers already named
    by the writers (build_ground_truth_report — never unmentioned graph content),
  * a single targeted query tool the model must call to verify a claim
    (QUERY_GRAPH_TOOL / handle_query_graph),
  * a deterministic registry-signal enrichment (enrich_registry_from_graph).

Downstream tasks (not in this module) wire these into drift / auditor / revisers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from api.pipeline.schematic.schemas import ElectricalGraph

# Every list answer is capped so a pathological board (thousands of consumers on
# GND, say) can never blow up a tool result or a compiled report. 30 is plenty
# for the agent to reason about a rail/component locally; the truth stays the
# graph, this is just the human/LLM-facing slice.
_LIST_CAP = 30

# A power rail label on these boards is `PP<digits/letters>` — PP1V2_S2,
# PP3V3_MAIN, PPVDD_CPU. Anchored on word boundaries so it doesn't snag the
# tail of a longer token. Used by extract_mentions to harvest rails from free
# text and by enrich_registry_from_graph to find rails a description cites.
_RAIL_RE = re.compile(r"\bPP[A-Z0-9_]{2,40}\b")

# A reference designator is 1–3 letters, 1–5 digits, optional trailing letter:
# U8100, C0042, SWV011, Q3A. The digit run is what distinguishes a refdes from
# a plain uppercase English word (THE, USB) — those have no digits and never
# match. Anchored on \b so it doesn't bite into a rail or a longer identifier.
_REFDES_RE = re.compile(r"\b[A-Z]{1,3}[0-9]{1,5}[A-Z]?\b")

# Tokens that look refdes-shaped but are never components. "GND" has no digit so
# it can't match _REFDES_RE anyway, but it is listed for intent; the PP-prefix
# guard (a rail, not a refdes) is the load-bearing exclusion.
#
# The digit-bearing entries below are BUS / PROTOCOL names: `USB2`, `I2C`, `DDR4`,
# `PCIE4`… all match _REFDES_RE (letter-run + digit-run) but are signalling
# standards, never a board component. Without this list they harvest into
# mentions.refdes and produce misleading "component DDR4: ABSENT from schematic"
# noise in the ground-truth report. The list is BEST-EFFORT and need not be
# exhaustive: a stray miss only adds one ABSENT line of noise to the report — it
# never drives drift (drift keys off canonical registry entries, not free-text
# mentions), so over-curating here buys nothing.
_REFDES_STOPWORDS = frozenset({
    "GND",
    # USB / serial / inter-chip buses
    "USB2", "USB3", "USB4", "I2C", "I3C",
    "SPI0", "SPI1", "SPI2", "SPI3",
    "UART0", "UART1", "UART2",
    # memory standards
    "DDR3", "DDR4", "DDR5", "LPDDR4", "LPDDR5",
    # high-speed serial / display / storage
    "PCIE3", "PCIE4", "PCIE5", "HDMI2", "MIPI2", "SATA3",
})


def _dedup(items) -> list[str]:
    """Order-preserving dedup, then cap at _LIST_CAP. One helper so every list
    answer in this module is deduped + capped identically."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
            if len(out) >= _LIST_CAP:
                break
    return out


# ======================================================================
# Task 1 — GraphTruth: read-only existence/query index
# ======================================================================


class GraphTruth:
    """Read-only query index over a compiled `ElectricalGraph`.

    Indexes components / nets / rails once at construction and pre-builds two
    adjacency maps from the typed edges so who_powers / consumers_of / nets_of
    are O(1) lookups, not full-edge scans per call. The graph is never mutated
    and never serialised back out — this object only answers questions.
    """

    def __init__(self, graph: ElectricalGraph) -> None:
        self._graph = graph
        self._components = graph.components
        self._nets = graph.nets
        self._rails = graph.power_rails

        # _powers_by_dst[net] = [src refdes that `powers` this net]. Built from
        # typed_edges with kind == "powers" and dst pointing at a net/rail. This
        # is the producer side that who_powers reads (plus the rail's own
        # source_refdes, deduped).
        self._powers_by_dst: dict[str, list[str]] = {}
        # Both-direction adjacency refdes↔net, so nets_of(refdes) can walk edges
        # in either orientation (an edge may be src=refdes/dst=net OR vice-versa).
        self._edges_by_node: dict[str, list[str]] = {}

        for edge in graph.typed_edges:
            src, dst = edge.src, edge.dst
            if edge.kind == "powers" and (dst in self._nets or dst in self._rails):
                self._powers_by_dst.setdefault(dst, []).append(src)
            # Record both endpoints under each other so nets_of can resolve a
            # component's nets regardless of edge direction.
            self._edges_by_node.setdefault(src, []).append(dst)
            self._edges_by_node.setdefault(dst, []).append(src)

    # ---- existence -----------------------------------------------------

    def has_component(self, refdes: str) -> bool:
        return refdes in self._components

    def has_net(self, label: str) -> bool:
        """True for any net — power rails are a subset of nets, but a rail can
        also be present in power_rails without a plain net entry, so check both."""
        return label in self._nets or label in self._rails

    def has_net_family(self, token: str) -> bool:
        """True when `token` names a rail FAMILY whose concrete members in the
        graph carry a suffix — i.e. some net/rail starts with `token_`.
        Technicians write the family name in prose (`PPBUS`) while the graph
        only attests members (`PPBUS_G3H`). The `_` separator is required so a
        bare character prefix (`PP1V`) does NOT match `PP1V2_S2`."""
        prefix = f"{token}_"
        return any(n.startswith(prefix) for n in self._nets) or any(
            r.startswith(prefix) for r in self._rails
        )

    # ---- info ----------------------------------------------------------

    def component_info(self, refdes: str) -> dict | None:
        """Type/kind (+ role/pages) of a component, or None if it doesn't exist.
        Only the existence-relevant fields are surfaced — never pins/values, so
        this can't be abused as a back-door graph dump."""
        comp = self._components.get(refdes)
        if comp is None:
            return None
        return {
            "type": comp.type,
            "kind": comp.kind,
            "role": comp.role,
            "pages": list(comp.pages),
        }

    def rail_info(self, label: str) -> dict | None:
        """Nominal voltage + source + consumer count for a power rail, or None
        when the label is not a rail (a plain net returns None — use has_net)."""
        rail = self._rails.get(label)
        if rail is None:
            return None
        return {
            "voltage_nominal": rail.voltage_nominal,
            "source_refdes": rail.source_refdes,
            "n_consumers": len(rail.consumers),
        }

    # ---- connectivity --------------------------------------------------

    def who_powers(self, net: str) -> list[str]:
        """Refdes that produce this net: the `powers`-edge sources PLUS the
        rail's declared source_refdes, deduped (the two often agree). Capped."""
        sources = list(self._powers_by_dst.get(net, []))
        rail = self._rails.get(net)
        if rail is not None and rail.source_refdes:
            sources.append(rail.source_refdes)
        return _dedup(sources)

    def consumers_of(self, net: str) -> list[str]:
        """Refdes that consume this rail, straight from the compiled rail's
        consumers list. [] for a non-rail or unknown net. Capped."""
        rail = self._rails.get(net)
        if rail is None:
            return []
        return _dedup(rail.consumers)

    def nets_of(self, refdes: str) -> list[str]:
        """Nets this component touches, resolved from the both-direction edge
        adjacency and filtered to things that are actually nets. Capped."""
        neighbours = self._edges_by_node.get(refdes, [])
        return _dedup(n for n in neighbours if n in self._nets or n in self._rails)

    # ---- search --------------------------------------------------------

    def search(self, term: str) -> list[str]:
        """Case-insensitive substring search across component / net / rail names.

        The agent often types a glob (`PP1V*`) — strip a trailing '*' and lower
        the needle. Dedup preserves first-seen order across the three domains so
        a name living in both nets and rails appears exactly once. Capped."""
        needle = term.rstrip("*").lower()
        if not needle:
            return []
        names = list(self._components) + list(self._nets) + list(self._rails)
        return _dedup(n for n in names if needle in n.lower())


# ======================================================================
# Task 2 — Mentions / extract_mentions / build_ground_truth_report
# ======================================================================


@dataclass
class Mentions:
    """The set of identifiers the writers actually NAMED across the four pack
    artefacts. This is what scopes the ground-truth report — we only ever
    report on things the model already mentioned, never the whole graph."""

    refdes: set[str] = field(default_factory=set)
    rails: set[str] = field(default_factory=set)


def _scan_text(text: str | None, mentions: Mentions) -> None:
    """Harvest rails (PP…) then refdes (letter+digit…) from a free-text blob.

    Order matters: rails are matched first and any PP-prefixed token is claimed
    as a rail, so the refdes pass explicitly skips PP-prefixed tokens (a rail is
    not a component). GND-style stopwords are dropped (they carry no digit and
    wouldn't match _REFDES_RE anyway, but the guard documents intent)."""
    if not text:
        return
    for m in _RAIL_RE.findall(text):
        mentions.rails.add(m)
    for m in _REFDES_RE.findall(text):
        if m.startswith("PP") or m in _REFDES_STOPWORDS:
            continue
        mentions.refdes.add(m)


def extract_mentions(
    registry,
    knowledge_graph,
    rules,
    dictionary,
) -> Mentions:
    """Collect every identifier the writers named, from BOTH structured fields
    and free text across the four pack files.

    Structured names (registry canonical_names, kg node ids) are added directly
    so a clean refdes/rail is never missed; free text (descriptions, symptoms,
    mechanisms, step actions, notes) is regex-scanned to catch identifiers that
    only ever appear in prose — exactly the SWV011-in-a-probe-step case the
    auditor then flags as fabricated."""
    mentions = Mentions()

    # --- Registry: canonical names (structured) + descriptions (free text) ---
    for comp in registry.components:
        name = comp.canonical_name
        if name.startswith("PP"):
            mentions.rails.add(name)
        elif _REFDES_RE.fullmatch(name):
            mentions.refdes.add(name)
        _scan_text(comp.description, mentions)
    for sig in registry.signals:
        # PP-prefixed signal canonical_names are rails; others are scanned as
        # free text (a control/data signal name isn't a refdes per se).
        if sig.canonical_name.startswith("PP"):
            mentions.rails.add(sig.canonical_name)
        else:
            _scan_text(sig.canonical_name, mentions)

    # --- Knowledge graph: node ids + labels ---
    for node in knowledge_graph.nodes:
        # Node ids are N-prefixed (N-U8100, N-NET_PP1V2_S2); strip the prefix(es)
        # then scan, so the embedded refdes/rail is recovered as free text.
        _scan_text(node.id.replace("N-NET_", " ").replace("N-", " "), mentions)
        _scan_text(node.label, mentions)

    # --- Rules: symptoms + cause refdes/mechanism + diagnostic steps ---
    for rule in rules.rules:
        for symptom in rule.symptoms:
            _scan_text(symptom, mentions)
        for cause in rule.likely_causes:
            _scan_text(cause.refdes, mentions)
            _scan_text(cause.mechanism, mentions)
        for step in rule.diagnostic_steps:
            _scan_text(step.action, mentions)
            _scan_text(step.expected, mentions)

    # --- Dictionary: canonical_name + role + notes + failure modes ---
    for entry in dictionary.entries:
        name = entry.canonical_name
        if name.startswith("PP"):
            mentions.rails.add(name)
        elif _REFDES_RE.fullmatch(name):
            mentions.refdes.add(name)
        _scan_text(entry.role, mentions)
        _scan_text(entry.notes, mentions)
        for fm in entry.typical_failure_modes:
            _scan_text(fm, mentions)

    return mentions


def build_ground_truth_report(gt: GraphTruth, mentions: Mentions) -> str:
    """Compile a compact, MENTION-SCOPED existence report.

    One line per mentioned refdes (present + kind, or ABSENT) and one per
    mentioned rail/net (present + voltage/source/consumers, present(net), or
    ABSENT). This is the surface the revise-loop reads instead of the raw graph:
    it ONLY ever names identifiers the writers already mentioned — never
    unmentioned graph content — which is the whole anti-fabrication discipline
    (a present-but-unmentioned cap like C0042 must not appear)."""
    lines = ["Ground-truth existence check (from the compiled electrical graph):"]

    # Sorted for a deterministic, diff-friendly report.
    for refdes in sorted(mentions.refdes):
        info = gt.component_info(refdes)
        if info is None:
            lines.append(f"- component {refdes}: ABSENT from schematic")
        else:
            lines.append(f"- component {refdes}: present ({info['type']})")

    for rail in sorted(mentions.rails):
        if not gt.has_net(rail):
            lines.append(f"- rail/net {rail}: ABSENT from schematic")
            continue
        info = gt.rail_info(rail)
        if info is None:
            # Present as a net but not a power rail.
            lines.append(f"- rail/net {rail}: present (net)")
        else:
            voltage = info["voltage_nominal"]
            volt_str = f"{voltage} V nominal" if voltage is not None else "unknown V"
            lines.append(
                f"- rail/net {rail}: present — {volt_str}, "
                f"sourced by {info['source_refdes']}, {info['n_consumers']} consumers"
            )

    return "\n".join(lines)


# ======================================================================
# Task 3 — QUERY_GRAPH_TOOL + handle_query_graph
# ======================================================================


_OPS = ("component", "net", "rail", "who_powers", "consumers_of", "nets_of", "search")


QUERY_GRAPH_TOOL = {
    "name": "query_graph",
    "description": (
        "Query the compiled electrical graph — the EXISTENCE ground truth for "
        "this board. Call this to VERIFY before you assert: that a refdes or net "
        "exists, a rail's nominal voltage, which component sources a rail, or how "
        "a net is connected. Never claim an identifier is fabricated, or assert a "
        "voltage / source / connection, without checking here first. Ops: "
        "'component' / 'net' / 'rail' (existence + facts), 'who_powers' / "
        "'consumers_of' / 'nets_of' (connectivity), and 'search' which finds "
        "names by case-insensitive substring when you only remember part of one."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "enum": list(_OPS),
                "description": "Which query to run.",
            },
            "name": {
                "type": "string",
                "description": (
                    "The refdes, net/rail label, or (for 'search') the substring "
                    "to look up."
                ),
            },
        },
        "required": ["op", "name"],
    },
}


def handle_query_graph(gt: GraphTruth, tool_input: dict) -> dict:
    """Deterministic dispatcher for the query_graph tool. Returns a JSON-able
    dict per op; an unknown op returns an `{"error": ...}` rather than raising,
    so a model that hallucinates an op gets a corrective message, not a 500."""
    op = tool_input.get("op")
    name = tool_input.get("name", "")

    if op == "component":
        info = gt.component_info(name)
        return {"present": False} if info is None else {"present": True, **info}
    if op == "net":
        return {"present": gt.has_net(name)}
    if op == "rail":
        info = gt.rail_info(name)
        if info is not None:
            return {"present": True, **info}
        # rail_info is None for two distinct cases the model must not conflate:
        # the label doesn't exist at all, OR it exists as a plain net but isn't a
        # power rail. Disambiguate the latter with a note pointing at op=net, so
        # the model doesn't read present:False as "fabricated" and flag a real net.
        if gt.has_net(name):
            return {
                "present": False,
                "note": "exists as a plain net, not a power rail; use op=net",
            }
        return {"present": False}
    if op == "who_powers":
        return {"sources": gt.who_powers(name)}
    if op == "consumers_of":
        return {"consumers": gt.consumers_of(name)}
    if op == "nets_of":
        return {"nets": gt.nets_of(name)}
    if op == "search":
        return {"matches": gt.search(name)}

    return {"error": f"unknown op {op!r}; valid ops: {', '.join(_OPS)}"}


# ======================================================================
# Task 5 — enrich_registry_from_graph (deterministic Phase 2.6)
# ======================================================================


def enrich_registry_from_graph(registry, gt: GraphTruth) -> list[str]:
    """Deterministically close the registry-cites-an-undefined-rail gap.

    Phase 2.6: scan every component description for rail tokens (PP…). When a
    cited rail is real in the graph (`gt.has_net`) but missing from
    `registry.signals`, append a minimal RegistrySignal for it (canonical_name,
    nominal_voltage from the rail, kind=POWER_RAIL). Mutates `registry.signals`
    in place and returns the sorted list of names added.

    This closes the real U8100 / PP1V2_S2 case: the registry's U8100 description
    said it generates PP1V2_S2, but the rail was never added to signals — so the
    drift check had no canonical entry for it and the auditor called the rail
    fabricated. Idempotent: a rail already in signals is skipped. Purely
    deterministic — no LLM, no fabrication (a cited rail absent from the graph,
    PP9V9_FAKE, is NOT added)."""
    # Imported here, not at module top, to avoid an import cycle: api.pipeline
    # .schemas is heavy and may transitively reach back here in future wiring.
    from api.pipeline.schemas import RegistrySignal

    existing = {sig.canonical_name for sig in registry.signals}
    # `added` is the ordered list we return; `added_set` mirrors it for O(1)
    # membership so the per-token dedup check below stays O(1), not O(n) against
    # the growing list (a wide PMIC can cite dozens of rails).
    added: list[str] = []
    added_set: set[str] = set()

    for comp in registry.components:
        for tok in _RAIL_RE.findall(comp.description or ""):
            if tok in existing or tok in added_set:
                continue
            if not gt.has_net(tok):
                continue  # cited but not real → never invent it
            rail = gt.rail_info(tok)
            voltage = rail["voltage_nominal"] if rail else None
            registry.signals.append(
                RegistrySignal(
                    canonical_name=tok,
                    kind="POWER_RAIL",
                    nominal_voltage=voltage,
                )
            )
            added.append(tok)
            added_set.add(tok)

    return sorted(added)
