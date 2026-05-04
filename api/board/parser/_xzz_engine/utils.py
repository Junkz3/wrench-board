# utils.py
import struct

def read_uint8(data: bytes, offset: int) -> tuple[int, int]:
    """
    Reads an unsigned 8-bit integer from a binary data buffer at the given offset.

    Args:
        data (bytes): The binary data buffer to read from.
        offset (int): The current position (in bytes) in the buffer.

    Returns:
        tuple: (value, new_offset) where value is the 8-bit integer and new_offset is offset + 1.

    Raises:
        ValueError: If the offset is beyond the buffer's length.
    """
    if offset >= len(data):
        raise ValueError("Offset is beyond the buffer's length")
    value = data[offset]  # Read one byte as an integer (0-255)
    return value, offset + 1

def read_uint32(buffer: bytes, offset: int) -> (int, int):
    if offset + 4 > len(buffer):
        raise ValueError("Tentative de lire hors du buffer (uint32)")
    value = struct.unpack_from('<I', buffer, offset)[0]
    return value, offset + 4

def read_int32(buffer: bytes, offset: int) -> (int, int):
    if offset + 4 > len(buffer):
        raise ValueError("Tentative de lire hors du buffer (int32)")
    value = struct.unpack_from('<i', buffer, offset)[0]
    return value, offset + 4

def read_uint16(buffer: bytes, offset: int) -> (int, int):
    if offset + 2 > len(buffer):
        raise ValueError("Tentative de lire hors du buffer (uint16)")
    value = struct.unpack_from('<H', buffer, offset)[0]
    return value, offset + 2

def read_bytes(buffer: bytes, offset: int, length: int) -> (bytes, int):
    if offset + length > len(buffer):
        raise ValueError("Tentative de lire hors du buffer (bytes)")
    data = buffer[offset:offset+length]
    return data, offset + length

# --- String decoding (GB2312, with UTF-8 fallback) ---
#
# XZZ stores strings as ASCII, GB2312-encoded Chinese, or occasionally UTF-8.
# GB2312 is ASCII-compatible for bytes < 128, so we try it first and fall
# back to UTF-8 only when GB2312 produced too many replacement characters.
# Python's built-in `gb2312` codec handles the full lookup natively.

def translate_hex_string(input_bytes: bytes) -> str:
    result = input_bytes.decode('gb2312', errors='replace')
    if result and result.count('�') > len(result) * 0.5:
        try:
            result = input_bytes.decode('utf-8', errors='replace')
        except Exception:
            pass
    return result
