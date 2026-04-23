# SPDX-License-Identifier: Apache-2.0
"""Closed catalogues of tools and skills + status/level thresholds.

Adding a skill or tool is a code change + schema_version bump — no runtime
declaration. Every skill's `requires` references must resolve to a ToolId.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


# ---------------------------------------------------------------------------
# Thresholds — see docs/superpowers/specs/2026-04-23-technician-profile-design.md §2.3
# ---------------------------------------------------------------------------

LEARNING_THRESHOLD = 1     # usages >= 1  and < PRACTICED_THRESHOLD -> "learning"
PRACTICED_THRESHOLD = 3    # usages >= 3  and < MASTERY_THRESHOLD   -> "practiced"
MASTERY_THRESHOLD = 10     # usages >= 10                           -> "mastered"

MASTERED_LEVEL_INTERMEDIATE = 1   # 1..=2  mastered skills -> intermediate
MASTERED_LEVEL_CONFIRMED = 3      # 3..=7  mastered skills -> confirmed
MASTERED_LEVEL_EXPERT = 8         # 8+     mastered skills -> expert

SKILL_EVIDENCES_CAP = 20


# ---------------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------------

class ToolId(StrEnum):
    SOLDERING_IRON = "soldering_iron"
    HOT_AIR = "hot_air"
    MICROSCOPE = "microscope"
    OSCILLOSCOPE = "oscilloscope"
    MULTIMETER = "multimeter"
    BGA_REWORK = "bga_rework"
    PREHEATER = "preheater"
    BENCH_PSU = "bench_psu"
    THERMAL_CAMERA = "thermal_camera"
    REBALLING_KIT = "reballing_kit"
    UV_LAMP = "uv_lamp"
    STENCIL_PRINTER = "stencil_printer"


@dataclass(frozen=True)
class ToolEntry:
    id: str
    label: str
    group: str  # "soldering" | "rework" | "inspection" | "measurement" | "power" | "supplies"


TOOLS_CATALOG: tuple[ToolEntry, ...] = (
    ToolEntry(ToolId.SOLDERING_IRON, "Fer à souder",        "soldering"),
    ToolEntry(ToolId.HOT_AIR,        "Hot air",              "rework"),
    ToolEntry(ToolId.BGA_REWORK,     "BGA rework",           "rework"),
    ToolEntry(ToolId.PREHEATER,      "Preheater",            "rework"),
    ToolEntry(ToolId.MICROSCOPE,     "Microscope",           "inspection"),
    ToolEntry(ToolId.THERMAL_CAMERA, "Caméra thermique",     "inspection"),
    ToolEntry(ToolId.UV_LAMP,        "Lampe UV",             "inspection"),
    ToolEntry(ToolId.MULTIMETER,     "Multimètre",           "measurement"),
    ToolEntry(ToolId.OSCILLOSCOPE,   "Oscilloscope",         "measurement"),
    ToolEntry(ToolId.BENCH_PSU,      "Alimentation de labo", "power"),
    ToolEntry(ToolId.REBALLING_KIT,  "Kit de reballing",     "supplies"),
    ToolEntry(ToolId.STENCIL_PRINTER,"Stencil / pochoir",    "supplies"),
)


# ---------------------------------------------------------------------------
# Skill catalogue
# ---------------------------------------------------------------------------

class SkillId(StrEnum):
    REFLOW_BGA = "reflow_bga"
    REBALLING = "reballing"
    JUMPER_WIRE = "jumper_wire"
    MICROSOLDER_0201 = "microsolder_0201"
    POP_REWORK = "pop_rework"
    TRACE_REPAIR = "trace_repair"
    STENCIL_APPLICATION = "stencil_application"
    SHORT_ISOLATION = "short_isolation"
    VOLTAGE_PROBING = "voltage_probing"
    SIGNAL_PROBING = "signal_probing"
    THERMAL_IMAGING = "thermal_imaging"
    POWER_SEQUENCING = "power_sequencing"
    FLUX_CLEANING = "flux_cleaning"
    COLD_JOINT_REWORK = "cold_joint_rework"
    CONNECTOR_REPLACEMENT = "connector_replacement"


@dataclass(frozen=True)
class SkillEntry:
    id: str
    label: str
    requires: tuple[str, ...]  # tuple of ToolId values


SKILLS_CATALOG: tuple[SkillEntry, ...] = (
    SkillEntry(SkillId.REFLOW_BGA,            "Reflow BGA",                  (ToolId.HOT_AIR,)),
    SkillEntry(SkillId.REBALLING,             "Reballing",                   (ToolId.BGA_REWORK, ToolId.REBALLING_KIT)),
    SkillEntry(SkillId.JUMPER_WIRE,           "Jumper wires",                (ToolId.SOLDERING_IRON, ToolId.MICROSCOPE)),
    SkillEntry(SkillId.MICROSOLDER_0201,      "Microsoudure 0201",           (ToolId.SOLDERING_IRON, ToolId.MICROSCOPE)),
    SkillEntry(SkillId.POP_REWORK,            "Rework PoP",                  (ToolId.HOT_AIR, ToolId.PREHEATER)),
    SkillEntry(SkillId.TRACE_REPAIR,          "Réparation pistes gravées",   (ToolId.SOLDERING_IRON, ToolId.MICROSCOPE)),
    SkillEntry(SkillId.STENCIL_APPLICATION,   "Pose de stencil",             (ToolId.STENCIL_PRINTER, ToolId.PREHEATER)),
    SkillEntry(SkillId.SHORT_ISOLATION,       "Isolation court-circuit",     (ToolId.MULTIMETER,)),
    SkillEntry(SkillId.VOLTAGE_PROBING,       "Mesure tensions de rails",    (ToolId.MULTIMETER,)),
    SkillEntry(SkillId.SIGNAL_PROBING,        "Mesure signaux scope",        (ToolId.OSCILLOSCOPE,)),
    SkillEntry(SkillId.THERMAL_IMAGING,       "Imagerie thermique diag",     (ToolId.THERMAL_CAMERA,)),
    SkillEntry(SkillId.POWER_SEQUENCING,      "Analyse power sequencing",    (ToolId.OSCILLOSCOPE,)),
    SkillEntry(SkillId.FLUX_CLEANING,         "Nettoyage flux / résidus",    ()),
    SkillEntry(SkillId.COLD_JOINT_REWORK,     "Rework soudure froide",       (ToolId.SOLDERING_IRON,)),
    SkillEntry(SkillId.CONNECTOR_REPLACEMENT, "Remplacement connecteur",     (ToolId.HOT_AIR, ToolId.MICROSCOPE)),
)


# Stable specialty ids, free to expand (no `requires` relation).
SPECIALTIES: tuple[tuple[str, str], ...] = (
    ("apple",       "Apple"),
    ("android",     "Android"),
    ("consoles",    "Consoles"),
    ("laptops",     "Laptops"),
    ("industriel",  "Industriel"),
    ("vintage",     "Vintage"),
)
