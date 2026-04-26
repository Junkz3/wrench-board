# Boardview formats v1 — complete the 9 remaining parsers

Supersedes the STUB entries in
`docs/superpowers/specs/2026-04-22-boardview-formats-roadmap.md`.

## Goal

Promote `.fz`, `.bdv`, `.asc`, `.bv`, `.gr`, `.cst`, `.tvw`, `.f2b`, `.cad`
from STUB → DONE. Every parser is clean-room: written from scratch against
public format descriptions (OpenBoardView issue trackers, reverse-engineering
gists, EDA forum notes). No code copied from OBV, FlexBV, or any external
codebase. Apache 2.0, permissive deps only.

Every parser emits the same `api/board/model.py::Board` pydantic object that
already drives `brd_viewer.js` — the frontend and the agent stack are
unchanged. A `.bv` upload renders exactly like a `.brd` upload.

## Required contract (unchanged)

Every `parse()` returns a `Board` with:
- `board_id`, `file_hash`, `source_format`
- `outline: list[Point]` (may be empty)
- `parts: list[Part]` — `refdes`, `layer`, `is_smd`, `bbox(min,max)`, `pin_refs`
- `pins: list[Pin]` — `part_refdes`, `index` (1-based within part), `pos`, `net`, `probe`, `layer`
- `nets: list[Net]` — `name`, `pin_refs`, `is_power`, `is_ground`
- `nails: list[Nail]` — may be empty

Optional enrichments (`value`, `footprint`, `rotation_deg`, `pad_shape`,
`pad_size`, `pad_rotation_deg`) stay `None` unless the format carries them.

## Format families

Three layout families cover all nine formats:

### Family A — ASCII block-based (plaintext)
`.bv`, `.gr`, `.cad`, `.cst`, `.f2b`

All five descend from the historical OBV/BRD2 shape: an ASCII file with
block markers (`Parts:` / `PARTS:` / `Components:`, `Pins:` / `PINS:`,
`Nets:` / `NETS:`, `Nails:` / `NAILS:`, `Format:` / `BRDOUT:` for the
board outline) followed by fixed-width numeric lines. Exact marker
spelling and field order vary per vendor, but the shape is the same.

Strategy: one shared helper module `_ascii_boardview.py` exposes a
`BlockParser` configured with:
- `outline_markers`, `parts_markers`, `pins_markers`, `nets_markers`, `nails_markers`
- `parts_format`: `test_link` (`refdes type_layer end_of_pins`) or
  `brd2_bbox` (`refdes x1 y1 x2 y2 first_pin side`)
- `pins_format`: `test_link` (`x y probe part_idx [net]`) or
  `brd2_netid` (`x y net_id side`)
- `nails_format`: `test_link` (`probe x y side net`) or
  `brd2_netid` (`probe x y net_id side`)

Each concrete parser instantiates `BlockParser` with its format quirks
and calls `.parse()`. No duplicated block-walking logic.

### Family B — Obfuscated text (binary → decode → ASCII)
`.bdv`, `.tvw`, `.fz`

These wrap ASCII boardview data inside a trivial obfuscation layer:

- **`.bdv`** (HONHAN): arithmetic substitution. `clear = key - cipher`,
  `key` starts at 160, increments per byte, wraps from 285 back to 159.
  Bytes 13/10 pass through untouched (preserves line breaks). After
  decode → plain ASCII boardview → Family A parse.
- **`.tvw`** (Teboview): per-character rotation cipher. Digits rotate
  by 3, letters rotate by 10. Literal symbols pass through. Header
  signature `O95w-28ps49m 02v9o.` decodes to `Tebo-ictview files.`.
  After decode → Family A parse.
- **`.fz`** (PCB Repair Tool): XOR-stream cipher with a 16-byte sliding
  window. Each byte is XORed with the low 8 bits of a 32-bit value
  derived from the first 4 bytes of the window, then the window shifts
  left and the new cleartext byte lands at position 15. The cipher is
  seeded by a 44×32-bit key that ASUS ships with every `.fz` — the key
  is not embedded in the file. Without the key we cannot decrypt.
  Policy: implement the descrambling structure, raise
  `MissingFZKeyError` (new, subclass of `InvalidBoardFile`) when no key
  is supplied. The key can be passed via `FZParser(key=...)` or the
  `WRENCH_BOARD_FZ_KEY` environment variable (hex-space-separated 44
  32-bit words).

### Family C — Multi-file ASCII (ASUS TSICT)
`.asc`

The TSICT tool originally emits five files in a directory:
`format.asc`, `parts.asc`, `pins.asc`, `nails.asc`, `nets.asc`. In the
repair community, these are commonly redistributed as a single file
whose content is the concatenation of all five with their block
markers, or as a zip.

Strategy: two-phase detection inside `ASCParser.parse_file()`:
1. If the uploaded file already contains multiple block markers
   (combined form), parse directly via Family A.
2. Otherwise, look for sibling `parts.asc` / `pins.asc` / `nails.asc` /
   `format.asc` / `nets.asc` in the same directory and concatenate
   them into a single logical text before parsing.

When invoked via raw `parse(raw, ...)` (no `path`), only phase 1
applies — a single-file upload that contains only one of the five
sub-sections raises `InvalidBoardFile("TSICT: incomplete payload")`.

## Shared helper: `api/board/parser/_ascii_boardview.py`

Single source of truth for:
- Block locator (`find_block`, `iter_block_lines`) — tolerant to
  case variations, trailing whitespace, blank lines between blocks.
- Part builder (Test_Link variant: bitmask layer/SMD; BRD2 variant: bbox inline).
- Pin linker (Test_Link `part_idx` 1-based ; BRD2 `first_pin` cumulative).
- Nail parser (shared across both variants).
- Net derivation (reuses `_POWER_RE` / `_GROUND_RE` from `test_link.py`).

Existing `test_link.py` and `brd2.py` stay as-is — they're stable and
tested. The new helper is additive.

## Failure modes

Every parser raises from the existing error hierarchy in `parser/base.py`:
- `InvalidBoardFile` — unknown encoding, missing sections, thin file.
- `MalformedHeaderError(field)` — known section present but malformed.
- `PinPartMismatchError(pin_index)` — pin references an unknown part.
- `ObfuscatedFileError` — existing, reused for `.bdv` / `.tvw` / `.fz`
  when the cipher signature is present but decoding fails.
- `MissingFZKeyError(InvalidBoardFile)` — new, for `.fz` without a key.

## Out of scope (deferred, captured in the roadmap)

- **Binary PCB-geometry formats** (KiCad-native aside, which we already
  handle via `pcbnew`): Eagle `.brd`, Altium `.PcbDoc`. Different
  category — full CAD sources, not boardview exports.
- **Live reverse-engineering** of `.fz` keys: community key lists
  exist; bundling them in-repo is legally unclear. Keep the runtime
  key-input path, leave sourcing to the tech.
- **Native KiCad gold-standard fields** for the text formats: only
  KiCad carries `value`, `footprint`, `rotation_deg` reliably. Leave
  these as `None` for the new parsers.

## Tests

Every parser ships with:
- A synthetic fixture under `tests/board/fixtures/` (safe, no
  proprietary data). Fixture is author-written against the format's
  public structure.
- A happy-path test asserting counts + a spot-check part.
- A malformed-file test asserting the right error class.
- For Family B: a decode round-trip test proving
  `encode(decode(cipher)) == cipher` on the synthetic fixture.

The existing `tests/board/test_stub_parsers.py` shrinks to empty —
all nine formats leave STUB status — so the file is deleted.
