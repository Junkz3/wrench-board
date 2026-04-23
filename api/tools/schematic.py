# SPDX-License-Identifier: Apache-2.0
"""`mb_schematic_graph` — deterministic reader over the compiled electrical graph.

Pure disk-read tool for the diagnostic agent. Zero LLM calls, zero mutation,
zero session coupling. Reads `memory/{slug}/electrical_graph.json` (produced
by the schematic sub-pipeline) and dispatches on a `query` parameter into
rail / component / downstream / boot_phase / list_rails / list_boot lookups.

Every miss returns a structured `{found: false, reason, ...}` — no
fabrication. Closest-match suggestions are offered when a label or refdes
is typoed, matching the guardrail shape of `mb_get_component`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from api.pipeline.schematic.schemas import AnalyzedBootSequence, ElectricalGraph
from api.pipeline.schematic.simulator import SimulationEngine

_VALID_QUERIES = (
    "rail",
    "component",
    "downstream",
    "boot_phase",
    "list_rails",
    "list_boot",
    "critical_path",
    "net",
    "net_domain",
    "simulate",
)


def _load_graph(device_slug: str, memory_root: Path) -> tuple[dict | None, str | None]:
    path = memory_root / device_slug / "electrical_graph.json"
    if not path.exists():
        return None, "no_schematic_graph"
    try:
        graph = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None, "malformed_graph"

    # If an Opus-refined boot analysis is on disk, surface its phases as the
    # primary `boot_sequence` and tag the source. The compiler version stays
    # available under `boot_sequence_compiler` for callers that want both.
    analyzed_path = memory_root / device_slug / "boot_sequence_analyzed.json"
    if analyzed_path.exists():
        try:
            analyzed = json.loads(analyzed_path.read_text())
            graph["boot_sequence_compiler"] = graph.get("boot_sequence", [])
            graph["boot_sequence"] = analyzed.get("phases", graph.get("boot_sequence", []))
            graph["boot_sequence_source"] = "analyzer"
            graph["boot_analyzer_meta"] = {
                "sequencer_refdes": analyzed.get("sequencer_refdes"),
                "global_confidence": analyzed.get("global_confidence"),
                "model_used": analyzed.get("model_used"),
                "ambiguities": analyzed.get("ambiguities", []),
            }
        except (json.JSONDecodeError, OSError):
            graph["boot_sequence_source"] = "compiler"
    else:
        graph["boot_sequence_source"] = "compiler"

    # Attach net classification (Opus-produced when available, regex fallback
    # otherwise). `net_domains` = map of net label → ClassifiedNet dict.
    classified_path = memory_root / device_slug / "nets_classified.json"
    if classified_path.exists():
        try:
            classified = json.loads(classified_path.read_text())
            graph["net_domains"] = classified.get("nets", {})
            graph["net_domains_meta"] = {
                "domain_summary": classified.get("domain_summary", {}),
                "model_used": classified.get("model_used", "regex"),
                "ambiguities": classified.get("ambiguities", []),
            }
        except (json.JSONDecodeError, OSError):
            graph["net_domains"] = {}
            graph["net_domains_meta"] = {}
    else:
        graph["net_domains"] = {}
        graph["net_domains_meta"] = {}

    return graph, None


def _boot_phase_for_rail(graph: dict, label: str) -> int | None:
    for phase in graph.get("boot_sequence", []):
        if label in phase.get("rails_stable", []):
            return phase.get("index")
    return None


def _boot_phase_for_component(graph: dict, refdes: str) -> int | None:
    for phase in graph.get("boot_sequence", []):
        if refdes in phase.get("components_entering", []):
            return phase.get("index")
    return None


def _rails_produced_by(graph: dict, refdes: str) -> list[str]:
    return sorted(
        label
        for label, rail in graph.get("power_rails", {}).items()
        if rail.get("source_refdes") == refdes
    )


def _rails_consumed_by(graph: dict, refdes: str) -> list[str]:
    comp = graph.get("components", {}).get(refdes)
    if not comp:
        return []
    produced = set(_rails_produced_by(graph, refdes))
    rails = graph.get("power_rails", {})
    consumed: set[str] = set()
    for pin in comp.get("pins", []):
        label = pin.get("net_label")
        if label and label in rails and label not in produced:
            consumed.add(label)
    return sorted(consumed)


def _closest_matches(candidates: list[str], needle: str, k: int = 5) -> list[str]:
    needle_upper = needle.upper()
    prefix = needle_upper[:1] if needle_upper else ""
    substr_hits = sorted(c for c in candidates if needle_upper and needle_upper in c.upper())
    prefix_hits = sorted(c for c in candidates if prefix and c.upper().startswith(prefix))
    merged = list(dict.fromkeys(substr_hits + prefix_hits))
    return merged[:k]


def _rail_query(graph: dict, label: str | None) -> dict[str, Any]:
    if not label:
        return {
            "found": False,
            "reason": "missing_parameter",
            "hint": "Provide `label` for query=rail (e.g. '+5V').",
        }
    rails = graph.get("power_rails", {})
    if label not in rails:
        return {
            "found": False,
            "reason": "unknown_rail",
            "label": label,
            "closest_matches": _closest_matches(list(rails.keys()), label),
        }
    rail = rails[label]
    nets = graph.get("nets", {})
    return {
        "found": True,
        "query": "rail",
        "label": label,
        "voltage_nominal": rail.get("voltage_nominal"),
        "source_refdes": rail.get("source_refdes"),
        "source_type": rail.get("source_type"),
        "enable_net": rail.get("enable_net"),
        "consumers": rail.get("consumers", []),
        "decoupling": rail.get("decoupling", []),
        "boot_phase": _boot_phase_for_rail(graph, label),
        "pages": nets.get(label, {}).get("pages", []),
    }


def _component_query(graph: dict, refdes: str | None) -> dict[str, Any]:
    if not refdes:
        return {
            "found": False,
            "reason": "missing_parameter",
            "hint": "Provide `refdes` for query=component (e.g. 'U7').",
        }
    components = graph.get("components", {})
    if refdes not in components:
        return {
            "found": False,
            "reason": "unknown_component",
            "refdes": refdes,
            "closest_matches": _closest_matches(list(components.keys()), refdes),
        }
    comp = components[refdes]
    return {
        "found": True,
        "query": "component",
        "refdes": refdes,
        "type": comp.get("type"),
        "value": comp.get("value"),
        "pages": comp.get("pages", []),
        "pins": comp.get("pins", []),
        "populated": comp.get("populated", True),
        "rails_produced": _rails_produced_by(graph, refdes),
        "rails_consumed": _rails_consumed_by(graph, refdes),
        "boot_phase": _boot_phase_for_component(graph, refdes),
    }


def _downstream_query(graph: dict, refdes: str | None) -> dict[str, Any]:
    if not refdes:
        return {
            "found": False,
            "reason": "missing_parameter",
            "hint": "Provide `refdes` for query=downstream.",
        }
    components = graph.get("components", {})
    if refdes not in components:
        return {
            "found": False,
            "reason": "unknown_component",
            "refdes": refdes,
            "closest_matches": _closest_matches(list(components.keys()), refdes),
        }
    rails = graph.get("power_rails", {})
    rails_direct = _rails_produced_by(graph, refdes)

    components_direct: set[str] = set()
    for r in rails_direct:
        for c in rails[r].get("consumers", []):
            if c != refdes:
                components_direct.add(c)

    rails_transitive: set[str] = set(rails_direct)
    components_transitive: set[str] = set(components_direct)
    frontier: list[str] = list(components_direct)
    while frontier:
        node = frontier.pop()
        for produced in _rails_produced_by(graph, node):
            if produced in rails_transitive:
                continue
            rails_transitive.add(produced)
            for consumer in rails[produced].get("consumers", []):
                if consumer != node and consumer not in components_transitive:
                    components_transitive.add(consumer)
                    frontier.append(consumer)

    return {
        "found": True,
        "query": "downstream",
        "refdes": refdes,
        "rails_direct": rails_direct,
        "components_direct": sorted(components_direct),
        "rails_transitive": sorted(rails_transitive),
        "components_transitive": sorted(components_transitive),
    }


def _boot_phase_query(graph: dict, index: int | None) -> dict[str, Any]:
    seq = graph.get("boot_sequence", [])
    total = len(seq)
    if index is None:
        return {
            "found": False,
            "reason": "missing_parameter",
            "hint": "Provide `index` (0- or 1-based) for query=boot_phase.",
        }
    source = graph.get("boot_sequence_source", "compiler")
    for phase in seq:
        if phase.get("index") == index:
            result: dict[str, Any] = {
                "found": True,
                "query": "boot_phase",
                "index": index,
                "name": phase.get("name"),
                "rails_stable": phase.get("rails_stable", []),
                "components_entering": phase.get("components_entering", []),
                "triggers_next": phase.get("triggers_next", []),
                "total_phases": total,
                "source": source,
            }
            # Analyzer-only fields — included only when the phase carries them.
            for extra in ("kind", "evidence", "confidence"):
                if extra in phase:
                    result[extra] = phase[extra]
            return result
    return {
        "found": False,
        "reason": "unknown_phase",
        "index": index,
        "total_phases": total,
        "source": source,
    }


def _list_rails_query(graph: dict) -> dict[str, Any]:
    rails = graph.get("power_rails", {})
    return {
        "found": True,
        "query": "list_rails",
        "count": len(rails),
        "rails": [
            {
                "label": label,
                "voltage_nominal": rail.get("voltage_nominal"),
                "source_refdes": rail.get("source_refdes"),
                "consumer_count": len(rail.get("consumers", [])),
            }
            for label, rail in sorted(rails.items())
        ],
    }


def _compute_blast_radius_all(graph: dict) -> list[dict[str, Any]]:
    """Compute downstream cascade size for every rail + component node.

    Returns a list sorted by blast_radius descending — the board's
    Single-Points-Of-Failure ranked by impact. Pure function of the power
    topology (producer → rail → consumer edges).
    """
    rails = graph.get("power_rails", {})
    adj: dict[str, list[str]] = {}
    all_nodes: set[str] = set()

    for label, rail in rails.items():
        rid = f"rail:{label}"
        all_nodes.add(rid)
        src = rail.get("source_refdes")
        if src:
            sid = f"comp:{src}"
            all_nodes.add(sid)
            adj.setdefault(sid, []).append(rid)
        for c in rail.get("consumers", []) or []:
            if c == src:
                continue
            cid = f"comp:{c}"
            all_nodes.add(cid)
            adj.setdefault(rid, []).append(cid)

    def blast(start: str) -> set[str]:
        dead = {start}
        stack = [start]
        while stack:
            n = stack.pop()
            for nxt in adj.get(n, ()):
                if nxt not in dead:
                    dead.add(nxt)
                    stack.append(nxt)
        return dead - {start}

    scores: list[dict[str, Any]] = []
    total = len(all_nodes) or 1
    for nid in all_nodes:
        cas = blast(nid)
        rails_lost = sum(1 for x in cas if x.startswith("rail:"))
        comps_lost = sum(1 for x in cas if x.startswith("comp:"))
        prefix, label = nid.split(":", 1)
        scores.append({
            "id": nid,
            "kind": "rail" if prefix == "rail" else "component",
            "label": label,
            "blast_radius": len(cas),
            "rails_lost": rails_lost,
            "comps_lost": comps_lost,
            # Percentage of the board that goes down if this node dies.
            "impact_pct": round(100 * len(cas) / total, 1),
        })
    scores.sort(key=lambda s: (-s["blast_radius"], s["label"]))
    return scores


def _critical_path_query(graph: dict) -> dict[str, Any]:
    """Rank nodes by blast radius and surface per-phase critical gates.

    Returns the global Single-Points-Of-Failure (top 10 by impact) plus,
    for every boot phase, the 3 components/rails with the biggest cascade.
    Uses the analyzer's boot_sequence when on disk (more accurate phase
    placement), falls back to the compiler's topological one otherwise.
    """
    scores = _compute_blast_radius_all(graph)
    by_label = {s["label"]: s for s in scores}

    boot_seq = graph.get("boot_sequence", [])
    per_phase: list[dict[str, Any]] = []
    for phase in boot_seq:
        comps = phase.get("components_entering", []) or []
        rails_stable = phase.get("rails_stable", []) or []
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name in (*comps, *rails_stable):
            if name in by_label and name not in seen:
                candidates.append(by_label[name])
                seen.add(name)
        candidates.sort(key=lambda s: -s["blast_radius"])
        per_phase.append({
            "index": phase.get("index"),
            "name": phase.get("name"),
            "kind": phase.get("kind"),
            "critical": candidates[:3],
        })

    return {
        "found": True,
        "query": "critical_path",
        "total_nodes": len(scores),
        "top_spofs": scores[:10],
        "per_phase": per_phase,
        "source": graph.get("boot_sequence_source", "compiler"),
    }


def _list_boot_query(graph: dict) -> dict[str, Any]:
    seq = graph.get("boot_sequence", [])
    source = graph.get("boot_sequence_source", "compiler")
    result: dict[str, Any] = {
        "found": True,
        "query": "list_boot",
        "count": len(seq),
        "source": source,
        "phases": [
            {
                "index": p.get("index"),
                "name": p.get("name"),
                "rail_count": len(p.get("rails_stable", [])),
                "component_count": len(p.get("components_entering", [])),
                **({"kind": p["kind"]} if "kind" in p else {}),
                **({"confidence": p["confidence"]} if "confidence" in p else {}),
            }
            for p in seq
        ],
    }
    meta = graph.get("boot_analyzer_meta")
    if meta is not None:
        result["analyzer_meta"] = meta
    return result


def _net_query(graph: dict, label: str | None) -> dict[str, Any]:
    """Return the classified-net metadata + the components touching the net."""
    if not label:
        return {
            "found": False,
            "reason": "missing_parameter",
            "hint": "Provide `label` for query=net.",
        }
    nets = graph.get("nets", {})
    if label not in nets:
        candidates = list(nets.keys())
        return {
            "found": False,
            "reason": "unknown_net",
            "label": label,
            "closest_matches": _closest_matches(candidates, label),
        }
    net = nets[label]
    classified = (graph.get("net_domains") or {}).get(label, {})
    # Components that touch this net — cross-reference via `net.connects`
    # ("U7.3" format) and a lookup of components with a pin on that net.
    touching: set[str] = set()
    for pin_ref in net.get("connects", []) or []:
        refdes = pin_ref.split(".")[0] if "." in pin_ref else pin_ref
        if refdes:
            touching.add(refdes)
    # Back-fill from components' pin lists in case `connects` is sparse.
    for refdes, comp in (graph.get("components") or {}).items():
        for p in comp.get("pins", []) or []:
            if p.get("net_label") == label:
                touching.add(refdes)
                break
    return {
        "found": True,
        "query": "net",
        "label": label,
        "is_power": net.get("is_power", False),
        "is_global": net.get("is_global", False),
        "pages": net.get("pages", []),
        "domain": classified.get("domain"),
        "description": classified.get("description"),
        "voltage_level": classified.get("voltage_level"),
        "confidence": classified.get("confidence"),
        "touching_components": sorted(touching),
    }


# Secondary label-prefix patterns per domain — mirrors web/js/schematic.js
# DOMAIN_SUBSTRING. A net classified as `power_rail` (e.g. USB_PWR) still
# shows up when the tech / agent queries the functional domain, because
# `USB_PWR` is a USB concern even if structurally a rail. Keep this in
# sync with the frontend copy.
_DOMAIN_SUBSTRING: dict[str, re.Pattern] = {
    "hdmi":     re.compile(r"\b(?:HDMI|TMDS|DDC|CEC)\b|^(?:HDMI|TMDS|DDC)_", re.IGNORECASE),
    "usb":      re.compile(r"\bUSB\b|^USB|USB_", re.IGNORECASE),
    "pcie":     re.compile(r"\bPCIE\b|^PCIE", re.IGNORECASE),
    "ethernet": re.compile(r"\b(?:ETH|RGMII|MII|MDIO|PHY)\b|^(?:ETH|RGMII|MII|MDIO|PHY)_", re.IGNORECASE),
    "audio":    re.compile(r"\b(?:I2S|DAC|ADC|SPDIF|AUDIO|MICBIAS|AVDD|DBVDD|DCVDD|SPKVDD)\b|^(?:I2S|DAC|ADC|SPDIF|AUDIO|MIC)_", re.IGNORECASE),
    "display":  re.compile(r"\b(?:EDP|DSI|LCD|BACKLIGHT|LVDS|DP_AUX)\b|^(?:EDP|DSI|LCD|BL_)", re.IGNORECASE),
    "storage":  re.compile(r"\b(?:SD|EMMC|MMC|SDHC|SDIO)\b|^(?:SD|EMMC|MMC)_", re.IGNORECASE),
    "debug":    re.compile(r"\b(?:JTAG|SWD|UART|TDI|TDO|TCK|TMS|SWDIO|SWCLK)\b|^(?:JTAG|SWD|UART)_", re.IGNORECASE),
}


def _net_domain_query(graph: dict, domain: str | None) -> dict[str, Any]:
    """Return every net in the given domain + components ranked by impact."""
    if not domain:
        return {
            "found": False,
            "reason": "missing_parameter",
            "hint": "Provide `domain` for query=net_domain (e.g. 'hdmi').",
        }
    domain = domain.strip().lower()
    net_domains = graph.get("net_domains") or {}
    meta = graph.get("net_domains_meta") or {}
    if not net_domains:
        return {
            "found": False,
            "reason": "no_classification",
            "hint": "No net classification on disk yet — run the net_classifier or ingest the schematic.",
        }
    # 1) Primary — nets whose classified domain matches.
    matching_nets = {
        label: cn for label, cn in net_domains.items()
        if (cn.get("domain") or "").lower() == domain
    }
    # 2) Secondary — functional-family substring match so USB_PWR (classified
    # as power_rail) still comes up when the agent asks for 'usb'.
    pattern = _DOMAIN_SUBSTRING.get(domain)
    if pattern is not None:
        all_net_labels = set((graph.get("nets") or {}).keys()) | set(net_domains.keys())
        for label in all_net_labels:
            if label not in matching_nets and pattern.search(label):
                # Use classified metadata if available, otherwise stub the entry.
                matching_nets[label] = net_domains.get(label) or {
                    "label": label,
                    "domain": domain,
                    "description": "",
                    "voltage_level": None,
                    "confidence": 0.5,
                }
    if not matching_nets:
        return {
            "found": False,
            "reason": "unknown_domain",
            "domain": domain,
            "available_domains": sorted((meta.get("domain_summary") or {}).keys()),
        }
    # Count how many pins each component has on nets in this domain.
    components = graph.get("components") or {}
    touch_count: dict[str, int] = {}
    for refdes, comp in components.items():
        for p in comp.get("pins", []) or []:
            if p.get("net_label") in matching_nets:
                touch_count[refdes] = touch_count.get(refdes, 0) + 1
    # Combine with blast radius (criticality) if available. We recompute
    # lightly using the same logic as _compute_blast_radius_all but only
    # for components we care about.
    scores = _compute_blast_radius_all(graph)
    blast_by_label = {s["label"]: s["blast_radius"] for s in scores}
    ranked = [
        {
            "refdes": refdes,
            "touch_count": count,
            "blast_radius": blast_by_label.get(refdes, 0),
            "type": components.get(refdes, {}).get("type"),
        }
        for refdes, count in touch_count.items()
    ]
    # Rank by touch_count desc, then blast_radius desc, then refdes asc.
    ranked.sort(key=lambda r: (-r["touch_count"], -r["blast_radius"], r["refdes"]))
    suspects = [r["refdes"] for r in ranked[:3]]
    return {
        "found": True,
        "query": "net_domain",
        "domain": domain,
        "source": meta.get("model_used", "regex"),
        "nets": sorted(matching_nets.keys()),
        "net_details": {
            label: {
                "description": cn.get("description"),
                "voltage_level": cn.get("voltage_level"),
                "confidence": cn.get("confidence"),
            }
            for label, cn in sorted(matching_nets.items())
        },
        "components_ranked": ranked,
        "suspects_top_3": suspects,
        "total_nets_in_domain": len(matching_nets),
    }


def _simulate_query(
    graph_dict: dict,
    memory_root: Path,
    device_slug: str,
    killed_refdes: list[str] | None,
) -> dict[str, Any]:
    """Run the behavioral simulator and return a compact cascade summary.

    Validates every refdes in killed_refdes against the on-disk graph — an
    unknown refdes returns `{found: false, invalid_refdes, closest_matches}`
    rather than silently treating it as a real fault. Mirrors the
    anti-hallucination guardrail of `mb_get_component`.
    """
    killed = list(killed_refdes or [])
    components = graph_dict.get("components", {})
    invalid = [r for r in killed if r not in components]
    if invalid:
        return {
            "found": False,
            "reason": "unknown_refdes",
            "invalid_refdes": invalid,
            "closest_matches": {
                r: _closest_matches(list(components.keys()), r) for r in invalid
            },
        }
    # Re-validate from disk so we get the real Pydantic shapes the engine expects.
    pack = memory_root / device_slug
    try:
        electrical = ElectricalGraph.model_validate_json(
            (pack / "electrical_graph.json").read_text()
        )
    except (OSError, ValueError):
        return {"found": False, "reason": "malformed_graph"}
    analyzed: AnalyzedBootSequence | None = None
    ab_path = pack / "boot_sequence_analyzed.json"
    if ab_path.exists():
        try:
            analyzed = AnalyzedBootSequence.model_validate_json(ab_path.read_text())
        except ValueError:
            analyzed = None
    tl = SimulationEngine(
        electrical, analyzed_boot=analyzed, killed_refdes=killed
    ).run()
    last_state = tl.states[-1] if tl.states else None
    return {
        "found": True,
        "query": "simulate",
        "killed_refdes": tl.killed_refdes,
        "final_verdict": tl.final_verdict,
        "blocked_at_phase": tl.blocked_at_phase,
        "blocked_reason": (
            last_state.blocked_reason if last_state and last_state.blocked else None
        ),
        "phase_count": len(tl.states),
        "cascade_dead_components": tl.cascade_dead_components,
        "cascade_dead_rails": tl.cascade_dead_rails,
    }


def mb_schematic_graph(
    *,
    device_slug: str,
    memory_root: Path,
    query: str,
    label: str | None = None,
    refdes: str | None = None,
    index: int | None = None,
    domain: str | None = None,
    killed_refdes: list[str] | None = None,
) -> dict[str, Any]:
    """Deterministic read over `memory/{device_slug}/electrical_graph.json`.

    Supported queries:
      - `rail`,        with `label=<str>`            — rail details + boot phase
      - `component`,   with `refdes=<str>`           — component enriched with rails
      - `downstream`,  with `refdes=<str>`           — transitive loss-of-power DAG
      - `boot_phase`,  with `index=<int>`            — phase contents (1-based)
      - `list_rails`                                 — brief catalogue of power rails
      - `list_boot`                                  — brief catalogue of boot phases
      - `simulate`,    with `killed_refdes=[<str>]`  — behavioral cascade summary

    Always returns a dict; `found: false` with a `reason` on any miss.
    """
    graph, err = _load_graph(device_slug, memory_root)
    if err:
        return {"found": False, "reason": err, "device_slug": device_slug}
    assert graph is not None  # narrow for the type checker

    if query == "rail":
        return _rail_query(graph, label)
    if query == "component":
        return _component_query(graph, refdes)
    if query == "downstream":
        return _downstream_query(graph, refdes)
    if query == "boot_phase":
        return _boot_phase_query(graph, index)
    if query == "list_rails":
        return _list_rails_query(graph)
    if query == "list_boot":
        return _list_boot_query(graph)
    if query == "critical_path":
        return _critical_path_query(graph)
    if query == "net":
        return _net_query(graph, label)
    if query == "net_domain":
        return _net_domain_query(graph, domain)
    if query == "simulate":
        return _simulate_query(graph, memory_root, device_slug, killed_refdes)
    return {
        "found": False,
        "reason": "invalid_query",
        "query": query,
        "valid_queries": list(_VALID_QUERIES),
    }
