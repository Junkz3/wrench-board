"""Per-repair persistence + replay of the boardview overlay state.

The chat replay (managed runtime) re-emits agent text + tool_use events
from MA's event store, but it never re-runs `dispatch_bv` — so the
visual side-effects (highlights, focus, annotations, dim, layer flip)
that those tool calls produced are gone the moment the WS reconnects.
The tech reopens the panel and the board is bare even though the chat
shows "I highlighted U7 for you".

Fix: snapshot the SessionState's overlay fields to
`memory/{slug}/repairs/{repair_id}/board_state.json` after every bv_*
mutation, and on WS reopen replay the snapshot as a sequence of
`boardview.*` events to brd_viewer. End result: refresh the page and
the board shows up exactly as the agent left it.

State is keyed per-repair, not per-conv: the device is the same across
conversations of a repair, and the tech's mental model is "what I see
on the board" — not "what conv I'm currently in".
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from api.session.state import SessionState

logger = logging.getLogger("wrench_board.agent.board_state")

_FILENAME = "board_state.json"


def _state_path(
    memory_root: Path,
    device_slug: str,
    repair_id: str,
    conv_id: str | None,
) -> Path:
    """Path for the board overlay snapshot.

    Conv-scoped when `conv_id` is given (the desirable shape: each chat
    thread gets its own canvas, so opening a fresh conv shows a clean
    board even though another conv on the same repair has annotations
    and arrows). Falls back to the repair-root location when no conv id
    is provided — preserves backward compat with snapshots written
    before the per-conv refactor.
    """
    base = memory_root / device_slug / "repairs" / repair_id
    if conv_id:
        return base / "conversations" / conv_id / _FILENAME
    return base / _FILENAME


def save_board_state(
    *,
    memory_root: Path,
    device_slug: str,
    repair_id: str | None,
    session: SessionState,
    conv_id: str | None = None,
) -> None:
    """Snapshot the session's overlay state to disk. Best-effort — never
    blocks the WS path on a write failure (logs at warning).

    Anonymous sessions (no repair_id) skip silently — without a repair
    there's no place to scope the snapshot.
    """
    if not repair_id:
        return
    snapshot = session.serialize_view()
    # Cheap empty-state shortcut — no point persisting an empty overlay
    # over an empty file. Also avoids a noisy ENOENT->mkdir->write cycle
    # on every WS open of a fresh repair where the agent hasn't called
    # any bv_* yet.
    if (
        snapshot["layer"] == "top"
        and not snapshot["highlights"]
        and snapshot["net_highlight"] is None
        and not snapshot["annotations"]
        and not snapshot["arrows"]
        and not snapshot["dim_unrelated"]
        and snapshot["filter_prefix"] is None
        and snapshot["layer_visibility"] == {"top": True, "bottom": True}
    ):
        return
    path = _state_path(memory_root, device_slug, repair_id, conv_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning(
            "[BoardState] save failed for repair=%s/%s conv=%s: %s",
            device_slug, repair_id, conv_id, exc,
        )


def load_board_state(
    *,
    memory_root: Path,
    device_slug: str,
    repair_id: str | None,
    conv_id: str | None = None,
) -> dict[str, Any] | None:
    """Read a previously-saved snapshot for this conv, or None.

    Strictly per-conv when `conv_id` is given (no legacy fallback): a
    fresh conversation MUST land on a clean board, even if a sibling
    conv on the same repair has a populated overlay. Without this, the
    "+ Nouvelle conversation" path inherited annotations/arrows from
    whichever conv last touched the board.

    The repair-root legacy path is only consulted when no conv_id is
    supplied at all (anonymous WS, mostly tests).
    """
    if not repair_id:
        return None
    path = _state_path(memory_root, device_slug, repair_id, conv_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "[BoardState] load failed for %s: %s", path, exc,
        )
        return None


async def replay_board_state_to_ws(ws: Any, snapshot: dict[str, Any]) -> int:
    """Push the boardview events that reconstruct `snapshot` to the WS.

    Order matters: layer/visibility/filter first (skeleton), then
    highlights, then annotations/arrows, then dim_unrelated last (it
    operates on the just-set highlights). Returns the count of events
    sent so the caller can log / decide whether to surface anything.
    """
    if not isinstance(snapshot, dict):
        return 0
    sent = 0

    # Layer flip — only when it diverges from the default top.
    layer = snapshot.get("layer")
    if layer == "bottom":
        await ws.send_json({
            "type": "boardview.flip",
            "new_side": "bottom",
            "preserve_cursor": False,
        })
        sent += 1

    # Layer visibility — only push when one side is hidden.
    lv = snapshot.get("layer_visibility") or {}
    for side in ("top", "bottom"):
        visible = lv.get(side, True)
        if visible is False:
            await ws.send_json({
                "type": "boardview.layer_visibility",
                "layer": side,
                "visible": False,
            })
            sent += 1

    # Filter by type — text-only on the brd renderer.
    filter_prefix = snapshot.get("filter_prefix")
    if filter_prefix:
        await ws.send_json({
            "type": "boardview.filter",
            "prefix": filter_prefix,
        })
        sent += 1

    # Component highlights — single batched event so the renderer applies
    # them in one pass (additive=False = replace existing on the client).
    # Color carried through from the saved overlay so warn/amber tags survive
    # the reload (the previous flat-accent version repainted everything cyan
    # and silently dropped the agent's amber "risky part" semantics).
    highlights = snapshot.get("highlights") or []
    color = snapshot.get("highlight_color") or "accent"
    if color not in ("accent", "warn", "mute"):
        color = "accent"
    if highlights:
        await ws.send_json({
            "type": "boardview.highlight",
            "refdes": list(highlights),
            "color": color,
            "additive": False,
        })
        sent += 1

    # Focus — replay the pan/zoom centred on the last bv_focus target.
    # `boardview.focus` carries bbox + zoom; the renderer will pan and
    # apply its highlight pulse animation. Replayed AFTER the bare
    # highlight so focus's single-target highlight doesn't get clobbered
    # by the broader set above.
    last_focused = snapshot.get("last_focused")
    last_bbox = snapshot.get("last_focused_bbox")
    last_zoom = snapshot.get("last_focused_zoom") or 1.4
    if last_focused and isinstance(last_bbox, list) and len(last_bbox) == 2:
        await ws.send_json({
            "type": "boardview.focus",
            "refdes": last_focused,
            "bbox": last_bbox,
            "zoom": last_zoom,
            "auto_flipped": False,  # the layer flip event was already emitted above
        })
        sent += 1

    # Net highlight — only the name; pin_refs aren't snapshotted (we'd
    # need the parsed board to recompute). The renderer can still tag
    # the net label even without pin overlays.
    net = snapshot.get("net_highlight")
    if net:
        await ws.send_json({
            "type": "boardview.highlight_net",
            "net": net,
            "pin_refs": [],
        })
        sent += 1

    # Annotations + arrows — re-emit individually so the renderer's
    # per-id store rebuilds with the same ids the agent originally used
    # (lets bv_* tool replays line up if the agent later removes them).
    for ann_id, ann in (snapshot.get("annotations") or {}).items():
        if not isinstance(ann, dict):
            continue
        await ws.send_json({
            "type": "boardview.annotate",
            "id": ann_id,
            "refdes": ann.get("refdes", ""),
            "label": ann.get("label", ""),
        })
        sent += 1

    for arrow_id, arrow in (snapshot.get("arrows") or {}).items():
        if not isinstance(arrow, dict):
            continue
        from_pt = arrow.get("from") or arrow.get("from_")
        to_pt = arrow.get("to")
        if not from_pt or not to_pt:
            continue
        await ws.send_json({
            "type": "boardview.draw_arrow",
            "id": arrow_id,
            "from": list(from_pt),
            "to": list(to_pt),
        })
        sent += 1

    # Dim unrelated — must come AFTER highlights so the dim mask knows
    # what's "related" (the just-set highlight set on the renderer side).
    if snapshot.get("dim_unrelated"):
        await ws.send_json({"type": "boardview.dim_unrelated"})
        sent += 1

    return sent
