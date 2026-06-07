"""Safety classification for stock substitution.

See docs/superpowers/specs/2026-05-08-stock-inventory-design.md §7.
The fail-safe rule: any unknown or unclassified role → "exact_only".
"""

from __future__ import annotations

from typing import Literal

SafetyClass = Literal["exact_only", "tolerant_with_warning", "blocked"]

ROLE_TO_SAFETY: dict[str, SafetyClass] = {
    # Resistors (passive_r)
    "feedback":           "exact_only",
    "current_sense":      "exact_only",
    "damping":            "exact_only",
    "series":             "tolerant_with_warning",
    "pull_up":            "tolerant_with_warning",
    "pull_down":          "tolerant_with_warning",
    # Capacitors (passive_c)
    "tank":               "exact_only",
    "ac_coupling":        "exact_only",
    "decoupling":         "tolerant_with_warning",
    "bulk":               "tolerant_with_warning",
    "bypass":             "tolerant_with_warning",
    # "filter" is listed here so ROLE_TO_SAFETY covers all passive_classifier
    # roles (see test_role_to_safety_table_complete).  classify_safety()
    # intercepts this role before the table lookup and dispatches by type,
    # so this sentinel value is never actually used at runtime.
    "filter":             "exact_only",
    # Diodes (passive_d) — all exact
    "flyback":            "exact_only",
    "rectifier":          "exact_only",
    "esd":                "exact_only",
    "reverse_protection": "exact_only",
    "signal_clamp":       "exact_only",
    # Transistors (passive_q) — all exact
    "load_switch":        "exact_only",
    "level_shifter":      "exact_only",
    "inrush_limiter":     "exact_only",
    "flyback_switch":     "exact_only",
    # Non-passive
    "ic":                 "exact_only",
    "connector":          "exact_only",
}


def classify_safety(role: str | None, type: str) -> SafetyClass:
    """Classify a component's safety_class given its functional role and type.

    Disambiguates "filter":
    - capacitor + filter → exact_only (post-regulator LC filter knee tuned)
    - ferrite + filter → tolerant_with_warning (impedance close enough)

    Unknown role or absent → "exact_only" (fail-safe).
    """
    if role is None:
        return "exact_only"
    if role == "filter":
        if type == "ferrite":
            return "tolerant_with_warning"
        return "exact_only"
    return ROLE_TO_SAFETY.get(role, "exact_only")
