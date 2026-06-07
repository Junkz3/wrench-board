"""FZ container cipher engine.

The XOR-flavoured `.fz` boardview wraps an FZ-zlib payload (4-byte LE
size + zlib stream) in a 16-byte sliding-window byte cipher seeded by
a fixed 44 × uint32 key. Once decrypted the payload is identical to
the plain FZ-zlib variant already handled by `_fz_zlib.py`.
"""
