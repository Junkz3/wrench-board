# SPDX-License-Identifier: Apache-2.0
"""Per-session state for the boardview panel."""

from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal

from api.board.model import Board
from api.board.parser.base import parser_for

logger = logging.getLogger("wrench_board.session")

Side = Literal["top", "bottom"]

# Extension priority: richer formats first. If both exist for a slug,
# .kicad_pcb wins.
_BOARD_EXT_PRIORITY = (".kicad_pcb", ".brd")


def _board_assets_root() -> Path:
    """Root of board_assets/. Overridable via WRENCH_BOARD_BOARD_ASSETS env for tests."""
    override = os.environ.get("WRENCH_BOARD_BOARD_ASSETS")
    if override:
        return Path(override)
    # api/session/state.py → ../../board_assets
    return Path(__file__).resolve().parents[2] / "board_assets"


@dataclass
class SessionState:
    board: Board | None = None
    layer: Side = "top"
    highlights: set[str] = field(default_factory=set)
    # Color the last bv_highlight / bv_focus call asked for. Without this,
    # restoring the overlay always paints accent/cyan even when the agent
    # originally tagged a part as warn/amber — visually misleading.
    highlight_color: Literal["accent", "warn", "mute"] = "accent"
    # Last component the agent put under bv_focus (centred + pulsed). Tracked
    # separately from `highlights` because focus has visual side-effects
    # (pan/zoom + pulse) that a plain highlight doesn't replay.
    last_focused: str | None = None
    last_focused_bbox: tuple[tuple[int, int], tuple[int, int]] | None = None
    last_focused_zoom: float = 1.4
    net_highlight: str | None = None
    annotations: dict[str, dict[str, Any]] = field(default_factory=dict)
    arrows: dict[str, dict[str, Any]] = field(default_factory=dict)
    dim_unrelated: bool = False
    filter_prefix: str | None = None
    layer_visibility: dict[Side, bool] = field(
        default_factory=lambda: {"top": True, "bottom": True}
    )
    # R1: pack cache — keyed by device_slug, storing (max_mtime, pack_dict).
    pack_cache: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)
    # R2: per-session LRU for mb_get_component results, keyed by (device_slug, refdes).
    # Size cap kept small — sessions ask about the same ~dozen refdes repeatedly.
    component_cache: OrderedDict[tuple[str, str], dict[str, Any]] = field(
        default_factory=OrderedDict
    )

    COMPONENT_CACHE_MAX: ClassVar[int] = 64
    # R3: profile snapshot cache — mtime-checked on every lookup.
    profile_cache: tuple[float, dict[str, Any]] | None = None
    # R4: electrical_graph.json cache (+ analyzer & net-domain overlays), keyed
    # by device_slug. No explicit invalidator — the pipeline always rewrites the
    # file, so mtime comparison catches every realistic mutation.
    schematic_graph_cache: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)
    # Files+Vision : capability flag from the frontend's client.capabilities
    # frame at WS open. Default False — `cam_capture` is gated off until set.
    has_camera: bool = False
    # Files+Vision Flow B : per-request capture Futures, keyed by request_id.
    # Resolved when the frontend posts back client.capture_response.
    pending_captures: dict[str, asyncio.Future] = field(default_factory=dict)

    def invalidate_pack_cache(self, device_slug: str) -> None:
        """Drop the cached pack AND all derived component results for `device_slug`.

        Called after `mb_expand_knowledge` mutates the on-disk pack: both the
        pack JSON cache (pack_cache) and the per-refdes summary cache
        (component_cache, whose values embed registry/dictionary fields pulled
        from the pack) must be purged to avoid serving stale lookups.
        """
        self.pack_cache.pop(device_slug, None)
        stale_keys = [k for k in self.component_cache if k[0] == device_slug]
        for k in stale_keys:
            del self.component_cache[k]

    def set_board(self, board: Board) -> None:
        """Load a new board and reset all view state."""
        self.board = board
        self.layer = "top"
        self.highlights = set()
        self.highlight_color = "accent"
        self.last_focused = None
        self.last_focused_bbox = None
        self.last_focused_zoom = 1.4
        self.net_highlight = None
        self.annotations = {}
        self.arrows = {}
        self.dim_unrelated = False
        self.filter_prefix = None
        self.layer_visibility = {"top": True, "bottom": True}
        self.component_cache.clear()

    def serialize_view(self) -> dict[str, Any]:
        """Plain-data snapshot of the boardview overlay state.

        Caches (pack_cache, component_cache, schematic_graph_cache) and the
        Board object itself are deliberately excluded — they're either
        rebuilt from disk on demand or loaded from board_assets via
        from_device(). Only the per-session UI overlay state survives, so
        a reload reconstructs what the tech was looking at without
        re-pulling 60 kB of board geometry.
        """
        return {
            "layer": self.layer,
            "highlights": sorted(self.highlights),
            "highlight_color": self.highlight_color,
            "last_focused": self.last_focused,
            "last_focused_bbox": (
                [list(self.last_focused_bbox[0]), list(self.last_focused_bbox[1])]
                if self.last_focused_bbox else None
            ),
            "last_focused_zoom": self.last_focused_zoom,
            "net_highlight": self.net_highlight,
            "annotations": {k: dict(v) for k, v in self.annotations.items()},
            "arrows": {k: dict(v) for k, v in self.arrows.items()},
            "dim_unrelated": self.dim_unrelated,
            "filter_prefix": self.filter_prefix,
            "layer_visibility": dict(self.layer_visibility),
        }

    def restore_view(self, snapshot: dict[str, Any]) -> None:
        """Inverse of serialize_view. Defensive against missing keys —
        older on-disk snapshots that pre-date a field should still load."""
        if not isinstance(snapshot, dict):
            return
        layer = snapshot.get("layer")
        if layer in ("top", "bottom"):
            self.layer = layer  # type: ignore[assignment]
        highlights = snapshot.get("highlights")
        if isinstance(highlights, list):
            self.highlights = {h for h in highlights if isinstance(h, str)}
        color = snapshot.get("highlight_color")
        if color in ("accent", "warn", "mute"):
            self.highlight_color = color  # type: ignore[assignment]
        focused = snapshot.get("last_focused")
        if isinstance(focused, str) or focused is None:
            self.last_focused = focused
        bbox = snapshot.get("last_focused_bbox")
        if (
            isinstance(bbox, list) and len(bbox) == 2
            and all(isinstance(p, list) and len(p) == 2 for p in bbox)
            and all(isinstance(c, (int, float)) for p in bbox for c in p)
        ):
            self.last_focused_bbox = (
                (int(bbox[0][0]), int(bbox[0][1])),
                (int(bbox[1][0]), int(bbox[1][1])),
            )
        zoom = snapshot.get("last_focused_zoom")
        if isinstance(zoom, (int, float)):
            self.last_focused_zoom = float(zoom)
        net_highlight = snapshot.get("net_highlight")
        if isinstance(net_highlight, str) or net_highlight is None:
            self.net_highlight = net_highlight
        annotations = snapshot.get("annotations")
        if isinstance(annotations, dict):
            self.annotations = {
                k: dict(v) for k, v in annotations.items() if isinstance(v, dict)
            }
        arrows = snapshot.get("arrows")
        if isinstance(arrows, dict):
            self.arrows = {
                k: dict(v) for k, v in arrows.items() if isinstance(v, dict)
            }
        if isinstance(snapshot.get("dim_unrelated"), bool):
            self.dim_unrelated = snapshot["dim_unrelated"]
        filter_prefix = snapshot.get("filter_prefix")
        if isinstance(filter_prefix, str) or filter_prefix is None:
            self.filter_prefix = filter_prefix
        lv = snapshot.get("layer_visibility")
        if isinstance(lv, dict):
            self.layer_visibility = {
                k: bool(v) for k, v in lv.items() if k in ("top", "bottom")
            } or {"top": True, "bottom": True}

    @classmethod
    def from_device(cls, device_slug: str) -> SessionState:
        """Build a session for a device, auto-loading the board if available.

        Priority: .kicad_pcb first, then .brd. If no file is found or parsing
        fails, returns an empty SessionState — the agent will simply not get
        the `bv_*` tool family in its manifest.
        """
        root = _board_assets_root()
        for ext in _BOARD_EXT_PRIORITY:
            candidate = root / f"{device_slug}{ext}"
            if not candidate.exists():
                continue
            try:
                parser = parser_for(candidate)
                board = parser.parse_file(candidate)
                session = cls()
                session.set_board(board)
                return session
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "board load failed for %s (%s): %s", device_slug, candidate.name, exc
                )
                return cls()  # fall through with empty session
        return cls()
