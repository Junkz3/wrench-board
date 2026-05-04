#!/usr/bin/env python3
"""
XZZ File Parser - With Rust Acceleration

Uses Rust module (repairboard_core) when available for 10-50x faster parsing.
Falls back to pure Python implementation otherwise.
"""
import os
import sys
import logging
from typing import Union
from ._rm_base import BoardFormatBase, PartType, PartMountingSide, Point
from .decryptor import decrypt_file, decrypt_with_des
from .parser_helpers import (
    parse_header,
    parse_blocks_generator,
    parse_line,
    parse_arc,
    parse_text,
    parse_test_pad_block,
    parse_nets,
    parse_post_v6_block,
    parse_images,
    parse_part_block,

)
from .utils import read_uint32, translate_hex_string
import os
import json

# Rust acceleration disabled — pure Python is fast enough for the boards
# we deal with (a single 820-class fragment parses in ~25 ms). A native
# extension would otherwise need its own toolchain in `make install`.
_USE_RUST = False

CONVERSION_FACTOR = 1000000.0  # Les valeurs brutes sont en nm (nanomètres), conversion en mm

def setup_logging():
    """Configure le système de logging avec des formats personnalisés (optimisé pour la production)."""
    logger = logging.getLogger('xzz_parser')
    # OPTIMISATION: Passer en INFO par défaut pour réduire le volume de logs
    logger.setLevel(logging.INFO)

    # Supprimer les handlers existants
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    # Handler pour le fichier - uniquement WARNING et plus pour réduire la taille
    file_handler = logging.FileHandler("xzz_parser.log", mode="w", encoding='utf-8')
    file_handler.setLevel(logging.WARNING)  # Seulement les warnings/erreurs dans le fichier
    file_format = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_format)

    # Handler pour la console - INFO pour voir la progression
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    console_format = logging.Formatter('%(message)s')  # Format simplifié pour la console
    console_handler.setFormatter(console_format)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger

XZZ_KEY_ENV = "WRENCH_BOARD_XZZ_KEY"


class XZZFile(BoardFormatBase):
    # DES master key — loaded at runtime from WRENCH_BOARD_XZZ_KEY (8 bytes
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
        self.nets = []      # Liste indexée pour associer nets et pins
        self.parts = []
        self.pins = []
        self.vias = []
        self.net_pins = {}  # Dictionnaire {nom_net: [pins]}
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
        """Vérifie si les données correspondent au format XZZ (optimisé)."""
        if len(data) < 64:  # Taille minimale pour un fichier XZZ
            return False
        try:
            # OPTIMISATION: Décrypter seulement les 100 premiers octets pour vérifier la signature
            # au lieu de décrypter tout le fichier
            from .decryptor import de_xor_data

            # Extraire la clé XOR de l'offset 0x10
            xor_key = data[0x10]

            # Décrypter seulement les 100 premiers octets
            sample_size = min(100, len(data))
            header_sample = bytearray(data[:sample_size])

            # Appliquer XOR
            for i in range(sample_size):
                header_sample[i] ^= xor_key

            # Vérifier la signature après décryptage XOR
            signature = header_sample[:11].decode("ascii", errors="ignore")
            return signature.startswith("XZZ")
        except:
            return False

    def _check_signature(self, data: bytes) -> bool:
        try:
            signature = data[:11].decode("ascii", errors="ignore")
            if not signature.startswith("XZZ"):
                self.error_msg = f"Signature invalide: {signature}"
                self.logger.error(self.error_msg)
                return False
            return True
        except Exception as e:
            self.error_msg = f"Erreur lors de la vérification de la signature: {str(e)}"
            self.logger.error(self.error_msg)
            return False

    def _decrypt_file(self, data: bytes) -> bytes:
        """
        Décrypte le fichier XZZ (XOR + DES pour les blocs PART).

        Utilise Rust quand disponible (10-50x plus rapide).
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
            self.logger.info("Décryptage du fichier... (Rust)")
            try:
                data = rust_decrypt_xzz_file(data, self.MASTER_KEY, self.DIODE_PATTERN)
                # Extraire main_data_blocks_size après décryptage
                if len(data) >= 0x44:
                    self.main_data_blocks_size, _ = read_uint32(data, 0x40)
                    self.logger.debug(f"Bloc principal de taille: {self.main_data_blocks_size} octets (0x{self.main_data_blocks_size:X})")
                return data
            except Exception as e:
                self.logger.warning(f"Rust decryption failed, falling back to Python: {e}")
                # Fall through to Python implementation

        # === PYTHON FALLBACK ===
        self.logger.info("Décryptage du fichier... (Python)")
        # Utilise la fonction modulaire pour appliquer le XOR et retourner un bytearray décrypté
        data = bytearray(decrypt_file(data, self.MASTER_KEY, self.DIODE_PATTERN, self.logger))
        current_pointer = 0x40
        if len(data) < current_pointer + 4:
            self.error_msg = "Fichier trop court pour contenir main_data_blocks_size"
            self.logger.error(self.error_msg)
            return bytes(data)
        self.main_data_blocks_size, current_pointer = read_uint32(data, current_pointer)
        self.logger.debug(f"Bloc principal de taille: {self.main_data_blocks_size} octets (0x{self.main_data_blocks_size:X})")
        # Pour chaque bloc PART (type 0x07) on effectue un décryptage DES
        while current_pointer < 0x44 + self.main_data_blocks_size:
            block_type = data[current_pointer]
            current_pointer += 1
            block_size, current_pointer = read_uint32(data, current_pointer)
            if block_type == 0x07:
                self.logger.debug(f"Décryptage DES d'un bloc 0x07 de taille {block_size} à la position 0x{current_pointer:X}")
                try:
                    encrypted_data = data[current_pointer:current_pointer+block_size]
                    self.logger.debug(f"Données chiffrées: {encrypted_data.hex()[:50]}...")
                    decrypted_data = decrypt_with_des(encrypted_data, self.MASTER_KEY)
                    data[current_pointer:current_pointer+block_size] = decrypted_data
                    self.logger.debug(f"Données déchiffrées: {decrypted_data.hex()[:50]}...")
                except Exception as e:
                    self.error_msg = f"Erreur lors du décryptage DES: {str(e)}"
                    self.logger.error(self.error_msg)
                    return b""  # Retourner vide en cas d'échec DES
            current_pointer += block_size
        return bytes(data)

    
    

    def find_xy_translation(self):
        """Trouve le point de translation pour centrer le PCB (point minimum du contour)."""
        # Filtrer les lignes de contour (layer 28)
        outline_lines = [l for l in self.lines if l.layer == 28]

        if not outline_lines:
            self.logger.warning("[TRANSLATION] Aucune ligne de contour trouvée (layer 28)")
            return Point(0, 0)

        # Trouver les coordonnées minimales et maximales
        min_x = min(min(line.x1, line.x2) for line in outline_lines)
        min_y = min(min(line.y1, line.y2) for line in outline_lines)
        max_x = max(max(line.x1, line.x2) for line in outline_lines)
        max_y = max(max(line.y1, line.y2) for line in outline_lines)

        # Calculer les dimensions du PCB (en conservant les valeurs brutes en nm pour la conversion finale)
        self.width = (max_x - min_x) * CONVERSION_FACTOR
        self.height = (max_y - min_y) * CONVERSION_FACTOR

        self.logger.info(f"[TRANSLATION] Translation trouvée: ({min_x:.2f}, {min_y:.2f}) mm")
        self.logger.info(f"[TRANSLATION] Dimensions PCB: {max_x - min_x:.2f} x {max_y - min_y:.2f} mm")

        return Point(min_x, min_y)

    def translate_segments(self):
        """Applique la translation sur les coordonnées des lignes, arcs et textes."""
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
        """Applique la translation sur les positions des composants, pins et segments internes."""
        if not self.xy_translation:
            return

        # Translater les composants (parts)
        for part in self.parts:
            part.x -= self.xy_translation.x
            part.y -= self.xy_translation.y

            # Translater les pins (positions ABSOLUES depuis le fix)
            if hasattr(part, 'pins'):
                for pin in part.pins:
                    if hasattr(pin, 'pos'):
                        pin.pos.x -= self.xy_translation.x
                        pin.pos.y -= self.xy_translation.y

            # Translation des segments internes du composant
            if hasattr(part, 'lines'):
                for line in part.lines:
                    line.x1 -= self.xy_translation.x
                    line.y1 -= self.xy_translation.y
                    line.x2 -= self.xy_translation.x
                    line.y2 -= self.xy_translation.y

    def load(self, data_or_path: Union[str, bytes]) -> bool:
        """Charge et parse un fichier PCB."""
        self.error_msg = ""  # Réinitialiser à chaque appel

        # Log acceleration status
        if _USE_RUST:
            self.logger.info("XZZ Parser: Rust acceleration ENABLED")
        else:
            self.logger.info("XZZ Parser: Using Python (Rust not available)")

        try:
            if isinstance(data_or_path, str):
                if not os.path.exists(data_or_path):
                    self.error_msg = f"Le fichier {data_or_path} n'existe pas"
                    self.logger.error(self.error_msg)
                    return False
                    
                self.logger.info(f"Chargement du fichier PCB: {os.path.basename(data_or_path)}")
                try:
                    with open(data_or_path, 'rb') as f:
                        data = f.read()
                except Exception as e:
                    self.error_msg = f"Impossible de lire le fichier {data_or_path}: {str(e)}"
                    self.logger.error(self.error_msg)
                    return False
            else:
                data = data_or_path
                self.logger.info("Chargement des données depuis le buffer")

            # Vérifier la taille minimale du fichier
            if len(data) < 64:  # Taille minimale pour un fichier XZZ valide
                self.error_msg = "Le fichier est trop petit pour être un fichier XZZ valide"
                self.logger.error(self.error_msg)
                return False

            # Décryptage (on décrypte d'abord, puis on vérifie la signature après)
            decrypted_data = self._decrypt_file(data)
            if not decrypted_data:
                if not self.error_msg:
                    self.error_msg = "Le fichier n'est pas au format XZZ ou est corrompu"
                self.logger.error(self.error_msg)
                return False

            # Vérification de la signature après décryptage
            if not self._check_signature(decrypted_data):
                if not self.error_msg:
                    self.error_msg = "Le fichier n'est pas au format XZZ (signature invalide)"
                self.logger.error(self.error_msg)
                return False

            # Parsing
            self.logger.info("Début du parsing...")
            result = self.parse_decrypted_data(decrypted_data)

            if result:
                # CORRECTION: Appliquer la translation pour centrer tout le PCB à l'origine
                # Trouver le point minimum du contour
                self.xy_translation = self.find_xy_translation()

                if self.xy_translation and (self.xy_translation.x != 0 or self.xy_translation.y != 0):
                    self.logger.info(f"Application de la translation: ({self.xy_translation.x:.2f}, {self.xy_translation.y:.2f})")
                    # Appliquer la translation sur TOUT
                    self.translate_segments()  # Lignes, arcs, textes
                    self.translate_pins()      # Composants (pas les pins relatives)
                    self.logger.info("✓ Translation appliquée sur tous les éléments")
                else:
                    self.logger.info("Pas de translation nécessaire (déjà à l'origine)")

                # Conversion des dimensions en millimètres
                width_mm = self.width / CONVERSION_FACTOR
                height_mm = self.height / CONVERSION_FACTOR
                area_mm2 = width_mm * height_mm
                
                # Statistiques des composants
                smd_count = len([p for p in self.parts if p.part_type == "SMD"])
                th_count = len(self.parts) - smd_count
                top_count = len([p for p in self.parts if p.mounting_side == "TOP"])
                bottom_count = len([p for p in self.parts if p.mounting_side == "BOTTOM"])
                
                # Statistiques des nets
                total_pins = len(self.pins)
                avg_pins_per_net = total_pins / len(self.nets) if self.nets else 0
                
                # Statistiques des traces par couche
                layer_stats = {}
                for line in self.lines:
                    layer_stats.setdefault(line.layer, {'lines': 0, 'arcs': 0})
                    layer_stats[line.layer]['lines'] += 1
                for arc in self.arcs:
                    layer_stats.setdefault(arc.layer, {'lines': 0, 'arcs': 0})
                    layer_stats[arc.layer]['arcs'] += 1
                
                # Affichage du résumé
                self.logger.info("\n=== RÉSUMÉ DU PCB ===")
                self.logger.info(f"\nDimensions:")
                self.logger.info(f"  Largeur:  {width_mm:.2f} mm")
                self.logger.info(f"  Hauteur:  {height_mm:.2f} mm")
                self.logger.info(f"  Surface:  {area_mm2:.2f} mm²")
                
                self.logger.info(f"\nComposants ({len(self.parts)} total):")
                self.logger.info(f"  SMD:          {smd_count}")
                self.logger.info(f"  Through-hole: {th_count}")
                self.logger.info(f"  Face TOP:     {top_count}")
                self.logger.info(f"  Face BOTTOM:  {bottom_count}")
                
                self.logger.info(f"\nConnectivité:")
                self.logger.info(f"  Nets:         {len(self.nets)}")
                self.logger.info(f"  Pins:         {total_pins}")
                self.logger.info(f"  Vias:         {len(self.vias)}")
                self.logger.info(f"  Moy. pins/net: {avg_pins_per_net:.1f}")
                
                self.logger.info("\nTraces par couche:")
                for layer, stats in sorted(layer_stats.items()):
                    if stats['lines'] > 0 or stats['arcs'] > 0:
                        layer_name = "CONTOUR" if layer == 28 else f"LAYER {layer}"
                        self.logger.info(f"  {layer_name}:")
                        self.logger.info(f"    Lignes: {stats['lines']}")
                        self.logger.info(f"    Arcs:   {stats['arcs']}")
                
                # Log détaillé dans le fichier
                self.logger.debug("\nDétails des composants:", extra={
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
            self.error_msg = f"Erreur lors du chargement: {str(e)}"
            self.logger.error(self.error_msg, exc_info=True)
            return False

    def parse_decrypted_data(self, decrypted_data: bytes) -> bool:
        """Parse les données déchiffrées."""
        try:
            # Parsing
            self.logger.info("Début du parsing...")
            header_ok, header_info = parse_header(decrypted_data, self.logger)
            if not header_ok:
                self.error_msg = "Échec du parsing de l'en-tête"
                return False
            self.image_block_start = header_info.get("image_block_start", 0)
            self.net_block_start = header_info.get("net_block_start", 0)
            self.main_data_blocks_size = header_info.get("main_data_blocks_size", 0)
            current_offset = 0x44
            end_offset = 0x44 + self.main_data_blocks_size

            # IMPORTANT: Parser les nets AVANT les blocs de données
            # car parse_part_block a besoin de la liste des nets pour assigner les noms aux pins
            if self.net_block_start > 0:
                offset_nets = 0x20 + self.net_block_start
                self.logger.info(f"Parsing nets à offset 0x{offset_nets:X}...")
                parse_nets(decrypted_data, offset_nets, self.nets, self.logger)
                self.logger.info(f"Nets parsed: {len([n for n in self.nets if n is not None])} nets trouvés")

            # Parser les images (optionnel, avant les blocs principaux)
            if self.image_block_start > 0:
                offset_images = 0x20 + self.image_block_start
                _, self.images = parse_images(decrypted_data, offset_images, self.logger)

            # OPTIMISATION: Barre de progression pour les gros fichiers
            total_bytes = end_offset - current_offset
            processed_bytes = 0
            last_progress = -1

            for block_type, block_data, offset in parse_blocks_generator(decrypted_data, current_offset, end_offset, self.block_counts, self.logger):
                try:
                    # Afficher la progression tous les 10%
                    processed_bytes = offset - current_offset
                    progress = int((processed_bytes / total_bytes) * 100)
                    if progress // 10 > last_progress // 10:
                        self.logger.info(f"Parsing... {progress}%")
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
                        # Exemple simplifié de parsing d'un VIA
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
                            self.logger.error(f"Erreur lors du parsing d'un VIA: {e}")
                except Exception as e:
                    self.logger.error(f"Erreur lors du parsing du bloc type 0x{block_type:02X}: {str(e)}")
                    continue

            self.logger.info("Parsing... 100%")
            
            # Parser les données post-v6 (résistances, signaux, etc.) après le bloc nets
            if self.net_block_start > 0:
                offset_nets = 0x20 + self.net_block_start
                # Calculer la fin du bloc nets pour trouver le début des données post-v6
                import struct
                net_block_size = struct.unpack('<I', decrypted_data[offset_nets:offset_nets+4])[0]
                post_v6_start = offset_nets + 4 + net_block_size
                if post_v6_start < len(decrypted_data):
                    post_v6_start, post_v6_data = parse_post_v6_block(decrypted_data, post_v6_start, self.logger)
                    self.post_v6_data = post_v6_data
            
            
            
            return True
        except Exception as e:
            self.error_msg = f"Erreur lors du parsing des données déchiffrées: {str(e)}"
            self.logger.error(self.error_msg, exc_info=True)
            return False

    def to_board(self) -> 'Board':
        """
        Convertit XZZFile vers Board normalisé.

        XZZ a des spécificités:
        - nets est une liste indexée (pas de noms directs)
        - pins ont des positions RELATIVES (pos.x, pos.y) par rapport au composant
        - parts ont x, y absolus
        """
        from core.models.board import (
            Board, Component, Pin as NormalizedPin, Net,
            Point as NormalizedPoint, BoardSide, PinType, MountType,
            Line as NormalizedLine, Arc as NormalizedArc
        )

        board = Board(format_type="xzz")

        # Copier les lignes (traces + contours)
        for line in self.lines:
            board.lines.append(NormalizedLine(
                x1=line.x1,
                y1=line.y1,
                x2=line.x2,
                y2=line.y2,
                layer=line.layer
            ))

        # Copier les arcs
        for arc in self.arcs:
            board.arcs.append(NormalizedArc(
                x1=arc.x1,
                y1=arc.y1,
                radius=arc.radius,
                angle_start=arc.angle_start,
                angle_end=arc.angle_end,
                layer=arc.layer
            ))

        # Copier les vias
        board.vias = self.vias.copy() if hasattr(self, 'vias') else []

        # Copier les text elements
        board.text_elements = self.text_elements.copy() if hasattr(self, 'text_elements') else []

        # Convertir les nets (liste indexée -> dict par index)
        # Note: self.nets peut être sparse (contient des None pour les indices non définis)
        nets_dict = {}  # net_index -> Net

        # Récupérer le mapping signal si disponible (vrais noms de nets)
        signal_map = {}
        if hasattr(self, 'post_v6_data') and self.post_v6_data:
            signal_map = self.post_v6_data.get('signal_map', {})
            if signal_map:
                self.logger.info(f"[TO_BOARD] Signal map disponible: {len(signal_map)} mappings")

        for net_idx, net_obj in enumerate(self.nets):
            # Ignorer les entrées None (indices non définis dans le fichier XZZ)
            if net_obj is None:
                continue

            net_name = getattr(net_obj, 'name', f"NET_{net_idx}")
            if isinstance(net_name, bytes):
                net_name = net_name.decode('utf-8', errors='replace')

            # Appliquer le mapping signal si disponible (remplace Net973 -> PP3V3_G3H etc.)
            original_name = net_name
            if net_name in signal_map:
                net_name = signal_map[net_name]
                self.logger.debug(f"[TO_BOARD] Net renommé: {original_name} -> {net_name}")

            net = Net(
                name=net_name,
                number=net_idx,
                is_ground=(net_name.upper() in ["GND", "GROUND"])
            )
            nets_dict[net_idx] = net
            board.nets.append(net)

        # Convertir les composants et pins
        for part_idx, part in enumerate(self.parts):
            # Nom du composant
            part_name = getattr(part, 'name', b'')
            if isinstance(part_name, bytes):
                part_name = part_name.decode('utf-8', errors='replace')

            # Type de montage
            part_type_str = getattr(part, 'part_type', 'SMD')
            mount_type = MountType.SMD if part_type_str == "SMD" else MountType.THROUGH_HOLE

            # Côté
            mounting_side = getattr(part, 'mounting_side', 'TOP')
            if mounting_side == "TOP":
                board_side = BoardSide.TOP
            elif mounting_side == "BOTTOM":
                board_side = BoardSide.BOTTOM
            else:
                board_side = BoardSide.BOTH

            # Créer le composant
            component = Component(
                name=part_name,
                mfgcode="",
                mount_type=mount_type,
                board_side=board_side,
                center=NormalizedPoint(getattr(part, 'x', 0), getattr(part, 'y', 0)),
                rotation=getattr(part, 'rotation', 0.0)
            )

            # Copier les lignes du contour du composant (XZZ)
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

            # Convertir les pins de ce composant
            if hasattr(part, 'pins') and part.pins:
                for old_pin in part.pins:
                    # XZZ: position ABSOLUE dans pin.pos (déjà pré-transformée)
                    pin_pos = getattr(old_pin, 'pos', None)
                    if not pin_pos:
                        continue

                    # Position déjà ABSOLUE - utiliser directement
                    pin_x = getattr(pin_pos, 'x', 0)
                    pin_y = getattr(pin_pos, 'y', 0)

                    # Numéro du pin
                    pin_number = getattr(old_pin, 'snum', None)
                    if not pin_number:
                        pin_name_bytes = getattr(old_pin, 'name', b'')
                        if isinstance(pin_name_bytes, bytes):
                            pin_number = pin_name_bytes.decode('utf-8', errors='replace')
                    if not pin_number:
                        pin_number = str(len(component.pins) + 1)

                    # Type de pin (test pad si dummy)
                    is_dummy = getattr(part, 'part_type', '') == "TEST_PAD"
                    pin_type = PinType.TEST_PAD if is_dummy else PinType.COMPONENT

                    # Côté du pin
                    mirror = getattr(part, 'mirror', False)
                    pin_board_side = BoardSide.BOTTOM if mirror else BoardSide.TOP

                    # Net du pin (via net_index)
                    net_index = getattr(old_pin, 'net_index', 0)
                    pin_net = nets_dict.get(net_index, None)

                    # Dimensions
                    width = getattr(old_pin, 'width', None)
                    height = getattr(old_pin, 'height', None)
                    diameter = min(width, height) if (width and height) else 0.5

                    # Créer le pin normalisé avec position ABSOLUE
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

                    # Ajouter le pin au composant, au board, et au net
                    component.pins.append(new_pin)
                    board.pins.append(new_pin)
                    if pin_net:
                        pin_net.pins.append(new_pin)

        # Convertir le contour
        if hasattr(self, 'outline_segments') and self.outline_segments:
            for seg in self.outline_segments:
                board.outline_segments.append((
                    NormalizedPoint(seg[0].x, seg[0].y),
                    NormalizedPoint(seg[1].x, seg[1].y)
                ))

        # Construire les index
        board.build_indices()

        # Calculer les dimensions
        board.calculate_dimensions()

        self.logger.info(f"✓ Converted to normalized Board: {len(board.components)} components, {len(board.pins)} pins, {len(board.nets)} nets")

        return board

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python xzz_file.py <pcb_file>")
        sys.exit(1)
    pcb_file = XZZFile()
    if pcb_file.load(sys.argv[1]):
        print("Fichier XZZ chargé avec succès.")
    else:
        print(f"Erreur lors du chargement du fichier: {pcb_file.error_msg}")