"""Tool handlers for the boardview panel — invoked by the agent via tool-use."""

from __future__ import annotations

from typing import Any

from api.board.validator import is_valid_refdes, resolve_net, resolve_part, suggest_similar
from api.session.state import SessionState
from api.tools.ws_events import Flip, Focus, Highlight, HighlightNet, ResetView


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
