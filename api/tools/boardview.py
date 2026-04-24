# SPDX-License-Identifier: Apache-2.0
"""Tool handlers for the boardview panel — invoked by the agent via tool-use."""

from __future__ import annotations

import uuid
from typing import Any

from api.board.validator import is_valid_refdes, resolve_net, resolve_part, resolve_pin, suggest_similar
from api.session.state import SessionState
from api.tools.ws_events import Annotate as AnnotateEvent, DimUnrelated, DrawArrow, Filter, Flip, Focus, Highlight, HighlightNet, LayerVisibility, Measure, ResetView, ShowPin


def _no_board(session: SessionState) -> dict[str, Any] | None:
    if session.board is None:
        return {"ok": False, "reason": "no-board-loaded", "suggestions": []}
    return None


def _unknown_refdes(session: SessionState, refdes: str) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": "unknown-refdes",
        "suggestions": suggest_similar(session.board, refdes, k=3),
    }


def highlight_component(
    session: SessionState,
    *,
    refdes: str | list[str],
    color: str = "accent",
    additive: bool = False,
) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err

    targets = [refdes] if isinstance(refdes, str) else list(refdes)
    for r in targets:
        if not is_valid_refdes(session.board, r):
            return _unknown_refdes(session, r)

    if not additive:
        session.highlights = set()
    session.highlights.update(targets)

    event = Highlight(refdes=targets, color=color, additive=additive)
    summary = f"Highlighted {', '.join(targets)}."
    return {"ok": True, "summary": summary, "event": event}


def focus_component(session: SessionState, *, refdes: str, zoom: float = 2.5) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    part = resolve_part(session.board, refdes)
    if part is None:
        return _unknown_refdes(session, refdes)

    auto_flipped = False
    target_side = "top" if part.layer.value & 1 else "bottom"
    if session.layer != target_side:
        session.layer = target_side
        auto_flipped = True

    session.highlights = {refdes}

    bbox = ((part.bbox[0].x, part.bbox[0].y), (part.bbox[1].x, part.bbox[1].y))
    event = Focus(refdes=refdes, bbox=bbox, zoom=zoom, auto_flipped=auto_flipped)
    summary = f"Focused on {refdes} ({target_side})."
    return {"ok": True, "summary": summary, "event": event}


def reset_view(session: SessionState) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    session.highlights = set()
    session.net_highlight = None
    session.dim_unrelated = False
    session.annotations = {}
    session.arrows = {}
    session.filter_prefix = None
    return {"ok": True, "summary": "View reset.", "event": ResetView()}


def highlight_net(session: SessionState, *, net: str) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    n = resolve_net(session.board, net)
    if n is None:
        return {"ok": False, "reason": "unknown-net", "suggestions": []}
    session.net_highlight = net
    event = HighlightNet(net=net, pin_refs=n.pin_refs)
    summary = f"Highlighted net {net} ({len(n.pin_refs)} pins)."
    return {"ok": True, "summary": summary, "event": event}


def flip_board(session: SessionState, *, preserve_cursor: bool = False) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    session.layer = "bottom" if session.layer == "top" else "top"
    event = Flip(new_side=session.layer, preserve_cursor=preserve_cursor)
    return {"ok": True, "summary": f"Flipped to {session.layer}.", "event": event}


def annotate(session: SessionState, *, refdes: str, label: str) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    if not is_valid_refdes(session.board, refdes):
        return _unknown_refdes(session, refdes)
    ann_id = f"ann-{uuid.uuid4().hex[:8]}"
    session.annotations[ann_id] = {"refdes": refdes, "label": label}
    event = AnnotateEvent(refdes=refdes, label=label, id=ann_id)
    return {"ok": True, "summary": f"Annotated {refdes}.", "event": event}


def filter_by_type(session: SessionState, *, prefix: str) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    session.filter_prefix = prefix if prefix else None
    event = Filter(prefix=session.filter_prefix)
    return {"ok": True, "summary": f"Filter: {prefix or 'none'}.", "event": event}


def _part_center(part) -> tuple[int, int]:
    (a, b) = part.bbox
    return ((a.x + b.x) // 2, (a.y + b.y) // 2)


def draw_arrow(session: SessionState, *, from_refdes: str, to_refdes: str) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    a = resolve_part(session.board, from_refdes)
    b = resolve_part(session.board, to_refdes)
    if a is None:
        return _unknown_refdes(session, from_refdes)
    if b is None:
        return _unknown_refdes(session, to_refdes)
    arr_id = f"arr-{uuid.uuid4().hex[:8]}"
    frm = _part_center(a)
    to = _part_center(b)
    session.arrows[arr_id] = {"from": list(frm), "to": list(to)}
    event = DrawArrow(**{"from": frm, "to": to, "id": arr_id})
    return {
        "ok": True,
        "summary": f"Drew arrow from {from_refdes} to {to_refdes}.",
        "event": event,
    }


def measure_distance(session: SessionState, *, refdes_a: str, refdes_b: str) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    pa = resolve_part(session.board, refdes_a)
    pb = resolve_part(session.board, refdes_b)
    if pa is None:
        return _unknown_refdes(session, refdes_a)
    if pb is None:
        return _unknown_refdes(session, refdes_b)
    (ax, ay) = _part_center(pa)
    (bx, by) = _part_center(pb)
    dx_mils = ax - bx
    dy_mils = ay - by
    # 1 mil = 0.0254 mm
    distance_mm = round(((dx_mils**2 + dy_mils**2) ** 0.5) * 0.0254, 2)
    event = Measure(from_refdes=refdes_a, to_refdes=refdes_b, distance_mm=distance_mm)
    return {
        "ok": True,
        "summary": f"{refdes_a} ↔ {refdes_b}: {distance_mm} mm.",
        "event": event,
    }


def show_pin(session: SessionState, *, refdes: str, pin: int) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    if not is_valid_refdes(session.board, refdes):
        return _unknown_refdes(session, refdes)
    p = resolve_pin(session.board, refdes, pin)
    if p is None:
        return {"ok": False, "reason": "unknown-pin", "suggestions": []}
    event = ShowPin(refdes=refdes, pin=pin, pos=(p.pos.x, p.pos.y))
    return {"ok": True, "summary": f"{refdes}.{pin} at ({p.pos.x}, {p.pos.y}).", "event": event}


def dim_unrelated(session: SessionState) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    session.dim_unrelated = True
    return {"ok": True, "summary": "Dimmed unrelated components.", "event": DimUnrelated()}


def layer_visibility(session: SessionState, *, layer: str, visible: bool) -> dict[str, Any]:
    err = _no_board(session)
    if err:
        return err
    if layer not in ("top", "bottom"):
        return {"ok": False, "reason": "invalid-layer", "suggestions": ["top", "bottom"]}
    session.layer_visibility[layer] = visible  # type: ignore[index]
    event = LayerVisibility(layer=layer, visible=visible)  # type: ignore[arg-type]
    return {"ok": True, "summary": f"Layer {layer} visible={visible}.", "event": event}
