"""Transform on-disk pack files (V2 schema) into the graph payload
expected by web/index.html (frontend design v3).

Carries component / net / symptom nodes and their relations from
knowledge_graph verbatim (symptom IDs use the Cartographe's `sym:<slug>`
convention), enriches component nodes with dictionary / registry metadata,
and back-fills any rule-only symptom that the Cartographe missed so no
rule goes orphan in the UI.

Synthesizes one `action` node per rule — the concrete microsoldering
intervention (Replace / Reflow / Jumper / Lift / Reball / Hunt short)
derived from the rule's highest-probability cause mechanism. Actions
link to the rule's symptoms via `resolves` edges, completing the narrative
Actions → Components → Nets → Symptoms on the frontend.
"""

from __future__ import annotations

import re
from typing import Any

from api.pipeline.subsystem import classify_nodes


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "unknown"


_SUBSYSTEM_LABELS: dict[str, str] = {
    "power":   "POWER",
    "charge":  "CHARGE",
    "display": "DISPLAY",
    "usb":     "USB",
    "audio":   "AUDIO",
    "cpu-mem": "CPU / MEMORY",
    "io":      "I/O",
    "rf":      "RF / RADIO",
    "unknown": "OTHER",
}


def pack_to_graph_payload(
    *,
    registry: dict[str, Any],
    knowledge_graph: dict[str, Any],
    rules: dict[str, Any],
    dictionary: dict[str, Any],
) -> dict[str, Any]:
    """Merge the four pack files into a single {nodes, edges} payload.

    Returned shape matches what web/index.html's D3 layer expects:
      node: {id, type, label, description, confidence, meta, subsystem}
      edge: {source, target, relation, label, weight}
      subsystems: list[{key, label, count}]  # sorted by count desc, unknown last
    """
    kg_nodes = knowledge_graph.get("nodes", [])
    kg_edges = knowledge_graph.get("edges", [])
    dict_by_name = {e["canonical_name"]: e for e in dictionary.get("entries", [])}
    reg_components = {c["canonical_name"]: c for c in registry.get("components", [])}
    reg_signals = {s["canonical_name"]: s for s in registry.get("signals", [])}

    if not kg_nodes and not rules.get("rules"):
        return {"nodes": [], "edges": [], "subsystems": []}

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

    # 5. Synthesize action nodes — one per rule, labelled from the top cause's
    #    mechanism. Actions sit in the leftmost column of the visual narrative
    #    and `resolves` the rule's symptoms. Edges use the `resolves` relation
    #    which the frontend renders as a violet dotted arrow.
    for rule in rules.get("rules", []):
        rule_id = rule.get("id", "")
        if not rule_id:
            continue
        action_id = f"act:{rule_id}"
        label, top_mechanism = _derive_action_label(rule)
        nodes.append(
            {
                "id": action_id,
                "type": "action",
                "label": label,
                "description": top_mechanism,
                "confidence": rule.get("confidence", 0.6),
                "meta": {"rule_id": rule_id},
            }
        )
        for symptom_text in rule.get("symptoms", []):
            sid = symptom_id_by_label.get(symptom_text)
            if sid is None:
                continue
            edges.append(
                {
                    "source": action_id,
                    "target": sid,
                    "relation": "resolves",
                    "label": "resolves",
                    "weight": float(rule.get("confidence", 0.6)),
                }
            )

    # 5b. Collapse duplicate action nodes by label. When two or more actions
    # share a label we keep the first as representative and merge rule_ids
    # onto its meta. Edges from discarded actions are remapped to the
    # representative, then deduped so the frontend sees each resolves edge
    # once. The dedupe key is (source, target, relation) — if two merged
    # rules contribute a resolves edge with the same endpoints but
    # different `weight` (per-rule confidence), the first rule's weight
    # wins. This is intentional: D3's force sim doesn't handle parallel
    # edges and one weight per (action, symptom) is the right granularity.
    by_label: dict[str, list[dict[str, Any]]] = {}
    for n in nodes:
        if n["type"] == "action":
            by_label.setdefault(n["label"], []).append(n)

    id_remap: dict[str, str] = {}
    discarded: set[str] = set()
    for _label, group in by_label.items():
        if len(group) < 2:
            continue
        rep = group[0]
        rep["meta"]["count"] = len(group)
        rep["meta"]["rule_ids"] = [a["meta"]["rule_id"] for a in group]
        for a in group[1:]:
            id_remap[a["id"]] = rep["id"]
            discarded.add(a["id"])

    if discarded:
        nodes = [n for n in nodes if n["id"] not in discarded]
        remapped_edges: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for e in edges:
            new_src = id_remap.get(e["source"], e["source"])
            new_tgt = id_remap.get(e["target"], e["target"])
            key = (new_src, new_tgt, e["relation"])
            if key in seen:
                continue
            seen.add(key)
            e2 = dict(e)
            e2["source"] = new_src
            e2["target"] = new_tgt
            remapped_edges.append(e2)
        edges = remapped_edges

    # 6. Classify every node into a subsystem bucket, attach to node.
    sub_by_id = classify_nodes(nodes, edges)
    for n in nodes:
        n["subsystem"] = sub_by_id.get(n["id"], "unknown")

    # 7. Build the subsystems index: sorted by count desc, `unknown` last.
    counts: dict[str, int] = {}
    for n in nodes:
        counts[n["subsystem"]] = counts.get(n["subsystem"], 0) + 1
    non_unknown = sorted(
        ((k, v) for k, v in counts.items() if k != "unknown"),
        key=lambda kv: (-kv[1], kv[0]),
    )
    ordered: list[tuple[str, int]] = list(non_unknown)
    if counts.get("unknown"):
        ordered.append(("unknown", counts["unknown"]))
    subsystems = [
        {"key": k, "label": _SUBSYSTEM_LABELS[k], "count": v}
        for k, v in ordered
    ]

    return {"nodes": nodes, "edges": edges, "subsystems": subsystems}


_ACTION_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Jumper",
        ("jumper", "torn flex", "broken trace", "pad lift", "pad-lift", "trace cut", "trace break"),
    ),
    (
        "Reball",
        ("reball", "bga ball", "perimeter ball", "cracked ball", "cracked bga", "ball array"),
    ),
    ("Reflow", ("reflow", "cold joint", "cracked solder", "cracked joint")),
    (
        "Lift",
        (
            "leaky", "shorted cap", "shorted decoupling", "shorted mlcc", "leaky mlcc",
            "lift cap", "lift the", "shorted capacitor", "nand-adjacent cap",
            "decoupling cap",
        ),
    ),
    (
        "Replace",
        (
            "replace", "die failure", "damaged", "blown", "swap", "burn",
            "internal failure", "regulator failure", "die damage", "internal gpu",
            "internal regulator", "chip failure",
        ),
    ),
    (
        "Hunt short on",
        ("short to ground", "short to gnd", "shorted to gnd", "shorted to ground"),
    ),
)


_REFDES_PREFIX_VERBS: dict[str, str] = {
    "C": "Lift",      # capacitor — almost always lifted when shorted/leaky
    "U": "Replace",   # IC
    "Q": "Replace",   # MOSFET / transistor
    "L": "Replace",   # inductor
    "R": "Replace",   # resistor
    "D": "Replace",   # diode
    "J": "Jumper",    # connector — typically pad-repair / jumper
}


def _derive_action_label(rule: dict[str, Any]) -> tuple[str, str]:
    """Pick a short imperative microsoldering action label from a rule.

    Returns `(label, top_mechanism)`. The label uses the top-probability
    cause's refdes, preceded by a verb inferred from the cause's mechanism
    string. When no mechanism keyword fires, the refdes's PCB prefix
    (C/U/Q/L/R/D/J) chooses a reasonable default. `top_mechanism` is kept
    for the node's description.
    """
    causes = rule.get("likely_causes") or []
    if not causes:
        return (f"Investigate — {rule.get('id', 'rule')}", "")

    sorted_causes = sorted(
        causes,
        key=lambda c: c.get("probability", 0.0),
        reverse=True,
    )
    top = sorted_causes[0]
    refdes = top.get("refdes") or "component"
    mechanism = top.get("mechanism") or ""
    mech_lower = mechanism.lower()

    for verb, keywords in _ACTION_KEYWORDS:
        if any(kw in mech_lower for kw in keywords):
            return (f"{verb} {refdes}", mechanism)

    prefix = refdes[:1].upper() if refdes else ""
    verb = _REFDES_PREFIX_VERBS.get(prefix, "Repair")
    return (f"{verb} {refdes}", mechanism)
