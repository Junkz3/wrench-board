"""TVW header-string substitution cipher.

The TVW production-binary header carries Pascal strings (format
signature, vendor, build date, paths, etc.) encoded with a 2D
position-dependent substitution table indexed by (input_char, position
within the string). Layer header strings, source paths, and net names
are plain Pascal — the cipher applies to the file header only.

Algorithm (byte at a time, position resets per Pascal-string):

    if char in '0'-'9' or 'a'-'j':
        out = TABLE[char][pos % 3]
    elif char in 'A'-'Z' or 'k'-'z':
        out = TABLE[char][pos % 10]
    else:
        out = char  (passes through unchanged — '-', ' ', '.', '!', '@', etc.)

The 256×11 table below contains only the non-zero rows (every other
char passes through unchanged).
"""
from __future__ import annotations

# Each row is exactly 11 bytes; we only look up indices 0..9. The
# 11th byte is always zero (acts as a guard / alignment slot).

_ROWS: dict[int, bytes] = {
    # Digits '0'..'9' — output cycles every 3 positions
    0x30: b"efgefgefge\x00",
    0x31: b"fghfghfghf\x00",
    0x32: b"ghighighig\x00",
    0x33: b"hijhijhijh\x00",
    0x34: b"ijaijaijai\x00",
    0x35: b"jabjabjabj\x00",
    0x36: b"abcabcabca\x00",
    0x37: b"bcdbcdbcdb\x00",
    0x38: b"cdecdecdec\x00",
    0x39: b"defdefdefd\x00",
    # Capitals 'A'..'Z' — output cycles every 10 positions
    0x41: b"FGHIJKLMNO\x00",
    0x42: b"GHIJKLMNOP\x00",
    0x43: b"HIJKLMNOPQ\x00",
    0x44: b"IJKLMNOPQR\x00",
    0x45: b"JKLMNOPQRS\x00",
    0x46: b"KLMNOPQRST\x00",
    0x47: b"LMNOPQRSTU\x00",
    0x48: b"MNOPQRSTUV\x00",
    0x49: b"NOPQRSTUVW\x00",
    0x4a: b"OPQRSTUVWX\x00",
    0x4b: b"PQRSTUVWXY\x00",
    0x4c: b"QRSTUVWXYZ\x00",
    0x4d: b"RSTUVWXYZA\x00",
    0x4e: b"STUVWXYZAB\x00",
    0x4f: b"TUVWXYZABC\x00",
    0x50: b"UVWXYZABCD\x00",
    0x51: b"VWXYZABCDE\x00",
    0x52: b"WXYZABCDEF\x00",
    0x53: b"XYZABCDEFG\x00",
    0x54: b"YZABCDEFGH\x00",
    0x55: b"ZABCDEFGHI\x00",
    0x56: b"ABCDEFGHIJ\x00",
    0x57: b"BCDEFGHIJK\x00",
    0x58: b"CDEFGHIJKL\x00",
    0x59: b"DEFGHIJKLM\x00",
    0x5a: b"EFGHIJKLMN\x00",
    # Lowercase 'a'..'j' — fed through the digit path (offset = pos % 3)
    0x61: b"3453453453\x00",
    0x62: b"2342342342\x00",
    0x63: b"1231231231\x00",
    0x64: b"0120120120\x00",
    0x65: b"9019019019\x00",
    0x66: b"8908908908\x00",
    0x67: b"7897897897\x00",
    0x68: b"6786786786\x00",
    0x69: b"5675675675\x00",
    0x6a: b"4564564564\x00",
    # Lowercase 'k'..'z' — alpha path (offset = pos % 10)
    0x6b: b"vutsrqponm\x00",
    0x6c: b"wvutsrqpon\x00",
    0x6d: b"xwvutsrqpo\x00",
    0x6e: b"yxwvutsrqp\x00",
    0x6f: b"zyxwvutsrq\x00",
    0x70: b"kzyxwvutsr\x00",
    0x71: b"lkzyxwvuts\x00",
    0x72: b"mlkzyxwvut\x00",
    0x73: b"nmlkzyxwvu\x00",
    0x74: b"onmlkzyxwv\x00",
    0x75: b"ponmlkzyxw\x00",
    0x76: b"qponmlkzyx\x00",
    0x77: b"rqponmlkzy\x00",
    0x78: b"srqponmlkz\x00",
    0x79: b"tsrqponmlk\x00",
    0x7a: b"utsrqponml\x00",
}


def decode(s: bytes) -> str:
    """Decode an obfuscated TVW header Pascal string."""
    out = bytearray(len(s))
    for pos, c in enumerate(s):
        row = _ROWS.get(c)
        if row is None:
            out[pos] = c
        elif (0x30 <= c <= 0x39) or (0x61 <= c <= 0x6a):
            out[pos] = row[pos % 3]
        else:
            out[pos] = row[pos % 10]
    return out.decode("latin-1")


def encode(s: str) -> bytes:
    """Inverse of `decode`. Used by tests; not on the parsing path."""
    out = bytearray(len(s))
    for pos, c in enumerate(s.encode("latin-1")):
        out[pos] = _encode_one(c, pos)
    return bytes(out)


def _encode_one(plain: int, pos: int) -> int:
    """Find the input char that `decode` maps to `plain` at position `pos`."""
    for src, row in _ROWS.items():
        if (0x30 <= src <= 0x39) or (0x61 <= src <= 0x6a):
            if row[pos % 3] == plain:
                return src
        else:
            if row[pos % 10] == plain:
                return src
    return plain  # passes through if no mapping
