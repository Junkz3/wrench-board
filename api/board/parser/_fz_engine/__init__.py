"""FZ container decode engine.

The XOR-flavoured `.fz` boardview wraps an FZ-zlib payload (4-byte LE
size + zlib stream) in an encoded outer layer keyed by a fixed
44 × uint32 key. Once decoded the payload is identical to the plain
FZ-zlib variant already handled by `_fz_zlib.py`.
"""
