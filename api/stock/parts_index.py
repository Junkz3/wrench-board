"""Builds the searchable PartsIndex from already-computed schematic artefacts.

Called at the end of api.pipeline.schematic.orchestrator.ingest_schematic().
Pure function (no IO except the caller writes the result).

See docs/superpowers/specs/2026-05-08-stock-inventory-design.md §5.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Literal

from api.stock.safety import classify_safety
from api.stock.schemas import PartsIndex, PartsIndexEntry

logger = logging.getLogger(__name__)


def _coerce_voltage_rating(raw) -> float | None:
    """Source ComponentValue.voltage_rating is `str | None` ("6.3V", "16V").

    Strip a trailing V/v and parse the leading number. Returns None on any
    failure (including multi-rating strings like "6.3V/35V" — too lossy to
    pick one safely). Numeric inputs pass through.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip().rstrip("Vv").strip()
    try:
        return float(s)
    except ValueError:
        logger.debug("parts_index: could not parse voltage_rating %r", raw)
        return None


def _canonicalize_value(value: dict | None) -> str | None:
    """Normalize the raw value string to a canonical form.

    Strips annotations (current/freq ratings, type words, sheet refs,
    package suffixes) before unit matching. Cross-pipeline goal: lever1
    LP-md output and Opus baseline both reduce to identical canonical
    strings so stock_search keys agree.

    Caps:       100nF → 0.1uF, 0.1µF → 0.1uF
    Resistors:  10K0 → 10k, 0R1 → 0.1, 4.7k → 4.7k
                33-OHM → 33, 150OHM → 150, 10KOHM → 10k
                33Ω @ 1500mA → 33, 240-OHM-25%-0.20A-0.9DCR → 240
    Inductors:  1uH → 1uH, 0.47µH → 0.47uH, 15nH → 15nH
    Crystals:   24.000MHz → 24MHz, 32.768kHz → 32.768kHz
    Voltages:   5.5V Zener → 5.5V (zeners, TVS)
    MPNs:       BZT52C20LP, DFN10062 → BZT52C20LP (strips package suffix)
                D2462 WLCSP SYM 4 OF 4 → D2462 (strips sheet ref)
    Unknown → passthrough.
    """
    if value is None:
        return None
    raw = (value.get("primary") or value.get("raw") or "").strip()
    if not raw:
        return None

    s = raw
    # Strip annotations BEFORE unit normalization so the regexes see only
    # the bare <num><unit> pattern.

    # @-prefixed rating: "33Ω @ 1500mA", "150Ω@100MHz" → strip annotation
    s = re.sub(r"\s*@\s*[\d.]+\s*[A-Za-z]+\b.*$", "", s)
    # Trailing "<num><unit>" without @: "10Ω 750mA" → strip second number-with-unit
    s = re.sub(r"\s+\d+(?:\.\d+)?\s*[mukpnGM]?[AVHz]\b.*$", "", s)
    # Trailing type word: "5.5V Zener", "Schottky", "TVS", "Bidirectional"
    s = re.sub(
        r"\s+(?:Zener|Schottky|TVS|Bidirectional|Unidirectional)\b.*$",
        "", s, flags=re.IGNORECASE,
    )
    # Trailing sheet reference: "D2462 WLCSP SYM 4 OF 4" → "D2462 WLCSP"
    s = re.sub(r"\s+(?:SYM|SHEET|SH)\s.*$", "", s, flags=re.IGNORECASE)
    # Trailing package suffix after comma: "BZT52C20LP, DFN10062"
    s = re.sub(r",\s*[A-Z][A-Z0-9-]+\s*$", "", s)
    # Trailing all-caps token (typically a package): "D2462 WLCSP" → "D2462"
    s = re.sub(r"\s+[A-Z][A-Z0-9-]+\s*$", "", s)
    # Hyphenated multi-spec resistor: "240-OHM-25%-0.20A-0.9DCR" → "240-OHM"
    s = re.sub(r"^([\d.]+\s*-?\s*[Kk]?OHM)-.+$", r"\1", s, flags=re.IGNORECASE)

    s = s.replace("µ", "u").replace("Ω", "").strip()

    # Caps: <num><unit{pF,nF,uF,F}>
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(pF|nF|uF|F)", s, re.IGNORECASE)
    if m:
        num = float(m.group(1))
        unit = m.group(2).lower()
        # normalize: nF → uF, pF stays, F → 1 000 000 uF (rare, keep as-is)
        if unit == "nf":
            return f"{num / 1000:g}uF"
        if unit == "pf":
            return f"{num:g}pF"
        if unit == "uf":
            return f"{num:g}uF"
        return s

    # Resistors: 10k0 / 4k7 / 0R1 (Yageo-style decimal in unit position)
    m = re.fullmatch(r"(\d+)([RKkM])(\d+)", s)
    if m:
        whole, unit, frac = m.groups()
        num_val = float(f"{whole}.{frac}")
        unit_norm = unit.lower() if unit in ("k", "K") else unit.replace("R", "").replace("M", "M")
        if unit in ("R",):
            return f"{num_val:g}"
        return f"{num_val:g}{unit_norm}"

    # Resistors: 4.7k / 10k / 1M
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kKM])", s)
    if m:
        num, unit = m.groups()
        return f"{float(num):g}{unit.lower() if unit == 'K' else unit}"

    # Resistors with explicit OHM unit: 33-OHM, 150OHM, 10KOHM
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*-?\s*([Kk]?)OHM", s, re.IGNORECASE)
    if m:
        num, prefix = m.groups()
        if prefix.lower() == "k":
            return f"{float(num):g}k"
        return f"{float(num):g}"

    # Inductors: 1uH, 0.47uH, 15nH, 1.5mH
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([num])?H", s, re.IGNORECASE)
    if m:
        num, prefix = m.groups()
        prefix_norm = (prefix or "").lower()
        return f"{float(num):g}{prefix_norm}H"

    # Crystals/oscillators: 24MHz, 32.768kHz, 1GHz
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kMG])?Hz", s, re.IGNORECASE)
    if m:
        num, prefix = m.groups()
        if prefix:
            # k stays lowercase, M and G uppercase
            prefix_norm = "k" if prefix.upper() == "K" else prefix.upper()
        else:
            prefix_norm = ""
        return f"{float(num):g}{prefix_norm}Hz"

    # Voltages (zeners, TVS, etc.): 5.5V, 12V
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*V", s, re.IGNORECASE)
    if m:
        return f"{float(m.group(1)):g}V"

    # Resistors: bare number ohms (rare but happens — "47", "100", "0.00").
    # Normalize trailing zeros so "0.00" and "0" collapse to "0" (zero-ohm
    # jumpers are routinely written either way across pipelines).
    m = re.fullmatch(r"\d+(?:\.\d+)?", s)
    if m:
        return f"{float(s):g}"

    # Unknown form — passthrough the *stripped* form (annotations removed)
    # so MPN-like strings still benefit from the package/sheet/type strips.
    # Falls back to original raw when strips emptied the string.
    final = s.strip() or raw
    if final == raw:
        logger.warning("parts_index: unrecognized value form %r — passing through", raw)
    return final


def _canonicalize_package(package: str | None) -> str | None:
    """Normalize package strings so footprint-library variants collapse.

    Apple schematics dual-label imperial passives with library-variant
    suffixes: '0201-1', '0402-0.1MM', '01005-1'. The base footprint
    (0201, 0402, 01005) is what matters for stock substitution — a
    0201-1 donor fits a 0201 slot. KiCad and other tools emit just the
    base form. Collapsing the suffix lets cross-pipeline parts_index
    agree on the same key.
    """
    if not package:
        return None
    s = package.strip()
    # Apple variant suffix on imperial passives: <digits>-<variant>
    m = re.match(r"^(\d{4,5})-[\w.]+$", s)
    if m:
        return m.group(1)
    return s


def _criticality_from_boot_sequence(
    refdes: str, boot_sequence: list[dict]
) -> Literal["low", "medium", "high"]:
    """Map boot_sequence phase to criticality bucket.

    phase ≤ 2 → high (rails primaires, PMIC)
    phase 3-5 → medium
    absent or phase > 5 → low
    """
    for step in boot_sequence:
        if refdes in step.get("involved_refdes", []):
            phase = step.get("phase", 99)
            if phase <= 2:
                return "high"
            if phase <= 5:
                return "medium"
            return "low"
    return "low"


def _classifications_index(passive_classification: dict | None) -> dict[str, str | None]:
    """Build {refdes: role} from passive_classification_llm.json.

    The LLM artefact uses the `assignments` key (post-Phase-4 schema). Older
    fixtures and the synthetic test data use `classifications`. Tolerate both.
    """
    if not passive_classification:
        return {}
    items = (
        passive_classification.get("assignments")
        or passive_classification.get("classifications")
        or []
    )
    return {item["refdes"]: item.get("role") for item in items if "refdes" in item}


def _stable_hash(electrical_graph: dict) -> str:
    """SHA-256 of canonical-JSON-serialized electrical_graph."""
    canon = json.dumps(electrical_graph, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def build_parts_index(
    slug: str,
    electrical_graph: dict,
    passive_classification: dict | None = None,
    nets_classified: dict | None = None,
) -> PartsIndex:
    """Synthesise a PartsIndex from electrical_graph + classification artefacts.

    Pure function. The caller writes memory/{slug}/parts_index.json.
    """
    classifications = _classifications_index(passive_classification)
    boot_sequence = electrical_graph.get("boot_sequence", [])

    entries: dict[str, PartsIndexEntry] = {}
    for refdes, comp in electrical_graph.get("components", {}).items():
        comp_type = comp.get("type", "unknown")
        comp_kind = comp.get("kind", "ic")
        value = comp.get("value")

        # Role derivation. Two sources, in order:
        # 1. Heuristic role written by compile_electrical_graph into the
        #    component itself (covers ~85-90% of passives directly).
        # 2. LLM-filled role for the residual ambiguous cases — kept in
        #    passive_classification_llm.json:assignments.
        # ICs and connectors short-circuit to a fixed role.
        if comp_type == "ic":
            role = "ic"
        elif comp_type == "connector":
            role = "connector"
        else:
            role = comp.get("role") or classifications.get(refdes)

        safety = classify_safety(role=role, type=comp_type)

        entries[refdes] = PartsIndexEntry(
            refdes=refdes,
            type=comp_type,
            kind=comp_kind,
            value_canonical=_canonicalize_value(value),
            value_raw=(value.get("raw") if value else None),
            package=_canonicalize_package(value.get("package") if value else None),
            mpn=(value.get("mpn") if value else None),
            voltage_rating=_coerce_voltage_rating(value.get("voltage_rating") if value else None),
            tolerance=(value.get("tolerance") if value else None),
            role_in_design=role,
            safety_class=safety,
            criticality_in_design=_criticality_from_boot_sequence(refdes, boot_sequence),
            pages=comp.get("pages", []),
        )

    return PartsIndex(
        schema_version="1.0",
        device_slug=slug,
        generated_at=datetime.now(UTC),
        source_electrical_graph_hash=_stable_hash(electrical_graph),
        entries=entries,
    )
