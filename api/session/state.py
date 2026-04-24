"""Per-session state for the boardview panel."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from api.board.model import Board
from api.board.parser.base import parser_for

logger = logging.getLogger("microsolder.session")

Side = Literal["top", "bottom"]

# Extension priority: richer formats first. If both exist for a slug,
# .kicad_pcb wins.
_BOARD_EXT_PRIORITY = (".kicad_pcb", ".brd")


def _board_assets_root() -> Path:
    """Root of board_assets/. Overridable via MICROSOLDER_BOARD_ASSETS env for tests."""
    override = os.environ.get("MICROSOLDER_BOARD_ASSETS")
    if override:
        return Path(override)
    # api/session/state.py → ../../board_assets
    return Path(__file__).resolve().parents[2] / "board_assets"


@dataclass
class SessionState:
    board: Board | None = None
    schematic: Any = None  # Hook for future sch_* tool family; not populated here.
    layer: Side = "top"
    highlights: set[str] = field(default_factory=set)
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

    def invalidate_pack_cache(self, device_slug: str) -> None:
        """Drop the cached pack for `device_slug`. Called after mb_expand_knowledge."""
        self.pack_cache.pop(device_slug, None)

    def set_board(self, board: Board) -> None:
        """Load a new board and reset all view state."""
        self.board = board
        self.layer = "top"
        self.highlights = set()
        self.net_highlight = None
        self.annotations = {}
        self.arrows = {}
        self.dim_unrelated = False
        self.filter_prefix = None
        self.layer_visibility = {"top": True, "bottom": True}

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
