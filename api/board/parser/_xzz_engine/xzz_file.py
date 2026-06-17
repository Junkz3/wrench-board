#!/usr/bin/env python3
"""
XZZ File Parser - With Rust Acceleration

Uses Rust module (repairboard_core) when available for 10-50x faster parsing.
Falls back to pure Python implementation otherwise.
"""
import json
import logging
import os
import sys

from ._rm_base import BoardFormatBase, Point
from .decryptor import decrypt_file, decrypt_with_des
from .parser_helpers import (
    parse_arc,
    parse_blocks_generator,
    parse_header,
    parse_images,
    parse_line,
    parse_nets,
    parse_part_block,
    parse_post_v6_block,
    parse_text,
)
from .utils import read_uint32, translate_hex_string

# Rust acceleration disabled: pure Python is fast enough for the boards
# we deal with (a single 820-class fragment parses in ~25 ms). A native
# extension would otherwise need its own toolchain in `make install`.
_USE_RUST = False

CONVERSION_FACTOR = 1000000.0  # Raw values are in nm (nanometres); convert to mm

def setup_logging():
    """Return the module logger.

    Library code must not own handlers or write log files: the host
    application (api/logging_setup.py) configures the root logger and
    formatting. We only fetch the named logger and let records propagate
    to the app's handler, so output is single-line and consistently
    formatted. Verbose parse chatter is emitted at DEBUG.
    """
    return logging.getLogger("xzz_parser")

XZZ_KEY_ENV = "WRENCH_BOARD_XZZ_KEY"


class XZZFile(BoardFormatBase):
    # DES master key: loaded at runtime from WRENCH_BOARD_XZZ_KEY (8 bytes
    # hex). Aligns with the OpenBoardView convention of leaving cipher keys
    # as runtime configuration. Empty string disables DES decryption; XOR
    # decryption (using a key derived from the file itself at offset 0x10)
    # still works without it.
    MASTER_KEY = os.environ.get(XZZ_KEY_ENV, "").strip()
    DIODE_PATTERN = bytes([
        0x76, 0x36, 0x76, 0x36, 0x35, 0x35, 0x35, 0x76,
        0x36, 0x76, 0x36, 0x3D, 0x3D, 0x3D, 0xD7, 0xE8,
        0xD6, 0xB5, 0x0A
    ])

    def __init__(self):
        super().__init__()
        self.logger = setup_logging()
        self.error_msg = ""
        self.image_block_start = 0
        self.net_block_start = 0
        self.nets = []      # Indexed list mapping nets to pins
        self.parts = []
        self.pins = []
        self.vias = []
        self.net_pins = {}  # Dict {net_name: [pins]}
        self.net_vias = {}
        self.outline = None
        self.lines = []
        self.arcs = []
        self.text_elements = []
        self.images = []
        self.post_v6_data = {}
        self.text_stats = {
            'standalone': 0,
            'part_labels': 0,
            'pin_names': 0,
            'net_names': 0
        }
        self.block_counts = {}
        self.main_data_blocks_size = 0
        self.width = 0.0
        self.height = 0.0
        self.xy_translation = None

    @staticmethod
    def verify_format(data: bytes) -> bool:
        """Check whether the data matches the XZZ format (optimized)."""
        if len(data) < 64:  # Minimum size for an XZZ file
            return False
        try:
            # Only decrypt the first 100 bytes to check the signature
            # instead of decrypting the whole file.

            # Extract the XOR key from offset 0x10
            xor_key = data[0x10]

            # Decrypt only the first 100 bytes
            sample_size = min(100, len(data))
            header_sample = bytearray(data[:sample_size])

            # Apply XOR
            for i in range(sample_size):
                header_sample[i] ^= xor_key

            # Check the signature after XOR decryption
            signature = header_sample[:11].decode("ascii", errors="ignore")
            return signature.startswith("XZZ")
        except Exception:
            return False

    def _check_signature(self, data: bytes) -> bool:
        try:
            signature = data[:11].decode("ascii", errors="ignore")
            if not signature.startswith("XZZ"):
                self.error_msg = f"Invalid signature: {signature}"
                self.logger.error(self.error_msg)
                return False
            return True
        except Exception as e:
            self.error_msg = f"Error while verifying the signature: {str(e)}"
            self.logger.error(self.error_msg)
            return False

    def _decrypt_file(self, data: bytes) -> bytes:
        """
        Decrypt the XZZ file (XOR + DES for PART blocks).

        Uses Rust when available (10-50x faster).
        """
        if not self.MASTER_KEY:
            self.error_msg = (
                f"XZZ DES key not configured. Set {XZZ_KEY_ENV} in your .env "
                "(8-byte hex string) to enable .pcb parsing."
            )
            self.logger.error(self.error_msg)
            raise RuntimeError(self.error_msg)
        # === RUST FAST PATH ===
        if _USE_RUST:
            self.logger.debug("Decrypting file... (Rust)")
            try:
                data = rust_decrypt_xzz_file(data, self.MASTER_KEY, self.DIODE_PATTERN)  # noqa: F821 - rust ext, only reachable when _USE_RUST
                # Extract main_data_blocks_size after decryption
                if len(data) >= 0x44:
                    self.main_data_blocks_size, _ = read_uint32(data, 0x40)
                    self.logger.debug(f"Main block size: {self.main_data_blocks_size} bytes (0x{self.main_data_blocks_size:X})")
                return data
            except Exception as e:
                self.logger.warning(f"Rust decryption failed, falling back to Python: {e}")
                # Fall through to Python implementation

        # === PYTHON FALLBACK ===
        self.logger.debug("Decrypting file... (Python)")
        # Use the modular helper to apply XOR and return a decrypted bytearray
        data = bytearray(decrypt_file(data, self.MASTER_KEY, self.DIODE_PATTERN, self.logger))
        current_pointer = 0x40
        if len(data) < current_pointer + 4:
            self.error_msg = "File too short to contain main_data_blocks_size"
            self.logger.error(self.error_msg)
            return bytes(data)
        self.main_data_blocks_size, current_pointer = read_uint32(data, current_pointer)
        self.logger.debug(f"Main block size: {self.main_data_blocks_size} bytes (0x{self.main_data_blocks_size:X})")
        # Apply DES decryption to each PART block (type 0x07)
        while current_pointer < 0x44 + self.main_data_blocks_size:
            block_type = data[current_pointer]
            current_pointer += 1
            block_size, current_pointer = read_uint32(data, current_pointer)
            if block_type == 0x07:
                self.logger.debug(f"DES-decrypting a 0x07 block of size {block_size} at position 0x{current_pointer:X}")
                try:
                    encrypted_data = data[current_pointer:current_pointer+block_size]
                    self.logger.debug(f"Encrypted data: {encrypted_data.hex()[:50]}...")
                    decrypted_data = decrypt_with_des(encrypted_data, self.MASTER_KEY)
                    data[current_pointer:current_pointer+block_size] = decrypted_data
                    self.logger.debug(f"Decrypted data: {decrypted_data.hex()[:50]}...")
                except Exception as e:
                    self.error_msg = f"Error during DES decryption: {str(e)}"
                    self.logger.error(self.error_msg)
                    return b""  # Return empty on DES failure
            current_pointer += block_size
        return bytes(data)




    def find_xy_translation(self):
        """Find the translation point to center the PCB (minimum outline point)."""
        # Filter outline lines (layer 28)
        outline_lines = [ln for ln in self.lines if ln.layer == 28]

        if not outline_lines:
            self.logger.warning("[TRANSLATION] No outline line found (layer 28)")
            return Point(0, 0)

        # Find the minimum and maximum coordinates
        min_x = min(min(line.x1, line.x2) for line in outline_lines)
        min_y = min(min(line.y1, line.y2) for line in outline_lines)
        max_x = max(max(line.x1, line.x2) for line in outline_lines)
        max_y = max(max(line.y1, line.y2) for line in outline_lines)

        # Compute the PCB dimensions (keep raw nm values for the final conversion)
        self.width = (max_x - min_x) * CONVERSION_FACTOR
        self.height = (max_y - min_y) * CONVERSION_FACTOR

        self.logger.debug(f"[TRANSLATION] Translation found: ({min_x:.2f}, {min_y:.2f}) mm")
        self.logger.debug(f"[TRANSLATION] PCB dimensions: {max_x - min_x:.2f} x {max_y - min_y:.2f} mm")

        return Point(min_x, min_y)

    def translate_segments(self):
        """Apply the translation to line, arc and text coordinates."""
        if not self.xy_translation:
            return
        for line in self.lines:
            line.x1 -= self.xy_translation.x
            line.y1 -= self.xy_translation.y
            line.x2 -= self.xy_translation.x
            line.y2 -= self.xy_translation.y
        for arc in self.arcs:
            arc.x1 -= self.xy_translation.x
            arc.y1 -= self.xy_translation.y
        for text in self.text_elements:
            text.x -= self.xy_translation.x
            text.y -= self.xy_translation.y

    def translate_pins(self):
        """Apply the translation to component, pin and internal-segment positions."""
        if not self.xy_translation:
            return

        # Translate the components (parts)
        for part in self.parts:
            part.x -= self.xy_translation.x
            part.y -= self.xy_translation.y

            # Translate the pins (ABSOLUTE positions since the fix)
            if hasattr(part, 'pins'):
                for pin in part.pins:
                    if hasattr(pin, 'pos'):
                        pin.pos.x -= self.xy_translation.x
                        pin.pos.y -= self.xy_translation.y

            # Translate the component's internal segments
            if hasattr(part, 'lines'):
                for line in part.lines:
                    line.x1 -= self.xy_translation.x
                    line.y1 -= self.xy_translation.y
                    line.x2 -= self.xy_translation.x
                    line.y2 -= self.xy_translation.y

    def load(self, data_or_path: str | bytes) -> bool:
        """Load and parse a PCB file."""
        self.error_msg = ""  # Reset on every call

        # Log acceleration status
        if _USE_RUST:
            self.logger.debug("XZZ Parser: Rust acceleration ENABLED")
        else:
            self.logger.debug("XZZ Parser: Using Python (Rust not available)")

        try:
            if isinstance(data_or_path, str):
                if not os.path.exists(data_or_path):
                    self.error_msg = f"The file {data_or_path} does not exist"
                    self.logger.error(self.error_msg)
                    return False

                self.logger.debug(f"Loading PCB file: {os.path.basename(data_or_path)}")
                try:
                    with open(data_or_path, 'rb') as f:
                        data = f.read()
                except Exception as e:
                    self.error_msg = f"Unable to read the file {data_or_path}: {str(e)}"
                    self.logger.error(self.error_msg)
                    return False
            else:
                data = data_or_path
                self.logger.debug("Loading data from buffer")

            # Check the minimum file size
            if len(data) < 64:  # Minimum size for a valid XZZ file
                self.error_msg = "The file is too small to be a valid XZZ file"
                self.logger.error(self.error_msg)
                return False

            # Decryption (decrypt first, then verify the signature afterwards)
            decrypted_data = self._decrypt_file(data)
            if not decrypted_data:
                if not self.error_msg:
                    self.error_msg = "The file is not in XZZ format or is corrupted"
                self.logger.error(self.error_msg)
                return False

            # Verify the signature after decryption
            if not self._check_signature(decrypted_data):
                if not self.error_msg:
                    self.error_msg = "The file is not in XZZ format (invalid signature)"
                self.logger.error(self.error_msg)
                return False

            # Parsing
            self.logger.debug("Starting parsing...")
            result = self.parse_decrypted_data(decrypted_data)

            if result:
                # Apply the translation to center the whole PCB at the origin
                # Find the minimum outline point
                self.xy_translation = self.find_xy_translation()

                if self.xy_translation and (self.xy_translation.x != 0 or self.xy_translation.y != 0):
                    self.logger.debug(f"Applying translation: ({self.xy_translation.x:.2f}, {self.xy_translation.y:.2f})")
                    # Apply the translation to EVERYTHING
                    self.translate_segments()  # Lines, arcs, text
                    self.translate_pins()      # Components (not the relative pins)
                    self.logger.debug("Translation applied to all elements")
                else:
                    self.logger.debug("No translation needed (already at the origin)")

                # Convert the dimensions to millimetres
                width_mm = self.width / CONVERSION_FACTOR
                height_mm = self.height / CONVERSION_FACTOR
                area_mm2 = width_mm * height_mm

                # Component statistics
                smd_count = len([p for p in self.parts if p.part_type == "SMD"])
                th_count = len(self.parts) - smd_count
                top_count = len([p for p in self.parts if p.mounting_side == "TOP"])
                bottom_count = len([p for p in self.parts if p.mounting_side == "BOTTOM"])

                # Net statistics
                total_pins = len(self.pins)
                avg_pins_per_net = total_pins / len(self.nets) if self.nets else 0

                # Trace statistics per layer
                layer_stats = {}
                for line in self.lines:
                    layer_stats.setdefault(line.layer, {'lines': 0, 'arcs': 0})
                    layer_stats[line.layer]['lines'] += 1
                for arc in self.arcs:
                    layer_stats.setdefault(arc.layer, {'lines': 0, 'arcs': 0})
                    layer_stats[arc.layer]['arcs'] += 1

                # Print the summary
                self.logger.debug("\n=== PCB SUMMARY ===")
                self.logger.debug("\nDimensions:")
                self.logger.debug(f"  Width:    {width_mm:.2f} mm")
                self.logger.debug(f"  Height:   {height_mm:.2f} mm")
                self.logger.debug(f"  Area:     {area_mm2:.2f} mm²")

                self.logger.debug(f"\nComponents ({len(self.parts)} total):")
                self.logger.debug(f"  SMD:          {smd_count}")
                self.logger.debug(f"  Through-hole: {th_count}")
                self.logger.debug(f"  TOP side:     {top_count}")
                self.logger.debug(f"  BOTTOM side:  {bottom_count}")

                self.logger.debug("\nConnectivity:")
                self.logger.debug(f"  Nets:         {len(self.nets)}")
                self.logger.debug(f"  Pins:         {total_pins}")
                self.logger.debug(f"  Vias:         {len(self.vias)}")
                self.logger.debug(f"  Avg pins/net: {avg_pins_per_net:.1f}")

                self.logger.debug("\nTraces per layer:")
                for layer, stats in sorted(layer_stats.items()):
                    if stats['lines'] > 0 or stats['arcs'] > 0:
                        layer_name = "OUTLINE" if layer == 28 else f"LAYER {layer}"
                        self.logger.debug(f"  {layer_name}:")
                        self.logger.debug(f"    Lines: {stats['lines']}")
                        self.logger.debug(f"    Arcs:  {stats['arcs']}")

                # Detailed log
                self.logger.debug("\nComponent details:", extra={
                    'details': json.dumps([{
                        'name': p.name.decode('utf-8', errors='replace') if isinstance(p.name, bytes) else str(p.name),
                        'type': p.part_type,
                        'side': p.mounting_side,
                        'position': f"({p.x/CONVERSION_FACTOR:.2f}, {p.y/CONVERSION_FACTOR:.2f})",
                        'pins': len(p.pins)
                    } for p in self.parts], indent=2)
                })

            return result

        except Exception as e:
            self.error_msg = f"Error during loading: {str(e)}"
            self.logger.error(self.error_msg, exc_info=True)
            return False

    def parse_decrypted_data(self, decrypted_data: bytes) -> bool:
        """Parse the decrypted data."""
        try:
            # Parsing
            self.logger.debug("Starting parsing...")
            header_ok, header_info = parse_header(decrypted_data, self.logger)
            if not header_ok:
                self.error_msg = "Header parsing failed"
                return False
            self.image_block_start = header_info.get("image_block_start", 0)
            self.net_block_start = header_info.get("net_block_start", 0)
            self.main_data_blocks_size = header_info.get("main_data_blocks_size", 0)
            current_offset = 0x44
            end_offset = 0x44 + self.main_data_blocks_size

            # IMPORTANT: Parse the nets BEFORE the data blocks,
            # because parse_part_block needs the net list to assign names to pins
            if self.net_block_start > 0:
                offset_nets = 0x20 + self.net_block_start
                self.logger.debug(f"Parsing nets at offset 0x{offset_nets:X}...")
                parse_nets(decrypted_data, offset_nets, self.nets, self.logger)
                self.logger.debug(f"Nets parsed: {len([n for n in self.nets if n is not None])} nets found")

            # Parse the images (optional, before the main blocks)
            if self.image_block_start > 0:
                offset_images = 0x20 + self.image_block_start
                _, self.images = parse_images(decrypted_data, offset_images, self.logger)

            # Progress bar for large files
            total_bytes = end_offset - current_offset
            processed_bytes = 0
            last_progress = -1

            for block_type, block_data, offset in parse_blocks_generator(decrypted_data, current_offset, end_offset, self.block_counts, self.logger):
                try:
                    # Report progress every 10%
                    processed_bytes = offset - current_offset
                    progress = int((processed_bytes / total_bytes) * 100)
                    if progress // 10 > last_progress // 10:
                        self.logger.debug(f"Parsing... {progress}%")
                        last_progress = progress

                    if block_type == 0x05:  # LINE
                        line, _ = parse_line(block_data, 0, CONVERSION_FACTOR, self.logger)
                        self.lines.append(line)
                    elif block_type == 0x01:  # ARC
                        arc, _ = parse_arc(block_data, 0, CONVERSION_FACTOR, self.logger)
                        self.arcs.append(arc)
                    elif block_type == 0x06:  # TEXT
                        text_element, _ = parse_text(block_data, 0, CONVERSION_FACTOR, self.logger)
                        if text_element:
                            self.text_elements.append(text_element)
                            self.text_stats["standalone"] += 1
                    elif block_type == 0x07:  # PART
                        parse_part_block(block_data, self.nets, self.parts, self.pins, CONVERSION_FACTOR, self.logger)
                    elif block_type == 0x02:  # VIA
                        # Simplified VIA parsing example
                        import struct
                        try:
                            values = struct.unpack_from("<7i", block_data, 0)
                            text = ""
                            if values[6] > 0:
                                text = translate_hex_string(block_data[28:])
                            from .models import XZZVia
                            via = XZZVia(
                                x=values[1] / CONVERSION_FACTOR,
                                y=values[2] / CONVERSION_FACTOR,
                                layer_a_radius=values[3] / CONVERSION_FACTOR,
                                layer_b_radius=values[4] / CONVERSION_FACTOR,
                                layer_a_type=values[5],
                                layer_b_type=values[6],
                                net_index=0,
                                text=text
                            )
                            self.vias.append(via)
                        except Exception as e:
                            self.logger.error(f"Error while parsing a VIA: {e}")
                except Exception as e:
                    self.logger.error(f"Error while parsing block type 0x{block_type:02X}: {str(e)}")
                    continue

            self.logger.debug("Parsing... 100%")

            # Parse post-v6 data (resistances, signals, etc.) after the nets block
            if self.net_block_start > 0:
                offset_nets = 0x20 + self.net_block_start
                # Compute the end of the nets block to find the start of the post-v6 data
                import struct
                net_block_size = struct.unpack('<I', decrypted_data[offset_nets:offset_nets+4])[0]
                post_v6_start = offset_nets + 4 + net_block_size
                if post_v6_start < len(decrypted_data):
                    post_v6_start, post_v6_data = parse_post_v6_block(decrypted_data, post_v6_start, self.logger)
                    self.post_v6_data = post_v6_data



            return True
        except Exception as e:
            self.error_msg = f"Error while parsing the decrypted data: {str(e)}"
            self.logger.error(self.error_msg, exc_info=True)
            return False

    def to_board(self) -> 'Board':  # noqa: F821 - fwd-ref; Board imported locally in body
        """
        Convert XZZFile into a normalized Board.

        XZZ has its own quirks:
        - nets is an indexed list (no direct names)
        - pins have RELATIVE positions (pos.x, pos.y) relative to the component
        - parts have absolute x, y
        """
        from core.models.board import Arc as NormalizedArc
        from core.models.board import Board, BoardSide, Component, MountType, Net, PinType
        from core.models.board import Line as NormalizedLine
        from core.models.board import Pin as NormalizedPin
        from core.models.board import Point as NormalizedPoint

        board = Board(format_type="xzz")

        # Copy the lines (traces + outlines)
        for line in self.lines:
            board.lines.append(NormalizedLine(
                x1=line.x1,
                y1=line.y1,
                x2=line.x2,
                y2=line.y2,
                layer=line.layer
            ))

        # Copy the arcs
        for arc in self.arcs:
            board.arcs.append(NormalizedArc(
                x1=arc.x1,
                y1=arc.y1,
                radius=arc.radius,
                angle_start=arc.angle_start,
                angle_end=arc.angle_end,
                layer=arc.layer
            ))

        # Copy the vias
        board.vias = self.vias.copy() if hasattr(self, 'vias') else []

        # Copy the text elements
        board.text_elements = self.text_elements.copy() if hasattr(self, 'text_elements') else []

        # Convert the nets (indexed list -> dict by index)
        # Note: self.nets can be sparse (contains None for undefined indices)
        nets_dict = {}  # net_index -> Net

        # Fetch the signal map if available (real net names)
        signal_map = {}
        if hasattr(self, 'post_v6_data') and self.post_v6_data:
            signal_map = self.post_v6_data.get('signal_map', {})
            if signal_map:
                self.logger.debug(f"[TO_BOARD] Signal map available: {len(signal_map)} mappings")

        for net_idx, net_obj in enumerate(self.nets):
            # Skip None entries (indices not defined in the XZZ file)
            if net_obj is None:
                continue

            net_name = getattr(net_obj, 'name', f"NET_{net_idx}")
            if isinstance(net_name, bytes):
                net_name = net_name.decode('utf-8', errors='replace')

            # Apply the signal map if available (e.g. Net973 -> PP3V3_G3H)
            original_name = net_name
            if net_name in signal_map:
                net_name = signal_map[net_name]
                self.logger.debug(f"[TO_BOARD] Net renamed: {original_name} -> {net_name}")

            net = Net(
                name=net_name,
                number=net_idx,
                is_ground=(net_name.upper() in ["GND", "GROUND"])
            )
            nets_dict[net_idx] = net
            board.nets.append(net)

        # Convert the components and pins
        for _part_idx, part in enumerate(self.parts):
            # Component name
            part_name = getattr(part, 'name', b'')
            if isinstance(part_name, bytes):
                part_name = part_name.decode('utf-8', errors='replace')

            # Mount type
            part_type_str = getattr(part, 'part_type', 'SMD')
            mount_type = MountType.SMD if part_type_str == "SMD" else MountType.THROUGH_HOLE

            # Side
            mounting_side = getattr(part, 'mounting_side', 'TOP')
            if mounting_side == "TOP":
                board_side = BoardSide.TOP
            elif mounting_side == "BOTTOM":
                board_side = BoardSide.BOTTOM
            else:
                board_side = BoardSide.BOTH

            # Create the component
            component = Component(
                name=part_name,
                mfgcode="",
                mount_type=mount_type,
                board_side=board_side,
                center=NormalizedPoint(getattr(part, 'x', 0), getattr(part, 'y', 0)),
                rotation=getattr(part, 'rotation', 0.0)
            )

            # Copy the component's outline lines (XZZ)
            if hasattr(part, 'lines') and part.lines:
                for line in part.lines:
                    component.lines.append(NormalizedLine(
                        x1=line.x1,
                        y1=line.y1,
                        x2=line.x2,
                        y2=line.y2,
                        layer=0
                    ))

            board.components.append(component)

            # Convert this component's pins
            if hasattr(part, 'pins') and part.pins:
                for old_pin in part.pins:
                    # XZZ: ABSOLUTE position in pin.pos (already pre-transformed)
                    pin_pos = getattr(old_pin, 'pos', None)
                    if not pin_pos:
                        continue

                    # Position already ABSOLUTE - use directly
                    pin_x = getattr(pin_pos, 'x', 0)
                    pin_y = getattr(pin_pos, 'y', 0)

                    # Pin number
                    pin_number = getattr(old_pin, 'snum', None)
                    if not pin_number:
                        pin_name_bytes = getattr(old_pin, 'name', b'')
                        if isinstance(pin_name_bytes, bytes):
                            pin_number = pin_name_bytes.decode('utf-8', errors='replace')
                    if not pin_number:
                        pin_number = str(len(component.pins) + 1)

                    # Pin type (test pad if dummy)
                    is_dummy = getattr(part, 'part_type', '') == "TEST_PAD"
                    pin_type = PinType.TEST_PAD if is_dummy else PinType.COMPONENT

                    # Pin side
                    mirror = getattr(part, 'mirror', False)
                    pin_board_side = BoardSide.BOTTOM if mirror else BoardSide.TOP

                    # Pin net (via net_index)
                    net_index = getattr(old_pin, 'net_index', 0)
                    pin_net = nets_dict.get(net_index, None)

                    # Dimensions
                    width = getattr(old_pin, 'width', None)
                    height = getattr(old_pin, 'height', None)
                    diameter = min(width, height) if (width and height) else 0.5

                    # Create the normalized pin with ABSOLUTE position
                    new_pin = NormalizedPin(
                        position=NormalizedPoint(pin_x, pin_y),
                        number=pin_number,
                        diameter=diameter,
                        pin_type=pin_type,
                        board_side=pin_board_side,
                        net=pin_net,
                        component=component,
                        width=width,
                        height=height,
                        rotation=getattr(old_pin, 'rotation', 0.0),
                        shape_type=getattr(old_pin, 'shape_type', 0)
                    )

                    # Add the pin to the component, the board, and the net
                    component.pins.append(new_pin)
                    board.pins.append(new_pin)
                    if pin_net:
                        pin_net.pins.append(new_pin)

        # Convert the outline
        if hasattr(self, 'outline_segments') and self.outline_segments:
            for seg in self.outline_segments:
                board.outline_segments.append((
                    NormalizedPoint(seg[0].x, seg[0].y),
                    NormalizedPoint(seg[1].x, seg[1].y)
                ))

        # Build the indices
        board.build_indices()

        # Compute the dimensions
        board.calculate_dimensions()

        self.logger.debug(f"Converted to normalized Board: {len(board.components)} components, {len(board.pins)} pins, {len(board.nets)} nets")

        return board

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python xzz_file.py <pcb_file>")
        sys.exit(1)
    pcb_file = XZZFile()
    if pcb_file.load(sys.argv[1]):
        print("XZZ file loaded successfully.")
    else:
        print(f"Error while loading the file: {pcb_file.error_msg}")
