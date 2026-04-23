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

function logToolUse(name, input, isReplay = false) {
  let args = "";
  try { args = JSON.stringify(input ?? {}); } catch { args = String(input); }
  const cls = `tool${isReplay ? " replay" : ""}`;
  logRow(
    cls,
    `<span class="arrow">→</span><span class="name">${escapeHTML(name)}</span><span class="args">${escapeHTML(args)}</span>`,
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
        logSys(`replay · ${payload.count} events précédents`);
        break;
      case "history_replay_end":
        logSys("replay terminé — reprends où tu t'étais arrêté");
        break;
      case "context_loaded":
        // Backend stashed the repair's device + symptom context; it will
        // be prefixed to the tech's first message. The agent stays silent
        // until the tech actually types.
        logSys("contexte device + symptôme chargé · l'agent attend ton premier message");
        break;
      case "session_resumed":
        // Managed mode: we picked up an existing MA session so the agent
        // remembers the conversation. A history_replay_start will follow
        // with the past events so the chat panel also rebuilds visually.
        logSys("session reprise · historique et mémoire agent restaurés");
        break;
      case "message":
        logMessage(payload.role || "assistant", payload.text || "", payload.replay === true);
        break;
      case "tool_use":
        logToolUse(payload.name, payload.input, payload.replay === true);
        break;
      case "thinking":
        // Quieter than a full message — render as sys line.
        logSys(`thinking · ${payload.text.slice(0, 120)}`);
        break;
      case "turn_cost":
        sessionCostUsd += Number(payload.cost_usd || 0);
        sessionTurns += 1;
        updateCostTotal();
        attachCostChipToLastAssistant(payload);
        break;
      case "error":
        logSys(`erreur : ${payload.text}`, true);
        break;
      case "session_terminated":
        logSys("session terminée", true);
        break;
      default:
        // Unknown payload — show raw for debuggability.
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

export function initLLMPanel() {
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
