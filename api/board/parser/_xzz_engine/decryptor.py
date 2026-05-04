# decryptor.py
"""
XZZ File Decryption Module

Uses Rust acceleration when available for 100-500x faster XOR decryption.
Falls back to Python implementation otherwise.
"""
import binascii

# Use the pyca `cryptography` package (already a project dep) instead of
# pycryptodome to avoid pulling another crypto library. TripleDES with an
# 8-byte key is single-DES, identical to DES.MODE_ECB.
from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes

_USE_RUST = False


def hex_to_bytes(hex_string: str) -> bytes:
    return binascii.unhexlify(hex_string)


def decrypt_with_des(encrypted_data: bytes, master_key: str) -> bytes:
    """Décrypte des données avec DES (SANS unpadding car le format XZZ n'utilise pas de padding)."""
    key = hex_to_bytes(master_key)
    cipher = Cipher(TripleDES(key), modes.ECB())
    decryptor = cipher.decryptor()
    return decryptor.update(encrypted_data) + decryptor.finalize()


def de_xor_data(data: bytearray, diode_pattern: bytes, logger=None) -> bytearray:
    """
    XOR decrypt data with key from offset 0x10.

    Uses Rust acceleration when available (100-500x faster).
    """
    key = data[0x10]
    pos = data.find(diode_pattern)

    if _USE_RUST:
        # Use Rust acceleration
        if pos == -1:
            if logger:
                logger.debug("Motif diode non trouvé, XOR sur l'ensemble du fichier (Rust).")
            return bytearray(_rust_xor(bytes(data), key))
        else:
            if logger:
                logger.info(f"Motif diode trouvé à la position: {pos} (Rust)")
            return bytearray(_rust_xor_pattern(bytes(data), key, diode_pattern))
    else:
        # Pure Python fallback
        if pos == -1:
            if logger:
                logger.debug("Motif diode non trouvé, XOR sur l'ensemble du fichier.")
            return bytearray(a ^ key for a in data)
        else:
            if logger:
                logger.info(f"Motif diode trouvé à la position: {pos}")
            return bytearray(a ^ key for a in data[:pos]) + data[pos:]


def decrypt_file(data: bytes, master_key: str, diode_pattern: bytes, logger=None) -> bytes:
    """
    Decrypt XZZ file data (XOR only, DES is handled per-block).

    Args:
        data: Raw file bytes
        master_key: DES key (not used here, for API compatibility)
        diode_pattern: Pattern marking end of XOR region
        logger: Optional logger

    Returns:
        XOR-decrypted bytes
    """
    if logger:
        logger.info(f"Décryptage du fichier... {'(Rust)' if _USE_RUST else '(Python)'}")

    data_array = bytearray(data)

    if data_array[0x10] != 0x00:
        if logger:
            logger.info("Application du XOR sur les données...")
        data_array = de_xor_data(data_array, diode_pattern, logger)
    else:
        if logger:
            logger.info("Fichier déjà décrypté (XOR non nécessaire).")

    return bytes(data_array)
