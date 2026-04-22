"""Transform on-disk pack files (V2 schema) into the graph payload
expected by web/index.html (frontend design v3).

Carries component / net / symptom nodes and their relations from
knowledge_graph verbatim (symptom IDs use the Cartographe's `sym:<slug>`
convention), enriches component nodes with dictionary / registry metadata,
and back-fills any rule-only symptom that the Cartographe missed so no
rule goes orphan in the UI. `action` nodes will land once the diagnostic
agent starts persisting recommended actions.
"""

from __future__ import annotations

import re
from typing import Any


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "unknown"


def pack_to_graph_payload(
    *,
    registry: dict[str, Any],
    knowledge_graph: dict[str, Any],
    rules: dict[str, Any],
    dictionary: dict[str, Any],
) -> dict[str, Any]:
    """Merge the four pack files into a single {nodes, edges} payload.

    Returned shape matches what web/index.html's D3 layer expects:
      node: {id, type, label, description, confidence, meta}
      edge: {source, target, relation, label, weight}
    """
    kg_nodes = knowledge_graph.get("nodes", [])
    kg_edges = knowledge_graph.get("edges", [])
    dict_by_name = {e["canonical_name"]: e for e in dictionary.get("entries", [])}
    reg_components = {c["canonical_name"]: c for c in registry.get("components", [])}
    reg_signals = {s["canonical_name"]: s for s in registry.get("signals", [])}

    if not kg_nodes and not rules.get("rules"):
        return {"nodes": [], "edges": []}

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    # 1. Carry component / net / symptom nodes from the knowledge_graph.
    for n in kg_nodes:
        kind = n.get("kind")
        if kind not in ("component", "net", "symptom"):
            continue
        label = n.get("label", "")
        meta: dict[str, Any] = {}
        description = ""
        confidence = 0.55

        if kind == "component":
            reg = reg_components.get(label)
            dct = dict_by_name.get(label)
            if dct:
                if dct.get("package"):
                    meta["package"] = dct["package"]
                if dct.get("role"):
                    meta["role"] = dct["role"]
            description = (reg or {}).get("description") or (dct or {}).get("notes") or ""
            confidence = 0.80 if reg else 0.55
        elif kind == "net":
            reg = reg_signals.get(label)
            if reg and reg.get("nominal_voltage") is not None:
                meta["nominal"] = f"{reg['nominal_voltage']} V"
            description = (reg or {}).get("description", "")
            confidence = 0.80 if reg else 0.55
        else:  # symptom
            description = ""
            confidence = 0.70

        nodes.append(
            {
                "id": n["id"],
                "type": kind,
                "label": label,
                "description": description,
                "confidence": confidence,
                "meta": meta,
            }
        )

    # 2. Back-fill symptom nodes that rules mention but the Cartographe didn't
    #    emit. Keyed by label so we don't duplicate a Cartographe node. New IDs
    #    use the same `sym:<slug>` convention the Cartographe uses so everything
    #    shares one address space.
    symptom_id_by_label = {n["label"]: n["id"] for n in nodes if n["type"] == "symptom"}
    for rule in rules.get("rules", []):
        for symptom_text in rule.get("symptoms", []):
            if symptom_text in symptom_id_by_label:
                continue
            sid = f"sym:{_slug(symptom_text)}"
            # Ensure uniqueness when two different labels slugify to the same id.
            if any(n["id"] == sid for n in nodes):
                sid = f"sym:{_slug(symptom_text)}-{len(nodes)}"
            symptom_id_by_label[symptom_text] = sid
            nodes.append(
                {
                    "id": sid,
                    "type": "symptom",
                    "label": symptom_text,
                    "description": "",
                    "confidence": rule.get("confidence", 0.6),
                    "meta": {},
                }
            )

    # 3. Keep only edges whose endpoints exist. Drop orphans — D3's forceLink
    #    silently mangles node references when a source/target can't resolve,
    #    which is what broke the UI for rich packs (#bug:orphan-edges).
    known_node_ids = {n["id"] for n in nodes}
    for e in kg_edges:
        if e["source_id"] not in known_node_ids or e["target_id"] not in known_node_ids:
            continue
        edges.append(
            {
                "source": e["source_id"],
                "target": e["target_id"],
                "relation": e["relation"],
                "label": e.get("relation", ""),
                "weight": 1.0,
            }
        )

    # 4. Synthesize `causes` edges from rules.likely_causes. These are in
    #    addition to any causes edge the Cartographe already drew — duplicates
    #    are kept because they carry different weights (per-rule probability
    #    vs. the Cartographe's uniform 1.0).
    component_id_by_label = {n["label"]: n["id"] for n in nodes if n["type"] == "component"}
    for rule in rules.get("rules", []):
        for symptom_text in rule.get("symptoms", []):
            sid = symptom_id_by_label.get(symptom_text)
            if sid is None:
                continue
            for cause in rule.get("likely_causes", []):
                cid = component_id_by_label.get(cause["refdes"])
                if cid is None:
                    continue  # refdes not in registry → skip (anti-hallucination)
                edges.append(
                    {
                        "source": cid,
                        "target": sid,
                        "relation": "causes",
                        "label": cause.get("mechanism", "causes"),
                        "weight": float(cause.get("probability", 0.5)),
                    }
                )

    return {"nodes": nodes, "edges": edges}
