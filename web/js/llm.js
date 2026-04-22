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

function logMessage(role, text) {
  const roleLabel = role === "user" ? "Toi" : "Agent";
  logRow(
    `msg ${role}`,
    `<span class="role">${roleLabel}</span>${escapeHTML(text)}`,
  );
}

function logToolUse(name, input) {
  let args = "";
  try { args = JSON.stringify(input ?? {}); } catch { args = String(input); }
  logRow(
    "tool",
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

function wsURL(slug, tier) {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const q = tier ? `?tier=${encodeURIComponent(tier)}` : "";
  return `${scheme}://${window.location.host}/ws/diagnostic/${encodeURIComponent(slug)}${q}`;
}

function setSendEnabled(enabled) {
  el("llmSend").disabled = !enabled;
}

function connect() {
  const slug = currentDeviceSlug();
  el("llmDevice").textContent = slug;
  el("llmDevice").style.display = "";
  const url = wsURL(slug, currentTier);
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

    switch (payload.type) {
      case "session_ready": {
        const model = payload.model || "claude";
        const mode = payload.mode || "managed";
        el("llmModel").textContent = `${model} · ${mode}`;
        logSys(`session prête — ${mode} · ${model}`);
        break;
      }
      case "message":
        logMessage(payload.role || "assistant", payload.text || "");
        break;
      case "tool_use":
        logToolUse(payload.name, payload.input);
        break;
      case "thinking":
        // Quieter than a full message — render as sys line.
        logSys(`thinking · ${payload.text.slice(0, 120)}`);
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

export function initLLMPanel() {
  el("llmToggle")?.addEventListener("click", togglePanel);
  el("llmClose")?.addEventListener("click", closePanel);

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
    // Escape closes when panel is focused
    if (e.key === "Escape" && document.body.classList.contains("llm-open")) {
      if (document.activeElement && el("llmPanel").contains(document.activeElement)) {
        closePanel();
      }
    }
  });
}
