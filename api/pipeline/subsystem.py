"""Classify knowledge-graph nodes into a functional subsystem bucket.

Pure-function, deterministic, zero LLM. Used by graph_transform to attach a
`subsystem` tag on every node in the payload, which the frontend consumes
to lay out nodes in horizontal bands (alimentation / charge / display / …).

Rule table is ordered: first match wins. That ordering is load-bearing
(charge MUST precede power so "battery supply" lands in charge; usb MUST
precede power so "VBUS_5V" lands in usb).
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

# Ordered (key, compiled pattern) tuples. First match wins.
#
# Order is load-bearing:
#   - charge precedes power → "battery supply" / "LiFePO4 cell" land in charge.
#   - usb precedes power    → "VBUS_5V" lands in usb, not power via `\d+v`.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("charge",  re.compile(r"charge|battery|bms|balance|lifepo4|\bcell\b", re.I)),
    ("usb",     re.compile(r"\busb\d*\b|type-c|\bcc\d*\b|\bpd\b|vbus", re.I)),
    ("power",   re.compile(r"vbat|vcc|vdd|vsys|v\d+|barrel|\d+v\b|\brail\b|supply|\bpwr\b|pmic|regulator|power", re.I)),
    ("display", re.compile(r"hdmi|dsi|edp|\blcd\b|\blvds\b|backlight|vsync|hsync|\bdp\b", re.I)),
    ("audio",   re.compile(r"i2s|bclk|speaker|mic|headphone|audio", re.I)),
    ("rf",      re.compile(r"antenna|\bant\b|\brf\b|pcie|wifi|\bbt\b", re.I)),
    ("cpu-mem", re.compile(r"\bcpu\b|\bddr\b|\bram\b|\bsoc\b|\bsom\b|\bspi\b|\bi2c\b", re.I)),
    ("io",      re.compile(r"uart|gpio|button|\bkey\b|keyboard|\bled\b", re.I)),
)

UNKNOWN = "unknown"

# Token separators in net / refdes names (SPI_MOSI, V-BAT, audio/line).
# We normalise these to spaces so `\b` regex boundaries fire correctly —
# in Python `_` is a word character, so `\bspi\b` fails against "SPI_MOSI"
# without this step.
_SEPARATORS = re.compile(r"[_\-/]+")

__all__ = ["classify_nodes", "UNKNOWN"]


def _classify_text(text: str) -> str:
    normalized = _SEPARATORS.sub(" ", text)
    for key, pattern in _RULES:
        if pattern.search(normalized):
            return key
    return UNKNOWN


def classify_nodes(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> dict[str, str]:
    """Return {node_id: subsystem} for every node. Keys are a fixed vocabulary."""
    result: dict[str, str] = {}

    # Pass 1 — nets: direct classification on label.
    for n in nodes:
        if n["type"] == "net":
            result[n["id"]] = _classify_text(n["label"])

    # Pass 2 — components: majority vote over the nets this component touches
    # via net-adjacency edges, either direction. Fallback to description/role
    # regex. Fallback to UNKNOWN.
    #
    # Edge relations that indicate physical/electrical net adjacency from
    # the schema (api/pipeline/schemas.py — KnowledgeEdge.relation). A
    # capacitor that `decouples` a net is on that net; a probe that
    # `measured_at` a net observes it; a generic `connects` is the
    # Cartographe's default. `part_of` is structural (component-in-assembly)
    # and is deliberately excluded — it does not imply net adjacency.
    comp_ids = {n["id"] for n in nodes if n["type"] == "component"}
    adj: dict[str, list[str]] = {cid: [] for cid in comp_ids}
    for e in edges:
        if e["relation"] not in ("connects", "decouples", "measured_at", "powers"):
            continue
        if e["source"] in comp_ids and e["target"] in result:
            adj[e["source"]].append(result[e["target"]])
        if e["target"] in comp_ids and e["source"] in result:
            adj[e["target"]].append(result[e["source"]])
    for n in nodes:
        if n["type"] != "component":
            continue
        votes = [s for s in adj[n["id"]] if s != UNKNOWN]
        if votes:
            top, _ = Counter(votes).most_common(1)[0]
            result[n["id"]] = top
        else:
            result[n["id"]] = _classify_text(n.get("description", "") + " " + n.get("label", ""))

    # Pass 3 — symptoms: inherit majority from components that `causes` them.
    sym_ids = {n["id"] for n in nodes if n["type"] == "symptom"}
    sym_votes: dict[str, list[str]] = {sid: [] for sid in sym_ids}
    for e in edges:
        if e["relation"] != "causes":
            continue
        if e["target"] in sym_ids and e["source"] in result:
            val = result[e["source"]]
            if val != UNKNOWN:
                sym_votes[e["target"]].append(val)
    for n in nodes:
        if n["type"] != "symptom":
            continue
        votes = sym_votes[n["id"]]
        if votes:
            top, _ = Counter(votes).most_common(1)[0]
            result[n["id"]] = top
        else:
            result[n["id"]] = UNKNOWN

    # Pass 4 — actions: inherit majority from symptoms they `resolves`, and in
    # turn from the components causing those symptoms. Re-uses pass-3 output.
    act_ids = {n["id"] for n in nodes if n["type"] == "action"}
    act_votes: dict[str, list[str]] = {aid: [] for aid in act_ids}
    for e in edges:
        if e["relation"] != "resolves":
            continue
        if e["source"] in act_ids and e["target"] in result:
            val = result[e["target"]]
            if val != UNKNOWN:
                act_votes[e["source"]].append(val)
    for n in nodes:
        if n["type"] != "action":
            continue
        votes = act_votes[n["id"]]
        if votes:
            top, _ = Counter(votes).most_common(1)[0]
            result[n["id"]] = top
        else:
            result[n["id"]] = UNKNOWN

    # Contract: every input node gets an entry, even if its type isn't one
    # of the four the passes handle. Defensive — protects downstream
    # consumers (graph_transform, frontend) from KeyError if the schema
    # grows.
    for n in nodes:
        result.setdefault(n["id"], UNKNOWN)

    return result
