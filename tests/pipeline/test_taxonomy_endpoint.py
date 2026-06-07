"""Tests for GET /pipeline/taxonomy — groups memory/*/registry.json by
brand > model > version, with an uncategorized bucket for packs whose
taxonomy is null (hard rule #4 = null beats guessing).
"""

from __future__ import annotations

import json
from pathlib import Path


def _write_pack(
    root: Path,
    slug: str,
    *,
    device_label: str,
    taxonomy: dict | None,
    complete: bool = True,
) -> None:
    pack_dir = root / slug
    pack_dir.mkdir(parents=True, exist_ok=True)
    registry = {
        "schema_version": "1.0",
        "device_label": device_label,
        "components": [],
        "signals": [],
    }
    if taxonomy is not None:
        registry["taxonomy"] = taxonomy
    (pack_dir / "registry.json").write_text(json.dumps(registry))
    if complete:
        for name in ("knowledge_graph.json", "rules.json", "dictionary.json"):
            (pack_dir / name).write_text('{"schema_version":"1.0"}')


def test_taxonomy_empty_when_no_packs(memory_root, client):
    res = client.get("/pipeline/taxonomy")
    assert res.status_code == 200
    assert res.json() == {"brands": {}, "uncategorized": []}


def test_taxonomy_groups_by_brand_and_model(memory_root, client):
    _write_pack(
        memory_root,
        "mnt-reform-motherboard",
        device_label="MNT Reform motherboard",
        taxonomy={
            "brand": "MNT",
            "model": "Reform",
            "version": "Rev 2.0",
            "form_factor": "motherboard",
        },
    )
    _write_pack(
        memory_root,
        "rpi-4b",
        device_label="Raspberry Pi 4B",
        taxonomy={
            "brand": "Raspberry Pi",
            "model": "Model B",
            "version": "4",
            "form_factor": "mainboard",
        },
    )

    tree = client.get("/pipeline/taxonomy").json()
    assert set(tree["brands"].keys()) == {"MNT", "Raspberry Pi"}
    assert tree["brands"]["MNT"]["Reform"][0]["device_slug"] == "mnt-reform-motherboard"
    assert tree["brands"]["MNT"]["Reform"][0]["version"] == "Rev 2.0"
    assert tree["brands"]["MNT"]["Reform"][0]["form_factor"] == "motherboard"
    assert tree["uncategorized"] == []


def test_taxonomy_missing_brand_falls_to_uncategorized(memory_root, client):
    _write_pack(
        memory_root,
        "demo-pi",
        device_label="Demo Pi",
        taxonomy=None,  # older packs pre-taxonomy
    )
    _write_pack(
        memory_root,
        "weird-device",
        device_label="Weird Device",
        taxonomy={"brand": None, "model": "Unlabelled", "version": None, "form_factor": None},
    )

    tree = client.get("/pipeline/taxonomy").json()
    slugs = {e["device_slug"] for e in tree["uncategorized"]}
    assert slugs == {"demo-pi", "weird-device"}
    assert tree["brands"] == {}


def test_taxonomy_reports_completeness(memory_root, client):
    _write_pack(
        memory_root,
        "partial-pack",
        device_label="Partial Pack",
        taxonomy={"brand": "Acme", "model": "Widget", "version": None, "form_factor": None},
        complete=False,
    )
    tree = client.get("/pipeline/taxonomy").json()
    entry = tree["brands"]["Acme"]["Widget"][0]
    assert entry["complete"] is False
