"""Deterministic merger — list[SchematicPageGraph] → SchematicGraph.

Stitches per-page extractions into a single flat catalogue:

- Refdes are unique per board; duplicates across pages are fused (pins unioned,
  value field is the richer of the candidates, populated=False is sticky).
- Nets sharing a label across pages collapse to one NetNode; unlabelled wires
  stay page-local under a synthetic '__local__{page}__{local_id}' key so every
  pin ends up on exactly one net.
- Cross-page references stitch by label: any label appearing on a CrossPageRef
  but never as a real net or counter-ref on another page becomes an ambiguity.
- typed_edges and designer_notes are concatenated with exact-duplicate removal.

No LLM calls. Fully testable from hand-made SchematicPageGraph fixtures.
"""

from __future__ import annotations

from collections import OrderedDict

from api.pipeline.schematic.schemas import (
    Ambiguity,
    ComponentNode,
    ComponentValue,
    DesignerNote,
    NetNode,
    PageNet,
    PageNode,
    PagePin,
    SchematicGraph,
    SchematicPageGraph,
    TypedEdge,
)


def merge_pages(
    pages: list[SchematicPageGraph],
    *,
    device_slug: str,
    source_pdf: str,
) -> SchematicGraph:
    components: dict[str, ComponentNode] = {}
    nets: dict[str, NetNode] = {}
    typed_edges: list[TypedEdge] = []
    edge_keys: set[tuple[str, str, str]] = set()
    designer_notes: list[DesignerNote] = []
    note_keys: set[tuple[str, int, str | None, str | None]] = set()
    ambiguities: list[Ambiguity] = []
    hierarchy: OrderedDict[str, None] = OrderedDict()

    for page in pages:
        if page.sheet_path:
            hierarchy.setdefault(page.sheet_path, None)

        for node in page.nodes:
            _merge_component(components, ambiguities, node)

        for net in page.nets:
            _merge_net(nets, net)

        for edge in page.typed_edges:
            key = (edge.src, edge.dst, edge.kind)
            if key not in edge_keys:
                edge_keys.add(key)
                typed_edges.append(edge)

        for note in page.designer_notes:
            key = (note.text, note.page, note.attached_to_refdes, note.attached_to_net)
            if key not in note_keys:
                note_keys.add(key)
                designer_notes.append(note)

        ambiguities.extend(page.ambiguities)

    ambiguities.extend(_detect_orphan_cross_page_refs(pages, nets))

    return SchematicGraph(
        device_slug=device_slug,
        source_pdf=source_pdf,
        page_count=len(pages),
        hierarchy=list(hierarchy),
        components=components,
        nets=nets,
        typed_edges=typed_edges,
        designer_notes=designer_notes,
        ambiguities=ambiguities,
    )


def _merge_component(
    components: dict[str, ComponentNode],
    ambiguities: list[Ambiguity],
    node: PageNode,
) -> None:
    existing = components.get(node.refdes)
    if existing is None:
        components[node.refdes] = ComponentNode(
            refdes=node.refdes,
            type=node.type,
            value=node.value.model_copy(deep=True) if node.value else None,
            pages=[node.page],
            pins=[p.model_copy(deep=True) for p in node.pins],
            populated=node.populated,
        )
        return

    if existing.type != node.type:
        ambiguities.append(
            Ambiguity(
                description=(
                    f"Component {node.refdes} typed as '{existing.type}' on earlier "
                    f"pages but as '{node.type}' on page {node.page}"
                ),
                page=node.page,
                related_refdes=[node.refdes],
            )
        )

    if node.page not in existing.pages:
        existing.pages.append(node.page)

    _union_pins(existing.pins, node.pins)
    existing.value = _richer_value(existing.value, node.value)

    # NOSTUFF / DNP on any page is sticky.
    if not node.populated:
        existing.populated = False


def _union_pins(existing: list[PagePin], incoming: list[PagePin]) -> None:
    by_number = {p.number: p for p in existing}
    for pin in incoming:
        current = by_number.get(pin.number)
        if current is None:
            existing.append(pin.model_copy(deep=True))
            by_number[pin.number] = existing[-1]
            continue
        if current.name is None and pin.name is not None:
            current.name = pin.name
        if current.net_label is None and pin.net_label is not None:
            current.net_label = pin.net_label
        if current.role == "unknown" and pin.role != "unknown":
            current.role = pin.role


def _richer_value(
    a: ComponentValue | None, b: ComponentValue | None
) -> ComponentValue | None:
    if a is None:
        return b.model_copy(deep=True) if b else None
    if b is None:
        return a

    def score(v: ComponentValue) -> int:
        fields = (
            v.primary,
            v.package,
            v.mpn,
            v.mpn_alternate,
            v.tolerance,
            v.voltage_rating,
            v.temp_coef,
            v.description,
        )
        return sum(1 for f in fields if f)

    return a if score(a) >= score(b) else b.model_copy(deep=True)


def _merge_net(nets: dict[str, NetNode], net: PageNet) -> None:
    key = net.label if net.label else f"__local__{net.page}__{net.local_id}"
    existing = nets.get(key)
    if existing is None:
        nets[key] = NetNode(
            label=key,
            is_power=net.is_power,
            is_global=net.is_global,
            pages=[net.page],
            connects=list(net.connects),
        )
        return
    if net.page not in existing.pages:
        existing.pages.append(net.page)
    for conn in net.connects:
        if conn not in existing.connects:
            existing.connects.append(conn)
    existing.is_power = existing.is_power or net.is_power
    existing.is_global = existing.is_global or net.is_global


def _detect_orphan_cross_page_refs(
    pages: list[SchematicPageGraph], nets: dict[str, NetNode]
) -> list[Ambiguity]:
    orphans: list[Ambiguity] = []

    # Build an index of every label that appears on any cross-page ref, so a
    # ref with no matching net can still be "stitched" against another ref.
    ref_label_pages: dict[str, set[int]] = {}
    for page in pages:
        for ref in page.cross_page_refs:
            if ref.label is None:
                continue
            ref_label_pages.setdefault(ref.label, set()).add(page.page)

    for page in pages:
        for ref in page.cross_page_refs:
            if ref.label is None:
                orphans.append(
                    Ambiguity(
                        description=(
                            "Cross-page connector with unreadable label on page "
                            f"{page.page}"
                            + (f" at {ref.at_pin}" if ref.at_pin else "")
                        ),
                        page=page.page,
                    )
                )
                continue
            if ref.label in nets:
                continue
            # At least one other page carries the same label on a ref: stitched.
            other_pages = ref_label_pages.get(ref.label, set()) - {page.page}
            if other_pages:
                continue
            orphans.append(
                Ambiguity(
                    description=(
                        f"Cross-page ref '{ref.label}' on page {page.page} has no "
                        "matching net or counter-ref on other pages"
                    ),
                    page=page.page,
                    related_nets=[ref.label],
                )
            )
    return orphans
