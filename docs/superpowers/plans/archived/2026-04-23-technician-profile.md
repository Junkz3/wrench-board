# Technician Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `#profile` section as a living technician profile — manual identity / tools / preferences edits + automatic skill progression driven by the diagnostic agent via three `profile_*` tools, with the profile state injected into the agent's system prompt every session.

**Architecture:** New backend module `api/profile/` owns model + store + derivations + HTTP + prompt rendering + tool handlers. Existing `api/agent/` is extended to (a) always expose the three `profile_*` tools in the manifest, (b) inject a `<technician_profile>` block into the DIRECT runtime system prompt, (c) push a synthetic `[CONTEXTE TECHNICIEN]` user message at MANAGED runtime session open. Frontend replaces the stub with a dedicated `profile.js` / `profile.css` module consuming `GET /profile` (single-shot payload with profile + derived + catalog).

**Tech Stack:** Python 3.11, FastAPI ~0.136, Pydantic v2, pytest + pytest-asyncio, vanilla JS (no build), D3-less. Tokens existants seulement (`--cyan`, `--emerald`, `--violet`, `--amber`, `--text-*`, `--panel*`, `--border*`).

**Parallel agents note:** Another agent is working on `#schematic` in `web/`. Every `git commit` in this plan uses `-- <paths>` explicit form to avoid bundling parallel staged work (CLAUDE.md rule).

---

## File Structure

**Created (backend):**
- `api/profile/__init__.py` — re-exports `router`.
- `api/profile/catalog.py` — `ToolId`, `SkillId` string enums + `TOOLS_CATALOG` + `SKILLS_CATALOG` + threshold constants.
- `api/profile/model.py` — Pydantic models (`Identity`, `Preferences`, `ToolInventory`, `SkillEvidence`, `SkillRecord`, `TechnicianProfile`).
- `api/profile/derive.py` — `skill_status`, `global_level`, `effective_verbosity`, `skills_by_status`.
- `api/profile/store.py` — `load_profile`, `save_profile`, `update_profile`, `bump_skill`.
- `api/profile/prompt.py` — `render_technician_block`.
- `api/profile/router.py` — `GET /profile`, `PUT /profile/{identity,tools,preferences}`.
- `api/profile/tools.py` — `profile_get`, `profile_check_skills`, `profile_track_skill` handlers.

**Modified (backend):**
- `api/main.py` — include profile router.
- `api/agent/manifest.py` — add `PROFILE_TOOLS` list + unconditional inclusion in `build_tools_manifest()`.
- `api/agent/runtime_direct.py` — dispatch `profile_*` in the tool loop + extend system prompt with technician block.
- `api/agent/runtime_managed.py` — push `[CONTEXTE TECHNICIEN]` user message once per WS connection.
- `.gitignore` — add `memory/_profile/`.

**Created (frontend):**
- `web/profil.html` — full HTML markup of the profile section (head + blocks + drawer + identity modal). Fetched as a partial at section init and injected into the mount point in `index.html`.
- `web/js/profile.js` — module.
- `web/styles/profile.css` — styles.

**Modified (frontend):**
- `web/index.html` — replace the stub with a minimal mount point `<section id="profileSection" class="profile hidden" data-partial="/profil.html"></section>`, add `<link>` + `<script type=module>`.
- `web/js/main.js` — import + dispatch `initProfileSection()` on `section === "profile"`.
- `web/js/router.js` — hide/show `#profileSection` in `navigate()`.

**Tests (created):**
- `tests/profile/__init__.py`
- `tests/profile/test_catalog.py`
- `tests/profile/test_model.py`
- `tests/profile/test_derive.py`
- `tests/profile/test_store.py`
- `tests/profile/test_prompt.py`
- `tests/profile/test_router.py`
- `tests/profile/test_tools.py`

**Modified (test):**
- `tests/agent/test_manifest_dynamic.py` — assert `profile_*` always included.

---

## Task 1 — Catalogue fermé (tools + skills + thresholds)

**Files:**
- Create: `api/profile/__init__.py`
- Create: `api/profile/catalog.py`
- Test: `tests/profile/__init__.py`
- Test: `tests/profile/test_catalog.py`

- [ ] **Step 1.1: Create empty `__init__.py`**

```python
# api/profile/__init__.py
# SPDX-License-Identifier: Apache-2.0
"""Technician profile sub-system."""
```

```python
# tests/profile/__init__.py  (empty)
```

- [ ] **Step 1.2: Write failing test for catalogue shape**

Create `tests/profile/test_catalog.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Catalogue integrity: ids unique, requires resolve, thresholds monotonic."""

from api.profile.catalog import (
    MASTERY_THRESHOLD,
    PRACTICED_THRESHOLD,
    LEARNING_THRESHOLD,
    MASTERED_LEVEL_EXPERT,
    MASTERED_LEVEL_CONFIRMED,
    MASTERED_LEVEL_INTERMEDIATE,
    SKILLS_CATALOG,
    SkillId,
    TOOLS_CATALOG,
    ToolId,
)


def test_tool_ids_unique_and_match_enum():
    ids = [t.id for t in TOOLS_CATALOG]
    assert len(ids) == len(set(ids)), "duplicate tool id"
    assert set(ids) == {m.value for m in ToolId}


def test_skill_ids_unique_and_match_enum():
    ids = [s.id for s in SKILLS_CATALOG]
    assert len(ids) == len(set(ids)), "duplicate skill id"
    assert set(ids) == {m.value for m in SkillId}


def test_every_skill_requires_resolves_to_known_tool():
    tool_ids = {t.id for t in TOOLS_CATALOG}
    for skill in SKILLS_CATALOG:
        for req in skill.requires:
            assert req in tool_ids, f"skill {skill.id} requires unknown tool {req}"


def test_skill_status_thresholds_monotonic():
    assert 0 < LEARNING_THRESHOLD < PRACTICED_THRESHOLD < MASTERY_THRESHOLD


def test_level_thresholds_monotonic():
    assert (
        MASTERED_LEVEL_INTERMEDIATE
        < MASTERED_LEVEL_CONFIRMED
        < MASTERED_LEVEL_EXPERT
    )
```

- [ ] **Step 1.3: Run tests (expect fail — module missing)**

Run: `.venv/bin/pytest tests/profile/test_catalog.py -v`
Expected: FAIL — `ModuleNotFoundError: api.profile.catalog`.

- [ ] **Step 1.4: Implement catalogue**

Create `api/profile/catalog.py`:

```python
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

LEARNING_THRESHOLD = 1     # usages ≥ 1  and < PRACTICED_THRESHOLD → "learning"
PRACTICED_THRESHOLD = 3    # usages ≥ 3  and < MASTERY_THRESHOLD   → "practiced"
MASTERY_THRESHOLD = 10     # usages ≥ 10                           → "mastered"

MASTERED_LEVEL_INTERMEDIATE = 1   # 1..=2  mastered skills → intermediate
MASTERED_LEVEL_CONFIRMED = 3      # 3..=7  mastered skills → confirmed
MASTERED_LEVEL_EXPERT = 8         # 8+     mastered skills → expert

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
```

- [ ] **Step 1.5: Run tests (expect pass)**

Run: `.venv/bin/pytest tests/profile/test_catalog.py -v`
Expected: all 5 tests pass.

- [ ] **Step 1.6: Commit**

```bash
git add -- api/profile/__init__.py api/profile/catalog.py \
           tests/profile/__init__.py tests/profile/test_catalog.py
git commit -m "$(cat <<'EOF'
feat(profile): closed catalogues of tools and skills

Static constants for 12 tools and 15 skills with their tool requirements,
plus status/level thresholds. StrEnum-keyed so skill/tool ids cross-reference
cleanly from Pydantic models and HTTP payloads.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/profile/__init__.py api/profile/catalog.py \
      tests/profile/__init__.py tests/profile/test_catalog.py
```

---

## Task 2 — Pydantic model

**Files:**
- Create: `api/profile/model.py`
- Test: `tests/profile/test_model.py`

- [ ] **Step 2.1: Write failing test**

```python
# SPDX-License-Identifier: Apache-2.0
"""TechnicianProfile model — round-trip, defaults, validation."""

import pytest
from pydantic import ValidationError

from api.profile.catalog import SkillId, ToolId
from api.profile.model import (
    Identity,
    Preferences,
    SkillEvidence,
    SkillRecord,
    TechnicianProfile,
)


def test_default_profile_valid_and_empty():
    p = TechnicianProfile.default()
    assert p.schema_version == 1
    assert p.identity.name == ""
    assert p.identity.level_override is None
    assert p.preferences.verbosity == "auto"
    assert p.preferences.language == "fr"
    # Every tool key present, all False.
    for tool in ToolId:
        assert getattr(p.tools, tool.value) is False
    assert p.skills == {}


def test_roundtrip_serialization():
    p = TechnicianProfile.default()
    p.identity.name = "Alexis"
    p.tools.soldering_iron = True
    p.skills[SkillId.REFLOW_BGA] = SkillRecord(
        usages=2,
        first_used="2026-04-01T10:00:00Z",
        last_used="2026-04-02T11:00:00Z",
        evidences=[
            SkillEvidence(
                repair_id="rep_1",
                device_slug="iphone-x",
                symptom="no_boot",
                action_summary="Reflow du PMIC U2 après court-circuit VDD_MAIN",
                date="2026-04-02T11:00:00Z",
            )
        ],
    )
    payload = p.model_dump(mode="json")
    restored = TechnicianProfile.model_validate(payload)
    assert restored == p


def test_identity_level_override_rejects_unknown_value():
    with pytest.raises(ValidationError):
        Identity(level_override="wizard")


def test_preferences_verbosity_rejects_unknown_value():
    with pytest.raises(ValidationError):
        Preferences(verbosity="verbose")
```

- [ ] **Step 2.2: Run tests (expect fail)**

Run: `.venv/bin/pytest tests/profile/test_model.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 2.3: Implement `api/profile/model.py`**

```python
# SPDX-License-Identifier: Apache-2.0
"""Pydantic v2 models for the technician profile.

Source of truth for both runtime validation and the JSON Schema surface
exposed to agent tools. Mirrors the on-disk shape described in
docs/superpowers/specs/2026-04-23-technician-profile-design.md §2.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from api.profile.catalog import SkillId, ToolId


LevelValue = Literal["beginner", "intermediate", "confirmed", "expert"]
VerbosityValue = Literal["auto", "concise", "normal", "teaching"]
LanguageValue = Literal["fr", "en"]


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    avatar: str = ""  # 1 emoji or up to 2 letters
    years_experience: int = Field(default=0, ge=0, le=80)
    specialties: list[str] = Field(default_factory=list)
    level_override: LevelValue | None = None


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verbosity: VerbosityValue = "auto"
    language: LanguageValue = "fr"


def _tool_inventory_fields() -> dict[str, tuple[type, bool]]:
    return {tool.value: (bool, False) for tool in ToolId}


class ToolInventory(BaseModel):
    """Bitmap of owned tools, one bool field per ToolId."""

    model_config = ConfigDict(extra="forbid")

    soldering_iron: bool = False
    hot_air: bool = False
    microscope: bool = False
    oscilloscope: bool = False
    multimeter: bool = False
    bga_rework: bool = False
    preheater: bool = False
    bench_psu: bool = False
    thermal_camera: bool = False
    reballing_kit: bool = False
    uv_lamp: bool = False
    stencil_printer: bool = False


class SkillEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repair_id: str
    device_slug: str
    symptom: str
    action_summary: str
    date: str  # ISO 8601


class SkillRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usages: int = Field(default=0, ge=0)
    first_used: str | None = None
    last_used: str | None = None
    evidences: list[SkillEvidence] = Field(default_factory=list)


class TechnicianProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    identity: Identity = Field(default_factory=Identity)
    preferences: Preferences = Field(default_factory=Preferences)
    tools: ToolInventory = Field(default_factory=ToolInventory)
    skills: dict[SkillId, SkillRecord] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @classmethod
    def default(cls) -> "TechnicianProfile":
        return cls()
```

- [ ] **Step 2.4: Run tests (expect pass)**

Run: `.venv/bin/pytest tests/profile/test_model.py -v`
Expected: 4 pass.

- [ ] **Step 2.5: Commit**

```bash
git add -- api/profile/model.py tests/profile/test_model.py
git commit -m "$(cat <<'EOF'
feat(profile): Pydantic model for TechnicianProfile

Nested models (Identity, Preferences, ToolInventory, SkillRecord,
SkillEvidence) with forbid-extra and constrained Literals so unknown
verbosity / level values fail at validation time rather than silently
drifting into the JSON file.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/profile/model.py tests/profile/test_model.py
```

---

## Task 3 — Derivations (pure functions)

**Files:**
- Create: `api/profile/derive.py`
- Test: `tests/profile/test_derive.py`

- [ ] **Step 3.1: Write failing test**

```python
# SPDX-License-Identifier: Apache-2.0
"""Pure derivation helpers: skill_status, global_level, effective_verbosity."""

import pytest

from api.profile.catalog import SkillId
from api.profile.derive import (
    effective_verbosity,
    global_level,
    skill_status,
    skills_by_status,
)
from api.profile.model import SkillRecord, TechnicianProfile


@pytest.mark.parametrize(
    "usages,expected",
    [
        (0, "unlearned"),
        (1, "learning"),
        (2, "learning"),
        (3, "practiced"),
        (9, "practiced"),
        (10, "mastered"),
        (99, "mastered"),
    ],
)
def test_skill_status_thresholds(usages, expected):
    assert skill_status(usages) == expected


def _profile_with_mastered(count: int) -> TechnicianProfile:
    p = TechnicianProfile.default()
    for i in range(count):
        p.skills[SkillId(list(SkillId)[i].value)] = SkillRecord(usages=10)
    return p


@pytest.mark.parametrize(
    "mastered_count,expected",
    [
        (0, "beginner"),
        (1, "intermediate"),
        (2, "intermediate"),
        (3, "confirmed"),
        (7, "confirmed"),
        (8, "expert"),
        (12, "expert"),
    ],
)
def test_global_level_without_override(mastered_count, expected):
    profile = _profile_with_mastered(mastered_count)
    assert global_level(profile) == expected


def test_global_level_respects_override():
    profile = _profile_with_mastered(0)
    profile.identity.level_override = "expert"
    assert global_level(profile) == "expert"


@pytest.mark.parametrize(
    "level,expected",
    [
        ("beginner", "teaching"),
        ("intermediate", "teaching"),
        ("confirmed", "normal"),
        ("expert", "concise"),
    ],
)
def test_effective_verbosity_auto_maps_from_level(level, expected):
    profile = TechnicianProfile.default()
    profile.preferences.verbosity = "auto"
    profile.identity.level_override = level
    assert effective_verbosity(profile) == expected


def test_effective_verbosity_explicit_wins_over_auto():
    profile = TechnicianProfile.default()
    profile.preferences.verbosity = "concise"
    profile.identity.level_override = "beginner"
    assert effective_verbosity(profile) == "concise"


def test_skills_by_status_buckets_all_catalog_entries():
    profile = TechnicianProfile.default()
    profile.skills[SkillId.REFLOW_BGA] = SkillRecord(usages=12)
    profile.skills[SkillId.REBALLING] = SkillRecord(usages=4)
    profile.skills[SkillId.JUMPER_WIRE] = SkillRecord(usages=1)
    buckets = skills_by_status(profile)
    assert "reflow_bga" in buckets["mastered"]
    assert "reballing" in buckets["practiced"]
    assert "jumper_wire" in buckets["learning"]
    # Any skill not present in profile.skills is unlearned.
    assert "pop_rework" in buckets["unlearned"]
    total = sum(len(v) for v in buckets.values())
    assert total == 15  # every catalogue skill accounted for
```

- [ ] **Step 3.2: Run tests (expect fail)**

Run: `.venv/bin/pytest tests/profile/test_derive.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3.3: Implement `api/profile/derive.py`**

```python
# SPDX-License-Identifier: Apache-2.0
"""Pure derivation helpers — no I/O, safe to call from prompt rendering / HTTP."""

from __future__ import annotations

from typing import Literal

from api.profile.catalog import (
    LEARNING_THRESHOLD,
    MASTERED_LEVEL_CONFIRMED,
    MASTERED_LEVEL_EXPERT,
    MASTERED_LEVEL_INTERMEDIATE,
    MASTERY_THRESHOLD,
    PRACTICED_THRESHOLD,
    SKILLS_CATALOG,
)
from api.profile.model import TechnicianProfile


SkillStatus = Literal["unlearned", "learning", "practiced", "mastered"]


def skill_status(usages: int) -> SkillStatus:
    if usages >= MASTERY_THRESHOLD:
        return "mastered"
    if usages >= PRACTICED_THRESHOLD:
        return "practiced"
    if usages >= LEARNING_THRESHOLD:
        return "learning"
    return "unlearned"


def global_level(profile: TechnicianProfile) -> str:
    if profile.identity.level_override is not None:
        return profile.identity.level_override
    mastered_count = sum(
        1 for rec in profile.skills.values() if skill_status(rec.usages) == "mastered"
    )
    if mastered_count >= MASTERED_LEVEL_EXPERT:
        return "expert"
    if mastered_count >= MASTERED_LEVEL_CONFIRMED:
        return "confirmed"
    if mastered_count >= MASTERED_LEVEL_INTERMEDIATE:
        return "intermediate"
    return "beginner"


_LEVEL_TO_VERBOSITY = {
    "beginner": "teaching",
    "intermediate": "teaching",
    "confirmed": "normal",
    "expert": "concise",
}


def effective_verbosity(profile: TechnicianProfile) -> str:
    declared = profile.preferences.verbosity
    if declared != "auto":
        return declared
    return _LEVEL_TO_VERBOSITY[global_level(profile)]


def skills_by_status(profile: TechnicianProfile) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {
        "mastered": [],
        "practiced": [],
        "learning": [],
        "unlearned": [],
    }
    for entry in SKILLS_CATALOG:
        rec = profile.skills.get(entry.id)
        usages = rec.usages if rec is not None else 0
        buckets[skill_status(usages)].append(entry.id)
    return buckets
```

- [ ] **Step 3.4: Run tests (expect pass)**

Run: `.venv/bin/pytest tests/profile/test_derive.py -v`
Expected: all tests pass.

- [ ] **Step 3.5: Commit**

```bash
git add -- api/profile/derive.py tests/profile/test_derive.py
git commit -m "$(cat <<'EOF'
feat(profile): pure derivations for status, level, verbosity

skill_status bucketises usages (0 / 1-2 / 3-9 / 10+), global_level counts
mastered skills (or respects identity.level_override), effective_verbosity
maps level→verbosity when preference is "auto". skills_by_status groups
every catalogue skill into a single dict for frontend rendering.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/profile/derive.py tests/profile/test_derive.py
```

---

## Task 4 — Store (disk I/O)

**Files:**
- Create: `api/profile/store.py`
- Test: `tests/profile/test_store.py`
- Modify: `.gitignore`

- [ ] **Step 4.1: Write failing test**

```python
# SPDX-License-Identifier: Apache-2.0
"""Profile store — disk I/O, atomicity, bump_skill."""

import json
from pathlib import Path

import pytest

from api.profile.catalog import SKILL_EVIDENCES_CAP, SkillId, ToolId
from api.profile.model import SkillEvidence, TechnicianProfile
from api.profile.store import bump_skill, load_profile, save_profile


@pytest.fixture
def tmp_memory_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    from api.config import get_settings
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_load_absent_file_returns_default(tmp_memory_root: Path):
    p = load_profile()
    assert p == TechnicianProfile.default()


def test_save_then_load_roundtrips(tmp_memory_root: Path):
    p = TechnicianProfile.default()
    p.identity.name = "Alexis"
    p.tools.soldering_iron = True
    save_profile(p)
    restored = load_profile()
    assert restored.identity.name == "Alexis"
    assert restored.tools.soldering_iron is True
    assert (tmp_memory_root / "_profile" / "technician.json").exists()


def test_save_is_atomic_no_tmp_residue(tmp_memory_root: Path):
    save_profile(TechnicianProfile.default())
    listing = {f.name for f in (tmp_memory_root / "_profile").iterdir()}
    assert listing == {"technician.json"}


def _evidence(i: int) -> SkillEvidence:
    return SkillEvidence(
        repair_id=f"rep_{i}",
        device_slug="iphone-x",
        symptom="no_boot",
        action_summary=f"Reflow #{i} du PMIC U2 après short VDD_MAIN 1V8",
        date=f"2026-04-{(i % 28) + 1:02d}T10:00:00Z",
    )


def test_bump_skill_creates_record_first_time(tmp_memory_root: Path):
    result = bump_skill(SkillId.REFLOW_BGA, _evidence(1))
    assert result.usages == 1
    assert result.first_used is not None
    assert result.first_used == result.last_used
    p = load_profile()
    assert SkillId.REFLOW_BGA in p.skills


def test_bump_skill_increments_and_appends(tmp_memory_root: Path):
    bump_skill(SkillId.REFLOW_BGA, _evidence(1))
    result = bump_skill(SkillId.REFLOW_BGA, _evidence(2))
    assert result.usages == 2
    assert len(result.evidences) == 2


def test_bump_skill_caps_evidences_fifo(tmp_memory_root: Path):
    for i in range(SKILL_EVIDENCES_CAP + 5):
        bump_skill(SkillId.REFLOW_BGA, _evidence(i))
    p = load_profile()
    rec = p.skills[SkillId.REFLOW_BGA]
    assert rec.usages == SKILL_EVIDENCES_CAP + 5
    assert len(rec.evidences) == SKILL_EVIDENCES_CAP
    # FIFO: oldest were dropped — the first evidences we should still see
    # is for i == 5 (we dropped i == 0..4).
    assert rec.evidences[0].repair_id == "rep_5"
```

- [ ] **Step 4.2: Check `api/config.py` exposes `MEMORY_ROOT`**

Run: `grep -n "memory_root" api/config.py`
Expected: a `memory_root` attribute (pydantic-settings reads `MEMORY_ROOT` env var). If the attribute is already there (see how `runtime_direct.py:240` uses it), proceed. If not, add it in config.py before the test fixture will work — step 4.2b below.

- [ ] **Step 4.2b (conditional): add `MEMORY_ROOT` to settings if missing**

If `memory_root` is not already in `api/config.py`:

```python
# add to Settings class:
memory_root: str = "memory"
```

Commit this micro-change separately before Task 4 proceeds:

```bash
git add -- api/config.py
git commit -m "chore(config): expose MEMORY_ROOT setting" -- api/config.py
```

- [ ] **Step 4.3: Run tests (expect fail)**

Run: `.venv/bin/pytest tests/profile/test_store.py -v`
Expected: FAIL — `api.profile.store` missing.

- [ ] **Step 4.4: Implement `api/profile/store.py`**

```python
# SPDX-License-Identifier: Apache-2.0
"""On-disk profile store.

Single file `memory/_profile/technician.json`. Writes are atomic via
tempfile + os.replace. Evidence history is FIFO-capped per skill.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from api.config import get_settings
from api.profile.catalog import SKILL_EVIDENCES_CAP, SkillId
from api.profile.model import SkillEvidence, SkillRecord, TechnicianProfile


_PROFILE_SUBDIR = "_profile"
_PROFILE_FILENAME = "technician.json"


def _profile_path() -> Path:
    root = Path(get_settings().memory_root)
    return root / _PROFILE_SUBDIR / _PROFILE_FILENAME


def load_profile() -> TechnicianProfile:
    path = _profile_path()
    if not path.exists():
        return TechnicianProfile.default()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return TechnicianProfile.model_validate(raw)
    except Exception:
        # Corrupt file → fall back to defaults rather than crashing the server.
        # The user can edit to recover; the corrupt file is left alone.
        return TechnicianProfile.default()


def save_profile(profile: TechnicianProfile) -> None:
    path = _profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    profile.updated_at = datetime.now(timezone.utc).isoformat()
    payload = profile.model_dump(mode="json")
    # Atomic write: write to tmp in same dir, then os.replace.
    fd, tmp_path = tempfile.mkstemp(
        prefix=".technician.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def bump_skill(skill_id: SkillId, evidence: SkillEvidence) -> SkillRecord:
    profile = load_profile()
    rec = profile.skills.get(skill_id) or SkillRecord()
    rec.usages += 1
    rec.last_used = evidence.date
    if rec.first_used is None:
        rec.first_used = evidence.date
    rec.evidences.append(evidence)
    if len(rec.evidences) > SKILL_EVIDENCES_CAP:
        rec.evidences = rec.evidences[-SKILL_EVIDENCES_CAP:]
    profile.skills[skill_id] = rec
    save_profile(profile)
    return rec
```

- [ ] **Step 4.5: Add `memory/_profile/` to `.gitignore`**

Append to `.gitignore`:

```
memory/_profile/
```

- [ ] **Step 4.6: Run tests (expect pass)**

Run: `.venv/bin/pytest tests/profile/test_store.py -v`
Expected: all 6 tests pass.

- [ ] **Step 4.7: Commit**

```bash
git add -- api/profile/store.py tests/profile/test_store.py .gitignore
git commit -m "$(cat <<'EOF'
feat(profile): on-disk store with atomic writes and FIFO evidences

load_profile returns a validated TechnicianProfile (or default on absent/
corrupt file), save_profile writes via tmp + os.replace so partial writes
can't leave the file in an intermediate state, bump_skill appends an
evidence and caps the history at SKILL_EVIDENCES_CAP (20) entries FIFO.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/profile/store.py tests/profile/test_store.py .gitignore
```

---

## Task 5 — Prompt rendering

**Files:**
- Create: `api/profile/prompt.py`
- Test: `tests/profile/test_prompt.py`

- [ ] **Step 5.1: Write failing test**

```python
# SPDX-License-Identifier: Apache-2.0
"""render_technician_block — default + rich profile snapshots."""

from api.profile.catalog import SkillId
from api.profile.model import SkillRecord, TechnicianProfile
from api.profile.prompt import render_technician_block


def test_default_profile_produces_minimal_block():
    p = TechnicianProfile.default()
    block = render_technician_block(p)
    assert "<technician_profile>" in block
    assert "</technician_profile>" in block
    assert "Niveau : beginner" in block
    assert "Verbosité cible : teaching" in block
    assert "Aucun outil déclaré" in block


def test_rich_profile_lists_tools_and_mastered_skills():
    p = TechnicianProfile.default()
    p.identity.name = "Alexis"
    p.identity.years_experience = 5
    p.identity.specialties = ["apple", "consoles"]
    p.tools.soldering_iron = True
    p.tools.hot_air = True
    p.tools.microscope = True
    p.tools.multimeter = True
    p.skills[SkillId.REFLOW_BGA] = SkillRecord(usages=12)
    p.skills[SkillId.JUMPER_WIRE] = SkillRecord(usages=18)
    p.skills[SkillId.MICROSOLDER_0201] = SkillRecord(usages=1)

    block = render_technician_block(p)
    assert "Alexis" in block
    assert "5 ans" in block
    assert "apple, consoles" in block
    assert "soldering_iron" in block
    assert "hot_air" in block
    # Non-disponibles list should include oscilloscope (unchecked)
    assert "oscilloscope" in block
    assert "reflow_bga (12)" in block
    assert "jumper_wire (18)" in block
    assert "microsolder_0201 (1)" in block


def test_block_always_contains_rules_preamble():
    p = TechnicianProfile.default()
    block = render_technician_block(p)
    assert "profile_track_skill" in block
```

- [ ] **Step 5.2: Run tests (expect fail)**

Run: `.venv/bin/pytest tests/profile/test_prompt.py -v`
Expected: FAIL — module missing.

- [ ] **Step 5.3: Implement `api/profile/prompt.py`**

```python
# SPDX-License-Identifier: Apache-2.0
"""Render the <technician_profile> block injected into the agent prompt."""

from __future__ import annotations

from api.profile.catalog import SKILLS_CATALOG, SkillId, ToolId
from api.profile.derive import effective_verbosity, global_level, skills_by_status
from api.profile.model import TechnicianProfile


def _group_skill_ids_with_usages(
    profile: TechnicianProfile, ids: list[str]
) -> list[str]:
    out = []
    for sid in ids:
        rec = profile.skills.get(SkillId(sid))
        usages = rec.usages if rec is not None else 0
        out.append(f"{sid} ({usages})")
    return out


def render_technician_block(profile: TechnicianProfile) -> str:
    level = global_level(profile)
    verbosity = effective_verbosity(profile)
    buckets = skills_by_status(profile)

    tools_have = [t.value for t in ToolId if getattr(profile.tools, t.value)]
    tools_missing = [t.value for t in ToolId if not getattr(profile.tools, t.value)]

    mastered = _group_skill_ids_with_usages(profile, buckets["mastered"])
    practiced = _group_skill_ids_with_usages(profile, buckets["practiced"])
    learning = _group_skill_ids_with_usages(profile, buckets["learning"])

    name = profile.identity.name or "—"
    years = profile.identity.years_experience
    specs = ", ".join(profile.identity.specialties) or "—"

    tools_have_str = ", ".join(tools_have) if tools_have else "Aucun outil déclaré"
    tools_missing_str = ", ".join(tools_missing) if tools_missing else "—"
    mastered_str = ", ".join(mastered) if mastered else "—"
    practiced_str = ", ".join(practiced) if practiced else "—"
    learning_str = ", ".join(learning) if learning else "—"

    return (
        "<technician_profile>\n"
        f"Nom : {name} · {years} ans d'XP · Niveau : {level}\n"
        f"Verbosité cible : {verbosity} "
        "(ajuste si le tech demande plus/moins de détail)\n"
        f"Spécialités : {specs}\n"
        f"Outils disponibles : {tools_have_str}\n"
        f"Outils NON disponibles : {tools_missing_str}\n"
        f"Compétences maîtrisées (≥10×) : {mastered_str}\n"
        f"Compétences pratiquées (3-9×) : {practiced_str}\n"
        f"Compétences en apprentissage (1-2×) : {learning_str}\n"
        "Règles :\n"
        "  - NE propose JAMAIS une action qui requiert un outil non dispo "
        "— propose un workaround ou demande.\n"
        "  - Pour les compétences mastered, va direct au fait "
        "(refdes, geste, fin). Pour learning ou unlearned, détaille les étapes "
        "et les risques.\n"
        "  - Quand le tech confirme avoir exécuté une action, appelle "
        "profile_track_skill avec une evidence claire (refdes, symptôme, "
        "geste résolu).\n"
        "</technician_profile>"
    )
```

- [ ] **Step 5.4: Run tests (expect pass)**

Run: `.venv/bin/pytest tests/profile/test_prompt.py -v`

- [ ] **Step 5.5: Commit**

```bash
git add -- api/profile/prompt.py tests/profile/test_prompt.py
git commit -m "$(cat <<'EOF'
feat(profile): render_technician_block for system-prompt injection

Produces the <technician_profile> string embedded in the agent system
prompt: name / years / level / verbosity / specialties / tools available
and missing / mastered+practiced+learning skills with usage counts /
behavioural rules (skip unavailable tools, adapt verbosity, call
profile_track_skill on confirmed action).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/profile/prompt.py tests/profile/test_prompt.py
```

---

## Task 6 — HTTP router + `api/main.py` include

**Files:**
- Create: `api/profile/router.py`
- Modify: `api/main.py`
- Test: `tests/profile/test_router.py`

- [ ] **Step 6.1: Write failing test**

```python
# SPDX-License-Identifier: Apache-2.0
"""HTTP surface: GET /profile + 3 PUTs."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    from api.config import get_settings
    get_settings.cache_clear()
    from api.main import app
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def test_get_returns_envelope_with_profile_derived_catalog(client: TestClient):
    res = client.get("/profile")
    assert res.status_code == 200
    body = res.json()
    assert "profile" in body
    assert "derived" in body
    assert "catalog" in body
    # Catalog payload shape
    assert {e["id"] for e in body["catalog"]["tools"]}.issuperset({"soldering_iron"})
    assert {e["id"] for e in body["catalog"]["skills"]}.issuperset({"reflow_bga"})
    # Derived payload shape
    assert body["derived"]["level"] == "beginner"
    assert body["derived"]["verbosity_effective"] == "teaching"
    assert "mastered" in body["derived"]["skills_by_status"]


def test_put_identity_persists(client: TestClient):
    res = client.put(
        "/profile/identity",
        json={
            "name": "Alexis",
            "avatar": "AC",
            "years_experience": 5,
            "specialties": ["apple"],
            "level_override": None,
        },
    )
    assert res.status_code == 200
    assert res.json()["profile"]["identity"]["name"] == "Alexis"
    # Re-read independently
    assert client.get("/profile").json()["profile"]["identity"]["name"] == "Alexis"


def test_put_tools_is_full_replace(client: TestClient):
    body = {tool: False for tool in (
        "soldering_iron", "hot_air", "microscope", "oscilloscope",
        "multimeter", "bga_rework", "preheater", "bench_psu",
        "thermal_camera", "reballing_kit", "uv_lamp", "stencil_printer"
    )}
    body["soldering_iron"] = True
    body["hot_air"] = True
    res = client.put("/profile/tools", json=body)
    assert res.status_code == 200
    tools = res.json()["profile"]["tools"]
    assert tools["soldering_iron"] is True
    assert tools["hot_air"] is True
    assert tools["microscope"] is False


def test_put_preferences_persists(client: TestClient):
    res = client.put(
        "/profile/preferences",
        json={"verbosity": "concise", "language": "en"},
    )
    assert res.status_code == 200
    prefs = res.json()["profile"]["preferences"]
    assert prefs["verbosity"] == "concise"
    assert prefs["language"] == "en"


def test_put_identity_rejects_unknown_level_override(client: TestClient):
    res = client.put(
        "/profile/identity",
        json={
            "name": "X", "avatar": "", "years_experience": 0,
            "specialties": [], "level_override": "wizard",
        },
    )
    assert res.status_code == 422
```

- [ ] **Step 6.2: Run tests (expect fail)**

Run: `.venv/bin/pytest tests/profile/test_router.py -v`
Expected: FAIL — no such route.

- [ ] **Step 6.3: Implement `api/profile/router.py`**

```python
# SPDX-License-Identifier: Apache-2.0
"""HTTP surface for the technician profile."""

from __future__ import annotations

from fastapi import APIRouter

from api.profile.catalog import SKILLS_CATALOG, TOOLS_CATALOG
from api.profile.derive import effective_verbosity, global_level, skills_by_status
from api.profile.model import Identity, Preferences, ToolInventory
from api.profile.store import load_profile, save_profile

router = APIRouter(prefix="/profile", tags=["profile"])


def _envelope() -> dict:
    profile = load_profile()
    return {
        "profile": profile.model_dump(mode="json"),
        "derived": {
            "level": global_level(profile),
            "verbosity_effective": effective_verbosity(profile),
            "skills_by_status": skills_by_status(profile),
        },
        "catalog": {
            "tools": [
                {"id": t.id, "label": t.label, "group": t.group}
                for t in TOOLS_CATALOG
            ],
            "skills": [
                {"id": s.id, "label": s.label, "requires": list(s.requires)}
                for s in SKILLS_CATALOG
            ],
        },
    }


@router.get("")
def get_profile() -> dict:
    return _envelope()


@router.put("/identity")
def put_identity(identity: Identity) -> dict:
    profile = load_profile()
    profile.identity = identity
    save_profile(profile)
    return _envelope()


@router.put("/tools")
def put_tools(tools: ToolInventory) -> dict:
    profile = load_profile()
    profile.tools = tools
    save_profile(profile)
    return _envelope()


@router.put("/preferences")
def put_preferences(prefs: Preferences) -> dict:
    profile = load_profile()
    profile.preferences = prefs
    save_profile(profile)
    return _envelope()
```

- [ ] **Step 6.4: Wire router into `api/main.py`**

In `api/main.py`, next to the other `from … import router`:

```python
from api.profile.router import router as profile_router
```

Find the block that includes other routers (search for `app.include_router(pipeline_router)` or `app.include_router(board_router)`) and add:

```python
app.include_router(profile_router)
```

- [ ] **Step 6.5: Run tests (expect pass)**

Run: `.venv/bin/pytest tests/profile/test_router.py -v`
Expected: 5 tests pass.

- [ ] **Step 6.6: Commit**

```bash
git add -- api/profile/router.py api/main.py tests/profile/test_router.py
git commit -m "$(cat <<'EOF'
feat(profile): HTTP surface (GET /profile + 3 PUT blocks)

Single GET returns a full envelope {profile, derived, catalog} so the
frontend hydrates in one round-trip. Three PUT endpoints (identity, tools,
preferences) replace their block atomically and return the refreshed
envelope. Skills are not exposed via PUT — they only evolve through the
agent's profile_track_skill tool.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/profile/router.py api/main.py tests/profile/test_router.py
```

---

## Task 7 — Agent tools (`profile_*` handlers)

**Files:**
- Create: `api/profile/tools.py`
- Test: `tests/profile/test_tools.py`

- [ ] **Step 7.1: Write failing test**

```python
# SPDX-License-Identifier: Apache-2.0
"""profile_get / profile_check_skills / profile_track_skill handlers."""

from pathlib import Path

import pytest

from api.profile.catalog import SkillId
from api.profile.model import SkillRecord, TechnicianProfile
from api.profile.store import save_profile
from api.profile.tools import (
    EVIDENCE_MIN_CHARS,
    profile_check_skills,
    profile_get,
    profile_track_skill,
)


@pytest.fixture
def memroot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path))
    from api.config import get_settings
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_profile_get_default_shape(memroot: Path):
    out = profile_get()
    assert set(out["identity"].keys()) >= {"name", "years_experience", "specialties"}
    assert out["level"] == "beginner"
    assert out["verbosity_effective"] == "teaching"
    assert "soldering_iron" in out["tools_missing"]
    assert out["skills_summary"]["mastered"] == []


def test_profile_check_skills_reports_tools_ok_flag(memroot: Path):
    p = TechnicianProfile.default()
    p.tools.multimeter = True
    p.skills[SkillId.SHORT_ISOLATION] = SkillRecord(usages=4)
    save_profile(p)

    out = profile_check_skills(["short_isolation", "reballing"])
    assert out["short_isolation"]["status"] == "practiced"
    assert out["short_isolation"]["tools_ok"] is True
    assert out["reballing"]["tools_ok"] is False
    assert set(out["reballing"]["missing_tools"]) == {"bga_rework", "reballing_kit"}


def test_profile_check_skills_rejects_unknown(memroot: Path):
    out = profile_check_skills(["short_isolation", "nonsense_skill"])
    assert out["nonsense_skill"] == {"error": "not_in_catalog"}
    assert "status" in out["short_isolation"]


def test_profile_track_skill_rejects_thin_evidence(memroot: Path):
    out = profile_track_skill(
        "reflow_bga",
        {"repair_id": "r1", "device_slug": "ix", "symptom": "dead",
         "action_summary": "short", "date": "2026-04-22T10:00:00Z"},
    )
    assert out["error"] == "evidence_too_thin"
    assert EVIDENCE_MIN_CHARS in str(out.get("min_chars", "")) or "min_chars" in out


def test_profile_track_skill_rejects_unknown_id(memroot: Path):
    out = profile_track_skill(
        "not_a_skill",
        {"repair_id": "r1", "device_slug": "ix", "symptom": "dead",
         "action_summary": "an action summary that is long enough to pass the guard",
         "date": "2026-04-22T10:00:00Z"},
    )
    assert out["error"] == "unknown_skill"
    assert "closest_matches" in out


def test_profile_track_skill_happy_path_promotes(memroot: Path):
    p = TechnicianProfile.default()
    p.skills[SkillId.REFLOW_BGA] = SkillRecord(usages=9)
    save_profile(p)

    out = profile_track_skill(
        "reflow_bga",
        {"repair_id": "r1", "device_slug": "ix", "symptom": "no_boot",
         "action_summary": "Reflow du PMIC U2 après court-circuit VDD_MAIN",
         "date": "2026-04-22T10:00:00Z"},
    )
    assert out["usages_before"] == 9
    assert out["usages_after"] == 10
    assert out["status_before"] == "practiced"
    assert out["status_after"] == "mastered"
    assert out["promoted"] is True
```

- [ ] **Step 7.2: Run tests (expect fail)**

Run: `.venv/bin/pytest tests/profile/test_tools.py -v`
Expected: FAIL — module missing.

- [ ] **Step 7.3: Implement `api/profile/tools.py`**

```python
# SPDX-License-Identifier: Apache-2.0
"""Agent-facing tool handlers for the technician profile.

Three tools:
  - profile_get: full read (identity, level, verbosity, tool bitmap, skills
    grouped by status).
  - profile_check_skills(candidate_skills): per-skill status + tool
    availability.
  - profile_track_skill(skill_id, evidence): bump usages, append evidence.
    Guards: skill must be in catalogue, evidence.action_summary must be
    >= EVIDENCE_MIN_CHARS.
"""

from __future__ import annotations

from typing import Any

from api.profile.catalog import SKILLS_CATALOG, SkillId, ToolId
from api.profile.derive import effective_verbosity, global_level, skill_status, skills_by_status
from api.profile.model import SkillEvidence
from api.profile.store import bump_skill, load_profile


EVIDENCE_MIN_CHARS = 20

_SKILL_LOOKUP = {entry.id: entry for entry in SKILLS_CATALOG}


def _skills_summary(profile) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {
        "mastered": [], "practiced": [], "learning": []
    }
    for entry in SKILLS_CATALOG:
        rec = profile.skills.get(entry.id)
        if rec is None or rec.usages == 0:
            continue
        status = skill_status(rec.usages)
        if status == "unlearned":
            continue
        out[status].append({"id": entry.id, "usages": rec.usages})
    return out


def profile_get() -> dict[str, Any]:
    profile = load_profile()
    return {
        "identity": {
            "name": profile.identity.name,
            "avatar": profile.identity.avatar,
            "years_experience": profile.identity.years_experience,
            "specialties": profile.identity.specialties,
        },
        "level": global_level(profile),
        "verbosity_effective": effective_verbosity(profile),
        "tools_available": [
            t.value for t in ToolId if getattr(profile.tools, t.value)
        ],
        "tools_missing": [
            t.value for t in ToolId if not getattr(profile.tools, t.value)
        ],
        "skills_summary": _skills_summary(profile),
    }


def profile_check_skills(candidate_skills: list[str]) -> dict[str, Any]:
    profile = load_profile()
    out: dict[str, Any] = {}
    for sid in candidate_skills:
        entry = _SKILL_LOOKUP.get(sid)
        if entry is None:
            out[sid] = {"error": "not_in_catalog"}
            continue
        rec = profile.skills.get(entry.id)
        usages = rec.usages if rec is not None else 0
        missing = [
            req for req in entry.requires
            if not getattr(profile.tools, req)
        ]
        out[sid] = {
            "status": skill_status(usages),
            "usages": usages,
            "tools_ok": len(missing) == 0,
            "missing_tools": missing,
        }
    return out


def _closest_skill_matches(skill_id: str, limit: int = 3) -> list[str]:
    # Very simple prefix / substring heuristic — good enough here.
    needle = skill_id.lower()
    scored = []
    for entry in SKILLS_CATALOG:
        cand = entry.id
        if cand.startswith(needle[:3]) or needle[:3] in cand:
            scored.append(cand)
    return scored[:limit]


def profile_track_skill(skill_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
    entry = _SKILL_LOOKUP.get(skill_id)
    if entry is None:
        return {
            "error": "unknown_skill",
            "closest_matches": _closest_skill_matches(skill_id),
        }

    action_summary = (evidence or {}).get("action_summary", "")
    if len(action_summary.strip()) < EVIDENCE_MIN_CHARS:
        return {
            "error": "evidence_too_thin",
            "min_chars": EVIDENCE_MIN_CHARS,
            "got_chars": len(action_summary.strip()),
        }

    try:
        ev = SkillEvidence.model_validate(evidence)
    except Exception as exc:
        return {"error": "invalid_evidence", "detail": str(exc)}

    profile = load_profile()
    prev = profile.skills.get(SkillId(skill_id))
    usages_before = prev.usages if prev is not None else 0
    status_before = skill_status(usages_before)

    rec = bump_skill(SkillId(skill_id), ev)
    status_after = skill_status(rec.usages)

    return {
        "skill_id": skill_id,
        "usages_before": usages_before,
        "usages_after": rec.usages,
        "status_before": status_before,
        "status_after": status_after,
        "promoted": status_before != status_after,
    }
```

- [ ] **Step 7.4: Run tests (expect pass)**

Run: `.venv/bin/pytest tests/profile/test_tools.py -v`

- [ ] **Step 7.5: Commit**

```bash
git add -- api/profile/tools.py tests/profile/test_tools.py
git commit -m "$(cat <<'EOF'
feat(profile): agent tool handlers (get / check_skills / track_skill)

profile_get returns the full profile snapshot consumed by the agent at
session start. profile_check_skills reports per-skill status + tool
availability so the agent can adapt depth per step. profile_track_skill
bumps usages on explicit tech confirmation, guarded by a 20-char evidence
minimum and closed-catalogue membership.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/profile/tools.py tests/profile/test_tools.py
```

---

## Task 8 — Manifest integration (agent always sees `profile_*`)

**Files:**
- Modify: `api/agent/manifest.py`
- Test: `tests/agent/test_manifest_dynamic.py`

- [ ] **Step 8.1: Read the current `build_tools_manifest` and its tests**

Run: `grep -n "build_tools_manifest\|PROFILE_TOOLS\|MB_TOOLS\|BV_TOOLS" api/agent/manifest.py tests/agent/test_manifest_dynamic.py`

Get an anchor point for the `MB_TOOLS` list end and the `build_tools_manifest` body.

- [ ] **Step 8.2: Add the failing assertion to the existing test**

In `tests/agent/test_manifest_dynamic.py`, append:

```python
def test_profile_tools_always_present():
    from api.agent.manifest import build_tools_manifest
    from api.session.state import SessionState

    names = {t["name"] for t in build_tools_manifest(SessionState())}
    assert {"profile_get", "profile_check_skills", "profile_track_skill"} <= names

    session_with_board = SessionState.from_device("iphone-x-logic-board")  # whatever fixture exists
    names2 = {t["name"] for t in build_tools_manifest(session_with_board)}
    assert {"profile_get", "profile_check_skills", "profile_track_skill"} <= names2
```

*(If the file already has a pattern for "session without board", reuse it rather than introducing a new fixture. The key assertion is: `profile_*` appear regardless of `session.board`.)*

- [ ] **Step 8.3: Run test (expect fail)**

Run: `.venv/bin/pytest tests/agent/test_manifest_dynamic.py::test_profile_tools_always_present -v`
Expected: FAIL — names missing.

- [ ] **Step 8.4: Add `PROFILE_TOOLS` to `api/agent/manifest.py`**

After `BV_TOOLS = [...]` and before `def build_tools_manifest`, insert:

```python
PROFILE_TOOLS: list[dict] = [
    {
        "type": "custom",
        "name": "profile_get",
        "description": (
            "Read the technician's profile: identity, current level, "
            "verbosity preference, list of available and missing tools, and "
            "summary of mastered/practiced/learning skills with usage counts. "
            "Call once at session start if the system prompt context is stale, "
            "or when the tech reports having updated their profile."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "type": "custom",
        "name": "profile_check_skills",
        "description": (
            "Given a list of candidate skill ids from the catalogue (e.g. "
            "reflow_bga, short_isolation), return for each: the tech's status "
            "(unlearned|learning|practiced|mastered), usage count, whether the "
            "required tools are available, and if not the missing tool ids. "
            "Use BEFORE proposing an action plan so you can adapt depth per step "
            "and skip actions with missing tools."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "candidate_skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
            },
            "required": ["candidate_skills"],
        },
    },
    {
        "type": "custom",
        "name": "profile_track_skill",
        "description": (
            "Record that the technician has executed an action requiring this "
            "skill, with evidence. Call ONLY after explicit confirmation from "
            "the tech that the action was performed. action_summary must be at "
            "least 20 characters and quote the actual fix (refdes, symptom, "
            "outcome) — the backend rejects thin evidence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_id": {"type": "string"},
                "evidence": {
                    "type": "object",
                    "properties": {
                        "repair_id": {"type": "string"},
                        "device_slug": {"type": "string"},
                        "symptom": {"type": "string"},
                        "action_summary": {"type": "string", "minLength": 20},
                        "date": {"type": "string"},
                    },
                    "required": ["repair_id", "device_slug", "symptom", "action_summary", "date"],
                },
            },
            "required": ["skill_id", "evidence"],
        },
    },
]
```

Then replace `build_tools_manifest`:

```python
def build_tools_manifest(session: SessionState) -> list[dict]:
    """Return the tools list for `session`. `profile_*` always present; `bv_*`
    only when a board is loaded. Future: `sch_*` when a schematic is attached."""
    manifest: list[dict] = list(MB_TOOLS) + list(PROFILE_TOOLS)
    if session.board is not None:
        manifest.extend(BV_TOOLS)
    return manifest
```

- [ ] **Step 8.5: Run test (expect pass)**

Run: `.venv/bin/pytest tests/agent/test_manifest_dynamic.py -v`
Expected: all pass including the new assertion.

- [ ] **Step 8.6: Commit**

```bash
git add -- api/agent/manifest.py tests/agent/test_manifest_dynamic.py
git commit -m "$(cat <<'EOF'
feat(agent): profile_* tools always in manifest

profile_get / profile_check_skills / profile_track_skill are unconditional —
not gated on session.board. Their descriptions spell out the guards
(closed catalogue, 20-char evidence minimum) so the LLM knows in advance
when a call will be rejected.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/agent/manifest.py tests/agent/test_manifest_dynamic.py
```

---

## Task 9 — DIRECT runtime integration (system prompt + tool dispatch)

**Files:**
- Modify: `api/agent/manifest.py` (`render_system_prompt`)
- Modify: `api/agent/runtime_direct.py` (tool loop)
- Test: add an inline check to `tests/profile/test_prompt.py` covering the integrated prompt.

- [ ] **Step 9.1: Extend `render_system_prompt`**

In `api/agent/manifest.py`, import at top:

```python
from api.profile.prompt import render_technician_block
from api.profile.store import load_profile
```

Update `render_system_prompt` to inject the block just above the "RÈGLE ANTI-HALLUCINATION" line:

```python
def render_system_prompt(session: SessionState, *, device_slug: str) -> str:
    boardview_status = "✅" if session.board is not None else "❌ (no board file loaded)"
    schematic_status = (
        "✅ (mb_schematic_graph)"
        if _has_electrical_graph(device_slug)
        else "❌ (not yet parsed)"
    )
    technician_block = render_technician_block(load_profile())
    return f"""\
You are a calm, methodical board-level diagnostics assistant for a
microsoldering technician. Tu tutoies, en français, direct et pédagogique.

Device courant : {device_slug}.

{technician_block}

Capabilities for this session:
  - memory bank ✅ (mb_get_component, mb_get_rules_for_symptoms, mb_list_findings, mb_record_finding, mb_expand_knowledge)
  - profile ✅ (profile_get, profile_check_skills, profile_track_skill)
  - boardview {boardview_status}
  - schematic {schematic_status}

RÈGLE ANTI-HALLUCINATION : tu NE mentionnes JAMAIS un refdes (U7, C29,
J3100…) sans l'avoir validé via mb_get_component. Si le tool retourne
{{found: false, closest_matches: [...]}}, tu proposes une des
closest_matches ou tu demandes clarification — JAMAIS d'invention. Les
refdes non validés seront automatiquement wrapped ⟨?U999⟩ dans la
réponse finale (sanitizer post-hoc) — signal de debug, pas d'excuse.

Quand l'utilisateur décrit des symptômes, consulte d'abord mb_list_findings
(historique cross-session de ce device), puis mb_get_rules_for_symptoms.
Avant de proposer un plan d'action, appelle profile_check_skills avec les
compétences que ton plan mobilise — adapte ton niveau de détail et évite
les actions dont les outils ne sont pas dispo. Quand le tech confirme
avoir exécuté une étape avec succès, appelle profile_track_skill (evidence
concrète — refdes, symptôme, geste — JAMAIS un résumé vague).

Si mb_get_rules_for_symptoms retourne 0 matches sur un symptôme sérieux,
PROPOSE mb_expand_knowledge ("Je peux lancer un Scout ciblé sur ces symptômes
— ~30s, ~0.40$ de tokens. Go ?"). NE LANCE PAS tant que le tech n'a pas dit
oui. Après son go, invoque le tool, patiente, puis re-call
mb_get_rules_for_symptoms. Quand il demande un composant, appelle
mb_get_component. Si la boardview est disponible, enchaîne bv_focus +
bv_highlight pour MONTRER le suspect. Quand le tech confirme la cause,
appelle mb_record_finding. Ne réponds JAMAIS depuis ta mémoire de formation
pour des refdes ou des symptômes — utilise toujours les tools ci-dessus.
"""
```

- [ ] **Step 9.2: Add assertion to `tests/profile/test_prompt.py`**

```python
def test_system_prompt_includes_technician_block():
    from api.agent.manifest import render_system_prompt
    from api.session.state import SessionState

    prompt = render_system_prompt(SessionState(), device_slug="demo-pi")
    assert "<technician_profile>" in prompt
    assert "profile_check_skills" in prompt
    assert "profile_track_skill" in prompt
```

- [ ] **Step 9.3: Dispatch `profile_*` in `runtime_direct.py`**

Find the branch that tests `name.startswith("mb_")` (or the `_dispatch_mb_tool` call). Add just before the `unknown-tool` fallback:

```python
# runtime_direct.py — inside the tool_use loop, after bv_* + mb_* dispatches:
elif name.startswith("profile_"):
    from api.profile.tools import (
        profile_check_skills as _profile_check_skills,
        profile_get as _profile_get,
        profile_track_skill as _profile_track_skill,
    )
    if name == "profile_get":
        result = _profile_get()
    elif name == "profile_check_skills":
        result = _profile_check_skills(
            payload.get("candidate_skills", [])
        )
    elif name == "profile_track_skill":
        result = _profile_track_skill(
            payload.get("skill_id", ""),
            payload.get("evidence", {}),
        )
    else:
        result = {"ok": False, "reason": "unknown-tool"}
```

*(The exact insertion point depends on the current file — the test_manifest_dynamic extension in Task 8 confirms the names but dispatch lives in `runtime_direct.py`. Read lines 150-230 first to find the current dispatch pattern, then add the profile branch next to mb/bv dispatches.)*

- [ ] **Step 9.4: Run tests**

Run: `.venv/bin/pytest tests/profile/ tests/agent/ -v`
Expected: all pass.

- [ ] **Step 9.5: Commit**

```bash
git add -- api/agent/manifest.py api/agent/runtime_direct.py \
           tests/profile/test_prompt.py
git commit -m "$(cat <<'EOF'
feat(agent): DIRECT runtime reads profile + dispatches profile_* tools

render_system_prompt injects <technician_profile> from load_profile() so
the agent sees the tech's level / tools / mastered skills on the first
turn. The direct tool loop dispatches profile_{get,check_skills,track_skill}
next to the existing mb_* / bv_* branches.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/agent/manifest.py api/agent/runtime_direct.py \
      tests/profile/test_prompt.py
```

---

## Task 10 — MANAGED runtime integration

**Files:**
- Modify: `api/agent/runtime_managed.py`

The MANAGED path carries its system prompt server-side via `managed_ids.json` and cannot have the technician block injected there. We push a synthetic user message `[CONTEXTE TECHNICIEN]` once per WS connection, just after session open, before relaying the first real user message.

- [ ] **Step 10.1: Read the current session-open flow**

Run: `grep -n "session.create\|sessions.create\|memory_seed\|first message" api/agent/runtime_managed.py | head -30`

Identify (a) where the MA session is created/resumed, (b) where device context is currently seeded (memory_seed pattern per CLAUDE.md).

- [ ] **Step 10.2: Add the profile injection helper**

Near the top of `api/agent/runtime_managed.py`:

```python
from api.profile.prompt import render_technician_block
from api.profile.store import load_profile


def _build_technician_context_message() -> str:
    """Compose the one-shot user message that carries the technician block."""
    return "[CONTEXTE TECHNICIEN]\n" + render_technician_block(load_profile())
```

- [ ] **Step 10.3: Push the context message after session open**

At the point in `run_diagnostic_session_managed` (or equivalent) where the session is first created — AND before the first client user message is relayed to the MA endpoint — send the context message as a `user` message in the MA session (the exact SDK call mirrors how existing client messages are forwarded; look at where `{"type": "message", "text": client_text}` events are turned into MA calls and use the same entry point with `_build_technician_context_message()` as the text).

- [ ] **Step 10.4: Also dispatch `profile_*` tool calls in the MA path**

When the MA emits `agent.custom_tool_use` events for `profile_get`, `profile_check_skills`, `profile_track_skill`, handle them identically to the DIRECT runtime. Given the MA path already has a dispatcher for custom tools (it cached `agent.custom_tool_use` and replies via `user.custom_tool_result`), extend that dispatcher with the same three branches as Task 9.3.

- [ ] **Step 10.5: Smoke test via WS (manual — no automated test)**

No unit test for the MA runtime (would need a full MA mock). Manual procedure:

1. `make run`
2. Open `http://localhost:8000/#profile`, edit identity (`Name: Alexis, 5 ans, hot_air + microscope + multimetre + bench_psu + uv_lamp coché`)
3. Return home, start a new repair on any device in `DIAGNOSTIC_MODE=direct` mode first.
4. In the chat panel, ask: "Tu peux me dire mon niveau et les outils que tu sais que j'ai ?"
5. Verify the agent quotes the profile block in its response (level beginner, hot_air + microscope + … listed).
6. Repeat with `DIAGNOSTIC_MODE=managed`.

- [ ] **Step 10.6: Commit**

```bash
git add -- api/agent/runtime_managed.py
git commit -m "$(cat <<'EOF'
feat(agent): MANAGED runtime pushes technician context at session open

A synthetic [CONTEXTE TECHNICIEN] user message carrying the rendered
<technician_profile> block is sent once per WS connection, just after
session open and before the first real user turn. profile_* custom-tool
calls are dispatched identically to the DIRECT path.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- api/agent/runtime_managed.py
```

---

## Task 11 — Frontend shell (DOM + CSS + read-only render)

**Files:**
- Modify: `web/index.html`
- Create: `web/styles/profile.css`
- Create: `web/js/profile.js`
- Modify: `web/js/main.js`
- Modify: `web/js/router.js`

This task delivers a visually complete `#profile` section that **reads** from `GET /profile` and renders identity, tool bitmap, 4-bucket skills, preferences. Edits come in Task 12.

- [ ] **Step 11.1: Replace the stub in `web/index.html` with a mount point**

Find `<section class="stub hidden" data-section-stub="profile">…</section>` (around line 157) and replace with a minimal mount point — the HTML body itself lives in `web/profil.html`:

```html
<section class="profile hidden" id="profileSection" data-partial="/profil.html"></section>
```

Add the CSS `<link>` in `<head>`:

```html
<link rel="stylesheet" href="/styles/profile.css" />
```

Add the JS `<script>` at the bottom, next to `main.js`:

```html
<script type="module" src="/js/profile.js"></script>
```

- [ ] **Step 11.1b: Create `web/profil.html` with the full section body + identity modal**

```html
<!-- Profile section body. Fetched by web/js/profile.js and injected into
     #profileSection in index.html on first navigation. The identity
     modal lives here too so it ships with the section's DOM in one fetch. -->
<header class="profile-head">
  <div class="profile-avatar" id="profAvatar">—</div>
  <div class="profile-head-main">
    <h1 id="profName">—</h1>
    <p class="profile-head-meta">
      <span id="profLevel" class="tag">—</span>
      <span>·</span>
      <span id="profYears">—</span>
      <span>·</span>
      <span id="profSpecs">—</span>
    </p>
  </div>
  <button class="btn" id="profEditIdentityBtn">
    <svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>
    Éditer
  </button>
</header>

<section class="profile-block">
  <h2>Outillage</h2>
  <div class="profile-tools" id="profTools"></div>
</section>

<section class="profile-block">
  <h2>Compétences</h2>
  <div class="profile-skills" id="profSkills"></div>
</section>

<section class="profile-block">
  <h2>Préférences diagnostic</h2>
  <div class="profile-prefs" id="profPrefs"></div>
</section>

<aside class="profile-drawer hidden" id="profDrawer">
  <header class="profile-drawer-head">
    <h3 id="profDrawerTitle">—</h3>
    <button class="btn-icon" id="profDrawerClose" aria-label="Fermer">
      <svg class="icon" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6l-12 12"/></svg>
    </button>
  </header>
  <div class="profile-drawer-body" id="profDrawerBody"></div>
</aside>

<!-- Identity edit modal (opened by #profEditIdentityBtn). Wired in Task 12. -->
<div class="modal hidden" id="profIdentityModal">
  <div class="modal-backdrop" data-dismiss></div>
  <div class="modal-card">
    <header class="modal-head">
      <h3>Profil technicien</h3>
      <button class="btn-icon" data-dismiss aria-label="Fermer">
        <svg class="icon" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6l-12 12"/></svg>
      </button>
    </header>
    <form id="profIdentityForm" class="modal-body">
      <label class="modal-field">
        <span>Nom</span>
        <input name="name" type="text" maxlength="40"/>
      </label>
      <label class="modal-field">
        <span>Avatar (2 lettres ou emoji)</span>
        <input name="avatar" type="text" maxlength="2"/>
      </label>
      <label class="modal-field">
        <span>Années d'expérience</span>
        <input name="years_experience" type="number" min="0" max="80"/>
      </label>
      <label class="modal-field">
        <span>Spécialités (séparées par virgule)</span>
        <input name="specialties" type="text" placeholder="apple, consoles, laptops"/>
      </label>
      <label class="modal-field">
        <span>Forcer le niveau (optionnel)</span>
        <select name="level_override">
          <option value="">(dérivé automatiquement)</option>
          <option value="beginner">beginner</option>
          <option value="intermediate">intermediate</option>
          <option value="confirmed">confirmed</option>
          <option value="expert">expert</option>
        </select>
      </label>
      <footer class="modal-foot">
        <button type="button" class="btn" data-dismiss>Annuler</button>
        <button type="submit" class="btn primary">Enregistrer</button>
      </footer>
    </form>
  </div>
</div>
```

- [ ] **Step 11.2: Create `web/styles/profile.css`**

```css
/* =========== PROFILE SECTION =========== */
.profile{
  position:fixed;top:92px;left:52px;right:0;bottom:28px;
  overflow-y:auto;padding:28px 32px;z-index:2;
}
.profile.hidden{display:none}
body.no-metabar .profile{top:92px} /* stays 92 because no-metabar applies elsewhere */

/* head */
.profile-head{
  display:flex;align-items:center;gap:16px;padding:20px;
  background:var(--panel);border:1px solid var(--border);border-radius:10px;
}
.profile-avatar{
  width:64px;height:64px;border-radius:50%;
  background:var(--panel-2);border:1px solid var(--border);
  display:flex;align-items:center;justify-content:center;
  font-family:var(--mono);font-size:22px;letter-spacing:.5px;color:var(--text);
}
.profile-head-main{flex:1}
.profile-head-main h1{margin:0 0 4px;font-size:18px;color:var(--text)}
.profile-head-meta{margin:0;color:var(--text-2);font-size:12px;display:flex;gap:8px;align-items:center}
.profile-head-meta .tag{
  font-family:var(--mono);font-size:10.5px;text-transform:uppercase;letter-spacing:.4px;
  padding:2px 6px;border:1px solid var(--border);border-radius:4px;color:var(--cyan);
}

/* block shells */
.profile-block{
  margin-top:20px;padding:18px 20px;
  background:var(--panel);border:1px solid var(--border);border-radius:10px;
}
.profile-block > h2{
  margin:0 0 12px;font-size:11px;text-transform:uppercase;letter-spacing:.5px;
  color:var(--text-2);font-weight:500;
}

/* tools grid */
.profile-tools{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px}
.profile-tool{
  display:flex;align-items:center;gap:8px;padding:8px 10px;
  background:var(--panel-2);border:1px solid var(--border);border-radius:6px;
  cursor:pointer;transition:all .15s;font-size:13px;color:var(--text-2);
}
.profile-tool:hover{border-color:var(--border-soft);color:var(--text)}
.profile-tool.on{color:var(--emerald);border-color:rgba(110,200,150,.4)}
.profile-tool .dot{
  width:8px;height:8px;border-radius:50%;background:var(--text-3);
}
.profile-tool.on .dot{background:var(--emerald)}

/* skills — 4 columns of status */
.profile-skills{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.profile-skill-col > h3{
  margin:0 0 8px;font-family:var(--mono);font-size:10px;text-transform:uppercase;
  letter-spacing:.5px;color:var(--text-3);
}
.profile-skill-col[data-status="mastered"] > h3{color:var(--violet)}
.profile-skill-col[data-status="practiced"] > h3{color:var(--emerald)}
.profile-skill-col[data-status="learning"]  > h3{color:var(--cyan)}
.profile-skill-col[data-status="unlearned"] > h3{color:var(--text-3)}

.profile-skill{
  display:flex;flex-direction:column;gap:4px;padding:8px 10px;margin-bottom:6px;
  background:var(--panel-2);border:1px solid var(--border);border-radius:6px;
  cursor:pointer;transition:border-color .15s;
}
.profile-skill:hover{border-color:var(--border-soft)}
.profile-skill-label{font-size:12px;color:var(--text)}
.profile-skill-meta{display:flex;align-items:center;justify-content:space-between;gap:8px}
.profile-skill-bar{flex:1;height:3px;background:var(--border-soft);border-radius:2px;overflow:hidden}
.profile-skill-bar > span{display:block;height:100%;background:var(--cyan)}
.profile-skill-count{
  font-family:var(--mono);font-size:10.5px;color:var(--text-2);text-transform:uppercase;letter-spacing:.4px;
}

/* prefs */
.profile-prefs{display:flex;flex-wrap:wrap;gap:16px}
.profile-prefs-group{display:flex;flex-direction:column;gap:4px}
.profile-prefs-group label{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--text-3)}
.profile-prefs-group .opts{display:flex;gap:4px}
.profile-prefs-opt{
  padding:4px 10px;border:1px solid var(--border);border-radius:6px;
  font-size:12px;color:var(--text-2);background:var(--panel-2);cursor:pointer;
}
.profile-prefs-opt.on{color:var(--cyan);border-color:rgba(100,190,230,.4)}

/* drawer (glass) */
.profile-drawer{
  position:absolute;top:0;right:0;bottom:0;width:360px;
  background:rgba(24,24,27,.92);backdrop-filter:blur(10px);
  border-left:1px solid var(--border);
  display:flex;flex-direction:column;z-index:3;
  animation:profDrawerIn .28s cubic-bezier(.2,.8,.2,1);
}
.profile-drawer.hidden{display:none}
@keyframes profDrawerIn{from{transform:translateX(20px);opacity:0}to{transform:translateX(0);opacity:1}}
.profile-drawer-head{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--border)}
.profile-drawer-head h3{margin:0;font-size:14px;color:var(--text)}
.profile-drawer-body{padding:14px 18px;overflow-y:auto;display:flex;flex-direction:column;gap:8px}
.profile-evidence{padding:10px;background:var(--panel-2);border:1px solid var(--border);border-radius:6px;font-size:12px;color:var(--text-2)}
.profile-evidence .dev{font-family:var(--mono);font-size:10.5px;color:var(--cyan);text-transform:uppercase;letter-spacing:.4px}
.profile-evidence .sum{display:block;margin-top:4px;color:var(--text)}
.profile-evidence .date{display:block;margin-top:4px;font-family:var(--mono);font-size:10px;color:var(--text-3)}
```

- [ ] **Step 11.3: Create `web/js/profile.js`**

```javascript
// Technician profile section.
// On first activation, fetches web/profil.html (the section's DOM partial)
// and injects it into #profileSection. Subsequent activations skip the fetch.
// Consumes GET /profile and renders identity / tools / skills / preferences.
// Tool toggles → PUT /profile/tools ; preference changes → PUT /profile/preferences
// ; skill click opens the evidence drawer. Identity modal handler lands in Task 12.

let _state = null;    // {profile, derived, catalog}
let _partialLoaded = false;

async function ensurePartial() {
  if (_partialLoaded) return;
  const mount = document.getElementById("profileSection");
  const url = mount.dataset.partial || "/profil.html";
  const res = await fetch(url);
  if (!res.ok) throw new Error(`partial ${url} → ${res.status}`);
  mount.innerHTML = await res.text();
  _partialLoaded = true;
}

const STATUS_LABELS = {
  mastered:  "Maîtrisées",
  practiced: "Pratiquées",
  learning:  "En apprentissage",
  unlearned: "Non pratiquées",
};
const VERBOSITIES = ["auto", "concise", "normal", "teaching"];
const LANGUAGES = ["fr", "en"];

async function fetchJSON(url, init) {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`${init?.method || "GET"} ${url} → ${res.status}`);
  return res.json();
}

function fmtYears(n) {
  if (!n) return "0 an";
  return `${n} an${n > 1 ? "s" : ""} d'XP`;
}

function renderHead() {
  const id = _state.profile.identity;
  const level = _state.derived.level;
  document.getElementById("profAvatar").textContent = id.avatar || (id.name.slice(0,2).toUpperCase() || "—");
  document.getElementById("profName").textContent = id.name || "Sans nom";
  document.getElementById("profLevel").textContent = level.toUpperCase();
  document.getElementById("profYears").textContent = fmtYears(id.years_experience);
  document.getElementById("profSpecs").textContent = id.specialties.length ? id.specialties.join(" · ") : "Sans spécialité";
}

function renderTools() {
  const host = document.getElementById("profTools");
  host.innerHTML = "";
  for (const tool of _state.catalog.tools) {
    const on = !!_state.profile.tools[tool.id];
    const chip = document.createElement("div");
    chip.className = "profile-tool" + (on ? " on" : "");
    chip.innerHTML = `<span class="dot"></span><span>${tool.label}</span>`;
    chip.addEventListener("click", () => toggleTool(tool.id));
    host.appendChild(chip);
  }
}

async function toggleTool(toolId) {
  const nextTools = { ..._state.profile.tools };
  nextTools[toolId] = !nextTools[toolId];
  const fresh = await fetchJSON("/profile/tools", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(nextTools),
  });
  _state = fresh;
  renderTools();
}

function renderSkills() {
  const host = document.getElementById("profSkills");
  host.innerHTML = "";
  const buckets = _state.derived.skills_by_status;
  const bySkillId = new Map(_state.catalog.skills.map(s => [s.id, s]));

  for (const status of ["mastered", "practiced", "learning", "unlearned"]) {
    const col = document.createElement("div");
    col.className = "profile-skill-col";
    col.dataset.status = status;
    col.innerHTML = `<h3>${STATUS_LABELS[status]}</h3>`;
    const ids = buckets[status] || [];
    for (const sid of ids) {
      const entry = bySkillId.get(sid);
      if (!entry) continue;
      const rec = _state.profile.skills[sid];
      const usages = rec ? rec.usages : 0;
      const pct = Math.min(100, (usages / 12) * 100); // 12 = bar reference max
      const card = document.createElement("div");
      card.className = "profile-skill";
      card.innerHTML = `
        <span class="profile-skill-label">${entry.label}</span>
        <div class="profile-skill-meta">
          <div class="profile-skill-bar"><span style="width:${pct}%"></span></div>
          <span class="profile-skill-count">${usages}×</span>
        </div>`;
      card.addEventListener("click", () => openDrawer(sid, entry, rec));
      col.appendChild(card);
    }
    host.appendChild(col);
  }
}

function renderPrefs() {
  const host = document.getElementById("profPrefs");
  host.innerHTML = "";
  const prefs = _state.profile.preferences;

  const makeGroup = (label, key, options) => {
    const g = document.createElement("div");
    g.className = "profile-prefs-group";
    g.innerHTML = `<label>${label}</label><div class="opts"></div>`;
    const opts = g.querySelector(".opts");
    for (const v of options) {
      const btn = document.createElement("button");
      btn.className = "profile-prefs-opt" + (prefs[key] === v ? " on" : "");
      btn.textContent = v;
      btn.addEventListener("click", () => changePref(key, v));
      opts.appendChild(btn);
    }
    return g;
  };

  host.appendChild(makeGroup("Verbosité", "verbosity", VERBOSITIES));
  host.appendChild(makeGroup("Langue", "language", LANGUAGES));
}

async function changePref(key, value) {
  const next = { ..._state.profile.preferences, [key]: value };
  const fresh = await fetchJSON("/profile/preferences", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(next),
  });
  _state = fresh;
  renderPrefs();
  renderHead(); // verbosity_effective may change head hint in future
}

function openDrawer(sid, entry, rec) {
  const drawer = document.getElementById("profDrawer");
  drawer.classList.remove("hidden");
  document.getElementById("profDrawerTitle").textContent = entry.label;
  const body = document.getElementById("profDrawerBody");
  body.innerHTML = "";
  const evidences = rec?.evidences || [];
  if (!evidences.length) {
    body.innerHTML = `<p style="color:var(--text-3);font-size:12px">Aucun historique pour cette compétence — elle sera tracée dès que l'agent détectera une utilisation confirmée.</p>`;
    return;
  }
  // Most recent first
  for (const ev of [...evidences].reverse()) {
    const card = document.createElement("div");
    card.className = "profile-evidence";
    card.innerHTML = `
      <span class="dev">${ev.device_slug} · ${ev.symptom}</span>
      <span class="sum">${ev.action_summary}</span>
      <span class="date">${ev.date}</span>`;
    body.appendChild(card);
  }
}

function wireDrawerClose() {
  document.getElementById("profDrawerClose").addEventListener("click", () => {
    document.getElementById("profDrawer").classList.add("hidden");
  });
}

export async function initProfileSection() {
  try {
    await ensurePartial();
    _state = await fetchJSON("/profile");
  } catch (err) {
    console.error("initProfileSection:", err);
    return;
  }
  renderHead();
  renderTools();
  renderSkills();
  renderPrefs();
  wireDrawerClose();
}
```

- [ ] **Step 11.4: Wire dispatch in `web/js/main.js`**

Find where other sections dispatch (near `initMemoryBank` or `initHome`). Add:

```javascript
import { initProfileSection } from "./profile.js";

// inside the existing section dispatch switch:
if (section === "profile") initProfileSection();
```

- [ ] **Step 11.5: Update `web/js/router.js` `navigate()`**

Find the hide/show block (around line 161):

```javascript
document.getElementById("memoryBank").classList.toggle("hidden", section !== "memory-bank");
```

Add below it:

```javascript
document.getElementById("profileSection").classList.toggle("hidden", section !== "profile");
```

- [ ] **Step 11.6: Manual browser verification**

1. Restart the backend: `make run`
2. Open `http://localhost:8000/#profile`. Section renders with "—" avatar, "Sans nom" title, empty tool chips, 4 columns of skills ("Non pratiquées" lists every skill), "auto" + "fr" selected in prefs.
3. Click a tool chip → it turns emerald + filled dot, reloads state.
4. Change verbosity from `auto` → `normal` → back to `auto`. Persists (reload `/#profile` to confirm).
5. Click a skill in "Non pratiquées" → drawer slides in with the "no history" copy.
6. Click the close button → drawer closes.

**IMPORTANT:** do not commit until Alexis gives an in-browser visual OK. Render-affecting work requires explicit visual verification per project rules.

- [ ] **Step 11.7: Commit once Alexis confirms visually**

```bash
git add -- web/index.html web/styles/profile.css web/js/profile.js \
           web/js/main.js web/js/router.js
git commit -m "$(cat <<'EOF'
feat(web): technician profile section — read state, toggles, drawer

Replaces the stub with a real section driven by GET /profile. Tool
chips toggle the bitmap via PUT /profile/tools. Preference buttons toggle
verbosity / language via PUT /profile/preferences. Skills render in 4
status columns (mastered / practiced / learning / unlearned); clicking a
skill opens a glass drawer listing evidences newest-first. Identity edit
modal lands in the next commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- web/index.html web/styles/profile.css web/js/profile.js \
      web/js/main.js web/js/router.js
```

---

## Task 12 — Frontend identity modal + level override

**Files:**
- Modify: `web/js/profile.js` (handler + render)

The identity modal markup is already part of `web/profil.html` (added in Task 11.1b). Task 12 plugs the Edit button into that modal and writes `PUT /profile/identity`.

- [ ] **Step 12.1: (no HTML change)**

Skipped — the modal markup shipped with the partial in Step 11.1b.

- [ ] **Step 12.2: Extend `web/js/profile.js` with the modal handler**

Append to `profile.js`:

```javascript
function openIdentityModal() {
  const modal = document.getElementById("profIdentityModal");
  const form = document.getElementById("profIdentityForm");
  const id = _state.profile.identity;
  form.name.value = id.name;
  form.avatar.value = id.avatar;
  form.years_experience.value = id.years_experience;
  form.specialties.value = id.specialties.join(", ");
  form.level_override.value = id.level_override || "";
  modal.classList.remove("hidden");
}

function closeIdentityModal() {
  document.getElementById("profIdentityModal").classList.add("hidden");
}

async function submitIdentity(evt) {
  evt.preventDefault();
  const form = evt.target;
  const payload = {
    name: form.name.value.trim(),
    avatar: form.avatar.value.trim(),
    years_experience: parseInt(form.years_experience.value || "0", 10),
    specialties: form.specialties.value
      .split(",")
      .map(s => s.trim())
      .filter(Boolean),
    level_override: form.level_override.value || null,
  };
  const fresh = await fetchJSON("/profile/identity", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  _state = fresh;
  renderHead();
  closeIdentityModal();
}

function wireIdentityModal() {
  document.getElementById("profEditIdentityBtn")
    .addEventListener("click", openIdentityModal);
  document.getElementById("profIdentityModal")
    .querySelectorAll("[data-dismiss]")
    .forEach(el => el.addEventListener("click", closeIdentityModal));
  document.getElementById("profIdentityForm")
    .addEventListener("submit", submitIdentity);
}
```

Call `wireIdentityModal()` from `initProfileSection()` once, just before or after `wireDrawerClose()`:

```javascript
wireDrawerClose();
wireIdentityModal();
```

- [ ] **Step 12.3: Manual browser verification**

1. Navigate to `/#profile`, click "Éditer".
2. Modal opens with current values prefilled (empty on first run).
3. Fill: name=`Alexis`, avatar=`AC`, years=`5`, specialties=`apple, consoles`, level_override=`(dérivé…)`.
4. Submit. Modal closes. Header updates: avatar shows "AC", title shows "Alexis", tag shows "BEGINNER", years shows "5 ans d'XP", specs show "apple · consoles".
5. Reopen modal — fields rehydrated.
6. Try `level_override = expert` — tag changes to "EXPERT" on save.
7. Try 422 path: set `level_override = wizard` by temporarily editing the `<option value="">` to `wizard` in DevTools, submit. Verify the PUT returns 422 and the modal stays open.

Wait for Alexis's visual OK.

- [ ] **Step 12.4: Commit once Alexis confirms**

```bash
git add -- web/js/profile.js
git commit -m "$(cat <<'EOF'
feat(web): wire identity edit modal on the profile section

Edit button opens the modal declared in profil.html, prefilled from
current state, and writes PUT /profile/identity. Specialties are parsed
as a comma-separated list. level_override drop-down lets the tech force
a level other than the one derived from mastered skills.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)" -- web/js/profile.js
```

---

## Task 13 — Bootstrap a realistic profile for demo / ensure `make test` is green

**Files:**
- Run `make test`
- Optionally seed `memory/_profile/technician.json` for the demo session.

- [ ] **Step 13.1: Run the full test suite**

Run: `make test`
Expected: everything green. Fix anything red before shipping — most likely issues: forgotten import, test pollution from the `get_settings` cache, path-to-`memory_root` mismatch.

- [ ] **Step 13.2: (Optional) Seed a demo profile**

To avoid a completely empty UI on demo, open `/#profile` and fill:
- name=`Alexis`, years=`5`, specialties=`apple, consoles`.
- Check: soldering_iron, hot_air, microscope, multimeter, preheater, bench_psu, uv_lamp.
- preferences: verbosity=`auto`, language=`fr`.

Then manually trigger skill bumps via a short diagnostic session in `DIAGNOSTIC_MODE=direct` to show that `profile_track_skill` actually wires (confirm a "reflow" action twice to move `reflow_bga` into `learning`).

The seed is **not** committed — `memory/_profile/` is gitignored.

- [ ] **Step 13.3: Final summary commit (optional, only if any fixes landed)**

No commit if `make test` was already green.

---

## Self-Review

Run after completing the plan; fix inline and move on.

**Spec coverage:** Walking through the spec:
- §2 Modèle de données → Tasks 1, 2, 4 ✓
- §2.3 Dérivations → Task 3 ✓
- §3 Store → Task 4 ✓
- §4 HTTP surface → Task 6 ✓
- §5 Tools → Tasks 7, 8 ✓
- §6 Injection runtime → Tasks 9, 10 ✓
- §7 Frontend → Tasks 11, 12 ✓
- §8 Flow bout-en-bout → covered by Task 10 manual + Task 13 demo ✓
- §9 Layout fichiers → mirrored in File Structure section ✓
- §10 Tests → every file in §9 has a mirror test file in the plan ✓

**Placeholder scan:** No TBD/TODO/"add appropriate validation" patterns. Every code step shows full code. Two "(conditional)" steps (4.2b, 13.2) are explicitly gated, not placeholders.

**Type consistency:** `TechnicianProfile`, `SkillRecord`, `SkillEvidence`, `skill_status`, `global_level`, `effective_verbosity`, `skills_by_status`, `bump_skill`, `load_profile`, `save_profile`, `render_technician_block`, `profile_get`, `profile_check_skills`, `profile_track_skill` are defined once and reused verbatim. `MASTERY_THRESHOLD`, `PRACTICED_THRESHOLD`, `LEARNING_THRESHOLD`, `SKILL_EVIDENCES_CAP` — one site of truth in `catalog.py`, imported by tests and derive/store.

**Parallel-agent safety:** every `git commit` uses `-- <paths>` form per CLAUDE.md rule.
