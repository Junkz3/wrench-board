# Boardview formats — roadmap

wrench-board is designed to read any PCB boardview format a technician might legitimately have. The parser architecture (`api/board/parser/`) dispatches via file extension + content-sniffing to a format-specific parser that populates the unified `api/board/model.py::Board` model. Adding a new format = one new file in `api/board/parser/`, registered automatically via the `@register` decorator. No changes to `base.py`, the validator, the agent, or the UI.

This document tracks the status of every format we know about.

## Fixture policy

Per CLAUDE.md hard rule #4 (**open hardware only**), we commit fixtures under `board_assets/` **only** for genuinely open-source hardware (MNT Reform, whitequark example, our synthetic bilayer). We do **not** commit proprietary boardviews (Apple, Samsung, ASUS, Lenovo, ZXW, WUXINJI, etc.). Users who have legitimately-acquired proprietary files upload them through the UI dropzone at runtime — their responsibility, not ours. The parser code itself is format-agnostic and may be distributed freely (precedent: OpenBoardView is open source and reads proprietary formats).

## Status key

- **DONE** — parser implemented, tested, wired into registry
- **STUB** — placeholder file exists, declares extension, raises `NotImplementedError` on `parse()`
- **FUTURE** — not yet stubbed

## Format matrix

| Extension | Format | Origin / vendor | Our parser | Status | Notes |
|-----------|--------|-----------------|------------|--------|-------|
| `.brd` | Test_Link | Landrex (80s) | `test_link.py::BRDParser` | **DONE** | Refuses OBV-signature obfuscated files. Content-sniffed via `str_length:` marker. |
| `.brd` | BRD2 | whitequark/kicad-boardview | `brd2.py::BRD2Parser` | **DONE** | Content-sniffed via `BRDOUT:` marker. 0BSD reference fixture at `web/boards/whitequark-example.brd`. |
| `.kicad_pcb` | KiCad native | KiCad project | `kicad.py::KicadPcbParser` | **DONE** | Rich source — value, footprint, rotation, pad shape / size. Via `pcbnew` Python API. |
| `.fz` | PCB Repair Tool / Boardview | mixed | `fz.py::FZParser` | **DONE (zlib variant)** / PARTIAL (XOR variant) | **FZ-zlib** is the dominant variant in the field — verified end-to-end on real Quanta BKL (2701 parts / 11438 pins / 1985 nets) and ASRock X470 (2833 / 11378 / 2018). 4-byte LE size + zlib + pipe-delimited columnar (`A!schema` / `S!data`). The OBV-documented XOR-key variant remains a fallback that needs `WRENCH_BOARD_FZ_KEY`. |
| `.bdv` | HONHAN BoardViewer | HONHAN (CN) | `bdv.py::BDVParser` | **DONE** | Arithmetic cipher (key 160, incr, wraps 286→159). Algorithm publicly documented (piernov gist). Decodes to Test_Link ASCII. |
| `.asc` | ASUS TSICT | ASUS | `asc.py::ASCParser` | **DONE** | Plain ASCII (confirmed via OBV issue #45). Accepts combined single-file or the five-file sub-directory layout. |
| `.bv` | ATE Boardview | ATE | `bv.py::BVParser` | **SPECULATIVE** | Test_Link-shape ASCII variant only. No public spec — production `.bv` likely binary. Binary payloads detected and rejected with hint. |
| `.gr` | BoardView R5.0 | generic | `gr.py::GRParser` | **SPECULATIVE** | Test_Link-shape ASCII with `Components:` / `TestPoints:` markers. No public spec — production `.gr` likely binary. Binary detection in place. |
| `.cst` | Card Analysis ST | IBM/Lenovo | `cst.py::CSTParser` | **SPECULATIVE** | Test_Link-shape ASCII with `[Components]` / `[Pins]` / `[Nails]` sections. Castw v3.32 is a 1990s tool with no published spec — production likely binary. Binary detection in place. |
| `.tvw` | Tebo IctView | Tebo | `tvw.py::TVWParser` | **PARTIAL** | Rotation cipher (digits 3, alpha 10) for the ASCII variant. Production binary layout (`fileformat-tvw.txt`: Pascal strings + layer sections) is **detected and rejected** with a clear hint — verified on a real Gigabyte H610M `.tvw`. Proper binary support is out of scope for v1. |
| `.f2b` | Unisoft ProntoPLACE | Unisoft | `f2b.py::F2BParser` | **SPECULATIVE** | Test_Link-shape with `Outline:` / `Components:` markers + `Annotations:` skip. Unisoft labels `.f2b` as proprietary "complete board save" — production almost certainly binary. Binary detection in place. |
| `.cad` | GenCAD 1.4 / BRD2 / Test_Link | Mentor / various | `cad.py::CADParser` | **DONE** | Umbrella verified on real ASUS Prime A520M (3442p/11049pins) and GRANGER 6050A2977701 (1451p/5711pins) — both ship as **GenCAD 1.4**. Dispatch order: FZ-zlib sniff → GenCAD `$HEADER`/`GENCAD` sniff → BRD2 `BRDOUT:` sniff → Test_Link fallback (with binary-detection guard). |

### Real-board verification status

Concrete topology stats for files validated end-to-end (via `tests/board/test_real_files_runner.py` against `/tmp/wrench-board-real-boards/`):

| Source board                                  | Format     | Parts | Pins   | Nets  | Nails |
|-----------------------------------------------|------------|-------|--------|-------|-------|
| Quanta BKL DABKLMB28A0 (BoardView)            | FZ-zlib    | 2 701 | 11 438 | 1 985 |     0 |
| ASRock X470 Master SLI/AC                     | FZ-zlib    | 2 833 | 11 378 | 2 018 |     0 |
| ASUS Prime A520M-A II                         | GenCAD 1.4 | 3 442 | 11 049 | 1 705 |     0 |
| GRANGER 6050A2977701 MB A01                   | GenCAD 1.4 | 1 451 |  5 711 | 1 083 |     0 |
| LPM-2 MB 18809-2 0GU05                        | BRD2       | 3 829 | 14 771 | 2 729 | 1 539 |
| Gigabyte H610M H DDR4 r1.0                    | TVW binary |   —   |    —   |   —   |   —   |
| MNT Reform Motherboard (CERN-OHL-S-2.0, repo) | BRD2       |   493 |  2 104 |   647 |     5 |
| whitequark example (0BSD, repo)               | BRD2       |   245 |  1 130 |   251 |    19 |

Total real bytes parsed across the seven non-rejected boards: ~25 400 parts, ~68 000 pins, ~10 400 nets, ~1 565 nails.

### Confidence levels

- **DONE** — verified against real open-hardware data or against a publicly documented algorithm decoding a real file in the wild.
- **PARTIAL** — structurally implemented, but a real-world signal is missing (`.fz` XOR variant needs the ASUS key; `.tvw` binary needs a container walker).
- **SPECULATIVE** — no public format specification and no real sample to-date; assumes a Test_Link-shape ASCII variant observed in some redistributions but the production native format is almost certainly a different binary container. Each speculative parser detects clearly-binary payloads and raises `ObfuscatedFileError` with a specific hint instead of silently emitting an empty `Board`.

When real samples land for `.bv` / `.gr` / `.cst` / `.f2b`, drop them in `/tmp/wrench-board-real-boards/` and run the runner — either the ASCII assumption holds (promote SPECULATIVE → DONE), or the runner surfaces a clean binary-rejection hint giving us the next reverse-engineering target.

## Unified model

All parsers populate the same `Board` object. Each format fills what it can; absent fields stay `None`. The frontend and agent degrade gracefully — a part with `value == None` renders as its `refdes` only, a part with `value == "10µF"` renders as `refdes + value`.

Required fields (every parser must fill these):
- `refdes`, `bbox`, `layer`, `pin_refs`
- `pin.pos`, `pin.net`, `pin.layer`, `pin.part_refdes`, `pin.index`

Optional enrichments (only richer formats — `.kicad_pcb` is the current gold standard):
- `part.value`, `part.footprint`, `part.rotation_deg`
- `pin.pad_shape`, `pin.pad_size`

## When to promote a STUB to DONE

1. A concrete user need arises (request, demo, repair scenario).
2. A legitimate open test fixture is available (ideally community-distributed, not leaked).
3. The format has public documentation or is reverse-engineered elsewhere under a permissive license (reference: OpenBoardView source).

Until then the stub file exists so that:
- the registry is already wired (a user uploading `.fz` gets a clean `501 Not Implemented`, not a confusing `415 Unsupported Format`)
- the scope is visibly tracked (anyone scanning `api/board/parser/` sees the roadmap at a glance)
- a future implementer has a drop-in location without touching `base.py`

## Fixtures policy for binary / obfuscated formats

For the three Family-B formats (`.fz`, `.bdv`, `.tvw`) we can't ship a real-world proprietary binary in the repo. Each parser's test suite therefore generates its synthetic fixture at authoring time by running the symmetric encoder on a plaintext Test_Link payload. The committed fixture is the encoded bytes; a "fixture-is-genuinely-encoded" test guards against the encoder silently regressing to a no-op. Real ASUS `.fz` files additionally require the user's 44×32-bit key (via `WRENCH_BOARD_FZ_KEY` or the constructor) — this stays a runtime concern.

## Testing real-world files

When a technician has a legitimate copy of a real boardview file (iPhone, ThinkPad, whatever lands on the bench — brand-unrestricted at runtime per the Open-hardware-rule-is-repo-only memory note), drop the file into any of these three directories — first populated one wins:

1. Path set via `WRENCH_BOARD_REAL_BOARDS_DIR` env var
2. `/tmp/wrench-board-real-boards/`
3. `~/Downloads/wrench-board-real-boards/`

Then `pytest tests/board/test_real_files_runner.py -v -s` parametrises one test per file, asserts the parse either succeeds or raises a documented known-limitation error (fz-key-missing, binary-TVW), cross-validates pin→part and net→pin on the real bytes, and prints a PASS summary with counts. Nothing in that dir is committed.

Verified against `whitequark/kicad-boardview/example/example.brd` (245 parts / 1130 pins / 251 nets — a real open-hardware BRD2 file, distinct from the MNT Reform committed fixture) transcoded through the test serializer into every new parser's dialect: all 10 parsers reproduce the source topology exactly.

## References

- v1 completion spec + plan: `docs/superpowers/specs/2026-04-25-boardview-formats-v1.md`
- OpenBoardView source (multi-format reader, MIT): https://github.com/OpenBoardView/OpenBoardView
- whitequark/kicad-boardview (0BSD, KiCad→BRD2/BVRAW): https://github.com/whitequark/kicad-boardview
- KiCad `.kicad_pcb` format spec: https://dev-docs.kicad.org/en/file-formats/sexpr-pcb/
- Format directory (catalog of boardview extensions): https://gist.github.com/vyach-vasiliev/35d610e14c40b4060f5d929ac70746a3
