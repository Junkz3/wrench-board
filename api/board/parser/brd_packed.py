"""Decoder for a legacy packed-binary boardview ``.brd`` container.

Some ``.brd`` files in the wild are not the open Test_Link / BRD2 ASCII
boardview but a packed *binary* container — a serialized in-memory heap image.
This module reads the structure that is deterministically recoverable from the
bytes of a file the user supplies, for interoperability:

  * the string heap (net names + footprint/device names), and
  * the placed-component records (refdes), in two on-disk layouts that share the
    same heap format at different container versions: a ``..1200`` layout with a
    fixed 64-byte sentinel record, and a ``..1300`` layout with a single
    contiguous fixed-stride record array keyed by a heap pointer to the refdes.

Per-pin coordinates and pin->net connectivity are NOT reliably recoverable from
this packed layout (they live behind a private, non-linearly-mapped pointer
graph), so the parser emits real parts + real nets but **no pins** — a
conservative, honest partial result. Recognised by a small set of 4-byte file
prefixes plus a tail marker.
"""

from __future__ import annotations

import re
import struct

from api.board.model import Board, Layer, Net, Part, Point
from api.board.parser._ascii_boardview import GROUND_RE, POWER_RE
from api.board.parser.base import (
    BoardParser,
    MalformedHeaderError,
    register,
)

# Four-byte file prefixes for the packed container. The first two bytes are a
# per-build tag; the trailing ``..1200`` / ``..1300`` is the container version.
_PACKED_BRD_MAGICS: frozenset[bytes] = frozenset(
    bytes.fromhex(h)
    for h in (
        "0a0a1200",
        "030c1300",
        "03101300",
        "060b1200",
        "0c0f1200",
        "04151300",
    )
)

# Tail marker every file of this family carries — a secondary recognition signal.
_WATERMARK = bytes.fromhex("4d41535445525f44455349474e")

_HEAP_START = 0x1200

# A real placed-component refdes: a 1-3 letter class prefix + digits. Used both
# to mine refdes from the heap (..1300 fallback) and to exclude refdes from the
# net-name set. Restricted to genuine component classes so BGA pad labels
# ("A1", "B14") that also match [A-Z]+digits are NOT mistaken for parts.
_COMPONENT_CLASSES = "CRLUDQJKPYXFTBVZHGSEMNW"
_REFDES_RE = re.compile(rf"^([{_COMPONENT_CLASSES}]{{1,3}})([0-9]{{1,5}})$")

# Footprint / device / library names that live in the same heap as net names.
# These are NOT nets; the hints below cover the families seen across the corpus
# (VX_C0402_SMALL, R_0805, DEV_317, VIA20D10A32, TF-127785, *_QFN48, …).
_FOOTPRINT_HINT_RE = re.compile(
    r"(_[0-9]{4}(_|$)"           # ..._0402  ..._0805_...
    r"|^VX_|^VIA|^DEV_|^TF-|^FID|^DIM_"
    r"|^[A-Z]_[0-9]{3,4}"        # C_0402  R_0805
    r"|_SMALL$|_BIG$|_SMALL_|_BIG_"
    r"|_H[0-9]+(_|$)"
    r"|_(QFN|BGA|SOT|SOD|DPAK|SOIC|TSOP|QFP|SON|DFN|LGA)[0-9]*"
    r")"
)

# A 64-byte component record (..1200 versions): inline refdes at [0:20],
# sentinel dword 0x00001c00 at +0x38.
_COMP_REC_SIZE = 0x40
_COMP_SENTINEL_OFF = 0x38
_COMP_SENTINEL = 0x00001C00
# Component records start well past the header; the heap ends by ~0xC000 on the
# largest corpus file, so scanning from here never hits the header/heap.
_COMP_SCAN_START = 0xC000


def _looks_like_packed_brd(raw: bytes) -> bool:
    """True if `raw` is a packed-binary `.brd` container (prefix or tail marker)."""
    if len(raw) < 8:
        return False
    if raw[:4] in _PACKED_BRD_MAGICS:
        return True
    # Fallback: the tail marker (covers a future prefix we haven't seen).
    return _WATERMARK in raw[-512:]


def _walk_string_heap(raw: bytes) -> list[tuple[int, str]]:
    """Walk the densely-packed string heap from 0x1200.

    Each entry = <4-byte LE absolute heap pointer><NUL-terminated ASCII string>
    padded to a 4-byte boundary. Returns ``(tag, string)`` pairs in file order,
    where ``tag`` is the pointer's high 16 bits (the per-file segment id).

    Walks contiguously and stops at the first sizeable gap of non-entry bytes —
    the real heap is densely packed, so a >=256-byte run of junk marks its end
    (and keeps us out of the binary record clusters that follow).
    """
    n = len(raw)
    out: list[tuple[int, str]] = []
    i = _HEAP_START
    gap = 0
    while i < n - 5:
        ptr = struct.unpack_from("<I", raw, i)[0]
        end = raw.find(b"\x00", i + 4)
        body = raw[i + 4 : end] if end > i + 4 else b""
        ok = (
            0 < end - (i + 4) <= 48
            and (ptr >> 16) != 0
            and all(33 <= b < 127 for b in body)
        )
        if ok:
            out.append((ptr >> 16, body.decode("latin-1")))
            i = (end + 1 + 3) & ~3
            gap = 0
        else:
            i += 4
            gap += 4
            if gap >= 256 and out:
                break
    return out


def _extract_component_records(raw: bytes) -> list[str]:
    """Extract refdes from the 64-byte component records (..1200 versions).

    A record carries an inline refdes in bytes [0:20] and the sentinel dword
    ``0x00001c00`` at +0x38. Returns the de-duplicated refdes list in file order.
    Empty when the file is a ..1300 version (sentinel absent).
    """
    n = len(raw)
    seen: set[str] = set()
    out: list[str] = []
    o = _COMP_SCAN_START
    # Records are 4-byte aligned; scan at 4-byte steps so we lock onto the array.
    while o + _COMP_REC_SIZE <= n:
        if struct.unpack_from("<I", raw, o + _COMP_SENTINEL_OFF)[0] != _COMP_SENTINEL:
            o += 4
            continue
        z = raw.find(b"\x00", o, o + 20)
        if z <= o:
            o += 4
            continue
        name = raw[o:z]
        if all(33 <= b < 127 for b in name) and _REFDES_RE.match(name.decode("latin-1")):
            r = name.decode("latin-1")
            if r not in seen:
                seen.add(r)
                out.append(r)
            o += _COMP_REC_SIZE
        else:
            o += 4
    return out


# --- ..1300 part recovery (structural; see module docstring) ----------------
#
# The ..1300 part list is a SINGLE contiguous fixed-stride record array whose
# record holds, at field +0x10, a heap pointer to the owner refdes. We find it
# structurally (no version magic): index every self-pointer record cluster, then
# pick the single-cluster array whose +0x10 field maps to a refdes string for a
# strong majority of its records with a near-1.0 unique-per-record ratio.

# Strides seen across the ..1300 part arrays (32/40 in the corpus; the wider set
# keeps a future variant in range).
_CLUSTER_STRIDES: tuple[int, ...] = (24, 28, 32, 36, 40, 44, 48, 52, 56, 60, 64, 68, 72, 80, 104)
# The part record's refdes-pointer field. Constant +0x10 across the validated
# ..1300 samples.
_PART_REFDES_FIELD = 0x10
# A cluster must have at least this many records to be the part array (filters
# out the many small graph-node clusters).
_PART_MIN_RECORDS = 300
# Fraction of records whose +0x10 must resolve to a refdes, and the minimum
# unique-per-record ratio (each part is placed exactly once → ~1.0).
_PART_REFDES_FRACTION = 0.6
_PART_UNIQUE_RATIO = 0.85


def _self_pointer_clusters(raw: bytes, min_run: int = 8) -> list[tuple[int, int, int, int]]:
    """Index every fixed-stride self-pointer record cluster.

    Each record's FIRST dword is its own original heap self-address; within a
    cluster it increments by exactly the record stride. Returns
    ``(file_offset, stride, count, segment)`` per detected cluster. Kept inline
    so the import path stays self-contained.
    """
    n = len(raw)
    out: list[tuple[int, int, int, int]] = []
    o = _HEAP_START
    while o < n - 8:
        a0 = struct.unpack_from("<I", raw, o)[0]
        best: tuple[int, int] | None = None
        for st in _CLUSTER_STRIDES:
            if o + st * 2 + 4 > n:
                continue
            a1 = struct.unpack_from("<I", raw, o + st)[0]
            a2 = struct.unpack_from("<I", raw, o + 2 * st)[0]
            if a1 - a0 == st and a2 - a0 == 2 * st and (a0 >> 16) >= 0x0500:
                cnt = 1
                while (
                    o + cnt * st + 4 <= n
                    and struct.unpack_from("<I", raw, o + cnt * st)[0] == a0 + st * cnt
                ):
                    cnt += 1
                if cnt >= min_run:
                    best = (st, cnt)
                    break
        if best:
            st, cnt = best
            out.append((o, st, cnt, a0 >> 16))
            o += st * cnt
        else:
            o += 4
    return out


def _heap_addr_index(raw: bytes) -> dict[int, str]:
    """Map every heap entry's stored self-address → its string.

    The string heap stores each entry as ``<4B self-addr><ASCII><NUL>`` padded
    to 4B; the self-addr is the value other records point at. This index lets a
    record's +0x10 pointer resolve directly to the refdes it owns. Over-collects
    binary noise the same way ``_walk_string_heap`` does, but the refdes filter
    at the call site rejects it.
    """
    n = len(raw)
    out: dict[int, str] = {}
    i = _HEAP_START
    while i < n - 5:
        ptr = struct.unpack_from("<I", raw, i)[0]
        end = raw.find(b"\x00", i + 4)
        if 0 < end - (i + 4) <= 48 and (ptr >> 16) != 0:
            body = raw[i + 4 : end]
            if all(33 <= b < 127 for b in body):
                out[ptr] = body.decode("latin-1")
                i = (end + 1 + 3) & ~3
                continue
        i += 4
    return out


def _extract_1300_parts(raw: bytes) -> list[str]:
    """Recover the placed-part refdes list for a ..1300 file.

    Returns the de-duplicated refdes in file order, or ``[]`` if no part array
    is found (then the caller falls back to the ..1200 sentinel path / no parts).
    """
    addr2str = _heap_addr_index(raw)
    if not addr2str:
        return []
    best_refs: list[str] | None = None
    best_unique = 0
    for off, st, cnt, _seg in _self_pointer_clusters(raw):
        if cnt < _PART_MIN_RECORDS or st <= _PART_REFDES_FIELD:
            continue
        refs: list[str] = []
        for k in range(cnt):
            v = struct.unpack_from("<I", raw, off + k * st + _PART_REFDES_FIELD)[0]
            s = addr2str.get(v)
            if s and _REFDES_RE.match(s):
                refs.append(s)
        if len(refs) < cnt * _PART_REFDES_FRACTION:
            continue
        unique = len(set(refs))
        # Each placed part appears exactly once → a genuine part array has a
        # near-1.0 unique-per-record ratio. Graph-node clusters that happen to
        # reference refdes repeat them (ratio ~0.5) and are rejected.
        if unique / len(refs) < _PART_UNIQUE_RATIO:
            continue
        if unique > best_unique:
            best_unique = unique
            # de-dup, order-preserving
            seen: set[str] = set()
            ordered: list[str] = []
            for r in refs:
                if r not in seen:
                    seen.add(r)
                    ordered.append(r)
            best_refs = ordered
    return best_refs or []


def _net_names_from_heap(entries: list[tuple[int, str]]) -> list[str]:
    """Pick the net names out of the string heap.

    Net names = entries that are neither a refdes nor a footprint/device name nor
    obvious heap noise. Order-preserving, de-duplicated. (We do NOT also return
    refdes: heap refdes-tokens are polluted by BGA/connector pad-grid labels and
    are unsafe as a parts source — parts come from the 64-byte records instead.)
    """
    nets: list[str] = []
    net_seen: set[str] = set()
    for _tag, s in entries:
        if not s or len(s) < 2 or len(s) > 48:
            # Length-1 / over-long strings are heap noise, not net names.
            continue
        if _REFDES_RE.match(s):
            continue
        if _FOOTPRINT_HINT_RE.search(s):
            continue
        # A net name carries at least one alphanumeric char; reject pure
        # punctuation runs that the heap walk can incidentally pick up.
        if not any(c.isalnum() for c in s):
            continue
        if s not in net_seen:
            net_seen.add(s)
            nets.append(s)
    return nets


def _is_power(name: str) -> bool:
    return bool(POWER_RE.match(name)) or name.startswith("+") or name.startswith("PP")


def decode_packed_brd(raw: bytes, *, file_hash: str, board_id: str) -> Board:
    """Partially decode a packed-binary `.brd` into a Board.

    Emits real Parts (refdes) and real Nets (names + power/ground). Emits no
    pins — pad coordinates and pin->net connectivity live behind the container's
    private, non-linearly-mapped heap pointers (documented blocker; see module
    docstring). Raises ``MalformedHeaderError`` if the file is recognised as the
    packed family but no usable structure can be recovered.
    """
    if not _looks_like_packed_brd(raw):
        raise MalformedHeaderError("packed-brd-magic")

    heap = _walk_string_heap(raw)
    net_names = _net_names_from_heap(heap)

    # Parts: the ..1200 versions use the validated 64-byte sentinel records; the
    # ..1300 versions use the single-array part record (+0x10 -> refdes; see
    # module docstring). We key on the format-version byte (the 3rd byte of the
    # prefix: 0x13 => ..1300) so the ..1200 path is byte-for-byte untouched. If a
    # ..1300 file's part array isn't found, fall back to the sentinel scan
    # (harmless — it yields nothing on ..1300) so behaviour only ever improves.
    is_1300 = len(raw) >= 4 and raw[2:4] == b"\x13\x00"
    refdes = _extract_1300_parts(raw) if is_1300 else []
    if not refdes:
        refdes = _extract_component_records(raw)

    if not refdes and not net_names:
        # Recognised as the packed family but heap/records yielded nothing usable.
        raise MalformedHeaderError("packed-brd-structure")

    # No per-component layer is recoverable inline (it lives behind a pointer),
    # so default to TOP — a conservative choice that never misleads the renderer
    # into hiding a part.
    parts = [
        Part(
            refdes=r,
            layer=Layer.TOP,
            is_smd=True,  # SMD dominates these laptop boards; format carries no THT flag inline
            bbox=(Point(x=0, y=0), Point(x=0, y=0)),
            pin_refs=[],
        )
        for r in refdes
    ]

    nets = [
        Net(
            name=name,
            pin_refs=[],
            is_power=_is_power(name),
            is_ground=bool(GROUND_RE.match(name)),
        )
        for name in sorted(set(net_names))
    ]

    return Board(
        board_id=board_id,
        file_hash=file_hash,
        source_format="brd-packed",
        outline=[],
        parts=parts,
        pins=[],  # connectivity behind unresolved heap pointers — see docstring
        nets=nets,
        nails=[],
    )


@register
class PackedBinaryBoardParser(BoardParser):
    # Real files use the `.brd` extension (same as Test_Link / BRD2); the
    # content-sniff in base.py routes the packed-binary magic here. The synthetic
    # `.brd-packed` tag only keeps the registry non-colliding.
    extensions = (".brd-packed",)

    def parse(self, raw: bytes, *, file_hash: str, board_id: str) -> Board:
        return decode_packed_brd(raw, file_hash=file_hash, board_id=board_id)
