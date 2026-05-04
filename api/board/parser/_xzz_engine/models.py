from dataclasses import dataclass, field
from typing import List, Any
from enum import IntEnum

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
    points: List[Point] = field(default_factory=list)

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
class XZZLine:  # Déplacé avant Pin
    layer: int
    x1: float
    y1: float
    x2: float
    y2: float
    scale: float
    net_index: int = 0

@dataclass
class Pin:
    name: bytes = b""  # Nom du pin (par exemple, b"1" pour le pin 1)
    pos: Point = field(default_factory=lambda: Point(0, 0))  # Position (x, y) en mm
    side: str = "TOP"  # Côté du PCB (TOP ou BOTTOM)
    net: str = ""  # Réseau auquel le pin appartient
    net_index: int = 0  # Index du réseau
    probe: int = 0  # Indice de test (optionnel, pour débogage ou vérification)
    part_index: int = 0  # Indice du composant parent
    shape_type: int = 0  # Type de forme (1165000 pour pins 1/2, 1005000 pour pins 3/4/5)
    width: float = 0.0  # Largeur du pin en mm (pour un carré, égal à height)
    height: float = 0.0  # Hauteur du pin en mm (pour un carré, égal à width)
    rotation: float = 0.0  # Rotation en degrés (par exemple, 298.24° pour pins 1/2, 257.28° pour pins 3/4/5)
    layer: int = 0  # Couche du PCB (optionnel)
    unknown_bytes: str = None  # Attribut pour les 8 octets inconnus
    raw_shape_data: bytes = None  # Données brutes de la forme
    snum: str = ""  # Numéro de série de la pin

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
    category: str = ""  # Catégorie du composant (U, L, R, C, D, Q, etc.)
    pins: List[Pin] = field(default_factory=list)
    texts: List[Any] = field(default_factory=list)
    net_name: str = ""
    visibility: bool = False
    group_name: str = ""  # Ajout du nom de groupe

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
    connected_pins: List[Any] = field(default_factory=list)
    connected_vias: List[Any] = field(default_factory=list)
    connected_lines: List[Any] = field(default_factory=list)

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