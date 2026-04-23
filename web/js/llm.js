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
// Multi-conversation state. `currentConvId` is captured from session_ready.
// `conversationsCache` backs the popover render. `pendingConvParam` is the
// ?conv value to use on the next connect() — "new" to force a fresh conv,
// a concrete id to target an existing one, null to let the backend resolve
// to the active conv.
let currentConvId = null;
let conversationsCache = [];
let pendingConvParam = null;
// Session cost accumulator — reset on each (re)connect. The backend emits
// `turn_cost` after every agent inference turn; we attach a chip to the most
// recent assistant message and bump the running total in the status bar.
let sessionCostUsd = 0;
let sessionTurns = 0;
let lastTurnCostUsd = 0;

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
  const deltaPart = lastTurnCostUsd > 0 ? ` · +${fmtUsd(lastTurnCostUsd)} dernier` : "";
  el2.textContent = `${fmtUsd(sessionCostUsd)} · ${sessionTurns} turn${sessionTurns > 1 ? "s" : ""}${deltaPart}`;
  el2.classList.toggle("hot", sessionCostUsd >= 0.50 || lastTurnCostUsd >= 0.10);
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

// Distinct card rendered when an expired MA session had to be recreated and
// Haiku summarised the prior conversation for the fresh agent. Shows the
// same block the new agent is seeing, so the tech knows what carried over.
function renderResumeSummary(payload) {
  const summary = payload?.summary || "";
  const tokIn = payload?.tokens_in ?? "—";
  const tokOut = payload?.tokens_out ?? "—";
  let bodyHTML = escapeHTML(summary);
  if (typeof window.marked !== "undefined" && typeof window.DOMPurify !== "undefined") {
    try {
      bodyHTML = window.DOMPurify.sanitize(window.marked.parse(summary));
    } catch (e) { /* keep escaped fallback */ }
  }
  logRow(
    "resume-summary",
    `<header>
       <span class="icon-dot"></span>
       <span class="title">Reprise de session</span>
       <span class="meta">résumé Haiku · ${tokIn}→${tokOut} tok</span>
     </header>
     <div class="body">${bodyHTML}</div>`,
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

function ensurePendingNode(turn, label = "l'agent réfléchit") {
  const rail = turn.querySelector(".turn-rail");
  if (!rail || rail.querySelector(".step.pending")) return;
  const step = document.createElement("div");
  step.className = "step pending";
  step.innerHTML =
    `<span class="node"></span>` +
    `<span class="step-phrase">${escapeHTML(label)}` +
    `<span class="pending-dots"><span>.</span><span>.</span><span>.</span></span>` +
    `</span>`;
  rail.appendChild(step);
  el("llmLog").scrollTop = el("llmLog").scrollHeight;
}

function clearPendingNode(turn) {
  const p = turn.querySelector(".step.pending");
  if (p) p.remove();
}

// Append a .step into the turn's rail. kind ∈ {"thinking","mb","bv"}.
// phraseHTML is trusted HTML (callers escape user-provided fragments
// themselves — currently only tool names + refdes which are validated).
function appendStep(turn, kind, phraseHTML) {
  clearPendingNode(turn);
  const rail = turn.querySelector(".turn-rail");
  const step = document.createElement("div");
  step.className = `step ${kind}`;
  step.innerHTML = `<span class="node"></span><span class="step-phrase">${phraseHTML}</span>`;
  rail.appendChild(step);
  ensurePendingNode(turn);
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

// Regex shapes. Kept loose — the semantic filter is the Boardview lookup.
const RE_REFDES = /\b[A-Z]{1,3}\d{1,4}\b/g;
// Nets: common naming conventions used in iPhone / Mac / Pi schematics.
// Over-matches on purpose; Boardview.hasNet is the truth gate.
const RE_NET = /\b(?:PP_[A-Z0-9_]+|[PN]P_[A-Z0-9_]+|L\d{1,3}|VCC(?:_[A-Z0-9_]+)?|VDD(?:_[A-Z0-9_]+)?|AVDD(?:_[A-Z0-9_]+)?|DVDD(?:_[A-Z0-9_]+)?|GND(?:_[A-Z0-9_]+)?|[A-Z][A-Z0-9_]{3,})\b/g;
const RE_UNKNOWN_REFDES = /⟨\?([A-Z]{1,3}\d{1,4})⟩/g;

function appendTurnMessage(turn, text) {
  let msg = turn.querySelector(".turn-message");
  if (msg) {
    // Second assistant message in the same turn — open a new turn.
    closeTurn();
    turn = ensureTurn();
    msg = null;
  }
  clearPendingNode(turn);
  msg = document.createElement("div");
  msg.className = "turn-message";
  renderAgentMarkup(msg, text || "");
  // Caret: visible until turn_cost arrives (removed in appendTurnFoot).
  const caret = document.createElement("span");
  caret.className = "caret";
  msg.appendChild(caret);
  turn.appendChild(msg);
  el("llmLog").scrollTop = el("llmLog").scrollHeight;
  return msg;
}

// Parse markdown → sanitize → walk text nodes → replace validated tokens
// with clickable chips. If marked / DOMPurify aren't on the page, fall back
// to plain text (defensive: network hiccup loading the CDN).
function renderAgentMarkup(container, text) {
  let html;
  if (typeof marked !== "undefined" && typeof DOMPurify !== "undefined") {
    const raw = marked.parse(text, { breaks: true, gfm: true });
    html = DOMPurify.sanitize(raw, {
      ALLOWED_TAGS: ["p", "br", "strong", "em", "ul", "ol", "li", "code"],
      ALLOWED_ATTR: [],
    });
  } else {
    html = escapeHTML(text).replaceAll("\n", "<br>");
  }
  container.innerHTML = html;
  decorateChipsIn(container);
}

// Walk all text nodes under `root` and replace validated refdes / net
// tokens with clickable chips, plus unknown-refdes ⟨?U999⟩ with amber
// span. Text inside <code> is skipped (agent's verbatim intent).
function decorateChipsIn(root) {
  const hasBoard = !!(window.Boardview && window.Boardview.hasBoard && window.Boardview.hasBoard());
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      if (!n.nodeValue) return NodeFilter.FILTER_REJECT;
      if (n.parentElement && n.parentElement.closest("code, .refdes-unknown, .chip-refdes, .chip-net"))
        return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const targets = [];
  while (walker.nextNode()) targets.push(walker.currentNode);
  for (const textNode of targets) decorateOneTextNode(textNode, hasBoard);
}

function decorateOneTextNode(textNode, hasBoard) {
  const original = textNode.nodeValue;
  const matches = [];
  for (const m of original.matchAll(RE_UNKNOWN_REFDES)) {
    matches.push({ kind: "unknown", start: m.index, end: m.index + m[0].length, raw: m[0], inner: m[1] });
  }
  for (const m of original.matchAll(RE_REFDES)) {
    if (hasBoard && window.Boardview.hasRefdes(m[0])) {
      matches.push({ kind: "refdes", start: m.index, end: m.index + m[0].length, raw: m[0] });
    }
  }
  for (const m of original.matchAll(RE_NET)) {
    if (hasBoard && window.Boardview.hasNet(m[0])) {
      matches.push({ kind: "net", start: m.index, end: m.index + m[0].length, raw: m[0] });
    }
  }
  if (matches.length === 0) return;
  // Resolve overlaps: earliest-start first, ties broken by longest-wins.
  matches.sort((a, b) => a.start - b.start || (b.end - b.start) - (a.end - a.start));
  const cleaned = [];
  let cursor = 0;
  for (const m of matches) {
    if (m.start < cursor) continue;
    cleaned.push(m);
    cursor = m.end;
  }
  const frag = document.createDocumentFragment();
  let i = 0;
  for (const m of cleaned) {
    if (m.start > i) frag.appendChild(document.createTextNode(original.slice(i, m.start)));
    frag.appendChild(makeChipNode(m));
    i = m.end;
  }
  if (i < original.length) frag.appendChild(document.createTextNode(original.slice(i)));
  textNode.parentNode.replaceChild(frag, textNode);
}

function makeChipNode(match) {
  if (match.kind === "refdes") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip-refdes";
    btn.dataset.refdes = match.raw;
    btn.textContent = match.raw;
    btn.addEventListener("click", () => {
      if (window.Boardview && window.Boardview.focusRefdes) {
        window.Boardview.focusRefdes(match.raw);
      }
    });
    return btn;
  }
  if (match.kind === "net") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip-net";
    btn.dataset.net = match.raw;
    btn.textContent = match.raw;
    btn.addEventListener("click", () => {
      if (window.Boardview && window.Boardview.highlightNet) {
        window.Boardview.highlightNet(match.raw);
      }
    });
    return btn;
  }
  const span = document.createElement("span");
  span.className = "refdes-unknown";
  span.textContent = match.raw;
  return span;
}

function appendTurnFoot(turn, payload) {
  // Terminal signal for this turn — clear transient indicators.
  const caret = turn.querySelector(".turn-message .caret");
  if (caret) caret.remove();
  clearPendingNode(turn);
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

function wsURL(slug, tier, repairId, convParam) {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const params = new URLSearchParams();
  if (tier) params.set("tier", tier);
  if (repairId) params.set("repair", repairId);
  if (convParam) params.set("conv", convParam);
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
  const title = el("llmTitle");
  if (title) {
    const human = slug.replace(/[-_]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    title.textContent = human;
  }
  // New connection = new cost scope. Replayed history doesn't re-bill so we
  // reset here and let live turns accumulate fresh.
  sessionCostUsd = 0;
  sessionTurns = 0;
  lastTurnCostUsd = 0;
  currentTurn = null;
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
  const url = wsURL(slug, currentTier, repairId, pendingConvParam);
  pendingConvParam = null;  // consume after this connect
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
        const rid = payload.repair_id ? ` · repair ${payload.repair_id.slice(0, 8)}` : "";
        const sub = el("llmSubline");
        if (sub) sub.textContent = `${model} · ${mode}${rid}`;
        logSys(`session prête — ${mode} · ${model}${rid}`);
        currentConvId = payload.conv_id || null;
        loadConversations();
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
      case "session_resumed_summary":
        renderResumeSummary(payload);
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
        lastTurnCostUsd = Number(payload.cost_usd || 0);
        sessionCostUsd += lastTurnCostUsd;
        sessionTurns += 1;
        updateCostTotal();
        if (currentTurn) appendTurnFoot(currentTurn, payload);
        clearTimeout(window._llmConvRefreshT);
        window._llmConvRefreshT = setTimeout(() => loadConversations(), 500);
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
  const chip = el("llmTierChip");
  if (chip) {
    chip.dataset.tier = newTier;
    const label = chip.querySelector(".tier-label");
    if (label) label.textContent = newTier.toUpperCase();
  }
  document.querySelectorAll(".llm-tier-popover button[data-tier]").forEach(btn => {
    btn.classList.toggle("on", btn.dataset.tier === newTier);
  });
  logSys(`→ changement de tier : ${newTier}. Nouvelle conversation.`);
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
  const slug = new URLSearchParams(window.location.search).get("device");
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

function renderConvItems() {
  const list = el("llmConvList");
  const label = el("llmConvLabel");
  if (!list || !label) return;
  list.innerHTML = "";
  if (conversationsCache.length === 0) {
    label.textContent = "CONV 0/0";
    return;
  }
  const activeIdx = Math.max(0, conversationsCache.findIndex(c => c.id === currentConvId));
  label.textContent = `CONV ${activeIdx + 1}/${conversationsCache.length}`;
  conversationsCache.forEach((c, idx) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "conv-item" + (c.id === currentConvId ? " active" : "");
    btn.dataset.convId = c.id;
    const tier = (c.tier || "fast").toLowerCase();
    const title = escapeHTML((c.title || `Conversation ${idx + 1}`).slice(0, 80));
    const cost = Number(c.cost_usd || 0);
    const ago = c.last_turn_at ? humanAgo(c.last_turn_at) : "—";
    btn.innerHTML =
      `<span class="conv-item-head">` +
        `<span class="conv-item-tier t-${tier}">${tier.toUpperCase()}</span>` +
        `<span class="conv-item-title">${title}</span>` +
      `</span>` +
      `<span class="conv-item-meta">` +
        `<span>${c.turns || 0} turn${(c.turns || 0) === 1 ? "" : "s"}</span>` +
        `<span class="conv-item-sep">·</span>` +
        `<span>${fmtUsd(cost)}</span>` +
        `<span class="conv-item-sep">·</span>` +
        `<span>${ago}</span>` +
      `</span>`;
    btn.addEventListener("click", () => {
      if (c.id === currentConvId) { closeConvPopover(); return; }
      switchConv(c.id);
      closeConvPopover();
    });
    list.appendChild(btn);
  });
}

function humanAgo(iso) {
  try {
    const then = new Date(iso).getTime();
    const diff = Math.max(0, Date.now() - then) / 1000;
    if (diff < 60) return `il y a ${Math.floor(diff)} s`;
    if (diff < 3600) return `il y a ${Math.floor(diff / 60)} min`;
    if (diff < 86400) return `il y a ${Math.floor(diff / 3600)} h`;
    return `il y a ${Math.floor(diff / 86400)} j`;
  } catch { return "—"; }
}

function switchConv(convIdOrNew) {
  if (convIdOrNew === currentConvId) return;
  logSys(`→ changement de conversation : ${convIdOrNew}`);
  if (ws && ws.readyState <= 1) {
    try { ws.close(); } catch (_) {}
  }
  ws = null;
  // Route connect() to target the requested conv on reopen.
  pendingConvParam = convIdOrNew;
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
      logSys("impossible d'envoyer : socket non ouvert", true);
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
}
