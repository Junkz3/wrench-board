# SPDX-License-Identifier: Apache-2.0
"""Dispatch router for the bv_* tool family.

Maps the public names (exposed to Claude in the manifest) to the existing
handlers in api/tools/boardview.py. Each handler returns a dict that may
contain {ok, summary, event, reason, suggestions}.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from api.session.state import SessionState
from api.tools import boardview as bv

logger = logging.getLogger("microsolder.agent.dispatch_bv")


BV_DISPATCH: dict[str, Callable[..., dict[str, Any]]] = {
    "bv_highlight":        bv.highlight_component,
    "bv_focus":            bv.focus_component,
    "bv_reset_view":       bv.reset_view,
    "bv_flip":             bv.flip_board,
    "bv_annotate":         bv.annotate,
    "bv_dim_unrelated":    bv.dim_unrelated,
    "bv_highlight_net":    bv.highlight_net,
    "bv_show_pin":         bv.show_pin,
    "bv_draw_arrow":       bv.draw_arrow,
    "bv_measure":          bv.measure_distance,
    "bv_filter_by_type":   bv.filter_by_type,
    "bv_layer_visibility": bv.layer_visibility,
    "bv_scene":            bv.compose_scene,
}


def dispatch_bv(session: SessionState, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Route a bv_* tool call to its handler. Traps any exception.

    Returns {ok: false, reason: "unknown-tool"} if the name isn't in BV_DISPATCH.
    Returns {ok: false, reason: "handler-exception", error: str(exc)} if the
    handler raises (e.g. malformed payload).
    """
    handler = BV_DISPATCH.get(name)
    if handler is None:
        return {"ok": False, "reason": "unknown-tool"}
    try:
        return handler(session, **payload)
    except Exception as exc:  # noqa: BLE001 — intentional catch-all at dispatch boundary
        logger.exception("bv_* handler %s raised", name)
        return {"ok": False, "reason": "handler-exception", "error": str(exc)}
