"""Tests for the package-aware pad-shape inference.

The `.fz` format and a handful of legacy boardview formats ship no pad
shape per pin — every pad lands as a circle by default. Real boards
have rectangular pads on every chip-passive (0402/0603/0805/1206), every
leaded SMD (SOT/QFN/QFP/SOIC/SOP/TSSOP/DIP), and every SMD inductor.
The inference uses the footprint name (extracted from `Part.footprint`)
to pick `"rect"` for these conventions and `"circle"` for grid arrays
(BGA/CSP/LGA), mounting holes, and explicit test points.

Convention is universal in PCB fabrication — chip resistor pads are
always rectangular, BGA balls are always circular. Inference is
deterministic and reversible (caller decides whether to apply it).
"""

from __future__ import annotations

from api.board.parser._pad_shape_inference import infer_pad_shape


# ---------------------------------------------------------------------------
# Rect-shaped pads (chip passives + leaded packages)
# ---------------------------------------------------------------------------


def test_chip_passive_imperial_inferred_as_rect():
    """Imperial-coded chip resistors / capacitors / inductors / ferrites:
    `R0402`, `C0603`, `L0805`, `FB1206` etc. — universally rectangular."""
    assert infer_pad_shape("R0402") == "rect"
    assert infer_pad_shape("C0603") == "rect"
    assert infer_pad_shape("L0805") == "rect"
    assert infer_pad_shape("R1206") == "rect"
    assert infer_pad_shape("FB0402") == "rect"


def test_chip_passive_metric_inferred_as_rect():
    """Metric-coded chip parts: `RES1005`, `CAP1608`, `MLCC0603`,
    `IND2012`, `SMIND1005`. Common in Asian dump conventions."""
    assert infer_pad_shape("RES1005") == "rect"
    assert infer_pad_shape("CAP1608") == "rect"
    assert infer_pad_shape("MLCC0603") == "rect"
    assert infer_pad_shape("IND2012") == "rect"
    assert infer_pad_shape("SMIND1005") == "rect"


def test_chip_passive_with_height_suffix_inferred_as_rect():
    """Real-world footprints carry a height / variant suffix:
    `R0402_H16` (0402 component 1.6 mm tall), `CAP1005_0_55H`."""
    assert infer_pad_shape("R0402_H16") == "rect"
    assert infer_pad_shape("CAP1005_0_55H") == "rect"
    assert infer_pad_shape("RES1005_0_4H") == "rect"
    assert infer_pad_shape("CAP1608_2012_1_53H") == "rect"


def test_leaded_packages_inferred_as_rect():
    """SOT/SOIC/QFP/QFN/TSSOP and friends — leaded packages with
    rectangular SMD landing pads."""
    assert infer_pad_shape("SOT23_3PIN") == "rect"
    assert infer_pad_shape("SOIC8") == "rect"
    assert infer_pad_shape("QFP100") == "rect"
    assert infer_pad_shape("QFN32") == "rect"
    assert infer_pad_shape("TSSOP16") == "rect"
    assert infer_pad_shape("TSOP48") == "rect"
    assert infer_pad_shape("DPAK") == "rect"
    assert infer_pad_shape("DFN8") == "rect"


def test_leaded_substring_inferred_as_rect():
    """Real footprints embed the package name as a substring with a
    project-specific prefix: `MB_SOT669_COLAY`, `HH_SOT363_SIRENZA`,
    `MB_CPL_TSSLP`. Rect rule must match anywhere in the name."""
    assert infer_pad_shape("MB_SOT669_COLAY") == "rect"
    assert infer_pad_shape("HH_SOT363_SIRENZA") == "rect"
    assert infer_pad_shape("SMIND_TSSLP-2-1_0_40H") == "rect"


def test_inductor_keyword_inferred_as_rect():
    """`INDUCTOR_D_2P_421X283_LF3` — full-name SMD inductor."""
    assert infer_pad_shape("INDUCTOR_D_2P_421X283_LF3") == "rect"


# ---------------------------------------------------------------------------
# Circle-shaped pads (grid arrays, mounting, test points)
# ---------------------------------------------------------------------------


def test_grid_array_packages_inferred_as_circle():
    """BGA / CSP / LGA / WLCSP — grid-array balls or pads, circular."""
    assert infer_pad_shape("BGA080_12_5X15-170_1_2H") == "circle"
    assert infer_pad_shape("CSP49") == "circle"
    assert infer_pad_shape("LGA28") == "circle"
    assert infer_pad_shape("WLCSP25") == "circle"


def test_mounting_holes_inferred_as_circle():
    """Mechanical mounting holes — always circular through-holes."""
    assert infer_pad_shape("MTG3_175_8VIAS") == "circle"
    assert infer_pad_shape("MTGHOLE_M3") == "circle"


def test_explicit_test_points_inferred_as_circle():
    """Test points (TP050, TP100…) — single circular pad for probing."""
    assert infer_pad_shape("TP050") == "circle"
    assert infer_pad_shape("TP100") == "circle"


# ---------------------------------------------------------------------------
# Edge cases — rect wins over circle, unknown returns None
# ---------------------------------------------------------------------------


def test_rect_pattern_wins_over_circle_pattern():
    """Real dataset: `CAP1005_0_55H_BGA` is a chip capacitor with a
    `_BGA` *position tag* (placed under a BGA), NOT an actual BGA
    package. The rect rule (CAP1005) must override the circle rule
    (BGA suffix) — otherwise we collapse every chip-under-BGA back to
    a misleading circle."""
    assert infer_pad_shape("CAP1005_0_55H_BGA") == "rect"
    assert infer_pad_shape("RES1005_BGA") == "rect"
    assert infer_pad_shape("CAP1005_0_55H_BGA_B") == "rect"


def test_unknown_footprint_returns_none():
    """Footprint name that doesn't match any rule → no opinion. Caller
    keeps whatever shape was set upstream."""
    assert infer_pad_shape("CUSTOM_LOGO_PAD") is None
    assert infer_pad_shape("WHATEVER_OEM_TAG") is None
    assert infer_pad_shape("XYZQQQ") is None


def test_empty_or_none_footprint_returns_none():
    """Defensive — never crash on missing footprint strings."""
    assert infer_pad_shape(None) is None
    assert infer_pad_shape("") is None
    assert infer_pad_shape("   ") is None


# ---------------------------------------------------------------------------
# Real-world dataset patterns we initially missed
# ---------------------------------------------------------------------------


def test_bare_imperial_codes_inferred_as_rect():
    """Many `.fz` files store the footprint as the imperial chip code
    alone — `0402`, `0603`, `0805`, `1206`. No `R`/`C`/`L` prefix. These
    are still chip passives → rect. Top-10K instances on the dataset."""
    assert infer_pad_shape("0402") == "rect"
    assert infer_pad_shape("0603") == "rect"
    assert infer_pad_shape("0805") == "rect"
    assert infer_pad_shape("1206") == "rect"
    assert infer_pad_shape("2010") == "rect"


def test_motherboard_prefixed_chips_inferred_as_rect():
    """`MB_R0402`, `MB_C0603`, `MBS_R0402`, `MBS_C0402`, `MB_C0805` —
    same chip passives with a project-namespace prefix."""
    assert infer_pad_shape("MB_R0402") == "rect"
    assert infer_pad_shape("MB_C0603") == "rect"
    assert infer_pad_shape("MBS_R0402") == "rect"
    assert infer_pad_shape("MBS_C0402") == "rect"
    assert infer_pad_shape("MB_C0805") == "rect"


def test_inductor_short_prefix_and_smd_variants():
    """`INDUC_D_2P_421X283_H315_LF3`, `IND_SMD_0603`, `IND_SMD_0402`."""
    assert infer_pad_shape("INDUC_D_2P_421X283_H315_LF3") == "rect"
    assert infer_pad_shape("IND_SMD_0603") == "rect"
    assert infer_pad_shape("IND_SMD_0402") == "rect"


def test_smd_capacitor_variants_inferred_as_rect():
    """`CAP_SMD_7343` (tantalum/poly), `MB_CPL_820U_3V_6D3X9_LFH`,
    `MB_CE_820U_3V_6D3X8_LFH`, `CAP_E_6M3X9M0_P2M5` — SMD electrolytic
    or polymer capacitors. Modern packages have rectangular pads."""
    assert infer_pad_shape("CAP_SMD_7343") == "rect"
    assert infer_pad_shape("MB_CPL_820U_3V_6D3X9_LFH") == "rect"
    assert infer_pad_shape("MB_CE_820U_3V_6D3X8_LFH") == "rect"
    assert infer_pad_shape("CAP_E_6M3X9M0_P2M5") == "rect"


def test_son_and_powerpak_packages_inferred_as_rect():
    """SON (Small Outline No-leads) and POWERPAK variants are leadless
    SMD packages with rectangular bottom pads — same family as QFN."""
    assert infer_pad_shape("MB_SON5_8P_197X236_PAD_12V") == "rect"
    assert infer_pad_shape("SO8_POWERPAK_1_1H") == "rect"


def test_led_two_pin_smd_inferred_as_rect():
    """`LED_2P_63X31`, `LED_2P_63X31_H16` — 2-pin SMD LEDs."""
    assert infer_pad_shape("LED_2P_63X31") == "rect"
    assert infer_pad_shape("LED_2P_63X31_H16") == "rect"


def test_diode_clamp_slp_inferred_as_rect():
    """`DIO_CLAMP_SLP2510P8` — SLP (similar to SOT) leaded diode."""
    assert infer_pad_shape("DIO_CLAMP_SLP2510P8") == "rect"


def test_bga_with_underscore_inferred_as_circle():
    """`BGA_0170_P080_140X120` — BGA with underscore right after the
    `BGA` keyword. Earlier regex required `BGA\\d` and missed it."""
    assert infer_pad_shape("BGA_0170_P080_140X120") == "circle"


def test_hole_prefix_inferred_as_circle():
    """`HOLE_C118D118N` — explicitly named mounting hole / via."""
    assert infer_pad_shape("HOLE_C118D118N") == "circle"


def test_sc70_to252_packages_inferred_as_rect():
    """SC-70 (5/6-pin SOT-23 variant) and TO-252 / TO-220 packages —
    leaded SMD with rectangular pads."""
    assert infer_pad_shape("SC70_5") == "rect"
    assert infer_pad_shape("SC70_6") == "rect"
    assert infer_pad_shape("TO252_H94") == "rect"
    assert infer_pad_shape("TO220") == "rect"


def test_choke_and_pulse_inductors_inferred_as_rect():
    """`CHOKE_4P_79X47_TDK`, `IND_PULSE_PA2080NL` — SMD inductors / chokes."""
    assert infer_pad_shape("CHOKE_4P_79X47_TDK") == "rect"
    assert infer_pad_shape("IND_PULSE_PA2080NL") == "rect"


def test_sod_diode_packages_inferred_as_rect():
    """SOD (Small Outline Diode) and DIOSOD prefixes."""
    assert infer_pad_shape("DIOSOD_523") == "rect"
    assert infer_pad_shape("SOD123") == "rect"
    assert infer_pad_shape("SOD323") == "rect"


def test_sma_dpak_diode_inferred_as_rect():
    """`SMA` is the JEDEC DO-214AC SMD diode package — rectangular pads."""
    assert infer_pad_shape("SMA") == "rect"


def test_fuse_inferred_as_rect():
    """`NB_FUSE_2P_240X100_H106` — SMD fuse 2-pin → rect."""
    assert infer_pad_shape("NB_FUSE_2P_240X100_H106") == "rect"


def test_short_and_net_shortpin_inferred_as_rect():
    """`SHORTPIN`, `SHORTPIN_R0402`, `NET_SHORT`, `NET_SHORT_0_50` —
    bridging / shorting pads on a board → rectangular SMD."""
    assert infer_pad_shape("SHORTPIN") == "rect"
    assert infer_pad_shape("SHORTPIN_R0402") == "rect"
    assert infer_pad_shape("NET_SHORT") == "rect"
    assert infer_pad_shape("NET_SHORT_0_50") == "rect"


def test_edge_connector_inferred_as_rect():
    """`EDGECON_CROSSFIRE_40_SC` — board edge connector → rect."""
    assert infer_pad_shape("EDGECON_CROSSFIRE_40_SC") == "rect"


def test_test_point_with_suffix_inferred_as_circle():
    """`TP055PRI`, `TP050X025`, `TP0_127SQ_PRI` — test points with
    descriptive suffixes. Earlier regex required `^TP\\d{2,4}$` (digits
    only); relaxed to accept any TP-prefixed footprint."""
    assert infer_pad_shape("TP055PRI") == "circle"
    assert infer_pad_shape("TP050X025") == "circle"
    assert infer_pad_shape("TP0_127SQ_PRI") == "circle"


def test_short_substring_anywhere_inferred_as_rect():
    """`NB_PWR_FB_SHORT_PT` — short pad with project prefix. Match
    `_SHORT` anywhere (top unmatched class on the dataset, 510
    instances)."""
    assert infer_pad_shape("NB_PWR_FB_SHORT_PT") == "rect"


def test_open_pad_substring_inferred_as_rect():
    """`NB_3MM_OPEN_5MIL_LF`, `NBS_3MM_OPEN_5MIL_002` — open pads
    (exposed test/bridging pads, no chip on top)."""
    assert infer_pad_shape("NB_3MM_OPEN_5MIL_LF") == "rect"
    assert infer_pad_shape("NBS_3MM_OPEN_5MIL_002") == "rect"


def test_resistor_network_inferred_as_rect():
    """`RN1`, `RN2`, `RN3` — resistor network arrays, chip-passive
    style with multiple rectangular pads."""
    assert infer_pad_shape("RN1") == "rect"
    assert infer_pad_shape("RN5") == "rect"


def test_header_and_pinrex_inferred_as_rect():
    """`HD_1X1P_PINREX_LF3` — board header / connector strip → rect."""
    assert infer_pad_shape("HD_1X1P_PINREX_LF3") == "rect"


def test_connector_refdes_prefix_inferred_as_rect():
    """`CN1`, `CN12`, `J5`, `J100` — generic connector designators
    when the source format ships no real footprint name. PCI-Express
    edge connectors / DP / HDMI / USB connectors all use rectangular
    landing pads."""
    assert infer_pad_shape("CN1") == "rect"
    assert infer_pad_shape("CN12") == "rect"
    assert infer_pad_shape("J1") == "rect"
    assert infer_pad_shape("J100") == "rect"


def test_io_connector_keywords_in_bom_inferred_as_rect():
    """`HDMI CON 19P`, `DP_SCREW 20P`, `USB 3.0`, `VGA D-SUB` — every
    common IO connector keyword in the BOM description maps to rect."""
    assert infer_pad_shape("HDMI CON 19P,0.5mm,A TYPE,R/A") == "rect"
    assert infer_pad_shape("DP_SCREW 20P 3U 0.5MM R/A") == "rect"
    assert infer_pad_shape("USB 3.0 TYPE-A") == "rect"
    assert infer_pad_shape("DISPLAYPORT 20P") == "rect"
    assert infer_pad_shape("CONNECTOR PCIE x16") == "rect"


def test_bare_tantalum_smd_codes_inferred_as_rect():
    """Bare 4-digit SMD tantalum/poly codes that aren't imperial chip
    codes: `7343` (D), `7520`, `3528` (B), `6032` (C). These are
    rectangular pads even without an explicit `CAP_` prefix."""
    assert infer_pad_shape("7343") == "rect"
    assert infer_pad_shape("7520") == "rect"
    assert infer_pad_shape("3528") == "rect"
    assert infer_pad_shape("6032") == "rect"


# ---------------------------------------------------------------------------
# BOM-description form (Part.value strings — used as a fallback when the
# `.fz` SYM_NAME column duplicates the refdes instead of carrying the
# real footprint, as on Asus DUAL 1060 dumps).
# ---------------------------------------------------------------------------


def test_dashed_package_names_inferred_as_rect():
    """`SOT-23`, `SOT-353`, `TSOT-23-8`, `SC-70-5` — the BOM uses dashes
    inside package names where the footprint column drops them."""
    assert infer_pad_shape("N-MOSFET BSS138 SOT-23") == "rect"
    assert infer_pad_shape("N-MOSFET NX7002AK2 SOT-23") == "rect"
    assert infer_pad_shape("DOWN CONVERTER RT7296FGJ8F TSOT-23-8") == "rect"
    assert infer_pad_shape("DRIVER SC-70-5") == "rect"


def test_bom_keyword_mlcc_inferred_as_rect():
    """`MLCC 10UF/16V(0805) X6S` — the parenthesised chip code already
    matches, but `MLCC` itself is the ceramic-cap keyword and should
    classify standalone."""
    assert infer_pad_shape("MLCC 10UF/16V(0805) X6S 10%") == "rect"
    assert infer_pad_shape("MLCC 0.1UF/16V(0402)X7R 10%") == "rect"


def test_bom_keyword_mosfet_inferred_as_rect():
    """`N-MOSFET QM3054M6 PRPAK5X6` — leadless power MOSFET package."""
    assert infer_pad_shape("N-MOSFET QM3054M6 PRPAK5X6//UBIQ") == "rect"
    assert infer_pad_shape("P-MOSFET PMV56XP") == "rect"
    assert infer_pad_shape("MOSFET DRIVER IR3598") == "rect"


def test_bom_keyword_resistor_capacitor_ferrite():
    """`RES 100 OHM 1/16W (0402)` and `FERRITE BEAD SMD(0805)`."""
    assert infer_pad_shape("RES 100 OHM 1/16W (0402) 5%") == "rect"
    assert infer_pad_shape("FERRITE BEAD SMD(0805)70OHM/3A") == "rect"
    assert infer_pad_shape("POSCAP 330UF/2.5V(3528) 20%") == "rect"


def test_bom_keyword_dc_dc_converter_inferred_as_rect():
    """`DOWN CONVERTER RT7296FGJ8F TSOT-23-8`."""
    assert infer_pad_shape("DOWN CONVERTER RT7296FGJ8F") == "rect"
    assert infer_pad_shape("DC-DC CONVERTER") == "rect"
