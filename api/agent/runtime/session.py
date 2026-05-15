"""Main entry point: ``run_diagnostic_session_managed``.

Open a Managed Agents session on the tier-scoped agent, attach the
layered memory stores, replay the prior conversation when applicable,
and run the symmetric WS forwarders until the tech disconnects.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from api.agent import runtime_managed as _rm
from api.agent._session_mirrors import SessionMirrors as _SessionMirrors
from api.agent.chat_history import (
    build_ctx_tag,
    build_session_intro,
    load_ma_session_id,
    save_ma_session_id,
)
from api.agent.runtime import _aux
from api.agent.runtime._aux import (
    DEFAULT_TIER,
    TierLiteral,
    _active_diagnostic_keys,
    _build_log_id,
    _mirror_jsonl,
    _PendingConv,
    _sessions_create_with_retry,
    logger,
)
from api.agent.runtime.replay import (
    _replay_jsonl_history_to_ws,
    _replay_ma_history_to_ws,
)
from api.agent.session_start_mode import (
    SessionStartMode,
    decide_session_start_mode,
)
from api.session.state import SessionState


async def run_diagnostic_session_managed(
    ws: WebSocket,
    device_slug: str,
    tier: TierLiteral = DEFAULT_TIER,
    repair_id: str | None = None,
    conv_id: str | None = None,
) -> None:
    """Open a Managed Agents session on the tier-scoped agent and relay it to `ws`.

    `tier` picks which agent (fast=Haiku, normal=Sonnet, deep=Opus) handles the
    conversation. A new WS connection with a different tier = a fresh MA session
    on that tier's agent. No in-session swap: by design, tier choice is explicit
    and the user starts a new conversation when changing it.

    MA persists the full event stream server-side, so the happy-path replay
    on resume pulls from `client.beta.sessions.events.list(sid)` rather than
    from the local JSONL. The JSONL under `memory/{slug}/repairs/{repair_id}/
    conversations/{conv}/messages.jsonl` is still written live as an on-disk
    mirror — used for UI re-rendering on reconnect when MA's event stream is
    inaccessible (checkpoint expired after 30 d idle, Anthropic outage, etc.).
    That mirror is the reason JSONL keeps being written — the technician's
    repair history (chat bubbles in the UI) survives even if the managed
    session is gone. Semantic context for the agent now comes from the
    per-repair scribe mount instead of an LLM-summarized recap.
    """
    settings = _rm.get_settings()
    if not settings.anthropic_api_key:
        await ws.accept()
        await ws.send_json({
            "type": "error",
            "code": "missing_api_key",
            "text": "ANTHROPIC_API_KEY absente — configure-la dans .env puis relance le serveur.",
        })
        await ws.close()
        return

    try:
        ids = _rm.load_managed_ids()
        agent_info = _rm.get_agent(ids, tier)
    except RuntimeError as exc:
        await ws.accept()
        await ws.send_json({"type": "error", "text": str(exc)})
        await ws.close()
        return

    client = _rm.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=settings.anthropic_max_retries)  # noqa: E501
    session_mirrors = _SessionMirrors()
    memory_root = Path(settings.memory_root)

    # Layered MA memory — provision up to 4 stores per session:
    #   1. global-patterns   (RO) — cross-device failure archetypes
    #   2. global-playbooks  (RO) — protocol templates
    #   3. device-{slug}     (RW) — knowledge pack + field reports
    #   4. repair-{repair_id} (RW) — agent's working notes (scribe layer)
    # Each surfaces as /mnt/memory/<store-name>/ inside the session container.
    # See docs/superpowers/plans/2026-04-26-ma-memory-layered-architecture.md

    PATTERNS_DESC_RUNTIME = (
        "Cross-device failure archetypes for board-level diagnostics: "
        "short-to-GND on power rails, thermal cascade failures, BGA "
        "solder ball lift, bench anti-patterns. Markdown documents "
        "under /patterns/<id>.md. Read this store first when the "
        "device-specific rules return 0 matches."
    )
    PLAYBOOKS_DESC_RUNTIME = (
        "Diagnostic protocol templates conformant to bv_propose_protocol's "
        "schema. JSON documents under /playbooks/<id>.json indexed by "
        "symptom (boot-no-power, usb-no-charge, pmic-rail-collapse). "
        "Reference these BEFORE synthesizing a protocol from scratch — "
        "they are field-tested."
    )

    # Collect any store-provisioning failures so the WS layer can tell
    # the technician they're operating with a degraded memory layer.
    # Without this signal the agent silently runs without its scribe
    # mount or its global-patterns / global-playbooks references — the
    # tech would see "session ready" and have no idea memory was off.
    # Each entry: {"store": "device|repair|patterns|playbooks", "error": "<msg>"}.
    memory_setup_failures: list[dict[str, str]] = []

    async def _safe_ensure(store_label: str, coro):
        """Run an ensure_* coroutine; on failure record the error and return None.

        Memory stores are non-critical for session start — the agent can
        still function (custom mb_* tools also serve the same data via
        disk reads). But the technician needs to know so they can stop
        relying on cross-session continuity.
        """
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Diag-MA] memory store %s provision failed: %s — "
                "session continues with that layer disabled",
                store_label,
                exc,
            )
            memory_setup_failures.append(
                {"store": store_label, "error": str(exc)[:300]}
            )
            return None

    patterns_store_id = await _safe_ensure(
        "patterns",
        _rm.ensure_global_store(
            client, kind="patterns", description=PATTERNS_DESC_RUNTIME,
        ),
    ) if settings.ma_memory_store_enabled else None
    playbooks_store_id = await _safe_ensure(
        "playbooks",
        _rm.ensure_global_store(
            client, kind="playbooks", description=PLAYBOOKS_DESC_RUNTIME,
        ),
    ) if settings.ma_memory_store_enabled else None
    memory_store_id = await _safe_ensure(
        "device", _rm.ensure_memory_store(client, device_slug),
    )
    repair_store_id = await _safe_ensure(
        "repair",
        _rm.ensure_repair_store(
            client, device_slug=device_slug, repair_id=repair_id,
        ),
    ) if (repair_id and settings.ma_memory_store_enabled) else None

    await _rm.maybe_auto_seed(
        client=client,
        device_slug=device_slug,
        memory_root=memory_root,
        session_mirrors=session_mirrors,
    )
    session_state = SessionState.from_device(device_slug)

    # Resolve which conversation within the repair this WS targets. Anonymous
    # sessions (no repair_id) skip conversation tracking — MA still persists
    # server-side, but we can't index it without an owning repair. Lazy
    # materialization (`materialize=False`): when the resolution would create
    # a fresh conv, we get back a pre-allocated id but nothing is written
    # to disk yet — the slot only persists if the tech actually sends a
    # message. Without this, every "+ Nouvelle conversation" click and every
    # tier switch leaves a 0-turn entry behind. Materialization happens on
    # the first user.message via `pending_conv.materialize_now()`.
    resolved_conv_id: str | None = None
    pending_materialize = False
    conversation_count = 0
    if repair_id:
        resolved_conv_id, pending_materialize = _rm.ensure_conversation(
            device_slug=device_slug,
            repair_id=repair_id,
            conv_id=conv_id,
            tier=tier,
            memory_root=memory_root,
            materialize=False,
        )
        conversation_count = len(
            _rm.list_conversations(
                device_slug=device_slug,
                repair_id=repair_id,
                memory_root=memory_root,
            )
        )

    # Single-WS guard. The dedup of `responded_tool_ids` is per-forwarder,
    # so two WS that share an MA session (same triplet) would each respond
    # to the same `agent.custom_tool_use`; the second POST is rejected by
    # MA (HTTP 400, "waiting on responses to events …") and the stream
    # gets torn down. Reject the second WS at handshake time instead of
    # letting it crash later. The key is claimed BEFORE any further await
    # so a concurrent open can't slip through between the membership
    # check and the add (asyncio is single-threaded; both happen in one
    # uninterrupted scheduler step). Released in `finally` further down.
    #
    # Acquire policy: on contention, briefly wait for the sibling WS to
    # finish its teardown rather than rejecting outright. The frontend
    # routinely does close + immediate reconnect (tier auto-align on
    # session_ready, page reload while a prior session is still tearing
    # down), and the two sockets land on the server out of order under
    # asyncio scheduling. Without the wait, the second WS sees the guard
    # still claimed and bounces with `session_already_open` even though
    # the user only opened the panel once. Typical legit teardown is
    # <100 ms (forwarders cancel-and-await is bounded by
    # ma_forwarder_unwind_timeout_seconds, default 2 s); 5 s of waiting
    # comfortably absorbs that. Anything still claimed after 5 s is a
    # genuine concurrent double-open (two tabs / two browsers) and gets
    # the original rejection.
    diagnostic_key: tuple[str, str, str] | None = None
    if repair_id and resolved_conv_id:
        candidate_key = (device_slug, repair_id, resolved_conv_id)
        loop = asyncio.get_event_loop()
        # Read via the module so tests can monkeypatch
        # `_aux._GUARD_ACQUIRE_TIMEOUT_SECONDS` to a small value.
        wait_deadline = loop.time() + _aux._GUARD_ACQUIRE_TIMEOUT_SECONDS
        while candidate_key in _active_diagnostic_keys:
            if loop.time() >= wait_deadline:
                await ws.accept()
                await ws.send_json({
                    "type": "error",
                    "code": "session_already_open",
                    "text": (
                        "Another conversation is already open for this repair. "
                        "Close it before opening a new one."
                    ),
                })
                await ws.close(code=1008, reason="session already open")
                return
            await asyncio.sleep(0.05)
        _active_diagnostic_keys.add(candidate_key)
        diagnostic_key = candidate_key

    # Build session params. `resources` is the current (2026-04-01) surface
    # for attaching memory stores. We attach up to 4 layers (global patterns +
    # global playbooks + device + repair); any that returned None (beta off,
    # API failure, missing repair_id) is silently skipped.
    session_kwargs: dict[str, Any] = {
        "agent": {
            "type": "agent",
            "id": agent_info["id"],
            "version": agent_info["version"],
        },
        "environment_id": ids["environment_id"],
        "title": f"diag-{device_slug}-{tier}",
    }
    resources: list[dict] = []
    if patterns_store_id:
        resources.append({
            "type": "memory_store",
            "memory_store_id": patterns_store_id,
            "access": "read_only",
            "prompt": (
                "Global cross-device failure archetypes (short-to-GND, "
                "thermal cascades, BGA lift, bench anti-patterns). Grep "
                "here when the device-specific rules don't match the "
                "symptom — patterns often generalize across families."
            ),
        })
    if playbooks_store_id:
        resources.append({
            "type": "memory_store",
            "memory_store_id": playbooks_store_id,
            "access": "read_only",
            "prompt": (
                "Diagnostic protocol templates indexed by symptom. Before "
                "calling bv_propose_protocol, grep here for a matching "
                "playbook and prefer it over synthesizing one from scratch."
            ),
        })
    if memory_store_id:
        resources.append({
            "type": "memory_store",
            "memory_store_id": memory_store_id,
            "access": "read_only",
            "prompt": (
                "Knowledge pack + confirmed field reports for THIS device. "
                "/knowledge/* is pipeline-authored (registry, rules, etc.); "
                "/field_reports/* is mirrored from mb_record_finding — do "
                "NOT write directly here, use the tool for canonical "
                "findings (validation + format guarantees)."
            ),
        })
    if repair_store_id:
        resources.append({
            "type": "memory_store",
            "memory_store_id": repair_store_id,
            "access": "read_write",
            "prompt": (
                "Your scratch notebook for THIS repair, persisted across "
                "all sessions of the same repair_id. Read state.md at "
                "session start to orient yourself. Write decisions/{ts}.md "
                "when you validate or refute a hypothesis, append to "
                "measurements/{rail}.md when the tech reports a probed "
                "value, and edit open_questions.md for unresolved threads. "
                "Do NOT use this for chat narration or duplicates of "
                "field_reports/."
            ),
        })
    if resources:
        session_kwargs["resources"] = resources

    # Reuse the repair's previously-persisted MA session when possible —
    # that's how conversation context survives a WS close/reopen. Sessions
    # are keyed BY (CONV, TIER): each conversation owns its own MA session
    # id and each tier within a conversation has its own agent identity.
    # Pending convs (lazy-materialized) have no on-disk dir yet, so there's
    # no saved MA session id to look up — skip the read.
    reused_session_id = None
    if resolved_conv_id and not pending_materialize:
        reused_session_id = load_ma_session_id(
            device_slug=device_slug,
            repair_id=repair_id,
            conv_id=resolved_conv_id,
            tier=tier,
        )
    session = None
    # Classify the session-start path into one of five disjoint modes
    # (see api/agent/session_start_mode.py for the full table). The mode
    # drives the WS event contract — `context_lost` vs `session_resumed`
    # vs silent — and the recap-injection branch downstream. Centralizing
    # the decision here keeps the UI contract auditable instead of
    # reconstructing it from intermixed booleans 200 lines later.
    session = None
    retrieved_agent_id: str | None = None
    retrieve_failed = False
    if reused_session_id:
        try:
            session = await client.beta.sessions.retrieve(reused_session_id)
            session_agent = getattr(session, "agent", None)
            retrieved_agent_id = (
                getattr(session_agent, "id", None) if session_agent else None
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Diag-MA] could not retrieve session=%s (%s) — creating fresh",
                reused_session_id,
                exc,
            )
            retrieve_failed = True

    decision = decide_session_start_mode(
        reused_session_id=reused_session_id,
        retrieved_session_agent_id=retrieved_agent_id,
        current_agent_id=agent_info["id"],
        retrieve_failed=retrieve_failed,
    )
    start_mode = decision.mode
    if start_mode == SessionStartMode.RESUMED:
        logger.info(
            "[Diag-MA] Resuming existing session=%s for repair=%s conv=%s",
            reused_session_id,
            repair_id,
            resolved_conv_id,
        )
    elif start_mode == SessionStartMode.FRESH_RECOVERED_AGENT_BUMP:
        logger.info(
            "[Diag-MA] session=%s bound to stale agent=%s (current=%s) — "
            "forcing fresh session + silent recap",
            reused_session_id,
            retrieved_agent_id,
            agent_info["id"],
        )
        session = None  # discard the retrieved (stale-agent) session
    elif start_mode in (
        SessionStartMode.FRESH_RECOVERED_LOST,
        SessionStartMode.FRESH_NEW,
    ):
        # Either no prior id on disk, or retrieve failed — both lead to
        # the create branch below. session is already None.
        session = None

    # Back-compat aliases for the rest of the function — the boolean
    # forms are still threaded through replay decisions and intro
    # injection. Kept as derived views of `start_mode` so there's a
    # single source of truth.
    resumed = start_mode in (
        SessionStartMode.RESUMED,
        SessionStartMode.RESUMED_BUT_EMPTY,
    )
    stale_agent_recovery = (
        start_mode == SessionStartMode.FRESH_RECOVERED_AGENT_BUMP
    )

    if session is None:
        # The old MA session is gone (archived / expired) OR its bound agent
        # no longer matches the current one (overnight evolve bump). With the
        # per-repair scribe mount, the new agent self-orients by reading
        # state.md + decisions/ — no pre-session LLM summary call needed.
        try:
            session = await _sessions_create_with_retry(client, **session_kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Diag-MA] session create failed for device=%s", device_slug)
            await ws.accept()
            await ws.send_json({"type": "error", "text": f"session create failed: {exc}"})
            await ws.close()
            # Release the single-WS guard claimed up-stream so the next
            # tab open isn't permablocked by a transient session.create
            # failure (e.g. MA quota burst). Mirror release happens in
            # the function-final `finally`; this early return bypasses
            # it. discard() is a no-op if the key was never claimed
            # (anonymous WS path).
            if diagnostic_key is not None:
                _active_diagnostic_keys.discard(diagnostic_key)
            return
        # Save the link from this conv to the fresh MA session id NOW only
        # for already-materialized convs. Pending convs defer this until
        # `pending_conv.materialize_now()` runs on the first user message —
        # otherwise we'd write a `ma_session_<tier>.json` into a directory
        # whose parent index doesn't list this conv, leaving an orphan.
        if resolved_conv_id and not pending_materialize:
            save_ma_session_id(
                device_slug=device_slug,
                repair_id=repair_id,
                conv_id=resolved_conv_id,
                session_id=session.id,
                tier=tier,
            )

    pending_conv = _PendingConv(
        device_slug=device_slug,
        repair_id=repair_id,
        conv_id=resolved_conv_id,
        tier=tier,
        memory_root=memory_root,
        session_id=session.id,
        pending=pending_materialize,
    )

    # log_id pairs the session_id with a human-grep-friendly triplet so a
    # post-mortem can find an incident either by `sesn_…` or by
    # `repair:conv:tier`. Emitted once at session_start; downstream log
    # lines keep `session=%s` so existing log-grep alerts still work, and
    # an operator can pivot from one to the other via this anchor line.
    log_id = _build_log_id(repair_id, resolved_conv_id, tier)
    logger.info(
        "[Diag-MA] session_start log_id=%s session=%s device=%s "
        "tier=%s model=%s memory=%s resumed=%s",
        log_id,
        session.id,
        device_slug,
        tier,
        agent_info["model"],
        memory_store_id,
        resumed,
    )

    # Surface the conv's "preferred" tier (the one it was originally created
    # with) so the frontend can auto-align if the WS opened with a default
    # tier that doesn't match — e.g. tech reopens panel which defaults to
    # `fast`, lands on a Sonnet conv, and would otherwise silently see the
    # nearly-empty Haiku thread of that same conv instead of the real
    # Sonnet history.
    conv_tier_pref: str | None = None
    if resolved_conv_id and repair_id and not pending_materialize:
        conv_tier_pref = _rm.get_conversation_tier(
            device_slug=device_slug,
            repair_id=repair_id,
            conv_id=resolved_conv_id,
            memory_root=memory_root,
        )

    await ws.accept()
    await ws.send_json(
        {
            "type": "session_ready",
            "mode": "managed",
            "session_id": session.id,
            "memory_store_id": memory_store_id,
            "device_slug": device_slug,
            "tier": tier,
            "conv_tier": conv_tier_pref,
            "model": agent_info["model"],
            "board_loaded": session_state.board is not None,
            "repair_id": repair_id,
            "conv_id": resolved_conv_id,
            "conversation_count": conversation_count,
        }
    )
    if memory_setup_failures:
        # Tell the UI which memory layers came up degraded so the chat
        # banner can warn the tech that cross-session continuity is off
        # for this run. The session itself is healthy — we only emit
        # this when at least one ensure_* failed silently.
        await ws.send_json(
            {
                "type": "memory_store_setup_failed",
                "failures": memory_setup_failures,
            }
        )

    # Hydrate any active protocol so the UI panel rebuilds on reconnect.
    # When no protocol exists for this conv, push an explicit
    # `protocol_cleared` so the wizard sidebar drops any leftover state
    # from the previous conv (silence would have left the prior wizard
    # pinned on screen — same root cause as the boardview reset above).
    if repair_id:
        from api.tools.protocol import load_active_protocol as _lap
        active = _lap(memory_root, device_slug, repair_id or "", conv_id=resolved_conv_id)
        if active is not None:
            await ws.send_json({
                "type": "protocol_proposed",
                "protocol_id": active.protocol_id,
                "title": active.title,
                "rationale": active.rationale,
                "steps": [s.model_dump(mode="json") for s in active.steps],
                "current_step_id": active.current_step_id,
                "replay": True,
            })
        else:
            await ws.send_json({"type": "protocol_cleared"})

    # Hydrate the boardview overlay (highlights, focus, annotations,
    # dim, layer flip) from the per-repair snapshot. This survives MA
    # archiving the conv: even if the agent's chat memory is gone, the
    # board still shows the same components highlighted / annotated as
    # before the reload — the visual state IS the on-disk truth, not
    # something that has to be reconstructed from MA events. Apply it
    # to the live SessionState too so the next bv_* dispatch sees the
    # restored overlay rather than silently overwriting it.
    if repair_id:
        from api.agent.board_state import load_board_state, replay_board_state_to_ws
        # Always wipe the renderer's overlay first so a switch from a
        # heavily-annotated conv to a fresh one shows a clean board. Without
        # this, brd_viewer keeps the previous conv's highlights / annotations
        # / focus on screen because the per-conv backend has nothing to
        # send for the new conv (and silence ≠ "clear what was there").
        await ws.send_json({"type": "boardview.reset_view"})
        snapshot = load_board_state(
            memory_root=memory_root,
            device_slug=device_slug,
            repair_id=repair_id,
            conv_id=resolved_conv_id,
        )
        if snapshot:
            session_state.restore_view(snapshot)
            sent = await replay_board_state_to_ws(ws, snapshot)
            if sent:
                logger.info(
                    "[Diag-MA] replayed boardview state for repair=%s conv=%s (%d events)",
                    repair_id, resolved_conv_id, sent,
                )

    # The intro (device context + reported symptom + technician profile) only
    # needs injection on a FRESH session. When we resume, the MA session
    # already carries the full conversation history including the original intro.
    # Fresh sessions get the device intro PLUS the technician profile block.
    # On a recovered fresh session (old MA session expired), the agent reads
    # state.md / decisions/ from the per-repair scribe mount on its first
    # turn — no pre-cuisined LLM summary is injected here.
    # MA stores the intro as one hidden user message prefixed to the first real
    # turn (see _forward_ws_to_session's pending_intro handling).
    intro: str | None
    state_summary: dict[str, Any] = {"measurements": 0, "protocol": None, "outcome": False}
    if resumed:
        intro = None
        # Even when MA resumes cleanly, compute the summary so we can ship
        # it in any later context_lost emitted from the replay path.
        from api.agent.recovery_state import build_repair_state_block as _brsb
        _, state_summary = _brsb(
            memory_root=memory_root, device_slug=device_slug, repair_id=repair_id,
            conv_id=resolved_conv_id,
        )
    else:
        from api.agent.recovery_state import build_repair_state_block
        from api.profile.prompt import render_technician_block
        from api.profile.store import load_profile

        device_intro = build_session_intro(device_slug=device_slug, repair_id=repair_id)
        tech_block = render_technician_block(load_profile())
        # Hard-fact snapshot from disk (measurements + protocol + outcome).
        # Surfaces what the tech actually has on record so a fresh MA agent
        # doesn't redo work or re-ask measurements that already exist on
        # disk. Critical when the prior MA session was lost (cf. context_lost
        # path) — without this the agent is back to "tell me your symptom"
        # even though 8 measurements + a 5-step protocol survive.
        state_block, state_summary = build_repair_state_block(
            memory_root=memory_root, device_slug=device_slug, repair_id=repair_id,
            conv_id=resolved_conv_id,
        )
        parts: list[str] = []
        if device_intro:
            parts.append(device_intro)
        if state_block:
            parts.append(state_block)
        parts.append(f"[TECHNICIAN CONTEXT]\n{tech_block}")
        intro = "\n\n---\n\n".join(parts) if parts else None

    # Per-turn context tag — prepended to EVERY user message so smaller models
    # (Haiku in particular) keep the device + symptom in their foreground even
    # on terse follow-ups like "salut" / "ok" after a resume. ~25 tokens, stable
    # prefix so prompt caching covers it after the first turn.
    ctx_tag = build_ctx_tag(
        device_slug=device_slug, repair_id=repair_id, memory_root=memory_root
    )
    if reused_session_id and not resumed:
        # The old MA session is gone (archived / expired) or its agent was
        # bumped overnight. The new session has no native memory of the
        # prior turns, but the agent will read state.md / decisions/ from
        # the per-repair scribe mount on its first turn and self-orient.
        # Tell the tech we created a fresh session so they don't assume
        # the agent remembers the live in-conv chat (it doesn't — it
        # remembers what was scribed to the mount).
        if not stale_agent_recovery:
            await ws.send_json(
                {
                    "type": "context_lost",
                    "old_session_id": reused_session_id,
                    "new_session_id": session.id,
                    "preserved": state_summary,
                }
            )
        else:
            logger.info(
                "[Diag-MA] silent agent-bump (stale agent_id) — fresh "
                "session=%s for repair=%s conv=%s, agent will self-orient "
                "from scribe mount",
                session.id,
                repair_id,
                resolved_conv_id,
            )
        logger.warning(
            "[Diag-MA] context_lost emitted for repair=%s conv=%s — old "
            "session=%s archived and no JSONL backup; new agent starts blank",
            repair_id,
            resolved_conv_id,
            reused_session_id,
        )
    if intro:
        await ws.send_json(
            {
                "type": "context_loaded",
                "device_slug": device_slug,
                "repair_id": repair_id,
            }
        )
        logger.info(
            "[Diag-MA] Stashed session intro for repair=%s (awaiting tech input)",
            repair_id,
        )
    # Replay the chat history from local JSONL when we just created a fresh
    # MA session AND we have a transcript on disk. Without this, the silent
    # agent-bump path (bootstrap reload → stale agent_id → fresh session)
    # leaves the chat panel empty even though the conv has 37 lines on
    # disk. Symmetric with the `if resumed:` MA-events replay below — the
    # tech never has to guess whether their conversation history is
    # actually visible based on which recovery path the runtime took.
    if not resumed and repair_id and resolved_conv_id:
        replayed_local = await _replay_jsonl_history_to_ws(
            ws,
            device_slug=device_slug,
            repair_id=repair_id,
            conv_id=resolved_conv_id,
            memory_root=memory_root,
            session_state=session_state,
        )
        if replayed_local:
            logger.info(
                "[Diag-MA] replayed chat from local JSONL for fresh session "
                "(repair=%s conv=%s)",
                repair_id, resolved_conv_id,
            )
    if resumed:
        await ws.send_json(
            {
                "type": "session_resumed",
                "session_id": session.id,
                "repair_id": repair_id,
            }
        )
        # Replay the MA session's past events so the UI chat panel rebuilds
        # the conversation visually. Also replays per-turn costs from the
        # span.model_request_end events MA stores alongside so the lifetime
        # cost chip survives the reopen.
        replayed_anything = await _replay_ma_history_to_ws(
            ws,
            client,
            session.id,
            session_state,
            agent_info["model"],
            device_slug=device_slug,
            repair_id=repair_id,
            conv_id=resolved_conv_id,
            memory_root=memory_root,
        )
        # If MA's events.list returned empty AND there was no JSONL backup
        # to replay, the resumed session is alive in name only — its
        # internal context has likely been compacted/dropped. The chat
        # panel showing nothing is a lie unless we tell the tech the agent
        # is effectively starting fresh. Emit `context_lost` so the
        # frontend renders an explicit alert card.
        if not replayed_anything:
            # Promote the start mode to the post-replay observation:
            # session retrieved fine, agent_id matched, but the event
            # log was empty. This is a runtime fact, not a startup
            # decision — `decide_session_start_mode` cannot return this
            # value because it doesn't have replay information. Logging
            # the transition keeps the audit trail intact.
            start_mode = SessionStartMode.RESUMED_BUT_EMPTY
            logger.info(
                "[Diag-MA] start_mode promoted to RESUMED_BUT_EMPTY for "
                "session=%s repair=%s — agent has no conversational "
                "history, will be primed with state block",
                session.id,
                repair_id,
            )
            # The resumed MA session is alive but empty (events.list returned
            # only metadata, no JSONL backup). The agent has no
            # conversational history. Inject the on-disk state snapshot as
            # a synthetic user.message so it has the hard facts (mesures,
            # protocol progress, outcome) before the tech's next turn —
            # otherwise the agent would re-ask measurements that already
            # exist on disk. The intro path was skipped because we entered
            # via the resumed=True branch, so this is the only chance to
            # prime the agent.
            from api.agent.recovery_state import build_repair_state_block as _brsb

            state_block_now, _ = _brsb(
                memory_root=memory_root,
                device_slug=device_slug,
                repair_id=repair_id,
                conv_id=resolved_conv_id,
            )
            if state_block_now:
                try:
                    await client.beta.sessions.events.send(
                        session.id,
                        events=[{
                            "type": "user.message",
                            "content": [{"type": "text", "text": state_block_now}],
                        }],
                    )
                    _mirror_jsonl(
                        device_slug=device_slug,
                        repair_id=repair_id,
                        conv_id=resolved_conv_id,
                        memory_root=memory_root,
                        event={
                            "role": "user",
                            "content": [{"type": "text", "text": state_block_now}],
                        },
                    )
                    logger.info(
                        "[Diag-MA] context_lost recovery — pushed state block "
                        "(%d measurements, protocol=%s, outcome=%s) to fresh agent",
                        state_summary["measurements"],
                        bool(state_summary["protocol"]),
                        state_summary["outcome"],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[Diag-MA] failed to push state block on context_lost: %s",
                        exc,
                    )
            await ws.send_json(
                {
                    "type": "context_lost",
                    "old_session_id": session.id,
                    "new_session_id": session.id,
                    "reason": "ma_events_empty",
                    "preserved": state_summary,
                }
            )
            logger.warning(
                "[Diag-MA] context_lost (resumed but empty) for repair=%s "
                "conv=%s session=%s — events.list returned 0 and no JSONL "
                "backup; agent will respond as if starting fresh",
                repair_id,
                resolved_conv_id,
                session.id,
            )

    # Cache: agent.custom_tool_use events by event.id, so we can look up
    # name+input when `requires_action` arrives and only hands us event_ids.
    events_by_id: dict[str, Any] = {}

    from api.tools.measurements import set_ws_emitter
    from api.tools.validation import set_ws_emitter as set_validation_emitter

    def _emit(event: dict) -> None:
        # Route through session_mirrors instead of bare asyncio.create_task
        # so the send is awaited on session close. Bare create_task left the
        # task orphan: a fast WS close would tear down the session before the
        # frame hit the wire, and the technician would never see the
        # measurement / validation event the agent had just acknowledged.
        # Bonus: spawn() already wires a done callback that surfaces
        # exceptions instead of letting them die silently in the loop.
        session_mirrors.spawn(ws.send_json(event))

    set_ws_emitter(_emit)
    set_validation_emitter(_emit)

    try:
        recv_task = asyncio.create_task(
            _rm._forward_ws_to_session(
                ws,
                client,
                session.id,
                pending_intro=intro,
                ctx_tag=ctx_tag,
                repair_id=repair_id,
                device_slug=device_slug,
                conv_id=resolved_conv_id,
                memory_root=memory_root,
                pending_conv=pending_conv,
                session_state=session_state,
            ),
            name="ws->session",
        )
        emit_task = asyncio.create_task(
            _rm._forward_session_to_ws(
                ws,
                client,
                session.id,
                device_slug,
                memory_root,
                events_by_id,
                session_state,
                agent_info["model"],
                tier=tier,
                environment_id=ids["environment_id"],
                repair_id=repair_id,
                conv_id=resolved_conv_id,
                session_mirrors=session_mirrors,
                pending_conv=pending_conv,
            ),
            name="session->ws",
        )
        done, pending = await asyncio.wait(
            {recv_task, emit_task}, return_when=asyncio.FIRST_COMPLETED
        )
        # Wait for cancelled forwarder tasks to actually unwind before the
        # finally block tears down the global emitters. Without this await,
        # a recv_task interrupted mid-`ws.receive_text()` can race with the
        # `set_ws_emitter(None)` cleanup: the cancellation propagates while
        # _emit is still being invoked from a measurement tool, leading to
        # writes on a torn-down WS.
        #
        # Per-task cancel + bounded wait (instead of one global gather) so:
        #   - Each forwarder gets its own per-task unwind budget — a slow
        #     task can't starve a clean-finishing one out of the shared
        #     window the previous gather provided. The default budget
        #     (settings.ma_forwarder_unwind_timeout_seconds) is 2 s.
        #   - A task that ignores its cancel is logged BY NAME, so the
        #     operator can map "did not unwind" to recv vs emit when
        #     reading a session teardown trace.
        # asyncio.wait() is preferred over wait_for() here: it never
        # raises CancelledError or TimeoutError on the awaited tasks
        # (it just returns them in the pending set), so a task that
        # observed its cancel and re-raised does not produce a noisy
        # exception path during teardown.
        forwarder_unwind_timeout = (
            _rm.get_settings().ma_forwarder_unwind_timeout_seconds
        )
        for task in pending:
            task.cancel()
            _, unwind_pending = await asyncio.wait({task}, timeout=forwarder_unwind_timeout)
            if unwind_pending:
                logger.warning(
                    "[Diag-MA] forwarder task %s did not unwind within "
                    "%.1fs after cancel — session=%s; proceeding with "
                    "teardown",
                    task.get_name(),
                    forwarder_unwind_timeout,
                    session.id,
                )
                continue
            # Task unwound: surface any non-cancellation exception so
            # a forwarder that died with an unexpected error during
            # the cancel path is visible in the logs.
            try:
                exc = task.exception()
            except asyncio.CancelledError:
                # Expected unwind path — nothing to log.
                continue
            if exc is not None and not isinstance(
                exc, (asyncio.CancelledError, WebSocketDisconnect)
            ):
                logger.warning(
                    "[Diag-MA] forwarder task %s raised during unwind: "
                    "%s — session=%s; proceeding with teardown",
                    task.get_name(),
                    exc,
                    session.id,
                )
        # Surface exceptions from the completed task to the logger. A WS close
        # (code 1000 normal, 1012 service restart) raised inside a forwarder task
        # is expected — log it as INFO, not ERROR with a stacktrace.
        for task in done:
            exc = task.exception()
            if exc is None:
                continue
            if isinstance(exc, WebSocketDisconnect):
                logger.info(
                    "[Diag-MA] task %s finished on WS disconnect code=%s",
                    task.get_name(),
                    getattr(exc, "code", "?"),
                )
            else:
                logger.exception(
                    "[Diag-MA] task %s raised",
                    task.get_name(),
                    exc_info=exc,
                )
    except WebSocketDisconnect:
        logger.info("[Diag-MA] WS disconnected for device=%s", device_slug)
    finally:
        # Release the single-WS guard FIRST, before the slow teardown steps
        # below. The forwarders were already cancelled-and-awaited above
        # (lines 854-866), so by the time we hit this finally there is no
        # more code on this coroutine that can race with a sibling WS
        # against the MA session. Keeping the release at the end of the
        # finally caused page-reload races: the frontend's tier auto-align
        # in session_ready closes WS1 and immediately reconnects WS2 on the
        # correct tier; WS2 used to land while WS1 was still inside
        # wait_drain + events.send (up to ~7 s total), and the guard
        # rejected WS2 with `session_already_open`. Clearing the key
        # immediately removes that window — the next reconnect on the same
        # triplet can claim cleanly.
        if diagnostic_key is not None:
            _active_diagnostic_keys.discard(diagnostic_key)
            diagnostic_key = None
        # Drain pending mirror tasks before tearing down the session so a
        # fast WS close doesn't cancel a mirror mid-flight. Default budget
        # (settings.ma_session_drain_timeout_seconds) is 5 s.
        await session_mirrors.wait_drain()
        # DO NOT archive: we want this session reusable on the next reopen
        # so the tech picks up the conversation where they left off. MA
        # keeps idle sessions alive (checkpoint TTL ~30 days per the beta
        # docs). We only interrupt in case the stream was mid-turn, so the
        # next connection doesn't inherit a stuck session_status_running.
        try:
            await client.beta.sessions.events.send(
                session.id,
                events=[{"type": "user.interrupt"}],
            )
        except Exception:  # noqa: BLE001 — best-effort
            pass
        set_ws_emitter(None)
        set_validation_emitter(None)
