# board_assets/ — attributions

This directory contains third-party hardware design files included as
reference fixtures for wrench-board. All files are redistributed
under their original open-hardware licenses, which require attribution.

## MNT Reform motherboard v2.5

- **Committed files**: `mnt-reform-motherboard.brd`,
  `mnt-reform-motherboard.kicad_pcb`
- **Upstream**: MNT Research GmbH — https://source.mnt.re/reform/reform
  (sources in `reform2-motherboard25-pcb/`)
- **License**: CERN-OHL-S-2.0 (Strongly Reciprocal Variant)
- **Notice**: This hardware design is licensed under the CERN
  Open Hardware Licence Version 2 — Strongly Reciprocal. You may
  redistribute and modify this design under the terms of that licence.
  This design is distributed WITHOUT ANY EXPRESS OR IMPLIED WARRANTY,
  including the implied warranties of MERCHANTABILITY, SATISFACTORY
  QUALITY and FITNESS FOR A PARTICULAR PURPOSE. Please see
  https://ohwr.org/cern_ohl_s_v2.txt for applicable conditions.

The `.brd` file is a BRD2-format derivative produced by running
`whitequark/kicad-boardview` (0BSD) against the `.kicad_pcb` source.

### Schematic PDF (local-only, not committed)

Fetch the matching v2.5 schematic PDF locally to exercise the schematic
ingestion pipeline integration tests. The PDF is gitignored
(`board_assets/*.pdf`) because of its size and because the same rule
keeps users' proprietary uploads out of the repo:

    curl -L -o board_assets/mnt-reform-motherboard.pdf \
      https://mntre.com/documentation/reform-handbook/_static/schem/reform2-motherboard25.pdf

CERN-OHL-S-2.0, upstream Eeschema export (KiCad, 12 pages A4, native
vector — no rasterisation), matches the v2.5 revision of the
`.kicad_pcb` source.

## whitequark kicad-boardview example

- **Files**: `web/boards/whitequark-example.brd` (outside this dir but
  listed here for completeness)
- **Upstream**: https://github.com/whitequark/kicad-boardview
- **License**: BSD Zero Clause License (0BSD)
- **Notice**: Permission to use, copy, modify, and/or distribute this
  software for any purpose with or without fee is hereby granted. No
  attribution is required under 0BSD; we include this anyway for
  transparency.

## Policy

Only committed fixtures are listed here. Users may upload their own
boardviews (proprietary or otherwise) through the UI at runtime — those
are never committed to this repo (per CLAUDE.md hard rule #4).
