"""Custom-tool dispatch registry for the managed diagnostic runtime.

Why this module exists
----------------------
``runtime_managed.py`` historically routed every custom tool the agent could
call (``profile_*``, ``bv_*``, ``mb_*`` and the protocol bridges) through one
~360-line ``_dispatch_tool`` if/elif waterfall. That made:

* every new tool a merge-conflict magnet (one giant function = one diff hot
  spot);
* the dispatcher impossible to unit-test in isolation (it captured the
  WebSocket task's locals);
* the call signature creep across ten positional/keyword arguments.

The fix is a thin **dispatch table**: each tool name maps to a coroutine
``handler(payload, ctx) -> dict``. The ``ctx`` (``ToolContext`` dataclass)
bundles the per-session deps the original closure relied on — ``device_slug``,
``memory_root``, ``client``, ``session``, ``session_id``, ``repair_id``,
``session_mirrors``, ``conv_id`` — so handlers can reach them without leaking
back into the runtime module.

Two pattern-prefix fallbacks remain identical to the legacy waterfall:

* ``profile_*`` → resolved against the small ``_PROFILE_HANDLERS`` table
  (kept separate so the ``api.profile.tools`` import is lazy);
* unknown ``bv_*`` → forwarded to ``dispatch_bv`` (the boardview catch-all).

Anything not matched returns the same structured error the legacy dispatcher
emitted (``{"ok": False, "reason": "unknown-tool", "error": "unknown tool: …"}``).

Zero behaviour change versus the pre-refactor ``_dispatch_tool`` — handlers
are byte-for-byte ports of the original branches.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from anthropic import AsyncAnthropic

from api.agent._session_mirrors import SessionMirrors
from api.agent.dispatch_bv import dispatch_bv
from api.agent.tools import (
    mb_expand_knowledge,
    mb_get_component,
    mb_get_rules_for_symptoms,
    mb_record_finding,
)
from api.session.state import SessionState
from api.tools.schematic import mb_schematic_graph


@dataclass
class ToolContext:
    """Per-session deps bundled together so handlers stay pure functions of
    ``(payload, ctx)`` instead of the original ten-positional-args waterfall.

    Field order intentionally mirrors the legacy ``_dispatch_tool`` signature
    so the runtime caller only needs to instantiate the dataclass and pass it
    in — no rewiring of the call site.
    """

    device_slug: str
    memory_root: Path
    client: AsyncAnthropic
    session: SessionState
    session_id: str | None = None
    repair_id: str | None = None
    session_mirrors: SessionMirrors | None = None
    conv_id: str | None = None


ToolHandler = Callable[[dict, ToolContext], Awaitable[dict]]


# --------------------------------------------------------------------------- #
# profile_* handlers
# --------------------------------------------------------------------------- #
# Kept inside a helper instead of exploded to module scope so the
# ``api.profile.tools`` import stays deferred (matches the legacy lazy
# import inside the if/elif body — avoids a circular import at module load).


async def _dispatch_profile(name: str, payload: dict, ctx: ToolContext) -> dict:
    from api.profile.tools import (
        profile_check_skills,
        profile_get,
        profile_track_skill,
    )

    if name == "profile_get":
        return profile_get(session=ctx.session)
    if name == "profile_check_skills":
        return profile_check_skills(payload.get("candidate_skills", []))
    if name == "profile_track_skill":
        return profile_track_skill(
            payload.get("skill_id", ""),
            payload.get("evidence", {}),
        )
    return {
        "ok": False,
        "reason": "unknown-tool",
        "error": f"unknown profile tool: {name}",
    }


# --------------------------------------------------------------------------- #
# bv_* protocol bridge handlers (the four protocol tools that need MA-side
# context — repair_id / conv_id / memory_root — beyond what dispatch_bv has)
# --------------------------------------------------------------------------- #


async def _bv_propose_protocol(payload: dict, ctx: ToolContext) -> dict:
    from api.tools.protocol import (
        StepInput as _SI,
    )
    from api.tools.protocol import (
        propose_protocol as _propose,
    )

    valid_refdes = (
        {p.refdes for p in ctx.session.board.parts}
        if ctx.session.board is not None
        else None
    )
    # Tolerate "comp:U7" / "rail:+5V" prefixes the agent learns from
    # mb_set_observation; strip "comp:" so the refdes validates, drop
    # rail/test-point prefixes into test_point so the step still anchors
    # somewhere meaningful even without a board part.
    for s in payload.get("steps", []) or []:
        t = s.get("target")
        if isinstance(t, str) and ":" in t:
            kind, _, rest = t.partition(":")
            if kind == "comp":
                s["target"] = rest
            else:
                s["target"] = None
                s.setdefault("test_point", t)
    try:
        step_inputs = [_SI.model_validate(s) for s in payload.get("steps", [])]
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "invalid_step_input", "detail": str(exc)}
    result = _propose(
        memory_root=ctx.memory_root,
        device_slug=ctx.device_slug,
        repair_id=ctx.repair_id or "",
        title=payload.get("title", ""),
        rationale=payload.get("rationale", ""),
        steps=step_inputs,
        rule_inspirations=payload.get("rule_inspirations") or None,
        valid_refdes=valid_refdes,
        conv_id=ctx.conv_id,
    )
    if result.get("ok"):
        from api.tools.protocol import load_active_protocol
        proto = load_active_protocol(
            ctx.memory_root, ctx.device_slug, ctx.repair_id or "", conv_id=ctx.conv_id,
        )
        if proto is not None:
            result["event"] = {
                "type": "protocol_proposed",
                "protocol_id": proto.protocol_id,
                "title": proto.title,
                "rationale": proto.rationale,
                "steps": [s.model_dump() for s in proto.steps],
                "current_step_id": proto.current_step_id,
            }
    return result


async def _bv_update_protocol(payload: dict, ctx: ToolContext) -> dict:
    from api.tools.protocol import StepInput as _SI
    from api.tools.protocol import update_protocol as _update

    new_step_payload = payload.get("new_step")
    new_step = None
    if new_step_payload is not None:
        try:
            new_step = _SI.model_validate(new_step_payload)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": "invalid_new_step", "detail": str(exc)}
    result = _update(
        memory_root=ctx.memory_root,
        device_slug=ctx.device_slug,
        repair_id=ctx.repair_id or "",
        action=payload.get("action", ""),
        reason=payload.get("reason", ""),
        step_id=payload.get("step_id"),
        after=payload.get("after"),
        new_step=new_step,
        new_order=payload.get("new_order"),
        verdict=payload.get("verdict"),
        conv_id=ctx.conv_id,
    )
    if result.get("ok"):
        from api.tools.protocol import load_active_protocol
        proto = load_active_protocol(
            ctx.memory_root, ctx.device_slug, ctx.repair_id or "", conv_id=ctx.conv_id,
        )
        history_tail = proto.history[-3:] if proto is not None else []
        result["event"] = {
            "type": "protocol_updated",
            "protocol_id": result.get("protocol_id"),
            "action": payload.get("action"),
            "current_step_id": result.get("current_step_id"),
            "steps": [s.model_dump() for s in (proto.steps if proto else [])],
            "history_tail": [h.model_dump() for h in history_tail],
        }
    return result


async def _bv_record_step_result(payload: dict, ctx: ToolContext) -> dict:
    from api.tools.protocol import record_step_result as _record
    result = _record(
        memory_root=ctx.memory_root,
        device_slug=ctx.device_slug,
        repair_id=ctx.repair_id or "",
        step_id=payload.get("step_id", ""),
        value=payload.get("value"),
        unit=payload.get("unit"),
        observation=payload.get("observation"),
        skip_reason=payload.get("skip_reason"),
        submitted_by="agent",
        conv_id=ctx.conv_id,
    )
    if result.get("ok"):
        from api.tools.protocol import load_active_protocol
        proto = load_active_protocol(
            ctx.memory_root, ctx.device_slug, ctx.repair_id or "", conv_id=ctx.conv_id,
        )
        history_tail = proto.history[-3:] if proto is not None else []
        result["event"] = {
            "type": "protocol_updated",
            "protocol_id": result.get("protocol_id"),
            "action": "step_completed",
            "current_step_id": result.get("current_step_id"),
            "steps": [s.model_dump() for s in (proto.steps if proto else [])],
            "history_tail": [h.model_dump() for h in history_tail],
        }
    return result


async def _bv_get_protocol(payload: dict, ctx: ToolContext) -> dict:
    from api.tools.protocol import load_active_protocol
    proto = load_active_protocol(
        ctx.memory_root, ctx.device_slug, ctx.repair_id or "", conv_id=ctx.conv_id,
    )
    if proto is None:
        return {"ok": True, "active": False}
    return {
        "ok": True, "active": True,
        "protocol_id": proto.protocol_id,
        "title": proto.title,
        "rationale": proto.rationale,
        "current_step_id": proto.current_step_id,
        "status": proto.status,
        "steps": [s.model_dump() for s in proto.steps],
        "history": [h.model_dump() for h in proto.history],
    }


# --------------------------------------------------------------------------- #
# mb_* handlers (memory bank + board aggregation + measurements + validation)
# --------------------------------------------------------------------------- #


async def _mb_get_component(payload: dict, ctx: ToolContext) -> dict:
    return mb_get_component(
        device_slug=ctx.device_slug,
        refdes=payload.get("refdes", ""),
        memory_root=ctx.memory_root,
        session=ctx.session,
    )


async def _mb_get_rules_for_symptoms(payload: dict, ctx: ToolContext) -> dict:
    return mb_get_rules_for_symptoms(
        device_slug=ctx.device_slug,
        symptoms=payload.get("symptoms", []),
        memory_root=ctx.memory_root,
        max_results=payload.get("max_results", 5),
        session=ctx.session,
    )


async def _mb_record_finding(payload: dict, ctx: ToolContext) -> dict:
    return await mb_record_finding(
        client=ctx.client,
        device_slug=ctx.device_slug,
        refdes=payload.get("refdes", ""),
        symptom=payload.get("symptom", ""),
        confirmed_cause=payload.get("confirmed_cause", ""),
        memory_root=ctx.memory_root,
        mechanism=payload.get("mechanism"),
        notes=payload.get("notes"),
        session_id=ctx.session_id,
    )


async def _mb_record_session_log(payload: dict, ctx: ToolContext) -> dict:
    from api.agent.tools import mb_record_session_log as _mb_session_log

    return await _mb_session_log(
        client=ctx.client,
        device_slug=ctx.device_slug,
        repair_id=ctx.repair_id or "",
        conv_id=ctx.conv_id or "",
        symptom=payload.get("symptom", ""),
        outcome=payload.get("outcome", "unresolved"),
        memory_root=ctx.memory_root,
        tested=payload.get("tested"),
        hypotheses=payload.get("hypotheses"),
        findings=payload.get("findings"),
        next_steps=payload.get("next_steps"),
        lesson=payload.get("lesson"),
    )


async def _mb_schematic_graph(payload: dict, ctx: ToolContext) -> dict:
    return mb_schematic_graph(
        device_slug=ctx.device_slug,
        memory_root=ctx.memory_root,
        query=payload.get("query", ""),
        label=payload.get("label"),
        refdes=payload.get("refdes"),
        index=payload.get("index"),
        domain=payload.get("domain"),
        killed_refdes=payload.get("killed_refdes"),
        failures=payload.get("failures"),
        rail_overrides=payload.get("rail_overrides"),
        session=ctx.session,
    )


async def _mb_hypothesize(payload: dict, ctx: ToolContext) -> dict:
    from api.tools.hypothesize import mb_hypothesize as _mb_hypothesize

    return _mb_hypothesize(
        device_slug=ctx.device_slug,
        memory_root=ctx.memory_root,
        state_comps=payload.get("state_comps"),
        state_rails=payload.get("state_rails"),
        metrics_comps=payload.get("metrics_comps"),
        metrics_rails=payload.get("metrics_rails"),
        max_results=payload.get("max_results", 5),
        # Always pass the session's repair_id — it scopes the
        # diagnosis_log.jsonl hook (for field-corpus calibration) and
        # is the fallback for journal-based synthesis when the agent
        # didn't supply explicit state/metrics.
        repair_id=payload.get("repair_id") or ctx.repair_id,
    )


async def _mb_record_measurement(payload: dict, ctx: ToolContext) -> dict:
    from api.tools.measurements import mb_record_measurement as _mb_rec

    return _mb_rec(
        device_slug=ctx.device_slug,
        repair_id=ctx.repair_id or "",
        memory_root=ctx.memory_root,
        target=payload.get("target", ""),
        value=payload.get("value", 0.0),
        unit=payload.get("unit", "V"),
        nominal=payload.get("nominal"),
        note=payload.get("note"),
        source="agent",
    )


async def _mb_list_measurements(payload: dict, ctx: ToolContext) -> dict:
    from api.tools.measurements import mb_list_measurements as _mb_list

    return _mb_list(
        device_slug=ctx.device_slug,
        repair_id=ctx.repair_id or "",
        memory_root=ctx.memory_root,
        target=payload.get("target"),
        since=payload.get("since"),
    )


async def _mb_compare_measurements(payload: dict, ctx: ToolContext) -> dict:
    from api.tools.measurements import mb_compare_measurements as _mb_cmp

    return _mb_cmp(
        device_slug=ctx.device_slug,
        repair_id=ctx.repair_id or "",
        memory_root=ctx.memory_root,
        target=payload.get("target", ""),
        before_ts=payload.get("before_ts"),
        after_ts=payload.get("after_ts"),
    )


async def _mb_observations_from_measurements(payload: dict, ctx: ToolContext) -> dict:
    from api.tools.measurements import mb_observations_from_measurements as _mb_syn

    return _mb_syn(
        device_slug=ctx.device_slug,
        repair_id=ctx.repair_id or "",
        memory_root=ctx.memory_root,
    )


async def _mb_set_observation(payload: dict, ctx: ToolContext) -> dict:
    from api.tools.measurements import mb_set_observation as _mb_set

    return _mb_set(
        device_slug=ctx.device_slug,
        repair_id=ctx.repair_id or "",
        memory_root=ctx.memory_root,
        target=payload.get("target", ""),
        mode=payload.get("mode", "unknown"),
    )


async def _mb_clear_observations(payload: dict, ctx: ToolContext) -> dict:
    from api.tools.measurements import mb_clear_observations as _mb_clr

    return _mb_clr(
        device_slug=ctx.device_slug,
        repair_id=ctx.repair_id or "",
        memory_root=ctx.memory_root,
    )


async def _mb_validate_finding(payload: dict, ctx: ToolContext) -> dict:
    from api.tools.validation import (
        mb_validate_finding as _mb_val,
    )
    from api.tools.validation import (
        mirror_outcome_to_memory,
    )

    result = _mb_val(
        device_slug=ctx.device_slug,
        repair_id=ctx.repair_id or "",
        memory_root=ctx.memory_root,
        fixes=payload.get("fixes", []),
        tech_note=payload.get("tech_note"),
        agent_confidence=payload.get("agent_confidence", "high"),
    )
    # Fire-and-forget: mirror the validated outcome into the device's
    # MA memory store so future repair sessions can `memory_search` it.
    # Kept off the critical path — the tool's response to the agent
    # doesn't wait for the HTTP upsert to complete.
    # session_mirrors ensures the task is awaited on WS close so a
    # fast disconnect doesn't cancel it mid-flight.
    if result.get("validated") and ctx.repair_id:
        if ctx.session_mirrors is None:
            raise RuntimeError(
                "mb_validate_finding dispatch requires session_mirrors; "
                "this path is only valid from run_diagnostic_session_managed"
            )
        ctx.session_mirrors.spawn(
            mirror_outcome_to_memory(
                client=ctx.client,
                device_slug=ctx.device_slug,
                repair_id=ctx.repair_id,
                memory_root=ctx.memory_root,
            )
        )
    return result


async def _mb_expand_knowledge(payload: dict, ctx: ToolContext) -> dict:
    return await mb_expand_knowledge(
        client=ctx.client,
        device_slug=ctx.device_slug,
        focus_symptoms=payload.get("focus_symptoms", []),
        focus_refdes=payload.get("focus_refdes", []),
        memory_root=ctx.memory_root,
        session=ctx.session,
    )


# --------------------------------------------------------------------------- #
# Registry — exact-name lookup table.
# --------------------------------------------------------------------------- #
# Order matches the legacy if/elif body so a future audit can diff this list
# against ``runtime_managed.py``'s git history without surprises.
_HANDLERS: dict[str, ToolHandler] = {
    # bv_* protocol bridges (intercepted before the generic dispatch_bv).
    "bv_propose_protocol": _bv_propose_protocol,
    "bv_update_protocol": _bv_update_protocol,
    "bv_record_step_result": _bv_record_step_result,
    "bv_get_protocol": _bv_get_protocol,
    # mb_* memory-bank surface.
    "mb_get_component": _mb_get_component,
    "mb_get_rules_for_symptoms": _mb_get_rules_for_symptoms,
    "mb_record_finding": _mb_record_finding,
    "mb_record_session_log": _mb_record_session_log,
    "mb_schematic_graph": _mb_schematic_graph,
    "mb_hypothesize": _mb_hypothesize,
    "mb_record_measurement": _mb_record_measurement,
    "mb_list_measurements": _mb_list_measurements,
    "mb_compare_measurements": _mb_compare_measurements,
    "mb_observations_from_measurements": _mb_observations_from_measurements,
    "mb_set_observation": _mb_set_observation,
    "mb_clear_observations": _mb_clear_observations,
    "mb_validate_finding": _mb_validate_finding,
    "mb_expand_knowledge": _mb_expand_knowledge,
}


async def dispatch_tool(name: str, payload: dict, ctx: ToolContext) -> dict:
    """Resolve ``name`` against the dispatch table and run the matching handler.

    Resolution order matches the legacy ``_dispatch_tool`` waterfall:

    1. ``profile_*`` → routed via ``_dispatch_profile`` (lazy import of
       ``api.profile.tools``).
    2. Exact-name match in ``_HANDLERS`` (covers all named ``mb_*`` and the
       four ``bv_*`` protocol bridges).
    3. Any remaining ``bv_*`` → forwarded to ``dispatch_bv`` (synchronous;
       the boardview catch-all).
    4. Anything else → structured ``unknown-tool`` error, logged at WARNING
       to mirror the legacy log line.
    """
    if name.startswith("profile_"):
        return await _dispatch_profile(name, payload, ctx)

    handler = _HANDLERS.get(name)
    if handler is not None:
        return await handler(payload, ctx)

    if name.startswith("bv_"):
        return dispatch_bv(ctx.session, name, payload)

    # Mirror the legacy WARNING so log-grep heuristics keep working.
    import logging
    logging.getLogger("wrench_board.agent.managed").warning(
        "unknown mb_* tool: %s", name,
    )
    return {
        "ok": False,
        "reason": "unknown-tool",
        "error": f"unknown tool: {name}",
    }
