# Plan — complete the 9 boardview parsers

Spec: `docs/superpowers/specs/2026-04-25-boardview-formats-v1.md`.

Execution order: shared helper first (unblocks everything), Family A
formats next (simplest), Family B (decoders + ASCII delegation), Family
C (multi-file ASUS) last. Test-as-you-go — never commit a parser
without its happy-path + malformed-file tests green.

## T1 — Shared ASCII helper `_ascii_boardview.py`

Add `api/board/parser/_ascii_boardview.py` exposing:

- `BlockMarkers` dataclass — configurable per format (outline, parts,
  pins, nets, nails markers, plus optional `header_var_data` /
  `header_brdout` for the `var_data: 4 2 4 1` or `BRDOUT: n w h`
  count-bearing headers).
- `PartsLayout = Literal["test_link", "brd2_bbox"]`
- `PinsLayout = Literal["test_link", "brd2_netid"]`
- `NailsLayout = Literal["test_link", "brd2_netid"]`
- `BlockParser(markers, parts_layout, pins_layout, nails_layout,
  source_format).parse(text, *, board_id, file_hash) -> Board`
- `derive_nets(pins)` helper — same regexes as `test_link.py`, but
  imported from a shared module to avoid the current cross-import
  (`brd2.py` → `test_link.py`).
- `normalize_bbox(x1, y1, x2, y2)` helper.

Tests: `tests/board/test_ascii_boardview_helper.py` covers block
locator edge cases (blank lines, missing blocks with n==0, extra
tokens on part lines) and the two layout variants.

## T2 — `.bv` (ATE BoardView)

Historical convention: ASCII, Test_Link-shaped with `BoardView 1.5`
header, `Parts:`/`Pins:`/`Nails:` markers. Configure a `BlockParser`
with Test_Link layout. Fixture `tests/board/fixtures/minimal.bv`
synthetic 2-part 4-pin.

Deliverable: `api/board/parser/bv.py` replaces stub; test
`tests/board/test_bv_parser.py`.

## T3 — `.gr` (BoardView R5.0)

Text ASCII, R5.0 dialect uses `Components:` and `Nets:` markers on
parts/nets blocks respectively, Test_Link-shaped counts. Configure
accordingly. Fixture `minimal.gr` synthetic.

Deliverable: `api/board/parser/gr.py` replaces stub; test
`tests/board/test_gr_parser.py`.

## T4 — `.cad` (Generic BoardViewer)

BoardViewer 2.1.0.8 umbrella → best-effort: accept both the `PARTS:` /
`PINS:` / `NAILS:` uppercase convention (BRD2-shaped) and the
Test_Link fallback. `CADParser` tries BRD2 layout first, falls back to
Test_Link on sniff. Fixture `minimal.cad` synthetic BRD2-shaped.

Deliverable: `api/board/parser/cad.py` replaces stub; test
`tests/board/test_cad_parser.py`.

## T5 — `.cst` (IBM Lenovo CAST)

Castw v3.32 — ASCII, uses `[Components]` / `[Pins]` / `[Nails]`
bracketed section headers and INI-style `key=value` prelude for
counts. Configure a `BlockParser` with custom section detection (the
helper accepts either `:` suffix or `[brackets]`). Fixture
`minimal.cst` synthetic.

Deliverable: `api/board/parser/cst.py` replaces stub; test
`tests/board/test_cst_parser.py`.

## T6 — `.f2b` (Unisoft ProntoPLACE)

Unisoft Place5 converter output. Text ASCII with `Parts:`, `Pins:`,
`Nails:` — Test_Link-shape. Fixture `minimal.f2b`.

Deliverable: `api/board/parser/f2b.py` replaces stub; test
`tests/board/test_f2b_parser.py`.

## T7 — `.bdv` (HONHAN BoardViewer, decoder)

Add a decoder `_deobfuscate_bdv(raw: bytes) -> str` implementing:
- `key = 160`
- For each byte: if byte is `\r` or `\n`, emit as-is; else emit
  `chr(key - byte)` and `key += 1`; wrap `key` from 286 back to 159.

After decode → Test_Link-shape parse via `BlockParser`. Fixture:
synthesize a plaintext Test_Link file then encode it with the
inverse transform to produce `minimal.bdv`.

Deliverable: `api/board/parser/bdv.py` replaces stub; test
`tests/board/test_bdv_parser.py` with (1) decode round-trip, (2)
happy-path parse of the encoded fixture.

## T8 — `.tvw` (Tebo IctView, decoder)

Add `_deobfuscate_tvw(raw: bytes) -> str` implementing the per-char
rotation cipher:
- Digits rotate by 3: `'0' → '3'` on encode, `'3' → '0'` on decode.
  Decode: `d = (c - '0' - 3) mod 10 + '0'`.
- Letters rotate by 10: lowercase `'o' → 'y'` on encode, `'y' → 'o'`
  on decode. Apply mod-26 per case class.
- Other symbols pass through.

Assert the decoded header starts with `Tebo-ictview files.` (sanity).
After decode → Test_Link-shape parse. Fixture `minimal.tvw`
synthesized by encoding a plaintext.

Deliverable: `api/board/parser/tvw.py` replaces stub; test
`tests/board/test_tvw_parser.py` with (1) header-sanity assertion,
(2) round-trip decode, (3) happy-path parse.

## T9 — `.fz` (ASUS PCB Repair Tool, XOR descrambler)

Add `_deobfuscate_fz(raw: bytes, key: list[int]) -> bytes` implementing
the 16-byte sliding-buffer XOR described in spec Family B. Add
`MissingFZKeyError(InvalidBoardFile)` to `parser/base.py`. Key input:

- `FZParser(key=<tuple of 44 ints>)` constructor; or
- `WRENCH_BOARD_FZ_KEY` env var (`44` space- or hex-separated ints);
- Missing → `MissingFZKeyError`.

Output bytes are parsed as Test_Link-shape ASCII.

Fixture: we can't include a real `.fz` (proprietary data). Ship only
a negative-path test that asserts the right error class when no key
is provided and a synthetic round-trip test with a dummy key proving
the descrambler structure is symmetric (encode(decode(data)) == data
for a test key).

Deliverable: `api/board/parser/fz.py` replaces stub; test
`tests/board/test_fz_parser.py` (error-path + round-trip only).

## T10 — `.asc` (ASUS TSICT)

Implement `ASCParser` with two-phase detection in `parse_file()`:

1. If the uploaded single file contains ≥2 Test_Link-shape block
   markers (combined redistribution), parse directly.
2. Otherwise, if sibling `.asc` files exist in the same directory
   (`format.asc`, `parts.asc`, `pins.asc`, `nails.asc`, `nets.asc`),
   concatenate them with block markers inserted and parse.

When invoked via `parse(raw, ...)` (no `path` context, e.g. HTTP
upload through `/api/board/parse`), only phase 1 runs.

Fixture: `tests/board/fixtures/tsict_combined.asc` (single-file
combined form, synthetic).

Deliverable: `api/board/parser/asc.py` replaces stub; test
`tests/board/test_asc_parser.py` covering combined + split paths.

## T11 — Retire stub tests

Delete `tests/board/test_stub_parsers.py` (all nine formats are DONE).

## T12 — Run suite & fix

`make test` — fix any regressions. The upload endpoint
`POST /api/board/parse` must still accept all nine new extensions.
`make lint` clean.

## T13 — Roadmap update

Flip the nine STUB rows to DONE in
`docs/superpowers/specs/2026-04-22-boardview-formats-roadmap.md` with
a reference to the v1 spec and a one-line note about the fixture
policy for binary formats (synthetic only).

## T14 — Commits

One commit per parser family for reviewability:

1. `feat(board): shared _ascii_boardview helper` (T1)
2. `feat(board): .bv parser (ATE BoardView)` (T2)
3. `feat(board): .gr parser (BoardView R5.0)` (T3)
4. `feat(board): .cad parser (Generic BoardViewer)` (T4)
5. `feat(board): .cst parser (IBM Lenovo CAST)` (T5)
6. `feat(board): .f2b parser (Unisoft ProntoPLACE)` (T6)
7. `feat(board): .bdv parser (HONHAN BoardViewer)` (T7)
8. `feat(board): .tvw parser (Tebo IctView)` (T8)
9. `feat(board): .fz parser (ASUS PCB Repair Tool)` (T9)
10. `feat(board): .asc parser (ASUS TSICT)` (T10)
11. `test(board): retire stub-parser test as all formats DONE` (T11)
12. `docs(board): promote 9 boardview formats STUB → DONE` (T13)

Each commit uses `git commit -- <paths>` explicit-path form per
CLAUDE.md to stay safe under parallel-agent work.

## Out of scope

- Binary CAD sources (.PcbDoc, Eagle .brd).
- Embedding / distributing `.fz` keys in the repo.
- UI changes — the existing `brd_viewer.js` consumes `Board` unchanged.
