from dataclasses import dataclass
from enum import Enum, auto
from typing import List, Optional, Tuple, Union
import numpy as np
import logging

class PartMountingSide(Enum):
    """Face de montage d'un composant."""
    BOTH = 0    # Les deux faces
    BOTTOM = 1  # Face du dessous
    TOP = 2     # Face du dessus

class PartType(Enum):
    """Type de composant."""
    SMD = auto()
    THROUGH_HOLE = auto()

class PinSide(Enum):
    """Côté d'une broche."""
    BOTH = auto()
    BOTTOM = auto()
    TOP = auto()

@dataclass
class Point:
    """Point en coordonnées x, y."""
    x: float = 0
    y: float = 0

    def __eq__(self, other):
        if not isinstance(other, Point):
            return NotImplemented
        return self.x == other.x and self.y == other.y

class Part:
    """Un composant sur le circuit."""
    def __init__(self, name: str = "", mfg_code: str = "", 
                 mounting_side: PartMountingSide = PartMountingSide.TOP,
                 part_type: PartType = PartType.THROUGH_HOLE):
        self.name = name
        self.mfg_code = mfg_code
        self.mounting_side = mounting_side
        self.part_type = part_type
        self.end_of_pins = 0  # Nombre de broches attendu
        self.pins: List['Pin'] = []  # Liste des broches
        self.p1 = Point()  # Point en haut à gauche
        self.p2 = Point()  # Point en bas à droite
        self._position = Point()  # Position centrale
        self.component_type = "normal"  # peut être "normal" ou "dummy"
        
    def is_dummy(self) -> bool:
        """Vérifie si c'est un composant dummy (commençant par ...)"""
        return self.component_type == "dummy" or self.name.startswith("...")

    @property
    def position(self) -> Point:
        """Position centrale du composant."""
        if not hasattr(self, '_position'):
            self._position = Point(
                (self.p2.x + self.p1.x) / 2,
                (self.p2.y + self.p1.y) / 2
            )
        return self._position

    @position.setter
    def position(self, pos: Point):
        """Définit la position centrale du composant."""
        self._position = pos

    @property
    def width(self) -> float:
        """Largeur du composant."""
        return abs(self.p2.x - self.p1.x)

    @property
    def height(self) -> float:
        """Hauteur du composant."""
        return abs(self.p2.y - self.p1.y)

    def __str__(self):
        return f"{self.name} ({self.part_type.name})"

    def __eq__(self, other):
        if not isinstance(other, Part):
            return NotImplemented
        return (self.name == other.name and 
                self.mfg_code == other.mfg_code and 
                self.mounting_side == other.mounting_side and
                self.part_type == other.part_type and
                self.p1 == other.p1 and
                self.p2 == other.p2)

    def __hash__(self):
        return hash((self.name, self.mfg_code, self.mounting_side, 
                    self.part_type, self.p1.x, self.p1.y, self.p2.x, self.p2.y))

class Pin:
    """Représente une broche sur un composant."""
    def __init__(self, position: Point, probe: int, part_index: int,
                 side: PinSide = PinSide.TOP, net: str = "UNCONNECTED",
                 number: str = "", name: str = "", radius: float = 0.5):
        self.position = position
        self.probe = probe
        self.part_index = part_index
        self.side = side
        self.net = net
        self.number = number  # Numéro de la broche (ex: "1", "2", "A1", "B2")
        self.name = name  # Nom de la broche (ex: "GND", "VCC", "MOSI")
        self.radius = radius  # Rayon en millimètres
        
    def __lt__(self, other):
        """Pour trier les broches par composant puis par numéro."""
        if not isinstance(other, Pin):
            return NotImplemented
        return (self.part_index, self.number or "") < (other.part_index, other.number or "")

    def __eq__(self, other):
        if not isinstance(other, Pin):
            return NotImplemented
        return (self.position == other.position and 
                self.probe == other.probe and
                self.part_index == other.part_index and
                self.side == other.side and
                self.net == other.net and
                self.number == other.number and
                self.name == other.name)

    def __hash__(self):
        return hash((self.position.x, self.position.y, self.probe, 
                    self.part_index, self.side, self.net, 
                    self.number, self.name))

class Nail:
    """Un point de test sur le circuit."""
    def __init__(self, probe: int, position: Point, side: PartMountingSide,
                 net: str = "UNCONNECTED"):
        self.probe = probe
        self.position = position
        self.side = side
        self.net = net

class Outline:
    """Classe pour le contour."""
    def __init__(self):
        self.points = []  # Points du contour
        self.segments = []  # Segments du contour

    def add_point(self, point: Point):
        """Ajoute un point au contour."""
        self.points.append(point)

    def add_segment(self, segment: Tuple[Point, Point]):
        """Ajoute un segment au contour."""
        self.segments.append(segment)

class BoardFormatBase:
    """Classe de base pour les formats de fichier de circuit."""
    
    def __init__(self):
        self.valid = False
        self.error_msg = ""
        self.logger = logging.getLogger(self.__class__.__name__)
        
        # Données du circuit
        self.format_points = []  # Points de format/contour
        self.outline_segments = []  # Segments de contour
        self.parts = []  # Liste des composants
        self.pins = []  # Liste des broches
        self.nails = []  # Liste des points de test
        self.outline = Outline()  # Contour
        
    def generate_outline(self):
        """Génère un contour rectangulaire basé sur les positions des broches."""
        if len(self.outline_segments) >= 3 or len(self.format_points) >= 3:
            return  # Déjà un contour défini
            
        # Trouve les limites du circuit
        margin = 200  # Marge en mils comme dans OpenBoardView
        min_x = min_y = float('inf')
        max_x = max_y = float('-inf')
        
        # Vérifie les broches
        for pin in self.pins:
            min_x = min(min_x, pin.position.x)
            max_x = max(max_x, pin.position.x)
            min_y = min(min_y, pin.position.y)
            max_y = max(max_y, pin.position.y)
            
        # Vérifie les points de test
        for nail in self.nails:
            min_x = min(min_x, nail.position.x)
            max_x = max(max_x, nail.position.x)
            min_y = min(min_y, nail.position.y)
            max_y = max(max_y, nail.position.y)
            
        # Ajoute la marge
        min_x -= margin
        min_y -= margin
        max_x += margin
        max_y += margin
        
        # Crée les points de contour
        self.format_points = [
            Point(min_x, min_y),  # Coin supérieur gauche
            Point(max_x, min_y),  # Coin supérieur droit
            Point(max_x, max_y),  # Coin inférieur droit
            Point(min_x, max_y),  # Coin inférieur gauche
        ]
        
        # Crée les segments de contour
        self.outline_segments = [
            (self.format_points[0], self.format_points[1]),  # Haut
            (self.format_points[1], self.format_points[2]),  # Droite
            (self.format_points[2], self.format_points[3]),  # Bas
            (self.format_points[3], self.format_points[0]),  # Gauche
        ]
        
        self.logger.info(f"Contour généré: ({min_x}, {min_y}) - ({max_x}, {max_y})")

    @staticmethod
    def verify_format(data: bytes) -> bool:
        """Vérifie si les données correspondent à ce format."""
        raise NotImplementedError("Cette méthode doit être implémentée par les classes dérivées")
    
    def load(self, data: bytes) -> bool:
        """Charge les données du fichier."""
        raise NotImplementedError("Cette méthode doit être implémentée par les classes dérivées")
    
    def add_nails_as_pins(self):
        """Convertit les nails en pins pour l'affichage."""
        for nail in self.nails:
            self.pins.append(Pin(
                position=nail.position,
                probe=nail.probe,
                part_index=len(self.parts),  # Les nails sont ajoutés comme une nouvelle partie
                side=PinSide.BOTH if nail.side == PartMountingSide.BOTH else
                     PinSide.BOTTOM if nail.side == PartMountingSide.BOTTOM else
                     PinSide.TOP,
                net=nail.net
            ))
    
    @staticmethod
    def arc_to_segments(start_angle: float, end_angle: float, radius: float,
                       p1: Point, p2: Point, pc: Point,
                       slice_angle_rad: float = np.pi/18) -> List[Tuple[Point, Point]]:
        """Convertit un arc en segments de ligne."""
        segments = []
        angle = start_angle
        while angle < end_angle:
            next_angle = min(angle + slice_angle_rad, end_angle)
            x1 = pc.x + radius * np.cos(angle)
            y1 = pc.y + radius * np.sin(angle)
            x2 = pc.x + radius * np.cos(next_angle)
            y2 = pc.y + radius * np.sin(next_angle)
            segments.append((Point(x1, y1), Point(x2, y2)))
            angle = next_angle
        return segments

    def to_board(self) -> 'Board':
        """
        Convertit ce format vers une structure Board normalisée.

        Cette méthode doit être surchargée par les sous-classes pour
        gérer les spécificités de chaque format.
        """
        from core.models.board import (
            Board, Component, Pin as NormalizedPin, Net,
            Point as NormalizedPoint, BoardSide, PinType, MountType
        )

        # Créer le board normalisé
        # Extraire le type de format depuis le nom de classe (ex: "BRDFile" -> "brd")
        class_name = self.__class__.__name__
        format_type = class_name.replace("File", "").lower()
        board = Board(format_type=format_type)

        # Convertir les nets
        nets_dict = {}  # name -> Net

        # Collecter tous les nets uniques depuis les pins
        for pin in self.pins:
            net_name = getattr(pin, 'net', 'UNCONNECTED')
            if not net_name or net_name == "":
                net_name = "UNCONNECTED"

            if net_name not in nets_dict:
                net = Net(
                    name=net_name,
                    is_ground=(net_name.upper() in ["GND", "GROUND"])
                )
                nets_dict[net_name] = net
                board.nets.append(net)

        # Ajouter aussi les nets depuis les nails (points de test)
        for nail in self.nails:
            net_name = getattr(nail, 'net', 'UNCONNECTED')
            if not net_name or net_name == "":
                net_name = "UNCONNECTED"

            if net_name not in nets_dict:
                net = Net(
                    name=net_name,
                    is_ground=(net_name.upper() in ["GND", "GROUND"])
                )
                nets_dict[net_name] = net
                board.nets.append(net)

        # Convertir les composants et pins
        for part_idx, part in enumerate(self.parts):
            # Convertir le composant
            component = Component(
                name=getattr(part, 'name', ''),
                mfgcode=getattr(part, 'mfg_code', getattr(part, 'mfgcode', '')),
                mount_type=MountType.SMD if getattr(part, 'part_type', PartType.THROUGH_HOLE) == PartType.SMD else MountType.THROUGH_HOLE,
                board_side=BoardSide.TOP if getattr(part, 'mounting_side', PartMountingSide.TOP) == PartMountingSide.TOP else
                           BoardSide.BOTTOM if getattr(part, 'mounting_side', PartMountingSide.BOTTOM) == PartMountingSide.BOTTOM else
                           BoardSide.BOTH,
                center=NormalizedPoint(part.position.x, part.position.y) if hasattr(part, 'position') else NormalizedPoint(0, 0),
                rotation=getattr(part, 'rotation', 0.0)
            )

            # Copier les dimensions (p1/p2) vers bbox_min/bbox_max
            # Vérifier que p1 et p2 existent et définissent une taille valide (pas juste 0,0)
            if hasattr(part, 'p1') and hasattr(part, 'p2'):
                has_valid_bbox = (part.p1.x != part.p2.x or part.p1.y != part.p2.y)
                if has_valid_bbox:
                    component.bbox_min = NormalizedPoint(part.p1.x, part.p1.y)
                    component.bbox_max = NormalizedPoint(part.p2.x, part.p2.y)

            board.components.append(component)

            # Convertir les pins de ce composant
            part_pins = [p for p in self.pins if p.part_index == part_idx]

            for old_pin in part_pins:
                # Calculer la position absolue du pin
                # XZZ: pin.pos (relatif) + part.x/y (absolu)
                # GenCAD: pin.position (absolu directement)
                if hasattr(old_pin, 'position') and old_pin.position is not None:
                    # GenCAD: position déjà absolue
                    pin_x = old_pin.position.x
                    pin_y = old_pin.position.y
                elif hasattr(old_pin, 'pos') and old_pin.pos is not None:
                    # XZZ: position relative, calculer l'absolue
                    part_x = getattr(part, 'x', component.center.x)
                    part_y = getattr(part, 'y', component.center.y)
                    pin_x = part_x + old_pin.pos.x
                    pin_y = part_y + old_pin.pos.y
                else:
                    # Fallback
                    pin_x = component.center.x
                    pin_y = component.center.y

                # Créer le pin normalisé avec position ABSOLUE
                pin_number = getattr(old_pin, 'number', None) or getattr(old_pin, 'snum', None) or str(len(component.pins) + 1)

                # Déterminer le type de pin
                is_dummy = getattr(part, 'is_dummy', lambda: False)()  if callable(getattr(part, 'is_dummy', None)) else False
                pin_type = PinType.TEST_PAD if is_dummy else PinType.COMPONENT

                # Déterminer le side
                old_side = getattr(old_pin, 'side', PinSide.BOTH)
                board_side = (BoardSide.TOP if old_side == PinSide.TOP else
                             BoardSide.BOTTOM if old_side == PinSide.BOTTOM else
                             BoardSide.BOTH)

                old_radius = getattr(old_pin, 'radius', 0.5)
                old_width = getattr(old_pin, 'width', None)
                old_height = getattr(old_pin, 'height', None)

                new_pin = NormalizedPin(
                    position=NormalizedPoint(pin_x, pin_y),
                    number=pin_number,
                    diameter=old_radius * 2,  # radius -> diameter
                    pin_type=pin_type,
                    board_side=board_side,
                    net=nets_dict.get(getattr(old_pin, 'net', 'UNCONNECTED'), nets_dict.get("UNCONNECTED")),
                    component=component,
                    width=old_width,
                    height=old_height,
                    rotation=getattr(old_pin, 'rotation', 0.0),
                    shape_type=getattr(old_pin, 'shape_type', 0)
                )

                # Ajouter le pin au composant et au board
                component.pins.append(new_pin)
                board.pins.append(new_pin)

                # Ajouter le pin au net
                if new_pin.net:
                    new_pin.net.pins.append(new_pin)

        # Collecter les positions des pins des composants dummy pour éviter les doublons
        dummy_pin_positions = set()
        for part in self.parts:
            if getattr(part, 'component_type', 'normal') == 'dummy' or part.name.startswith('...'):
                for pin in part.pins:
                    dummy_pin_positions.add((round(pin.position.x, 1), round(pin.position.y, 1)))

        # Convertir les nails (test pads) en pins, en évitant les doublons avec les composants dummy
        for nail in self.nails:
            # Vérifier si cette position est déjà couverte par un composant dummy
            nail_pos = (round(nail.position.x, 1), round(nail.position.y, 1))
            if nail_pos in dummy_pin_positions:
                continue  # Skip - ce nail est déjà représenté par un pin de composant dummy

            nail_side = getattr(nail, 'side', None)
            # Gérer les deux types possibles: PinSide et PartMountingSide
            if nail_side == PinSide.TOP or nail_side == PartMountingSide.TOP:
                board_side = BoardSide.TOP
            elif nail_side == PinSide.BOTTOM or nail_side == PartMountingSide.BOTTOM:
                board_side = BoardSide.BOTTOM
            else:
                board_side = BoardSide.BOTH

            net_name = getattr(nail, 'net', 'UNCONNECTED')
            nail_net = nets_dict.get(net_name, nets_dict.get("UNCONNECTED"))

            new_pin = NormalizedPin(
                position=NormalizedPoint(nail.position.x, nail.position.y),
                number=str(getattr(nail, 'probe', len(board.pins) + 1)),
                diameter=20,  # Taille par défaut pour les test pads
                pin_type=PinType.TEST_PAD,
                board_side=board_side,
                net=nail_net,
                component=None,  # Les nails n'ont pas de composant parent
            )
            board.pins.append(new_pin)

            if nail_net:
                nail_net.pins.append(new_pin)

        # Convertir le contour
        for point in self.format_points:
            board.outline_points.append(NormalizedPoint(point.x, point.y))

        for seg in self.outline_segments:
            board.outline_segments.append((
                NormalizedPoint(seg[0].x, seg[0].y),
                NormalizedPoint(seg[1].x, seg[1].y)
            ))

        # Construire les index
        board.build_indices()

        # Calculer les dimensions
        board.calculate_dimensions()

        return board
