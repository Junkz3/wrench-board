import pytest

from api.stock.safety import (
    ROLE_TO_SAFETY,
    classify_safety,
)


@pytest.mark.parametrize("role,expected", [
    # Resistors — exact
    ("feedback", "exact_only"),
    ("current_sense", "exact_only"),
    ("damping", "exact_only"),
    # Resistors — tolerant
    ("series", "tolerant_with_warning"),
    ("pull_up", "tolerant_with_warning"),
    ("pull_down", "tolerant_with_warning"),
    # Caps — exact
    ("tank", "exact_only"),
    ("ac_coupling", "exact_only"),
    # Caps — tolerant
    ("decoupling", "tolerant_with_warning"),
    ("bulk", "tolerant_with_warning"),
    ("bypass", "tolerant_with_warning"),
    # Diodes — all exact
    ("flyback", "exact_only"),
    ("rectifier", "exact_only"),
    ("esd", "exact_only"),
    ("reverse_protection", "exact_only"),
    ("signal_clamp", "exact_only"),
    # Transistors — all exact
    ("load_switch", "exact_only"),
    ("level_shifter", "exact_only"),
    ("inrush_limiter", "exact_only"),
    ("flyback_switch", "exact_only"),
    # Non-passives
    ("ic", "exact_only"),
    ("connector", "exact_only"),
])
def test_classify_safety_table(role, expected):
    # type doesn't matter for these — the role alone determines safety_class
    assert classify_safety(role=role, type="resistor") == expected


def test_classify_filter_cap_is_exact_only():
    # "filter" + capacitor → exact_only (post-regulator LC filter knee)
    assert classify_safety(role="filter", type="capacitor") == "exact_only"


def test_classify_filter_ferrite_is_tolerant():
    # "filter" + ferrite → tolerant_with_warning (impedance close enough)
    # NOTE: ComponentType uses "ferrite" (not "ferrite_bead") —
    # see api/pipeline/schematic/schemas.py L32.
    assert classify_safety(role="filter", type="ferrite") == "tolerant_with_warning"


def test_classify_unknown_role_is_exact_only_failsafe():
    assert classify_safety(role=None, type="resistor") == "exact_only"
    assert classify_safety(role="some_role_we_never_heard_of", type="capacitor") == "exact_only"


def test_role_to_safety_table_complete():
    # Sanity: the table covers all roles emitted by passive_classifier.py
    expected_roles = {
        # passive_r
        "series", "feedback", "pull_up", "pull_down", "current_sense", "damping",
        # passive_c
        "decoupling", "bulk", "filter", "ac_coupling", "tank", "bypass",
        # passive_d
        "flyback", "rectifier", "esd", "reverse_protection", "signal_clamp",
        # passive_fb (disambiguated via type, but the literal is "filter")
        # passive_q
        "load_switch", "level_shifter", "inrush_limiter", "flyback_switch",
        # non-passive
        "ic", "connector",
    }
    assert expected_roles.issubset(set(ROLE_TO_SAFETY.keys()))
