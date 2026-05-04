"""WebSocket event envelopes for the boardview panel (backend → frontend).

All events have a `type` field of the form "boardview.<verb>".
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _BVEvent(BaseModel):
    """Base class with a fixed `type` field set per subclass."""

    type: str


class BoardLoaded(_BVEvent):
    type: Literal["boardview.board_loaded"] = "boardview.board_loaded"
    board_id: str
    file_hash: str
    parts_count: int
    outline: list[tuple[int, int]]
    parts: list[dict[str, Any]]
    pins: list[dict[str, Any]]
    nets: list[dict[str, Any]]


class Highlight(_BVEvent):
    type: Literal["boardview.highlight"] = "boardview.highlight"
    refdes: list[str]
    color: Literal["accent", "warn", "mute"] = "accent"
    additive: bool = False


class HighlightNet(_BVEvent):
    type: Literal["boardview.highlight_net"] = "boardview.highlight_net"
    net: str
    pin_refs: list[int]


class Focus(_BVEvent):
    type: Literal["boardview.focus"] = "boardview.focus"
    refdes: str
    bbox: tuple[tuple[int, int], tuple[int, int]]
    zoom: float
    auto_flipped: bool = False


class Flip(_BVEvent):
    type: Literal["boardview.flip"] = "boardview.flip"
    new_side: Literal["top", "bottom"]
    preserve_cursor: bool = False


class Annotate(_BVEvent):
    type: Literal["boardview.annotate"] = "boardview.annotate"
    refdes: str
    label: str
    id: str


class ResetView(_BVEvent):
    type: Literal["boardview.reset_view"] = "boardview.reset_view"


class DimUnrelated(_BVEvent):
    type: Literal["boardview.dim_unrelated"] = "boardview.dim_unrelated"


class LayerVisibility(_BVEvent):
    type: Literal["boardview.layer_visibility"] = "boardview.layer_visibility"
    layer: Literal["top", "bottom"]
    visible: bool


class Filter(_BVEvent):
    type: Literal["boardview.filter"] = "boardview.filter"
    prefix: str | None


class DrawArrow(_BVEvent):
    # wire contract: frontend reads ev.from
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    type: Literal["boardview.draw_arrow"] = "boardview.draw_arrow"
    from_: tuple[int, int] = Field(alias="from")
    to: tuple[int, int]
    id: str


class Measure(_BVEvent):
    type: Literal["boardview.measure"] = "boardview.measure"
    from_refdes: str
    to_refdes: str
    distance_mm: float


class ShowPin(_BVEvent):
    type: Literal["boardview.show_pin"] = "boardview.show_pin"
    refdes: str
    pin: int
    pos: tuple[int, int]


class UploadError(_BVEvent):
    type: Literal["boardview.upload_error"] = "boardview.upload_error"
    reason: Literal["obfuscated", "malformed-header", "unsupported-format", "io-error"]
    message: str


class _SimEvent(BaseModel):
    """Base class for simulation / observation events (backend → frontend)."""

    type: str


class SimulationObservationSet(_SimEvent):
    type: Literal["simulation.observation_set"] = "simulation.observation_set"
    target: str  # e.g. "rail:+3V3" | "comp:U7" | "pin:U7:3"
    mode: str    # ComponentMode | RailMode | "unknown"
    measurement: dict[str, Any] | None = None


class SimulationObservationClear(_SimEvent):
    type: Literal["simulation.observation_clear"] = "simulation.observation_clear"


class SimulationRepairValidated(_SimEvent):
    type: Literal["simulation.repair_validated"] = "simulation.repair_validated"
    repair_id: str
    fixes_count: int
