# SPDX-License-Identifier: Apache-2.0
"""Compose a one-shot snapshot of a repair's hard, on-disk state.

Used to bootstrap a fresh Managed-Agents session with the technician's
real progress when MA's server-side event stream is gone (or never had
the data — e.g. brand-new conversation on an existing repair). Without
this block the agent would re-ask "have you measured anything yet?"
even though the tech has filled in three steps of a protocol and
produced 8 measurements on disk.

Sources are independent of MA — they live under
`memory/{slug}/repairs/{repair_id}/`:
- `measurements.jsonl` — every meter reading (`api/agent/measurement_memory`)
- `protocols/{pid}.json` (+ `protocol.json` pointer) — the active stepwise
  diagnostic plan with per-step results (`api/tools/protocol`)
- `outcome.json` — final validated fix when the repair is closed

The block is rendered as plain text addressed to the agent. The
`summary` dict mirrors the same counts in machine-readable form so the
WS layer can surface them in the chat panel's "context lost" alert
(reassures the tech that hard facts survived even when the agent's
conversational memory didn't).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from api.agent.measurement_memory import load_measurements
from api.tools.protocol import load_active_protocol

logger = logging.getLogger("wrench_board.agent.recovery_state")


_BLOCK_HEADER = "[ÉTAT REPAIR — faits persistés sur disque, indépendants de MA]"

# Cap measurement lines so the intro stays readable. The deterministic
# engines (simulator + hypothesize) consume the full journal anyway via
# `mb_*` tools — this snippet is just for the agent's situational awareness.
_MEASUREMENTS_TAIL_CAP = 12


def _format_measurement_line(ev: Any) -> str:
    """One short line per measurement, ordered by recency."""
    target = ev.target or "?"
    value = ev.value
    unit = ev.unit or ""
    mode = ev.auto_classified_mode
    note = ev.note or ""
    ts = (ev.timestamp or "")[:19].replace("T", " ")
    if value is None:
        # Placeholder events (mb_set_observation without a meter reading).
        body = f"{target} → mode={mode or '?'}"
    else:
        body = f"{target} = {value}{unit}"
        if mode:
            body += f" [{mode}]"
    if note:
        body += f" — {note[:60]}"
    return f"  - {ts} · {body}"


def _format_protocol_block(proto: Any) -> tuple[str, dict[str, Any]]:
    """Render the active protocol's progress as a compact block + summary."""
    steps = list(proto.steps or [])
    total = len(steps)
    completed = sum(
        1 for s in steps if s.result is not None or s.status == "completed"
    )
    current_id = proto.current_step_id
    current_idx = next(
        (i + 1 for i, s in enumerate(steps) if s.id == current_id), None
    )
    lines = [
        f"Protocole actif: « {proto.title} » (id={proto.protocol_id})",
        f"Progression: {completed}/{total} steps",
    ]
    if current_idx is not None:
        current_step = steps[current_idx - 1]
        lines.append(
            f"Step courant ({current_idx}/{total}, id={current_id}): "
            f"{current_step.instruction}"
        )
    if completed:
        lines.append("Steps complétés:")
        for step in steps:
            if step.result is None:
                continue
            res = step.result
            outcome = res.outcome
            target = step.target or step.test_point or "?"
            if step.type == "numeric" and res.value is not None:
                detail = f"{res.value}{res.unit or step.unit or ''}"
            elif step.type == "boolean":
                detail = "oui" if res.value else "non"
            elif step.type == "observation":
                obs = (res.observation or res.value or "")
                detail = str(obs)[:80]
            elif res.skip_reason:
                detail = f"skip ({res.skip_reason[:40]})"
            else:
                detail = "fait"
            lines.append(f"  - {step.id} ({target}): {detail} → {outcome}")
    return "\n".join(lines), {
        "id": proto.protocol_id,
        "title": proto.title,
        "completed": completed,
        "total": total,
        "current_step_id": current_id,
    }


def _load_outcome(memory_root: Path, device_slug: str, repair_id: str) -> dict[str, Any] | None:
    path = memory_root / device_slug / "repairs" / repair_id / "outcome.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "[RecoveryState] outcome.json unreadable for %s/%s: %s",
            device_slug, repair_id, exc,
        )
        return None


def build_repair_state_block(
    *,
    memory_root: Path,
    device_slug: str,
    repair_id: str | None,
    conv_id: str | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Render an `[ÉTAT REPAIR …]` block from on-disk artefacts.

    Returns `(block_text_or_None, summary_dict)`. Summary always has the
    `measurements` / `protocol` / `outcome` keys (counts / nested dict /
    bool). `block_text` is None when there's nothing on disk worth
    surfacing — caller should skip the section in that case rather than
    glue an empty header.
    """
    summary: dict[str, Any] = {
        "measurements": 0,
        "protocol": None,
        "outcome": False,
    }
    if not repair_id:
        return None, summary

    sections: list[str] = []

    measurements = load_measurements(
        memory_root=memory_root,
        device_slug=device_slug,
        repair_id=repair_id,
    )
    if measurements:
        summary["measurements"] = len(measurements)
        tail = measurements[-_MEASUREMENTS_TAIL_CAP:]
        elided = len(measurements) - len(tail)
        header = (
            f"Mesures persistées ({len(measurements)} total"
            + (f", {len(tail)} dernières affichées" if elided else "")
            + "):"
        )
        sections.append(
            "\n".join([header] + [_format_measurement_line(ev) for ev in tail])
        )

    try:
        proto = load_active_protocol(memory_root, device_slug, repair_id, conv_id=conv_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[RecoveryState] load_active_protocol failed for %s/%s: %s",
            device_slug, repair_id, exc,
        )
        proto = None
    if proto is not None:
        block, proto_summary = _format_protocol_block(proto)
        sections.append(block)
        summary["protocol"] = proto_summary

    outcome = _load_outcome(memory_root, device_slug, repair_id)
    if outcome:
        summary["outcome"] = True
        verdict = outcome.get("verdict") or outcome.get("status") or "validé"
        components = outcome.get("components") or outcome.get("fixes") or []
        comp_str = ", ".join(str(c) for c in components) if components else "—"
        sections.append(
            "Outcome final enregistré:\n"
            f"  - verdict: {verdict}\n"
            f"  - composants: {comp_str}"
        )

    if not sections:
        return None, summary

    body = (
        f"{_BLOCK_HEADER}\n"
        "Ces faits viennent du disque (measurements.jsonl, "
        "protocols/, outcome.json) — pas de la mémoire MA. "
        "Ils restent valides même si la session conversationnelle a été "
        "réinitialisée. Pars de là plutôt que de redemander au technicien "
        "ce qu'il a déjà mesuré.\n\n"
        + "\n\n".join(sections)
    )
    return body, summary
