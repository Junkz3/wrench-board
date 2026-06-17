import logging
from dataclasses import dataclass
from enum import Enum, auto

import numpy as np


class PartMountingSide(Enum):
    """Mounting side of a component."""
    BOTH = 0    # Both sides
    BOTTOM = 1  # Bottom side
    TOP = 2     # Top side

class PartType(Enum):
    """Component type."""
    SMD = auto()
    THROUGH_HOLE = auto()

class PinSide(Enum):
    """Side of a pin."""
    BOTH = auto()
    BOTTOM = auto()
    TOP = auto()

@dataclass
class Point:
    """Point in x, y coordinates."""
    x: float = 0
    y: float = 0

    def __eq__(self, other):
        if not isinstance(other, Point):
            return NotImplemented
        return self.x == other.x and self.y == other.y

class Part:
    """A component on the board."""
    def __init__(self, name: str = "", mfg_code: str = "",
                 mounting_side: PartMountingSide = PartMountingSide.TOP,
                 part_type: PartType = PartType.THROUGH_HOLE):
        self.name = name
        self.mfg_code = mfg_code
        self.mounting_side = mounting_side
        self.part_type = part_type
        self.end_of_pins = 0  # Expected number of pins
        self.pins: list[Pin] = []  # List of pins
        self.p1 = Point()  # Top-left point
        self.p2 = Point()  # Bottom-right point
        self._position = Point()  # Center position
        self.component_type = "normal"  # can be "normal" or "dummy"

    def is_dummy(self) -> bool:
        """Check whether this is a dummy component (starting with ...)."""
        return self.component_type == "dummy" or self.name.startswith("...")

    @property
    def position(self) -> Point:
        """Center position of the component."""
        if not hasattr(self, '_position'):
            self._position = Point(
                (self.p2.x + self.p1.x) / 2,
                (self.p2.y + self.p1.y) / 2
            )
        return self._position

    @position.setter
    def position(self, pos: Point):
        """Set the center position of the component."""
        self._position = pos

    @property
    def width(self) -> float:
        """Component width."""
        return abs(self.p2.x - self.p1.x)

    @property
    def height(self) -> float:
        """Component height."""
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
    """Represents a pin on a component."""
    def __init__(self, position: Point, probe: int, part_index: int,
                 side: PinSide = PinSide.TOP, net: str = "UNCONNECTED",
                 number: str = "", name: str = "", radius: float = 0.5):
        self.position = position
        self.probe = probe
        self.part_index = part_index
        self.side = side
        self.net = net
        self.number = number  # Pin number (e.g. "1", "2", "A1", "B2")
        self.name = name  # Pin name (e.g. "GND", "VCC", "MOSI")
        self.radius = radius  # Radius in millimetres

    def __lt__(self, other):
        """Sort pins by component, then by number."""
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
    """A test point on the board."""
    def __init__(self, probe: int, position: Point, side: PartMountingSide,
                 net: str = "UNCONNECTED"):
        self.probe = probe
        self.position = position
        self.side = side
        self.net = net

class Outline:
    """Outline class."""
    def __init__(self):
        self.points = []  # Outline points
        self.segments = []  # Outline segments

    def add_point(self, point: Point):
        """Add a point to the outline."""
        self.points.append(point)

    def add_segment(self, segment: tuple[Point, Point]):
        """Add a segment to the outline."""
        self.segments.append(segment)

class BoardFormatBase:
    """Base class for board file formats."""

    def __init__(self):
        self.valid = False
        self.error_msg = ""
        self.logger = logging.getLogger(self.__class__.__name__)

        # Board data
        self.format_points = []  # Format/outline points
        self.outline_segments = []  # Outline segments
        self.parts = []  # List of components
        self.pins = []  # List of pins
        self.nails = []  # List of test points
        self.outline = Outline()  # Outline

    def generate_outline(self):
        """Generate a rectangular outline based on the pin positions."""
        if len(self.outline_segments) >= 3 or len(self.format_points) >= 3:
            return  # Outline already defined

        # Find the board bounds
        margin = 200  # Margin in mils, as in OpenBoardView
        min_x = min_y = float('inf')
        max_x = max_y = float('-inf')

        # Check the pins
        for pin in self.pins:
            min_x = min(min_x, pin.position.x)
            max_x = max(max_x, pin.position.x)
            min_y = min(min_y, pin.position.y)
            max_y = max(max_y, pin.position.y)

        # Check the test points
        for nail in self.nails:
            min_x = min(min_x, nail.position.x)
            max_x = max(max_x, nail.position.x)
            min_y = min(min_y, nail.position.y)
            max_y = max(max_y, nail.position.y)

        # Add the margin
        min_x -= margin
        min_y -= margin
        max_x += margin
        max_y += margin

        # Create the outline points
        self.format_points = [
            Point(min_x, min_y),  # Top-left corner
            Point(max_x, min_y),  # Top-right corner
            Point(max_x, max_y),  # Bottom-right corner
            Point(min_x, max_y),  # Bottom-left corner
        ]

        # Create the outline segments
        self.outline_segments = [
            (self.format_points[0], self.format_points[1]),  # Top
            (self.format_points[1], self.format_points[2]),  # Right
            (self.format_points[2], self.format_points[3]),  # Bottom
            (self.format_points[3], self.format_points[0]),  # Left
        ]

        self.logger.debug(f"Outline generated: ({min_x}, {min_y}) - ({max_x}, {max_y})")

    @staticmethod
    def verify_format(data: bytes) -> bool:
        """Check whether the data matches this format."""
        raise NotImplementedError("This method must be implemented by derived classes")

    def load(self, data: bytes) -> bool:
        """Load the file data."""
        raise NotImplementedError("This method must be implemented by derived classes")

    def add_nails_as_pins(self):
        """Convert nails into pins for display."""
        for nail in self.nails:
            self.pins.append(Pin(
                position=nail.position,
                probe=nail.probe,
                part_index=len(self.parts),  # Nails are added as a new part
                side=PinSide.BOTH if nail.side == PartMountingSide.BOTH else
                     PinSide.BOTTOM if nail.side == PartMountingSide.BOTTOM else
                     PinSide.TOP,
                net=nail.net
            ))

    @staticmethod
    def arc_to_segments(start_angle: float, end_angle: float, radius: float,
                       p1: Point, p2: Point, pc: Point,
                       slice_angle_rad: float = np.pi/18) -> list[tuple[Point, Point]]:
        """Convert an arc into line segments."""
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

    def to_board(self) -> 'Board':  # noqa: F821 - fwd-ref; Board imported locally in body
        """
        Convert this format into a normalized Board structure.

        This method must be overridden by subclasses to handle the
        quirks of each format.
        """
        from core.models.board import Board, BoardSide, Component, MountType, Net, PinType
        from core.models.board import Pin as NormalizedPin
        from core.models.board import Point as NormalizedPoint

        # Create the normalized board
        # Extract the format type from the class name (e.g. "BRDFile" -> "brd")
        class_name = self.__class__.__name__
        format_type = class_name.replace("File", "").lower()
        board = Board(format_type=format_type)

        # Convert the nets
        nets_dict = {}  # name -> Net

        # Collect all unique nets from the pins
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

        # Also add the nets from the nails (test points)
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

        # Convert the components and pins
        for part_idx, part in enumerate(self.parts):
            # Convert the component
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

            # Copy the dimensions (p1/p2) into bbox_min/bbox_max
            # Make sure p1 and p2 exist and define a valid size (not just 0,0)
            if hasattr(part, 'p1') and hasattr(part, 'p2'):
                has_valid_bbox = (part.p1.x != part.p2.x or part.p1.y != part.p2.y)
                if has_valid_bbox:
                    component.bbox_min = NormalizedPoint(part.p1.x, part.p1.y)
                    component.bbox_max = NormalizedPoint(part.p2.x, part.p2.y)

            board.components.append(component)

            # Convert this component's pins
            part_pins = [p for p in self.pins if p.part_index == part_idx]

            for old_pin in part_pins:
                # Compute the pin's absolute position
                # XZZ: pin.pos (relative) + part.x/y (absolute)
                # GenCAD: pin.position (already absolute)
                if hasattr(old_pin, 'position') and old_pin.position is not None:
                    # GenCAD: position already absolute
                    pin_x = old_pin.position.x
                    pin_y = old_pin.position.y
                elif hasattr(old_pin, 'pos') and old_pin.pos is not None:
                    # XZZ: relative position, compute the absolute one
                    part_x = getattr(part, 'x', component.center.x)
                    part_y = getattr(part, 'y', component.center.y)
                    pin_x = part_x + old_pin.pos.x
                    pin_y = part_y + old_pin.pos.y
                else:
                    # Fallback
                    pin_x = component.center.x
                    pin_y = component.center.y

                # Create the normalized pin with ABSOLUTE position
                pin_number = getattr(old_pin, 'number', None) or getattr(old_pin, 'snum', None) or str(len(component.pins) + 1)

                # Determine the pin type
                is_dummy = getattr(part, 'is_dummy', lambda: False)()  if callable(getattr(part, 'is_dummy', None)) else False
                pin_type = PinType.TEST_PAD if is_dummy else PinType.COMPONENT

                # Determine the side
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
                    diameter=old_radius * 2,  # radius to diameter
                    pin_type=pin_type,
                    board_side=board_side,
                    net=nets_dict.get(getattr(old_pin, 'net', 'UNCONNECTED'), nets_dict.get("UNCONNECTED")),
                    component=component,
                    width=old_width,
                    height=old_height,
                    rotation=getattr(old_pin, 'rotation', 0.0),
                    shape_type=getattr(old_pin, 'shape_type', 0)
                )

                # Add the pin to the component and the board
                component.pins.append(new_pin)
                board.pins.append(new_pin)

                # Add the pin to the net
                if new_pin.net:
                    new_pin.net.pins.append(new_pin)

        # Collect dummy-component pin positions to avoid duplicates
        dummy_pin_positions = set()
        for part in self.parts:
            if getattr(part, 'component_type', 'normal') == 'dummy' or part.name.startswith('...'):
                for pin in part.pins:
                    dummy_pin_positions.add((round(pin.position.x, 1), round(pin.position.y, 1)))

        # Convert the nails (test pads) into pins, avoiding duplicates with dummy components
        for nail in self.nails:
            # Check whether this position is already covered by a dummy component
            nail_pos = (round(nail.position.x, 1), round(nail.position.y, 1))
            if nail_pos in dummy_pin_positions:
                continue  # Skip - this nail is already represented by a dummy-component pin

            nail_side = getattr(nail, 'side', None)
            # Handle both possible types: PinSide and PartMountingSide
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
                diameter=20,  # Default size for test pads
                pin_type=PinType.TEST_PAD,
                board_side=board_side,
                net=nail_net,
                component=None,  # Nails have no parent component
            )
            board.pins.append(new_pin)

            if nail_net:
                nail_net.pins.append(new_pin)

        # Convert the outline
        for point in self.format_points:
            board.outline_points.append(NormalizedPoint(point.x, point.y))

        for seg in self.outline_segments:
            board.outline_segments.append((
                NormalizedPoint(seg[0].x, seg[0].y),
                NormalizedPoint(seg[1].x, seg[1].y)
            ))

        # Build the indices
        board.build_indices()

        # Compute the dimensions
        board.calculate_dimensions()

        return board
