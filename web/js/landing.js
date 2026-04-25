// Landing hero — captures {device_label, symptom}, kicks the existing
// /pipeline/repairs endpoint, and renders a live narrated timeline of the
// pipeline phases as the agent learns the device. When the pipeline finishes
// (or the pack was already on disk) the page redirects into the workspace
// at ?repair={id}&device={slug}.
//
// No classifier here — the existing pipeline (Scout → Registry → Mapper? →
// Writers ×3 → Auditor) does device identification + knowledge construction
// in one shot. The narrator agent (api/pipeline/phase_narrator.py) emits a
// `phase_narration` event after each phase_finished; we render those into
// the timeline rows so the technician watches the agent learn.

const STATUS_NEUTRAL = "";
const STATUS_LOADING = "loading";
const STATUS_ERROR = "error";

const PHASE_ORDER = ["scout", "registry", "mapper", "writers", "audit"];

let isSubmitting = false;
let progressWs = null;
let pipelineStartedAt = 0;

export function showLanding() {
  document.body.classList.add("show-landing");
  const ov = document.getElementById("landing-overlay");
  if (ov) ov.hidden = false;
  setTimeout(() => document.getElementById("landingDevice")?.focus(), 50);
}

export function hideLanding() {
  document.body.classList.remove("show-landing");
  const ov = document.getElementById("landing-overlay");
  if (ov) ov.hidden = true;
  if (progressWs && progressWs.readyState <= 1) {
    try { progressWs.close(); } catch (_) {}
  }
  progressWs = null;
}

function setStatus(msg, kind) {
  const el = document.getElementById("landingStatus");
  if (!el) return;
  el.textContent = msg || "";
  el.classList.remove("error");
  if (kind === STATUS_ERROR) el.classList.add("error");
}

function setSubmitting(on) {
  isSubmitting = on;
  const btn = document.getElementById("landingSubmit");
  if (btn) btn.disabled = on;
  const dev = document.getElementById("landingDevice");
  const sym = document.getElementById("landingSymptom");
  if (dev) dev.disabled = on;
  if (sym) sym.disabled = on;
}

function showTimeline() {
  const tl = document.getElementById("landingTimeline");
  if (tl) tl.hidden = false;
  pipelineStartedAt = Date.now();
  startEtaTicker();
}

function startEtaTicker() {
  const eta = document.getElementById("landingTimelineEta");
  if (!eta) return;
  if (window.__landingEtaTimer) clearInterval(window.__landingEtaTimer);
  const tick = () => {
    const elapsed = Math.max(0, (Date.now() - pipelineStartedAt) / 1000);
    eta.textContent = `${elapsed.toFixed(0)}s`;
  };
  tick();
  window.__landingEtaTimer = setInterval(tick, 250);
}

function stopEtaTicker() {
  if (window.__landingEtaTimer) {
    clearInterval(window.__landingEtaTimer);
    window.__landingEtaTimer = null;
  }
}

function setPhaseState(phase, state) {
  // state ∈ "running" | "done" | "failed"
  const li = document.querySelector(`.landing-phase[data-phase="${phase}"]`);
  if (!li) return;
  li.hidden = false;  // mapper starts hidden until a phase_started arrives
  li.classList.remove("is-running", "is-done", "is-failed");
  if (state === "running") li.classList.add("is-running");
  if (state === "done") li.classList.add("is-done");
  if (state === "failed") li.classList.add("is-failed");
}

function setPhaseNarration(phase, text) {
  const li = document.querySelector(`.landing-phase[data-phase="${phase}"]`);
  if (!li) return;
  const slot = li.querySelector(".landing-phase-narration");
  if (!slot) return;
  slot.textContent = text;
  li.classList.add("has-narration");
}

function setTimelineTitle(text) {
  const t = document.getElementById("landingTimelineTitle");
  if (t) t.textContent = text;
}

function resetTimeline() {
  PHASE_ORDER.forEach((p) => {
    const li = document.querySelector(`.landing-phase[data-phase="${p}"]`);
    if (!li) return;
    li.classList.remove("is-running", "is-done", "is-failed", "has-narration");
    if (p === "mapper") li.hidden = true;
    const slot = li.querySelector(".landing-phase-narration");
    if (slot) slot.textContent = "";
  });
}

async function onSubmit(ev) {
  ev.preventDefault();
  if (isSubmitting) return;
  const deviceEl = document.getElementById("landingDevice");
  const symptomEl = document.getElementById("landingSymptom");
  const device = (deviceEl?.value || "").trim();
  const symptom = (symptomEl?.value || "").trim();

  if (device.length < 2) {
    setStatus("Précise l'appareil — au moins quelques mots.", STATUS_ERROR);
    deviceEl?.focus();
    return;
  }
  if (symptom.length < 5) {
    setStatus("Décris un peu plus le symptôme.", STATUS_ERROR);
    symptomEl?.focus();
    return;
  }

  setStatus("J'enregistre ta réparation et je vérifie si je connais déjà cet appareil…", STATUS_LOADING);
  setSubmitting(true);
  resetTimeline();

  try {
    const res = await fetch("/pipeline/repairs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_label: device, symptom }),
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status} ${detail}`);
    }
    const repair = await res.json();
    const rid = repair.repair_id;
    const slug = repair.device_slug;
    if (!rid || !slug) throw new Error("réponse invalide du serveur");

    if (!repair.pipeline_started) {
      setStatus(`Je connais déjà ${repair.device_label}. J'ouvre le diagnostic…`, STATUS_NEUTRAL);
      goToWorkspace(rid, slug);
      return;
    }

    setStatus("Nouveau pour moi — je construis la fiche en arrière-plan. Tu peux regarder.", STATUS_NEUTRAL);
    showTimeline();
    setTimelineTitle(`Construction de la fiche · ${repair.device_label}`);
    subscribeToProgress(slug, rid);
  } catch (err) {
    console.error("[landing] submit failed", err);
    setStatus(`Échec de la création : ${err.message || err}`, STATUS_ERROR);
    setSubmitting(false);
  }
}

function subscribeToProgress(slug, repairId) {
  if (progressWs && progressWs.readyState <= 1) {
    try { progressWs.close(); } catch (_) {}
  }
  const proto = (location.protocol === "https:") ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/pipeline/progress/${encodeURIComponent(slug)}`;

  progressWs = new WebSocket(url);

  progressWs.addEventListener("open", () => {
    console.log("[landing] progress WS open", slug);
  });

  progressWs.addEventListener("message", (ev) => {
    let data;
    try { data = JSON.parse(ev.data); }
    catch { return; }
    handleProgressEvent(data, slug, repairId);
  });

  progressWs.addEventListener("error", (ev) => {
    console.warn("[landing] progress WS error", ev);
    setStatus("Connexion au pipeline interrompue. Recharge la page si rien ne bouge.", STATUS_ERROR);
  });

  progressWs.addEventListener("close", () => {
    stopEtaTicker();
  });
}

function handleProgressEvent(ev, slug, repairId) {
  switch (ev.type) {
    case "subscribed":
      break;
    case "pipeline_started":
      setStatus(`Pipeline démarré sur ${ev.device_slug || slug}.`, STATUS_LOADING);
      break;
    case "phase_started": {
      const phase = ev.phase;
      if (PHASE_ORDER.includes(phase)) setPhaseState(phase, "running");
      break;
    }
    case "phase_finished": {
      const phase = ev.phase;
      if (PHASE_ORDER.includes(phase)) setPhaseState(phase, "done");
      break;
    }
    case "phase_narration": {
      const phase = ev.phase;
      const text = (ev.text || "").trim();
      if (text && PHASE_ORDER.includes(phase)) setPhaseNarration(phase, text);
      break;
    }
    case "pipeline_finished": {
      setTimelineTitle(`Fiche prête · ${ev.status || ""}`);
      setStatus("C'est prêt. J'ouvre le diagnostic…", STATUS_NEUTRAL);
      stopEtaTicker();
      setTimeout(() => goToWorkspace(repairId, slug), 1200);
      break;
    }
    case "pipeline_failed": {
      setTimelineTitle("Pipeline échoué");
      setStatus(`Erreur : ${ev.error || ev.status || "inconnue"}.`, STATUS_ERROR);
      const running = document.querySelector(".landing-phase.is-running");
      if (running) {
        running.classList.remove("is-running");
        running.classList.add("is-failed");
      }
      stopEtaTicker();
      setSubmitting(false);
      break;
    }
    default:
      break;
  }
}

function goToWorkspace(repairId, slug) {
  const url = new URL(location.href);
  url.searchParams.set("repair", repairId);
  url.searchParams.set("device", slug);
  url.searchParams.delete("landing");
  location.href = url.toString();
}

function onChipClick(ev) {
  const btn = ev.target.closest(".landing-chip");
  if (!btn) return;
  const dev = document.getElementById("landingDevice");
  const sym = document.getElementById("landingSymptom");
  if (dev && btn.dataset.device) dev.value = btn.dataset.device;
  if (sym && btn.dataset.symptom) sym.value = btn.dataset.symptom;
  sym?.focus();
}

export function initLanding() {
  const form = document.getElementById("landingForm");
  if (form) form.addEventListener("submit", onSubmit);
  const chips = document.getElementById("landingChips");
  if (chips) chips.addEventListener("click", onChipClick);
}
