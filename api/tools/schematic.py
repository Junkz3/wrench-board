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
from pathlib import Path
from typing import Any

_VALID_QUERIES = (
    "rail",
    "component",
    "downstream",
    "boot_phase",
    "list_rails",
    "list_boot",
)


def _load_graph(device_slug: str, memory_root: Path) -> tuple[dict | None, str | None]:
    path = memory_root / device_slug / "electrical_graph.json"
    if not path.exists():
        return None, "no_schematic_graph"
    try:
        return json.loads(path.read_text()), None
    except (json.JSONDecodeError, OSError):
        return None, "malformed_graph"


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
            "hint": "Provide `index` (1-based) for query=boot_phase.",
        }
    for phase in seq:
        if phase.get("index") == index:
            return {
                "found": True,
                "query": "boot_phase",
                "index": index,
                "name": phase.get("name"),
                "rails_stable": phase.get("rails_stable", []),
                "components_entering": phase.get("components_entering", []),
                "triggers_next": phase.get("triggers_next", []),
                "total_phases": total,
            }
    return {
        "found": False,
        "reason": "unknown_phase",
        "index": index,
        "total_phases": total,
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


def _list_boot_query(graph: dict) -> dict[str, Any]:
    seq = graph.get("boot_sequence", [])
    return {
        "found": True,
        "query": "list_boot",
        "count": len(seq),
        "phases": [
            {
                "index": p.get("index"),
                "name": p.get("name"),
                "rail_count": len(p.get("rails_stable", [])),
                "component_count": len(p.get("components_entering", [])),
            }
            for p in seq
        ],
    }


def mb_schematic_graph(
    *,
    device_slug: str,
    memory_root: Path,
    query: str,
    label: str | None = None,
    refdes: str | None = None,
    index: int | None = None,
) -> dict[str, Any]:
    """Deterministic read over `memory/{device_slug}/electrical_graph.json`.

    Supported queries:
      - `rail`,        with `label=<str>`   — rail details + boot phase
      - `component`,   with `refdes=<str>`  — component enriched with rails
      - `downstream`,  with `refdes=<str>`  — transitive loss-of-power DAG
      - `boot_phase`,  with `index=<int>`   — phase contents (1-based)
      - `list_rails`                        — brief catalogue of power rails
      - `list_boot`                         — brief catalogue of boot phases

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
    return {
        "found": False,
        "reason": "invalid_query",
        "query": query,
        "valid_queries": list(_VALID_QUERIES),
    }
