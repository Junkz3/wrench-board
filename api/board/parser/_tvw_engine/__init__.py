"""Tebo IctView 3.0/4.0 production-binary `.tvw` parser — pure Python.

Modules:

  * `cipher.py` — header-string substitution table (file-header strings
    only — layer header strings, source paths, and net names are plain
    Pascal strings)
  * `magic.py` — file magic detection (3 Pascal-prefixed constants at the
    start of every production-binary `.tvw` we surveyed)
  * `walker.py` — section-aware reader: file_header, layer headers,
    dcode (aperture) tables, pin records (variable-length, 19 base
    bytes + optional 3-47 byte extension), and the trailing network
    names list. Walks lines / arcs / surfaces / texts / probes / nails
    sections opaquely (their record layouts are not yet decoded).
  * `board_mapper.py` — `TVWFile` → `Board` adapter; groups pin records
    by `part_index` to synthesize one `Part` per logical component,
    surfaces real net names from `network_names`.

Pin → net association is not yet decoded — the optional 12 / 16 / 16
byte payload blocks inside the pin record extension are still opaque
and likely carry the net id. Currently every pin lands on a catch-all
`__unmapped__` carrier net; the device-specific net *names* (`+12V`,
`GND`, `PCIE_RX`, …) are still surfaced in `Board.nets` for the agent
to grep against.
"""
