// Diagnostic agent panel — WS client to /ws/diagnostic/{device_slug}.
// The panel is push-mode: when open, body.llm-open is set and the main
// content zones shrink 420px on the right.
//
// Wire protocol (matches api/agent/runtime_{managed,direct}.py):
//   send: {type: "message", text: "..."}
//         {type: "client.capabilities", camera_available, ...}     (Files+Vision)
//         {type: "client.upload_macro", base64, mime, filename}    (Flow A)
//         {type: "client.capture_response", request_id, base64,    (Flow B)
//                  mime, device_label}
//         {type: "client.protocol_confirmation", tool_use_id,      (Pattern 4)
//                  decision: "accept"|"reject", reason?}
//   recv: {type: "session_ready", mode, device_slug, session_id?, memory_store_id?}
//         {type: "message", role: "assistant", text}
//         {type: "tool_use", name, input}
//         {type: "thinking", text}                 (managed mode only)
//         {type: "error", text}
//         {type: "session_terminated"}
//         {type: "server.capture_request", request_id, tool_use_id, reason}
//         {type: "server.upload_macro_error", reason}
//         {type: "protocol_pending_confirmation", tool_use_id, title, …}
//         {type: "protocol_confirmation_timeout", tool_use_id}
//
// Activated by ⌘/Ctrl+J and by clicking the topbar "Agent" button.

import {
  selectedCameraDeviceId,
  selectedCameraLabel,
} from './camera.js';
import {
  closePreview,
  isPreviewOpen,
  openPreview,
} from './camera_preview.js';
import { mountMascot, setMascotState } from './mascot.js';
import { escapeHtml as escapeHTML } from './shared/dom.js';
import { getDeviceSlug, getRepairId } from './shared/context.js';
import { connectDiagnostic } from './services/diagnosticSocket.js';
// Same ?v=quest4 query main.js uses — ESM keys modules by URL, so a bare
// './protocol.js' would create a second instance with its own state, missing
// the Protocol.init() wiring main.js applied.
import * as Protocol from './protocol.js?v=quest4';
// SimulationController owns the schematic observation UI; we mirror agent
// measurement events onto it. Same ?v=fitzoom query main.js uses (single
// module instance). schematic.js does not import llm.js → no cycle.
import { SimulationController } from './schematic.js?v=fitzoom';
import { store } from './store.js';
import {
  TOOL_PHRASES,
  MEMORY_TOOL_PHRASES,
  toolFallback,
  memToolFallback,
} from './features/repair/diagnostic/toolPhrases.js';
import {
  fmtUsd,
  updateCostTotal,
  resetCost,
  recordTurnCost,
} from './features/repair/diagnostic/costDisplay.js';
import {
  sendCapabilities,
  handleMacroUpload,
  handleCaptureRequest,
} from './features/repair/diagnostic/filesVision.js';
import {
  logMessage,
  logSys,
  renderContextLost,
  renderResumeSummary,
  appendProtocolSystemEvent,
  renderInlineProtocolCard,
  ensureTurn,
  closeTurn,
  getCurrentTurn,
  ensurePendingNode,
  appendStep,
  addExpandToStep,
  appendTurnMessage,
  appendTurnFoot,
} from './features/repair/diagnostic/chatLog.js';

let ws = null;
// Route scope the live socket was dialed with ({slug, repairId}). openPanel()
// compares it to the CURRENT route: SPA navigation repair A → repair B never
// closes the old socket, and a live socket used to short-circuit the
// reconnect — leaving the panel (and every typed message) bound to repair A.
let wsScope = null;
let currentTier = "deep";
// Cached <svg.mascot> mounted into #llmMascot at panel-fragment init time.
// `setPanelMascot()` is the single chokepoint for animating it from WS events
// and form submission — null-safe when the fragment hasn't loaded yet.
let panelMascot = null;
let _errorRecoveryT = null;
let _typingHoldT = null;
function _clearMascotTimers() {
  if (_errorRecoveryT) { clearTimeout(_errorRecoveryT); _errorRecoveryT = null; }
  if (_typingHoldT) { clearTimeout(_typingHoldT); _typingHoldT = null; }
}
function setPanelMascot(state) {
  if (!panelMascot) return;
  _clearMascotTimers();
  setMascotState(panelMascot, state);
  if (state === "error") {
    _errorRecoveryT = setTimeout(() => {
      setMascotState(panelMascot, "idle");
      _errorRecoveryT = null;
    }, 1800);
  }
}
// The WS delivers the assistant message as one block (no token stream), so
// "typing" wouldn't otherwise be visible. Flash it for a readable window when
// the answer lands, then settle to idle — unless the turn does more work
// (a tool_use flips it to "working", cancelling this hold).
function flashPanelMascotTyping() {
  if (!panelMascot) return;
  _clearMascotTimers();
  setMascotState(panelMascot, "typing");
  _typingHoldT = setTimeout(() => {
    _typingHoldT = null;
    setMascotState(panelMascot, "idle");
  }, 1800);
}
// True once the tech has explicitly chosen a tier this page-load (clicked
// the popover, or any path that calls switchTier). Until that happens,
// session_ready may auto-realign currentTier with the resumed conv's
// preferred tier — opening a Sonnet/Haiku conv shouldn't silently land on
// its almost-empty Opus thread because the URL default was `deep`.
let userPickedTier = false;
// Multi-conversation state. `currentConvId` is captured from session_ready.
// `conversationsCache` backs the popover render. `pendingConvParam` is the
// ?conv value to use on the next connect() — "new" to force a fresh conv,
// a concrete id to target an existing one, null to let the backend resolve
// to the active conv.
let currentConvId = null;
let conversationsCache = [];
let pendingConvParam = null;
// Auto-reconnect after an UNEXPECTED socket drop (idle-cut by an upstream proxy,
// a brief network blip, a cloud redeploy). The cloud relay now keep-alives the
// tunnel, so these are rare — but when one slips through we resume the same
// conversation transparently instead of stranding the tech on "erreur socket"
// with a manual reload. Voluntary closes (tier/conv switch, route change, panel
// teardown) reassign or null the module `ws`, so the closing socket is no longer
// the live one and we DON'T reconnect it. Capped exponential backoff; after the
// last attempt we surface the error and stop.
let _reconnectT = null;
let _reconnectAttempts = 0;
const _RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 15000];
function _clearReconnect() {
  if (_reconnectT) { clearTimeout(_reconnectT); _reconnectT = null; }
  _reconnectAttempts = 0;
}
import { ICON_CHECK } from './icons.js';

function el(id) { return document.getElementById(id); }

function statusTone(tone, label) {
  const s = el("llmStatus");
  s.classList.remove("connecting", "connected", "closed", "error");
  if (tone) s.classList.add(tone);
  el("llmStatusText").textContent = label;
}

function safeJSON(v) {
  try { return JSON.stringify(v ?? {}); } catch { return String(v); }
}

function currentDeviceSlug() {
  return getDeviceSlug();
}

function currentRepairId() {
  return getRepairId();
}

function setSendEnabled(enabled) {
  el("llmSend").disabled = !enabled;
  el("llmStop").disabled = !enabled;
}

// Interrupt the live agent turn. The server translates this into an
// official `user.interrupt` session event (see
// https://platform.claude.com/docs/en/managed-agents/events-and-streaming).
// MA guarantees the agent halts mid-execution; the session stays alive so
// the tech can keep typing right after without reconnecting.
function interruptAgent() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  logSys(t('chat.session.interrupt_sent'));
  try {
    ws.send(JSON.stringify({ type: "interrupt" }));
  } catch (err) {
    console.warn("interrupt send failed", err);
  }
}

function connect() {
  // Any fresh dial supersedes a queued reconnect (tier/conv switch, route
  // change all funnel through here).
  _clearReconnect();
  const slug = currentDeviceSlug();
  if (!slug) {
    console.warn("[llm] connect() called without ?device= in the URL — aborting.");
    return;
  }
  const repairId = currentRepairId();
  // The shipped example repair is READ-ONLY (the cloud refuses its agent WS to
  // protect quota/credits). Don't even dial — surface a notice instead of a
  // silent failed socket. Defense-in-depth; the security boundary is the cloud.
  if (repairId && String(repairId).startsWith("example-")) {
    statusTone("closed", t('chat.status.idle'));
    logSys(t('chat.demo.read_only'));
    setSendEnabled(false);
    return;
  }
  el("llmDevice").textContent = repairId
    ? t('chat.session.device_label_with_repair', { slug, repair: repairId.slice(0, 8) })
    : t('chat.session.device_label_simple', { slug });
  el("llmDevice").style.display = "";
  const title = el("llmTitle");
  if (title) {
    const human = slug.replace(/[-_]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    title.textContent = human;
  }
  // New connection = new cost scope. Replayed history doesn't re-bill so we
  // reset here and let live turns accumulate fresh.
  resetCost();
  closeTurn();
  currentConvId = null;
  // Clear the log — the next session_ready / history_replay_start will
  // rebuild the right content. Without this, switching conv or tier
  // appends the replayed history below the old conv's visible messages.
  const log = el("llmLog");
  if (log) {
    log.innerHTML = "";
    log.classList.remove("replay");
  }
  updateCostTotal();
  const conv = pendingConvParam;
  pendingConvParam = null;  // consume after this connect
  statusTone("connecting", t('chat.status.connecting', { slug, tier: currentTier }));

  try {
    wsScope = { slug, repairId: repairId || null };
    ws = connectDiagnostic(
      slug,
      { tier: currentTier, repairId, conv },
      {
        onOpen: () => {
          // A clean open clears any pending reconnect: we're live again.
          _clearReconnect();
          statusTone("connected", t('chat.status.connected', { slug, tier: currentTier }));
          setSendEnabled(true);
          // Files+Vision : announce camera availability so the backend gates
          // cam_capture in the manifest (runtime_direct) and can short-circuit
          // empty captures (managed runtime).
          sendCapabilities();
        },
        onClose: (ev) => {
          statusTone("closed", t('chat.status.closed'));
          setSendEnabled(false);
          setPanelMascot("idle");
          // Reconnect only if THIS is still the live socket (a voluntary close
          // reassigned/nulled `ws` first) — see scheduleReconnect for the rest
          // of the guards (panel open + same route).
          if (ev && ev.target === ws) scheduleReconnect();
        },
        onError: () => {
          statusTone("error", t('chat.status.error_socket'));
          setSendEnabled(false);
          setPanelMascot("error");
        },
      },
    );
  } catch (err) {
    statusTone("error", t('chat.status.url_invalid'));
    logSys(t('chat.send.connect_failed', { error: err.message }), true);
    return;
  }

  ws.addEventListener("message", ev => {
    let payload;
    try { payload = JSON.parse(ev.data); }
    catch { payload = { type: "message", role: "assistant", text: ev.data }; }
    handleDiagnosticFrame(payload);
  });
}

// Schedule a reconnect after an unexpected drop. Bails (no reconnect) when the
// panel is closed (reopening dials fresh) or the live route has moved on since
// the socket was dialed — reconnecting then would resume the WRONG session.
// Otherwise it redials on a capped backoff, resuming the SAME conversation
// (pendingConvParam = currentConvId), and gives up after the last delay.
function scheduleReconnect() {
  if (_reconnectT) return; // a retry is already queued
  const panelOpen = el("llmPanel")?.classList.contains("open");
  const sameRoute = wsScope
    && wsScope.slug === currentDeviceSlug()
    && wsScope.repairId === (currentRepairId() || null);
  if (!panelOpen || !sameRoute) return;
  if (_reconnectAttempts >= _RECONNECT_DELAYS_MS.length) {
    // Out of attempts — leave the tech on the socket error so a manual reload
    // is the clear next step.
    statusTone("error", t('chat.status.error_socket'));
    return;
  }
  const delay = _RECONNECT_DELAYS_MS[_reconnectAttempts++];
  statusTone("connecting", t('chat.status.connecting', { slug: currentDeviceSlug(), tier: currentTier }));
  _reconnectT = setTimeout(() => {
    _reconnectT = null;
    // Resume the same thread; connect() consumes pendingConvParam then resets
    // currentConvId, so capture it here first.
    pendingConvParam = currentConvId || null;
    ws = null;
    connect();
  }, delay);
}

// Dispatch a single diagnostic-WS frame (boardview/protocol/simulation routing
// + the main type switch). Extracted from the live socket listener so the
// onboarding demo replayer can feed recorded frames through the EXACT same
// rendering path. All helpers + module state it references (ws, currentTier,
// currentConvId, pendingConvParam, Protocol, window.Boardview, el, logSys,
// recordTurnCost, setPanelMascot, …) are module-scoped, so it behaves
// identically whether driven by a live frame or a replayed one.
export function handleDiagnosticFrame(payload) {
    // Boardview events are visual mutations — not chat content. Route them
    // to the renderer (or its pending buffer if the renderer hasn't mounted).
    if (typeof payload.type === "string" && payload.type.startsWith("boardview.")) {
      window.Boardview.apply(payload);
      return;
    }

    // Protocol events drive the stepwise diagnostic wizard. Route them to
    // the Protocol module which owns state + renderer (protocol.js). When
    // no board is loaded, also render an inline card in the chat stream
    // (Mode C) so the tech still sees the active step + form even without
    // the wizard column visible.
    if (typeof payload.type === "string" && payload.type.startsWith("protocol_")) {
      Protocol.applyEvent(payload);
      // Surface terminal-state chips in the chat stream regardless of board
      // mode — abandon and completion are session-level events the tech
      // should see in their scrollback. Reason is the human-supplied
      // textarea entry from the abandon modal (or "tech_dismiss" default).
      if (payload.type === "protocol_updated") {
        if (payload.action === "abandoned" || payload.status === "abandoned") {
          appendProtocolSystemEvent("abandoned", {
            protocol_id: payload.protocol_id,
            reason: payload.reason || payload.history_tail?.slice(-1)?.[0]?.reason,
          });
        } else if (payload.status === "completed") {
          appendProtocolSystemEvent("completed", {
            protocol_id: payload.protocol_id,
          });
        }
      } else if (payload.type === "protocol_completed") {
        appendProtocolSystemEvent("completed", {
          protocol_id: payload.protocol_id,
        });
      }
      // Pending confirmation + timeout drive the modal only — do not render
      // an inline card in the chat stream (the modal is a global blocker).
      const isModalOnly = (
        payload.type === "protocol_pending_confirmation" ||
        payload.type === "protocol_confirmation_timeout"
      );
      const noBoard = !window.Boardview?.hasBoard?.();
      if (!isModalOnly && noBoard && payload.type !== "protocol_completed") {
        renderInlineProtocolCard(payload);
      }
      return;
    }

    // Simulation observation events mirror the agent's measurement tools
    // onto the schematic UI in real time. Same one-way channel, different
    // controller (SimulationController lives in schematic.js).
    if (typeof payload.type === "string" && payload.type.startsWith("simulation.")) {
      const SC = SimulationController;
      if (payload.type === "simulation.observation_set" && SC) {
        const parsed = (typeof payload.target === "string" && payload.target.includes(":"))
          ? payload.target.split(":", 2) : [null, null];
        const kind = parsed[0] === "rail" ? "rail" : parsed[0] === "comp" ? "comp" : null;
        const key = parsed[1];
        if (kind && key) SC.setObservation(kind, key, payload.mode, payload.measurement);
      } else if (payload.type === "simulation.observation_clear" && SC) {
        SC.clearObservations();
      } else if (payload.type === "simulation.repair_validated") {
        const btn = document.getElementById("dashboardFixBtn");
        if (btn) {
          // Clear any pending safety timeout from the dashboard-side.
          if (btn._fixTimeoutId) { clearTimeout(btn._fixTimeoutId); btn._fixTimeoutId = null; }
          const n = payload.fixes_count || 1;
          btn.innerHTML = ICON_CHECK + " " + escapeHTML(t(n > 1 ? 'chat.fix.validated_many' : 'chat.fix.validated_one', { n }));
          btn.classList.add("is-validated");
          btn.disabled = true;
        }
      }
      return;
    }

    switch (payload.type) {
      case "session_ready": {
        const model = payload.model || "claude";
        const mode = payload.mode || "managed";
        const repairShort = payload.repair_id ? payload.repair_id.slice(0, 8) : null;
        const sub = el("llmSubline");
        if (sub) {
          sub.textContent = repairShort
            ? t('chat.session.subline_with_repair', { model, mode, repair: repairShort })
            : t('chat.session.subline', { model, mode });
        }
        logSys(repairShort
          ? t('chat.session.ready_with_repair', { mode, model, repair: repairShort })
          : t('chat.session.ready', { mode, model }));
        currentConvId = payload.conv_id || null;
        loadConversations();
        // Auto-align tier with the resumed conv when the tech hasn't
        // explicitly picked one this session AND the conv was created on
        // a different tier. Without this, defaulting to fast/Haiku silently
        // resumed the (almost empty) per-tier thread of a Sonnet conv —
        // the user saw "0 messages" on a 31-turn conversation because
        // they were looking at the wrong thread.
        const convTier = payload.conv_tier;
        if (
          convTier &&
          convTier !== currentTier &&
          !userPickedTier &&
          ["fast", "normal", "deep"].includes(convTier)
        ) {
          logSys(t('chat.session.tier_auto_align', { tier: convTier }));
          // Mirror switchTier logic but skip the "user-chose" mark so a
          // future explicit tier pick still gates this auto-align.
          currentTier = convTier;
          const chip = el("llmTierChip");
          if (chip) {
            chip.dataset.tier = convTier;
            const label = chip.querySelector(".tier-label");
            if (label) label.textContent = convTier.toUpperCase();
          }
          document.querySelectorAll(".llm-tier-popover button[data-tier]").forEach(btn => {
            btn.classList.toggle("on", btn.dataset.tier === convTier);
          });
          if (ws && ws.readyState <= 1) {
            try { ws.close(); } catch (_) { /* ignore */ }
          }
          ws = null;
          // Keep the same conv_id on reconnect so we land on the right thread.
          pendingConvParam = currentConvId || null;
          connect();
        }
        break;
      }
      case "history_replay_start":
        el("llmLog").classList.add("replay");
        logSys(t('chat.session.replay_count', { n: payload.count }));
        break;
      case "history_replay_end":
        el("llmLog").classList.remove("replay");
        logSys(t('chat.session.replay_done'));
        closeTurn();
        break;
      case "context_loaded":
        logSys(t('chat.session.context_loaded'));
        break;
      case "session_resumed":
        logSys(t('chat.session.session_resumed'));
        break;
      case "session_resumed_summary":
        renderResumeSummary(payload);
        break;
      case "context_lost":
        // The Anthropic Managed Agents session has been silently dropped /
        // compacted by the beta backend, AND we had no local JSONL backup
        // to summarize. The freshly created session has no memory of the
        // prior turns — anything the tech asks now will be answered as if
        // it's the first turn of the conversation. Without this card the
        // chat panel pretends nothing happened and the tech wastes minutes
        // wondering why the agent forgot the symptom they discussed.
        renderContextLost(payload);
        break;
      case "message":
        if ((payload.role || "assistant") === "user") {
          closeTurn();
          logMessage("user", payload.text || "", payload.replay === true);
        } else {
          const turn = ensureTurn();
          appendTurnMessage(turn, payload.text || "");
          if (payload.replay !== true) flashPanelMascotTyping();
        }
        break;
      case "tool_use": {
        const turn = ensureTurn();
        const name = payload.name || "?";
        const kind = name.startsWith("bv_") ? "bv" :
                     name.startsWith("stock_") ? "stock" :
                     name.startsWith("mb_") ? "mb" : "mb";
        const renderer = TOOL_PHRASES[name];
        const { icon, phraseHTML, group } = renderer ? renderer(payload.input || {}) : toolFallback(name);
        const step = appendStep(turn, kind, `${icon}${phraseHTML}`, group);
        const payloadJSON = {
          args: payload.input || {},
          ...(payload.result != null ? { result: payload.result } : {}),
        };
        addExpandToStep(step, payloadJSON);
        setPanelMascot("working");
        break;
      }
      case "memory_tool_use": {
        // MA-native filesystem ops on /mnt/memory/{slug}/. These can arrive
        // after the assistant's message (next agent inference step starting),
        // so open a fresh turn in that case — otherwise the new step would
        // render in the rail above the message.
        let turn = ensureTurn();
        if (turn.querySelector(".turn-message")) {
          closeTurn();
          turn = ensureTurn();
        }
        const name = payload.name || "?";
        const renderer = MEMORY_TOOL_PHRASES[name];
        const { icon, phraseHTML, group } = renderer ? renderer(payload.input || {}) : memToolFallback(name);
        const step = appendStep(turn, "mem", `${icon}${phraseHTML}`, group);
        addExpandToStep(step, { args: payload.input || {} });
        setPanelMascot("working");
        break;
      }
      case "thinking": {
        const turn = ensureTurn();
        appendStep(turn, "thinking", escapeHTML(payload.text || "…"));
        setPanelMascot("thinking");
        break;
      }
      case "turn_cost": {
        recordTurnCost(payload);
        const ct = getCurrentTurn();
        if (ct) appendTurnFoot(ct, payload);
        // Offline demo replay has no live conversation list to refresh — skip
        // the network round-trip (it would 404-flood on the demo repair id).
        if (!_demoOffline) {
          clearTimeout(window._llmConvRefreshT);
          window._llmConvRefreshT = setTimeout(() => loadConversations(), 500);
        }
        break;
      }
      case "error":
        logSys(t('chat.error.generic', { text: payload.text }), true);
        // If the dashboard fix button is pending, clear its spinner so the
        // tech can retry instead of staring at "… Claude valide" forever.
        // The dashboard subscribes to this store key (it owns the button).
        store.set("fixButtonReset", true);
        setPanelMascot("error");
        break;
      case "stream_error":
        // Terminal: the engine ended the turn (Anthropic API error — e.g. a
        // spending-limit 400 — or a stream stall) and will NOT stream more.
        // Without this the "thinking" mascot spins forever (the symptom Alex
        // hit). Surface the message, close the turn, settle the indicator.
        // The engine sends `message` (not `text`); fall back across both.
        logSys(t('chat.error.generic', { text: payload.message || payload.text || '' }), true);
        store.set("fixButtonReset", true);
        closeTurn();
        setPanelMascot("error");
        break;
      case "session_terminated":
        logSys(t('chat.session.session_terminated'), true);
        closeTurn();
        setPanelMascot("idle");
        break;
      case "server.capture_request":
        // Flow B: agent called cam_capture. Snap from the metabar-selected
        // device and post back client.capture_response (success or empty).
        handleCaptureRequest(payload).catch((err) => {
          console.error("capture handler crash", err);
        });
        break;
      case "server.upload_macro_error":
        logSys(t('chat.upload.rejected', { reason: payload.reason || t('chat.error.unknown_reason') }), true);
        break;
      case "turn_complete":
        // Internal signal for benchmarks (end of an agent tech-turn). UI
        // doesn't render it — turn boundaries are already conveyed by the
        // turn_cost foot and the next user message. Don't cut a just-started
        // typing flash short; its own timer settles to idle.
        if (!_typingHoldT) setPanelMascot("idle");
        break;
      default:
        // Unknown WS event type — typically means the backend grew a new
        // signal that the frontend handler list hasn't caught up with, OR
        // the browser is serving a stale cached llm.js. Either way, raw
        // JSON in the chat log is meaningless to the tech (looked like
        // garbage `? {...}` rows in production). Surface to the dev console
        // for debug instead so the chat stream stays readable.
        console.warn("[llm] unhandled WS event:", payload?.type, payload);
    }
}

// ── Demo replay (offline) ────────────────────────────────────────────────
// When the onboarding plays a recorded session, there is NO live socket and no
// real repair: frames are fed straight to handleDiagnosticFrame. This flag tells
// the dispatcher to skip live-only side-effects (conversation-list refresh).
let _demoOffline = false;
export function setDemoOffline(on) { _demoOffline = !!on; }

// Open the chat panel chrome for a replay — WITHOUT dialing a WebSocket. Clears
// the log + cost scope so the recorded turns render into a clean panel.
export function openPanelForReplay() {
  resetCost();
  closeTurn();
  currentConvId = null;
  const log = el("llmLog");
  if (log) { log.innerHTML = ""; log.classList.remove("replay"); }
  updateCostTotal();
  el("llmPanel").classList.add("open");
  el("llmPanel").setAttribute("aria-hidden", "false");
  document.body.classList.add("llm-open");
  el("llmToggle").classList.add("on");
}

// `targetConv` (optional): "new" or an existing conv id. When set, the panel
// opens directly on that conversation in a single connect() — avoids the
// double-connect race where openPanel() first lands on the active conv and
// switchConv() then immediately reconnects to the right one (which spawned
// useless 0-turn slots before the lazy-materialize landing).
export function openPanel(targetConv) {
  if (targetConv && targetConv !== currentConvId) {
    pendingConvParam = targetConv;
    if (ws && ws.readyState <= 1) {
      try { ws.close(); } catch (_) { /* ignore */ }
    }
    ws = null;
  }
  // Stale-route guard: SPA navigation never tears the socket down, so a live
  // socket may still be bound to the PREVIOUS repair/device. Reusing it would
  // show — and send into — the other repair's conversation. Close + redial.
  if (ws && ws.readyState <= 1 && wsScope
      && (wsScope.slug !== currentDeviceSlug()
          || wsScope.repairId !== (currentRepairId() || null))) {
    try { ws.close(); } catch (_) { /* ignore */ }
    ws = null;
  }
  el("llmPanel").classList.add("open");
  el("llmPanel").setAttribute("aria-hidden", "false");
  document.body.classList.add("llm-open");
  el("llmToggle").classList.add("on");
  if (!ws || ws.readyState === WebSocket.CLOSED) connect();
  setTimeout(() => el("llmInput").focus(), 50);
}

function closePanel() {
  el("llmPanel").classList.remove("open");
  el("llmPanel").setAttribute("aria-hidden", "true");
  document.body.classList.remove("llm-open");
  el("llmToggle").classList.remove("on");
}

// Force-close the chat panel and tear down the live WS when the conversation
// it points at has just been deleted from the dashboard. Prevents the panel
// from sitting on a dead conv_id and trying to send to a now-404 session.
export function closePanelIfConv(convId) {
  if (!convId || convId !== currentConvId) return;
  _clearReconnect();
  if (ws && ws.readyState <= 1) {
    try { ws.close(); } catch (_) { /* ignore */ }
  }
  ws = null;
  currentConvId = null;
  if (el("llmPanel").classList.contains("open")) closePanel();
}

function togglePanel() {
  if (el("llmPanel").classList.contains("open")) closePanel();
  else openPanel();
}

function switchTier(newTier) {
  if (newTier === currentTier) return;
  // Mark as explicit user choice — disables the conv_tier auto-align that
  // session_ready otherwise applies on default landings.
  userPickedTier = true;
  currentTier = newTier;
  const chip = el("llmTierChip");
  if (chip) {
    chip.dataset.tier = newTier;
    const label = chip.querySelector(".tier-label");
    if (label) label.textContent = newTier.toUpperCase();
  }
  document.querySelectorAll(".llm-tier-popover button[data-tier]").forEach(btn => {
    btn.classList.toggle("on", btn.dataset.tier === newTier);
  });
  logSys(t('chat.session.tier_switch', { tier: newTier }));
  if (ws && ws.readyState <= 1) {
    try { ws.close(); } catch (_) { /* ignore */ }
  }
  ws = null;
  pendingConvParam = "new";  // new tier = new conversation
  connect();
}

// Auto-open the panel when the URL carries ?repair=<id>. Called from the
// main bootstrap so that clicking a repair card on Home lands the user
// directly in the conversation — no extra click needed.
export function openLLMPanelIfRepairParam() {
  const rid = currentRepairId();
  const slug = getDeviceSlug();
  if (rid && slug) {
    // Defer one frame so the DOM is definitely wired (openPanel touches
    // llmInput, llmToggle, etc.) and the status bar has mounted.
    requestAnimationFrame(() => openPanel());
  }
}

// ============ Conversation switcher helpers ============

async function loadConversations() {
  const rid = currentRepairId();
  if (!rid) { conversationsCache = []; renderConvItems(); return; }
  try {
    const res = await fetch(`/pipeline/repairs/${encodeURIComponent(rid)}/conversations`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    conversationsCache = Array.isArray(data.conversations) ? data.conversations : [];
    renderConvItems();
  } catch (err) {
    console.warn("[llm] loadConversations failed", err);
  }
}

async function deleteConvFromPanel(convId) {
  const rid = currentRepairId();
  if (!rid || !convId) return;
  let res;
  try {
    res = await fetch(
      `/pipeline/repairs/${encodeURIComponent(rid)}/conversations/${encodeURIComponent(convId)}`,
      { method: "DELETE" },
    );
  } catch (_) {
    logSys(t('chat.conv.delete_failed'));
    return;
  }
  if (!res.ok) {
    logSys(t('chat.conv.delete_failed'));
    return;
  }
  const wasCurrent = convId === currentConvId;
  conversationsCache = conversationsCache.filter(c => c.id !== convId);

  if (wasCurrent) {
    if (ws && ws.readyState <= 1) {
      try { ws.close(); } catch (_) { /* ignore */ }
    }
    ws = null;
    currentConvId = null;
    const fallback = conversationsCache[0]?.id || "new";
    pendingConvParam = fallback;
    if (el("llmPanel").classList.contains("open")) {
      connect();
    }
  }
  await loadConversations();
}

function renderConvItems() {
  const list = el("llmConvList");
  const label = el("llmConvLabel");
  if (!list || !label) return;
  list.innerHTML = "";
  if (conversationsCache.length === 0) {
    label.textContent = t('chat.conv.label_empty');
    return;
  }
  const activeIdx = Math.max(0, conversationsCache.findIndex(c => c.id === currentConvId));
  label.textContent = t('chat.conv.label_count', { idx: activeIdx + 1, total: conversationsCache.length });
  conversationsCache.forEach((c, idx) => {
    const row = document.createElement("div");
    row.className = "conv-item" + (c.id === currentConvId ? " active" : "");
    row.dataset.convId = c.id;
    const tier = (c.tier || "deep").toLowerCase();
    const fallbackTitle = t('chat.conv.default_title', { idx: idx + 1 });
    const title = escapeHTML((c.title || fallbackTitle).slice(0, 80));
    const cost = Number(c.cost_usd || 0);
    const ago = c.last_turn_at ? humanAgo(c.last_turn_at) : t('chat.conv.ago_unknown');
    const turnsCount = c.turns || 0;
    const turnsLabel = t(turnsCount === 1 ? 'chat.conv.turns_one' : 'chat.conv.turns_many', { n: turnsCount });

    const open = document.createElement("button");
    open.type = "button";
    open.className = "conv-item-open";
    open.innerHTML =
      `<span class="conv-item-head">` +
        `<span class="conv-item-tier t-${tier}">${tier.toUpperCase()}</span>` +
        `<span class="conv-item-title">${title}</span>` +
      `</span>` +
      `<span class="conv-item-meta">` +
        `<span>${escapeHTML(turnsLabel)}</span>` +
        `<span class="conv-item-sep">·</span>` +
        `<span>${fmtUsd(cost)}</span>` +
        `<span class="conv-item-sep">·</span>` +
        `<span>${escapeHTML(ago)}</span>` +
      `</span>`;
    open.addEventListener("click", () => {
      if (c.id === currentConvId) { closeConvPopover(); return; }
      switchConv(c.id);
      closeConvPopover();
    });

    const del = document.createElement("button");
    del.type = "button";
    del.className = "conv-item-delete";
    del.title = t('chat.conv.delete_aria');
    del.setAttribute("aria-label", t('chat.conv.delete_aria'));
    del.innerHTML =
      '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" ' +
      'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M3 4h10M6.5 4V2.5h3V4M5 4l.5 9a1 1 0 001 1h3a1 1 0 001-1l.5-9"/>' +
      '<path d="M7 7v5M9 7v5"/></svg>';
    del.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      if (!confirm(t('chat.conv.delete_confirm'))) return;
      await deleteConvFromPanel(c.id);
    });

    row.appendChild(open);
    row.appendChild(del);
    list.appendChild(row);
  });
}

function humanAgo(iso) {
  try {
    const then = new Date(iso).getTime();
    const diff = Math.max(0, Date.now() - then) / 1000;
    if (diff < 60) return t('chat.conv.ago_seconds', { n: Math.floor(diff) });
    if (diff < 3600) return t('chat.conv.ago_minutes', { n: Math.floor(diff / 60) });
    if (diff < 86400) return t('chat.conv.ago_hours', { n: Math.floor(diff / 3600) });
    return t('chat.conv.ago_days', { n: Math.floor(diff / 86400) });
  } catch { return t('chat.conv.ago_unknown'); }
}

export function switchConv(convIdOrNew) {
  if (convIdOrNew === currentConvId) return;
  logSys(t('chat.conv.switching', { id: convIdOrNew }));
  if (ws && ws.readyState <= 1) {
    try { ws.close(); } catch (_) {}
  }
  ws = null;
  // Route connect() to target the requested conv on reopen.
  pendingConvParam = convIdOrNew;
  // Picking a different conv defers tier choice back to the conv: its
  // active per-tier thread is what the tech actually means to see, even
  // if they had picked a tier earlier in this session. "+ Nouvelle
  // conversation" keeps the explicit tier choice intact (no conv_tier
  // to align to anyway).
  if (convIdOrNew !== "new") {
    userPickedTier = false;
  }
  connect();
}

function openConvPopover() {
  const chip = el("llmConvChip");
  const pop = el("llmConvPopover");
  if (!chip || !pop) return;
  loadConversations(); // refresh on open
  pop.hidden = false;
  chip.setAttribute("aria-expanded", "true");
}
function closeConvPopover() {
  const chip = el("llmConvChip");
  const pop = el("llmConvPopover");
  if (!chip || !pop) return;
  pop.hidden = true;
  chip.setAttribute("aria-expanded", "false");
}
function toggleConvPopover() {
  const pop = el("llmConvPopover");
  if (!pop) return;
  if (pop.hidden) openConvPopover(); else closeConvPopover();
}

// ── Demo replay: the conversation switcher, offline ───────────────────────
// The onboarding shows the REAL conversation UI being used — the chip, the
// popover, the "+ New conversation" button — so the tech sees the actual
// gesture, not a description of it. But offline there is no backend to list or
// create conversations (and `openConvPopover` would fire a 404-flooding fetch),
// so these helpers drive the very same DOM the live code does, from a
// caller-supplied COSMETIC list. Deterministic, free, no network.
export function replaySeedConversations(list) {
  conversationsCache = Array.isArray(list) ? list.slice() : [];
  // currentConvId drives the active-row marker + the "CONV n/total" chip label.
  const active = conversationsCache.find((c) => c.active);
  currentConvId = (active || conversationsCache[0] || {}).id || null;
  renderConvItems();
}
export function replayOpenConvPopover() {
  const chip = el("llmConvChip"), pop = el("llmConvPopover");
  if (!chip || !pop) return;
  renderConvItems();          // render the seeded cache — NO network fetch
  pop.hidden = false;
  chip.setAttribute("aria-expanded", "true");
}
export function replayCloseConvPopover() { closeConvPopover(); }

// Fetch the chat panel fragment from web/llm_panel.html and inject it
// into #llmRoot. Isolating the markup in its own file keeps parallel
// work on web/index.html from colliding with chat-panel edits.
async function mountPanelFragment() {
  const root = el("llmRoot");
  if (!root) return false;
  if (root.childElementCount > 0) return true; // already mounted (hot-reload guard)
  try {
    const res = await fetch("llm_panel.html", { cache: "no-cache" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    root.innerHTML = await res.text();
    // Translate the freshly-injected markup. Wait for dictionaries first
    // so the initial paint is in the right language; the i18n core falls
    // back to the inline English text if anything is missing.
    if (window.i18n) {
      try {
        await window.i18n.ready;
        window.i18n.applyDom(root);
      } catch (e) { /* keep the inline fallback */ }
    }
    return true;
  } catch (err) {
    console.warn("[llm] failed to mount panel fragment:", err);
    return false;
  }
}

export async function initLLMPanel() {
  const mounted = await mountPanelFragment();
  if (!mounted) return;

  panelMascot = mountMascot(el("llmMascot"), { size: "sm", state: "idle" });

  // Re-render imperative bits (status pill, conv chip) on locale switch.
  // The static markup is handled by `data-i18n` + i18n.applyDom; everything
  // emitted from JS (status text, conv label, replayed log lines) needs an
  // explicit redraw hook.
  if (window.i18n && typeof window.i18n.onChange === "function") {
    window.i18n.onChange(() => {
      // Conv chip (label + items) reads localized strings on every render.
      renderConvItems();
      // Status text — only refresh the current tone's label so we don't
      // overwrite an active "connecting" with a stale "idle".
      const statusEl = el("llmStatus");
      if (statusEl) {
        const txt = el("llmStatusText");
        if (txt && statusEl.classList.contains("connected")) {
          const slug = currentDeviceSlug();
          if (slug) txt.textContent = t('chat.status.connected', { slug, tier: currentTier });
        } else if (txt && !statusEl.classList.contains("connecting") &&
                   !statusEl.classList.contains("closed") &&
                   !statusEl.classList.contains("error")) {
          txt.textContent = t('chat.status.idle');
        }
      }
    });
  }

  el("llmToggle")?.addEventListener("click", togglePanel);
  el("llmClose")?.addEventListener("click", closePanel);
  el("llmStop")?.addEventListener("click", interruptAgent);

  // Tier chip → popover → switchTier.
  const tierChip = el("llmTierChip");
  const tierPopover = el("llmTierPopover");
  function openTierPopover() {
    if (!tierChip || !tierPopover) return;
    tierPopover.hidden = false;
    tierChip.setAttribute("aria-expanded", "true");
  }
  function closeTierPopover() {
    if (!tierChip || !tierPopover) return;
    tierPopover.hidden = true;
    tierChip.setAttribute("aria-expanded", "false");
  }
  function toggleTierPopover() {
    if (!tierPopover) return;
    if (tierPopover.hidden) openTierPopover(); else closeTierPopover();
  }
  tierChip?.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleTierPopover();
  });
  tierPopover?.querySelectorAll("button[data-tier]").forEach(btn => {
    btn.addEventListener("click", () => {
      const t = btn.dataset.tier;
      switchTier(t);
      closeTierPopover();
    });
  });
  document.addEventListener("click", (e) => {
    if (tierPopover && !tierPopover.hidden &&
        !tierPopover.contains(e.target) && e.target !== tierChip &&
        !tierChip?.contains(e.target)) {
      closeTierPopover();
    }
  }, true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && tierPopover && !tierPopover.hidden) {
      closeTierPopover();
    }
  });

  // Conversation chip + popover.
  const convChip = el("llmConvChip");
  const convPopover = el("llmConvPopover");
  const convNew = el("llmConvNew");
  convChip?.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleConvPopover();
  });
  convNew?.addEventListener("click", () => {
    switchConv("new");
    closeConvPopover();
  });
  document.addEventListener("click", (e) => {
    if (convPopover && !convPopover.hidden &&
        !convPopover.contains(e.target) && e.target !== convChip &&
        !convChip?.contains(e.target)) {
      closeConvPopover();
    }
  }, true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && convPopover && !convPopover.hidden) {
      closeConvPopover();
    }
  });

  const input = el("llmInput");
  const form = el("llmForm");

  function autoGrow() {
    if (!input) return;
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
  }
  input?.addEventListener("input", autoGrow);

  input?.addEventListener("keydown", (e) => {
    // Enter (without Shift) → submit. Shift+Enter → newline (default).
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form?.requestSubmit();
    }
  });

  form?.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = (input?.value || "").trim();
    if (!text) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      logSys(t('chat.send.socket_closed'), true);
      return;
    }
    logMessage("user", text);
    ws.send(JSON.stringify({ type: "message", text }));
    // Immediate feedback: open a fresh turn and show the pending indicator
    // before the backend has produced its first event. Subsequent tool_use /
    // thinking / message events reuse this turn via ensureTurn().
    closeTurn();
    const turn = ensureTurn();
    ensurePendingNode(turn);
    setPanelMascot("thinking");
    if (input) {
      input.value = "";
      autoGrow();
    }
  });

  document.addEventListener("keydown", e => {
    // ⌘J / Ctrl+J toggle
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "j") {
      e.preventDefault();
      togglePanel();
      return;
    }
    // Escape when panel focused: if the agent is live + connected, interrupt
    // it first; second Escape closes the panel.
    if (e.key === "Escape" && document.body.classList.contains("llm-open")) {
      if (document.activeElement && el("llmPanel").contains(document.activeElement)) {
        if (ws && ws.readyState === WebSocket.OPEN) {
          e.preventDefault();
          interruptAgent();
        } else {
          closePanel();
        }
      }
    }
  });

  // --- Files+Vision: upload button + drag-drop + preview toggle --------
  const uploadBtn = el("llmUploadBtn");
  const uploadInput = el("llmUploadInput");
  const dropzone = el("llmDropzone");
  const panelEl = el("llmPanel");
  const previewBtn = el("cameraPreviewBtn");

  function syncPreviewBtn() {
    if (!previewBtn) return;
    const on = isPreviewOpen();
    previewBtn.setAttribute("aria-pressed", on ? "true" : "false");
  }

  previewBtn?.addEventListener("click", async () => {
    if (isPreviewOpen()) {
      closePreview();
      syncPreviewBtn();
      return;
    }
    const id = selectedCameraDeviceId();
    if (!id) {
      logSys(t('chat.preview.select_camera_first'), true);
      return;
    }
    const ok = await openPreview(id, selectedCameraLabel());
    if (!ok) {
      logSys(t('chat.preview.open_failed'), true);
    }
    syncPreviewBtn();
  });
  syncPreviewBtn();

  uploadBtn?.addEventListener("click", () => uploadInput?.click());
  uploadInput?.addEventListener("change", (e) => {
    const file = e.target.files && e.target.files[0];
    e.target.value = "";  // allow re-uploading the same file
    handleMacroUpload(file);
  });

  if (panelEl && dropzone) {
    let dragDepth = 0;
    panelEl.addEventListener("dragenter", (e) => {
      if (!e.dataTransfer || !e.dataTransfer.types || !e.dataTransfer.types.includes("Files")) {
        return;
      }
      dragDepth += 1;
      dropzone.hidden = false;
    });
    panelEl.addEventListener("dragleave", () => {
      dragDepth -= 1;
      if (dragDepth <= 0) {
        dragDepth = 0;
        dropzone.hidden = true;
      }
    });
    panelEl.addEventListener("dragover", (e) => e.preventDefault());
    panelEl.addEventListener("drop", (e) => {
      e.preventDefault();
      dragDepth = 0;
      dropzone.hidden = true;
      const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      handleMacroUpload(file);
    });
  }
}
