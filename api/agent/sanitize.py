"""Post-hoc refdes sanitizer.

Second layer of defense against hallucinated component IDs. The first
layer is tool discipline (mb_get_component returns {found: false} for
unknown refdes); this layer scans outbound agent text and wraps
refdes-shaped tokens that don't resolve on the current board.
"""

from __future__ import annotations

import re

from api.board.model import Board
from api.board.validator import is_valid_refdes

REFDES_RE = re.compile(r"\b[A-Z]{1,3}\d{1,4}\b")


# Bus / interface / standard names that match the refdes regex
# (`[A-Z]{1,3}\d{1,4}`) but are NOT references to physical components.
# Wrapping them as `⟨?USB3⟩` is a false-positive that erodes user trust:
# when the agent says "the USB3 D+ line is degraded", the technician
# should see the bus name verbatim, not a phantom-refdes warning.
#
# This is preferred over a regex tweak because it's easy to extend and
# easy to read. A genuine refdes that happens to collide with one of
# these names (e.g. a chip on the board literally labelled `USB3`) will
# resolve through `is_valid_refdes` BEFORE this blocklist is consulted —
# so the blocklist never masks a real, validated refdes; it only avoids
# wrapping unknown-but-clearly-not-a-refdes tokens.
#
# Ambiguities (documented decisions):
# - `DP1`..`DP9`: could be a DisplayPort lane name OR a physical refdes
#   prefix `DP` (rare — diodes are usually `D`, but some legacy boardviews
#   use `DP` for protection diodes). Default: blocklist wins. Rationale:
#   the false-positive cost on UI confidence is higher than the rare
#   miss on a legitimate `DP1` diode, and a real `DP1` on the board
#   resolves through `is_valid_refdes` first anyway.
# - `SD0`..`SD9`: could be SD-card slot signal naming OR a Schottky
#   diode prefix. Same reasoning as `DP` — blocklist wins, real refdes
#   resolves earlier.
# - `GPIO0`..`GPIO9`: GPIO bank/pin labels. Wide net but no known
#   refdes prefix collision (`GPIO` is 4 letters, exceeds [A-Z]{1,3}
#   when the digit is appended — except `GPI`+digit is plausible).
#   Listed for completeness; the regex caps the prefix at 3 letters
#   so only `GPI0`..`GPI9` would actually match — kept here as a
#   marker that GPIO-style naming is intentionally excluded.
# Note on regex coverage: the active regex `[A-Z]{1,3}\d{1,4}` constrains
# the alpha prefix to 1–3 letters AND requires a continuous run of digits
# at the end. So `I2C1`, `I2S0`, `UART2`, `PCIE4`, `LPDDR4`, `JTAG0`,
# `HDMI2`, `SDIO0`, `EMMC0`, `LVDS0`, `MIPI0`, `HDCP1` etc. do NOT in fact
# match the regex (mixed alphanumerics or 4+ letter prefix break it).
# We still list them in the blocklist defensively — if the regex ever
# loosens to catch them, the blocklist already covers the false-positive
# cost. Listing is cheap; missing a future false-positive is expensive.
PROTOCOL_BLOCKLIST: frozenset[str] = frozenset(
    {
        # USB family
        "USB1", "USB2", "USB3", "USB4",
        "USB30", "USB31", "USB40",
        # UART (4-letter prefix — defensive)
        "UART0", "UART1", "UART2", "UART3", "UART4",
        "UART5", "UART6", "UART7", "UART8", "UART9",
        # I2C / I2S (mixed alnum — defensive)
        "I2C0", "I2C1", "I2C2", "I2C3", "I2C4",
        "I2C5", "I2C6", "I2C7", "I2C8", "I2C9",
        "I2S0", "I2S1", "I2S2", "I2S3", "I2S4",
        "I2S5", "I2S6", "I2S7", "I2S8", "I2S9",
        # SPI / QSPI (SPI matches; QSPI is 4-letter prefix — defensive)
        "SPI0", "SPI1", "SPI2", "SPI3", "SPI4",
        "SPI5", "SPI6", "SPI7", "SPI8", "SPI9",
        "QSPI0", "QSPI1", "QSPI2", "QSPI3", "QSPI4",
        "QSPI5", "QSPI6", "QSPI7", "QSPI8", "QSPI9",
        # PCIe lanes (PCI matches; PCIE 4-letter prefix — defensive)
        "PCI0", "PCI1", "PCI2", "PCI3", "PCI4",
        "PCI5", "PCI6", "PCI7", "PCI8", "PCI9",
        "PCIE0", "PCIE1", "PCIE2", "PCIE3", "PCIE4",
        "PCIE5", "PCIE6", "PCIE7", "PCIE8", "PCIE9",
        "PCIE16", "PCIE32",
        # SATA (4-letter prefix; SAT matches)
        "SAT0", "SAT1", "SAT2", "SAT3", "SAT4",
        "SAT5", "SAT6", "SAT7", "SAT8", "SAT9",
        "SATA0", "SATA1", "SATA2", "SATA3", "SATA4",
        "SATA5", "SATA6", "SATA7", "SATA8", "SATA9",
        # DDR / LPDDR (DDR matches; LPDDR 5-letter prefix — defensive)
        "DDR0", "DDR1", "DDR2", "DDR3", "DDR4", "DDR5",
        "DDR6", "DDR7", "DDR8", "DDR9",
        "LPDDR2", "LPDDR3", "LPDDR4", "LPDDR5", "LPDDR4X",
        # HDMI (4-letter — defensive; HDM 3-letter form also covered)
        "HDM1", "HDM2", "HDM3",
        "HDMI0", "HDMI1", "HDMI2", "HDMI3", "HDMI4",
        "HDMI5", "HDMI6", "HDMI7", "HDMI8", "HDMI9",
        # DP (DisplayPort) — see ambiguity note above
        "DP1", "DP2", "DP3", "DP4",
        "DP5", "DP6", "DP7", "DP8", "DP9",
        # VGA
        "VGA0", "VGA1", "VGA2", "VGA3", "VGA4",
        "VGA5", "VGA6", "VGA7", "VGA8", "VGA9",
        # LVDS (4-letter prefix — defensive)
        "LVDS0", "LVDS1", "LVDS2", "LVDS3", "LVDS4",
        "LVDS5", "LVDS6", "LVDS7", "LVDS8", "LVDS9",
        # MIPI / CSI / DSI (MIPI 4-letter — defensive; CSI/DSI 3-letter)
        "MIPI0", "MIPI1", "MIPI2", "MIPI3", "MIPI4",
        "CSI0", "CSI1", "CSI2", "CSI3", "CSI4",
        "DSI0", "DSI1", "DSI2", "DSI3", "DSI4",
        # SDIO (4-letter — defensive) / eMMC (4-letter — defensive) / SD
        "SDIO0", "SDIO1", "SDIO2", "SDIO3", "SDIO4",
        "EMMC0", "EMMC1", "EMMC2", "EMMC3", "EMMC4",
        "SD0", "SD1", "SD2", "SD3", "SD4",
        "SD5", "SD6", "SD7", "SD8", "SD9",
        # JTAG (4-letter — defensive)
        "JTAG0", "JTAG1", "JTAG2", "JTAG3", "JTAG4",
        # CAN bus
        "CAN0", "CAN1", "CAN2", "CAN3", "CAN4",
        "CAN5", "CAN6", "CAN7", "CAN8", "CAN9",
        # GPIO — 3-letter GPI*+digit matches; full GPIO 4-letter — defensive
        "GPI0", "GPI1", "GPI2", "GPI3", "GPI4",
        "GPI5", "GPI6", "GPI7", "GPI8", "GPI9",
        "GPIO0", "GPIO1", "GPIO2", "GPIO3", "GPIO4",
        "GPIO5", "GPIO6", "GPIO7", "GPIO8", "GPIO9",
        # HDCP (4-letter — defensive)
        "HDCP1", "HDCP2",
    }
)


def sanitize_agent_text(text: str, board: Board | None) -> tuple[str, list[str]]:
    """Return (clean_text, unknown_refdes_list).

    If board is None, no ground truth exists — returns text unchanged.
    """
    if board is None:
        return text, []

    unknown: list[str] = []

    def _wrap(match: re.Match[str]) -> str:
        token = match.group(0)
        if is_valid_refdes(board, token):
            return token
        if token in PROTOCOL_BLOCKLIST:
            return token
        unknown.append(token)
        return f"⟨?{token}⟩"

    return REFDES_RE.sub(_wrap, text), unknown
