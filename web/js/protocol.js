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
        title: ev.title,
        rationale: ev.rationale,
        steps: ev.steps || [],
        current_step_id: ev.current_step_id,
        history: [],
      };
      break;
    case "protocol_updated":
      if (!state.proto || state.proto.protocol_id !== ev.protocol_id) break;
      state.proto.steps = ev.steps || state.proto.steps;
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
    default:
      return;
  }
  notify();
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

export function abandonProtocol() {
  if (!state.proto || !state.send) return;
  state.send({
    type: "protocol_abandon",
    protocol_id: state.proto.protocol_id,
    reason: "tech_dismiss",
  });
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
  if (step.type === "boolean") return `${r.value ? "oui" : "non"} (${r.outcome})`;
  if (step.type === "observation") return r.value || "—";
  if (step.type === "ack") return "fait";
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
    input.placeholder = step.nominal != null ? `nominal ${step.nominal}` : "valeur";
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
    yes.type = "button"; yes.textContent = "Oui";
    yes.addEventListener("click", () => submitBoolean(step, true));
    const no = document.createElement("button");
    no.type = "button"; no.textContent = "Non"; no.classList.add("is-skip");
    no.addEventListener("click", () => submitBoolean(step, false));
    form.appendChild(yes); form.appendChild(no);
  } else if (step.type === "observation") {
    const ta = document.createElement("textarea");
    ta.name = "observation"; ta.rows = 2; ta.required = true;
    ta.placeholder = "ce que tu observes…";
    form.appendChild(ta);
  } else if (step.type === "ack") {
    // ack: just a Done button below; submit fires submit event with no value.
  }

  const submit = document.createElement("button");
  submit.type = "submit";
  submit.textContent = step.type === "ack" ? "Fait" : "Valider";
  form.appendChild(submit);

  const skip = document.createElement("button");
  skip.type = "button"; skip.className = "is-skip"; skip.textContent = "Skip";
  skip.addEventListener("click", () => {
    const reason = window.prompt("Pourquoi tu skip ce step ?", "");
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
  target.textContent = step.target || step.test_point || "—";
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
      toggle.setAttribute("title", collapsed ? "Déplier" : "Plier");
    });
    toggle.dataset.bound = "1";
  }
  const btn = document.getElementById("protocolAbandonBtn");
  if (btn && !btn.dataset.bound) {
    btn.addEventListener("click", () => {
      if (window.confirm("Abandonner le protocole en cours ?")) abandonProtocol();
    });
    btn.dataset.bound = "1";
  }
};

subscribe(renderQuest);
subscribe(bindChrome);

function pushBadgesToBoard(proto) {
  if (!window.Boardview || !window.Boardview.setProtocolBadges) return;
  if (!proto) {
    window.Boardview.clearProtocolBadges();
    return;
  }
  const minimal = proto.steps.map((s) => ({
    id: s.id, target: s.target, status: s.status,
  }));
  window.Boardview.setProtocolBadges(minimal, proto.current_step_id);
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
