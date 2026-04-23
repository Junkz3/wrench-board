// Diagnostic agent panel — WS client to /ws/diagnostic/{device_slug}.
// The panel is push-mode: when open, body.llm-open is set and the main
// content zones shrink 420px on the right.
//
// Wire protocol (matches api/agent/runtime_{managed,direct}.py):
//   send: {type: "message", text: "..."}
//   recv: {type: "session_ready", mode, device_slug, session_id?, memory_store_id?}
//         {type: "message", role: "assistant", text}
//         {type: "tool_use", name, input}
//         {type: "thinking", text}                 (managed mode only)
//         {type: "error", text}
//         {type: "session_terminated"}
//
// Activated by ⌘/Ctrl+J and by clicking the topbar "Agent" button.

let ws = null;
let currentTier = "fast";
// Session cost accumulator — reset on each (re)connect. The backend emits
// `turn_cost` after every agent inference turn; we attach a chip to the most
// recent assistant message and bump the running total in the status bar.
let sessionCostUsd = 0;
let sessionTurns = 0;

// Turn-block state machine.
// currentTurn is the DOM node receiving the next incoming thinking / tool_use /
// message event. A user.message closes it (set to null). An assistant.message
// that arrives when currentTurn already has a .turn-message opens a new turn
// (agent emitted two messages back-to-back without a user interjection).
let currentTurn = null;

// Family icons for tool-call steps. 12×12, stroke currentColor, per CLAUDE.md §icons.
const ICON_MB =
  '<svg class="step-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" ' +
  'aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/>' +
  '<circle cx="12" cy="12" r="3"/></svg>';
const ICON_BV =
  '<svg class="step-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" ' +
  'aria-hidden="true"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/>' +
  '<circle cx="12" cy="12" r="1.2" fill="currentColor"/></svg>';

// French paraphrase + family icon for each known tool name. Each entry
// is a function receiving the tool input object and returning
// {icon, phraseHTML}. phraseHTML may embed a <span class="refdes"> or
// <span class="net"> for typographic emphasis on the target; all user
// input is passed through escapeHTML before interpolation.
const TOOL_PHRASES = {
  // --- MB (memory bank — perception / reading) ---
  mb_get_component: (i) => ({
    icon: ICON_MB,
    phraseHTML: `Consultation de <span class="refdes">${escapeHTML(i?.refdes || "?")}</span>`,
  }),
  mb_get_rules_for_symptoms: (i) => {
    const syms = Array.isArray(i?.symptoms) ? i.symptoms.join(", ") : (i?.symptoms || "");
    return {
      icon: ICON_MB,
      phraseHTML: `Lecture des règles pour « ${escapeHTML(syms)} »`,
    };
  },
  mb_list_findings: (i) => ({
    icon: ICON_MB,
    phraseHTML: `Revue des findings${i?.device ? ` pour <span class="refdes">${escapeHTML(i.device)}</span>` : ""}`,
  }),
  mb_record_finding: () => ({
    icon: ICON_MB,
    phraseHTML: `Enregistrement d'un finding`,
  }),
  mb_expand_knowledge: (i) => {
    const scope = [i?.component, i?.symptom].filter(Boolean).join(" / ");
    return {
      icon: ICON_MB,
      phraseHTML: `Extension du pack${scope ? ` — ${escapeHTML(scope)}` : ""}`,
    };
  },
  mb_schematic_graph: () => ({
    icon: ICON_MB,
    phraseHTML: `Lecture du graphe schématique`,
  }),

  // --- BV (boardview — action) ---
  bv_highlight_component: (i) => ({
    icon: ICON_BV,
    phraseHTML: `Mise en évidence de <span class="refdes">${escapeHTML(i?.refdes || "?")}</span>`,
  }),
  bv_focus_component: (i) => ({
    icon: ICON_BV,
    phraseHTML: `Focus sur <span class="refdes">${escapeHTML(i?.refdes || "?")}</span>`,
  }),
  bv_reset_view: () => ({ icon: ICON_BV, phraseHTML: `Réinitialisation de la vue` }),
  bv_highlight_net: (i) => ({
    icon: ICON_BV,
    phraseHTML: `Highlight du net <span class="net">${escapeHTML(i?.net || "?")}</span>`,
  }),
  bv_flip_board: () => ({ icon: ICON_BV, phraseHTML: `Retournement du board` }),
  bv_annotate: (i) => {
    const tgt = i?.refdes ? `près de <span class="refdes">${escapeHTML(i.refdes)}</span>` :
                (Number.isFinite(i?.x) && Number.isFinite(i?.y) ? `en (${i.x}, ${i.y})` : "");
    return { icon: ICON_BV, phraseHTML: `Annotation ${tgt}` };
  },
  bv_filter_by_type: (i) => ({
    icon: ICON_BV,
    phraseHTML: `Filtrage par type — ${escapeHTML(i?.type || "?")}`,
  }),
  bv_draw_arrow: (i) => ({
    icon: ICON_BV,
    phraseHTML: `Flèche de <span class="refdes">${escapeHTML(i?.from || "?")}</span> ` +
                `vers <span class="refdes">${escapeHTML(i?.to || "?")}</span>`,
  }),
  bv_measure_distance: (i) => ({
    icon: ICON_BV,
    phraseHTML: `Mesure entre <span class="refdes">${escapeHTML(i?.a || "?")}</span> ` +
                `et <span class="refdes">${escapeHTML(i?.b || "?")}</span>`,
  }),
  bv_show_pin: (i) => ({
    icon: ICON_BV,
    phraseHTML: `Pin ${escapeHTML(String(i?.pin ?? "?"))} de <span class="refdes">${escapeHTML(i?.refdes || "?")}</span>`,
  }),
  bv_dim_unrelated: () => ({ icon: ICON_BV, phraseHTML: `Atténuation des éléments non liés` }),
  bv_layer_visibility: (i) => ({
    icon: ICON_BV,
    phraseHTML: `Visibilité de la couche ${escapeHTML(i?.layer || "?")}`,
  }),
};

function toolFallback(name) {
  return {
    icon: "",
    phraseHTML: `<span class="tool-name-raw">${escapeHTML(name)}</span>`,
  };
}

function fmtUsd(amount) {
  if (amount >= 1) return `$${amount.toFixed(2)}`;
  if (amount >= 0.01) return `$${amount.toFixed(3)}`;
  if (amount >= 0.0001) return `$${amount.toFixed(4)}`;
  return amount > 0 ? `<$0.0001` : `$0.00`;
}

function updateCostTotal() {
  const el2 = el("llmCostTotal");
  if (!el2) return;
  if (sessionTurns === 0) {
    el2.style.display = "none";
    return;
  }
  el2.style.display = "";
  el2.textContent = `${fmtUsd(sessionCostUsd)} · ${sessionTurns} turn${sessionTurns > 1 ? "s" : ""}`;
  el2.classList.toggle("hot", sessionCostUsd >= 0.50);
}

function attachCostChipToLastAssistant(payload) {
  const log = el("llmLog");
  const messages = log.querySelectorAll(".msg.assistant:not(.replay)");
  const target = messages[messages.length - 1];
  if (!target || target.querySelector(".cost-chip")) return;
  const chip = document.createElement("span");
  chip.className = "cost-chip";
  const tokensLabel = `${(payload.input_tokens || 0) + (payload.cache_read_input_tokens || 0) + (payload.cache_creation_input_tokens || 0)}→${payload.output_tokens || 0} tok`;
  const modelLabel = payload.model ? payload.model.replace("claude-", "") : "?";
  const priceLabel = payload.priced ? fmtUsd(payload.cost_usd) : "—";
  chip.textContent = `${modelLabel} · ${tokensLabel} · ${priceLabel}`;
  target.appendChild(chip);
}

function el(id) { return document.getElementById(id); }

function statusTone(tone, label) {
  const s = el("llmStatus");
  s.classList.remove("connecting", "connected", "closed", "error");
  if (tone) s.classList.add(tone);
  el("llmStatusText").textContent = label;
}

function logRow(cls, innerHTML) {
  const log = el("llmLog");
  const row = document.createElement("div");
  row.className = cls;
  row.innerHTML = innerHTML;
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
  return row;
}

function logMessage(role, text, isReplay = false) {
  const roleLabel = role === "user" ? "Toi" : "Agent";
  const cls = `msg ${role}${isReplay ? " replay" : ""}`;
  logRow(
    cls,
    `<span class="role">${roleLabel}${isReplay ? " · replay" : ""}</span>${escapeHTML(text)}`,
  );
}

function logSys(text, isErr = false) {
  logRow(isErr ? "sys err" : "sys", escapeHTML(text));
}

function escapeHTML(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

// Create a fresh turn-block container and append it to the log.
function createTurn() {
  const log = el("llmLog");
  const turn = document.createElement("div");
  turn.className = "turn";
  const rail = document.createElement("div");
  rail.className = "turn-rail";
  turn.appendChild(rail);
  log.appendChild(turn);
  log.scrollTop = log.scrollHeight;
  return turn;
}

function ensureTurn() {
  if (!currentTurn) currentTurn = createTurn();
  return currentTurn;
}

function closeTurn() {
  currentTurn = null;
}

// Append a .step into the turn's rail. kind ∈ {"thinking","mb","bv"}.
// phraseHTML is trusted HTML (callers escape user-provided fragments
// themselves — currently only tool names + refdes which are validated).
function appendStep(turn, kind, phraseHTML) {
  const rail = turn.querySelector(".turn-rail");
  const step = document.createElement("div");
  step.className = `step ${kind}`;
  step.innerHTML = `<span class="node"></span><span class="step-phrase">${phraseHTML}</span>`;
  rail.appendChild(step);
  el("llmLog").scrollTop = el("llmLog").scrollHeight;
  return step;
}

function addExpandToStep(step, payloadObj) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "step-expand";
  btn.setAttribute("aria-expanded", "false");
  btn.title = "Voir le payload";
  btn.innerHTML =
    '<svg class="chevron" width="10" height="10" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
    '<polyline points="9 6 15 12 9 18"/></svg>';
  step.appendChild(btn);

  const pre = document.createElement("pre");
  pre.className = "step-payload";
  const hasResult = payloadObj && typeof payloadObj === "object" && "result" in payloadObj;
  const body = hasResult
    ? JSON.stringify(payloadObj, null, 2)
    : JSON.stringify(payloadObj, null, 2) + "\n\n— result non rendu par le runtime";
  pre.textContent = body;
  step.appendChild(pre);

  btn.addEventListener("click", () => {
    const expanded = step.classList.toggle("expanded");
    btn.setAttribute("aria-expanded", expanded ? "true" : "false");
  });
}

// Append the assistant text into the current turn. Plain textContent for
// now — Task 4 will replace this with markdown + chip rendering.
function appendTurnMessage(turn, text) {
  let msg = turn.querySelector(".turn-message");
  if (msg) {
    // An assistant message already landed in this turn — open a new one.
    closeTurn();
    turn = ensureTurn();
    msg = null;
  }
  msg = document.createElement("div");
  msg.className = "turn-message";
  msg.textContent = text ?? "";
  turn.appendChild(msg);
  el("llmLog").scrollTop = el("llmLog").scrollHeight;
  return msg;
}

function appendTurnFoot(turn, payload) {
  let foot = turn.querySelector(".turn-foot");
  if (!foot) {
    foot = document.createElement("div");
    foot.className = "turn-foot";
    turn.appendChild(foot);
  }
  const priceLabel = payload.priced ? fmtUsd(payload.cost_usd) : "—";
  const modelLabel = payload.model ? payload.model.replace("claude-", "") : "?";
  const tokensLabel = `${(payload.input_tokens || 0) + (payload.cache_read_input_tokens || 0) + (payload.cache_creation_input_tokens || 0)}→${payload.output_tokens || 0} tok`;
  foot.innerHTML =
    `<span class="foot-cost">${priceLabel}</span>` +
    `<span class="foot-sep">·</span>` +
    `<span class="foot-tokens">${tokensLabel}</span>` +
    `<span class="foot-sep">·</span>` +
    `<span class="foot-model">${escapeHTML(modelLabel)}</span>`;
}

function safeJSON(v) {
  try { return JSON.stringify(v ?? {}); } catch { return String(v); }
}

function currentDeviceSlug() {
  return new URLSearchParams(window.location.search).get("device") || "demo-pi";
}

function currentRepairId() {
  return new URLSearchParams(window.location.search).get("repair") || null;
}

function wsURL(slug, tier, repairId) {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const params = new URLSearchParams();
  if (tier) params.set("tier", tier);
  if (repairId) params.set("repair", repairId);
  const q = params.toString() ? `?${params.toString()}` : "";
  return `${scheme}://${window.location.host}/ws/diagnostic/${encodeURIComponent(slug)}${q}`;
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
  logSys("interruption envoyée · l'agent s'arrête");
  try {
    ws.send(JSON.stringify({ type: "interrupt" }));
  } catch (err) {
    console.warn("interrupt send failed", err);
  }
}

function connect() {
  const slug = currentDeviceSlug();
  const repairId = currentRepairId();
  el("llmDevice").textContent = repairId ? `${slug} · ${repairId.slice(0, 8)}` : slug;
  el("llmDevice").style.display = "";
  // New connection = new cost scope. Replayed history doesn't re-bill so we
  // reset here and let live turns accumulate fresh.
  sessionCostUsd = 0;
  sessionTurns = 0;
  currentTurn = null;
  updateCostTotal();
  const url = wsURL(slug, currentTier, repairId);
  statusTone("connecting", `connexion · ${slug} · ${currentTier}`);

  try {
    ws = new WebSocket(url);
  } catch (err) {
    statusTone("error", "URL invalide");
    logSys(`échec de connexion : ${err.message}`, true);
    return;
  }

  ws.addEventListener("open", () => {
    statusTone("connected", `connecté · ${slug} · ${currentTier}`);
    setSendEnabled(true);
  });

  ws.addEventListener("close", () => {
    statusTone("closed", "fermé");
    setSendEnabled(false);
  });

  ws.addEventListener("error", () => {
    statusTone("error", "erreur socket");
    setSendEnabled(false);
  });

  ws.addEventListener("message", ev => {
    let payload;
    try { payload = JSON.parse(ev.data); }
    catch { payload = { type: "message", role: "assistant", text: ev.data }; }

    // Boardview events are visual mutations — not chat content. Route them
    // to the renderer (or its pending buffer if the renderer hasn't mounted).
    if (typeof payload.type === "string" && payload.type.startsWith("boardview.")) {
      window.Boardview.apply(payload);
      return;
    }

    switch (payload.type) {
      case "session_ready": {
        const model = payload.model || "claude";
        const mode = payload.mode || "managed";
        el("llmModel").textContent = `${model} · ${mode}`;
        const rid = payload.repair_id ? ` · repair ${payload.repair_id.slice(0, 8)}` : "";
        logSys(`session prête — ${mode} · ${model}${rid}`);
        break;
      }
      case "history_replay_start":
        el("llmLog").classList.add("replay");
        logSys(`replay · ${payload.count} events précédents`);
        break;
      case "history_replay_end":
        el("llmLog").classList.remove("replay");
        logSys("replay terminé — reprends où tu t'étais arrêté");
        closeTurn();
        break;
      case "context_loaded":
        logSys("contexte device + symptôme chargé · l'agent attend ton premier message");
        break;
      case "session_resumed":
        logSys("session reprise · historique et mémoire agent restaurés");
        break;
      case "message":
        if ((payload.role || "assistant") === "user") {
          closeTurn();
          logMessage("user", payload.text || "", payload.replay === true);
        } else {
          const turn = ensureTurn();
          appendTurnMessage(turn, payload.text || "");
        }
        break;
      case "tool_use": {
        const turn = ensureTurn();
        const name = payload.name || "?";
        const kind = name.startsWith("bv_") ? "bv" :
                     name.startsWith("mb_") ? "mb" : "mb";
        const renderer = TOOL_PHRASES[name];
        const { icon, phraseHTML } = renderer ? renderer(payload.input || {}) : toolFallback(name);
        const step = appendStep(turn, kind, `${icon}${phraseHTML}`);
        const payloadJSON = {
          args: payload.input || {},
          ...(payload.result != null ? { result: payload.result } : {}),
        };
        addExpandToStep(step, payloadJSON);
        break;
      }
      case "thinking": {
        const turn = ensureTurn();
        appendStep(turn, "thinking", escapeHTML(payload.text || "…"));
        break;
      }
      case "turn_cost":
        sessionCostUsd += Number(payload.cost_usd || 0);
        sessionTurns += 1;
        updateCostTotal();
        if (currentTurn) appendTurnFoot(currentTurn, payload);
        break;
      case "error":
        logSys(`erreur : ${payload.text}`, true);
        break;
      case "session_terminated":
        logSys("session terminée", true);
        closeTurn();
        break;
      default:
        logSys(`? ${JSON.stringify(payload)}`);
    }
  });
}

function openPanel() {
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

function togglePanel() {
  if (el("llmPanel").classList.contains("open")) closePanel();
  else openPanel();
}

function switchTier(newTier) {
  if (newTier === currentTier) return;
  currentTier = newTier;
  document.querySelectorAll(".llm-tier").forEach(btn => {
    const isOn = btn.dataset.tier === newTier;
    btn.classList.toggle("on", isOn);
    btn.setAttribute("aria-selected", isOn ? "true" : "false");
  });
  logSys(`→ changement de tier : ${newTier}. Nouvelle conversation.`);
  // Drop current WS and reconnect — explicit new session on the tier's agent.
  if (ws && ws.readyState <= 1) {
    try { ws.close(); } catch (_) { /* ignore */ }
  }
  ws = null;
  connect();
}

// Auto-open the panel when the URL carries ?repair=<id>. Called from the
// main bootstrap so that clicking a repair card on Home lands the user
// directly in the conversation — no extra click needed.
export function openLLMPanelIfRepairParam() {
  const rid = currentRepairId();
  const slug = new URLSearchParams(window.location.search).get("device");
  if (rid && slug) {
    // Defer one frame so the DOM is definitely wired (openPanel touches
    // llmInput, llmToggle, etc.) and the status bar has mounted.
    requestAnimationFrame(() => openPanel());
  }
}

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
    return true;
  } catch (err) {
    console.warn("[llm] failed to mount panel fragment:", err);
    return false;
  }
}

export async function initLLMPanel() {
  const mounted = await mountPanelFragment();
  if (!mounted) return;

  el("llmToggle")?.addEventListener("click", togglePanel);
  el("llmClose")?.addEventListener("click", closePanel);
  el("llmStop")?.addEventListener("click", interruptAgent);

  document.querySelectorAll(".llm-tier").forEach(btn => {
    btn.addEventListener("click", () => switchTier(btn.dataset.tier));
  });

  el("llmForm")?.addEventListener("submit", e => {
    e.preventDefault();
    const input = el("llmInput");
    const text = input.value.trim();
    if (!text) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      logSys("impossible d'envoyer : socket non ouvert", true);
      return;
    }
    logMessage("user", text);
    ws.send(JSON.stringify({ type: "message", text }));
    input.value = "";
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
}
