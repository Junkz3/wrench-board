"""Registry ↔ schematic reconciliation.

When a compiled electrical graph is available, the schematic is the authority
on which components physically exist. The web-sourced registry (Scout) can
carry FICTIONS — refdes that exist in web sources/teardowns but not on this
board's schematic. Left in the registry, the Cartographe trusts them and wires
phantom edges into the knowledge graph (e.g. `U6903 powers PP3V3_G3H` on a
MacBook whose rail is really sourced by R6999).

A component is a fiction iff it is attested by NEITHER:
  - the compiled graph (`graph_truth.has_component`), NOR
  - the raw vision/OCR (`seen_refdes` — every refdes any page actually showed).

The vision/OCR clause is the safety net: a real component the compiler couldn't
*trace* (graph ambiguity) still appears in the page vision, so it is KEPT. Only
refdes seen nowhere on the board are dropped — the triple-negative is what makes
the purge safe against vision recall gaps.
"""
from __future__ import annotations

import glob
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from api.pipeline.graph_truth import _REFDES_RE, GraphTruth
from api.pipeline.schemas import KnowledgeGraph, Registry

# Component types that can GENERATE a power rail. `who_powers` frequently returns
# the nearest IN-LINE element of a rail (a series sense resistor, a protection
# fuse, a rectifier diode) as its "source"; none of those can produce a rail, so
# none may contradict an IC attribution. Only a different source-capable producer
# can. (NOTE: a fuse carries type="fuse" even though its `kind` is "ic" in the
# compiler taxonomy — so the discriminator keys off `type`, never `kind`.)
_SOURCE_CAPABLE_TYPES = frozenset({"ic", "module"})

# kg edge relations that assert a component PRODUCES / controls a rail. `senses`
# (feedback) and `shares_net` (mere connectivity) are not production claims and
# are out of scope for contradiction.
_POWER_RELATIONS = frozenset({"powers", "drives"})


def _attested(name: str, graph_truth: GraphTruth, seen_refdes: set[str]) -> bool:
    """A refdes is real iff the compiled graph has it OR the raw vision/OCR saw
    it. Single source of truth for both the registry purge (input) and the kg
    fiction metric (output)."""
    return graph_truth.has_component(name) or name in seen_refdes


def load_seen_refdes(pack_dir: Path) -> set[str]:
    """Harvest every refdes the raw vision/OCR captured for this pack — the
    per-page SchematicPageGraph JSON and the grounding anchors under
    `schematic_pages/`. Empty set when the dir is absent (web-only pack)."""
    texts = [
        Path(p).read_text()
        for p in glob.glob(str(Path(pack_dir) / "schematic_pages" / "*.json"))
    ]
    return seen_refdes_from_texts(texts)


def seen_refdes_from_texts(texts: Iterable[str | None]) -> set[str]:
    """Harvest refdes-shaped tokens from raw vision/OCR text blobs (page-vision
    JSON, grounding anchors). PP-prefixed tokens are rails, not components, and
    are skipped. Deliberately broad — a refdes seen ANYWHERE on the board keeps
    its registry entry safe from the fiction purge (recall over precision)."""
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        for tok in _REFDES_RE.findall(text):
            if not tok.startswith("PP"):
                seen.add(tok)
    return seen


def find_registry_fictions(
    registry: Registry, graph_truth: GraphTruth, seen_refdes: set[str]
) -> list[str]:
    """Return the registry component canonical_names that the schematic attests
    nowhere (neither compiled graph nor raw vision/OCR) — sorted, deduped."""
    fictions: set[str] = set()
    for comp in registry.components:
        name = comp.canonical_name
        if _attested(name, graph_truth, seen_refdes):
            continue
        fictions.add(name)
    return sorted(fictions)


@dataclass(frozen=True)
class ContradictedEdge:
    """A kg `powers`/`drives` edge whose rail the compiled graph attributes to a
    DIFFERENT source-capable component. `graph_sources` are the active (ic/module)
    producers the graph names — what the Cartographe should have credited."""

    src: str
    rail: str
    relation: str
    graph_sources: tuple[str, ...]


def _is_source_capable(refdes: str, graph_truth: GraphTruth) -> bool:
    """True iff `refdes` is a component the graph types as able to GENERATE a
    rail (ic / module). Unknown refdes → False."""
    info = graph_truth.component_info(refdes)
    return info is not None and info["type"] in _SOURCE_CAPABLE_TYPES


def find_contradicted_edges(
    knowledge_graph: KnowledgeGraph, graph_truth: GraphTruth
) -> list[ContradictedEdge]:
    """Return the kg component→rail power edges the compiled graph CONTRADICTS.

    An edge `U_src {powers|drives} RAIL` is contradicted iff:
      - U_src is itself a source-capable IC/module (a passive src is out of scope
        — the discriminator is only sound for IC over-attribution), AND
      - U_src is NOT among `who_powers(RAIL)` (not a graph-attested producer), AND
      - the graph names at least one DIFFERENT source-capable producer for RAIL.

    The third clause is the false-positive guard proven on real packs: when the
    graph's only "producer" is an in-line passive (fuse F7000, series resistor
    R6999, rectifier diode D8410), the kg's IC attribution is the correct,
    technician-meaningful one and is left untouched. Deterministic, no LLM."""
    contradicted: list[ContradictedEdge] = []
    for edge in knowledge_graph.edges:
        if edge.relation not in _POWER_RELATIONS:
            continue
        if not edge.target_id.startswith("N-NET_"):
            continue  # only component→rail edges
        src = edge.source_id.removeprefix("N-")
        rail = edge.target_id.removeprefix("N-NET_")
        if not _is_source_capable(src, graph_truth):
            continue
        producers = graph_truth.who_powers(rail)
        if src in producers:
            continue  # confirmed by the graph
        active = tuple(p for p in producers if _is_source_capable(p, graph_truth))
        if not active:
            continue  # graph only knows in-line passives → cannot contradict
        contradicted.append(
            ContradictedEdge(
                src=src, rail=rail, relation=edge.relation, graph_sources=active
            )
        )
    return contradicted


def prune_contradicted_edges(
    knowledge_graph: KnowledgeGraph, graph_truth: GraphTruth
) -> tuple[KnowledgeGraph, list[ContradictedEdge]]:
    """Return a NEW knowledge graph with every contradicted power edge removed,
    plus the list of what was removed.

    The deterministic backstop: after the LLM revise-loop has had its chance to
    re-attribute, this guarantees no graph-contradicted `powers`/`drives` edge
    ever ships. Pure edge surgery — nodes are untouched (a node losing one edge
    keeps its others; a genuinely orphaned node is the drift orphan check's job,
    not this one's). Does NOT mutate the input — the orchestrator's best-of
    snapshot must stay byte-stable."""
    contradicted = find_contradicted_edges(knowledge_graph, graph_truth)
    if not contradicted:
        return knowledge_graph, []
    drop = {(c.src, c.rail, c.relation) for c in contradicted}
    kept = [
        e
        for e in knowledge_graph.edges
        if (
            e.source_id.removeprefix("N-"),
            e.target_id.removeprefix("N-NET_"),
            e.relation,
        )
        not in drop
    ]

    # Drop any node the prune STRANDED — one that had an edge before and none
    # after. Otherwise removing `U7800 powers PP1V8_S0` leaves the rail node
    # N-NET_PP1V8_S0 dangling, which just trades contradiction-drift for
    # orphan-drift and keeps the gate blocked. A node already edgeless BEFORE the
    # prune is a PRE-EXISTING orphan (the Cartographe's own drift) and is left
    # alone — we only clean up what this surgery caused.
    edged_before = {e.source_id for e in knowledge_graph.edges} | {
        e.target_id for e in knowledge_graph.edges
    }
    edged_after = {e.source_id for e in kept} | {e.target_id for e in kept}
    stranded = edged_before - edged_after
    kept_nodes = [n for n in knowledge_graph.nodes if n.id not in stranded]

    pruned = knowledge_graph.model_copy(update={"edges": kept, "nodes": kept_nodes})
    return pruned, contradicted


def find_kg_fictions(
    knowledge_graph: KnowledgeGraph, graph_truth: GraphTruth, seen_refdes: set[str]
) -> list[str]:
    """The OUTPUT-side metric: component nodes the Cartographe wired into the
    knowledge graph that the schematic attests nowhere. This is what an effective
    registry purge should drive toward zero."""
    fictions: set[str] = set()
    for node in knowledge_graph.nodes:
        if node.kind != "component":
            continue
        refdes = node.id.removeprefix("N-")
        if not _attested(refdes, graph_truth, seen_refdes):
            fictions.add(refdes)
    return sorted(fictions)
