"""Package-aware pad-shape inference.

A few boardview formats — `.fz` chief among them — ship per-pin
positions and radii but no pad shape. The wire format simply doesn't
encode whether a pad is rectangular (chip passive, leaded SMD, lead
of a QFP) or circular (BGA ball, mounting hole, test point). Real
boards mix both, and rendering everything as a circle visually
collapses chip-passive bodies into floating discs.

This module pattern-matches the `Part.footprint` (extracted from the
`SYM_NAME` column on `.fz`, or the `SHAPE` name on GenCAD) against the
universal PCB-fab convention:

    chip passive (0402 / 0603 / 0805 / 1206 / 1005 / 1608 …) → rect
    leaded SMD (SOT / SOIC / SOP / TSSOP / QFP / QFN / DFN / DPAK / DIP) → rect
    SMD inductor (`INDUCTOR…` / `SMIND…`)                            → rect
    grid array (BGA / CSP / LGA / WLCSP)                              → circle
    mounting hole (MTG…)                                              → circle
    explicit test point (TPxxx)                                       → circle

Rect rules are evaluated first so they win over circle rules for
ambiguous footprints like `CAP1005_0_55H_BGA` — the `_BGA` suffix is a
position tag (placed under a BGA chip), not the actual package; the
chip itself is still a rectangular 1005-metric capacitor.

The function never fabricates: it returns `None` when no rule applies,
leaving the caller free to keep whatever upstream shape was set.
"""

from __future__ import annotations

import re

# Boundary regex: match start, end, or any non-letter (digit, dash, dot,
# underscore). Python's `\b` treats `_` as a word character, so `\b` does
# not split between `MB_` and `SOT669` — we need a fixed-width
# lookbehind/lookahead that excludes letters only.
_LB = r"(?<![A-Za-z])"   # left boundary: preceding char is not a letter
_RB = r"(?![A-Za-z])"    # right boundary: following char is not a letter

_RECT_PATTERNS = [
    # Bare imperial chip codes (`0402`, `0603`, `0805`, `1206`, `2010`,
    # `2512`) — many `.fz` files store the footprint as the dimension
    # alone, no `R`/`C`/`L` prefix. Boundary on both sides so we don't
    # eat coords or random digits.
    re.compile(_LB + r"(0402|0603|0805|1206|2010|2012|2512|3216)" + _RB),
    # Single-letter chip passives anchored at start: R0402, C0603,
    # L0805, Y2520. Tail must be `_` or end.
    re.compile(r"^[CRLY]\d{3,4}(?:_|$)", re.IGNORECASE),
    re.compile(r"^FB\d{3,4}(?:_|$)", re.IGNORECASE),
    # Same chip passives behind a project-namespace prefix:
    # `MB_R0402`, `MBS_R0402`, `MB_C0603`. Match `[CRL]\d{3,4}` after a
    # non-letter boundary.
    re.compile(_LB + r"[CRL]\d{3,4}" + _RB, re.IGNORECASE),
    # Long-name chip passives — match anywhere with non-letter boundary:
    # CAP1005, RES1005, MLCC0603, IND2012, SMIND1005, FUSE_…, FERR_…,
    # plus the short `IND_…` and `INDUC_…` Asian-dump variants.
    re.compile(_LB + r"(CAP|RES|MLCC|IND|SMIND|FUSE|FERR)\d{3,4}", re.IGNORECASE),
    re.compile(_LB + r"INDUC", re.IGNORECASE),
    re.compile(_LB + r"IND_SMD", re.IGNORECASE),
    # SMD electrolytic / polymer capacitors: `CAP_SMD_7343`,
    # `MB_CPL_820U_3V_…`, `MB_CE_820U_3V_…`, `CAP_E_6M3X9M0_P2M5`.
    re.compile(_LB + r"CAP_(SMD|E|ELEC|POLY)", re.IGNORECASE),
    re.compile(_LB + r"CPL_\d", re.IGNORECASE),
    re.compile(_LB + r"CE_\d", re.IGNORECASE),
    # SMD LED 2-pin: `LED_2P_63X31`.
    re.compile(_LB + r"LED_\d", re.IGNORECASE),
    # Leaded packages, anchored on the package keyword anywhere:
    # `MB_SOT669_COLAY`, `HH_SOT363_SIRENZA`, `SMIND_TSSLP-2-1_0_40H`,
    # plus BOM-style dashed forms `SOT-23`, `TSOT-23-8`.
    re.compile(_LB + r"(T?SOT)[-_]?\d+", re.IGNORECASE),
    re.compile(_LB + r"SLP\d+", re.IGNORECASE),  # diode SLP packages
    re.compile(_LB + r"(TSOP|MSOP|SOP|SOIC|TSSOP|SSOP|TSSLP)[-_]?\d*", re.IGNORECASE),
    # SO followed by digits — generic small-outline (SO8, SO16, …).
    # Only match when there's a number — bare `SO` would be too greedy.
    re.compile(_LB + r"SO\d+", re.IGNORECASE),
    re.compile(_LB + r"(QFP|LQFP|PQFP|TQFP|VQFP|QFN|TQFN|VQFN|MLF)\d*", re.IGNORECASE),
    # SON (Small Outline No-leads) — sister of QFN.
    re.compile(_LB + r"SON\d", re.IGNORECASE),
    re.compile(_LB + r"(DPAK|D2PAK|DDPAK)" + _RB, re.IGNORECASE),
    re.compile(_LB + r"DIP\d+", re.IGNORECASE),
    re.compile(_LB + r"(DFN|TDFN)\d*", re.IGNORECASE),
    re.compile(_LB + r"INDUCTOR", re.IGNORECASE),
    # POWERPAK is a Vishay leadless package.
    re.compile(_LB + r"POWERPAK", re.IGNORECASE),
    # SC-70 (SOT-353/363 cousin), TO-220 / TO-252 / TO-263 — leaded.
    re.compile(_LB + r"SC[\-_]?70", re.IGNORECASE),
    re.compile(_LB + r"TO\d{2,3}", re.IGNORECASE),
    # Chokes, pulse inductors.
    re.compile(_LB + r"CHOKE", re.IGNORECASE),
    re.compile(_LB + r"PULSE", re.IGNORECASE),
    # SOD (Small Outline Diode) and DIOSOD prefix.
    re.compile(_LB + r"SOD\d", re.IGNORECASE),
    re.compile(_LB + r"DIOSOD", re.IGNORECASE),
    # SMA / SMB / SMC — JEDEC DO-214 SMD diode packages.
    re.compile(r"^SM[ABC]" + _RB),
    # SMD fuses.
    re.compile(_LB + r"FUSE_\d", re.IGNORECASE),
    re.compile(_LB + r"FUSE\d", re.IGNORECASE),
    # Shorting pads / bridges — flat SMD lands.
    re.compile(_LB + r"SHORTPIN", re.IGNORECASE),
    re.compile(_LB + r"NET_SHORT", re.IGNORECASE),
    # Connector refdes prefixes (CN1, CN12, J1, J100). ASUS dumps with
    # the SYM_NAME==REFDES quirk (DUAL 1060 etc.) ship no footprint or
    # BOM value for the PCI-Express edge connector or the screw-on IO
    # connectors — `^CN\d` / `^J\d` are the safe last-resort match.
    re.compile(r"^(CN|J)\d+(?:_|$)", re.IGNORECASE),
    # IO connector keywords inside BOM descriptions.
    re.compile(_LB + r"(HDMI|USB|DISPLAYPORT|DVI|VGA|DP_SCREW)", re.IGNORECASE),
    re.compile(_LB + r"CONNECTOR", re.IGNORECASE),
    re.compile(_LB + r"\bCON\s\d", re.IGNORECASE),
    # Edge connectors.
    re.compile(_LB + r"EDGECON", re.IGNORECASE),
    # `_SHORT` and `_OPEN` substrings — bridging / open SMD pads on
    # motherboard / netbook dumps (`NB_PWR_FB_SHORT_PT`,
    # `NB_3MM_OPEN_5MIL_LF`).
    re.compile(_LB + r"SHORT" + _RB, re.IGNORECASE),
    re.compile(_LB + r"OPEN" + _RB, re.IGNORECASE),
    # Resistor networks (`RN1`, `RN5` — chip arrays with multiple
    # rectangular pads).
    re.compile(r"^RN\d+$", re.IGNORECASE),
    # 1x1P pin headers / PINREX header strips.
    re.compile(_LB + r"PINREX", re.IGNORECASE),
    re.compile(_LB + r"\dX\dP" + _RB, re.IGNORECASE),
    # Bare SMD tantalum / polymer capacitor codes (`7343`, `7520`,
    # `3528`, `6032`, `2917`, `2812`, `1812`). Match anywhere so the
    # codes work both as standalone footprints and embedded in BOM
    # descriptions like `POSCAP 330UF/2.5V(3528)`.
    re.compile(_LB + r"(7343|7520|3528|6032|2917|2812|1812)" + _RB),
    # BOM-description keywords — used as a fallback when the SYM_NAME
    # column duplicates the refdes (Asus DUAL 1060 dumps). These are
    # plain English / lowercase-aware words from the BOM stream.
    re.compile(_LB + r"MOSFET", re.IGNORECASE),
    re.compile(_LB + r"MLCC" + _RB, re.IGNORECASE),
    re.compile(_LB + r"POSCAP", re.IGNORECASE),
    re.compile(_LB + r"FERRITE", re.IGNORECASE),
    re.compile(_LB + r"CONVERTER", re.IGNORECASE),
    re.compile(_LB + r"DRIVER" + _RB, re.IGNORECASE),
    # Plain English BOM keywords — `CAP PL 270UF/16V`, `RESISTOR`,
    # `CAPACITOR`, `INDUCTOR ARRAY`, `DIODE BAS16`. Match the keyword
    # followed by a separator so we don't sweep up substrings like
    # `MICROCAPSULE`. (`MOSFET`, `FERRITE` etc. above already covered.)
    re.compile(_LB + r"(CAP|RES|DIODE|TRANS|FUSE|CHOKE|RELAY|CRYSTAL|XTAL)[\s_-]", re.IGNORECASE),
    re.compile(_LB + r"(CAPACITOR|RESISTOR|INDUCTOR|TRANSISTOR)" + _RB, re.IGNORECASE),
    # Polymer / electrolytic capacitor specific dialects:
    # `PL EL 470UF/2V (7343/D)`, `CAP PL`.
    re.compile(_LB + r"PL[\s_]EL" + _RB, re.IGNORECASE),
    # Power packages — PRPAK (Pulse Research SMD MOSFET pack), DPAK
    # variants, PowerPAK (Vishay), CSP (chip-scale).
    re.compile(_LB + r"PRPAK", re.IGNORECASE),
]

_CIRCLE_PATTERNS = [
    # BGA / CSP / LGA / WLCSP — accept either a digit or an underscore
    # immediately after the keyword (`BGA080…`, `BGA_0170_…`).
    re.compile(r"^(BGA|FBGA|CSP|LGA|WLCSP)[\d_]", re.IGNORECASE),
    re.compile(r"^MTG", re.IGNORECASE),
    re.compile(r"^HOLE", re.IGNORECASE),
    # Test points: `TP050`, `TP100`, `TP055PRI`, `TP050X025`,
    # `TP0_127SQ_PRI`. The trailing suffix can be anything (height,
    # variant tag, "PRI" for primary side).
    re.compile(r"^TP\d", re.IGNORECASE),
]


def infer_pad_shape(footprint: str | None) -> str | None:
    """Return `"rect"` / `"circle"` for the given footprint, `None` when
    no rule applies.

    Rect rules are tried first so chip-under-BGA suffixes like
    `CAP1005_0_55H_BGA` resolve to rect (matching the chip's actual
    package) instead of circle (matching the trailing position tag).
    """
    if not footprint or not footprint.strip():
        return None
    for pat in _RECT_PATTERNS:
        if pat.search(footprint):
            return "rect"
    for pat in _CIRCLE_PATTERNS:
        if pat.search(footprint):
            return "circle"
    return None
