# SPDX-License-Identifier: Apache-2.0
"""Schema-level tests for the Registry refdes_candidates extension.

Behavioural tests for the actual run_registry_builder enrichment live
in test_registry_builder.py once that exists; here we only assert that
the Pydantic shape accepts both the legacy (no candidates) and enriched
(with candidates) forms without breaking either.
"""

from __future__ import annotations

import pytest

from api.pipeline.schemas import (
    RefdesCandidate,
    Registry,
    RegistryComponent,
)


def test_legacy_registry_component_has_null_candidates() -> None:
    """A component constructed without refdes_candidates is unaffected."""
    comp = RegistryComponent(canonical_name="U14", kind="ic")
    assert comp.refdes_candidates is None


def test_legacy_registry_json_roundtrip_with_no_candidates() -> None:
    """Existing on-disk registry shape (no `refdes_candidates` key) loads."""
    payload = {
        "schema_version": "1.0",
        "device_label": "demo",
        "components": [
            {
                "canonical_name": "LPC controller",
                "aliases": ["LPC"],
                "kind": "ic",
                "description": "MCU sequencer",
            }
        ],
        "signals": [],
    }
    reg = Registry.model_validate(payload)
    assert reg.components[0].refdes_candidates is None
    # And serializes back: refdes_candidates: null is acceptable in JSON shape.
    serialized = reg.model_dump()
    assert serialized["components"][0]["refdes_candidates"] is None


def test_enriched_registry_accepts_refdes_candidates() -> None:
    payload = {
        "schema_version": "1.0",
        "device_label": "demo",
        "components": [
            {
                "canonical_name": "LPC controller",
                "aliases": ["LPC"],
                "kind": "ic",
                "description": "MCU sequencer",
                "refdes_candidates": [
                    {
                        "refdes": "U14",
                        "confidence": 0.92,
                        "evidence": (
                            "Forum thread cites the LPC as U14 in the rev 2.0 "
                            "schematic; matches the Reform community wiki."
                        ),
                    },
                    {
                        "refdes": "U7",
                        "confidence": 0.4,
                        "evidence": "weaker — alternative LPC reference seen on rev 1.0",
                    },
                ],
            }
        ],
        "signals": [],
    }
    reg = Registry.model_validate(payload)
    cands = reg.components[0].refdes_candidates
    assert cands is not None and len(cands) == 2
    assert cands[0].refdes == "U14"
    assert 0.0 <= cands[0].confidence <= 1.0


def test_refdes_candidate_rejects_empty_evidence() -> None:
    with pytest.raises(ValueError):
        RefdesCandidate(refdes="U14", confidence=0.9, evidence="")


def test_refdes_candidate_rejects_out_of_range_confidence() -> None:
    with pytest.raises(ValueError):
        RefdesCandidate(refdes="U14", confidence=1.7, evidence="ok")


