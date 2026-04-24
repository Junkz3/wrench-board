# SPDX-License-Identifier: Apache-2.0
"""`mb_*` custom tools for the hackathon diagnostic agent.

Deliberately simple: prefix-letter closest-matches (no Levenshtein at this
layer — the boardview validator keeps the distance-based version for refdes
typos on a parsed board). Reads straight from disk on every call; caching is
a Phase-D concern.

mb_record_finding and mb_list_findings power cross-session memory: every
confirmed repair becomes a field report on disk, and the next session on the
same device can surface prior learnings without depending on the MA memory
store research preview.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from api.agent.field_reports import list_field_reports, record_field_report
from api.board.validator import suggest_similar
from api.session.state import SessionState


def _load_pack(
    slug: str,
    memory_root: Path,
    session: SessionState | None = None,
) -> dict[str, Any]:
    pack_dir = memory_root / slug
    paths = (
        pack_dir / "registry.json",
        pack_dir / "dictionary.json",
        pack_dir / "rules.json",
    )
    try:
        max_mtime = max(p.stat().st_mtime for p in paths)
    except FileNotFoundError:
        # Propagate the original read error so callers fail the same way.
        return {
            "registry": json.loads(paths[0].read_text()),
            "dictionary": json.loads(paths[1].read_text()),
            "rules": json.loads(paths[2].read_text()),
        }

    if session is not None:
        cached = session.pack_cache.get(slug)
        if cached is not None and cached[0] >= max_mtime:
            return cached[1]

    pack = {
        "registry": json.loads(paths[0].read_text()),
        "dictionary": json.loads(paths[1].read_text()),
        "rules": json.loads(paths[2].read_text()),
    }
    if session is not None:
        session.pack_cache[slug] = (max_mtime, pack)
    return pack


def mb_get_component(
    *,
    device_slug: str,
    refdes: str,
    memory_root: Path,
    session: SessionState | None = None,
) -> dict[str, Any]:
    """Return component info, aggregated from memory bank + parsed board.

    Response shape (cf. spec §5.1, 4 presence cases):
      - case 1: {found: true, canonical_name, memory_bank: {...}, board: {...}}
      - case 2: {found: true, canonical_name, memory_bank: {...}, board: null}
      - case 3: {found: true, canonical_name, memory_bank: null, board: {...}}
      - case 4: {found: false, closest_matches: [...]}  # no memory_bank/board keys
    """
    pack = _load_pack(device_slug, memory_root, session=session)
    reg_by_name = {c["canonical_name"]: c for c in pack["registry"].get("components", [])}
    dct_by_name = {e["canonical_name"]: e for e in pack["dictionary"].get("entries", [])}

    memory_section: dict[str, Any] | None = None
    if refdes in reg_by_name:
        reg = reg_by_name[refdes]
        dct = dct_by_name.get(refdes, {})
        memory_section = {
            "role": dct.get("role"),
            "package": dct.get("package"),
            "aliases": reg.get("aliases", []),
            "kind": reg.get("kind", "unknown"),
            "typical_failure_modes": dct.get("typical_failure_modes", []),
            "description": reg.get("description", ""),
        }

    board_section: dict[str, Any] | None = None
    if session is not None and session.board is not None:
        part = session.board.part_by_refdes(refdes)
        if part is not None:
            pin_indexes = set(part.pin_refs)
            connected_nets: list[str] = []
            for net in session.board.nets:
                if set(net.pin_refs) & pin_indexes:
                    connected_nets.append(net.name)
            side = "top" if part.layer & 1 else "bottom"
            bbox = part.bbox
            board_section = {
                "side": side,
                "pin_count": len(part.pin_refs),
                "bbox": [[bbox[0].x, bbox[0].y], [bbox[1].x, bbox[1].y]],
                "nets": connected_nets,
            }

    if memory_section is None and board_section is None:
        # Case 4: unknown on both sides. Union of candidates.
        prefix = refdes[0].upper() if refdes else ""
        mem_candidates = sorted(c for c in reg_by_name if prefix and c.startswith(prefix))
        board_candidates: list[str] = []
        if session is not None and session.board is not None:
            board_candidates = suggest_similar(session.board, refdes, k=5)
        merged = list(dict.fromkeys(mem_candidates + board_candidates))[:5]
        return {
            "found": False,
            "error": "not_found",
            "queried_refdes": refdes,
            "closest_matches": merged,
            "hint": f"No refdes {refdes!r} on device {device_slug!r}.",
        }

    return {
        "found": True,
        "canonical_name": refdes,
        "memory_bank": memory_section,
        "board": board_section,
    }


def mb_get_rules_for_symptoms(
    *,
    device_slug: str,
    symptoms: list[str],
    memory_root: Path,
    max_results: int = 5,
    session: SessionState | None = None,
) -> dict[str, Any]:
    """Return rules whose symptoms overlap the query, ranked by overlap + confidence."""
    pack = _load_pack(device_slug, memory_root, session=session)
    qset = {s.lower() for s in symptoms}
    matches: list[dict[str, Any]] = []
    for rule in pack["rules"].get("rules", []):
        rset = {s.lower() for s in rule.get("symptoms", [])}
        overlap = qset & rset
        if not overlap:
            continue
        matches.append(
            {
                "rule_id": rule["id"],
                "overlap_count": len(overlap),
                "symptoms_matched": sorted(overlap),
                "likely_causes": rule.get("likely_causes", []),
                "diagnostic_steps": rule.get("diagnostic_steps", []),
                "confidence": rule.get("confidence", 0.5),
                "sources": rule.get("sources", []),
            }
        )
    matches.sort(key=lambda m: (m["overlap_count"], m["confidence"]), reverse=True)
    return {
        "device_slug": device_slug,
        "query_symptoms": symptoms,
        "matches": matches[: max(max_results, 0)],
        "total_available_rules": len(pack["rules"].get("rules", [])),
    }


async def mb_record_finding(
    *,
    client,  # AsyncAnthropic | None — typed loose to keep this import-light
    device_slug: str,
    refdes: str,
    symptom: str,
    confirmed_cause: str,
    memory_root: Path,
    mechanism: str | None = None,
    notes: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Persist a confirmed repair finding for cross-session learning.

    JSON-first write to `memory/{slug}/field_reports/*.md`. When the MA
    memory_stores flag is on, the same content is mirrored to the device's
    memory store so native `memory_search` can surface it too.
    """
    return await record_field_report(
        client=client,
        device_slug=device_slug,
        refdes=refdes,
        symptom=symptom,
        confirmed_cause=confirmed_cause,
        mechanism=mechanism,
        notes=notes,
        session_id=session_id,
        memory_root=memory_root,
    )


def mb_list_findings(
    *,
    device_slug: str,
    memory_root: Path,
    limit: int = 20,
    filter_refdes: str | None = None,
) -> dict[str, Any]:
    """Return prior confirmed findings for this device, newest first."""
    reports = list_field_reports(
        device_slug=device_slug,
        memory_root=memory_root,
        limit=limit,
        filter_refdes=filter_refdes,
    )
    return {
        "device_slug": device_slug,
        "count": len(reports),
        "filter_refdes": filter_refdes,
        "reports": reports,
    }


async def mb_expand_knowledge(
    *,
    client,  # AsyncAnthropic — kept loose-typed for import hygiene
    device_slug: str,
    focus_symptoms: list[str],
    focus_refdes: list[str] | None = None,
    memory_root: Path | None = None,
    session: SessionState | None = None,
) -> dict[str, Any]:
    """Grow the pack's memory bank around a focus symptom area.

    Invoked when mb_get_rules_for_symptoms returns 0 matches and the
    technician's symptom is worth researching. Triggers a targeted
    Scout + Registry + Clinicien mini-pipeline and merges the output
    into the on-disk pack. Costs ~$0.40 / ~30-60s. Returns a summary
    the agent can relay to the tech, then the agent can re-call
    mb_get_rules_for_symptoms to see the freshly added rules.
    """
    from api.pipeline.expansion import expand_pack

    try:
        summary = await expand_pack(
            device_slug=device_slug,
            focus_symptoms=focus_symptoms,
            focus_refdes=focus_refdes or [],
            client=client,
            memory_root=memory_root,
        )
        summary["ok"] = True
        if session is not None:
            session.invalidate_pack_cache(device_slug)
        return summary
    except Exception as exc:  # noqa: BLE001 — defensive: never crash the session
        return {
            "ok": False,
            "expanded": False,
            "reason": type(exc).__name__,
            "error": str(exc)[:300],
        }
