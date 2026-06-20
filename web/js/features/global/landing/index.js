// Landing hero — captures {device_label, symptom}, kicks the existing
// /pipeline/repairs endpoint, and renders a live narrated timeline of the
// pipeline phases as the agent learns the device. When the pipeline finishes
// (or the pack was already on disk) the page redirects into the workspace
// at ?repair={id}&device={slug}.
//
// No classifier here — the existing pipeline (Scout → Registry → Mapper? →
// Writers ×3 → Auditor) does device identification + knowledge construction
// in one shot. The orchestrator emits live `phase_step` events from inside
// each phase (Scout rounds, schematic pages, each writer completing, audit
// rounds); we render those into each timeline row's live line + a collapsible
// "détail" log so the technician watches the agent work in real time.

import { mountMascot, setMascotState } from '../../../mascot.js';
import { prettifySlug, repairHash, seedSlugForRepair } from '../../../router.js';
import i18n from '../../../i18n.js';
import { escapeHtml as _escapeHtml } from '../../../shared/dom.js';
import { initProfileMenu, refreshProfileMenu } from './profile_menu.js';
import { initCatalogue, closeCatalogue } from './catalogue.js';
import { maybeStartOnboarding, preGateOnboarding } from './onboarding.js';
import { openInfoModal } from '../../../info_modal.js';
import { packedOnly, hideUploads, planHints } from '../../../cloud_hints.js';

const KNOWLEDGE_INFO_FLAG = 'wb_knowledge_info_seen';
import { connectProgress, fetchPendingKind } from '../../../services/pipelineSocket.js';
import { loadDevices } from '../../../services/deviceCatalog.js';
import {
  PHASE_ORDER,
  LANDING_DYNAMIC_PHASES,
  showTimeline,
  stopEtaTicker,
  ensureLandingPhase,
  setPhaseState,
  setPhaseStep,
  setTimelineTitle,
  resetTimelineRows,
} from './timeline.js';

const STATUS_NEUTRAL = "";
const STATUS_LOADING = "loading";
const STATUS_ERROR = "error";

// Short device-kind codes for the suggest chip — not i18n'd (compact mono
// codes, same in every locale). Mirrors the backend device_kind enum.
export const DEVICE_KIND_SHORT = { gpu_card:"GPU", laptop_logic_board:"PORTABLE", phone_logic_board:"TÉLÉPHONE", desktop_motherboard:"BUREAU", sbc_board:"SBC", power_charging_board:"ALIM", other:"AUTRE" };

let isSubmitting = false;
let progressConn = null;
let _landingMascot = null;
// Set by the pre-flight modal's email checkbox (cloud only); read after the
// repair is created to arm the "email me when ready" opt-in. Reset per launch.
let _preflightNotifyOptIn = false;
// Whether the CURRENT progress subscription navigates into the workspace when
// the build finishes. True for a fresh submit and an explicit tile-resume click;
// false for a passive resume-on-load (we don't want to yank a browsing tech).
let _autoNavOnFinish = true;
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

// Mobile drawer (≤999px): the recent-repairs sidebar slides in from the left,
// opened by #landingRepairsToggle. On desktop the sidebar is a persistent
// column and these are inert (the toggle/backdrop are display:none via CSS).
function openRepairsDrawer() {
  document.getElementById("landing-overlay")?.classList.add("sidebar-open");
  const bd = document.getElementById("landingSidebarBackdrop");
  if (bd) bd.hidden = false;
  document.getElementById("landingRepairsToggle")?.setAttribute("aria-expanded", "true");
}
function closeRepairsDrawer() {
  document.getElementById("landing-overlay")?.classList.remove("sidebar-open");
  const bd = document.getElementById("landingSidebarBackdrop");
  if (bd) bd.hidden = true;
  document.getElementById("landingRepairsToggle")?.setAttribute("aria-expanded", "false");
}

async function loadAndRenderSidebar() {
  const sidebar = document.getElementById("landingSidebar");
  const list = document.getElementById("landingSidebarList");
  const count = document.getElementById("landingSidebarCount");
  const toggle = document.getElementById("landingRepairsToggle");
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
    if (toggle) toggle.hidden = true;   // nothing to open → hide the mobile trigger
    closeRepairsDrawer();
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

  const tFn = window.t || ((k) => k);
  list.innerHTML = "";
  for (const r of repairs) {
    const li = document.createElement("li");
    li.className = "landing-sidebar-item";

    // Pack build state of this repair's device (from _build_state.json via the
    // listing). Only the non-ready states get a tile badge — 'complete'/null is
    // the normal case and stays unbadged.
    const bs = r.build_state;
    const badged = bs === "building" || bs === "failed" || bs === "paused";
    if (badged) li.classList.add(`is-${bs}`);

    const a = document.createElement("a");
    a.className = "landing-sidebar-link";
    seedSlugForRepair(r.repair_id, r.device_slug);   // known slug — keep nav synchronous
    a.href = repairHash(r.repair_id, "diagnostic");
    if (bs === "building") {
      // A building tile routes to the LIVE timeline (resume), not the workspace
      // whose pack isn't ready yet. autoNav: an explicit click means "I want to
      // watch this", so navigate into the device when it finishes.
      a.title = tFn("landing.sidebar.locked_hint");
      a.setAttribute("aria-label", tFn("landing.sidebar.resume_aria"));
      a.addEventListener("click", (ev) => {
        ev.preventDefault();
        closeRepairsDrawer();
        resumeBuild(r.device_slug, r.repair_id, { autoNav: true });
      });
    } else {
      a.addEventListener("click", closeRepairsDrawer);  // close the mobile drawer on navigation
      if (bs === "failed" || bs === "paused") a.title = tFn(`landing.sidebar.state_${bs}`);
    }

    const dev = document.createElement("span");
    dev.className = "landing-sidebar-device";
    const devName = document.createElement("span");
    devName.className = "landing-sidebar-device-name";
    devName.textContent = prettifySlug(r.device_slug);
    dev.appendChild(devName);
    if (badged) {
      const badge = document.createElement("span");
      badge.className = `landing-sidebar-state mono is-${bs}`;
      badge.textContent = tFn(`landing.sidebar.state_${bs}`);
      dev.appendChild(badge);
    }

    const sym = document.createElement("span");
    sym.className = "landing-sidebar-symptom";
    sym.textContent = r.symptom || "…";
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
  if (toggle) toggle.hidden = false;   // repairs exist → expose the mobile trigger

  // A build was in flight when the page (re)loaded → resume its live timeline so
  // a refresh mid-build doesn't lose the progress view. Passive (autoNav off) so
  // a finish doesn't yank a tech who reopened the landing to start another job.
  maybeResumeActiveBuild(repairs);
}

// Build a human-readable Error from a failed Response. Prefers the structured
// error message when the backend sent one (the cloud front-door's
// {error:{message}} gates, FastAPI's {detail:...}) — a raw JSON body in a
// status line reads as a bug to the technician. Non-JSON bodies keep the raw
// `HTTP <status> <body>` line (still the most useful thing to show).
async function httpError(res) {
  const detail = await res.text().catch(() => "");
  let msg = `HTTP ${res.status} ${detail}`;
  try {
    const parsed = JSON.parse(detail);
    const m = parsed?.error?.message
      || parsed?.detail?.message
      || (typeof parsed?.detail === "string" ? parsed.detail : null);
    if (m) msg = m;
  } catch { /* not JSON — keep the raw line */ }
  return new Error(msg);
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
    if (!res.ok) throw await httpError(res);
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
  _updateFreeLock(); // état initial du lock free (mode managé uniquement)
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

// Called by the catalogue modal's fiche "Start diagnostic" button. Pins the
// chosen device into the landing state + form, then runs the standard submit
// so ALL existing gating (free-lock, fresh-build preflight, disambiguation,
// navigation) applies unchanged — no POST duplication.
export async function launchFromCatalogue({ slug, label, complete, device_kind, symptom }) {
  _selectedDeviceSlug = slug || null;
  _selectedDeviceComplete = Boolean(complete);
  const deviceEl = document.getElementById("landingDevice");
  const symptomEl = document.getElementById("landingSymptom");
  const kindEl = document.getElementById("landingDeviceKind");
  if (deviceEl) deviceEl.value = label || "";
  if (symptomEl) symptomEl.value = symptom || "";
  if (kindEl) kindEl.value = device_kind || "";
  await submitDiagnostic();
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
  // En sortie de soumission, le lock free reprend la main sur l'état du bouton.
  if (!on) _updateFreeLock();
}

// Lock free du start-diagnostic (cloud_hints.packedOnly — mode managé
// uniquement) : le bouton reste désactivé tant que le tech n'a pas SÉLECTIONNÉ
// au picker un appareil dont le pack est complet (badge ✓), avec un texte
// d'aide + CTA upgrade sous le formulaire. Self-host : packedOnly() est faux,
// cette fonction ne touche à rien. Cosmétique pur — le cloud répond 402
// FREE_PACK_ONLY de toute façon si on force la soumission.
function _updateFreeLock() {
  if (!packedOnly()) return;
  const locked = !(_selectedDeviceSlug && _selectedDeviceComplete);
  const btn = document.getElementById("landingSubmit");
  if (btn && !isSubmitting) btn.disabled = locked;
  let hint = document.getElementById("landingFreeHint");
  if (!hint) {
    const form = document.getElementById("landingForm");
    if (!form) return;
    const t = window.t || ((k) => k);
    hint = document.createElement("p");
    hint.id = "landingFreeHint";
    hint.className = "landing-free-hint";
    hint.append(document.createTextNode(`${t("landing.free.locked_hint")} `));
    const a = document.createElement("a");
    a.href = "/app/upgrade";
    a.textContent = t("landing.free.upgrade_cta");
    hint.appendChild(a);
    form.appendChild(hint);
  }
  hint.hidden = !locked;
}

// Reset for a fresh run: clear the orchestration-pause state + device-kind
// panel (owned here), then delegate the phase-row DOM reset to timeline.js.
function resetTimeline() {
  _landingPaused = false;
  document.getElementById("landingKindPanel")?.remove();
  resetTimelineRows();
}

// A launch is a "fresh build" (full ~15-min pipeline, 1 credit on cloud) when
// the tech did NOT pick an already-complete known pack. Free-text or an
// incomplete pack → the backend will run the full pipeline. A complete pack
// pick → cache hit or cheap background expand (no pre-flight gate). Mirrors the
// free-lock predicate so the two stay consistent.
function _isFreshBuild() {
  return !(_selectedDeviceSlug && _selectedDeviceComplete);
}

async function onSubmit(ev) {
  ev?.preventDefault();
  return submitDiagnostic();
}

async function submitDiagnostic() {
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

  // Ceinture du lock free : la soumission implicite (Entrée) ne doit pas
  // contourner le bouton désactivé. Le serveur refuserait pareil (402).
  if (packedOnly() && !(_selectedDeviceSlug && _selectedDeviceComplete)) {
    _updateFreeLock();
    deviceEl?.focus();
    return;
  }

  // Fresh build → gate behind the pre-flight modal (credit cost, ~15-min build,
  // last-chance schematic, email opt-in). Confirming there calls _launchDiagnostic.
  // Known-complete pack → no gate, launch straight away (cache hit / expand).
  if (_isFreshBuild()) {
    openPreflightModal(device);
    return;
  }
  await _launchDiagnostic();
}

async function _launchDiagnostic() {
  const t = window.t || ((k) => k);
  const device = (document.getElementById("landingDevice")?.value || "").trim();
  const symptom = (document.getElementById("landingSymptom")?.value || "").trim();

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
    const boardNumber = (document.getElementById("landingBoardNumber")?.value || "").trim();
    if (boardNumber) body.append("board_number", boardNumber);
    // Signal the out-of-band schematic so the pipeline waits for its electrical
    // graph before device-kind classification (the upload fires below, post-create).
    if (_schematicFile) body.append("schematic_pending", "true");
    const res = await fetch("/pipeline/repairs", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    });
    if (!res.ok) throw await httpError(res);
    const repair = await res.json();

    // T9a confirm-on-uncertainty: a broad label matched several sibling boards.
    // No repair was created and no quota spent — show the candidate menu in the
    // suggest dropdown; picking one pins its device_slug, then the tech re-runs.
    if (repair && repair.needs_disambiguation) {
      setSubmitting(false);
      _renderDisambiguation(repair.candidates || []);
      setStatus(
        "Plusieurs cartes correspondent : choisis-en une, puis relance le diagnostic.",
        STATUS_NEUTRAL,
      );
      return;
    }

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
      // Cache hit — the pack is already on disk, there is genuinely nothing to
      // build. Go straight to the diagnostic workspace (no artificial wait). The
      // status message above is the lead-in. (A ~15s fake-timeline animation used
      // to play here for demo polish — removed; only the real flow remains.)
      goToWorkspace(rid, slug, "diagnostic");
      return;
    }

    // Branch 3 — pack exists but the symptom is new: the backend kicked a real
    // targeted expand in the background. The pack is on disk so the agent works
    // from existing rules immediately; the expand finishes silently. Go straight
    // to the workspace — no artificial wait. (A ~15s fake-timeline used to play
    // here too — removed.)
    if (repair.pipeline_kind === "expand") {
      setStatus(
        t("landing.status.device_known", { device: repair.device_label }),
        STATUS_NEUTRAL,
      );
      goToWorkspace(rid, slug, "diagnostic");
      return;
    }

    // Branch 1 — full pipeline on a fresh device. Preparing a brand-new device
    // can take a while (vision build, possibly queued), so be upfront about it.
    setStatus(t("landing.status.build_delay"), STATUS_NEUTRAL);
    showTimeline();
    setTimelineTitle(t("landing.timeline.title_build", { device: repair.device_label }));
    subscribeToProgress(slug, rid);
    // Email-when-ready opt-in was chosen in the pre-flight modal. Arm it now that
    // the repair exists. Cloud-only (the checkbox is hidden without plan hints,
    // and /notify is a front-door route), so self-host never reaches the POST.
    if (_preflightNotifyOptIn && planHints()) armNotify(rid);
  } catch (err) {
    console.error("[landing] submit failed", err);
    setStatus(t("landing.status.error_create", { error: err.message || err }), STATUS_ERROR);
    setLandingMascot("error");
    setSubmitting(false);
  }
}

// Arm the "email me when ready" opt-in chosen in the pre-flight modal. POSTs to
// the cloud front-door's /notify route, which emails the tech at
// pipeline_finished. Best-effort: a failure is logged, never blocks the build.
// Self-host never calls this (the checkbox is hidden without plan hints).
async function armNotify(repairId) {
  const tt = window.t || ((k) => k);
  try {
    const res = await fetch(`/pipeline/repairs/${encodeURIComponent(repairId)}/notify`, { method: "POST" });
    if (res.ok) setStatus(tt("landing.status.notify_armed"), STATUS_NEUTRAL);
  } catch (err) {
    console.warn("[landing] notify opt-in failed", err);
  }
}

// ============================================================
// Pre-flight launch-confirmation modal. Opened from onSubmit when the launch is
// a fresh build (_isFreshBuild). Makes the cost explicit and gives the tech a
// last chance to attach the schematic before a ~15-min build. The credit line
// and the email opt-in are cloud-only (gated on planHints()); self-host still
// sees the duration warning + schematic prompt. Confirm → _launchDiagnostic.
// ============================================================

let _preflightLastFocus = null;

// Reflect the shared _schematicFile state inside the modal: either the
// "attach it now" prompt or the "attached ✓" filename. Safe to call when the
// modal is closed (the elements just aren't visible).
function renderPreflightSchematic() {
  const hint = document.getElementById("landingPreflightSchematicHint");
  const name = document.getElementById("landingPreflightSchematicName");
  const pick = document.getElementById("landingPreflightSchematicPick");
  const t = window.t || ((k) => k);
  if (name) name.textContent = _schematicFile ? _schematicFile.name : "";
  if (hint) {
    hint.textContent = _schematicFile
      ? t("landing.preflight.schematic_attached")
      : t("landing.preflight.schematic_none");
  }
  if (pick) {
    pick.textContent = _schematicFile
      ? t("landing.preflight.schematic_replace")
      : t("landing.schematic.cta");
  }
}

function openPreflightModal(deviceLabel) {
  const bd = document.getElementById("landingPreflightBackdrop");
  if (!bd) {
    // No modal markup (shouldn't happen) — fail open rather than block a launch.
    _launchDiagnostic();
    return;
  }
  const t = window.t || ((k) => k);
  _preflightLastFocus = document.activeElement;

  const title = document.getElementById("landingPreflightTitle");
  if (title) title.textContent = t("landing.preflight.title", { device: deviceLabel });

  // Cloud-only rows: the credit line and the email opt-in. Hidden on self-host
  // (no plan hints) — the engine must not surface a billing/email concept.
  const cloud = !!planHints();
  const credit = document.getElementById("landingPreflightCredit");
  if (credit) credit.hidden = !cloud;
  const notifyRow = document.getElementById("landingPreflightNotifyRow");
  if (notifyRow) notifyRow.hidden = !cloud;
  const notifyCb = document.getElementById("landingPreflightNotify");
  if (notifyCb) notifyCb.checked = false;

  // Schematic block: hidden on plans that can't upload (defensive — the free
  // lock already blocks fresh-build launches before we get here).
  const schemField = document.getElementById("landingPreflightSchematicField");
  if (schemField) schemField.hidden = hideUploads();
  renderPreflightSchematic();

  bd.classList.add("open");
  bd.setAttribute("aria-hidden", "false");
  requestAnimationFrame(() => document.getElementById("landingPreflightConfirm")?.focus());
}

function closePreflightModal() {
  const bd = document.getElementById("landingPreflightBackdrop");
  if (!bd || !bd.classList.contains("open")) return;
  bd.classList.remove("open");
  bd.setAttribute("aria-hidden", "true");
  if (_preflightLastFocus && typeof _preflightLastFocus.focus === "function") {
    _preflightLastFocus.focus();
  }
}

async function confirmPreflight() {
  const cb = document.getElementById("landingPreflightNotify");
  _preflightNotifyOptIn = !!(cb && !cb.hidden && cb.checked);
  closePreflightModal();
  await _launchDiagnostic();
}

function subscribeToProgress(slug, repairId, { autoNav = true } = {}) {
  if (progressConn) { progressConn.close(); progressConn = null; }
  // Remember the active build so confirmLandingKind() can re-subscribe to the
  // fresh build on the same slug after the tech resolves a kind disagreement.
  _activeSlug = slug;
  _activeRid = repairId;
  _autoNavOnFinish = autoNav;

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

// Re-attach the live build timeline for a repair whose pack is still building.
// The progress event bus replays its recent-event ring buffer on (re)subscribe,
// so the timeline catches up to the phases already done instead of staring at a
// blank until the next phase boundary. Shared by the tile-resume click (autoNav
// true) and the passive resume-on-load (autoNav false).
function resumeBuild(slug, repairId, { autoNav = true } = {}) {
  // Both callers (tile click, passive resume-on-load) already run with the hero
  // visible — do NOT call showLanding() here: it re-renders the sidebar, which
  // re-enters maybeResumeActiveBuild before progressConn is set → recursion.
  const t = window.t || ((k) => k);
  resetTimeline();
  showTimeline();
  setTimelineTitle(t("landing.timeline.title_build", { device: prettifySlug(slug) }));
  setStatus(t("landing.status.build_delay"), STATUS_NEUTRAL);
  setLandingMascot("working");
  subscribeToProgress(slug, repairId, { autoNav });
}

// On landing (re)load, if a build was in flight, resume its timeline passively.
// Guarded so we never stomp an already-active connection (a fresh submit, or a
// resume already running) and never auto-resume while the tech is submitting.
function maybeResumeActiveBuild(repairs) {
  if (progressConn || isSubmitting) return;
  // Newest-first list (sorted by caller) → the first building repair is the most
  // recent one. The build cap means there's normally at most one in flight.
  const building = (repairs || []).find((r) => r.build_state === "building");
  if (!building) return;
  resumeBuild(building.device_slug, building.repair_id, { autoNav: false });
}

// Localize a live `phase_step` sub-step into the short line the timeline shows
// ("recherche web · tour 2", "page 3/12", "graphe ✓ 142 nœuds", "révision 1").
// Returns "" for an unknown step kind (forward-compat — ignored silently).
function phaseStepText(ev, t) {
  switch (ev.step) {
    case "search_round":
      return t("landing.timeline.step.search_round", { index: ev.index });
    case "page":
      return t("landing.timeline.step.page", { index: ev.index, total: ev.total });
    case "writer_done": {
      const key = ev.writer === "rules" ? "step.writer_rules"
        : ev.writer === "dict" ? "step.writer_dict"
        : "step.writer_graph";
      return t("landing.timeline." + key, { count: ev.count });
    }
    case "round":
      return ev.index >= 1
        ? t("landing.timeline.step.revision", { index: ev.index })
        : t("landing.timeline.step.audit");
    default:
      return "";
  }
}

function handleProgressEvent(ev, slug, repairId) {
  const t = window.t || ((k) => k);
  switch (ev.type) {
    case "subscribed":
      break;
    case "queued": {
      // Build accepté mais EN ATTENTE derrière le cap de builds concurrents.
      // On montre clairement la position ; elle décroît à mesure que la file se
      // vide, puis `pipeline_started` prend le relais quand un créneau se libère.
      const position = ev.position || 1;
      const ahead = ev.ahead != null ? ev.ahead : Math.max(0, position - 1);
      setTimelineTitle(t("landing.timeline.title_queued", { position }));
      setStatus(t("landing.status.queued", { position, ahead }), STATUS_LOADING);
      setLandingMascot("working");
      break;
    }
    case "pipeline_started": {
      const dev = ev.device_label || ev.device_slug || slug;
      // Reset the title in case it was showing the queued state ("En file
      // d'attente · position N") — the build just left the queue and started.
      setTimelineTitle(t("landing.timeline.title_build", { device: dev }));
      setStatus(t("landing.status.pipeline_started", { device: dev }), STATUS_LOADING);
      break;
    }
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
    case "phase_step": {
      const phase = ev.phase;
      if (!(PHASE_ORDER.includes(phase) || phase === "expand" || phase in LANDING_DYNAMIC_PHASES)) break;
      const text = phaseStepText(ev, t);
      if (text) setPhaseStep(phase, text);
      break;
    }
    case "pipeline_finished": {
      setTimelineTitle(t("landing.timeline.title_ready", { status: ev.status || "" }));
      setStatus(t("landing.status.ready"), STATUS_NEUTRAL);
      stopEtaTicker();
      setLandingMascot("success");
      if (_autoNavOnFinish) {
        // Fresh submit / explicit tile-resume: take the tech into the now-ready
        // device. Short grace so the final audit sub-step renders first.
        setTimeout(() => goToWorkspace(repairId, slug), 2500);
      } else {
        // Passive resume-on-load: don't yank a browsing tech. Just refresh the
        // sidebar so the tile flips from "building" to ready (clickable into the
        // workspace), and drop the now-finished progress connection.
        if (progressConn) { progressConn.close(); progressConn = null; }
        loadAndRenderSidebar();
      }
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
// Complétude du pack du device sélectionné au picker (badge ✓). Sert au lock
// free (cloud_hints.packedOnly) : le plan gratuit ne lance que sur un pack
// complet — cosmétique, le cloud refuse pareil côté serveur (402).
let _selectedDeviceComplete = false;
let _schematicFile = null;

async function loadPacksForSuggest() {
  try {
    _devicesCache = await loadDevices();
  } catch (err) {
    console.warn("[landing] loadPacksForSuggest failed", err);
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
      // T9a: match carnet aliases too (board# / Apple model / EMC / codename /
      // marketing) so "820-2533" or "A1286" finds the MacBook Pro 15 pack.
      const aliases = (d.aliases || []).join(" ").toLowerCase();
      // Also match the version line shown on the suggestion (the Apple model
      // number(s) / board# live there, e.g. "A1984" finds the iPhone XR) —
      // carnet aliases are empty for packs not registered in it.
      const version = (d.version || "").toLowerCase();
      return label.includes(q) || sub.includes(q) || slug.includes(q)
        || aliases.includes(q) || version.includes(q);
    })
    .slice(0, 6);
}

// Second line of a suggestion: the identifiers that distinguish THIS exact board
// — the model version / Apple number(s), the board number (820-xxxx) when known,
// and the form factor. e.g. "A2172 / A2176 · logic board" or
// "A1286 · 820-2533 · logic board". Empty when nothing distinguishing is known.
function _deviceIdLine(d) {
  const parts = [];
  const seen = new Set();
  const push = (v) => {
    const s = (v == null ? "" : String(v)).trim();
    if (!s) return;
    const k = s.toLowerCase();
    if (!seen.has(k)) { seen.add(k); parts.push(s); }
  };
  if (d.version) push(d.version);
  // Board number(s) from the carnet aliases, if not already in the version text.
  const vtext = (d.version || "").toLowerCase();
  for (const a of (d.aliases || [])) {
    const m = String(a).match(/\b8\d{2}-\d{3,5}\b/);
    if (m && !vtext.includes(m[0].toLowerCase())) push(m[0]);
  }
  if (d.form_factor) push(d.form_factor);
  return parts.join(" · ");
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
    // Readiness badges (right of line 1): draft marker (incomplete pack), the
    // graph badge (lit when an electrical graph is compiled), and the device-kind
    // short code (GPU / PORTABLE / …).
    const draftBadge = d.complete ? "" : `<span class="landing-suggest-badge is-draft">${_escapeHtml(draftLabel)}</span>`;
    const graphBadge = `<span class="landing-suggest-badge${d.has_electrical_graph ? " is-on" : ""}" title="${_escapeHtml(tFn("landing.suggest.graph_title"))}">${_escapeHtml(tFn("landing.suggest.graph_label"))}</span>`;
    const kindBadge = (d.device_kind && d.device_kind !== "unknown")
      ? `<span class="landing-suggest-badge mono">${_escapeHtml(DEVICE_KIND_SHORT[d.device_kind] || d.device_kind)}</span>`
      : "";
    const idLine = _escapeHtml(_deviceIdLine(d));
    const brand = safeSub ? `<span class="landing-suggest-brand">${safeSub}</span>` : "";
    // data-label = the short model name (e.g. "iPhone 12") that lands in
    // the input on selection. NOT d.device_label, which is the raw
    // registry label (e.g. "Apple iPhone 12 logic board") and would
    // pollute the input with brand + form-factor noise.
    return `<div class="landing-suggest-item" role="option" `
      + `data-slug="${safeSlug}" data-label="${safeLabel}" data-index="${i}" `
      + `data-graph="${d.has_electrical_graph ? "1" : ""}" data-complete="${d.complete ? "1" : ""}">`
      + `<span class="landing-suggest-icon ${iconClass}" aria-hidden="true">${iconText}</span>`
      + `<div class="landing-suggest-body">`
      + `<div class="landing-suggest-line1">`
      + `<span class="landing-suggest-label">${safeLabel}</span>${brand}`
      + `<span class="landing-suggest-meta">${draftBadge}${graphBadge}${kindBadge}</span>`
      + `</div>`
      + (idLine ? `<div class="landing-suggest-ids">${idLine}</div>` : "")
      + `</div>`
      + `</div>`;
  }).join("");
  box.hidden = false;
  _suggestActiveIdx = -1;
}

// T9a: render the disambiguation candidates into the suggest dropdown using the
// SAME .landing-suggest-item markup, so the existing mousedown handler pins the
// chosen device_slug (via _selectSuggest) with zero extra wiring.
function _renderDisambiguation(candidates) {
  const box = document.getElementById("landingSuggest");
  if (!box || !Array.isArray(candidates) || candidates.length === 0) return;
  box.innerHTML = candidates.map((c, i) => {
    const f = c.facets || {};
    const label = (f.marketing && f.marketing[0]) || (f.board && f.board[0]) || c.device_slug;
    const detail = [f.board && f.board[0], c.family].filter(Boolean).join(" · ");
    return `<div class="landing-suggest-item" role="option" `
      + `data-slug="${_escapeHtml(c.device_slug)}" data-label="${_escapeHtml(label)}" data-index="${i}" data-graph="">`
      + `<span class="landing-suggest-icon is-partial" aria-hidden="true">•</span>`
      + `<span class="landing-suggest-label">${_escapeHtml(label)}</span>`
      + `<span class="landing-suggest-meta">${_escapeHtml(detail)}</span>`
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

function _selectSuggest(label, slug, hasGraph, isComplete) {
  const dev = document.getElementById("landingDevice");
  const sym = document.getElementById("landingSymptom");
  if (dev) dev.value = label;
  // Pin the canonical slug so onSubmit sends device_slug to the backend
  // (skips re-slugification of the label and guarantees the cache hit
  // on the right pack — defends against near-but-not-identical spellings).
  _selectedDeviceSlug = slug || null;
  _selectedDeviceComplete = !!isComplete;
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
  _updateFreeLock();
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
    _selectedDeviceComplete = false;
    resetSchematicField();
    _renderSuggest(dev.value);
    _updateFreeLock();
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
      if (item) _selectSuggest(item.dataset.label, item.dataset.slug, !!item.dataset.graph, !!item.dataset.complete);
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
      _selectSuggest(item.dataset.label, item.dataset.slug, !!item.dataset.graph, !!item.dataset.complete);
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
  // Plan free (mode managé) : pas d'analyse de nouveau fichier → l'affordance
  // « Add knowledge » disparaît EN ENTIER : le bouton, son « ? » d'explication
  // (qui ouvrait le modal info) et la rangée de chips. Le serveur refuse
  // l'upload de toute façon (402).
  if (hideUploads()) {
    for (const id of ["landingKnowledgeBtn", "landingKnowledgeInfo", "landingKnowledgeChips"]) {
      const el = document.getElementById(id);
      if (el) el.hidden = true;
    }
  }
  document.getElementById("landingSchematicPick")?.addEventListener("click", () => {
    document.getElementById("landingSchematic")?.click();
  });
  document.getElementById("landingSchematic")?.addEventListener("change", (e) => {
    _schematicFile = e.target.files?.[0] || null;
    const n = document.getElementById("landingSchematicName");
    if (n) n.textContent = _schematicFile ? _schematicFile.name : "";
    e.target.value = "";
    renderKnowledgeIndicators();
    renderPreflightSchematic();   // keep the pre-flight modal's view in sync
  });
  // Pre-flight modal: its schematic pick triggers the SAME hidden file input as
  // the knowledge modal (single source of truth, _schematicFile). Cancel/confirm
  // gate the actual launch; the backdrop + Escape dismiss like the other modals.
  document.getElementById("landingPreflightSchematicPick")?.addEventListener("click", () => {
    document.getElementById("landingSchematic")?.click();
  });
  document.getElementById("landingPreflightCancel")?.addEventListener("click", closePreflightModal);
  document.getElementById("landingPreflightClose")?.addEventListener("click", closePreflightModal);
  document.getElementById("landingPreflightConfirm")?.addEventListener("click", confirmPreflight);
  const pfBackdrop = document.getElementById("landingPreflightBackdrop");
  if (pfBackdrop) {
    pfBackdrop.addEventListener("click", (ev) => {
      if (ev.target === pfBackdrop) closePreflightModal();
    });
  }
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
    if (ev.key === "Escape") { closeKnowledgeModal(); closePreflightModal(); closeRepairsDrawer(); closeCatalogue(); }
  });
  // Mobile recent-repairs drawer: toggle from the nav, dismiss via the scrim.
  document.getElementById("landingRepairsToggle")?.addEventListener("click", () => {
    const open = document.getElementById("landing-overlay")?.classList.contains("sidebar-open");
    if (open) closeRepairsDrawer(); else openRepairsDrawer();
  });
  document.getElementById("landingSidebarBackdrop")?.addEventListener("click", closeRepairsDrawer);
  document.getElementById("landingDeviceKind")?.addEventListener("change", renderKnowledgeIndicators);
  const kChips = document.getElementById("landingKnowledgeChips");
  if (kChips) kChips.addEventListener("click", onKnowledgeChipClick);
  _initSuggest();
  renderKnowledgeIndicators();
  initCatalogue();
  initProfileMenu();
}
