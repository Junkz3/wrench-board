from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class BoardFormatBase:
    pass

@dataclass
class Point:
    x: float
    y: float

@dataclass
class PartType:
    SMD: str = "SMD"

@dataclass
class PartMountingSide:
    TOP: str = "TOP"
    BOTTOM: str = "BOTTOM"

@dataclass
class PinSide:
    TOP: str = "TOP"

@dataclass
class Outline:
    points: list[Point] = field(default_factory=list)

@dataclass
class Via:
    position: Point
    net: str
    layer_a_radius: float = 0.0
    layer_b_radius: float = 0.0
    layer_a_type: int = 0
    layer_b_type: int = 0
    text: str = ""

@dataclass
class XZZLine:  # Moved before Pin
    layer: int
    x1: float
    y1: float
    x2: float
    y2: float
    scale: float
    net_index: int = 0

@dataclass
class Pin:
    name: bytes = b""  # Pin name (e.g. b"1" for pin 1)
    pos: Point = field(default_factory=lambda: Point(0, 0))  # Position (x, y) in mm
    side: str = "TOP"  # PCB side (TOP or BOTTOM)
    net: str = ""  # Net the pin belongs to
    net_index: int = 0  # Net index
    probe: int = 0  # Test index (optional, for debugging or verification)
    part_index: int = 0  # Parent component index
    shape_type: int = 0  # Shape type (1165000 for pins 1/2, 1005000 for pins 3/4/5)
    width: float = 0.0  # Pin width in mm (equals height for a square)
    height: float = 0.0  # Pin height in mm (equals width for a square)
    rotation: float = 0.0  # Rotation in degrees (e.g. 298.24° for pins 1/2, 257.28° for pins 3/4/5)
    layer: int = 0  # PCB layer (optional)
    unknown_bytes: str = None  # Attribute for the 8 unknown bytes
    raw_shape_data: bytes = None  # Raw shape data
    snum: str = ""  # Pin serial number

@dataclass
class XZZArc:
    layer: int
    x1: float
    y1: float
    radius: float
    angle_start: float
    angle_end: float
    scale: float

@dataclass
class XZZVia:
    x: float = 0
    y: float = 0
    layer_a_radius: float = 0
    layer_b_radius: float = 0
    layer_a_type: int = 0
    layer_b_type: int = 0
    net_index: int = 0
    text: str = ""

@dataclass
class XZZPart:
    x: float = 0.0
    y: float = 0.0
    rotation: int = 0
    mirror: bool = False
    part_type: str = "SMD"
    mounting_side: str = "TOP"
    name: bytes = b"Unknown"
    category: str = ""  # Component category (U, L, R, C, D, Q, etc.)
    pins: list[Pin] = field(default_factory=list)
    texts: list[Any] = field(default_factory=list)
    net_name: str = ""
    visibility: bool = False
    group_name: str = ""  # Group name

@dataclass
class XZZTestPad:
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    layer: int = 0
    net_index: int = 0
    net: str = ""
    name: bytes = b""
    rotation: float = 0.0
    mounting_side: str = "TOP"

@dataclass
class Net:
    index: int
    name: str
    connected_pins: list[Any] = field(default_factory=list)
    connected_vias: list[Any] = field(default_factory=list)
    connected_lines: list[Any] = field(default_factory=list)

@dataclass
class XZZText:
    text: bytes
    x: float
    y: float
    layer: int
    font_size: float
    font_scale: float
    visibility: bool
    source: str

class XZZBlockType(IntEnum):
    ARC = 0x01
    VIA = 0x02
    UNKNOWN_3 = 0x03
    UNKNOWN_4 = 0x04
    LINE = 0x05
    TEXT = 0x06
    PART = 0x07
    UNKNOWN_8 = 0x08
    TEST_PAD = 0x09
