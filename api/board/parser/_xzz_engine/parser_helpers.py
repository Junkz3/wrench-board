#!/usr/bin/env python3
"""
Unified PCB Parsing Code with Detailed Logging for Unit Conversion

- Toutes les positions du fichier source sont supposées être en µm.
- Pour l'affichage, on convertit en mm en divisant par MU_TO_MM (1 mm = 1000 µm).
- Des logs détaillés sont ajoutés pour afficher les valeurs brutes et converties.
"""

import struct
import json
import math
import sys
from .utils import read_uint32, read_int32, read_uint16, read_bytes, translate_hex_string
from .models import (XZZLine, XZZArc, XZZText, XZZPart, Pin, Point, Outline, XZZBlockType,
                 Net, XZZTestPad)

# Constante unique pour convertir les valeurs brutes en mm
# Les valeurs dans le fichier XZZ semblent être en nm (nanomètres) ou 0.001 µm
# Facteur observé: il faut diviser par 1000000 pour obtenir des mm
MU_TO_MM = 1000000.0

# -----------------------------------------------------------
# Fonctions de parsing de l'en-tête et des blocs
# -----------------------------------------------------------
def parse_header(data: bytes, logger):
    try:
        signature = data[:11].decode('ascii', errors='ignore')
        if not signature.startswith('XZZ'):
            logger.error(f"[HEADER] Signature invalide : {signature}")
            return False, {}
        offset = 0x20
        header_values = []
        for i in range(3):
            val, offset = read_uint32(data, offset)
            header_values.append(val)
            logger.debug(f"[HEADER] Valeur {i} brute: {val}")
        header_info = {
            'image_block_start': header_values[1],
            'net_block_start': header_values[2]
        }
        main_data_blocks_size, _ = read_uint32(data, 0x40)
        header_info['main_data_blocks_size'] = main_data_blocks_size
        logger.info(f"[HEADER] Parsed: image_block_start=0x{header_info['image_block_start']:X}, "
                    f"net_block_start=0x{header_info['net_block_start']:X}, "
                    f"main_data_blocks_size={main_data_blocks_size}")
        return True, header_info
    except Exception as e:
        logger.error(f"[HEADER] Erreur: {str(e)}")
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
            logger.debug(f"[BLOCK] Type 0x{block_type:02X} de taille {block_size} octets à offset 0x{offset:X}")
            if block_type in (XZZBlockType.ARC, XZZBlockType.VIA, XZZBlockType.LINE,
                              XZZBlockType.TEXT, XZZBlockType.PART, XZZBlockType.TEST_PAD):
                if block_type == XZZBlockType.TEST_PAD:
                    logger.debug("[BLOCK] Bloc TEST_PAD détecté")
                yield block_type, block_data, offset
            else:
                logger.info(f"[BLOCK] Bloc inconnu : type 0x{block_type:02X}, taille={block_size}")
                logger.debug(f"[BLOCK] Données (hex): {block_data.hex()[:64]}...")
            offset += block_size
        except Exception as e:
            logger.error(f"[BLOCK] Erreur lors du parsing du bloc type 0x{block_type:02X} à offset 0x{offset:X}: {str(e)}")
            offset += block_size
            continue

# -----------------------------------------------------------
# Fonctions de parsing des éléments graphiques
# -----------------------------------------------------------
def parse_line(data: bytes, offset: int, conversion_factor: float, logger):
    try:
        values = struct.unpack_from('<7i', data, offset)
        logger.debug(f"[LINE] Valeurs brutes: {values}")
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
        logger.debug(f"[LINE] Converti: layer={line.layer}, start=({line.x1:.2f} mm, {line.y1:.2f} mm), "
                     f"end=({line.x2:.2f} mm, {line.y2:.2f} mm)")
        return line, offset
    except Exception as e:
        logger.error(f"[LINE] Erreur à l'offset 0x{offset:X}: {str(e)}")
        raise

def parse_arc(data: bytes, offset: int, conversion_factor: float, logger):
    try:
        values = struct.unpack_from('<8i', data, offset)
        logger.debug(f"[ARC] Valeurs brutes: {values}")
        logger.debug(f"[ARC] Angles bruts: start={values[4]}, end={values[5]}")
        offset += 32

        # Les angles sont stockés en 1/10000ème de degré
        # Mais il faut peut-être les normaliser différemment
        angle_start_raw = values[4]
        angle_end_raw = values[5]

        # Essayer avec 10000.0 (valeurs en 1/10000 de degré)
        angle_start = angle_start_raw / 10000.0
        angle_end = angle_end_raw / 10000.0

        # Normaliser les angles entre 0 et 360
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
        logger.debug(f"[ARC] Converti: center=({arc.x1:.2f} mm, {arc.y1:.2f} mm), "
                     f"radius={arc.radius:.2f} mm, angles={arc.angle_start:.2f}° -> {arc.angle_end:.2f}°")
        return arc, offset
    except Exception as e:
        logger.error(f"[ARC] Erreur à l'offset 0x{offset:X}: {str(e)}")
        raise

def parse_text(data: bytes, offset: int, conversion_factor: float, logger):
    try:
        logger.debug(f"[TEXT] Début parsing à offset 0x{offset:X}, taille du bloc = {len(data)} octets")
        if len(data) < 36:
            logger.warning("[TEXT] Bloc trop court")
            return None, offset + len(data)
        values = struct.unpack_from('<8I', data, offset)
        pos_x, pos_y, text_size = values[1], values[2], values[3]
        layer = values[0]
        offset += 32
        one, offset = read_uint16(data, offset)
        logger.debug(f"[TEXT] Valeur 'one': {one}")
        text_length, offset = read_uint32(data, offset)
        logger.debug(f"[TEXT] Longueur brute du texte: {text_length}")
        text_element = None
        if text_length > len(data[offset:]):
            logger.warning("[TEXT] Longueur texte > données restantes")
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
            logger.debug(f"[TEXT] Converti: '{decoded_text}' à ({pos_x/conversion_factor:.2f} mm, {pos_y/conversion_factor:.2f} mm), layer={layer}")
        else:
            logger.debug("[TEXT] Texte ignoré (longueur nulle)")
        return text_element, offset
    except Exception as e:
        logger.error(f"[TEXT] Erreur à l'offset 0x{offset:X}: {str(e)}")
        logger.debug(f"[TEXT] Données (hex): {data.hex()[:64]}...")
        return None, offset + len(data)

def parse_test_pad_block(data: bytes, offset: int, nets: list, parts: list, pins: list, conversion_factor: float, logger):
    try:
        block_size, offset = read_uint32(data, offset)
        logger.info(f"[TEST_PAD] Début parsing test pad à offset {hex(offset)} - bloc de {block_size} octets")
        
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
                # net_obj est un objet Net, on accède à son attribut .name
                pin.net = "" if net_obj.name in ("UNCONNECTED", "NC") else net_obj.name
            else:
                pin.net = ""
        else:
            pin.net = ""
            
        pin.part_index = len(parts)
        pins.append(pin)
        part.pins = [pin]
        parts.append(part)
        
        logger.debug(f"[TEST_PAD] Part converti: name='{part.name.decode('utf-8', errors='replace')}', "
                     f"position=({part.x:.2f} mm, {part.y:.2f} mm), net='{pin.net}', "
                     f"width={pin.width:.2f} mm, height={pin.height:.2f} mm, rotation={pin.rotation:.2f}°")
    except Exception as e:
        logger.error(f"[TEST_PAD] Erreur: {str(e)}")
        logger.debug(f"[TEST_PAD] Raw data (first 32 octets): {data[offset:offset+32].hex()}")
    return offset

def parse_nets(data: bytes, offset: int, nets: list, logger):
    try:
        block_size, offset = read_uint32(data, offset)
        logger.info(f"[NETS] Début parsing nets à offset {hex(offset)} - bloc de {block_size} octets")
        
        end_offset = offset + block_size
        net_count = 0
        
        while offset < end_offset:
            # Log de l'offset actuel pour le debugging
            logger.debug(f"[NETS] Parsing net à l'offset {hex(offset)}")
            
            # Lecture de la taille du net et de son index
            net_size, offset = read_uint32(data, offset)
            net_index, offset = read_uint32(data, offset)
            
            # Lecture et décodage du nom du net
            net_name_bytes, offset = read_bytes(data, offset, net_size - 8)
            net_name = net_name_bytes.decode('utf-8', errors='replace').strip()
            
            # Création d'un nouvel objet Net
            new_net = Net(
                index=net_index,
                name=net_name
            )
            
            # Log détaillé des informations du net
            logger.debug(f"[NETS] Net {net_index}:")
            logger.debug(f"  - Taille: {net_size} octets")
            logger.debug(f"  - Nom: '{net_name}'")
            logger.debug(f"  - Offset suivant: {hex(offset)}")
            
            # Extension de la liste si nécessaire
            if net_index >= len(nets):
                logger.debug(f"[NETS] Extension de la liste des nets de {len(nets)} à {net_index + 1}")
                nets.extend([None] * (net_index - len(nets) + 1))
            
            # Stockage du net
            nets[net_index] = new_net
            net_count += 1
            
        logger.info(f"[NETS] Fin parsing nets: {net_count} nets trouvés")
        logger.info(f"[NETS] Premier net: {nets[1].name if len(nets) > 1 else 'Aucun'}")
        return offset
        
    except Exception as e:
        logger.error(f"[NETS] Erreur lors du parsing des nets à l'offset {hex(offset)}: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return offset

def parse_post_v6_block(data: bytes, offset: int, logger):
    """
    Parse les données post-v6 (voltage, résistance, signaux, etc.) qui se trouvent
    après le pattern v6v6555v6v6===

    Sections possibles (marqueurs GB2312):
    - 阻值 (D7E8D6B5) : Résistance - Format: =valeur=composant(pin)
    - 电压 (B5E7D1B9) : Voltage - Format: netName=voltage
    - 信号 (D0C5BAC5) : Signal - Format: oldNetName=realSignalName (vrais noms!)
    - 菜单 (B2CBB5A5) : Menu - JSON data
    """
    post_v6_data = {
        'resistance': [],
        'voltage': [],
        'signals': [],      # Mapping netName -> signalName (vrais noms!)
        'signal_map': {},   # Dict pour accès rapide: net_name -> signal_name
        'menu': [],
        'params': [],
        'resistance_diagram': []
    }

    # Marqueurs de section GB2312
    MARKERS = {
        'resistance': bytes.fromhex('D7E8D6B5'),  # 阻值
        'voltage': bytes.fromhex('B5E7D1B9'),     # 电压
        'signal': bytes.fromhex('D0C5BAC5'),      # 信号
        'menu': bytes.fromhex('B2CBB5A5'),        # 菜单
    }

    try:
        # Pattern de base: v6v6555v6v6===
        BASE_PATTERN = b'v6v6555v6v6==='

        # Chercher le pattern depuis le début des données
        pattern_pos = data.find(BASE_PATTERN)

        if pattern_pos == -1:
            logger.debug("[POST_V6] Pattern v6v6555v6v6 non trouvé, pas de données post-v6")
            return offset, post_v6_data

        logger.info(f"[POST_V6] Pattern v6v6555v6v6 trouvé à la position {pattern_pos} (0x{pattern_pos:X})")

        # Extraire toutes les données après le pattern de base
        post_v6_raw = data[pattern_pos:]

        # Décoder en GB2312
        try:
            text_data = post_v6_raw.decode('gb2312', errors='replace')
        except:
            try:
                text_data = post_v6_raw.decode('utf-8', errors='replace')
            except:
                text_data = post_v6_raw.decode('latin1', errors='replace')

        logger.info(f"[POST_V6] Données post-v6: {len(post_v6_raw)} octets")

        # Détecter les sections présentes
        sections_found = []
        for name, marker in MARKERS.items():
            pos = post_v6_raw.find(marker)
            if pos != -1:
                sections_found.append((name, pos))
                logger.info(f"[POST_V6] Section '{name}' trouvée à offset {pos}")

        # Trier par position
        sections_found.sort(key=lambda x: x[1])

        if not sections_found:
            logger.warning("[POST_V6] Aucune section reconnue trouvée")
            return len(data), post_v6_data

        # Parser chaque section
        for i, (section_name, section_start) in enumerate(sections_found):
            # Déterminer la fin de la section (début de la suivante ou fin des données)
            if i + 1 < len(sections_found):
                section_end = sections_found[i + 1][1]
            else:
                section_end = len(post_v6_raw)

            # Extraire les données de la section (après le marqueur de 4 bytes)
            section_data = post_v6_raw[section_start + 4:section_end]

            try:
                section_text = section_data.decode('gb2312', errors='replace')
            except:
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

        # Résumé
        logger.info(f"[POST_V6] ✅ Données extraites:")
        logger.info(f"  - Résistances: {len(post_v6_data['resistance'])}")
        logger.info(f"  - Voltages: {len(post_v6_data['voltage'])}")
        logger.info(f"  - Signaux (vrais noms): {len(post_v6_data['signals'])}")
        logger.info(f"  - Menu entries: {len(post_v6_data['menu'])}")

        return len(data), post_v6_data

    except Exception as e:
        logger.error(f"[POST_V6] Erreur: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return offset, post_v6_data


def _parse_resistance_section(lines: list, post_v6_data: dict, logger):
    """Parse la section résistance: =valeur=composant(pin)"""
    for line in lines:
        line = line.strip()
        if not line or not line.startswith('='):
            continue

        # Format: =VALEUR=COMPOSANT(PIN)
        # Exemple: =711=N485(D9)
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
    """Parse la section voltage: netName=voltage"""
    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Format: NET_NAME=VOLTAGE_VALUE
        # Exemple: PP3V3_G3H=3.3V ou GND=0
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
    Parse la section signal: oldNetName=realSignalName
    C'est ici qu'on trouve les VRAIS noms de signaux!
    """
    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Format: OLD_NET_NAME=REAL_SIGNAL_NAME
        # Exemple: Net973=PP3V3_G3H ou Net2=VCCIO_CPU
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
                    # Mapping pour accès rapide
                    post_v6_data['signal_map'][old_name] = real_name
                    logger.debug(f"[POST_V6] Signal: {old_name} -> {real_name}")


def _parse_menu_section(section_text: str, post_v6_data: dict, logger):
    """Parse la section menu (généralement JSON)"""
    import json

    # Chercher du JSON dans le texte
    try:
        # Trouver les accolades
        start = section_text.find('{')
        end = section_text.rfind('}')

        if start != -1 and end != -1 and end > start:
            json_str = section_text[start:end+1]
            menu_data = json.loads(json_str)
            post_v6_data['menu'].append(menu_data)
            logger.debug(f"[POST_V6] Menu JSON parsé: {len(json_str)} chars")
    except json.JSONDecodeError as e:
        logger.debug(f"[POST_V6] Menu non-JSON ou invalide: {e}")
    except Exception as e:
        logger.debug(f"[POST_V6] Erreur parsing menu: {e}")

def parse_images(data: bytes, offset: int, logger):
    try:
        start_offset = offset
        block_size, offset = read_uint32(data, offset)
        end_offset = start_offset + block_size
        logger.debug(f"[IMAGES] Bloc images: taille={block_size} octets, offset={start_offset} -> {end_offset}")
        image_count = 0
        images = []
        while offset < end_offset:
            if offset + 3 > len(data):
                logger.error("[IMAGES] Données insuffisantes pour l'en-tête de l'image")
                return offset, images
            type_byte = data[offset]
            index_byte = data[offset + 1]
            flag_byte = data[offset + 2]
            offset += 3
            width, offset = read_uint32(data, offset)
            height, offset = read_uint32(data, offset)
            name_length, offset = read_uint32(data, offset)
            if offset + name_length > len(data):
                logger.error(f"[IMAGES] Données insuffisantes pour le nom de l'image {image_count+1}")
                return offset, images
            name, offset = read_bytes(data, offset, name_length)
            image_name = translate_hex_string(name)
            logger.debug(f"[IMAGES] Image {image_count+1}: type=0x{type_byte:02X}, index={index_byte}, flag=0x{flag_byte:02X}, "
                         f"dimensions={width}x{height}, name='{image_name}'")
            images.append({'type': type_byte, 'index': index_byte, 'flag': flag_byte, 'width': width, 'height': height, 'name': image_name})
            image_count += 1
        logger.debug(f"[IMAGES] Fin parsing images: {image_count} images trouvées")
        return offset, images
    except Exception as e:
        logger.error(f"[IMAGES] Erreur: {str(e)}")
        raise

# -----------------------------------------------------------
# Fonctions spécifiques aux formes et aux composants
# -----------------------------------------------------------
# Pour les pins, un facteur spécifique est parfois utilisé dans le format XZZ.
# Ici, on conserve la conversion telle quelle pour l'instant.
CONVERSION_FACTOR_FOR_PINS = 10000.0

def parse_pin_shape(shape_raw: bytes, conversion_factor: float, logger) -> tuple:
    """
    Parse 32 octets de données de forme d'un pin et convertit la largeur et la hauteur en mm.

    Rotation contract: returns 0.0 by default (= "no per-pin rotation
    encoded in the shape block, fall back to part rotation upstream").
    The shape block in XZZ stores only geometry; the per-pin rotation
    overlay is encoded separately in flip_flag8bit and only applies for
    the non-standard 29-30° special cases handled in the part parsing
    branch. The previous 90° default broke the upstream sentinel
    `if pin_rot == 0.0: pin_rot = part_rotation` in xzz.py — every pin
    got stuck at 90° regardless of its package orientation.
    """
    if len(shape_raw) != 32:
        logger.warning(f"[PIN_SHAPE] Longueur incorrecte: {len(shape_raw)} octets (attendu 32)")
        return 0, 0.0, 0.0, 0.0
    try:
        values_int = struct.unpack('<8i', shape_raw)
        logger.debug(f"[PIN_SHAPE] Valeurs brutes: {values_int}")
        shape_type = values_int[1]
        raw_height = values_int[2]
        raw_width = values_int[3]
        # Conversion basée sur l'analyse du format XZZ :
        # Les dimensions des pins utilisent un facteur différent des positions
        # Facteur: 100000000 (100 millions) pour obtenir des dimensions réalistes (0.1-1 mm)
        width_mm = raw_width / 100_000_000.0
        height_mm = raw_height / 100_000_000.0
        rotation_deg = 0.0
        logger.debug(f"[PIN_SHAPE] Converti: type={shape_type}, width={width_mm:.3f} mm, height={height_mm:.3f} mm, rotation={rotation_deg}°")
        return shape_type, width_mm, height_mm, rotation_deg
    except Exception as e:
        logger.error(f"[PIN_SHAPE] Erreur: {str(e)}")
        return 0, 0.0, 0.0, 0.0

def parse_part_block(data: bytes, nets: list, parts: list, pins: list, conversion_factor: float, logger):
    current_pointer = 0
    from .models import XZZPart, Pin
    part = XZZPart()
    part_size, current_pointer = read_uint32(data, current_pointer)
    logger.debug(f"[PART] Début parsing, taille du bloc = {part_size} octets")
    if part_size > len(data) or part_size < 26:
        logger.error(f"[PART] Taille invalide: {part_size} (taille du buffer: {len(data)})")
        return
    block_end = current_pointer - 4 + part_size
    if block_end > len(data):
        logger.error(f"[PART] Fin du bloc {block_end} dépasse la taille du buffer {len(data)}")
        return
    # Lecture de la position (on convertit la position de µm en mm)
    # Lecture de la position (on convertit la position de µm en mm)
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

    # Lecture du nom de groupe
    group_name_size, current_pointer = read_uint32(data, current_pointer)
    group_name_bytes, current_pointer = read_bytes(data, current_pointer, group_name_size)
    decoded_group_name = translate_hex_string(group_name_bytes)
    initial_group_name = decoded_group_name
    logger.debug(f"[PART] Nom de groupe: '{initial_group_name}'")
    part.group_name = initial_group_name
    sub_block_count = 0
    part_lines = []
    part_pins = []
    while current_pointer < block_end:
        if current_pointer >= len(data):
            logger.error(f"[PART] Buffer overflow à l'offset {current_pointer}")
            break
        sub_type_identifier = data[current_pointer]
        current_pointer += 1
        if current_pointer + 4 > len(data):
            logger.error(f"[PART] Buffer overflow lors de la lecture de la taille du sous-bloc à {current_pointer}")
            break
        logger.debug(f"[PART] Sous-bloc {sub_block_count}: type 0x{sub_type_identifier:02X}")
        if sub_type_identifier == 0x06:
            block_size, current_pointer = read_uint32(data, current_pointer)
            logger.debug(f"[PART] Bloc nom: taille = {block_size} octets")
            if block_size > 31:
                # Lire les 31 premiers octets (header)
                header_bytes, current_pointer = read_bytes(data, current_pointer, 31)

                # Extraire le préfixe alphabétique depuis la fin du header
                prefix = b""
                for i in range(len(header_bytes) - 1, -1, -1):
                    char = bytes([header_bytes[i]])
                    if char.decode('utf-8', errors='ignore').isalpha():
                        prefix = char + prefix
                    else:
                        break

                # Lire le reste du nom (numéro)
                effective_name, current_pointer = read_bytes(data, current_pointer, block_size - 31)

                # Some XZZ exports (stripped-refdes flavour) ship TWO
                # 0x06 sub-blocks per part: the first carries the real
                # refdes (J1, C5, …) and the second a placeholder (U1 /
                # TEST_PAD_U1). Keep the FIRST one — overwriting with the
                # second leaves every part named U1 on those boards.
                # Other XZZ flavours ship a single 0x06 so the gate is a
                # no-op.
                if not getattr(part, "_name_set", False):
                    # Assembler le nom complet avec préfixe
                    part.name = prefix + effective_name

                    # Extraire et stocker la catégorie (préfixe uniquement)
                    part.category = prefix.decode('utf-8', errors='ignore').upper() if prefix else ""

                    # Nom de groupe pour compatibilité
                    initial_group_name = translate_hex_string(part.name)

                    logger.debug(f"[PART] Nom extrait: '{translate_hex_string(part.name)}', Catégorie: '{part.category}'")
                    part._name_set = True
                else:
                    logger.debug(f"[PART] Sub-block 0x06 ignoré (placeholder après le vrai nom): prefix={prefix!r}, eff={effective_name!r}")
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
                logger.debug(f"[PART] Ligne segment: layer={line.layer}, start=({line.x1:.2f} mm, {line.y1:.2f} mm), "
                             f"end=({line.x2:.2f} mm, {line.y2:.2f} mm)")
            logger.debug(f"[PART] Ajout de {num_segments} segments")
        elif sub_type_identifier == 0x09:
            pin = Pin()
            pin_block_size, current_pointer = read_uint32(data, current_pointer)
            pin_block_end = current_pointer + pin_block_size
            logger.debug(f"[PART] Bloc pin: taille = {pin_block_size} octets")
            pin_layer, current_pointer = read_int32(data, current_pointer)
            pin.layer = pin_layer
            
            # Lire la position absolue
            pin_pos_x, current_pointer = read_uint32(data, current_pointer)
            pin_pos_y, current_pointer = read_uint32(data, current_pointer)

            # XZZ: Les positions sont PRE-TRANSFORMEES (absolues)
            # On stocke directement les coordonnées absolues, pas de conversion relative
            # car app.py n'applique pas de rotation aux composants (rotation=0)
            abs_x = pin_pos_x / MU_TO_MM
            abs_y = pin_pos_y / MU_TO_MM

            # Stocker position ABSOLUE (sera utilisée directement par to_board)
            pin.pos.x = abs_x
            pin.pos.y = abs_y

            logger.debug(f"[PART] Pin position absolue: ({abs_x:.3f}, {abs_y:.3f}) mm")
            
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

            # XZZ: Positions pré-transformées
            # Stocker dimensions originales, swap appliqué plus tard selon le type de rotation
            pin.rotation = 0.0
            pin._original_width = pin.width
            pin._original_height = pin.height
            try:
                flip_bytes = bytes.fromhex(pin.flip_flag8bit)
                if len(flip_bytes) >= 8:
                    raw_rotation = struct.unpack('<I', flip_bytes[4:8])[0]
                    rotation_deg = (raw_rotation / 10000.0) % 360.0

                    # Swap width/height si proche de 0° ou 180° (pour rotations STANDARD)
                    if (rotation_deg < 45) or (135 < rotation_deg < 225) or (rotation_deg > 315):
                        pin.width, pin.height = pin.height, pin.width
            except:
                pass

            # --- NET INDEX ET TAIL DATA ---
            # Le net_index est au DEBUT des données restantes (premiers 4 octets)
            net_index, current_pointer = read_uint32(data, current_pointer)

            # Puis le reste est tail_data (généralement 4 octets de plus)
            remaining_len = pin_block_end - current_pointer
            if remaining_len > 0:
                logger.debug(f"[PART] Pin tail detectée: {remaining_len} octets")
                pin_tail, current_pointer = read_bytes(data, current_pointer, remaining_len)
                pin.tail_data = pin_tail.hex()
            else:
                pin.tail_data = ""
            # --- FIN ANALYSE ---

            logger.debug(f"[PART] Pin: rotation={pin.rotation:.1f}°, w={pin.width:.3f}, h={pin.height:.3f}")

            current_pointer = pin_block_end
            
            # Toujours assigner le net_index, même si pas de net correspondant
            pin.net_index = net_index
            
            # Assigner le nom du net si disponible
            if net_index < len(nets):
                pin_net = nets[net_index]
                if pin_net is not None:
                    # pin_net est un objet Net, on accède à son attribut .name
                    pin.net = "UNCONNECTED" if pin_net.name == "NC" else pin_net.name
                else:
                    pin.net = ""
            else:
                pin.net = ""
            
            pin.part_index = len(parts)
            pin.side = part.mounting_side
            part_pins.append(pin)  # Ajouter le pin une seule fois
            logger.debug(f"[PART] Pin ajoutée: pos=({pin.pos.x:.3f} mm, {pin.pos.y:.3f} mm), name={pin.snum}, net_index={net_index}, net='{pin.net}'")
        else:
            if sub_type_identifier != 0x00:
                part_name_decoded = translate_hex_string(part.name) if part.name else "Unknown"
                logger.warning(f"[PART] Sous-bloc inconnu: 0x{sub_type_identifier:02X} à offset {current_pointer} dans {part_name_decoded}")
            break
        sub_block_count += 1

    # ROTATION FIX: Gérer les rotations NON-STANDARD (pas 0/90/180/270)
    # SEULEMENT si part.rotation == 0 (rotation non définie dans l'en-tête)
    STANDARD_ROTATIONS = [0, 90, 180, 270]

    # Obtenir la rotation depuis les pins si disponible
    rotation_deg = None
    if part.rotation == 0 and part_pins:  # Seulement si rotation non définie
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
                except:
                    pass

        if pin_rotations_raw and all(r == pin_rotations_raw[0] for r in pin_rotations_raw):
            raw_val = pin_rotations_raw[0]
            rotation_deg = (raw_val / 10000.0) % 360.0

    # Appliquer le fix SEULEMENT pour rotation ~40° (composants diagonaux spéciaux)
    # La formule a été dérivée spécifiquement pour ce cas
    if rotation_deg is not None and 35 <= rotation_deg <= 45:
        # Formule dérivée des tests pour rotation ~40°:
        # Part = rotation_deg - 5
        # Pins = -(rotation_deg + 15)
        part.rotation = rotation_deg - 5.0
        pin_rotation = -(rotation_deg + 15.0)
        logger.debug(f"[PART] Rotation NON-STANDARD (~40°): part={part.rotation:.2f}°, pins={pin_rotation:.2f}° (raw={rotation_deg:.2f}°)")

        # Appliquer rotation négative aux pins ET restaurer dimensions originales (pas de swap)
        for pin in part_pins:
            pin.rotation = pin_rotation
            # Restaurer dimensions originales (annuler le swap)
            pin.width = getattr(pin, '_original_width', pin.width)
            pin.height = getattr(pin, '_original_height', pin.height)

    logger.debug(f"[PART] Rotation finale: {part.rotation}°")

    # Association des segments de ligne aux pins (tolérance en mm)
    TOLERANCE = 1.0
    for pin in part_pins:
        pin.lines = []
        pin_x, pin_y = pin.pos.x, pin.pos.y
        for line in part_lines:
            if (min(line.x1, line.x2) - TOLERANCE <= pin_x <= max(line.x1, line.x2) + TOLERANCE and
                min(line.y1, line.y2) - TOLERANCE <= pin_y <= max(line.y1, line.y2) + TOLERANCE):
                pin.lines.append(line)
                logger.debug(f"[PART] Ligne associée à la pin {pin.snum}: layer={line.layer}, "
                             f"({line.x1:.2f}, {line.y1:.2f}) -> ({line.x2:.2f}, {line.y2:.2f})")
    part.pins = part_pins

    # IMPORTANT: Ajouter les pins à la liste globale
    pins.extend(part_pins)

    if not hasattr(part, 'lines'):
        part.lines = []
    part.lines.extend(part_lines)
    logger.debug(f"[PART] Résumé: Nom final='{initial_group_name}', Position=({part.x:.3f} mm, {part.y:.3f} mm), "
                 f"Rotation={part.rotation} (brute), Pins={len(part.pins)}, Sous-blocs={sub_block_count}")
    part.net_name = initial_group_name
    if len(part.pins) == 1:
        pin = part.pins[0]
        old_name = part.name.decode("utf-8", errors="replace")
        
        # Créer un test pad à partir du composant à 1 pin
        test_pad = XZZTestPad(
            x=pin.pos.x,  # Utiliser la position du pin directement
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
        
        # Mettre à jour le composant
        part.part_type = "TEST_PAD"
        part.name = f"TEST_PAD_{old_name}".encode("utf-8")
        part.category = "TP"  # Catégorie Test Pad pour composants à 1 pin
        part.visibility = True
        # Ne pas modifier la position du pin, la laisser telle quelle
        pin.side = part.mounting_side
        
        logger.debug(f"[PART] Converti en TEST_PAD: '{old_name}' à ({pin.pos.x:.3f}, {pin.pos.y:.3f})")
        logger.debug(f"[PART] Dimensions du TEST_PAD: {test_pad.width:.3f}x{test_pad.height:.3f} mm, rotation={test_pad.rotation:.1f}°")
    parts.append(part)

def parse_decrypted_data(decrypted_data: bytes, parts: list, logger):
    try:
        part_size = int.from_bytes(decrypted_data[0:4], byteorder='little')
        part_x = int.from_bytes(decrypted_data[4:8], byteorder='little')
        part_y = int.from_bytes(decrypted_data[8:12], byteorder='little')
        visibility = decrypted_data[12]
        part_group_name_size = int.from_bytes(decrypted_data[18:22], byteorder='little')
        part_group_name_bytes = decrypted_data[22:22+part_group_name_size]
        logger.debug(f"[DECRYPT] Nom de groupe brut: {part_group_name_bytes.hex()}")
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
        logger.debug(f"[DECRYPT] Component ID: {component_id}, Position brute: ({x_mm:.3f} mm, {y_mm:.3f} mm)")
        print(f"Part Size: {part_size}")
        print(f"Part X: {x_mm:.3f} mm ({part_x})")
        print(f"Part Y: {y_mm:.3f} mm ({part_y})")
        print(f"Visibility: {'Visible' if visibility == 0x02 else 'Hidden'}")
        print(f"Component ID: {component_id}")
        if metadata:
            print(f"Additional Data: {metadata}")
        from .models import XZZPart
        part = XZZPart()
        part.x = x_mm
        part.y = y_mm
        part.name = component_id.encode('utf-8')
        part.visibility = (visibility == 0x02)
        part.group_name = part_group_name

        # Extraire la catégorie depuis component_id
        category = ""
        for char in component_id:
            if char.isalpha():
                category += char
            else:
                break
        part.category = category.upper() if category else ""

        parts.append(part)
    except Exception as e:
        logger.error(f"[DECRYPT] Erreur: {str(e)}")
        logger.error(f"[DECRYPT] Raw data: {decrypted_data.hex()}")

# -----------------------------------------------------------
# Fonctions de génération et de traduction (outline, translation)
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
