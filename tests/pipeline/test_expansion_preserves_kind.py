"""Task 10 — device_kind threading + preservation across expansion.

Two checks:
  1. `RepairRequest` accepts a `device_kind` prior (free `str | None`).
  2. `expand_pack` carries the prior `device_kind` even when the re-run
     Registry comes back with a null `device_kind` — mirrors the existing
     taxonomy-preservation test (`test_expansion.py`), but asserts on the
     registry the Clinicien actually sees (the carry happens on the in-memory
     `new_registry.taxonomy`, which the Clinicien consumes).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from api import config as config_mod
from api.pipeline import expansion
from api.pipeline.models import RepairRequest
from api.pipeline.prompts import device_kind_constraint
from api.pipeline.schemas import (
    Cause,
    DeviceTaxonomy,
    Registry,
    RegistryComponent,
    RegistrySignal,
    Rule,
    RulesSet,
)


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch):
    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    yield
    monkeypatch.setattr(config_mod, "_settings", None)


def test_repair_request_accepts_device_kind():
    r = RepairRequest(device_label="MSI V311_11", symptom="dead board", device_kind="gpu_card")
    assert r.device_kind == "gpu_card"


def test_repair_request_device_kind_defaults_none():
    r = RepairRequest(device_label="MSI V311_11", symptom="dead board")
    assert r.device_kind is None


def test_repair_request_rejects_invalid_device_kind():
    """device_kind is the closed _DeviceKind enum — an off-enum string (e.g.
    "laptop" instead of "laptop_logic_board") must fail validation at the HTTP
    boundary rather than flowing into reconcile_kind / the registry taxonomy."""
    with pytest.raises(ValidationError):
        RepairRequest(device_label="MSI V311_11", symptom="dead board", device_kind="laptop")


def _seed_pack_with_kind(tmp_path: Path, slug: str, kind: str) -> Path:
    pack = tmp_path / slug
    pack.mkdir()
    (pack / "raw_research_dump.md").write_text(
        "# Research Dump — test device\n\n## Device overview\ntest\n",
        encoding="utf-8",
    )
    seed_registry = Registry(
        device_label="Test GPU",
        taxonomy=DeviceTaxonomy(brand="TestCo", model="Thing", device_kind=kind),
        components=[RegistryComponent(canonical_name="U1", kind="IC")],
        signals=[RegistrySignal(canonical_name="VCC", kind="POWER_RAIL")],
    )
    # pre-T8 layout — expand_pack's migrate_pack_if_needed promotes this flat
    # root registry.json to baseline/ on first call.
    (pack / "registry.json").write_text(
        seed_registry.model_dump_json(indent=2), encoding="utf-8"
    )
    seed_rules = RulesSet(rules=[
        Rule(
            id="R-EXISTING-001",
            symptoms=["prior symptom"],
            likely_causes=[Cause(refdes="U1", probability=0.5, mechanism="short")],
            confidence=0.6,
        ),
    ])
    (pack / "rules.json").write_text(
        seed_rules.model_dump_json(indent=2), encoding="utf-8"
    )
    return pack


async def test_expand_pack_preserves_device_kind_when_regenerate_returns_null(
    tmp_path, monkeypatch
):
    """The re-run Registry returns a null device_kind (a single-symptom focus
    can starve the classifier); the prior kind must be carried onto the
    registry the Clinicien sees.
    """
    _seed_pack_with_kind(tmp_path, "test-gpu", "gpu_card")

    async def fake_scout(**_kwargs):
        return "\n\n- narrow scope chunk\n"

    null_kind_registry = Registry(
        device_label="Test GPU",
        # Re-run lost the resolved class — device_kind back to None.
        taxonomy=DeviceTaxonomy(brand="TestCo", model="Thing", device_kind=None),
        components=[RegistryComponent(canonical_name="U1", kind="IC")],
        signals=[RegistrySignal(canonical_name="VCC", kind="POWER_RAIL")],
    )
    same_rules = RulesSet(rules=[
        Rule(
            id="R-EXISTING-001",
            symptoms=["prior symptom"],
            likely_causes=[Cause(refdes="U1", probability=0.5, mechanism="short")],
            confidence=0.6,
        ),
    ])

    clinicien_mock = AsyncMock(return_value=same_rules)

    monkeypatch.setattr(expansion, "_run_targeted_scout", fake_scout)
    with patch(
        "api.pipeline.expansion.run_registry_builder",
        new=AsyncMock(return_value=null_kind_registry),
    ) as reg_mock, patch(
        "api.pipeline.expansion._run_clinicien_on_full_dump",
        new=clinicien_mock,
    ):
        await expansion.expand_pack(
            device_slug="test-gpu",
            focus_symptoms=["narrow thing"],
            client=object(),
            memory_root=tmp_path,
        )

    # The prior device_kind is threaded as a constraint into the Registry re-run.
    assert reg_mock.await_args.kwargs["device_kind"] == "gpu_card"

    # The carry lands on the registry the Clinicien consumes, even though the
    # re-run handed back a null device_kind.
    passed_registry = clinicien_mock.await_args.kwargs["registry"]
    assert passed_registry.taxonomy.device_kind == "gpu_card"


def _seed_pack_kind_only(tmp_path: Path, slug: str, kind: str) -> Registry:
    """Seed a prior pack whose taxonomy has all four named fields null but a
    resolved device_kind. Returns the seeded prior Registry for mutation checks.
    """
    pack = tmp_path / slug
    pack.mkdir()
    (pack / "raw_research_dump.md").write_text(
        "# Research Dump — test device\n\n## Device overview\ntest\n",
        encoding="utf-8",
    )
    seed_registry = Registry(
        device_label="Test GPU",
        # All four named fields null — only device_kind is resolved.
        taxonomy=DeviceTaxonomy(
            brand=None, model=None, version=None, form_factor=None,
            device_kind=kind,
        ),
        components=[RegistryComponent(canonical_name="U1", kind="IC")],
        signals=[RegistrySignal(canonical_name="VCC", kind="POWER_RAIL")],
    )
    # pre-T8 layout — expand_pack's migrate_pack_if_needed promotes this flat
    # root registry.json to baseline/ on first call.
    (pack / "registry.json").write_text(
        seed_registry.model_dump_json(indent=2), encoding="utf-8"
    )
    seed_rules = RulesSet(rules=[
        Rule(
            id="R-EXISTING-001",
            symptoms=["prior symptom"],
            likely_causes=[Cause(refdes="U1", probability=0.5, mechanism="short")],
            confidence=0.6,
        ),
    ])
    (pack / "rules.json").write_text(
        seed_rules.model_dump_json(indent=2), encoding="utf-8"
    )
    return seed_registry


async def test_expand_pack_full_replace_carries_kind_without_mutating_prior(
    tmp_path, monkeypatch
):
    """When the prior taxonomy has all four named fields null but a resolved
    device_kind, and the re-run comes back all-null, the full-replace branch
    fires AND the carry preserves device_kind. The model_copy() guard ensures
    the prior object is never mutated by the in-place carry assignment.
    """
    prior_seed = _seed_pack_kind_only(tmp_path, "test-gpu", "gpu_card")

    async def fake_scout(**_kwargs):
        return "\n\n- narrow scope chunk\n"

    # Re-run regressed to fully-null taxonomy (including device_kind) — this
    # triggers the all-named-fields-null full-replace branch.
    all_null_registry = Registry(
        device_label="Test GPU",
        taxonomy=DeviceTaxonomy(
            brand=None, model=None, version=None, form_factor=None,
            device_kind=None,
        ),
        components=[RegistryComponent(canonical_name="U1", kind="IC")],
        signals=[RegistrySignal(canonical_name="VCC", kind="POWER_RAIL")],
    )
    same_rules = RulesSet(rules=[
        Rule(
            id="R-EXISTING-001",
            symptoms=["prior symptom"],
            likely_causes=[Cause(refdes="U1", probability=0.5, mechanism="short")],
            confidence=0.6,
        ),
    ])

    clinicien_mock = AsyncMock(return_value=same_rules)

    monkeypatch.setattr(expansion, "_run_targeted_scout", fake_scout)
    with patch(
        "api.pipeline.expansion.run_registry_builder",
        new=AsyncMock(return_value=all_null_registry),
    ), patch(
        "api.pipeline.expansion._run_clinicien_on_full_dump",
        new=clinicien_mock,
    ):
        await expansion.expand_pack(
            device_slug="test-gpu",
            focus_symptoms=["narrow thing"],
            client=object(),
            memory_root=tmp_path,
        )

    # The full-replace branch fired (all named fields null) and the carry
    # restored device_kind onto the registry the Clinicien consumes.
    passed_registry = clinicien_mock.await_args.kwargs["registry"]
    assert passed_registry.taxonomy.device_kind == "gpu_card"

    # The in-memory prior_seed object we built is untouched — the model_copy()
    # broke the alias, so the in-place carry never mutated the prior taxonomy.
    assert prior_seed.taxonomy.device_kind == "gpu_card"


class _FakeTextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _FakeResponse:
    stop_reason = "end_turn"

    def __init__(self, text: str):
        self.content = [_FakeTextBlock(text)]


class _CapturingClient:
    """Minimal AsyncAnthropic stand-in: records the messages of each
    create() call and returns a one-text-block response (no pause_turn)."""

    def __init__(self):
        self.calls: list[list[dict]] = []
        self.messages = self  # client.messages.create(...) → self.create(...)

    async def create(self, *, messages, **_kwargs):
        self.calls.append(messages)
        return _FakeResponse("- a research bullet\n")


async def test_targeted_scout_injects_kind_constraint_when_prior_kind():
    """The targeted Scout's model prompt must carry the authoritative class
    constraint (same helper as the main Scout) when a prior device_kind exists.
    """
    client = _CapturingClient()
    await expansion._run_targeted_scout(
        client=client,
        model="claude-sonnet-x",
        device_label="MSI V311_11",
        focus_symptoms=["no display"],
        focus_refdes=["U1"],
        device_kind="gpu_card",
    )
    sent_prompt = client.calls[0][0]["content"]
    assert device_kind_constraint("gpu_card") in sent_prompt
    assert "device_kind=gpu_card" in sent_prompt


async def test_targeted_scout_prompt_unchanged_when_no_prior_kind():
    """device_kind_constraint(None) is '' — the targeted Scout prompt must be
    byte-identical to the no-kind case (no stray constraint text)."""
    client_none = _CapturingClient()
    await expansion._run_targeted_scout(
        client=client_none,
        model="claude-sonnet-x",
        device_label="MSI V311_11",
        focus_symptoms=["no display"],
        focus_refdes=["U1"],
        device_kind=None,
    )
    client_default = _CapturingClient()
    await expansion._run_targeted_scout(
        client=client_default,
        model="claude-sonnet-x",
        device_label="MSI V311_11",
        focus_symptoms=["no display"],
        focus_refdes=["U1"],
    )
    assert client_none.calls[0][0]["content"] == client_default.calls[0][0]["content"]
    assert "DEVICE CLASS (authoritative" not in client_none.calls[0][0]["content"]
