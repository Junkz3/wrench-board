// Pipeline progress drawer — bottom-right glass UI that streams live
// pipeline events via WS /pipeline/progress/{slug} and transitions the
// 4-step stepper (Scout → Registry → Writers → Audit) as events flow.
//
// Entry point: openPipelineProgress({repair_id, device_slug, device_label,
// pipeline_started}). When pipeline_started=false the pack is already
// complete on disk, so we skip the drawer and redirect immediately.

import { escapeHtml as escHtml } from "./shared/dom.js";
import { connectProgress, fetchPendingKind } from "./services/pipelineSocket.js";
import { repairHash, seedSlugForRepair } from "./router.js";

const PHASES = [
  {key: "scout",    labelKey: "pipeline.phase.scout.label",    subKey: "pipeline.phase.scout.sub"},
  {key: "registry", labelKey: "pipeline.phase.registry.label", subKey: "pipeline.phase.registry.sub"},
  {key: "writers",  labelKey: "pipeline.phase.writers.label",  subKey: "pipeline.phase.writers.sub"},
  {key: "audit",    labelKey: "pipeline.phase.audit.label",    subKey: "pipeline.phase.audit.sub"},
];

function _phaseLabel(key)  { return t(PHASES.find(p => p.key === key)?.labelKey || ""); }
function _phaseSub(key)    { return t(PHASES.find(p => p.key === key)?.subKey || ""); }

// Dynamic phases the orchestrator only emits when a schematic is ingested.
// They render as extra .pp-step rows, prepended before Scout, the first time
// their phase_started arrives — so a schematic-less build keeps the 4 static
// steps. Keyed by phase name to match phase_started/phase_finished payloads.
const DYNAMIC_PHASES = {
  schematic_ingest: { label: "pipeline.phase.schematic_ingest.label", sub: "pipeline.phase.schematic_ingest.sub" },
  device_kind:      { label: "pipeline.phase.device_kind.label",      sub: "pipeline.phase.device_kind.sub" },
};

function ensureDynamicStep(phaseKey) {
  const meta = DYNAMIC_PHASES[phaseKey];
  if (!meta) return;
  const body = el("ppBody");
  if (!body || body.querySelector(`.pp-step[data-step="${phaseKey}"]`)) return;
  const step = document.createElement("div");
  step.className = "pp-step";
  step.dataset.step = phaseKey;
  step.innerHTML = `
    <div class="pp-step-mark" aria-hidden="true"></div>
    <div class="pp-step-lbl" data-i18n="${meta.label}">${escHtml(t(meta.label))}</div>
    <div class="pp-step-sub" data-i18n="${meta.sub}" data-role="sub">${escHtml(t(meta.sub))}</div>
    <div class="pp-step-time" data-role="time">${escHtml(t("pipeline.step.time_placeholder"))}</div>`;
  // Prepend before the first static step (scout) so ingestion/classification
  // read first: Schéma → Type → Scout → Registry → Writers → Audit.
  const firstStatic = body.querySelector('.pp-step[data-step="scout"]');
  body.insertBefore(step, firstStatic);
  if (window.i18n && window.i18n.applyDom) window.i18n.applyDom(step);
}

const STATE = {
  conn: null,
  slug: null,
  repairId: null,
  deviceLabel: null,
  done: false,
  failed: false,
  paused: false,
  redirectTimer: null,
};

function el(id) { return document.getElementById(id); }

function fmtElapsed(sec) {
  if (typeof sec !== "number" || !isFinite(sec)) return "…";
  if (sec < 1) return `${(sec * 1000).toFixed(0)} ms`;
  if (sec < 60) return `${sec.toFixed(1)} s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}m ${s}s`;
}

function buildDrawer() {
  if (el("pipelineProgressDrawer")) return;
  const drawer = document.createElement("aside");
  drawer.className = "pp-drawer";
  drawer.id = "pipelineProgressDrawer";
  drawer.setAttribute("role", "status");
  drawer.setAttribute("aria-live", "polite");
  drawer.innerHTML = `
    <header class="pp-head">
      <span class="pp-dot" aria-hidden="true"></span>
      <div class="pp-title">
        <span class="lbl" data-i18n="pipeline.drawer.header_label">Building memory</span>
        <span class="name" id="ppDeviceLabel">…</span>
      </div>
      <button class="pp-close" id="ppClose"
              data-i18n-attr="aria-label:pipeline.drawer.close_aria"
              aria-label="Close progress panel" type="button">
        <svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
    </header>
    <div class="pp-body" id="ppBody">
      ${PHASES.map((p, i) => `
        <div class="pp-step" data-step="${p.key}" data-idx="${i}">
          <div class="pp-step-mark" aria-hidden="true"></div>
          <div class="pp-step-lbl" data-i18n="${p.labelKey}">${escHtml(_phaseLabel(p.key))}</div>
          <div class="pp-step-sub" data-i18n="${p.subKey}" data-role="sub">${escHtml(_phaseSub(p.key))}</div>
          <div class="pp-step-time" data-role="time">${escHtml(t("pipeline.step.time_placeholder"))}</div>
        </div>`).join("")}
    </div>
    <footer class="pp-foot">
      <div class="pp-status" id="ppStatus" data-i18n="pipeline.status.waiting">Waiting for first events…</div>
      <button class="pp-cta hidden" id="ppCta" type="button"></button>
    </footer>
  `;
  document.body.appendChild(drawer);
  if (window.i18n && window.i18n.applyDom) window.i18n.applyDom(drawer);

  el("ppClose").addEventListener("click", closeDrawer);
}

function openDrawer(deviceLabel) {
  buildDrawer();
  el("ppDeviceLabel").textContent = deviceLabel || t("pipeline.drawer.device_placeholder");
  setStatusKey("pipeline.status.connecting");
  el("ppCta").classList.add("hidden");
  el("ppCta").textContent = "";
  // Reset step states. Drop dynamic steps injected by a prior run first so a
  // fresh open starts clean — they re-inject when their phase_started arrives
  // again — then reset the static steps.
  document.querySelectorAll("#pipelineProgressDrawer .pp-step").forEach(s => {
    if (s.dataset.step in DYNAMIC_PHASES) { s.remove(); return; }
    s.classList.remove("running", "done", "error");
    s.querySelector('[data-role="time"]').textContent = t("pipeline.step.time_placeholder");
    const sub = s.querySelector(".pp-step-sub");
    if (sub) {
      const phase = PHASES.find(p => p.key === s.dataset.step);
      if (phase) {
        sub.setAttribute("data-i18n", phase.subKey);
        sub.textContent = _phaseSub(s.dataset.step);
      }
    }
  });
  // Remove any lingering error panel
  document.getElementById("ppErrorDetail")?.remove();
  // Remove any stale device-kind confirmation panel (plain div in ppBody,
  // not a dynamic .pp-step so it survives the loop above).
  document.getElementById("ppKindPanel")?.remove();
  requestAnimationFrame(() => {
    el("pipelineProgressDrawer").classList.add("open");
  });
}

function closeDrawer() {
  const drawer = el("pipelineProgressDrawer");
  if (!drawer) return;
  drawer.classList.remove("open");
  if (STATE.conn) {
    STATE.conn.close(1000, "user-closed");
    STATE.conn = null;
  }
  if (STATE.redirectTimer) {
    clearTimeout(STATE.redirectTimer);
    STATE.redirectTimer = null;
  }
}

function setStepState(phaseKey, klass) {
  const step = document.querySelector(`#pipelineProgressDrawer .pp-step[data-step="${phaseKey}"]`);
  if (!step) return;
  step.classList.remove("running", "done", "error");
  if (klass) step.classList.add(klass);
}

function setStepTime(phaseKey, text) {
  const step = document.querySelector(`#pipelineProgressDrawer .pp-step[data-step="${phaseKey}"]`);
  if (!step) return;
  const cell = step.querySelector('[data-role="time"]');
  if (cell) cell.textContent = text;
}

function setStepCounts(phaseKey, counts) {
  if (!counts || typeof counts !== "object") return;
  const step = document.querySelector(`#pipelineProgressDrawer .pp-step[data-step="${phaseKey}"]`);
  if (!step) return;
  const sub = step.querySelector(".pp-step-sub");
  if (!sub) return;
  const parts = Object.entries(counts).map(([k, v]) => `${v} ${k}`);
  sub.textContent = parts.join(" · ");
}

// setStatus accepts pre-resolved HTML text (for paths that interpolate <b>);
// setStatusKey accepts an i18n key + params and re-resolves on locale switch.
function setStatus(text, klass) {
  const s = el("ppStatus");
  if (!s) return;
  s.removeAttribute("data-i18n");
  delete s.dataset.i18nKey;
  delete s.dataset.i18nParams;
  s.dataset.i18nHtml = "1";
  s.innerHTML = text;
  s.className = "pp-status" + (klass ? " " + klass : "");
}

function setStatusKey(key, params, klass) {
  const s = el("ppStatus");
  if (!s) return;
  s.dataset.i18nKey = key;
  s.dataset.i18nParams = params ? JSON.stringify(params) : "";
  s.dataset.i18nHtml = "1";
  s.removeAttribute("data-i18n");
  s.innerHTML = t(key, params);
  s.className = "pp-status" + (klass ? " " + klass : "");
}

function _refreshStatusOnLocaleChange() {
  const s = el("ppStatus");
  if (!s || !s.dataset.i18nKey) return;
  let params;
  try { params = s.dataset.i18nParams ? JSON.parse(s.dataset.i18nParams) : undefined; }
  catch (_) { params = undefined; }
  s.innerHTML = t(s.dataset.i18nKey, params);
}

if (window.i18n && window.i18n.onChange) {
  window.i18n.onChange(() => {
    _refreshStatusOnLocaleChange();
    // Re-translate dynamic step time/sub cells whose text follows a known token.
    document.querySelectorAll("#pipelineProgressDrawer .pp-step").forEach(s => {
      const time = s.querySelector('[data-role="time"]');
      if (!time) return;
      // Static placeholder → re-localize. "running…" / "failed" cells follow,
      // but the running state flips when the next event lands so transient
      // localization drift is tolerable.
      if (s.classList.contains("running")) time.textContent = t("pipeline.step.in_progress");
      else if (s.classList.contains("error")) time.textContent = t("pipeline.step.failed");
    });
  });
}

function showCta(label, iconPath, primary, onClick) {
  const btn = el("ppCta");
  if (!btn) return;
  btn.classList.remove("hidden");
  btn.classList.toggle("primary", !!primary);
  btn.innerHTML = `${iconPath ? `<svg class="icon icon-sm" viewBox="0 0 24 24">${iconPath}</svg>` : ""}${escHtml(label)}`;
  btn.onclick = onClick;
}

function showErrorDetail(msg) {
  if (!msg) return;
  document.getElementById("ppErrorDetail")?.remove();
  const div = document.createElement("div");
  div.className = "pp-error-detail";
  div.id = "ppErrorDetail";
  div.textContent = msg;
  el("ppBody").appendChild(div);
}

function handleEvent(ev) {
  switch (ev.type) {
    case "subscribed":
      // Ack — the pipeline may already have started. Wait for pipeline_started
      // or the first phase_started to flip the UI.
      break;

    case "queued": {
      // Build en attente derrière le cap de builds concurrents : position visible,
      // décroît à mesure que la file se vide ; pipeline_started prendra le relais.
      const position = ev.position || 1;
      const ahead = ev.ahead != null ? ev.ahead : Math.max(0, position - 1);
      setStatusKey("pipeline.status.queued", { position, ahead });
      break;
    }

    case "pipeline_started":
      setStatusKey("pipeline.status.started");
      break;

    case "phase_started":
      ensureDynamicStep(ev.phase);
      setStepState(ev.phase, "running");
      setStepTime(ev.phase, t("pipeline.step.in_progress"));
      setStatusKey("pipeline.status.phase_running", { phase: escHtml(ev.phase || "") });
      break;

    case "phase_finished":
      setStepState(ev.phase, "done");
      setStepTime(ev.phase, fmtElapsed(ev.elapsed_s));
      if (ev.counts) setStepCounts(ev.phase, ev.counts);
      break;

    case "pipeline_finished": {
      STATE.done = true;
      const score = typeof ev.consistency_score === "number"
        ? ev.consistency_score.toFixed(2) : "n/a";
      const status = ev.status || "APPROVED";
      setStatusKey("pipeline.status.ready", { status: escHtml(status), score }, "ok");
      showCta(
        t("pipeline.cta.view_memory_bank"),
        '<path d="M5 12h14M13 6l6 6-6 6"/>',
        true,
        () => redirectToMemoryBank(),
      );
      // Auto-redirect after 2s unless the user clicks Close first.
      STATE.redirectTimer = setTimeout(redirectToMemoryBank, 2000);
      break;
    }

    case "pipeline_failed": {
      STATE.failed = true;
      // Paint the currently running step as error.
      const running = document.querySelector("#pipelineProgressDrawer .pp-step.running");
      if (running) {
        running.classList.remove("running");
        running.classList.add("error");
        running.querySelector('[data-role="time"]').textContent = t("pipeline.step.failed");
      }
      const status = ev.status || "ERROR";
      setStatusKey("pipeline.status.failed", { status: escHtml(status) }, "err");
      if (ev.error) showErrorDetail(ev.error);
      showCta(t("pipeline.cta.close"), "", false, closeDrawer);
      break;
    }

    case "pipeline_paused":
      if (ev.reason === "needs_kind_confirmation") {
        STATE.paused = true;
        ensureDynamicStep("device_kind");
        setStepState("device_kind", "running");
        renderKindConfirm(ev);
      }
      break;

    default:
      // Unknown event type — ignore silently; forward-compat.
      break;
  }
}

function redirectToMemoryBank() {
  if (!STATE.slug) return;
  if (STATE.redirectTimer) {
    clearTimeout(STATE.redirectTimer);
    STATE.redirectTimer = null;
  }
  closeDrawer();
  // Land on the repair's graph vue in markdown (memory-bank) mode. view=md lives
  // in the real query string (Decision A). Fall back to the global list — never
  // silently no-op — if no repair scope was captured.
  if (STATE.repairId) {
    seedSlugForRepair(STATE.repairId, STATE.slug);
    window.location.href = `?view=md${repairHash(STATE.repairId, "graph")}`;
  } else {
    window.location.hash = "#home";
  }
}

function subscribeProgress() {
  STATE.conn = connectProgress(STATE.slug, {
    onEvent: handleEvent,
    onError: () => setStatusKey("pipeline.status.lost_connection", null, "err"),
    onClose: () => {
      STATE.conn = null;
      // Paused is a deliberate stop (the build coroutine returned) — not a
      // failure. If the pipeline didn't reach a terminal event before the
      // close, flag it.
      if (!STATE.done && !STATE.failed && !STATE.paused) {
        setStatusKey("pipeline.status.closed_early", null, "err");
        showCta(t("pipeline.cta.close"), "", false, closeDrawer);
      }
    },
  });
}

/* ---------- pause / device-kind confirmation ---------- */

function _kindLabel(k) {
  if (!k) return t("pipeline.kind.undeclared");
  if (k === "unknown") return t("pipeline.kind.undeclared");
  const label = t("repair.device_kind.options." + k);
  // t() returns the raw key on miss → fall back to the slug itself.
  return label === ("repair.device_kind.options." + k) ? k : label;
}

function renderKindConfirm(ev) {
  document.getElementById("ppKindPanel")?.remove();
  const step = document.querySelector('#pipelineProgressDrawer .pp-step[data-step="device_kind"]');
  if (step) step.classList.add("paused");
  const conf = typeof ev.confidence === "number" ? Math.round(ev.confidence * 100) : null;
  const candidates = [];
  if (ev.graph_inferred) candidates.push({ k: ev.graph_inferred, recommended: true });
  if (ev.user_declared && ev.user_declared !== ev.graph_inferred) candidates.push({ k: ev.user_declared });
  // No inferred or declared kind → fall back to a single "unknown" radio so the
  // panel still has an actionable choice (confirm posts "unknown", pipeline proceeds).
  if (candidates.length === 0) candidates.push({ k: "unknown", recommended: true });
  const radios = candidates.map((c, i) => `
    <label class="pp-kind-opt">
      <input type="radio" name="ppKind" value="${escHtml(c.k)}" ${i === 0 ? "checked" : ""}>
      <span>${escHtml(_kindLabel(c.k))}${c.recommended ? ` <em>${escHtml(t("pipeline.kind.recommended"))}</em>` : ""}</span>
    </label>`).join("");
  const panel = document.createElement("div");
  panel.className = "pp-kind-panel";
  panel.id = "ppKindPanel";
  panel.innerHTML = `
    <div class="pp-kind-row"><span data-i18n="pipeline.kind.declared">Déclaré</span><b class="mono">${escHtml(_kindLabel(ev.user_declared))}</b></div>
    <div class="pp-kind-row"><span data-i18n="pipeline.kind.detected">Détecté</span><b class="mono">${escHtml(_kindLabel(ev.graph_inferred))}${conf !== null ? ` ${conf}%` : ""}</b></div>
    ${ev.evidence ? `<div class="pp-kind-evidence">${escHtml(ev.evidence)}</div>` : ""}
    <div class="pp-kind-opts">${radios}</div>
    <button type="button" class="pp-cta primary" id="ppKindConfirm" data-i18n="pipeline.kind.confirm">Confirmer et reprendre</button>`;
  el("ppBody").appendChild(panel);
  if (window.i18n && window.i18n.applyDom) window.i18n.applyDom(panel);
  document.getElementById("ppKindConfirm").addEventListener("click", () => {
    const chosen = panel.querySelector('input[name="ppKind"]:checked')?.value;
    if (chosen) confirmKind(chosen);
  });
}

async function confirmKind(deviceKind) {
  let ok = false;
  try {
    const res = await fetch(`/pipeline/packs/${encodeURIComponent(STATE.slug)}/confirm-kind`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_kind: deviceKind }),
    });
    ok = res.ok;
  } catch (_) { ok = false; }
  if (!ok) {
    // A 4xx/5xx (or network failure) means the pipeline did NOT resume — surface
    // it and stop, instead of re-subscribing into a confusing "closed early".
    setStatusKey("pipeline.status.lost_connection", null, "err");
    showCta(t("pipeline.cta.close"), "", false, closeDrawer);
    return;
  }
  document.getElementById("ppKindPanel")?.remove();
  const step = document.querySelector('#pipelineProgressDrawer .pp-step[data-step="device_kind"]');
  if (step) { step.classList.remove("paused"); setStepState("device_kind", "done"); }
  setStepTime("device_kind", t("pipeline.kind.confirmed"));
  // Fresh build started by confirm-kind — watch the re-run on the same slug.
  STATE.paused = false; STATE.done = false; STATE.failed = false;
  if (STATE.conn) { STATE.conn.close(1000, "resubscribe"); STATE.conn = null; }
  subscribeProgress();
}

/* ---------- public API ---------- */

export function openPipelineProgress(repairResponse) {
  if (!repairResponse || !repairResponse.device_slug) return;

  // Pack already complete on disk — skip the drawer and open the repair's graph
  // vue. Consistent with clicking a home card: the user lands on the rich visual
  // representation of the pack, not the read-only data dump.
  if (repairResponse.pipeline_started === false) {
    if (repairResponse.repair_id) {
      seedSlugForRepair(repairResponse.repair_id, repairResponse.device_slug);
      window.location.href = repairHash(repairResponse.repair_id, "graph");
    } else {
      window.location.hash = "#home";
    }
    return;
  }

  STATE.slug = repairResponse.device_slug;
  STATE.repairId = repairResponse.repair_id || null;
  STATE.deviceLabel = repairResponse.device_label || repairResponse.device_slug;
  STATE.done = false;
  STATE.failed = false;
  STATE.redirectTimer = null;

  openDrawer(STATE.deviceLabel);

  STATE.paused = false;
  subscribeProgress();

  // If a prior build is parked on a kind disagreement (e.g. after a reload),
  // the live pipeline_paused event is gone — rebuild the panel from disk.
  fetchPendingKind(STATE.slug).then(pending => {
    if (pending) {
      handleEvent({
        type: "pipeline_paused", reason: "needs_kind_confirmation",
        device_slug: STATE.slug, user_declared: pending.user_declared,
        graph_inferred: pending.graph_inferred, confidence: pending.confidence,
        evidence: pending.evidence,
      });
    }
  });
}

export function initPipelineProgress() {
  // Future-proof hook — currently the drawer is lazy-built when first shown,
  // so there's nothing to wire at bootstrap. Kept symmetric with the other
  // init* modules so main.js stays consistent.
}
