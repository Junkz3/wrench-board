"""Tests for api.pipeline.graph_transform."""

from __future__ import annotations

import json
from pathlib import Path

from api.pipeline.graph_transform import pack_to_graph_payload

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "demo-pack"


def _load(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_text())


def test_pack_to_graph_returns_expected_shape():
    payload = pack_to_graph_payload(
        registry=_load("registry.json"),
        knowledge_graph=_load("knowledge_graph.json"),
        rules=_load("rules.json"),
        dictionary=_load("dictionary.json"),
    )

    assert set(payload.keys()) == {"nodes", "edges", "subsystems"}

    # Every knowledge_graph node carried over, enriched from dictionary + registry.
    node_ids = {n["id"] for n in payload["nodes"]}
    assert {"cmp_U7", "cmp_C29", "net_3V3"} <= node_ids

    # Symptom nodes are synthesized from rules.symptoms.
    symptom_nodes = [n for n in payload["nodes"] if n["type"] == "symptom"]
    assert len(symptom_nodes) == 2  # "3V3 rail dead" + "device doesn't boot"
    assert all(n["confidence"] >= 0.0 and n["confidence"] <= 1.0 for n in symptom_nodes)

    # Causes edges are synthesized: likely_causes[i].refdes → symptom.
    causes_edges = [e for e in payload["edges"] if e["relation"] == "causes"]
    assert len(causes_edges) >= 2  # C29 + U7 causing each of the 2 symptoms

    # Component nodes carry dictionary metadata under "meta".
    u7 = next(n for n in payload["nodes"] if n["id"] == "cmp_U7")
    assert u7["type"] == "component"
    assert u7["meta"]["package"] == "QFN-24"
    assert u7["label"] == "U7"


def test_pack_synthesizes_action_nodes_from_rules():
    payload = pack_to_graph_payload(
        registry=_load("registry.json"),
        knowledge_graph=_load("knowledge_graph.json"),
        rules=_load("rules.json"),
        dictionary=_load("dictionary.json"),
    )
    action_nodes = [n for n in payload["nodes"] if n["type"] == "action"]
    # The demo-pack has 1 rule → we expect 1 action.
    assert len(action_nodes) == 1
    # Every action carries the originating rule_id so the frontend can trace back.
    assert all("rule_id" in n["meta"] for n in action_nodes)
    # Every action confidence is bounded.
    assert all(0.0 <= n["confidence"] <= 1.0 for n in action_nodes)

    # `resolves` edges wire the action to each symptom of the rule.
    resolves_edges = [e for e in payload["edges"] if e["relation"] == "resolves"]
    assert len(resolves_edges) == 2  # demo-pack rule has 2 symptoms

    # Every resolves edge source is an action node we synthesized.
    action_ids = {n["id"] for n in action_nodes}
    assert all(e["source"] in action_ids for e in resolves_edges)


def test_action_label_verb_derived_from_mechanism():
    """Keyword heuristic: the verb is picked from the top cause's mechanism."""
    from api.pipeline.graph_transform import _derive_action_label

    assert _derive_action_label({
        "id": "r1",
        "likely_causes": [
            {"refdes": "U2", "probability": 0.8, "mechanism": "Replace due to die failure"}
        ],
    })[0] == "Replace U2"

    assert _derive_action_label({
        "id": "r2",
        "likely_causes": [
            {"refdes": "C1750", "probability": 0.7, "mechanism": "leaky MLCC shorting PP_VDD_MAIN to GND"}
        ],
    })[0] == "Lift C1750"

    assert _derive_action_label({
        "id": "r3",
        "likely_causes": [
            {"refdes": "flex", "probability": 0.8, "mechanism": "torn flex — jumper required"}
        ],
    })[0] == "Jumper flex"

    assert _derive_action_label({
        "id": "r4",
        "likely_causes": [
            {"refdes": "U3101", "probability": 0.7, "mechanism": "cold joint — reflow restores"}
        ],
    })[0] == "Reflow U3101"

    # Fallback verb when no keyword matches.
    assert _derive_action_label({
        "id": "r5",
        "likely_causes": [{"refdes": "X7", "probability": 0.5, "mechanism": "weird issue"}],
    })[0] == "Repair X7"

    # Picks the highest-probability cause, not the first.
    top = _derive_action_label({
        "id": "r6",
        "likely_causes": [
            {"refdes": "LOW", "probability": 0.1, "mechanism": "edge case"},
            {"refdes": "HIGH", "probability": 0.6, "mechanism": "Replace due to die damage"},
        ],
    })[0]
    assert top == "Replace HIGH"


def test_every_node_has_subsystem_field():
    payload = pack_to_graph_payload(
        registry=_load("registry.json"),
        knowledge_graph=_load("knowledge_graph.json"),
        rules=_load("rules.json"),
        dictionary=_load("dictionary.json"),
    )
    assert all("subsystem" in n for n in payload["nodes"])
    assert all(isinstance(n["subsystem"], str) and n["subsystem"] for n in payload["nodes"])


def test_payload_includes_subsystems_index():
    payload = pack_to_graph_payload(
        registry=_load("registry.json"),
        knowledge_graph=_load("knowledge_graph.json"),
        rules=_load("rules.json"),
        dictionary=_load("dictionary.json"),
    )
    assert "subsystems" in payload
    subs = payload["subsystems"]
    assert isinstance(subs, list)
    assert all({"key", "label", "count"} <= set(s.keys()) for s in subs)
    # Every listed subsystem has at least one node (count=0 entries are dropped).
    assert all(s["count"] >= 1 for s in subs)
    # 'unknown' is always last when present.
    unknown = [i for i, s in enumerate(subs) if s["key"] == "unknown"]
    if unknown:
        assert unknown[0] == len(subs) - 1
    # Fixed French labels.
    labels = {s["key"]: s["label"] for s in subs}
    expected_fr = {"power": "ALIMENTATION", "charge": "CHARGE", "display": "DISPLAY",
                   "usb": "USB", "audio": "AUDIO", "cpu-mem": "CPU / MÉMOIRE",
                   "io": "E/S", "rf": "RF / RADIO", "unknown": "AUTRES"}
    for key, label in labels.items():
        assert label == expected_fr[key]


def test_subsystems_sorted_by_count_descending_unknown_last():
    """When multiple subsystems are present, they're ordered by count desc.
    Unknown is always last regardless of its count."""
    from api.pipeline.graph_transform import pack_to_graph_payload
    reg = {"schema_version": "1.0", "device_label": "synth",
           "components": [], "signals": [
               {"canonical_name": "VBAT"},
               {"canonical_name": "VCC"},
               {"canonical_name": "HDMI_X"},
           ]}
    kg = {"schema_version": "1.0", "nodes": [
        {"id": "n1", "kind": "net", "label": "VBAT"},
        {"id": "n2", "kind": "net", "label": "VCC"},
        {"id": "n3", "kind": "net", "label": "HDMI_X"},
        {"id": "n4", "kind": "net", "label": "XYZ_MISC"},
    ], "edges": []}
    payload = pack_to_graph_payload(
        registry=reg, knowledge_graph=kg,
        rules={"schema_version": "1.0", "rules": []},
        dictionary={"schema_version": "1.0", "entries": []},
    )
    keys = [s["key"] for s in payload["subsystems"]]
    # power (2) > display (1) > unknown (1, but always last)
    assert keys == ["power", "display", "unknown"]


def test_empty_pack_still_returns_subsystems_field():
    """Shape contract: subsystems is always present, empty list on empty pack."""
    payload = pack_to_graph_payload(
        registry={"schema_version": "1.0", "device_label": "empty",
                  "components": [], "signals": []},
        knowledge_graph={"schema_version": "1.0", "nodes": [], "edges": []},
        rules={"schema_version": "1.0", "rules": []},
        dictionary={"schema_version": "1.0", "entries": []},
    )
    assert payload == {"nodes": [], "edges": [], "subsystems": []}
