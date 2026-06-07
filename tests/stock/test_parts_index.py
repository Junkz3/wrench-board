from api.stock.parts_index import _canonicalize_package, _canonicalize_value, build_parts_index


def test_canonicalize_value_caps():
    assert _canonicalize_value({"raw": "100nF", "primary": None}) == "0.1uF"
    assert _canonicalize_value({"raw": "0.1uF", "primary": None}) == "0.1uF"
    assert _canonicalize_value({"raw": "1uF", "primary": "1µF"}) == "1uF"
    assert _canonicalize_value({"raw": "10pF", "primary": None}) == "10pF"


def test_canonicalize_value_resistors():
    assert _canonicalize_value({"raw": "10k", "primary": None}) == "10k"
    assert _canonicalize_value({"raw": "10K0", "primary": None}) == "10k"
    assert _canonicalize_value({"raw": "0R1", "primary": None}) == "0.1"
    assert _canonicalize_value({"raw": "4.7k", "primary": None}) == "4.7k"


def test_canonicalize_package_strips_apple_variant_suffix():
    # Apple imperial passives with library-variant suffix collapse to base
    assert _canonicalize_package("0201-1") == "0201"
    assert _canonicalize_package("0402-0.1MM") == "0402"
    assert _canonicalize_package("01005-1") == "01005"
    # Already-canonical forms pass through
    assert _canonicalize_package("0201") == "0201"
    assert _canonicalize_package("0402") == "0402"
    # Non-imperial packages pass through (BGA, QFN, WLCSP, …)
    assert _canonicalize_package("BGA-NN") == "BGA-NN"
    assert _canonicalize_package("WLCSP") == "WLCSP"
    assert _canonicalize_package("QFN-64") == "QFN-64"
    assert _canonicalize_package("SOT-23") == "SOT-23"
    # None / empty
    assert _canonicalize_package(None) is None
    assert _canonicalize_package("") is None


def test_canonicalize_value_zero_ohm_collapse():
    # "0.00" (lever1 LP-md) and "0" (Opus baseline) must collapse identically
    # — zero-ohm jumpers are written either way across pipelines.
    assert _canonicalize_value({"raw": "0.00", "primary": None}) == "0"
    assert _canonicalize_value({"raw": "0", "primary": None}) == "0"
    assert _canonicalize_value({"raw": "0.00", "primary": None}) == _canonicalize_value({"raw": "0", "primary": None})
    # And other trailing-zero cases canonicalize cleanly
    assert _canonicalize_value({"raw": "47.0", "primary": None}) == "47"
    assert _canonicalize_value({"raw": "100", "primary": None}) == "100"


def test_canonicalize_value_unrecognized_passes_through():
    assert _canonicalize_value({"raw": "weird_value_xyz", "primary": None}) == "weird_value_xyz"


def test_canonicalize_value_resistors_with_ohm_unit():
    # "33-OHM" / "150OHM" / "10KOHM" forms emitted by lever1 LP-md path
    assert _canonicalize_value({"raw": "33-OHM", "primary": None}) == "33"
    assert _canonicalize_value({"raw": "10-OHM", "primary": None}) == "10"
    assert _canonicalize_value({"raw": "150OHM", "primary": None}) == "150"
    assert _canonicalize_value({"raw": "10KOHM", "primary": None}) == "10k"


def test_canonicalize_value_resistors_with_current_or_freq_annotation():
    # "33Ω @ 1500mA" / "10Ω @ 750mA" — ferrites with current rating
    assert _canonicalize_value({"raw": "33Ω @ 1500mA", "primary": None}) == "33"
    assert _canonicalize_value({"raw": "10Ω @ 750mA", "primary": None}) == "10"
    # No-@ form: trailing space-separated current
    assert _canonicalize_value({"raw": "10Ω 750mA", "primary": None}) == "10"
    # Frequency annotation for ferrite beads
    assert _canonicalize_value({"raw": "150Ω@100MHz", "primary": None}) == "150"


def test_canonicalize_value_hyphenated_multi_spec():
    # Apple-style verbose resistor: "240-OHM-25%-0.20A-0.9DCR" → "240"
    assert (
        _canonicalize_value({"raw": "240-OHM-25%-0.20A-0.9DCR", "primary": None})
        == "240"
    )


def test_canonicalize_value_inductors():
    assert _canonicalize_value({"raw": "1uH", "primary": None}) == "1uH"
    assert _canonicalize_value({"raw": "0.47uH", "primary": None}) == "0.47uH"
    assert _canonicalize_value({"raw": "0.47µH", "primary": None}) == "0.47uH"
    assert _canonicalize_value({"raw": "0.47UH", "primary": None}) == "0.47uH"
    assert _canonicalize_value({"raw": "15nH", "primary": None}) == "15nH"
    assert _canonicalize_value({"raw": "15NH", "primary": None}) == "15nH"
    assert _canonicalize_value({"raw": "2.2uH", "primary": None}) == "2.2uH"
    assert _canonicalize_value({"raw": "1.5mH", "primary": None}) == "1.5mH"


def test_canonicalize_value_crystals():
    assert _canonicalize_value({"raw": "24MHz", "primary": None}) == "24MHz"
    assert _canonicalize_value({"raw": "24.000MHz", "primary": None}) == "24MHz"
    assert _canonicalize_value({"raw": "32.768kHz", "primary": None}) == "32.768kHz"
    assert _canonicalize_value({"raw": "1GHz", "primary": None}) == "1GHz"


def test_canonicalize_value_voltages():
    # Zeners, TVS — bare voltage value
    assert _canonicalize_value({"raw": "5.5V", "primary": None}) == "5.5V"
    assert _canonicalize_value({"raw": "5.5V Zener", "primary": None}) == "5.5V"
    assert _canonicalize_value({"raw": "12V Zener", "primary": None}) == "12V"
    assert _canonicalize_value({"raw": "5V", "primary": None}) == "5V"


def test_canonicalize_value_mpn_strips_package_suffix():
    # Diode/IC MPN with trailing package: "BZT52C20LP, DFN10062" → "BZT52C20LP"
    assert (
        _canonicalize_value({"raw": "BZT52C20LP, DFN10062", "primary": None})
        == "BZT52C20LP"
    )
    # Bare MPN passes through
    assert _canonicalize_value({"raw": "BZT52C20LP", "primary": None}) == "BZT52C20LP"
    # Both forms reduce to identical canonical (the goal)
    assert _canonicalize_value(
        {"raw": "BZT52C20LP, DFN10062", "primary": None}
    ) == _canonicalize_value({"raw": "BZT52C20LP", "primary": None})


def test_canonicalize_value_mpn_strips_sheet_reference():
    # Apple-style sheet reference: "D2462 WLCSP SYM 4 OF 4" → "D2462"
    assert (
        _canonicalize_value({"raw": "D2462 WLCSP SYM 4 OF 4", "primary": None})
        == "D2462"
    )
    assert _canonicalize_value({"raw": "D2462", "primary": None}) == "D2462"


def test_canonicalize_value_lever1_baseline_equivalence():
    # End-to-end: shapes seen in real lever1 vs Opus baseline divergence
    # must collapse to identical canonical strings.
    pairs = [
        # ferrites
        ("33-OHM", "33Ω @ 1500mA"),
        ("10-OHM", "10Ω @ 750mA"),
        ("10-OHM", "10Ω 750mA"),
        ("150OHM", "150Ω@100MHz"),
        # diodes
        ("BZT52C20LP, DFN10062", "BZT52C20LP"),
        # zeners
        ("5.5V", "5.5V Zener"),
        # inductors with unicode µ
        ("0.47uH", "0.47µH"),
    ]
    for lever1_raw, baseline_raw in pairs:
        l_canon = _canonicalize_value({"raw": lever1_raw, "primary": None})
        b_canon = _canonicalize_value({"raw": baseline_raw, "primary": None})
        assert l_canon == b_canon, (
            f"lever1 {lever1_raw!r} → {l_canon!r} but "
            f"baseline {baseline_raw!r} → {b_canon!r}"
        )


def test_canonicalize_value_none():
    assert _canonicalize_value(None) is None
    assert _canonicalize_value({"raw": None, "primary": None}) is None


def _make_electrical_graph(components):
    """Tiny helper: build a minimal-shaped dict matching ElectricalGraph
    JSON for build_parts_index input."""
    return {
        "schema_version": "1.0",
        "device_slug": "test-device",
        "components": components,
        "nets": {},
        "power_rails": [],
        "typed_edges": [],
        "boot_sequence": [],
        "designer_notes": [],
        "ambiguities": [],
        "quality": {},
        "hierarchy": [],
    }


def test_build_parts_index_capacitor_decoupling():
    eg = _make_electrical_graph(
        {
            "C42": {
                "refdes": "C42",
                "type": "capacitor",
                "kind": "passive_c",
                "role": None,
                "value": {
                    "raw": "0.1uF",
                    "primary": "0.1µF",
                    "package": "0402",
                    "mpn": None,
                    "tolerance": "±10%",
                    "voltage_rating": 25.0,
                    "temp_coef": None,
                    "polarity_marker": False,
                    "description": None,
                },
                "pages": [3],
                "pins": [],
                "populated": True,
            }
        }
    )
    passive_class = {"classifications": [{"refdes": "C42", "role": "decoupling"}]}

    idx = build_parts_index(
        slug="test-device",
        electrical_graph=eg,
        passive_classification=passive_class,
        nets_classified=None,
    )

    e = idx.entries["C42"]
    assert e.value_canonical == "0.1uF"
    assert e.role_in_design == "decoupling"
    assert e.safety_class == "tolerant_with_warning"
    assert e.criticality_in_design == "low"  # no boot_sequence → default low


def test_build_parts_index_ic_is_exact_only():
    eg = _make_electrical_graph(
        {
            "U7": {
                "refdes": "U7",
                "type": "ic",
                "kind": "ic",
                "role": None,
                "value": {
                    "raw": "MAX77818EWY",
                    "primary": None,
                    "package": "QFN-56",
                    "mpn": "MAX77818EWY",
                    "tolerance": None,
                    "voltage_rating": None,
                    "temp_coef": None,
                    "polarity_marker": False,
                    "description": None,
                },
                "pages": [4],
                "pins": [],
                "populated": True,
            }
        }
    )
    idx = build_parts_index(
        slug="test-device", electrical_graph=eg, passive_classification=None, nets_classified=None
    )
    e = idx.entries["U7"]
    assert e.role_in_design == "ic"
    assert e.safety_class == "exact_only"
    assert e.mpn == "MAX77818EWY"


def test_build_parts_index_unclassified_passive_is_exact_only():
    eg = _make_electrical_graph(
        {
            "C99": {
                "refdes": "C99",
                "type": "capacitor",
                "kind": "passive_c",
                "role": None,
                "value": {
                    "raw": "1uF",
                    "primary": None,
                    "package": "0402",
                    "mpn": None,
                    "tolerance": None,
                    "voltage_rating": None,
                    "temp_coef": None,
                    "polarity_marker": False,
                    "description": None,
                },
                "pages": [1],
                "pins": [],
                "populated": True,
            }
        }
    )
    idx = build_parts_index(
        slug="test-device",
        electrical_graph=eg,
        passive_classification={"classifications": []},
        nets_classified=None,
    )
    e = idx.entries["C99"]
    assert e.role_in_design is None
    assert e.safety_class == "exact_only"  # fail-safe


def test_build_parts_index_filter_cap_blocked():
    eg = _make_electrical_graph(
        {
            "C200": {
                "refdes": "C200",
                "type": "capacitor",
                "kind": "passive_c",
                "role": None,
                "value": {
                    "raw": "10uF",
                    "primary": None,
                    "package": "0805",
                    "mpn": None,
                    "tolerance": None,
                    "voltage_rating": 16.0,
                    "temp_coef": None,
                    "polarity_marker": False,
                    "description": None,
                },
                "pages": [2],
                "pins": [],
                "populated": True,
            }
        }
    )
    passive_class = {"classifications": [{"refdes": "C200", "role": "filter"}]}
    idx = build_parts_index(
        slug="test-device",
        electrical_graph=eg,
        passive_classification=passive_class,
        nets_classified=None,
    )
    e = idx.entries["C200"]
    assert e.role_in_design == "filter"
    assert e.safety_class == "exact_only"


def test_build_parts_index_criticality_from_boot_sequence():
    eg = _make_electrical_graph(
        {
            "U1": {
                "refdes": "U1",
                "type": "ic",
                "kind": "ic",
                "role": None,
                "value": {
                    "raw": "PMIC",
                    "primary": None,
                    "package": "BGA",
                    "mpn": "PMIC123",
                    "tolerance": None,
                    "voltage_rating": None,
                    "temp_coef": None,
                    "polarity_marker": False,
                    "description": None,
                },
                "pages": [1],
                "pins": [],
                "populated": True,
            },
            "C500": {
                "refdes": "C500",
                "type": "capacitor",
                "kind": "passive_c",
                "role": None,
                "value": {
                    "raw": "0.1uF",
                    "primary": None,
                    "package": "0402",
                    "mpn": None,
                    "tolerance": None,
                    "voltage_rating": None,
                    "temp_coef": None,
                    "polarity_marker": False,
                    "description": None,
                },
                "pages": [9],
                "pins": [],
                "populated": True,
            },
        }
    )
    eg["boot_sequence"] = [
        {"phase": 1, "involved_refdes": ["U1"]},
        {"phase": 6, "involved_refdes": ["C500"]},
    ]
    idx = build_parts_index(
        slug="test-device", electrical_graph=eg, passive_classification=None, nets_classified=None
    )
    assert idx.entries["U1"].criticality_in_design == "high"  # phase ≤ 2
    assert idx.entries["C500"].criticality_in_design == "low"  # phase > 5


def test_build_parts_index_hash_is_deterministic():
    eg = _make_electrical_graph(
        {
            "C1": {
                "refdes": "C1",
                "type": "capacitor",
                "kind": "passive_c",
                "role": None,
                "value": None,
                "pages": [1],
                "pins": [],
                "populated": True,
            }
        }
    )
    idx1 = build_parts_index(
        slug="t", electrical_graph=eg, passive_classification=None, nets_classified=None
    )
    idx2 = build_parts_index(
        slug="t", electrical_graph=eg, passive_classification=None, nets_classified=None
    )
    assert idx1.source_electrical_graph_hash == idx2.source_electrical_graph_hash
    assert len(idx1.source_electrical_graph_hash) == 64  # sha256 hex
