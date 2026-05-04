"""Static prompts + user-message assembly for the bench generator LLM call.

Kept in one module so the system prompt can be version-controlled in
isolation. The graph summary deliberately omits edges + pins — the LLM
only needs to know WHICH refdes and rails exist, not their connectivity.

Functional-bridge section: the Scout dump speaks in human language
("LPC controller", "charge board") and the graph in refdes ("U14", "BT1").
`build_functional_candidate_map` pre-computes deterministic candidates
linking each registry canonical_name to graph refdes so the LLM has a
legitimate basis for attribution — no fabrication from topology guessing.
"""

from __future__ import annotations

import json

from api.pipeline.schematic.schemas import ElectricalGraph

FORCED_TOOL_NAME = "propose_scenarios"


SYSTEM_PROMPT = """\
You are a diagnostic-scenario extractor for a board-level electronics
simulator benchmark. Given a device's research dump (forums, datasheets,
community posts — all web-search sourced with URLs), the device's
compiled electrical graph (refdes, power rails), and a functional-name
bridge mapping canonical entities to refdes candidates, you propose a
set of failure scenarios that can be run against a physics-lite
simulator.

Your output MUST satisfy these contracts — failures at any of them will
be discarded downstream:

1. GROUNDING. For every structured field you fill (cause.refdes,
   cause.mode, cause.value_ohms, cause.voltage_pct, expected_dead_rails,
   expected_dead_components), emit an `evidence` entry whose
   `source_quote_substring` is a LITERAL, VERBATIM substring of
   `source_quote`. Case-sensitive, no paraphrase, no normalisation.
   If you cannot find a literal substring that justifies a field, do
   NOT emit that field.

2. REFDES BRIDGE. The research dump typically names components by their
   functional role ("LPC controller", "battery charge board") not their
   refdes ("U14", "BT1"). You have a FUNCTIONAL CANDIDATE MAP block
   that lists, for each canonical_name from the registry, the refdes
   candidates from the graph. To attribute a scenario to a refdes when
   the quote only mentions a functional name:
   (a) The functional name (canonical_name or any of its aliases) must
       appear literally in source_quote.
   (b) The chosen cause.refdes must be one of the refdes candidates
       listed in the FUNCTIONAL CANDIDATE MAP for that canonical_name.
   (c) State the mapping explicitly in the evidence[].reasoning.
   If no candidate matches, SKIP the scenario — do not guess a refdes
   from graph topology alone.

3. TOPOLOGY. Every refdes (cause + expected_dead_components) and every
   rail name you emit must exist in the provided graph.

4. RAIL CASCADE FROM TOPOLOGY. When the scenario's cause.refdes is the
   `source_refdes` of a rail (or listed in its `decoupling`), you SHOULD
   include that rail in `expected_dead_rails` — the simulator models
   this cascade and cascade_recall depends on it. The rail label does
   NOT need to appear literally in source_quote; its electrical
   connection in the graph is ground truth. For the required evidence
   on `expected_dead_rails`, cite any span from the quote that
   symptomatically relates to the rail's death (e.g. "screen did not
   turn on" for a display-backlight rail; "board won't boot" for the
   main logic supply). The reasoning should explain the topology:
   "cause.refdes sources rail X per the graph; the symptom cited is
   consistent with X being offline."

5. PROVENANCE. source_url must be an http(s) URL from the dump. source_quote
   is verbatim from the dump (≥ 50 chars). If the dump is vague, emit
   fewer scenarios with high confidence; do not pad.

6. FAILURE MODES. Exactly one of: dead | shorted | open | leaky_short |
   regulating_low. leaky_short requires value_ohms (typical 100-500 Ω),
   regulating_low requires voltage_pct (typical 0.75-0.95).

7. DEDUP. Do not emit two scenarios with the same (refdes, mode, rails,
   components) tuple.

8. ZERO CASCADE IS VALID. If the source describes a silent / local
   failure, emit empty expected_dead_rails AND expected_dead_components.
   This is a legitimate anti-pattern scenario the bench needs.

Return the scenarios via the `propose_scenarios` tool. No prose output.
"""


def graph_summary(graph: ElectricalGraph) -> str:
    """Compact projection of ElectricalGraph for the user prompt. Drops
    edges and pin-level detail; keeps refdes + kind + role + rails."""
    lines = [f"Device slug: {graph.device_slug}"]
    lines.append(f"\n## Components ({len(graph.components)})")
    for refdes in sorted(graph.components):
        c = graph.components[refdes]
        role = c.role or "-"
        kind = c.kind or "-"
        lines.append(f"  {refdes} kind={kind} role={role}")
    lines.append(f"\n## Power rails ({len(graph.power_rails)})")
    for rail_key in sorted(graph.power_rails):
        r = graph.power_rails[rail_key]
        src = r.source_refdes or "-"
        dec = ",".join(r.decoupling or []) or "-"
        voltage = r.voltage_nominal if r.voltage_nominal is not None else float("nan")
        lines.append(
            f"  {r.label} voltage_nominal={voltage:.2f} source_refdes={src} decoupling={dec}"
        )
    return "\n".join(lines)


def score_refdes_for_canonical(
    refdes: str,
    refdes_kind: str | None,
    canonical_name: str,
    aliases: list[str],
    reg_kind: str | None,
    graph: ElectricalGraph,
) -> tuple[float, str] | None:
    """Return (score, reason) or None if this refdes is not a plausible
    candidate for the given canonical_name. Deterministic heuristics only."""
    names_lc = [n.lower() for n in [canonical_name] + aliases]

    # 1. Direct refdes mention in the canonical or any alias (e.g. "D9 LED" → D9)
    for n in names_lc:
        # Treat refdes as a token; avoid matching "C1" inside "CSA1" etc.
        tokens = {t.strip(".,;:()[]") for t in n.replace("/", " ").split()}
        if refdes.lower() in tokens:
            return (1.0, f"refdes token appears literally in '{canonical_name}' or alias")

    # 2. Kind compatibility (soft prerequisite). If registry says ic and
    # graph says passive_c, skip — almost certainly not the same entity.
    if reg_kind and refdes_kind and reg_kind != refdes_kind:
        return None

    # 3. Rail-name overlap: does the refdes source a rail whose label shares
    # a significant token with any alias? Uses len>=2 threshold to keep
    # rail-name tokens like "5v", "3v3" (common in schematic conventions).
    rail_bonus = 0.0
    rail_reason = ""
    for rail_id, rail in graph.power_rails.items():
        if rail.source_refdes != refdes:
            continue
        rail_tokens = {
            t for t in rail_id.lower().replace("_", " ").replace("+", "").split() if len(t) >= 2
        }
        for n in names_lc:
            alias_tokens = {t.strip(".,;:()[]") for t in n.split() if len(t) >= 2}
            # Also check tokens normalized by +/-/_ removal in alias side
            alias_tokens |= {t for t in n.replace("+", "").replace("-", " ").split() if len(t) >= 2}
            shared = rail_tokens & alias_tokens
            if shared:
                rail_bonus = 0.8
                rail_reason = (
                    f"sources rail {rail_id} sharing token(s) "
                    f"{sorted(shared)} with canonical/alias '{n}'"
                )
                break
        if rail_bonus:
            break

    if rail_bonus:
        return (rail_bonus, rail_reason)

    # 4. Role keyword match (weak): if the canonical description contains the
    # refdes's role (e.g. role="buck_regulator" in a canonical described as
    # "regulator for +3V3")
    # Skipped for now — too noisy without description text.

    return None


def _candidates_from_registry(entry: dict) -> list[tuple[float, str, str]] | None:
    """Read legacy `refdes_candidates` off a registry component, when present.

    Returns a list of (confidence, refdes, evidence) tuples sorted by
    descending confidence, or None when the entry has no candidates
    field, an empty list, or malformed shape. None means "fall back to
    the heuristic" — the registry didn't speak about this canonical.

    Deprecated path: refdes_candidates was emitted by the 2026-04-24
    Registry-with-graph design that has since been reverted. Kept for
    back-compat with packs already on disk; the active code path is
    `_candidates_from_attributions` reading the Mapper's output JSON."""
    raw = entry.get("refdes_candidates")
    if not raw:
        return None
    out: list[tuple[float, str, str]] = []
    for cand in raw:
        try:
            refdes = str(cand["refdes"])
            confidence = float(cand["confidence"])
            evidence = str(cand["evidence"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append((confidence, refdes, evidence))
    if not out:
        return None
    out.sort(key=lambda x: (-x[0], x[1]))
    return out


def _index_attributions(
    attributions: list[dict] | None,
) -> dict[str, list[tuple[float, str, str]]]:
    """Group Refdes Mapper attributions by canonical_name.

    Input is the raw `attributions` list from `refdes_attributions.json`
    (the Mapper's persisted output, server-side-validated). Output maps
    each canonical_name → sorted (confidence, refdes, reasoning) tuples
    so callers can look up by canonical without re-walking the list.
    Returns an empty dict when input is None / empty / malformed.
    """
    out: dict[str, list[tuple[float, str, str]]] = {}
    if not attributions:
        return out
    for a in attributions:
        try:
            canonical = str(a["canonical_name"])
            refdes = str(a["refdes"])
            confidence = float(a["confidence"])
            reasoning = str(a.get("reasoning") or a.get("evidence_quote", ""))
        except (KeyError, TypeError, ValueError):
            continue
        out.setdefault(canonical, []).append((confidence, refdes, reasoning))
    for items in out.values():
        items.sort(key=lambda x: (-x[0], x[1]))
    return out


def build_functional_candidate_map(
    registry: dict,
    graph: ElectricalGraph,
    attributions: list[dict] | None = None,
) -> str:
    """Pre-compute deterministic refdes candidates for each registry
    canonical_name. Produces a structured block for the user prompt so the
    LLM has a legitimate basis for attribution.

    Source order per canonical (highest priority first):
      1. **Mapper attributions** from `memory/{slug}/refdes_attributions.json`
         (Phase 2.5, server-side-validated against literal-quote rules).
      2. Legacy `registry.components[i].refdes_candidates` from the
         reverted 2026-04-24 design — kept for back-compat on existing packs.
      3. Deterministic `score_refdes_for_canonical` heuristic
         (rail-overlap, refdes-token mention, kind match).

    Output shape (Markdown-like, human+LLM readable):

        ## Functional → refdes candidate map
        ### LPC controller
          aliases: system controller, reform2_lpc, LPC, LPC MCU
          kind: ic
          description: Embedded MCU that manages power sequencing...
          source: mapper  (or 'registry' / 'heuristic')
          candidates:
            - U14 (score=0.95): dump quote ties LPC to U14 literally

    Canonical entries with zero candidates are emitted as
    `candidates: (none — do not attribute scenarios to this entity)`
    so the LLM is explicitly warned off.
    """
    components = registry.get("components", [])
    attr_index = _index_attributions(attributions)
    lines = ["## Functional → refdes candidate map"]
    lines.append(
        "When the dump names a functional entity (canonical_name or alias), "
        "only the refdes candidates listed here may be used for cause.refdes."
    )
    for entry in components:
        canonical = entry.get("canonical_name", "")
        aliases = entry.get("aliases", []) or []
        reg_kind = entry.get("kind")
        desc = entry.get("description", "") or ""
        lines.append(f"\n### {canonical}")
        if aliases:
            lines.append(f"  aliases: {', '.join(aliases)}")
        if reg_kind:
            lines.append(f"  kind: {reg_kind}")
        if desc:
            lines.append(f"  description: {desc[:240]}")

        # 1. Mapper attributions take absolute precedence — they carry
        # server-validated evidence (literal refdes / MPN in quote).
        mapper_cands = attr_index.get(canonical)
        if mapper_cands:
            lines.append("  source: mapper (refdes_attributions.json, phase 2.5)")
            lines.append("  candidates:")
            for score, refdes, reason in mapper_cands[:5]:
                lines.append(f"    - {refdes} (score={score:.2f}): {reason}")
            continue

        # 2. Legacy registry refdes_candidates (reverted 2026-04-24 design).
        registry_cands = _candidates_from_registry(entry)
        if registry_cands is not None:
            lines.append("  source: registry (refdes_candidates from phase 2)")
            lines.append("  candidates:")
            for score, refdes, evidence in registry_cands[:5]:
                lines.append(f"    - {refdes} (score={score:.2f}): {evidence}")
            continue

        # 3. Fallback — deterministic heuristic over the graph.
        scored: list[tuple[float, str, str]] = []
        for refdes, comp in graph.components.items():
            res = score_refdes_for_canonical(refdes, comp.kind, canonical, aliases, reg_kind, graph)
            if res is not None:
                scored.append((res[0], refdes, res[1]))
        scored.sort(key=lambda x: (-x[0], x[1]))
        if not scored:
            lines.append("  source: heuristic (rail-overlap)")
            lines.append("  candidates: (none — do not attribute scenarios to this entity)")
        else:
            lines.append("  source: heuristic (rail-overlap)")
            lines.append("  candidates:")
            for score, refdes, reason in scored[:5]:
                lines.append(f"    - {refdes} (score={score:.2f}): {reason}")
    return "\n".join(lines)


def build_rail_alias_map(registry: dict, graph: ElectricalGraph) -> str:
    """Produce a signals/rail alias block so the LLM knows which graph
    rail corresponds to descriptive phrases in the dump."""
    signals = registry.get("signals", []) or []
    lines = ["## Rail alias map (from registry.signals)"]
    graph_rails_lc = {k.lower(): k for k in graph.power_rails}
    for s in signals:
        canonical = s.get("canonical_name", "")
        aliases = s.get("aliases", []) or []
        kind = s.get("kind", "")
        lines.append(f"  '{canonical}' (aliases: {', '.join(aliases) or '—'}) kind={kind}")
        # Try to match canonical to an actual rail in the graph
        candidate = graph_rails_lc.get(canonical.lower())
        if candidate:
            lines.append(f"    → graph rail: {candidate}")
    return "\n".join(lines)


def build_user_message(
    *,
    raw_dump: str,
    rules_json: str,
    registry_json: str,
    graph: ElectricalGraph,
    attributions: list[dict] | None = None,
) -> str:
    """Concatenate the input blocks in a stable order for caching.

    Structure:
      1. Research dump (Scout) — narrative, functional-name language
      2. Rules (Clinicien) — structured symptom→cause→sources
      3. Registry (canonical vocabulary) — raw JSON reference
      4. Functional → refdes candidate map — the BRIDGE between (1) and (5)
      5. Rail alias map — secondary bridge for rails
      6. Electrical graph summary — refdes + rails + sourcing topology

    `attributions`, when supplied (typically loaded from
    `memory/{slug}/refdes_attributions.json`), takes precedence over the
    legacy registry.refdes_candidates and the rail-overlap heuristic for
    the functional bridge in step 4.
    """
    try:
        registry = json.loads(registry_json) if registry_json.strip() else {}
    except json.JSONDecodeError:
        registry = {}
    bridge = build_functional_candidate_map(registry, graph, attributions)
    rail_map = build_rail_alias_map(registry, graph)
    return (
        "# Research dump (Scout)\n"
        f"{raw_dump}\n"
        "\n# Rules (Clinicien)\n"
        f"{rules_json}\n"
        "\n# Registry (canonical vocabulary)\n"
        f"{registry_json}\n"
        "\n# Functional bridge\n"
        f"{bridge}\n"
        f"\n{rail_map}\n"
        "\n# Electrical graph summary\n"
        f"{graph_summary(graph)}\n"
        "\nEmit the propose_scenarios tool call now."
    )
