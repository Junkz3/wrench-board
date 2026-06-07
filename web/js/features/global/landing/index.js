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

import { mountMascot, setMascotState } from '../../../mascot.js';
import { prettifySlug, repairHash, seedSlugForRepair } from '../../../router.js';
import i18n from '../../../i18n.js';
import { escapeHtml as _escapeHtml } from '../../../shared/dom.js';
import { initProfileMenu, refreshProfileMenu } from './profile_menu.js';
import { maybeStartOnboarding, preGateOnboarding } from './onboarding.js';
import { openInfoModal } from '../../../info_modal.js';

const KNOWLEDGE_INFO_FLAG = 'wb_knowledge_info_seen';
import { connectProgress, fetchPendingKind } from '../../../services/pipelineSocket.js';
import {
  PHASE_ORDER,
  LANDING_DYNAMIC_PHASES,
  showTimeline,
  stopEtaTicker,
  ensureLandingPhase,
  setPhaseState,
  setPhaseNarration,
  setTimelineTitle,
  resetTimelineRows,
} from './timeline.js';

const STATUS_NEUTRAL = "";
const STATUS_LOADING = "loading";
const STATUS_ERROR = "error";

// Short device-kind codes for the suggest chip — not i18n'd (compact mono
// codes, same in every locale). Mirrors the backend device_kind enum.
const DEVICE_KIND_SHORT = { gpu_card:"GPU", laptop_logic_board:"PORTABLE", phone_logic_board:"TÉLÉPHONE", desktop_motherboard:"BUREAU", sbc_board:"SBC", power_charging_board:"ALIM", other:"AUTRE" };

let isSubmitting = false;
let progressConn = null;
let _landingMascot = null;
// Set true while a build is parked on a device-kind disagreement
// (pipeline_paused / needs_kind_confirmation). The build coroutine returns
// deliberately and the WS closes — we must NOT treat that close as a failure.
let _landingPaused = false;
// Active slug/rid for the in-flight build. Stored at (re)subscribe time so
// confirmLandingKind can re-subscribe to the fresh build on the same slug.
let _activeSlug = null;
let _activeRid = null;

function setLandingMascot(state) {
  if (!_landingMascot) return;
  setMascotState(_landingMascot, state);
}

// Date formatter follows the active i18n locale (driven by profile.reply_language
// since commit 548ed20 dropped the topbar switch). Re-derived lazily so we
// pick up locale changes mid-session without a page reload.
function _landingDateFmt() {
  const locale = (i18n && i18n.locale) || 'en';
  // Map our short locale codes to BCP-47 region tags Intl expects.
  const bcp47 = locale === 'fr' ? 'fr-FR' : 'en-US';
  return new Intl.DateTimeFormat(bcp47, {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  });
}

async function loadAndRenderSidebar() {
  const sidebar = document.getElementById("landingSidebar");
  const list = document.getElementById("landingSidebarList");
  const count = document.getElementById("landingSidebarCount");
  if (!sidebar || !list) return;

  let repairs = [];
  try {
    const res = await fetch("/pipeline/repairs");
    if (res.ok) repairs = await res.json();
  } catch (err) {
    console.warn("[landing] loadRepairs failed", err);
  }
  if (!repairs || repairs.length === 0) {
    sidebar.hidden = true;
    return;
  }

  // Most recent first.
  repairs.sort((a, b) => {
    const ta = new Date(a.created_at).getTime() || 0;
    const tb = new Date(b.created_at).getTime() || 0;
    return tb - ta;
  });

  if (count) {
    const key = repairs.length > 1 ? "landing.sidebar.count_many" : "landing.sidebar.count_one";
    count.textContent = window.t ? window.t(key, { n: repairs.length }) : `${repairs.length} repairs`;
  }

  list.innerHTML = "";
  for (const r of repairs) {
    const li = document.createElement("li");
    li.className = "landing-sidebar-item";

    const a = document.createElement("a");
    a.className = "landing-sidebar-link";
    seedSlugForRepair(r.repair_id, r.device_slug);   // known slug — keep nav synchronous
    a.href = repairHash(r.repair_id, "diagnostic");

    const dev = document.createElement("span");
    dev.className = "landing-sidebar-device";
    dev.textContent = prettifySlug(r.device_slug);

    const sym = document.createElement("span");
    sym.className = "landing-sidebar-symptom";
    sym.textContent = r.symptom || "—";
    if (r.symptom) sym.title = r.symptom;

    const meta = document.createElement("span");
    meta.className = "landing-sidebar-meta";
    const dateStr = r.created_at
      ? _landingDateFmt().format(new Date(r.created_at)).replace(/,\s*/g, " ")
      : "";
    const ridShort = (r.repair_id || "").slice(0, 8);
    meta.textContent = dateStr ? `${dateStr} · ${ridShort}` : ridShort;

    a.appendChild(dev);
    a.appendChild(sym);
    a.appendChild(meta);
    li.appendChild(a);

    const del = document.createElement("button");
    del.type = "button";
    del.className = "landing-sidebar-delete";
    del.setAttribute("aria-label", window.t ? window.t("landing.sidebar.delete_aria") : "Delete this repair");
    del.title = window.t ? window.t("landing.sidebar.delete_title") : "Delete";
    del.textContent = "×";
    del.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      onDeleteRepairClick(r.repair_id, li, del);
    });
    li.appendChild(del);

    list.appendChild(li);
  }
  sidebar.hidden = false;
}

async function onDeleteRepairClick(repairId, itemEl, btnEl) {
  const t = window.t || ((k) => k);
  const ok = window.confirm(t("landing.delete.confirm"));
  if (!ok) return;

  btnEl.disabled = true;
  try {
    const res = await fetch(`/pipeline/repairs/${encodeURIComponent(repairId)}`, {
      method: "DELETE",
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status} ${detail}`);
    }
  } catch (err) {
    console.error("[landing] delete failed", err);
    setStatus(t("landing.status.error_delete", { error: err.message || err }), STATUS_ERROR);
    btnEl.disabled = false;
    return;
  }

  itemEl.remove();
  const list = document.getElementById("landingSidebarList");
  const count = document.getElementById("landingSidebarCount");
  const remaining = list ? list.children.length : 0;
  if (count) {
    if (remaining > 0) {
      const key = remaining > 1 ? "landing.sidebar.count_many" : "landing.sidebar.count_one";
      count.textContent = t(key, { n: remaining });
    } else {
      count.textContent = "";
    }
  }
  if (remaining === 0) {
    const sidebar = document.getElementById("landingSidebar");
    if (sidebar) sidebar.hidden = true;
  }
}

export function showLanding() {
  document.body.classList.add("show-landing");
  const ov = document.getElementById("landing-overlay");
  if (ov) ov.hidden = false;
  // Dim the hero synchronously on a likely first-run so the staged reveal
  // doesn't flash the full cockpit first (cheap flag check; un-gated below if
  // onboarding turns out not to run).
  preGateOnboarding();
  // Mount the hero mascot once; reopens reset to idle. Sidebar refetches
  // every reopen so a fresh leaveSession() shows the latest repair list.
  if (!_landingMascot) {
    _landingMascot = mountMascot(document.getElementById("landingMascot"), {
      size: "md", state: "idle",
    });
  } else {
    setLandingMascot("idle");
  }
  loadAndRenderSidebar();
  loadPacksForSuggest();
  setTimeout(() => document.getElementById("landingDevice")?.focus(), 50);

  // Profile pill (always present) + the one-time guided onboarding. Both read
  // the profile; onboarding additionally gates on the repair count and a
  // localStorage flag, and drives the hero mascot through its states.
  refreshProfileMenu();
  maybeStartOnboarding({ setMascotState: setLandingMascot });
}

export function hideLanding() {
  document.body.classList.remove("show-landing");
  const ov = document.getElementById("landing-overlay");
  if (ov) ov.hidden = true;
  if (progressConn) { progressConn.close(); progressConn = null; }
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

// Reset for a fresh run: clear the orchestration-pause state + device-kind
// panel (owned here), then delegate the phase-row DOM reset to timeline.js.
function resetTimeline() {
  _landingPaused = false;
  document.getElementById("landingKindPanel")?.remove();
  resetTimelineRows();
}

async function onSubmit(ev) {
  ev.preventDefault();
  if (isSubmitting) return;
  const t = window.t || ((k) => k);
  const deviceEl = document.getElementById("landingDevice");
  const symptomEl = document.getElementById("landingSymptom");
  const device = (deviceEl?.value || "").trim();
  const symptom = (symptomEl?.value || "").trim();

  if (device.length < 2) {
    setStatus(t("landing.status.validation_device"), STATUS_ERROR);
    deviceEl?.focus();
    return;
  }
  if (symptom.length < 5) {
    setStatus(t("landing.status.validation_symptom"), STATUS_ERROR);
    symptomEl?.focus();
    return;
  }

  setStatus(t("landing.status.checking"), STATUS_LOADING);
  setSubmitting(true);
  setLandingMascot("thinking");
  resetTimeline();

  try {
    // If the tech picked a known device from the autocomplete, send the
    // canonical slug so the backend skips re-slugification and lands on
    // the right pack — sidesteps near-but-not-identical spellings.
    //
    // Repair-create is a pure metadata call (urlencoded, NO file): the
    // schematic rides the dedicated /packs/{slug}/documents endpoint once the
    // slug is known (see uploadSchematicForSlug). That keeps the cloud
    // front-door able to gate creation without the schematic ever bypassing
    // its encrypted, tenant-scoped uploader-only store.
    const body = new URLSearchParams();
    body.append("device_label", device);
    body.append("symptom", symptom);
    if (_selectedDeviceSlug) body.append("device_slug", _selectedDeviceSlug);
    const kind = document.getElementById("landingDeviceKind")?.value || "";
    if (kind) body.append("device_kind", kind);
    // Signal the out-of-band schematic so the pipeline waits for its electrical
    // graph before device-kind classification (the upload fires below, post-create).
    if (_schematicFile) body.append("schematic_pending", "true");
    const res = await fetch("/pipeline/repairs", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status} ${detail}`);
    }
    const repair = await res.json();
    const rid = repair.repair_id;
    const slug = repair.device_slug;
    if (!rid || !slug) throw new Error(t("landing.status.error_invalid_response"));

    // Schematic intake — canonical path now that the slug exists. Best-effort:
    // a failed upload must not abort the diagnostic the tech just started (the
    // pack still builds from web research; the schematic is re-importable from
    // the Memory Bank dashboard later).
    if (_schematicFile) await uploadSchematicForSlug(slug, _schematicFile);

    // Three response shapes, three UX flows.
    // Branch 2 — symptom already covered by a known rule: no LLM work,
    // fast redirect to workspace.
    if (!repair.pipeline_started) {
      if (repair.matched_rule_id) {
        setStatus(
          t("landing.status.rule_match", { rule_id: repair.matched_rule_id }),
          STATUS_NEUTRAL,
        );
      } else {
        setStatus(
          t("landing.status.device_known", { device: repair.device_label }),
          STATUS_NEUTRAL,
        );
      }
      // Pack on disk → play an accelerated fake-timeline (~15–17s) so the
      // tech sees the cache hit as a fast pipeline run, then navigate.
      // setStatus message above stays as the lead-in; setTimelineTitle
      // takes over once showTimeline() inside the helper fires.
      playCachedPipelineTimeline(slug, rid, repair.device_label || slug)
        .catch((err) => {
          console.warn("[landing] cached timeline failed, falling back to direct nav", err);
          goToWorkspace(rid, slug);
        });
      return;
    }

    // Branch 3 — pack exists but the symptom is new: the backend kicked
    // a real targeted expand in background. We play the same fake-timeline
    // as branch 2 (pack is on disk, agent works from existing rules even
    // if the expand hasn't finished). The expand runs silently — harmless.
    if (repair.pipeline_kind === "expand") {
      setStatus(
        t("landing.status.device_known", { device: repair.device_label }),
        STATUS_NEUTRAL,
      );
      playCachedPipelineTimeline(slug, rid, repair.device_label || slug)
        .catch((err) => {
          console.warn("[landing] cached timeline (expand) failed, falling back", err);
          goToWorkspace(rid, slug, "diagnostic");
        });
      return;
    }

    // Branch 1 — full pipeline on a fresh device (~5-10 min).
    setStatus(t("landing.status.build_new"), STATUS_NEUTRAL);
    showTimeline();
    setTimelineTitle(t("landing.timeline.title_build", { device: repair.device_label }));
    subscribeToProgress(slug, rid);
  } catch (err) {
    console.error("[landing] submit failed", err);
    setStatus(t("landing.status.error_create", { error: err.message || err }), STATUS_ERROR);
    setLandingMascot("error");
    setSubmitting(false);
  }
}

function subscribeToProgress(slug, repairId) {
  if (progressConn) { progressConn.close(); progressConn = null; }
  // Remember the active build so confirmLandingKind() can re-subscribe to the
  // fresh build on the same slug after the tech resolves a kind disagreement.
  _activeSlug = slug;
  _activeRid = repairId;

  progressConn = connectProgress(slug, {
    onEvent: (data) => handleProgressEvent(data, slug, repairId),
    onError: (ev) => {
      console.warn("[landing] progress WS error", ev);
      setStatus((window.t || ((k) => k))("landing.status.ws_lost"), STATUS_ERROR);
    },
    onClose: () => {
      stopEtaTicker();
      // _landingPaused → the build coroutine returned deliberately on a
      // device-kind disagreement; the panel is up and the tech will confirm.
      // Don't surface a "connection lost" failure in that case.
    },
  });

  // Reload restore — if a prior build is parked on a kind disagreement (e.g.
  // the tech refreshed the page), re-render the confirmation panel. A missing /
  // non-pending state is silent: the common case is "no pending disagreement".
  fetchPendingKind(slug).then((p) => {
    if (p) {
      handleProgressEvent({
        type: "pipeline_paused",
        reason: "needs_kind_confirmation",
        device_slug: slug,
        user_declared: p.user_declared,
        graph_inferred: p.graph_inferred,
        confidence: p.confidence,
        evidence: p.evidence,
      }, slug, repairId);
    }
  });
}

function handleProgressEvent(ev, slug, repairId) {
  const t = window.t || ((k) => k);
  switch (ev.type) {
    case "subscribed":
      break;
    case "pipeline_started":
      setStatus(t("landing.status.pipeline_started", { device: ev.device_label || ev.device_slug || slug }), STATUS_LOADING);
      break;
    case "phase_started": {
      const phase = ev.phase;
      ensureLandingPhase(phase);
      if (PHASE_ORDER.includes(phase) || phase === "expand" || phase in LANDING_DYNAMIC_PHASES) {
        setPhaseState(phase, "running");
        setLandingMascot("working");
      }
      break;
    }
    case "phase_finished": {
      const phase = ev.phase;
      if (PHASE_ORDER.includes(phase) || phase === "expand" || phase in LANDING_DYNAMIC_PHASES) {
        setPhaseState(phase, "done");
      }
      break;
    }
    case "phase_narration": {
      const phase = ev.phase;
      const text = (ev.text || "").trim();
      if (text && PHASE_ORDER.includes(phase)) setPhaseNarration(phase, text);
      break;
    }
    case "pipeline_finished": {
      setTimelineTitle(t("landing.timeline.title_ready", { status: ev.status || "" }));
      setStatus(t("landing.status.ready"), STATUS_NEUTRAL);
      stopEtaTicker();
      setLandingMascot("success");
      // 2500 ms grace gives the audit phase narration (Haiku ~800-1600 ms)
      // time to land on the WS bus and render before we navigate away.
      setTimeout(() => goToWorkspace(repairId, slug), 2500);
      break;
    }
    case "pipeline_paused":
      if (ev.reason === "needs_kind_confirmation") {
        _landingPaused = true;
        ensureLandingPhase("device_kind");
        setPhaseState("device_kind", "running");
        renderLandingKindConfirm(ev);
      }
      break;
    case "pipeline_failed": {
      setTimelineTitle(t("landing.timeline.title_failed"));
      setStatus(t("landing.status.error_pipeline", { error: ev.error || ev.status || t("landing.status.error_unknown") }), STATUS_ERROR);
      const running = document.querySelector(".landing-phase.is-running");
      if (running) {
        running.classList.remove("is-running");
        running.classList.add("is-failed");
      }
      stopEtaTicker();
      setLandingMascot("error");
      setSubmitting(false);
      break;
    }
    default:
      break;
  }
}

// ============================================================
// Device-kind pause panel — the orchestrator emits `pipeline_paused`
// (reason: needs_kind_confirmation) when the graph-inferred device kind
// disagrees with what the technician declared. We inject an inline
// confirmation panel into the timeline; confirming POSTs the chosen kind
// and starts a fresh build, which we re-subscribe to. Mirrors the drawer's
// renderKindConfirm / confirmKind (web/js/pipeline_progress.js).
// ============================================================

// Resolve a human label for a device_kind code. Empty / "unknown" /
// undeclared → the shared "non déclaré" string; otherwise the
// repair.device_kind.options.<k> label (resolves on the landing because
// i18n loads all modules at boot), falling back to the raw code on a miss.
function _landingKindLabel(k) {
  const tFn = window.t || ((key) => key);
  if (!k || k === "unknown") return tFn("pipeline.kind.undeclared");
  const key = "repair.device_kind.options." + k;
  const label = tFn(key);
  return label === key ? k : label;
}

function renderLandingKindConfirm(ev) {
  // Idempotent — drop any panel from a prior pause/restore.
  document.getElementById("landingKindPanel")?.remove();
  const timeline = document.getElementById("landingTimeline");
  if (!timeline) return;
  const tFn = window.t || ((k) => k);

  const conf = typeof ev.confidence === "number" ? Math.round(ev.confidence * 100) : null;
  const candidates = [];
  if (ev.graph_inferred) candidates.push({ k: ev.graph_inferred, recommended: true });
  if (ev.user_declared && ev.user_declared !== ev.graph_inferred) candidates.push({ k: ev.user_declared });
  // Neither inferred nor declared → a single "unknown" radio so the panel
  // still offers an actionable confirm (posts "unknown", pipeline proceeds).
  if (candidates.length === 0) candidates.push({ k: "unknown", recommended: true });

  const radios = candidates.map((c, i) => `
    <label class="landing-kind-opt">
      <input type="radio" name="landingKind" value="${_escapeHtml(c.k)}" ${i === 0 ? "checked" : ""}>
      <span>${_escapeHtml(_landingKindLabel(c.k))}${c.recommended ? ` <em>${_escapeHtml(tFn("pipeline.kind.recommended"))}</em>` : ""}</span>
    </label>`).join("");

  const panel = document.createElement("div");
  panel.className = "landing-kind-panel";
  panel.id = "landingKindPanel";
  panel.innerHTML = `
    <div class="landing-kind-row"><span data-i18n="pipeline.kind.declared">${_escapeHtml(tFn("pipeline.kind.declared"))}</span><b class="mono">${_escapeHtml(_landingKindLabel(ev.user_declared))}</b></div>
    <div class="landing-kind-row"><span data-i18n="pipeline.kind.detected">${_escapeHtml(tFn("pipeline.kind.detected"))}</span><b class="mono">${_escapeHtml(_landingKindLabel(ev.graph_inferred))}${conf !== null ? ` ${conf}%` : ""}</b></div>
    ${ev.evidence ? `<div class="landing-kind-evidence">${_escapeHtml(ev.evidence)}</div>` : ""}
    <div class="landing-kind-opts">${radios}</div>
    <button type="button" class="landing-kind-confirm" id="landingKindConfirm" data-i18n="pipeline.kind.confirm">${_escapeHtml(tFn("pipeline.kind.confirm"))}</button>`;

  // Append after #landingPhaseList so the panel sits at the foot of the
  // timeline, below the phase rows.
  timeline.appendChild(panel);
  if (window.i18n && window.i18n.applyDom) window.i18n.applyDom(panel);

  document.getElementById("landingKindConfirm").addEventListener("click", () => {
    const chosen = panel.querySelector('input[name="landingKind"]:checked')?.value;
    if (chosen) confirmLandingKind(chosen);
  });
}

async function confirmLandingKind(deviceKind) {
  const t = window.t || ((k) => k);
  let ok = false;
  try {
    const res = await fetch(`/pipeline/packs/${encodeURIComponent(_activeSlug)}/confirm-kind`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_kind: deviceKind }),
    });
    ok = res.ok;
  } catch (_) { ok = false; }
  if (!ok) {
    // A 4xx/5xx or network failure means the pipeline did NOT resume. Surface
    // the error and stop — do NOT re-subscribe into a confusing dead WS.
    setStatus(t("landing.status.ws_lost"), STATUS_ERROR);
    setLandingMascot("error");
    return;
  }
  document.getElementById("landingKindPanel")?.remove();
  setPhaseState("device_kind", "done");
  _landingPaused = false;
  // Fresh build started by confirm-kind — close the old WS and re-subscribe
  // to watch the re-run on the same slug.
  if (progressConn) { progressConn.close(); progressConn = null; }
  subscribeToProgress(_activeSlug, _activeRid);
}


function _sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// Plays a fake 5-phase pipeline timeline at ~3s per phase, then the
// mascot success state, then navigates to the workspace. Used when the
// backend signals `pipeline_started: false` (pack already on disk) so
// the technician sees the cache hit as a fast pipeline run instead of
// an instant flash. ~15s total + 1.5s success grace = ~16–17s.
async function playCachedPipelineTimeline(slug, repairId, deviceLabel) {
  const t = window.t || ((k) => k);
  showTimeline();
  setTimelineTitle(t("landing.timeline.title_loading", { device: deviceLabel }));
  setLandingMascot("working");

  // PHASE_ORDER includes "mapper" which the live pipeline marks hidden
  // until a phase event arrives. For a cache hit we want to show all
  // phases marching past, so unhide it first.
  const mapperRow = document.querySelector('.landing-phase[data-phase="mapper"]');
  if (mapperRow) mapperRow.hidden = false;

  const PER_PHASE_MS = 3000;
  for (const phase of PHASE_ORDER) {
    setPhaseState(phase, "running");
    await _sleep(PER_PHASE_MS * 0.7);
    setPhaseState(phase, "done");
    await _sleep(PER_PHASE_MS * 0.3);
  }

  setLandingMascot("success");
  setTimelineTitle(t("landing.timeline.title_ready", { status: deviceLabel }));
  await _sleep(1500);
  // Cache hit: land on the repair dashboard (#home) so the tech sees the
  // findings + timeline straight away, not the graph view that the live
  // pipeline path defaults to.
  goToWorkspace(repairId, slug, "diagnostic");
}

function goToWorkspace(repairId, slug, vue = "graph") {
  // Land the tech on the requested repair vue — default the graph view (loads
  // graph + memory bank + opens the chat via openLLMPanelIfRepairParam) rather
  // than the diagnostic dashboard. The dashboard is the "diagnostic" vue,
  // reachable via the left rail. repairHash coerces an unknown vue to diagnostic.
  //
  // Strip the landing overlay first so a hash navigation doesn't leave the
  // overlay sitting on top of the freshly-loaded view.
  hideLanding();
  // Close any active progress WS so it can't fire late events (e.g. a
  // duplicate pipeline_finished) onto the page after navigation.
  if (progressConn) { progressConn.close(); progressConn = null; }

  seedSlugForRepair(repairId, slug);   // known slug — keep the deep nav synchronous
  const target = new URL(location.origin + location.pathname);
  target.hash = repairHash(repairId, vue);

  // Force a real navigation. location.href to the same URL is a no-op and a
  // hash-only delta does not reload — either case would leave the landing
  // module's state inconsistent with the post-pipeline view. location.assign +
  // reload on duplicate guarantees a clean bootstrap of main.js.
  if (target.toString() === location.href) {
    location.reload();
  } else {
    location.assign(target.toString());
  }
}

function onChipClick(ev) {
  const btn = ev.target.closest(".landing-chip");
  if (!btn) return;
  const dev = document.getElementById("landingDevice");
  const sym = document.getElementById("landingSymptom");
  // Chips don't carry a canonical slug; clearing here prevents a stale
  // _selectedDeviceSlug from the autocomplete leaking onto a chip submit.
  // Same for any graph-backed schematic state from a prior pick.
  _selectedDeviceSlug = null;
  resetSchematicField();
  if (dev && btn.dataset.device) dev.value = btn.dataset.device;
  if (sym) {
    // Prefer the i18n key if present so the chip's symptom matches the active
    // locale; fall back to the literal data-symptom attribute.
    const key = btn.dataset.symptomKey;
    const fallback = btn.dataset.symptom || "";
    if (key && window.t) sym.value = window.t(key);
    else if (fallback) sym.value = fallback;
  }
  sym?.focus();
}

// ============================================================
// Device autocomplete — surfaces devices already known under the device
// input as the technician types. Sourced from /pipeline/taxonomy so the
// list is deduplicated to ONE entry per (brand, model) — no
// "iPhone X" / "iPhone X logic board" / "iPhone X bench" noise.
// Cached for the session in `_devicesCache`. Keyboard nav: ↑/↓/Enter/Esc.
//
// At selection, we store the canonical slug of the chosen pack on the
// form so onSubmit can pass `device_slug` to the backend explicitly,
// guaranteeing a cache hit on the right pack rather than re-slugifying
// the label and risking a miss on a near-but-not-identical spelling.
// ============================================================

let _devicesCache = null;
let _suggestActiveIdx = -1;
let _selectedDeviceSlug = null;
let _schematicFile = null;

// Flatten a TaxonomyTree into a plain list with one entry per
// (brand, model) — picks the most-complete pack as the canonical
// representative. Uncategorized packs become individual entries.
function _flattenTaxonomy(tree) {
  const out = [];
  const brands = (tree && tree.brands) || {};
  for (const [brand, models] of Object.entries(brands)) {
    for (const [model, packs] of Object.entries(models || {})) {
      if (!Array.isArray(packs) || packs.length === 0) continue;
      // Prefer a complete pack; fall back to the first one.
      const canonical = packs.find((p) => p && p.complete) || packs[0];
      out.push({
        label: model,
        subtitle: brand,
        slug: canonical.device_slug,
        device_label: canonical.device_label || model,
        complete: Boolean(canonical.complete),
        has_electrical_graph: Boolean(canonical.has_electrical_graph),
        device_kind: canonical.device_kind || null,
      });
    }
  }
  for (const p of (tree && tree.uncategorized) || []) {
    if (!p || !p.device_slug) continue;
    out.push({
      label: p.device_label || prettifySlug(p.device_slug),
      subtitle: null,
      slug: p.device_slug,
      device_label: p.device_label || prettifySlug(p.device_slug),
      complete: Boolean(p.complete),
      has_electrical_graph: Boolean(p.has_electrical_graph),
      device_kind: p.device_kind || null,
    });
  }
  // Sort: complete first, then alphabetical by label.
  out.sort((a, b) => {
    if (a.complete !== b.complete) return a.complete ? -1 : 1;
    return a.label.localeCompare(b.label);
  });
  return out;
}

async function loadPacksForSuggest() {
  try {
    const res = await fetch("/pipeline/taxonomy");
    if (res.ok) {
      const tree = await res.json();
      _devicesCache = _flattenTaxonomy(tree);
    } else {
      _devicesCache = [];
    }
  } catch (err) {
    console.warn("[landing] loadPacksForSuggest failed", err);
    _devicesCache = [];
  }
}

function _matchDevices(query) {
  if (!_devicesCache || _devicesCache.length === 0) return [];
  const q = (query || "").trim().toLowerCase();
  if (q.length < 1) return [];
  return _devicesCache
    .filter((d) => {
      const label = (d.label || "").toLowerCase();
      const sub = (d.subtitle || "").toLowerCase();
      const slug = (d.slug || "").toLowerCase();
      return label.includes(q) || sub.includes(q) || slug.includes(q);
    })
    .slice(0, 6);
}

function _renderSuggest(query) {
  const box = document.getElementById("landingSuggest");
  if (!box) return;
  const matches = _matchDevices(query);
  if (matches.length === 0) {
    box.hidden = true;
    box.innerHTML = "";
    _suggestActiveIdx = -1;
    return;
  }
  const tFn = window.t || ((k) => k);
  const draftLabel = tFn("landing.suggest.draft");
  box.innerHTML = matches.map((d, i) => {
    const safeLabel = _escapeHtml(d.label);
    const safeSub = d.subtitle ? _escapeHtml(d.subtitle) : "";
    const safeSlug = _escapeHtml(d.slug);
    const iconClass = d.complete ? "is-complete" : "is-partial";
    const iconText = d.complete ? "✓" : "•";
    const meta = d.complete ? safeSub : (safeSub ? `${safeSub} · ${draftLabel}` : draftLabel);
    // Readiness badges next to the complete (✓/•) "mémoire" marker:
    //   graph badge — lit (.is-on) when the device has a compiled electrical graph;
    //   kind chip — mono short code (GPU / PORTABLE / …) when device_kind is known.
    const graphBadge = `<span class="landing-suggest-badge${d.has_electrical_graph ? " is-on" : ""}" title="${_escapeHtml(tFn("landing.suggest.graph_title"))}">${_escapeHtml(tFn("landing.suggest.graph_label"))}</span>`;
    const kindBadge = (d.device_kind && d.device_kind !== "unknown")
      ? `<span class="landing-suggest-badge mono">${_escapeHtml(DEVICE_KIND_SHORT[d.device_kind] || d.device_kind)}</span>`
      : "";
    // data-label = the short model name (e.g. "iPhone 12") that lands in
    // the input on selection. NOT d.device_label, which is the raw
    // registry label (e.g. "Apple iPhone 12 logic board") and would
    // pollute the input with brand + form-factor noise.
    return `<div class="landing-suggest-item" role="option" `
      + `data-slug="${safeSlug}" data-label="${safeLabel}" data-index="${i}" `
      + `data-graph="${d.has_electrical_graph ? "1" : ""}">`
      + `<span class="landing-suggest-icon ${iconClass}" aria-hidden="true">${iconText}</span>`
      + `<span class="landing-suggest-label">${safeLabel}</span>`
      + `<span class="landing-suggest-meta">${meta}${graphBadge}${kindBadge}</span>`
      + `</div>`;
  }).join("");
  box.hidden = false;
  _suggestActiveIdx = -1;
}

function _setSuggestActive(idx) {
  const items = document.querySelectorAll(".landing-suggest-item");
  if (items.length === 0) return;
  const clamped = Math.max(0, Math.min(idx, items.length - 1));
  items.forEach((el, i) => el.classList.toggle("is-active", i === clamped));
  _suggestActiveIdx = clamped;
  items[clamped].scrollIntoView({ block: "nearest" });
}

function _selectSuggest(label, slug, hasGraph) {
  const dev = document.getElementById("landingDevice");
  const sym = document.getElementById("landingSymptom");
  if (dev) dev.value = label;
  // Pin the canonical slug so onSubmit sends device_slug to the backend
  // (skips re-slugification of the label and guarantees the cache hit
  // on the right pack — defends against near-but-not-identical spellings).
  _selectedDeviceSlug = slug || null;
  // When the picked device already has a compiled electrical graph, the
  // schematic is on disk — no need to attach a PDF. Otherwise restore the
  // default "attach" affordance.
  const field = document.getElementById("landingSchematicField");
  const pick = document.getElementById("landingSchematicPick");
  const name = document.getElementById("landingSchematicName");
  if (field) {
    if (hasGraph) {
      field.classList.add("is-ingested");
      if (pick) pick.disabled = true;
      if (name) name.textContent = (window.t || ((k) => k))("landing.schematic.already_ingested");
      _schematicFile = null;
      const fi = document.getElementById("landingSchematic"); if (fi) fi.value = "";
    } else {
      field.classList.remove("is-ingested");
      if (pick) pick.disabled = false;
      if (name) name.textContent = "";
    }
  }
  _hideSuggest();
  renderKnowledgeIndicators();
  if (sym) sym.focus();
}

function _hideSuggest() {
  const box = document.getElementById("landingSuggest");
  if (box) {
    box.hidden = true;
    box.innerHTML = "";
  }
  _suggestActiveIdx = -1;
}

function _initSuggest() {
  const dev = document.getElementById("landingDevice");
  const box = document.getElementById("landingSuggest");
  if (!dev || !box) return;

  dev.addEventListener("input", () => {
    // Free-text editing invalidates the previously-selected slug — the
    // tech may now be heading toward a different (or unknown) device.
    // Restore the schematic-attach affordance too: a graph-backed pick is
    // no longer in force once the label diverges.
    _selectedDeviceSlug = null;
    resetSchematicField();
    _renderSuggest(dev.value);
  });

  dev.addEventListener("focus", () => {
    if (dev.value && dev.value.length >= 1) _renderSuggest(dev.value);
  });

  dev.addEventListener("keydown", (ev) => {
    const items = document.querySelectorAll(".landing-suggest-item");
    if (items.length === 0) return;
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      _setSuggestActive(_suggestActiveIdx < 0 ? 0 : _suggestActiveIdx + 1);
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      _setSuggestActive(_suggestActiveIdx <= 0 ? items.length - 1 : _suggestActiveIdx - 1);
    } else if (ev.key === "Enter" && _suggestActiveIdx >= 0) {
      // Only intercept Enter when the user has explicitly highlighted a
      // suggestion via arrows. Otherwise let the form submit naturally.
      ev.preventDefault();
      const item = items[_suggestActiveIdx];
      if (item) _selectSuggest(item.dataset.label, item.dataset.slug, !!item.dataset.graph);
    } else if (ev.key === "Escape") {
      _hideSuggest();
    }
  });

  // Hide on blur, but with a small delay so a click on a suggestion
  // (which fires after blur) gets processed first.
  dev.addEventListener("blur", () => setTimeout(_hideSuggest, 150));

  box.addEventListener("mousedown", (ev) => {
    // Use mousedown (not click) so it fires before blur on the input.
    const item = ev.target.closest(".landing-suggest-item");
    if (item && item.dataset.label) {
      ev.preventDefault();
      _selectSuggest(item.dataset.label, item.dataset.slug, !!item.dataset.graph);
    }
  });
}

// Restore the schematic-upload affordance to its default "attach" state
// (no PDF attached, CTA re-enabled, not flagged ingested). Called when a
// graph-backed device pick is invalidated by free-text editing — the kind
// select is left untouched since it's an independent manual choice.
function resetSchematicField() {
  _schematicFile = null;
  const field = document.getElementById("landingSchematicField");
  if (field) field.classList.remove("is-ingested");
  const pick = document.getElementById("landingSchematicPick");
  if (pick) pick.disabled = false;
  const name = document.getElementById("landingSchematicName");
  if (name) name.textContent = "";
  const fi = document.getElementById("landingSchematic");
  if (fi) fi.value = "";
  renderKnowledgeIndicators();
}

// ─── Knowledge modal (optional device context: board type + schematic) ───
// The board-type <select> and schematic picker live inside this modal so the
// landing hero stays a clean device+symptom form. The modal is pure
// presentation — submit reads the live <select> value and `_schematicFile`
// exactly as before. The hero reflects what's been added via a count badge on
// the trigger button plus removable summary chips.
let _knowledgeLastFocus = null;

function openKnowledgeModal() {
  const backdrop = document.getElementById("landingKnowledgeBackdrop");
  if (!backdrop) return;
  _knowledgeLastFocus = document.activeElement;
  backdrop.classList.add("open");
  backdrop.setAttribute("aria-hidden", "false");
  requestAnimationFrame(() => document.getElementById("landingDeviceKind")?.focus());
}

function closeKnowledgeModal() {
  const backdrop = document.getElementById("landingKnowledgeBackdrop");
  if (!backdrop || !backdrop.classList.contains("open")) return;
  backdrop.classList.remove("open");
  backdrop.setAttribute("aria-hidden", "true");
  if (_knowledgeLastFocus && typeof _knowledgeLastFocus.focus === "function") {
    _knowledgeLastFocus.focus();
  }
}

// Re-render the hero's knowledge indicators (count badge + summary chips) from
// the current control state. Single source of truth: the live <select> value
// and `_schematicFile` — no duplicated mirror state.
function renderKnowledgeIndicators() {
  const select = document.getElementById("landingDeviceKind");
  const kind = select?.value || "";
  const kindLabel = kind ? (select.options[select.selectedIndex]?.textContent || kind) : "";
  const items = [];
  if (kind) items.push({ type: "kind", label: kindLabel });
  if (_schematicFile) items.push({ type: "schematic", label: _schematicFile.name });

  const badge = document.getElementById("landingKnowledgeBadge");
  if (badge) {
    badge.textContent = String(items.length);
    badge.hidden = items.length === 0;
  }
  const btn = document.getElementById("landingKnowledgeBtn");
  if (btn) btn.classList.toggle("has-knowledge", items.length > 0);

  const chips = document.getElementById("landingKnowledgeChips");
  if (chips) {
    chips.innerHTML = items.map((it) =>
      `<button type="button" class="landing-knowledge-chip" data-knowledge="${it.type}">`
      + `<span>${_escapeHtml(it.label)}</span>`
      + `<svg viewBox="0 0 24 24" width="11" height="11" stroke="currentColor" stroke-width="2" `
      + `fill="none" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>`
      + `</button>`
    ).join("");
    chips.hidden = items.length === 0;
  }
}

function onKnowledgeChipClick(ev) {
  const chip = ev.target.closest(".landing-knowledge-chip");
  if (!chip) return;
  if (chip.dataset.knowledge === "kind") {
    const select = document.getElementById("landingDeviceKind");
    if (select) select.value = "";
  } else if (chip.dataset.knowledge === "schematic") {
    resetSchematicField();
  }
  renderKnowledgeIndicators();
}

// Upload an attached schematic PDF to the device's pack via the dedicated
// document endpoint (kind=schematic_pdf) — the canonical ingestion path. The
// cloud front-door routes this through its encrypted, tenant-scoped
// uploader-only store, so the schematic never bypasses tenant isolation (which
// attaching it to repair-create would). Best-effort: logged on failure, never
// throws into the submit flow.
async function uploadSchematicForSlug(slug, file) {
  try {
    const fd = new FormData();
    fd.append("kind", "schematic_pdf");
    fd.append("file", file);
    const res = await fetch(`/pipeline/packs/${encodeURIComponent(slug)}/documents`, {
      method: "POST",
      body: fd,
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      console.warn(`[landing] schematic upload failed: HTTP ${res.status} ${detail}`);
    }
  } catch (err) {
    console.warn("[landing] schematic upload error", err);
  }
}

export function initLanding() {
  const form = document.getElementById("landingForm");
  if (form) form.addEventListener("submit", onSubmit);
  const chips = document.getElementById("landingChips");
  if (chips) chips.addEventListener("click", onChipClick);
  document.getElementById("landingSchematicPick")?.addEventListener("click", () => {
    document.getElementById("landingSchematic")?.click();
  });
  document.getElementById("landingSchematic")?.addEventListener("change", (e) => {
    _schematicFile = e.target.files?.[0] || null;
    const n = document.getElementById("landingSchematicName");
    if (n) n.textContent = _schematicFile ? _schematicFile.name : "";
    e.target.value = "";
    renderKnowledgeIndicators();
  });
  // Knowledge modal: trigger, close affordances, board-type change, chip removal.
  // First click explains what "Add knowledge" is for, then opens the modal;
  // afterwards it goes straight in. A persistent "?" reopens the explainer.
  document.getElementById("landingKnowledgeBtn")?.addEventListener("click", () => {
    let seen = true;
    try { seen = !!localStorage.getItem(KNOWLEDGE_INFO_FLAG); } catch { /* private mode */ }
    if (!seen) {
      try { localStorage.setItem(KNOWLEDGE_INFO_FLAG, "1"); } catch { /* ignore */ }
      openInfoModal("knowledge", { onClose: openKnowledgeModal });
    } else {
      openKnowledgeModal();
    }
  });
  document.getElementById("landingKnowledgeInfo")?.addEventListener("click", () => openInfoModal("knowledge"));
  document.getElementById("landingKnowledgeClose")?.addEventListener("click", closeKnowledgeModal);
  document.getElementById("landingKnowledgeDone")?.addEventListener("click", closeKnowledgeModal);
  const kBackdrop = document.getElementById("landingKnowledgeBackdrop");
  if (kBackdrop) {
    kBackdrop.addEventListener("click", (ev) => {
      if (ev.target === kBackdrop) closeKnowledgeModal();
    });
  }
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closeKnowledgeModal();
  });
  document.getElementById("landingDeviceKind")?.addEventListener("change", renderKnowledgeIndicators);
  const kChips = document.getElementById("landingKnowledgeChips");
  if (kChips) kChips.addEventListener("click", onKnowledgeChipClick);
  _initSuggest();
  renderKnowledgeIndicators();
  initProfileMenu();
}
