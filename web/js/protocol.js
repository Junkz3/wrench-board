// web/js/protocol.js
// Central state + DOM coordination for the diagnostic protocol surface.
// Receives WS events relayed by llm.js, owns the protocol object in
// memory, dispatches to the three render modules (wizard / floating /
// inline) which read state via getProtocol() and re-render on change.

const state = {
  proto: null,           // {protocol_id, title, steps:[…], current_step_id, …} or null
  send: null,            // (payload) => void  — set by main.js
  hasBoard: false,
};

// Some agents (Haiku) double-escape Unicode in tool-argument JSON strings,
// so titles arrive as `D11 éteinte — isoler` instead of
// `D11 éteinte — isoler`. The Pydantic schema decodes new protocols at
// the boundary; this client-side helper covers protocols that were
// persisted before that fix landed and are now replayed onto the WS.
const _UNICODE_ESCAPE_RE = /\\u([0-9a-fA-F]{4})/g;
function decodeEscapes(value) {
  if (typeof value !== "string" || value.indexOf("\\u") < 0) return value;
  return value.replace(_UNICODE_ESCAPE_RE, (_, hex) =>
    String.fromCharCode(parseInt(hex, 16))
  );
}
function cleanStep(s) {
  if (!s || typeof s !== "object") return s;
  return {
    ...s,
    instruction: decodeEscapes(s.instruction),
    rationale: decodeEscapes(s.rationale),
  };
}

const subscribers = new Set();

function notify() { subscribers.forEach((cb) => cb(state.proto)); }

export function init({ send, hasBoard }) {
  state.send = send;
  state.hasBoard = !!hasBoard;
  notify();
}

export function setHasBoard(value) {
  state.hasBoard = !!value;
  notify();
}

export function subscribe(cb) {
  subscribers.add(cb);
  cb(state.proto);
  return () => subscribers.delete(cb);
}

export function getProtocol() { return state.proto; }
export function hasBoard() { return state.hasBoard; }

export function applyEvent(ev) {
  if (!ev || typeof ev !== "object") return;
  switch (ev.type) {
    case "protocol_proposed":
      state.proto = {
        protocol_id: ev.protocol_id,
        title: decodeEscapes(ev.title),
        rationale: decodeEscapes(ev.rationale),
        steps: (ev.steps || []).map(cleanStep),
        current_step_id: ev.current_step_id,
        history: [],
      };
      break;
    case "protocol_updated":
      if (!state.proto || state.proto.protocol_id !== ev.protocol_id) break;
      // Abandon (or any terminal status) clears the quest panel entirely
      // — same effect as protocol_completed. Without this, the panel stays
      // pinned in an empty/zombie state because state.proto is still
      // truthy and renderQuest only hides on null.
      if (ev.action === "abandoned" || ev.status === "abandoned"
          || ev.status === "completed") {
        state.proto = null;
        break;
      }
      state.proto.steps = ev.steps ? ev.steps.map(cleanStep) : state.proto.steps;
      state.proto.current_step_id = ev.current_step_id;
      if (Array.isArray(ev.history_tail)) {
        state.proto.history = state.proto.history.concat(ev.history_tail);
      }
      break;
    case "protocol_completed":
      state.proto = null;
      break;
    case "protocol_cleared":
      // Emitted by the runtime at WS-open when the resolved conv has no
      // active protocol. Without this, switching from a conv with a
      // running wizard to a fresh conv left the previous wizard pinned
      // on screen because no `protocol_proposed` arrives to overwrite
      // state.proto and silence ≠ "no protocol here".
      state.proto = null;
      break;
    case "protocol_pending_confirmation":
      // Pattern 4 round-trip: the agent called bv_propose_protocol but the
      // runtime parked the call until the tech accepts or rejects via the
      // modal. No state.proto change yet — only on accept will the tool
      // dispatch and a real `protocol_proposed` arrive.
      showConfirmation(ev);
      return;
    case "protocol_confirmation_timeout":
      // Backend bailed on the wait — drop the modal so the tech doesn't
      // click into a void.
      hideConfirmation(ev.tool_use_id);
      return;
    default:
      return;
  }
  notify();
}

// --- Pattern 4 confirmation modal --------------------------------------------
// Backed by the static markup in web/index.html (#protocolConfirmBackdrop).
// Lazily wired on first show so the page can boot without the modal panel
// in the DOM (e.g. test harnesses, headless preview).

let _confirmWired = false;
let _activeConfirmId = null;

function _wireConfirmModal() {
  if (_confirmWired) return;
  const backdrop = document.getElementById("protocolConfirmBackdrop");
  if (!backdrop) return;
  const acceptBtn = document.getElementById("protocolConfirmAccept");
  const rejectBtn = document.getElementById("protocolConfirmReject");
  const rejectField = document.getElementById("protocolConfirmRejectField");
  const reasonInput = document.getElementById("protocolConfirmReason");

  // First click on Refuser unfolds the reason textarea — second click sends.
  // Less friction than a separate "details" step, while still letting the
  // tech send a one-click reject by hitting Refuser twice.
  let _rejectArmed = false;

  function _resetRejectArm() {
    _rejectArmed = false;
    if (rejectField) rejectField.hidden = true;
    if (rejectBtn) rejectBtn.textContent = "Refuser";
  }

  acceptBtn?.addEventListener("click", () => {
    const tid = _activeConfirmId;
    if (!tid || !state.send) return;
    state.send({
      type: "client.protocol_confirmation",
      tool_use_id: tid,
      decision: "accept",
    });
    hideConfirmation(tid);
  });

  rejectBtn?.addEventListener("click", () => {
    const tid = _activeConfirmId;
    if (!tid || !state.send) return;
    if (!_rejectArmed) {
      _rejectArmed = true;
      if (rejectField) rejectField.hidden = false;
      if (rejectBtn) rejectBtn.textContent = "Confirmer le refus";
      reasonInput?.focus();
      return;
    }
    const reason = (reasonInput?.value || "").trim();
    state.send({
      type: "client.protocol_confirmation",
      tool_use_id: tid,
      decision: "reject",
      reason,
    });
    hideConfirmation(tid);
  });

  // Re-arm the reject button on every open so a previous reject doesn't
  // leak its expanded textarea state into the next proposal.
  backdrop.addEventListener("transitionend", (ev) => {
    if (ev.target === backdrop && !backdrop.classList.contains("open")) {
      _resetRejectArm();
      if (reasonInput) reasonInput.value = "";
    }
  });

  _confirmWired = true;
}

function _renderStep(step, idx) {
  const li = document.createElement("li");
  const num = document.createElement("span");
  num.className = "step-num";
  num.textContent = `${idx + 1}.`;
  li.appendChild(num);

  const instr = document.createElement("span");
  instr.className = "step-instr";

  if (step.type) {
    const t = document.createElement("span");
    t.className = "step-type";
    t.textContent = step.type;
    instr.appendChild(t);
  }
  const target = step.target || step.test_point;
  if (target) {
    const tg = document.createElement("span");
    tg.className = "step-target";
    tg.textContent = target;
    instr.appendChild(tg);
  }
  const text = document.createTextNode(decodeEscapes(step.instruction || ""));
  instr.appendChild(text);
  li.appendChild(instr);
  return li;
}

export function showConfirmation(ev) {
  _wireConfirmModal();
  const backdrop = document.getElementById("protocolConfirmBackdrop");
  if (!backdrop) return;
  _activeConfirmId = ev.tool_use_id;

  const titleEl = document.getElementById("protocolConfirmTitle");
  const rationaleEl = document.getElementById("protocolConfirmRationale");
  const stepsEl = document.getElementById("protocolConfirmSteps");
  const countEl = document.getElementById("protocolConfirmStepCount");

  const t = window.t || ((k) => k);
  if (titleEl) titleEl.textContent = decodeEscapes(ev.title || t("protocol.confirm.badge"));
  if (rationaleEl) rationaleEl.textContent = decodeEscapes(ev.rationale || "—");
  if (countEl) {
    const n = Number(ev.step_count || (ev.steps || []).length || 0);
    countEl.textContent = t("protocol.confirm.step_count", { n });
  }
  if (stepsEl) {
    stepsEl.innerHTML = "";
    (ev.steps || []).forEach((s, i) => stepsEl.appendChild(_renderStep(s, i)));
  }

  backdrop.classList.add("open");
  backdrop.setAttribute("aria-hidden", "false");
}

export function hideConfirmation(toolUseId) {
  const backdrop = document.getElementById("protocolConfirmBackdrop");
  if (!backdrop) return;
  // Guard against a stale timeout/close racing past a fresh open — only
  // dismiss the modal that matches the active id.
  if (toolUseId && _activeConfirmId && toolUseId !== _activeConfirmId) return;
  backdrop.classList.remove("open");
  backdrop.setAttribute("aria-hidden", "true");
  _activeConfirmId = null;
}

export function submitStepResult({ stepId, value, unit, observation }) {
  if (!state.proto || !state.send) return;
  state.send({
    type: "protocol_step_result",
    protocol_id: state.proto.protocol_id,
    step_id: stepId,
    value, unit, observation,
  });
}

export function skipStep({ stepId, reason }) {
  if (!state.proto || !state.send) return;
  state.send({
    type: "protocol_step_result",
    protocol_id: state.proto.protocol_id,
    step_id: stepId,
    skip_reason: reason || "tech: skip",
  });
}

export function abandonProtocol(reason) {
  if (!state.proto || !state.send) return;
  state.send({
    type: "protocol_abandon",
    protocol_id: state.proto.protocol_id,
    reason: (reason && reason.trim()) || "tech_dismiss",
  });
}

// --- Abandon confirmation modal ----------------------------------------------
// Mirrors the showConfirmation/hideConfirmation pattern (initial proposal),
// but for the in-flight abandon path: the static markup lives in
// web/index.html under #protocolAbandonBackdrop, lazy-wired on first show.

let _abandonWired = false;

function _wireAbandonModal() {
  if (_abandonWired) return;
  const backdrop = document.getElementById("protocolAbandonBackdrop");
  if (!backdrop) return;
  const cancelBtn = document.getElementById("protocolAbandonCancel");
  const confirmBtn = document.getElementById("protocolAbandonConfirm");
  const reasonInput = document.getElementById("protocolAbandonReason");

  cancelBtn?.addEventListener("click", () => {
    hideAbandonModal();
  });

  confirmBtn?.addEventListener("click", () => {
    const reason = reasonInput ? reasonInput.value : "";
    abandonProtocol(reason);
    hideAbandonModal();
  });

  // Close on Escape and on backdrop click (outside the dialog).
  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop) hideAbandonModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && backdrop.classList.contains("open")) {
      hideAbandonModal();
    }
  });

  _abandonWired = true;
}

export function showAbandonModal() {
  _wireAbandonModal();
  const backdrop = document.getElementById("protocolAbandonBackdrop");
  if (!backdrop) return;
  // Reset the reason field every time so a previous abandon doesn't leak in.
  const reasonInput = document.getElementById("protocolAbandonReason");
  if (reasonInput) reasonInput.value = "";
  backdrop.classList.add("open");
  backdrop.setAttribute("aria-hidden", "false");
}

export function hideAbandonModal() {
  const backdrop = document.getElementById("protocolAbandonBackdrop");
  if (!backdrop) return;
  backdrop.classList.remove("open");
  backdrop.setAttribute("aria-hidden", "true");
}

// --- Wizard renderer + form builders -----------------------------------------

function numberFromStepId(id) {
  // s_1 → 1, ins_xx → "+"
  const m = /^s_(\d+)$/.exec(id);
  return m ? m[1] : "+";
}

function formatResult(step) {
  const r = step.result;
  if (!r) return "";
  if (step.type === "numeric") return `${r.value} ${r.unit || step.unit || ""} (${r.outcome})`;
  if (step.type === "boolean") return `${r.value ? t("protocol.step.result.yes") : t("protocol.step.result.no")} (${r.outcome})`;
  if (step.type === "observation") return r.value || t("protocol.step.result.empty");
  if (step.type === "ack") return t("protocol.step.result.done");
  return JSON.stringify(r);
}

function submitBoolean(step, value) {
  submitStepResult({ stepId: step.id, value });
}

function handleSubmit(step, form) {
  const fd = new FormData(form);
  if (step.type === "numeric") {
    const val = parseFloat(fd.get("value"));
    if (Number.isNaN(val)) return;
    submitStepResult({ stepId: step.id, value: val, unit: fd.get("unit") || step.unit });
  } else if (step.type === "observation") {
    const obs = String(fd.get("observation") || "").trim();
    if (!obs) return;
    submitStepResult({ stepId: step.id, value: obs });
  } else if (step.type === "ack") {
    submitStepResult({ stepId: step.id, value: "done" });
  }
}

export function buildStepForm(step) {
  const form = document.createElement("form");
  form.className = "protocol-step-form";
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    handleSubmit(step, form);
  });

  if (step.type === "numeric") {
    const input = document.createElement("input");
    input.type = "number"; input.step = "any"; input.required = true;
    input.placeholder = step.nominal != null
      ? t("protocol.step.nominal_placeholder", { value: step.nominal })
      : t("protocol.step.value_placeholder");
    input.name = "value";
    form.appendChild(input);
    const unit = document.createElement("select");
    unit.name = "unit";
    for (const u of ["V", "mV", "A", "mA", "Ω", "kΩ"]) {
      const opt = document.createElement("option");
      opt.value = u; opt.textContent = u;
      if (u === step.unit) opt.selected = true;
      unit.appendChild(opt);
    }
    form.appendChild(unit);
  } else if (step.type === "boolean") {
    const yes = document.createElement("button");
    yes.type = "button"; yes.textContent = t("protocol.step.yes");
    yes.addEventListener("click", () => submitBoolean(step, true));
    const no = document.createElement("button");
    no.type = "button"; no.textContent = t("protocol.step.no"); no.classList.add("is-skip");
    no.addEventListener("click", () => submitBoolean(step, false));
    form.appendChild(yes); form.appendChild(no);
  } else if (step.type === "observation") {
    const ta = document.createElement("textarea");
    ta.name = "observation"; ta.rows = 2; ta.required = true;
    ta.placeholder = t("protocol.step.observation_placeholder");
    form.appendChild(ta);
  } else if (step.type === "ack") {
    // ack: just a Done button below; submit fires submit event with no value.
  }

  const submit = document.createElement("button");
  submit.type = "submit";
  submit.textContent = step.type === "ack" ? t("protocol.step.done") : t("protocol.step.validate");
  form.appendChild(submit);

  const skip = document.createElement("button");
  skip.type = "button"; skip.className = "is-skip"; skip.textContent = t("protocol.step.skip");
  skip.addEventListener("click", () => {
    const reason = window.prompt(t("protocol.step.skip_prompt"), "");
    if (reason !== null) skipStep({ stepId: step.id, reason });
  });
  form.appendChild(skip);

  return form;
}

function renderStepRow(step, isActive) {
  const li = document.createElement("li");
  li.className = `protocol-step is-${step.status}`;
  li.dataset.stepId = step.id;

  const badge = document.createElement("span");
  badge.className = "protocol-step-badge";
  badge.textContent = step.status === "done" ? "✓"
                    : step.status === "skipped" ? "·"
                    : step.status === "failed" ? "✗"
                    : numberFromStepId(step.id);
  li.appendChild(badge);

  const body = document.createElement("div");
  body.className = "protocol-step-body";

  const target = document.createElement("div");
  target.className = "protocol-step-target";
  target.textContent = step.target || step.test_point || t("protocol.step.result.empty");
  body.appendChild(target);

  const instr = document.createElement("p");
  instr.className = "protocol-step-instruction";
  instr.textContent = step.instruction;
  body.appendChild(instr);

  const why = document.createElement("p");
  why.className = "protocol-step-rationale";
  why.textContent = step.rationale;
  body.appendChild(why);

  if (step.result && step.status !== "active") {
    const res = document.createElement("div");
    res.className = "protocol-step-result";
    res.textContent = formatResult(step);
    body.appendChild(res);
  }

  if (isActive) {
    body.appendChild(buildStepForm(step));
  }

  li.appendChild(body);
  return li;
}

function renderQuest(proto) {
  const root = document.getElementById("protocolQuest");
  if (!root) return;
  if (!proto) {
    root.classList.add("hidden");
    document.body.classList.remove("has-protocol-quest");
    return;
  }
  root.classList.remove("hidden");
  document.body.classList.add("has-protocol-quest");
  document.getElementById("protocolTitle").textContent = proto.title;

  const total = proto.steps.length;
  const doneCount = proto.steps.filter((s) =>
    s.status === "done" || s.status === "skipped" || s.status === "failed"
  ).length;
  const counter = document.getElementById("protocolCounter");
  if (counter) counter.textContent = `${doneCount} / ${total}`;

  const list = document.getElementById("protocolStepList");
  list.innerHTML = "";
  for (const step of proto.steps) {
    list.appendChild(renderStepRow(step, step.id === proto.current_step_id));
  }
  const histList = document.getElementById("protocolHistoryList");
  histList.innerHTML = "";
  for (const h of proto.history.slice(-10)) {
    const li = document.createElement("li");
    li.textContent = `${h.action}${h.step_id ? " · " + h.step_id : ""}${h.reason ? " · " + h.reason : ""}`;
    histList.appendChild(li);
  }
}

// Bind chrome buttons (toggle collapse, abandon) once on first render.
const bindChrome = () => {
  const toggle = document.getElementById("protocolToggleBtn");
  const root = document.getElementById("protocolQuest");
  if (toggle && root && !toggle.dataset.bound) {
    toggle.addEventListener("click", () => {
      const collapsed = root.classList.toggle("is-collapsed");
      toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
      toggle.setAttribute("title", collapsed
        ? t("protocol.toggle.expand")
        : t("protocol.toggle.collapse"));
    });
    toggle.dataset.bound = "1";
  }
  const btn = document.getElementById("protocolAbandonBtn");
  if (btn && !btn.dataset.bound) {
    btn.addEventListener("click", () => {
      showAbandonModal();
    });
    btn.dataset.bound = "1";
  }
};

subscribe(renderQuest);
subscribe(bindChrome);

// Re-render on locale switch — picks up i18n strings inside dynamically built rows.
if (window.i18n && window.i18n.onChange) {
  window.i18n.onChange(() => notify());
}

let _lastFocusedStepId = null;

function pushBadgesToBoard(proto) {
  if (!window.Boardview || !window.Boardview.setProtocolBadges) return;
  if (!proto) {
    window.Boardview.clearProtocolBadges();
    _lastFocusedStepId = null;
    return;
  }
  const minimal = proto.steps.map((s) => ({
    id: s.id, target: s.target, status: s.status,
  }));
  window.Boardview.setProtocolBadges(minimal, proto.current_step_id);
  // Auto-focus the camera on the active step's target when it changes —
  // fires on first accept (transition from null → step1.id) and on every
  // subsequent step transition. Same target as the previous push = no-op.
  if (proto.current_step_id !== _lastFocusedStepId) {
    _lastFocusedStepId = proto.current_step_id;
    const active = proto.steps.find((s) => s.id === proto.current_step_id);
    if (active && active.target && window.Boardview.focus) {
      try { window.Boardview.focus(active.target); } catch (_) {}
    }
  }
}
subscribe(pushBadgesToBoard);

function escapeHtmlLocal(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

// Floating refdes pin — read-only chip anchored above the active step's
// target component. Just a badge number + refdes label + arrow pointing
// to the quest tracker (top-right). Form input lives in the tracker.
function renderFloating(proto) {
  const card = document.getElementById("protocolFloatingCard");
  if (!card) return;
  if (!proto || !state.hasBoard) {
    card.classList.add("hidden");
    return;
  }
  const active = proto.steps.find((s) => s.id === proto.current_step_id);
  if (!active || !active.target) {
    card.classList.add("hidden");
    return;
  }
  const screenPos = window.Boardview?.refdesScreenPos?.(active.target);
  if (!screenPos) { card.classList.add("hidden"); return; }

  card.classList.remove("hidden");
  // Anchor the chip just above the bbox; centered horizontally on the part.
  // The chip is left-aligned to its inline-flex content so we offset by half
  // an estimated width for visual balance.
  card.style.left = `${screenPos.x - 40}px`;
  card.style.top  = `${screenPos.y - 32}px`;

  const idx = proto.steps.findIndex((s) => s.id === active.id) + 1;
  card.innerHTML =
    `<span class="protocol-float-badge">${idx}</span>` +
    `<span class="protocol-float-target">${escapeHtmlLocal(active.target)}</span>` +
    `<svg class="protocol-float-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
    `stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `<path d="M7 17L17 7M17 7H9M17 7v8"/></svg>`;
}
subscribe(renderFloating);
