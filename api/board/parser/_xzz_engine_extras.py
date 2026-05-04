"""Independent extractor for the XZZ post-v6 diagnostic block.

Sits next to the in-tree XZZ engine but doesn't touch it. The engine's
`parse_post_v6_block` recognizes the GB2312 section markers (阻值,
电压, 信号, 菜单) but its `_parse_resistance_section` expects a format
of `=value=component(pin)` lines that doesn't match the actual
`Net<id>=<value>` payload found on diagnostic-tagged XZZ exports —
the engine returns 0 entries while the section holds a complete
net→resistance mapping.

This module re-runs the decryption and walks the post-v6 block from
scratch using the bytes layout we observed:

    "v6v6555v6v6==="            base pattern, before any section
    <gb2312 marker, 4 bytes>    section type
    <gb2312 title, 2-4 bytes>   "图" / "表" suffixes (ignored)
    \r\n
    Net<id>=<value>\r\n         repeated lines until the next marker
    ...

Values are integers in ohms (manufacturer convention on diagnostic
dumps). Special tokens recognized:
- `>1000或OL` → expected_open=True (open / out-of-range)
- `0`        → ground / short-to-rail
- numeric    → resistance in ohms

We re-decrypt the buffer because the engine doesn't surface its
decrypted bytes; re-decryption is cheap (~50 ms on a 1.9 MB file).
Cost is paid once per parse, not per pin.
"""

from __future__ import annotations

import re

from api.board.parser._xzz_engine.xzz_file import XZZFile

# Pre-compiled GB2312 markers for the four post-v6 section types.
_RESISTANCE_MARKER = bytes.fromhex("D7E8D6B5")  # 阻值 (resistance)
_VOLTAGE_MARKER = bytes.fromhex("B5E7D1B9")     # 电压 (voltage)
_SIGNAL_MARKER = bytes.fromhex("D0C5BAC5")      # 信号 (signal name remap)
_MENU_MARKER = bytes.fromhex("B2CBB5A5")        # 菜单 (menu / metadata)

_BASE_PATTERN = b"v6v6555v6v6==="

# Match `Net<digits>=<rest>` lines after dropping CR/LF.
_NET_LINE_RE = re.compile(r"^(Net\d+)=(.*)$")

# `>1000或OL` = "≥1000 ohms or OL (over-limit / open-loop)" — the
# manufacturer's convention for "expect infinite resistance to ground".
# Other Chinese variants we accept: 开路 (kāilù = open-circuit), OL alone.
_OPEN_TOKENS = ("OL", "开路", "或OL")


def _decrypt_buffer(raw: bytes) -> bytes:
    """Re-run the in-tree XZZFile decryptor on a raw buffer.

    Returns the decrypted bytes. Uses a fresh XZZFile() instance so
    we don't mutate any caller's parser state.
    """
    return bytes(XZZFile()._decrypt_file(bytearray(raw)))


def _decode_section(payload: bytes) -> str:
    """GB2312-first decode with utf-8 / latin1 fallbacks. Net names
    and integer values are ASCII so any of the three works for the
    payload itself; we keep gb2312 for the title characters that
    follow the marker (e.g. 图 / 表 / 信号)."""
    for codec in ("gb2312", "utf-8", "latin1"):
        try:
            return payload.decode(codec)
        except UnicodeDecodeError:
            continue
    return payload.decode("latin1", errors="replace")


def _parse_resistance_payload(text: str) -> dict[str, dict[str, float | bool | None]]:
    """Extract `Net<id>=<value>` lines into a {net_name: {ohms, open}} map.

    The first line (after the marker) is the section title (`阻值图`
    etc.); we skip it.

    Each subsequent line is either:
    - `Net<id>=<integer>` → resistance in ohms (0 = short / ground rail)
    - `Net<id>=>1000或OL` → expected_open=True (≥1000 Ω or open-circuit)
    - `Net<id>=` (empty value) → ignored (no expectation recorded)
    - any other shape → skipped
    """
    out: dict[str, dict[str, float | bool | None]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _NET_LINE_RE.match(line)
        if m is None:
            continue
        net_name, value = m.group(1), m.group(2).strip()
        if not value:
            continue
        # Open / out-of-limit token. We accept `>1000`, `>1000或OL`,
        # `OL`, `开路` (kāilù). Numeric prefix (`>1000`) is normalized
        # to expected_open=True with no ohms reading.
        if any(tok in value for tok in _OPEN_TOKENS) or value.startswith(">"):
            out[net_name] = {"expected_resistance_ohms": None, "expected_open": True}
            continue
        # Plain integer (positive). Floats / fractional values weren't
        # observed in the surveyed fixtures but the parser accepts them.
        try:
            ohms = float(value)
        except ValueError:
            continue
        out[net_name] = {"expected_resistance_ohms": ohms, "expected_open": False}
    return out


def _parse_voltage_payload(text: str) -> dict[str, float]:
    """Voltage section payload: `Net<id>=<value>` lines, value in volts.

    Same line shape as resistance but no OL/open variant — voltages
    are always numeric. Returns {net_name: volts}."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _NET_LINE_RE.match(line)
        if m is None:
            continue
        try:
            out[m.group(1)] = float(m.group(2).strip())
        except ValueError:
            continue
    return out


def _parse_signal_payload(text: str) -> dict[str, str]:
    """Signal section: `Net<placeholder>=<real_signal_name>` lines.

    Resolves the v6+ placeholder net names (`Net204`) to their real
    schematic-level signal names (`PP_VBAT`, `PP3V3_G3H`, …). Already
    handled by the engine via `signal_map`, but we re-extract here for
    completeness — the in-engine extractor sometimes returns an empty
    dict when the rest of the post-v6 parser bails."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        placeholder, _, real = line.partition("=")
        placeholder = placeholder.strip()
        real = real.strip()
        if placeholder.startswith("Net") and real:
            out[placeholder] = real
    return out


def _parse_menu_payload(text: str) -> dict | list | str | None:
    """Menu section: per the XZZPCB-ImHex spec, free-form JSON data
    (typically a structured menu of diagnostic workflows the OEM
    embeds). We try `json.loads`; on failure return the raw string
    so the caller can still surface it.

    None of our four fixtures ship a menu section, so this is
    preemptive — the moment a board ships one, the dict / list is
    available in `diagnostics["menu"]` without further code changes.
    """
    import json

    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def extract_post_v6_diagnostics(raw: bytes) -> dict[str, dict]:
    """Top-level entry: decrypt the file, locate the post-v6 block,
    parse every section we recognise.

    Returns a dict with optional keys:
    - `resistance`: {net_name: {expected_resistance_ohms, expected_open}}
    - `voltage`:    {net_name: expected_volts}
    - `signal_map`: {placeholder_net_name: real_signal_name}

    Returns an empty dict when the buffer carries no diagnostic
    payload (most boards on the public web — only manufacturer-tagged
    XZZ exports ship this section).
    """
    out: dict[str, dict] = {}
    try:
        decrypted = _decrypt_buffer(raw)
    except (ValueError, OSError):
        return out

    base = decrypted.find(_BASE_PATTERN)
    if base < 0:
        return out
    blob = decrypted[base + len(_BASE_PATTERN):]

    # Locate every section's marker offset, sort by position so we can
    # carve adjacent slices [marker_n, marker_n+1).
    markers = [
        ("resistance", _RESISTANCE_MARKER),
        ("voltage", _VOLTAGE_MARKER),
        ("signal", _SIGNAL_MARKER),
        ("menu", _MENU_MARKER),
    ]
    found: list[tuple[int, str, bytes]] = []
    for name, m in markers:
        pos = blob.find(m)
        if pos >= 0:
            found.append((pos, name, m))
    found.sort()
    if not found:
        return out

    for i, (pos, name, m) in enumerate(found):
        section_start = pos + len(m)
        section_end = found[i + 1][0] if i + 1 < len(found) else len(blob)
        payload_text = _decode_section(blob[section_start:section_end])
        # Drop the leading title characters (e.g. "图\r\n" or "表\r\n")
        # — they precede the first \n.
        nl = payload_text.find("\n")
        body = payload_text[nl + 1:] if nl >= 0 else payload_text

        if name == "resistance":
            parsed = _parse_resistance_payload(body)
            if parsed:
                out["resistance"] = parsed
        elif name == "voltage":
            parsed_v = _parse_voltage_payload(body)
            if parsed_v:
                out["voltage"] = parsed_v
        elif name == "signal":
            parsed_s = _parse_signal_payload(body)
            if parsed_s:
                out["signal_map"] = parsed_s
        elif name == "menu":
            parsed_m = _parse_menu_payload(body)
            if parsed_m is not None:
                out["menu"] = parsed_m

    return out


# ---------------------------------------------------------------------------
# Type-03 markers (spec from XZZPCB-ImHex / Decoding unknown values.hexproj).
# Layout:  u8=0x03  u32=block_size(=36)  9*u32 payload
#   payload[0]  unknown — always 17 in observed samples (format marker?)
#   payload[1]  centre_x       (×1e6 = mm)
#   payload[2]  centre_y
#   payload[3]  bottom-left x
#   payload[4]  bottom-left y
#   payload[5]  top-right x
#   payload[6]  top-right y
#   payload[7]  unknown2 — 0 in observed samples
#   payload[8]  unknown3 — 0
# Function: re-decrypt the buffer and extract every type-03 block as a
# rectangle in absolute mm coords. The engine treats type-03 as
# UNKNOWN_3 and skips it; we don't modify the engine for this either.
#
# Empirical observation on a manufacturer-tagged fixture: ~15 blocks
# scattered across the board area, sizes from 0.5×0.5 mm to 5.6×5.4 mm.
# Likely "inspection regions" or component cluster bounding boxes the
# manufacturer marked for diagnosis. Most fixtures ship zero type-03
# blocks, so this is expected to surface only when the source format
# actually carries such overlays.
# ---------------------------------------------------------------------------

import struct as _struct

_TYPE_03_HEADER = b"\x03\x24\x00\x00\x00"  # u8=0x03, u32=36 LE
_MU_TO_MM = 1_000_000.0


def extract_type_03_markers(raw: bytes) -> list[dict]:
    """Return the list of type-03 marker rectangles in absolute mm.

    Each entry is `{centre_x, centre_y, x_min, y_min, x_max, y_max,
    width, height, marker}`. Empty list when the buffer carries no
    type-03 blocks.
    """
    out: list[dict] = []
    try:
        decrypted = _decrypt_buffer(raw)
    except (ValueError, OSError):
        return out

    pos = 0
    while True:
        idx = decrypted.find(_TYPE_03_HEADER, pos)
        if idx < 0:
            break
        pos = idx + 1
        payload = decrypted[idx + 5:idx + 5 + 36]
        if len(payload) < 36:
            continue
        try:
            vals = _struct.unpack("<9I", payload)
        except _struct.error:
            continue
        marker, cx, cy, blx, bly, trx, try_, _u2, _u3 = vals
        out.append({
            "centre_x": cx / _MU_TO_MM,
            "centre_y": cy / _MU_TO_MM,
            "x_min": blx / _MU_TO_MM,
            "y_min": bly / _MU_TO_MM,
            "x_max": trx / _MU_TO_MM,
            "y_max": try_ / _MU_TO_MM,
            "width": (trx - blx) / _MU_TO_MM,
            "height": (try_ - bly) / _MU_TO_MM,
            "marker": marker,
        })
    return out


# ---------------------------------------------------------------------------
# Type-09 test pads (from XZZPCB-ImHex `type_09` struct).
# These are standalone test pads — distinct from single-pin parts the
# engine converts to TEST_PAD via the part block 0x07 path.
# Layout (after the u8 type=0x09 byte and u32 block_size):
#   u32 pad_number
#   u32 x_origin
#   u32 y_origin
#   u32 inner_diameter (0 = solid pad, else inner hole)
#   u32 unknown1
#   u32 name_length
#   bytes[name_length] name
#   u32 outer_width_1, outer_height_1, u8 flag1   (ring 1)
#   u32 outer_width_2, outer_height_2, u8 flag2   (ring 2)
#   u32 outer_width_3, outer_height_3, u8 flag3   (ring 3 — outer pad)
#   u32 unknown2, u8 flag4
#   u32 net_index
#   optional: u32 reading_length, bytes reading
#
# The engine imports `parse_test_pad_block` but never dispatches
# block_type=0x09 → standalone test pads silently dropped on some
# fixtures (visible in screenshots as "missing components"). Re-extract
# independently.
#
# Coords: x_origin/y_origin in μm (1 unit = 1e-6 mm) → divide by
# 1e6 to get mm. Same factor as the engine's CONVERSION_FACTOR for
# parts. (parse_test_pad_block at parser_helpers.py:200 multiplies
# by an extra 10× — empirically wrong: applied to a manufacturer-tagged
# fixture, /1e7 yields ~53 mm absolute which falls OUTSIDE the
# 501-585 mm range of the board outline. /1e6 places the test pads
# inside the board area as expected.)
# ---------------------------------------------------------------------------

_TYPE_09_HEADER = b"\x09"
_TEST_PAD_MU_TO_MM = 1_000_000.0  # μm → mm


def _walk_main_blocks(decrypted: bytes) -> list[tuple[int, int, int, int]]:
    """Walk every recognized block in the main_data_blocks region and
    yield `(block_type, block_size, payload_start, total_block_len)`.

    Skips runs of leading null bytes (per the ImHex pattern's behavior
    on blocks that were padded out)."""
    if len(decrypted) < 0x44:
        return []
    main_size = _struct.unpack_from("<I", decrypted, 0x40)[0]
    end = 0x44 + main_size
    out = []
    offset = 0x44
    while offset < end and offset + 5 <= len(decrypted):
        # ImHex: skip 4 nulls (padding between blocks).
        if _struct.unpack_from("<I", decrypted, offset)[0] == 0:
            offset += 4
            continue
        block_type = decrypted[offset]
        if block_type not in (1, 2, 3, 4, 5, 6, 7, 8, 9):
            break
        block_size = _struct.unpack_from("<I", decrypted, offset + 1)[0]
        payload_start = offset + 5
        total_len = 5 + block_size
        out.append((block_type, block_size, payload_start, total_len))
        offset += total_len
    return out


def extract_type_09_test_pads(raw: bytes) -> list[dict]:
    """Return every type_09 test pad with its position, name, dimensions,
    net_index and optional reading. Empty list when the buffer carries
    no type_09 blocks.
    """
    out: list[dict] = []
    try:
        decrypted = _decrypt_buffer(raw)
    except (ValueError, OSError):
        return out

    for block_type, block_size, payload_start, _total in _walk_main_blocks(decrypted):
        if block_type != 9:
            continue
        if payload_start + 24 > len(decrypted):
            continue
        try:
            # Fixed header up to name_length
            pad_num = _struct.unpack_from("<I", decrypted, payload_start)[0]
            x = _struct.unpack_from("<I", decrypted, payload_start + 4)[0]
            y = _struct.unpack_from("<I", decrypted, payload_start + 8)[0]
            inner_d = _struct.unpack_from("<I", decrypted, payload_start + 12)[0]
            unk1 = _struct.unpack_from("<I", decrypted, payload_start + 16)[0]
            name_len = _struct.unpack_from("<I", decrypted, payload_start + 20)[0]
            cursor = payload_start + 24
            if cursor + name_len > len(decrypted):
                continue
            name = decrypted[cursor:cursor + name_len].decode("utf-8", errors="replace").strip("\x00").strip()
            cursor += name_len
            # Three (width, height, flag) ring blocks: 4+4+1 = 9 bytes each.
            rings = []
            for _ in range(3):
                if cursor + 9 > len(decrypted):
                    break
                rw = _struct.unpack_from("<I", decrypted, cursor)[0]
                rh = _struct.unpack_from("<I", decrypted, cursor + 4)[0]
                rf = decrypted[cursor + 8]
                rings.append((rw, rh, rf))
                cursor += 9
            # u32 unknown2 + u8 flag4 + u32 net_index
            if cursor + 9 > len(decrypted):
                continue
            unk2 = _struct.unpack_from("<I", decrypted, cursor)[0]
            flag4 = decrypted[cursor + 4]
            net_index = _struct.unpack_from("<I", decrypted, cursor + 5)[0]
            cursor += 9
            # Optional reading text — only present if there's room left.
            reading = ""
            block_end = payload_start + block_size
            if cursor + 4 <= block_end:
                reading_len = _struct.unpack_from("<I", decrypted, cursor)[0]
                cursor += 4
                if 0 < reading_len <= block_end - cursor:
                    reading = decrypted[cursor:cursor + reading_len].decode(
                        "utf-8", errors="replace"
                    ).strip("\x00").strip()
            # Pick the largest ring as the visible pad dimensions.
            outer = rings[-1] if rings else (0, 0, 0)
            ow_mm = outer[0] / _TEST_PAD_MU_TO_MM if outer[0] else 0.0
            oh_mm = outer[1] / _TEST_PAD_MU_TO_MM if outer[1] else 0.0
            out.append({
                "pad_number": pad_num,
                "name": name,
                "x_mm": x / _TEST_PAD_MU_TO_MM,
                "y_mm": y / _TEST_PAD_MU_TO_MM,
                "inner_diameter_mm": inner_d / _TEST_PAD_MU_TO_MM if inner_d else 0.0,
                "outer_width_mm": ow_mm,
                "outer_height_mm": oh_mm,
                "net_index": net_index,
                "reading": reading,
            })
        except (_struct.error, ValueError):
            continue
    return out
