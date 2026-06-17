#!/usr/bin/env python3
"""
Unified PCB Parsing Code with Detailed Logging for Unit Conversion

- All positions in the source file are assumed to be in µm.
- For display, convert to mm by dividing by MU_TO_MM (1 mm = 1000 µm).
- Detailed logs are added to show the raw and converted values.
"""

import json
import struct

from .models import (
    Net,
    Pin,
    Point,
    XZZArc,
    XZZBlockType,
    XZZLine,
    XZZPart,
    XZZTestPad,
    XZZText,
)
from .utils import read_bytes, read_int32, read_uint16, read_uint32, translate_hex_string

# Single constant to convert raw values to mm.
# Values in the XZZ file appear to be in nm (nanometres), or 0.001 µm.
# Observed factor: divide by 1000000 to get mm.
MU_TO_MM = 1000000.0

# -----------------------------------------------------------
# Header and block parsing functions
# -----------------------------------------------------------
def parse_header(data: bytes, logger):
    try:
        signature = data[:11].decode('ascii', errors='ignore')
        if not signature.startswith('XZZ'):
            logger.error(f"[HEADER] Invalid signature: {signature}")
            return False, {}
        offset = 0x20
        header_values = []
        for i in range(3):
            val, offset = read_uint32(data, offset)
            header_values.append(val)
            logger.debug(f"[HEADER] Raw value {i}: {val}")
        header_info = {
            'image_block_start': header_values[1],
            'net_block_start': header_values[2]
        }
        main_data_blocks_size, _ = read_uint32(data, 0x40)
        header_info['main_data_blocks_size'] = main_data_blocks_size
        logger.debug(f"[HEADER] Parsed: image_block_start=0x{header_info['image_block_start']:X}, "
                     f"net_block_start=0x{header_info['net_block_start']:X}, "
                     f"main_data_blocks_size={main_data_blocks_size}")
        return True, header_info
    except Exception as e:
        logger.error(f"[HEADER] Error: {str(e)}")
        return False, {}

def parse_blocks_generator(data: bytes, start_offset: int, end_offset: int, block_counts: dict, logger):
    offset = start_offset
    while offset < end_offset:
        try:
            block_type = data[offset]
            offset += 1
            block_size, offset = read_uint32(data, offset)
            block_counts[block_type] = block_counts.get(block_type, 0) + 1
            block_data = data[offset:offset + block_size]
            logger.debug(f"[BLOCK] Type 0x{block_type:02X} of size {block_size} bytes at offset 0x{offset:X}")
            if block_type in (XZZBlockType.ARC, XZZBlockType.VIA, XZZBlockType.LINE,
                              XZZBlockType.TEXT, XZZBlockType.PART, XZZBlockType.TEST_PAD):
                if block_type == XZZBlockType.TEST_PAD:
                    logger.debug("[BLOCK] TEST_PAD block detected")
                yield block_type, block_data, offset
            else:
                logger.debug(f"[BLOCK] Unknown block: type 0x{block_type:02X}, size={block_size}")
                logger.debug(f"[BLOCK] Data (hex): {block_data.hex()[:64]}...")
            offset += block_size
        except Exception as e:
            logger.error(f"[BLOCK] Error while parsing block type 0x{block_type:02X} at offset 0x{offset:X}: {str(e)}")
            offset += block_size
            continue

# -----------------------------------------------------------
# Graphical-element parsing functions
# -----------------------------------------------------------
def parse_line(data: bytes, offset: int, conversion_factor: float, logger):
    try:
        values = struct.unpack_from('<7i', data, offset)
        logger.debug(f"[LINE] Raw values: {values}")
        offset += 28
        line = XZZLine(
            layer=values[0],
            x1=values[1] / conversion_factor,
            y1=values[2] / conversion_factor,
            x2=values[3] / conversion_factor,
            y2=values[4] / conversion_factor,
            scale=values[5] / conversion_factor,
            net_index=values[6]
        )
        logger.debug(f"[LINE] Converted: layer={line.layer}, start=({line.x1:.2f} mm, {line.y1:.2f} mm), "
                     f"end=({line.x2:.2f} mm, {line.y2:.2f} mm)")
        return line, offset
    except Exception as e:
        logger.error(f"[LINE] Error at offset 0x{offset:X}: {str(e)}")
        raise

def parse_arc(data: bytes, offset: int, conversion_factor: float, logger):
    try:
        values = struct.unpack_from('<8i', data, offset)
        logger.debug(f"[ARC] Raw values: {values}")
        logger.debug(f"[ARC] Raw angles: start={values[4]}, end={values[5]}")
        offset += 32

        # Angles are stored in 1/10000th of a degree,
        # but they may need to be normalized differently.
        angle_start_raw = values[4]
        angle_end_raw = values[5]

        # Try with 10000.0 (values in 1/10000 of a degree)
        angle_start = angle_start_raw / 10000.0
        angle_end = angle_end_raw / 10000.0

        # Normalize the angles between 0 and 360
        angle_start = angle_start % 360
        angle_end = angle_end % 360

        arc = XZZArc(
            layer=values[0],
            x1=values[1] / conversion_factor,
            y1=values[2] / conversion_factor,
            radius=values[3] / conversion_factor,
            angle_start=angle_start,
            angle_end=angle_end,
            scale=values[6] / conversion_factor
        )
        logger.debug(f"[ARC] Converted: center=({arc.x1:.2f} mm, {arc.y1:.2f} mm), "
                     f"radius={arc.radius:.2f} mm, angles={arc.angle_start:.2f}° -> {arc.angle_end:.2f}°")
        return arc, offset
    except Exception as e:
        logger.error(f"[ARC] Error at offset 0x{offset:X}: {str(e)}")
        raise

def parse_text(data: bytes, offset: int, conversion_factor: float, logger):
    try:
        logger.debug(f"[TEXT] Starting parse at offset 0x{offset:X}, block size = {len(data)} bytes")
        if len(data) < 36:
            logger.warning("[TEXT] Block too short")
            return None, offset + len(data)
        values = struct.unpack_from('<8I', data, offset)
        pos_x, pos_y, text_size = values[1], values[2], values[3]
        layer = values[0]
        offset += 32
        one, offset = read_uint16(data, offset)
        logger.debug(f"[TEXT] 'one' value: {one}")
        text_length, offset = read_uint32(data, offset)
        logger.debug(f"[TEXT] Raw text length: {text_length}")
        text_element = None
        if text_length > len(data[offset:]):
            logger.warning("[TEXT] Text length > remaining data")
            return None, offset + len(data[offset:])
        if text_length > 0:
            text, offset = read_bytes(data, offset, text_length)
            decoded_text = translate_hex_string(text)
            text_element = XZZText(
                text=text,
                x=pos_x / conversion_factor,
                y=pos_y / conversion_factor,
                layer=layer,
                font_size=text_size / conversion_factor if text_size > 0 else 1.0,
                font_scale=1.0,
                visibility=True,
                source='standalone'
            )
            logger.debug(f"[TEXT] Converted: '{decoded_text}' at ({pos_x/conversion_factor:.2f} mm, {pos_y/conversion_factor:.2f} mm), layer={layer}")
        else:
            logger.debug("[TEXT] Text skipped (zero length)")
        return text_element, offset
    except Exception as e:
        logger.error(f"[TEXT] Error at offset 0x{offset:X}: {str(e)}")
        logger.debug(f"[TEXT] Data (hex): {data.hex()[:64]}...")
        return None, offset + len(data)

def parse_test_pad_block(data: bytes, offset: int, nets: list, parts: list, pins: list, conversion_factor: float, logger):
    try:
        block_size, offset = read_uint32(data, offset)
        logger.debug(f"[TEST_PAD] Starting test pad parse at offset {hex(offset)} - block of {block_size} bytes")

        # Read position data
        x_origin, offset = read_uint32(data, offset)
        y_origin, offset = read_uint32(data, offset)

        # Read name data
        name_bytes, offset = read_bytes(data, offset, 4)

        # Read net index
        net_index, offset = read_uint32(data, offset)

        # Read shape data (32 bytes)
        shape_data = data[offset:offset+32]
        offset += 32

        # Parse shape data to get dimensions
        shape_type, width, height, rotation = parse_pin_shape(shape_data, conversion_factor, logger)

        # Create part
        part = XZZPart()
        part.x = x_origin / (conversion_factor * 10)
        part.y = y_origin / (conversion_factor * 10)
        part.name = name_bytes
        part.part_type = "TEST_PAD"
        part.mounting_side = "TOP"
        part.category = "TP"  # Test Pad category

        # Create pin with proper dimensions
        pin = Pin()
        pin.name = name_bytes
        pin.snum = name_bytes.decode('utf-8', errors='replace')
        pin.side = "TOP"
        pin.pos = Point(x_origin / (conversion_factor * 10), y_origin / (conversion_factor * 10))
        pin.shape_type = shape_type
        pin.width = width
        pin.height = height
        pin.rotation = rotation

        # Set net information
        if net_index < len(nets):
            net_obj = nets[net_index]
            if net_obj is not None:
                # net_obj is a Net object; access its .name attribute
                pin.net = "" if net_obj.name in ("UNCONNECTED", "NC") else net_obj.name
            else:
                pin.net = ""
        else:
            pin.net = ""

        pin.part_index = len(parts)
        pins.append(pin)
        part.pins = [pin]
        parts.append(part)

        logger.debug(f"[TEST_PAD] Part converted: name='{part.name.decode('utf-8', errors='replace')}', "
                     f"position=({part.x:.2f} mm, {part.y:.2f} mm), net='{pin.net}', "
                     f"width={pin.width:.2f} mm, height={pin.height:.2f} mm, rotation={pin.rotation:.2f}°")
    except Exception as e:
        logger.error(f"[TEST_PAD] Error: {str(e)}")
        logger.debug(f"[TEST_PAD] Raw data (first 32 bytes): {data[offset:offset+32].hex()}")
    return offset

def parse_nets(data: bytes, offset: int, nets: list, logger):
    try:
        block_size, offset = read_uint32(data, offset)
        logger.debug(f"[NETS] Starting nets parse at offset {hex(offset)} - block of {block_size} bytes")

        end_offset = offset + block_size
        net_count = 0

        while offset < end_offset:
            # Log the current offset for debugging
            logger.debug(f"[NETS] Parsing net at offset {hex(offset)}")

            # Read the net size and its index
            net_size, offset = read_uint32(data, offset)
            net_index, offset = read_uint32(data, offset)

            # Read and decode the net name
            net_name_bytes, offset = read_bytes(data, offset, net_size - 8)
            net_name = net_name_bytes.decode('utf-8', errors='replace').strip()

            # Create a new Net object
            new_net = Net(
                index=net_index,
                name=net_name
            )

            # Detailed log of the net info
            logger.debug(f"[NETS] Net {net_index}:")
            logger.debug(f"  - Size: {net_size} bytes")
            logger.debug(f"  - Name: '{net_name}'")
            logger.debug(f"  - Next offset: {hex(offset)}")

            # Extend the list if needed
            if net_index >= len(nets):
                logger.debug(f"[NETS] Extending the net list from {len(nets)} to {net_index + 1}")
                nets.extend([None] * (net_index - len(nets) + 1))

            # Store the net
            nets[net_index] = new_net
            net_count += 1

        logger.debug(f"[NETS] Finished parsing nets: {net_count} nets found")
        logger.debug(f"[NETS] First net: {nets[1].name if len(nets) > 1 else 'None'}")
        return offset

    except Exception as e:
        logger.error(f"[NETS] Error while parsing nets at offset {hex(offset)}: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return offset

def parse_post_v6_block(data: bytes, offset: int, logger):
    """
    Parse the post-v6 data (voltage, resistance, signals, etc.) found
    after the v6v6555v6v6=== pattern.

    Possible sections (GB2312 markers):
    - 阻值 (D7E8D6B5): Resistance - Format: =value=component(pin)
    - 电压 (B5E7D1B9): Voltage - Format: netName=voltage
    - 信号 (D0C5BAC5): Signal - Format: oldNetName=realSignalName (real names!)
    - 菜单 (B2CBB5A5): Menu - JSON data
    """
    post_v6_data = {
        'resistance': [],
        'voltage': [],
        'signals': [],      # Mapping netName -> signalName (real names!)
        'signal_map': {},   # Dict for fast lookup: net_name -> signal_name
        'menu': [],
        'params': [],
        'resistance_diagram': []
    }

    # GB2312 section markers
    MARKERS = {
        'resistance': bytes.fromhex('D7E8D6B5'),  # 阻值
        'voltage': bytes.fromhex('B5E7D1B9'),     # 电压
        'signal': bytes.fromhex('D0C5BAC5'),      # 信号
        'menu': bytes.fromhex('B2CBB5A5'),        # 菜单
    }

    try:
        # Base pattern: v6v6555v6v6===
        BASE_PATTERN = b'v6v6555v6v6==='

        # Search for the pattern from the start of the data
        pattern_pos = data.find(BASE_PATTERN)

        if pattern_pos == -1:
            logger.debug("[POST_V6] Pattern v6v6555v6v6 not found, no post-v6 data")
            return offset, post_v6_data

        logger.debug(f"[POST_V6] Pattern v6v6555v6v6 found at position {pattern_pos} (0x{pattern_pos:X})")

        # Extract all data after the base pattern
        post_v6_raw = data[pattern_pos:]

        logger.debug(f"[POST_V6] Post-v6 data: {len(post_v6_raw)} bytes")

        # Detect the sections present
        sections_found = []
        for name, marker in MARKERS.items():
            pos = post_v6_raw.find(marker)
            if pos != -1:
                sections_found.append((name, pos))
                logger.debug(f"[POST_V6] Section '{name}' found at offset {pos}")

        # Sort by position
        sections_found.sort(key=lambda x: x[1])

        if not sections_found:
            logger.warning("[POST_V6] No recognized section found")
            return len(data), post_v6_data

        # Parse each section
        for i, (section_name, section_start) in enumerate(sections_found):
            # Determine the end of the section (start of the next one or end of the data)
            if i + 1 < len(sections_found):
                section_end = sections_found[i + 1][1]
            else:
                section_end = len(post_v6_raw)

            # Extract the section data (after the 4-byte marker)
            section_data = post_v6_raw[section_start + 4:section_end]

            try:
                section_text = section_data.decode('gb2312', errors='replace')
            except Exception:
                section_text = section_data.decode('utf-8', errors='replace')

            lines = section_text.split('\n')

            if section_name == 'resistance':
                _parse_resistance_section(lines, post_v6_data, logger)
            elif section_name == 'voltage':
                _parse_voltage_section(lines, post_v6_data, logger)
            elif section_name == 'signal':
                _parse_signal_section(lines, post_v6_data, logger)
            elif section_name == 'menu':
                _parse_menu_section(section_text, post_v6_data, logger)

        # Summary
        logger.debug("[POST_V6] Data extracted:")
        logger.debug(f"  - Resistances: {len(post_v6_data['resistance'])}")
        logger.debug(f"  - Voltages: {len(post_v6_data['voltage'])}")
        logger.debug(f"  - Signals (real names): {len(post_v6_data['signals'])}")
        logger.debug(f"  - Menu entries: {len(post_v6_data['menu'])}")

        return len(data), post_v6_data

    except Exception as e:
        logger.error(f"[POST_V6] Error: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return offset, post_v6_data


def _parse_resistance_section(lines: list, post_v6_data: dict, logger):
    """Parse the resistance section: =value=component(pin)"""
    for line in lines:
        line = line.strip()
        if not line or not line.startswith('='):
            continue

        # Format: =VALUE=COMPONENT(PIN)
        # Example: =711=N485(D9)
        if '=' in line[1:]:
            parts = line[1:].split('=', 1)
            if len(parts) == 2:
                resistance_value = parts[0].strip()
                component_pin = parts[1].strip()

                if '(' in component_pin and ')' in component_pin:
                    component = component_pin[:component_pin.find('(')].strip()
                    pin = component_pin[component_pin.find('(')+1:component_pin.find(')')].strip()

                    post_v6_data['resistance'].append({
                        'part': component,
                        'pin': pin,
                        'value': resistance_value
                    })


def _parse_voltage_section(lines: list, post_v6_data: dict, logger):
    """Parse the voltage section: netName=voltage"""
    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Format: NET_NAME=VOLTAGE_VALUE
        # Example: PP3V3_G3H=3.3V or GND=0
        if '=' in line:
            parts = line.split('=', 1)
            if len(parts) == 2:
                net_name = parts[0].strip()
                voltage_value = parts[1].strip()

                if net_name and voltage_value:
                    post_v6_data['voltage'].append({
                        'net': net_name,
                        'voltage': voltage_value
                    })
                    logger.debug(f"[POST_V6] Voltage: {net_name} = {voltage_value}")


def _parse_signal_section(lines: list, post_v6_data: dict, logger):
    """
    Parse the signal section: oldNetName=realSignalName
    This is where the REAL signal names are found!
    """
    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Format: OLD_NET_NAME=REAL_SIGNAL_NAME
        # Example: Net973=PP3V3_G3H or Net2=VCCIO_CPU
        if '=' in line:
            parts = line.split('=', 1)
            if len(parts) == 2:
                old_name = parts[0].strip()
                real_name = parts[1].strip()

                if old_name and real_name:
                    post_v6_data['signals'].append({
                        'original': old_name,
                        'signal_name': real_name
                    })
                    # Mapping for fast lookup
                    post_v6_data['signal_map'][old_name] = real_name
                    logger.debug(f"[POST_V6] Signal: {old_name} -> {real_name}")


def _parse_menu_section(section_text: str, post_v6_data: dict, logger):
    """Parse the menu section (usually JSON)"""

    # Look for JSON in the text
    try:
        # Find the braces
        start = section_text.find('{')
        end = section_text.rfind('}')

        if start != -1 and end != -1 and end > start:
            json_str = section_text[start:end+1]
            menu_data = json.loads(json_str)
            post_v6_data['menu'].append(menu_data)
            logger.debug(f"[POST_V6] Menu JSON parsed: {len(json_str)} chars")
    except json.JSONDecodeError as e:
        logger.debug(f"[POST_V6] Menu non-JSON or invalid: {e}")
    except Exception as e:
        logger.debug(f"[POST_V6] Error parsing menu: {e}")

def parse_images(data: bytes, offset: int, logger):
    try:
        start_offset = offset
        block_size, offset = read_uint32(data, offset)
        end_offset = start_offset + block_size
        logger.debug(f"[IMAGES] Image block: size={block_size} bytes, offset={start_offset} -> {end_offset}")
        image_count = 0
        images = []
        while offset < end_offset:
            if offset + 3 > len(data):
                logger.error("[IMAGES] Insufficient data for the image header")
                return offset, images
            type_byte = data[offset]
            index_byte = data[offset + 1]
            flag_byte = data[offset + 2]
            offset += 3
            width, offset = read_uint32(data, offset)
            height, offset = read_uint32(data, offset)
            name_length, offset = read_uint32(data, offset)
            if offset + name_length > len(data):
                logger.error(f"[IMAGES] Insufficient data for the name of image {image_count+1}")
                return offset, images
            name, offset = read_bytes(data, offset, name_length)
            image_name = translate_hex_string(name)
            logger.debug(f"[IMAGES] Image {image_count+1}: type=0x{type_byte:02X}, index={index_byte}, flag=0x{flag_byte:02X}, "
                         f"dimensions={width}x{height}, name='{image_name}'")
            images.append({'type': type_byte, 'index': index_byte, 'flag': flag_byte, 'width': width, 'height': height, 'name': image_name})
            image_count += 1
        logger.debug(f"[IMAGES] Finished parsing images: {image_count} images found")
        return offset, images
    except Exception as e:
        logger.error(f"[IMAGES] Error: {str(e)}")
        raise

# -----------------------------------------------------------
# Shape- and component-specific functions
# -----------------------------------------------------------
# For pins, the XZZ format sometimes uses a specific factor.
# For now we keep the conversion as-is.
CONVERSION_FACTOR_FOR_PINS = 10000.0

def parse_pin_shape(shape_raw: bytes, conversion_factor: float, logger) -> tuple:
    """
    Parse 32 bytes of pin shape data and convert the width and height to mm.

    Rotation contract: returns 0.0 by default (= "no per-pin rotation
    encoded in the shape block, fall back to part rotation upstream").
    The shape block in XZZ stores only geometry; the per-pin rotation
    overlay is encoded separately in flip_flag8bit and only applies for
    the non-standard 29-30° special cases handled in the part parsing
    branch. The previous 90° default broke the upstream sentinel
    `if pin_rot == 0.0: pin_rot = part_rotation` in xzz.py: every pin
    got stuck at 90° regardless of its package orientation.
    """
    if len(shape_raw) != 32:
        logger.warning(f"[PIN_SHAPE] Incorrect length: {len(shape_raw)} bytes (expected 32)")
        return 0, 0.0, 0.0, 0.0
    try:
        values_int = struct.unpack('<8i', shape_raw)
        logger.debug(f"[PIN_SHAPE] Raw values: {values_int}")
        shape_type = values_int[1]
        raw_height = values_int[2]
        raw_width = values_int[3]
        # Conversion based on analysis of the XZZ format:
        # Pin dimensions use a different factor than positions.
        # Factor: 100000000 (100 million) to get realistic dimensions (0.1-1 mm)
        width_mm = raw_width / 100_000_000.0
        height_mm = raw_height / 100_000_000.0
        rotation_deg = 0.0
        logger.debug(f"[PIN_SHAPE] Converted: type={shape_type}, width={width_mm:.3f} mm, height={height_mm:.3f} mm, rotation={rotation_deg}°")
        return shape_type, width_mm, height_mm, rotation_deg
    except Exception as e:
        logger.error(f"[PIN_SHAPE] Error: {str(e)}")
        return 0, 0.0, 0.0, 0.0

def parse_part_block(data: bytes, nets: list, parts: list, pins: list, conversion_factor: float, logger):
    current_pointer = 0
    from .models import Pin, XZZPart
    part = XZZPart()
    part_size, current_pointer = read_uint32(data, current_pointer)
    logger.debug(f"[PART] Starting parse, block size = {part_size} bytes")
    if part_size > len(data) or part_size < 26:
        logger.error(f"[PART] Invalid size: {part_size} (buffer size: {len(data)})")
        return
    block_end = current_pointer - 4 + part_size
    if block_end > len(data):
        logger.error(f"[PART] Block end {block_end} exceeds the buffer size {len(data)}")
        return
    # Read the position (convert from µm to mm)
    val1, current_pointer = read_uint32(data, current_pointer)  # Padding/flags (ImHex: padding[4])
    part.rotation = 0
    x_val, current_pointer = read_uint32(data, current_pointer)
    part.x = x_val / MU_TO_MM
    y_val, current_pointer = read_uint32(data, current_pointer)
    part.y = y_val / MU_TO_MM
    # --- PART ROTATION (New Logic) ---
    # ImHex & Diff confirm: 4 bytes after Y are Part Rotation, then 1 byte visibility, then 1 byte padding.
    # Total 6 bytes to skip to align with next field (Name).

    part_rotation_bytes, current_pointer = read_bytes(data, current_pointer, 4)
    part_rot_int = struct.unpack('<I', part_rotation_bytes)[0]

    # Check for valid rotation (multiples of 10000 or close)
    # We accept 0 as valid.
    if part_rot_int > 0:
         part.rotation = (part_rot_int / 10000.0) % 360.0
    else:
         part.rotation = 0.0

    # Skip 2 bytes (Visibility + Padding)
    current_pointer += 2

    # DEBUG: Store raw header bytes for analysis
    part._debug_rotation_bytes = part_rotation_bytes.hex()
    part._debug_header_start = data[:24].hex() if len(data) >= 24 else data.hex()

    # NOTE: Legacy 'flags' logic removed because it was reading the first 2 bytes of rotation.

    # Note: Logic continues to name parsing

    # Read the group name
    group_name_size, current_pointer = read_uint32(data, current_pointer)
    group_name_bytes, current_pointer = read_bytes(data, current_pointer, group_name_size)
    decoded_group_name = translate_hex_string(group_name_bytes)
    initial_group_name = decoded_group_name
    logger.debug(f"[PART] Group name: '{initial_group_name}'")
    part.group_name = initial_group_name
    sub_block_count = 0
    part_lines = []
    part_pins = []
    while current_pointer < block_end:
        if current_pointer >= len(data):
            logger.error(f"[PART] Buffer overflow at offset {current_pointer}")
            break
        sub_type_identifier = data[current_pointer]
        current_pointer += 1
        if current_pointer + 4 > len(data):
            logger.error(f"[PART] Buffer overflow while reading the sub-block size at {current_pointer}")
            break
        logger.debug(f"[PART] Sub-block {sub_block_count}: type 0x{sub_type_identifier:02X}")
        if sub_type_identifier == 0x06:
            block_size, current_pointer = read_uint32(data, current_pointer)
            logger.debug(f"[PART] Name block: size = {block_size} bytes")
            if block_size > 31:
                # Read the first 31 bytes (header)
                header_bytes, current_pointer = read_bytes(data, current_pointer, 31)

                # Extract the alphabetic prefix from the end of the header
                prefix = b""
                for i in range(len(header_bytes) - 1, -1, -1):
                    char = bytes([header_bytes[i]])
                    if char.decode('utf-8', errors='ignore').isalpha():
                        prefix = char + prefix
                    else:
                        break

                # Read the rest of the name (number)
                effective_name, current_pointer = read_bytes(data, current_pointer, block_size - 31)

                # Some XZZ exports (stripped-refdes flavour) ship TWO
                # 0x06 sub-blocks per part: the first carries the real
                # refdes (J1, C5, …) and the second a placeholder (U1 /
                # TEST_PAD_U1). Keep the FIRST one: overwriting with the
                # second leaves every part named U1 on those boards.
                # Other XZZ flavours ship a single 0x06 so the gate is a
                # no-op.
                if not getattr(part, "_name_set", False):
                    # Assemble the full name with prefix
                    part.name = prefix + effective_name

                    # Extract and store the category (prefix only)
                    part.category = prefix.decode('utf-8', errors='ignore').upper() if prefix else ""

                    # Group name for compatibility
                    initial_group_name = translate_hex_string(part.name)

                    logger.debug(f"[PART] Extracted name: '{translate_hex_string(part.name)}', Category: '{part.category}'")
                    part._name_set = True
                else:
                    logger.debug(f"[PART] Sub-block 0x06 skipped (placeholder after the real name): prefix={prefix!r}, eff={effective_name!r}")
            else:
                _, current_pointer = read_bytes(data, current_pointer, block_size)
                if not getattr(part, "_name_set", False):
                    part.category = ""
        elif sub_type_identifier == 0x01:
            block_size, current_pointer = read_uint32(data, current_pointer)
            _, current_pointer = read_bytes(data, current_pointer, block_size)
        elif sub_type_identifier == 0x05:
            block_size, current_pointer = read_uint32(data, current_pointer)
            num_segments = block_size // 28
            for _ in range(num_segments):
                values = struct.unpack_from('<7i', data, current_pointer)
                current_pointer += 28
                line = XZZLine(
                    layer=values[0],
                    x1=values[1] / conversion_factor,
                    y1=values[2] / conversion_factor,
                    x2=values[3] / conversion_factor,
                    y2=values[4] / conversion_factor,
                    scale=values[5] / conversion_factor,
                    net_index=values[6]
                )
                part_lines.append(line)
                logger.debug(f"[PART] Line segment: layer={line.layer}, start=({line.x1:.2f} mm, {line.y1:.2f} mm), "
                             f"end=({line.x2:.2f} mm, {line.y2:.2f} mm)")
            logger.debug(f"[PART] Added {num_segments} segments")
        elif sub_type_identifier == 0x09:
            pin = Pin()
            pin_block_size, current_pointer = read_uint32(data, current_pointer)
            pin_block_end = current_pointer + pin_block_size
            logger.debug(f"[PART] Pin block: size = {pin_block_size} bytes")
            pin_layer, current_pointer = read_int32(data, current_pointer)
            pin.layer = pin_layer

            # Read the absolute position
            pin_pos_x, current_pointer = read_uint32(data, current_pointer)
            pin_pos_y, current_pointer = read_uint32(data, current_pointer)

            # XZZ: Positions are PRE-TRANSFORMED (absolute)
            # We store the absolute coordinates directly, no relative conversion,
            # because the renderer applies no rotation to components (rotation=0)
            abs_x = pin_pos_x / MU_TO_MM
            abs_y = pin_pos_y / MU_TO_MM

            # Store ABSOLUTE position (used directly by to_board)
            pin.pos.x = abs_x
            pin.pos.y = abs_y

            logger.debug(f"[PART] Pin absolute position: ({abs_x:.3f}, {abs_y:.3f}) mm")

            flip_flag8bit, current_pointer = read_bytes(data, current_pointer, 8)
            pin.flip_flag8bit = flip_flag8bit.hex()
            logger.debug(f"[PART] Pin flip flag (8 bytes): {pin.flip_flag8bit}")
            pin_name_size, current_pointer = read_uint32(data, current_pointer)
            pin_name_bytes, current_pointer = read_bytes(data, current_pointer, pin_name_size)
            pin.name = pin_name_bytes
            pin.snum = translate_hex_string(pin.name)
            shape_raw, current_pointer = read_bytes(data, current_pointer, 32)
            pin.raw_shape_data = shape_raw.hex()
            logger.debug(f"[PART] Pin raw shape data: {pin.raw_shape_data}")
            pin.shape_type, pin.width, pin.height, base_rotation = parse_pin_shape(shape_raw, conversion_factor, logger)

            # XZZ: Pre-transformed positions
            # Store the original dimensions; the swap is applied later depending on the rotation type
            pin.rotation = 0.0
            pin._original_width = pin.width
            pin._original_height = pin.height
            try:
                flip_bytes = bytes.fromhex(pin.flip_flag8bit)
                if len(flip_bytes) >= 8:
                    raw_rotation = struct.unpack('<I', flip_bytes[4:8])[0]
                    rotation_deg = (raw_rotation / 10000.0) % 360.0

                    # Swap width/height if close to 0° or 180° (for STANDARD rotations)
                    if (rotation_deg < 45) or (135 < rotation_deg < 225) or (rotation_deg > 315):
                        pin.width, pin.height = pin.height, pin.width
            except Exception:
                pass

            # --- NET INDEX AND TAIL DATA ---
            # The net_index is at the START of the remaining data (first 4 bytes)
            net_index, current_pointer = read_uint32(data, current_pointer)

            # The rest is tail_data (usually 4 more bytes)
            remaining_len = pin_block_end - current_pointer
            if remaining_len > 0:
                logger.debug(f"[PART] Pin tail detected: {remaining_len} bytes")
                pin_tail, current_pointer = read_bytes(data, current_pointer, remaining_len)
                pin.tail_data = pin_tail.hex()
            else:
                pin.tail_data = ""
            # --- END ANALYSIS ---

            logger.debug(f"[PART] Pin: rotation={pin.rotation:.1f}°, w={pin.width:.3f}, h={pin.height:.3f}")

            current_pointer = pin_block_end

            # Always assign net_index, even with no matching net
            pin.net_index = net_index

            # Assign the net name if available
            if net_index < len(nets):
                pin_net = nets[net_index]
                if pin_net is not None:
                    # pin_net is a Net object; access its .name attribute
                    pin.net = "UNCONNECTED" if pin_net.name == "NC" else pin_net.name
                else:
                    pin.net = ""
            else:
                pin.net = ""

            pin.part_index = len(parts)
            pin.side = part.mounting_side
            part_pins.append(pin)  # Add the pin only once
            logger.debug(f"[PART] Pin added: pos=({pin.pos.x:.3f} mm, {pin.pos.y:.3f} mm), name={pin.snum}, net_index={net_index}, net='{pin.net}'")
        else:
            if sub_type_identifier != 0x00:
                part_name_decoded = translate_hex_string(part.name) if part.name else "Unknown"
                logger.warning(f"[PART] Unknown sub-block: 0x{sub_type_identifier:02X} at offset {current_pointer} in {part_name_decoded}")
            break
        sub_block_count += 1

    # ROTATION FIX: Handle NON-STANDARD rotations (not 0/90/180/270)
    # ONLY if part.rotation == 0 (rotation not defined in the header)

    # Get the rotation from the pins if available
    rotation_deg = None
    if part.rotation == 0 and part_pins:  # Only if rotation is undefined
        pin_rotations_raw = []
        for pin in part_pins:
            flip = getattr(pin, 'flip_flag8bit', '')
            if flip:
                try:
                    flip_bytes = bytes.fromhex(flip)
                    if len(flip_bytes) >= 8:
                        raw_rotation = struct.unpack('<I', flip_bytes[4:8])[0]
                        if raw_rotation > 0:
                            pin_rotations_raw.append(raw_rotation)
                except Exception:
                    pass

        if pin_rotations_raw and all(r == pin_rotations_raw[0] for r in pin_rotations_raw):
            raw_val = pin_rotations_raw[0]
            rotation_deg = (raw_val / 10000.0) % 360.0

    # Apply the fix ONLY for rotation ~40° (special diagonal components)
    # The formula was derived specifically for this case
    if rotation_deg is not None and 35 <= rotation_deg <= 45:
        # Formula derived from tests for rotation ~40°:
        # Part = rotation_deg - 5
        # Pins = -(rotation_deg + 15)
        part.rotation = rotation_deg - 5.0
        pin_rotation = -(rotation_deg + 15.0)
        logger.debug(f"[PART] NON-STANDARD rotation (~40°): part={part.rotation:.2f}°, pins={pin_rotation:.2f}° (raw={rotation_deg:.2f}°)")

        # Apply negative rotation to the pins AND restore original dimensions (no swap)
        for pin in part_pins:
            pin.rotation = pin_rotation
            # Restore original dimensions (undo the swap)
            pin.width = getattr(pin, '_original_width', pin.width)
            pin.height = getattr(pin, '_original_height', pin.height)

    logger.debug(f"[PART] Final rotation: {part.rotation}°")

    # Associate line segments with pins (tolerance in mm)
    TOLERANCE = 1.0
    for pin in part_pins:
        pin.lines = []
        pin_x, pin_y = pin.pos.x, pin.pos.y
        for line in part_lines:
            if (min(line.x1, line.x2) - TOLERANCE <= pin_x <= max(line.x1, line.x2) + TOLERANCE and
                min(line.y1, line.y2) - TOLERANCE <= pin_y <= max(line.y1, line.y2) + TOLERANCE):
                pin.lines.append(line)
                logger.debug(f"[PART] Line associated with pin {pin.snum}: layer={line.layer}, "
                             f"({line.x1:.2f}, {line.y1:.2f}) -> ({line.x2:.2f}, {line.y2:.2f})")
    part.pins = part_pins

    # IMPORTANT: Add the pins to the global list
    pins.extend(part_pins)

    if not hasattr(part, 'lines'):
        part.lines = []
    part.lines.extend(part_lines)
    logger.debug(f"[PART] Summary: Final name='{initial_group_name}', Position=({part.x:.3f} mm, {part.y:.3f} mm), "
                 f"Rotation={part.rotation} (raw), Pins={len(part.pins)}, Sub-blocks={sub_block_count}")
    part.net_name = initial_group_name
    if len(part.pins) == 1:
        pin = part.pins[0]
        old_name = part.name.decode("utf-8", errors="replace")

        # Create a test pad from the single-pin component
        test_pad = XZZTestPad(
            x=pin.pos.x,  # Use the pin position directly
            y=pin.pos.y,
            width=pin.width,
            height=pin.height,
            layer=pin.layer,
            net_index=pin.net_index,
            net=pin.net,
            name=part.name,
            rotation=pin.rotation,
            mounting_side=part.mounting_side
        )

        # Update the component
        part.part_type = "TEST_PAD"
        part.name = f"TEST_PAD_{old_name}".encode()
        part.category = "TP"  # Test Pad category for single-pin components
        part.visibility = True
        # Do not modify the pin position; leave it as-is
        pin.side = part.mounting_side

        logger.debug(f"[PART] Converted to TEST_PAD: '{old_name}' at ({pin.pos.x:.3f}, {pin.pos.y:.3f})")
        logger.debug(f"[PART] TEST_PAD dimensions: {test_pad.width:.3f}x{test_pad.height:.3f} mm, rotation={test_pad.rotation:.1f}°")
    parts.append(part)

def parse_decrypted_data(decrypted_data: bytes, parts: list, logger):
    try:
        part_size = int.from_bytes(decrypted_data[0:4], byteorder='little')
        part_x = int.from_bytes(decrypted_data[4:8], byteorder='little')
        part_y = int.from_bytes(decrypted_data[8:12], byteorder='little')
        visibility = decrypted_data[12]
        part_group_name_size = int.from_bytes(decrypted_data[18:22], byteorder='little')
        part_group_name_bytes = decrypted_data[22:22+part_group_name_size]
        logger.debug(f"[DECRYPT] Raw group name: {part_group_name_bytes.hex()}")
        part_group_name = translate_hex_string(part_group_name_bytes)
        component_id = ""
        metadata = ""
        if '$' in part_group_name:
            parts_split = part_group_name.split('$', 1)
            component_id = parts_split[0].strip()
            metadata = parts_split[1] if len(parts_split) > 1 else ""
        else:
            component_id = part_group_name.strip()
        component_id = ''.join(c for c in component_id if ord(c) >= 32)
        if not component_id:
            component_id = "Unknown"
        x_mm = part_x / 1_000_000.0
        y_mm = part_y / 1_000_000.0
        logger.debug(f"[DECRYPT] Component ID: {component_id}, Raw position: ({x_mm:.3f} mm, {y_mm:.3f} mm)")
        logger.debug(f"Part Size: {part_size}")
        logger.debug(f"Part X: {x_mm:.3f} mm ({part_x})")
        logger.debug(f"Part Y: {y_mm:.3f} mm ({part_y})")
        logger.debug(f"Visibility: {'Visible' if visibility == 0x02 else 'Hidden'}")
        logger.debug(f"Component ID: {component_id}")
        if metadata:
            logger.debug(f"Additional Data: {metadata}")
        from .models import XZZPart
        part = XZZPart()
        part.x = x_mm
        part.y = y_mm
        part.name = component_id.encode('utf-8')
        part.visibility = (visibility == 0x02)
        part.group_name = part_group_name

        # Extract the category from component_id
        category = ""
        for char in component_id:
            if char.isalpha():
                category += char
            else:
                break
        part.category = category.upper() if category else ""

        parts.append(part)
    except Exception as e:
        logger.error(f"[DECRYPT] Error: {str(e)}")
        logger.error(f"[DECRYPT] Raw data: {decrypted_data.hex()}")

# -----------------------------------------------------------
# Generation and translation functions (outline, translation)
# -----------------------------------------------------------




def translate_points(point, translation):
    point.x -= translation.x
    point.y -= translation.y

def translate_segments(outline, translation):
    for p in outline.points:
        translate_points(p, translation)

def translate_pins(pins, translation):
    for pin in pins:
        translate_points(pin.pos, translation)

def translate_line(line, translation):
    line.x1 -= translation.x
    line.y1 -= translation.y
    line.x2 -= translation.x
    line.y2 -= translation.y
    return line

def translate_arc(arc, translation):
    arc.x1 -= translation.x
    arc.y1 -= translation.y
    return arc

def translate_text(text_obj, translation):
    text_obj.x -= translation.x
    text_obj.y -= translation.y
    return text_obj

def translate_part(part, translation):
    part.x -= translation.x
    part.y -= translation.y
    for pin in part.pins:
        translate_points(pin.pos, translation)
    if hasattr(part, 'lines'):
        for line in part.lines:
            translate_line(line, translation)
    return part
