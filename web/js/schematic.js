// Schematic section V5 — Power Diagnostic Dashboard.
//
// Not a KiCad replica — a view that answers questions the PDF cannot:
//   - Where does +3V3 come from, end-to-end?
//   - If U7 dies, what else loses power?
//   - Which rails stabilise in which boot phase?
//
// Scope: only the ~115 components that matter for power diagnostics on
// MNT-class boards — rails + their source ICs + consumer ICs + decoupling
// caps. The 300 signal-only routing passives (R*, C*) stay in the PDF.
//
// Layout: X = causal depth in the power tree (BFS from root rails), not
// voltage buckets or schematic pages. Root rails (external supplies) sit
// far left, downstream regulators flow right. Y is force-determined with
// soft column clustering + strong collide.
//
// Killer features:
//   - Kill-switch cascade: click a node → highlight everything that dies.
//   - Boot timeline: swim-lane of the 4 boot phases at the bottom.
//   - Rich inspector: rail consumers, enable chains, decoupling margin.

const STATE = {
  slug: null,
  graph: null,
  model: null,
  zoom: null,
  selectedId: null,
  killswitch: false,         // when true, focus mode shows the full cascade
  showSignals: false,
  showAllPins: false,
  // "railfocus" (default, one rail at a time), "powertree" (all rails stacked),
  // "grid" (phase × voltage 2D). Persisted to localStorage so the user's
  // choice sticks.
  layoutMode: (typeof localStorage !== "undefined" && localStorage.getItem("schLayoutMode")) || "railfocus",
  // In railfocus mode, which rail is currently shown in the canvas.
  selectedRailId: (typeof localStorage !== "undefined" && localStorage.getItem("schSelectedRail")) || null,
};

// Infer the nominal voltage from a canonical rail label.
// "+3V3" → 3.3, "+5V" → 5, "+1V8" → 1.8, "+12V" → 12. Unknown labels → null.
function inferRailNominalV(label) {
  if (typeof label !== "string") return null;
  const m = label.match(/^\+?(\d+)V(\d+)?$/i);
  if (!m) return null;
  const whole = parseInt(m[1], 10);
  if (!m[2]) return whole;
  const frac = parseFloat(`0.${m[2]}`);
  return whole + frac;
}

// Client-side mirror of api/agent/measurement_memory.py::auto_classify.
// Keep thresholds in sync with the Python constants.
function clientAutoClassify(kind, value, unit, nominal) {
  if (kind === "rail" && (unit === "V" || unit === "mV")) {
    if (nominal == null || nominal === "") return null;
    const v = unit === "mV" ? value / 1000 : value;
    const nom = unit === "mV" ? nominal / 1000 : nominal;
    if (v < 0.05) return "dead";
    const ratio = nom !== 0 ? v / nom : 0;
    if (ratio > 1.10) return "shorted";
    if (ratio >= 0.90) return "alive";
    return "anomalous";
  }
  if (kind === "comp" && unit === "°C") {
    return value >= 65 ? "hot" : "alive";
  }
  return null;
}

/* ---------------------------------------------------------------------- *
 * SIMULATION                                                             *
 * Drives the behavioral simulator UI: fetches a SimulationTimeline from  *
 * POST /pipeline/packs/{slug}/schematic/simulate, exposes playback       *
 * controls, and applies sim-* CSS classes to nodes/rails for each phase. *
 * Scaffold for now — scrubber UI and state-class propagation land in     *
 * subsequent commits.                                                    *
 * ---------------------------------------------------------------------- */

const SimulationController = {
  timeline: null,          // server response
  killedRefdes: [],        // user-injected faults
  observations: {
    state_comps:   new Map(),     // refdes → "dead" | "alive" | "anomalous" | "hot"
    state_rails:   new Map(),     // rail label → "dead" | "alive" | "shorted"
    metrics_comps: new Map(),     // refdes → {measured, unit, nominal?, note?, ts}
    metrics_rails: new Map(),     // rail → {measured, unit, nominal?, note?, ts}
  },
  hypotheses: null,
  playing: false,
  speedMs: 800,            // ms per phase at 1×
  cursor: 0,               // current phase index within timeline.states
  _timer: null,

  async refresh(slug) {
    if (!slug) return;
    try {
      const res = await fetch(
        `/pipeline/packs/${encodeURIComponent(slug)}/schematic/simulate`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ killed_refdes: this.killedRefdes }),
        },
      );
      if (!res.ok) {
        console.warn("[simulator] fetch failed", res.status);
        this.timeline = null;
        return;
      }
      this.timeline = await res.json();
      this.cursor = 0;
      this.render();
    } catch (err) {
      console.warn("[simulator] fetch error", err);
      this.timeline = null;
    }
  },

  render() {
    this._ensureScrubber();
    // Only paint the graph with phase state when the scrubber is open —
    // if the user dismissed the timeline, keep the graph in its default look.
    const stored = (typeof localStorage !== "undefined" && localStorage.getItem("simScrubberVisible")) ?? "1";
    if (stored !== "0") {
      this._applyStateClasses();
    } else {
      this._clearStateClasses();
    }
    this._updateScrubberLabel();
  },

  _ensureScrubber() {
    const host = document.querySelector("#schematicSection") || document.body;
    // Mount the toggle-to-open chip once; it's always present and flips
    // visibility depending on whether the scrubber itself is open.
    let chip = document.querySelector(".sim-scrubber-toggle");
    if (!chip) {
      chip = document.createElement("button");
      chip.className = "sim-scrubber-toggle";
      chip.title = "Afficher la timeline";
      chip.textContent = "▸ Timeline";
      host.appendChild(chip);
      chip.addEventListener("click", () => this._setVisible(true));
    }

    let el = document.querySelector(".sim-scrubber");
    if (!el) {
      el = document.createElement("div");
      el.className = "sim-scrubber";
      el.innerHTML = `
        <button data-act="rewind" title="Début">⏮</button>
        <button data-act="step-back" title="Phase précédente">◀</button>
        <button data-act="play-pause">▶</button>
        <button data-act="step-fwd" title="Phase suivante">▶</button>
        <input type="range" min="0" max="0" step="1" value="0" />
        <span class="sim-phase-label">—</span>
        <span class="sim-blocked-overlay" hidden></span>
        <button data-act="close" class="sim-scrubber-close" title="Masquer la timeline">×</button>
      `;
      host.appendChild(el);
      el.addEventListener("click", (ev) => {
        const act = ev.target?.dataset?.act;
        if (!act) return;
        if (act === "rewind") this.seek(0);
        else if (act === "step-back") this.seek(this.cursor - 1);
        else if (act === "step-fwd") this.seek(this.cursor + 1);
        else if (act === "play-pause") this.playing ? this.pause() : this.play();
        else if (act === "close") this._setVisible(false);
      });
      el.querySelector("input[type=range]").addEventListener("input", (ev) => {
        this.seek(Number(ev.target.value));
      });
    }
    const total = (this.timeline?.states?.length ?? 1) - 1;
    const range = el.querySelector("input[type=range]");
    range.max = Math.max(0, total);
    range.value = this.cursor;
    // Scrubber is shown only when there's a timeline AND the user hasn't
    // explicitly closed it. `visible` persists across reloads via localStorage.
    const hasTl = !!(this.timeline && this.timeline.states.length > 0);
    const stored = (typeof localStorage !== "undefined" && localStorage.getItem("simScrubberVisible")) ?? "1";
    const visible = hasTl && stored !== "0";
    el.hidden = !visible;
    chip.hidden = !hasTl || visible;
  },

  _setVisible(on) {
    try { localStorage.setItem("simScrubberVisible", on ? "1" : "0"); } catch (_) {}
    if (!on) {
      this.pause();
      this._clearStateClasses();
    }
    this._ensureScrubber();
    if (on) this._applyStateClasses();
  },

  _updateScrubberLabel() {
    const el = document.querySelector(".sim-scrubber");
    if (!el) return;
    const state = this.timeline?.states?.[this.cursor];
    const label = state ? `Φ${state.phase_index} · ${state.phase_name}` : "—";
    el.querySelector(".sim-phase-label").textContent = label;
    const overlay = el.querySelector(".sim-blocked-overlay");
    if (state?.blocked) {
      overlay.textContent = `BLOQUÉE — ${state.blocked_reason ?? "cascade"}`;
      overlay.hidden = false;
    } else {
      overlay.hidden = true;
    }
    el.querySelector("[data-act=play-pause]").textContent = this.playing ? "⏸" : "▶";
  },

  _clearStateClasses() {
    // Remove every sim-* class from the schematic DOM so the graph returns
    // to its default appearance (no dimming, no cascade glyphs, no dead
    // outlines). Called when the user closes the timeline toggle.
    document.querySelectorAll(
      ".sim-off, .sim-rising, .sim-stable, .sim-dead, .sim-signal-high, .sim-signal-low, .sim-cascade"
    ).forEach((n) => n.classList.remove(
      "sim-off", "sim-rising", "sim-stable", "sim-dead", "sim-signal-high", "sim-signal-low", "sim-cascade",
    ));
  },

  _applyStateClasses() {
    const state = this.timeline?.states?.[this.cursor];
    if (!state) return;
    // Clear prior classes on anything currently marked.
    this._clearStateClasses();

    // Nodes — we rely on the existing graph renderer having attached
    // `data-refdes` / `data-rail` / `data-signal` on each selectable element.
    // If the attributes aren't wired yet (Task 13), this is a no-op for those
    // classes; the scrubber itself still renders.
    for (const [refdes, st] of Object.entries(state.components || {})) {
      document.querySelectorAll(`[data-refdes="${CSS.escape(refdes)}"]`).forEach((el) => {
        el.classList.add(`sim-${st}`);
      });
    }
    for (const [label, st] of Object.entries(state.rails || {})) {
      document.querySelectorAll(`[data-rail="${CSS.escape(label)}"]`).forEach((el) => {
        el.classList.add(`sim-${st}`);
      });
    }
    for (const [label, st] of Object.entries(state.signals || {})) {
      document.querySelectorAll(`[data-signal="${CSS.escape(label)}"]`).forEach((el) => {
        el.classList.add(`sim-signal-${st}`);
      });
    }

    // Overlay: cascade-dead nodes — downstream of a killed upstream rail
    // source but NOT directly killed by the user. Timeline-wide, not
    // phase-specific — once a cascade is computed, those nodes carry the
    // badge for the entire playback.
    const tl = this.timeline;
    if (tl) {
      const killedSet = new Set(tl.killed_refdes || []);
      for (const refdes of (tl.cascade_dead_components || [])) {
        if (killedSet.has(refdes)) continue;
        document.querySelectorAll(`[data-refdes="${CSS.escape(refdes)}"]`).forEach((el) => {
          el.classList.add("sim-cascade");
        });
      }
      for (const label of (tl.cascade_dead_rails || [])) {
        document.querySelectorAll(`[data-rail="${CSS.escape(label)}"]`).forEach((el) => {
          el.classList.add("sim-cascade");
        });
      }
    }
  },

  seek(idx) {
    const max = (this.timeline?.states?.length ?? 1) - 1;
    this.cursor = Math.max(0, Math.min(idx, max));
    this.render();
  },
  play() {
    if (!this.timeline || this.timeline.states.length === 0) return;
    this.playing = true;
    clearInterval(this._timer);
    this._timer = setInterval(() => {
      const max = this.timeline.states.length - 1;
      if (this.cursor >= max) { this.pause(); return; }
      this.seek(this.cursor + 1);
    }, this.speedMs);
    this._updateScrubberLabel();
  },
  pause() {
    this.playing = false;
    clearInterval(this._timer);
    this._timer = null;
    this._updateScrubberLabel();
  },

  // ---- Observations ----
  setObservation(kind, key, mode, measurement = null) {
    // kind: "comp" | "rail"
    // mode: "dead" | "alive" | "anomalous" | "hot" | "shorted" | "unknown"
    const stateMap  = kind === "comp" ? this.observations.state_comps  : this.observations.state_rails;
    const metricMap = kind === "comp" ? this.observations.metrics_comps : this.observations.metrics_rails;
    if (mode === "unknown" || mode == null) {
      stateMap.delete(key);
      metricMap.delete(key);
    } else {
      stateMap.set(key, mode);
      if (measurement) {
        metricMap.set(key, {
          ...measurement,
          ts: measurement.ts || new Date().toISOString(),
        });
      }
    }
    this._applyObservationClasses();
  },
  clearObservations() {
    for (const m of Object.values(this.observations)) m.clear();
    this.hypotheses = null;
    this._applyObservationClasses();
    document.querySelectorAll(".sim-hypotheses-panel").forEach(p => p.remove());
  },
  // Fetch the repair's measurement journal and seed the local observation
  // Maps with the latest event per target. Mirrors the Python side's
  // synthesise_observations (latest-per-target wins, state lit only for
  // valid mode literals). Silent no-op when no repair_id is in the URL.
  async hydrateFromJournal(slug) {
    const repairId = new URLSearchParams(location.search).get("repair")
      || new URLSearchParams(location.hash.split("?")[1] || "").get("repair");
    if (!slug || !repairId) return;
    try {
      const res = await fetch(
        `/pipeline/packs/${encodeURIComponent(slug)}/repairs/${encodeURIComponent(repairId)}/measurements`,
      );
      if (!res.ok) return;
      const payload = await res.json();
      const events = payload.events || [];
      // Keep the latest event per target (events are stored in insertion order).
      const latest = new Map();
      for (const ev of events) latest.set(ev.target, ev);
      this.measurementHistory = events;  // full journal, used by T19 timeline
      const COMP_MODES = new Set(["dead", "alive", "anomalous", "hot"]);
      const RAIL_MODES = new Set(["dead", "alive", "shorted"]);
      for (const [target, ev] of latest) {
        const idx = target.indexOf(":");
        if (idx <= 0) continue;
        const kind = target.slice(0, idx);
        const key = target.slice(idx + 1);
        const mode = ev.auto_classified_mode;
        const measurement = (ev.value != null) ? {
          measured: ev.value, unit: ev.unit, nominal: ev.nominal,
          note: ev.note, ts: ev.timestamp,
        } : null;
        if (kind === "comp") {
          if (COMP_MODES.has(mode)) {
            this.observations.state_comps.set(key, mode);
          }
          if (measurement) this.observations.metrics_comps.set(key, measurement);
        } else if (kind === "rail") {
          // Allow "anomalous" locally for UI; it's stripped / coerced at POST.
          if (RAIL_MODES.has(mode) || mode === "anomalous") {
            this.observations.state_rails.set(key, mode);
          }
          if (measurement) this.observations.metrics_rails.set(key, measurement);
        }
      }
      this._applyObservationClasses();
    } catch (err) {
      console.warn("[hydrateFromJournal] failed", err);
    }
  },
  _applyObservationClasses() {
    document
      .querySelectorAll(".obs-dead, .obs-alive, .obs-anomalous, .obs-hot, .obs-shorted")
      .forEach(n => n.classList.remove(
        "obs-dead", "obs-alive", "obs-anomalous", "obs-hot", "obs-shorted",
      ));
    for (const [refdes, mode] of this.observations.state_comps) {
      document.querySelectorAll(`[data-refdes="${CSS.escape(refdes)}"]`).forEach(el => {
        el.classList.add(`obs-${mode}`);
      });
    }
    for (const [rail, mode] of this.observations.state_rails) {
      document.querySelectorAll(`[data-rail="${CSS.escape(rail)}"]`).forEach(el => {
        el.classList.add(`obs-${mode}`);
      });
    }
  },

  // ---- Reverse-diagnostic: hypothesize + results panel ----
  async hypothesize(slug) {
    const obs = this.observations;
    const totalObs = obs.state_comps.size + obs.state_rails.size
                   + obs.metrics_comps.size + obs.metrics_rails.size;
    if (totalObs === 0) return;
    // Backend RailMode accepts only dead/alive/shorted. Phase 1 scoring
    // doesn't model anomalous rails — we coerce sagging readings to "dead"
    // so the buck upstream still scores as top candidate. The raw metric
    // rides along in metrics_rails so the narrative cites the exact value.
    const RAIL_MODES = new Set(["dead", "alive", "shorted"]);
    const stateRailsOut = {};
    for (const [k, v] of obs.state_rails) {
      if (RAIL_MODES.has(v)) stateRailsOut[k] = v;
      else if (v === "anomalous") stateRailsOut[k] = "dead";
    }
    // Backend ObservedMetric forbids extras (ts, note). Strip UI-only fields.
    const stripMetric = (m) => {
      const out = { measured: m.measured, unit: m.unit };
      if (m.nominal != null) out.nominal = m.nominal;
      return out;
    };
    const metricsCompsOut = {};
    for (const [k, v] of obs.metrics_comps) metricsCompsOut[k] = stripMetric(v);
    const metricsRailsOut = {};
    for (const [k, v] of obs.metrics_rails) metricsRailsOut[k] = stripMetric(v);
    const body = {
      state_comps:   Object.fromEntries(obs.state_comps),
      state_rails:   stateRailsOut,
      metrics_comps: metricsCompsOut,
      metrics_rails: metricsRailsOut,
      max_results: 5,
    };
    try {
      const res = await fetch(
        `/pipeline/packs/${encodeURIComponent(slug)}/schematic/hypothesize`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
      );
      if (!res.ok) {
        const detail = await res.text();
        console.error("[hypothesize] HTTP", res.status, detail);
        return;
      }
      const payload = await res.json();
      this.hypotheses = payload.hypotheses || [];
      this._renderHypothesesPanel();
    } catch (err) {
      console.error("[hypothesize] fetch error", err);
    }
  },

  _renderHypothesesPanel() {
    document.querySelectorAll(".sim-hypotheses-panel").forEach(p => p.remove());
    if (!this.hypotheses || this.hypotheses.length === 0) return;
    const panel = document.createElement("div");
    panel.className = "sim-hypotheses-panel";
    panel.innerHTML = `
      <div class="sim-hyp-head">
        <span class="sim-hyp-title">Hypothèses (top ${this.hypotheses.length})</span>
        <button class="sim-hyp-close" title="Fermer">×</button>
      </div>
      <div class="sim-hyp-body"></div>
    `;
    panel.querySelector(".sim-hyp-close").addEventListener("click", () => panel.remove());

    const body = panel.querySelector(".sim-hyp-body");
    this.hypotheses.forEach((h, i) => {
      const card = document.createElement("div");
      card.className = "sim-hyp-card";
      const chips = h.kill_refdes.map((r, i) => {
        const m = (h.kill_modes || [])[i] || "dead";
        const modeLabel = { dead: "mort", anomalous: "anomalous", hot: "chaud", shorted: "shorté" }[m] || m;
        return `<span class="sim-hyp-chip sim-hyp-chip--${m}">${escHtml(r)} · ${modeLabel}</span>`;
      }).join(" + ");
      const contradictions = (h.diff.contradictions || []).map(c => {
        if (Array.isArray(c) && c.length === 3) {
          const [target, observed, predicted] = c;
          return `<span class="sim-hyp-tag sim-hyp-tag-fp">${escHtml(target)} obs ${escHtml(observed)} → prédit ${escHtml(predicted)}</span>`;
        }
        return `<span class="sim-hyp-tag sim-hyp-tag-fp">${escHtml(c)}</span>`;
      }).join(" ");
      const missing = (h.diff.under_explained || []).map(c => `<span class="sim-hyp-tag sim-hyp-tag-fn">${escHtml(c)}</span>`).join(" ");
      card.innerHTML = `
        <div class="sim-hyp-card-head">
          <span class="sim-hyp-rank">#${i + 1}</span>
          <span class="sim-hyp-kills">${chips}</span>
          <span class="sim-hyp-score">score ${h.score.toFixed(1)}</span>
        </div>
        <div class="sim-hyp-narr">${escHtml(h.narrative)}</div>
        ${contradictions ? `<div class="sim-hyp-diff"><span class="k">contredit</span> ${contradictions}</div>` : ""}
        ${missing ? `<div class="sim-hyp-diff"><span class="k">ne couvre pas</span> ${missing}</div>` : ""}
      `;
      card.addEventListener("click", () => {
        // Preview the cascade by injecting this kill set into the simulator.
        SimulationController.killedRefdes = [...h.kill_refdes];
        SimulationController.refresh(STATE.slug);
      });
      body.appendChild(card);
    });

    const host = document.querySelector("#schematicSection") || document.body;
    host.appendChild(panel);
  },
};

function getDeviceSlug() {
  const params = new URLSearchParams(window.location.search);
  return params.get("device") || null;
}

function el(id) { return document.getElementById(id); }

function escHtml(s) {
  if (s === null || s === undefined) return "";
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ---------------------------------------------------------------------- *
 * FETCH                                                                  *
 * ---------------------------------------------------------------------- */

async function fetchSchematic(slug) {
  try {
    const res = await fetch(`/pipeline/packs/${encodeURIComponent(slug)}/schematic`);
    if (res.status === 404) return { missing: true };
    if (!res.ok) return { error: `HTTP ${res.status}` };
    return { graph: await res.json() };
  } catch (err) {
    return { error: String(err) };
  }
}

/* ---------------------------------------------------------------------- *
 * MODEL — filter to diag-relevant components, compute causal depth       *
 * ---------------------------------------------------------------------- */

const POWER_PIN_ROLES = new Set([
  "power_in", "power_out", "switch_node", "enable_in", "enable_out",
  "power_good_out", "reset_in", "reset_out", "feedback_in", "ground",
]);

// R/L/Ferrites are always included in the default view when they touch a
// power rail — they're the pull-up/sense/filter passives that matter for
// power diagnostics. Module-level so buildModel can synthesize edges for
// them later in the same function.
const ALWAYS_RL_TYPES_GLOBAL = new Set(["resistor", "inductor", "ferrite"]);

// A component "touches a power rail" if any of its pins has a known
// power role (power_in/out, ground, switch_node, enable_in/out) or a
// `net_label` that matches a compiled rail label. Used to decide whether
// to auto-include an R/L/FB in the default power-tree view.
function touchesPowerRail(comp, rails) {
  for (const p of comp.pins || []) {
    const role = p.role || "";
    if (role === "power_in" || role === "power_out" || role === "ground" ||
        role === "switch_node" || role === "enable_in" || role === "enable_out" ||
        role === "power_good_out" || role === "feedback_in") {
      return true;
    }
    if (p.net_label && rails[p.net_label]) return true;
  }
  return false;
}

function firstPage(comp) {
  return (comp.pages && comp.pages.length) ? comp.pages[0] : 0;
}

function classifyPins(comp, showAll) {
  const pins = comp.pins || [];
  const visible = [];
  let hidden = 0;
  for (const p of pins) {
    const isPower = POWER_PIN_ROLES.has(p.role || "");
    if (showAll || isPower) visible.push(p);
    else hidden += 1;
  }
  return { all: pins, visible, hidden };
}

// Assign a side to each visible pin for rendering. Sources align inputs
// on the left, outputs on the right. Rules mirror layoutPins in V4 but
// simpler — V5 only pins ICs (sources + consumers), never decoupling caps.
function layoutPins(comp, showAll) {
  const { visible, hidden } = classifyPins(comp, showAll);
  const sides = { left: [], right: [], top: [], bottom: [] };
  const sideFor = (r) => {
    if (r === "power_in" || r === "enable_in" || r === "reset_in" || r === "feedback_in" || r === "clock_in") return "left";
    if (r === "power_out" || r === "switch_node" || r === "power_good_out" || r === "reset_out" || r === "enable_out" || r === "clock_out") return "right";
    if (r === "ground") return "bottom";
    return null;
  };
  const unsorted = [];
  for (const p of visible) {
    const s = sideFor(p.role);
    if (s) sides[s].push(p);
    else unsorted.push(p);
  }
  for (const p of unsorted) {
    const order = ["right", "left", "top", "bottom"].sort((a, b) => sides[a].length - sides[b].length);
    sides[order[0]].push(p);
  }
  return { sides, hidden, all: visible };
}

function buildModel(graph) {
  const rails = graph.power_rails || {};
  const components = graph.components || {};
  // Prefer Opus-refined boot sequence when present — richer phases with
  // kind, evidence, confidence, object-shaped triggers_next.
  const analyzed = graph.analyzed_boot_sequence;
  const source = graph.boot_sequence_source || "compiler";
  const boot = (source === "analyzer" && analyzed?.phases?.length)
    ? analyzed.phases
    : (graph.boot_sequence || []);

  // --- 1. Select the diag-relevant subset of components ---------------
  const sourceRefs = new Set();
  const consumerRefs = new Set();
  const decouplingRefs = new Set();
  for (const rail of Object.values(rails)) {
    if (rail.source_refdes) sourceRefs.add(rail.source_refdes);
    (rail.consumers || []).forEach(c => consumerRefs.add(c));
    (rail.decoupling || []).forEach(c => decouplingRefs.add(c));
  }

  const nodes = [];
  const nodeById = new Map();

  // Rails first.
  for (const [label, rail] of Object.entries(rails)) {
    const phaseIdx = boot.findIndex(p => (p.rails_stable || []).includes(label));
    const n = {
      id: `rail:${label}`,
      kind: "rail",
      label,
      voltage_nominal: rail.voltage_nominal,
      source_refdes: rail.source_refdes,
      source_type: rail.source_type,
      enable_net: rail.enable_net,
      consumers: rail.consumers || [],
      decoupling: rail.decoupling || [],
      phase: phaseIdx >= 0 ? boot[phaseIdx].index : null,
      width: 100, height: 36, shape: "hex",
    };
    nodes.push(n); nodeById.set(n.id, n);
  }

  // Components to include:
  //   - always: nodes referenced by a rail (as source, consumer, or
  //     decoupling cap) — the backbone of the power tree
  //   - always: resistors, inductors, ferrites whose pins touch a power
  //     rail (pull-ups on EN lines, sense resistors, filter inductors —
  //     invisible otherwise but useful for diagnosing a bias failure)
  //   - when STATE.showPassives is on: every remaining component from
  //     graph.components (~380 signal-only passives on MNT)
  const railReferenced = new Set([...sourceRefs, ...consumerRefs, ...decouplingRefs]);
  const all = new Set(railReferenced);
  for (const [refdes, comp] of Object.entries(components)) {
    if (STATE.showPassives) {
      all.add(refdes);
      continue;
    }
    if (ALWAYS_RL_TYPES_GLOBAL.has(comp.type) && touchesPowerRail(comp, rails)) {
      all.add(refdes);
    }
  }
  for (const refdes of all) {
    const comp = components[refdes];
    if (!comp) {
      // Referenced but missing from components — we still make a stub node
      // so edges don't orphan, just flag it.
      const n = {
        id: `comp:${refdes}`,
        kind: "component",
        refdes,
        type: "other",
        role: sourceRefs.has(refdes) ? "source" : (decouplingRefs.has(refdes) ? "decoupling" : "consumer"),
        missing: true,
        width: 40, height: 20, shape: "rect",
        pins: { sides: { left: [], right: [], top: [], bottom: [] }, hidden: 0, all: [] },
        phase: null,
      };
      nodes.push(n); nodeById.set(n.id, n);
      continue;
    }
    // Role: a regulator may also be a consumer — source role takes priority.
    const role = sourceRefs.has(refdes)
      ? "source"
      : (decouplingRefs.has(refdes) && !consumerRefs.has(refdes))
        ? "decoupling"
        : "consumer";
    const isPassive = role === "decoupling" || ["capacitor", "resistor", "inductor", "ferrite"].includes(comp.type);
    const size = role === "source" ? 64 : role === "decoupling" ? 14 : (isPassive ? 18 : 48);
    const shape = role === "decoupling" ? "capsule" : (role === "source" ? "rect-big" : (isPassive ? "capsule" : "rect"));
    const pins = layoutPins(comp, STATE.showAllPins);
    const showPins = role !== "decoupling" && comp.type !== "resistor";

    const phaseIdx = boot.findIndex(p => (p.components_entering || []).includes(refdes));
    const n = {
      id: `comp:${refdes}`,
      kind: "component",
      refdes,
      type: comp.type,
      value: comp.value,
      pages: comp.pages || [],
      populated: comp.populated !== false,
      role,
      pins,
      showPins,
      pinsAll: comp.pins || [],
      phase: phaseIdx >= 0 ? boot[phaseIdx].index : null,
      width: size + (role === "source" ? 10 : 0),
      height: size,
      shape,
    };
    // Resize IC width based on pin count per side so they don't overlap.
    if (role === "source" || role === "consumer") {
      const maxSide = Math.max(pins.sides.left.length, pins.sides.right.length);
      n.height = Math.max(n.height, 18 + maxSide * 12);
      const maxTopBot = Math.max(pins.sides.top.length, pins.sides.bottom.length);
      n.width = Math.max(n.width, 34 + maxTopBot * 12);
    }
    nodes.push(n); nodeById.set(n.id, n);
  }

  // --- 2. Edges --------------------------------------------------------
  const edges = [];
  for (const [label, rail] of Object.entries(rails)) {
    const railId = `rail:${label}`;
    if (rail.source_refdes && nodeById.has(`comp:${rail.source_refdes}`)) {
      edges.push({
        id: `e:prod:${rail.source_refdes}->${label}`,
        kind: "produces",
        sourceId: `comp:${rail.source_refdes}`,
        targetId: railId,
        netLabel: label,
      });
    }
    for (const c of rail.consumers || []) {
      if (c === rail.source_refdes) continue;
      if (!nodeById.has(`comp:${c}`)) continue;
      edges.push({
        id: `e:pow:${label}->${c}`,
        kind: "powers",
        sourceId: railId,
        targetId: `comp:${c}`,
        netLabel: label,
      });
    }
    for (const d of rail.decoupling || []) {
      if (!nodeById.has(`comp:${d}`)) continue;
      edges.push({
        id: `e:dec:${d}->${label}`,
        kind: "decouples",
        sourceId: `comp:${d}`,
        targetId: railId,
        netLabel: label,
      });
    }
  }

  // --- 2b. Synthesize missing edges for R / L / ferrite ---------------
  // An always-included R/L/FB that touches a rail (via its pins) but
  // isn't listed in `rail.consumers` has no explicit edge from Opus —
  // without a visible link, the viz looks like the component is floating
  // on the rail line unrelated to it. Create a `powers` edge from the
  // rail to the component for every rail-touching pin, so the user
  // actually sees *why* it sits there.
  const existingEdgeKeys = new Set(
    edges.map(e => `${e.kind}|${e.sourceId}|${e.targetId}`)
  );
  for (const [refdes, comp] of Object.entries(components)) {
    if (!ALWAYS_RL_TYPES_GLOBAL.has(comp.type)) continue;
    const compId = `comp:${refdes}`;
    if (!nodeById.has(compId)) continue;
    const touchedRails = new Set();
    for (const p of comp.pins || []) {
      if (p.net_label && rails[p.net_label] && p.net_label !== "GND") {
        touchedRails.add(p.net_label);
      }
    }
    for (const railLabel of touchedRails) {
      const railId = `rail:${railLabel}`;
      const key = `powers|${railId}|${compId}`;
      if (existingEdgeKeys.has(key)) continue;
      edges.push({
        id: `e:pow-syn:${railLabel}->${refdes}`,
        kind: "powers",
        sourceId: railId,
        targetId: compId,
        netLabel: railLabel,
      });
      existingEdgeKeys.add(key);
    }
  }

  // --- 2c. Signal edges (opt-in via the "Signaux" toggle) -------------
  // When STATE.showSignals is on, surface non-power typed_edges (enables,
  // clocks, resets, produces_signal, consumes_signal) so the tech can
  // follow PG / EN / CLOCK chains through the ICs. These edges clutter
  // the viz when always visible — hence the toggle.
  if (STATE.showSignals) {
    const SIGNAL_KINDS = new Set([
      "enables", "clocks", "resets", "produces_signal",
      "consumes_signal", "feedback_in",
    ]);
    for (const e of graph.typed_edges || []) {
      if (!SIGNAL_KINDS.has(e.kind)) continue;
      const srcId = nodeById.has(`comp:${e.src}`)
        ? `comp:${e.src}`
        : nodeById.has(`rail:${e.src}`) ? `rail:${e.src}` : null;
      const dstId = nodeById.has(`comp:${e.dst}`)
        ? `comp:${e.dst}`
        : nodeById.has(`rail:${e.dst}`) ? `rail:${e.dst}` : null;
      if (!srcId || !dstId || srcId === dstId) continue;
      const key = `signal|${srcId}|${dstId}|${e.kind}`;
      if (existingEdgeKeys.has(key)) continue;
      edges.push({
        id: `e:sig:${e.kind}:${e.src}->${e.dst}`,
        kind: "signal",
        subkind: e.kind,
        sourceId: srcId,
        targetId: dstId,
        netLabel: null,
      });
      existingEdgeKeys.add(key);
    }
  }

  // --- 3. Causal depth (BFS) ------------------------------------------
  // Root rails: no source_refdes OR source_refdes not in our node set.
  const depth = new Map();
  for (const n of nodes) {
    if (n.kind === "rail" && (!n.source_refdes || !nodeById.has(`comp:${n.source_refdes}`))) {
      depth.set(n.id, 0);
    }
  }
  // Iterate until convergence.
  let changed = true; let safety = 0;
  while (changed && safety < 30) {
    changed = false; safety += 1;
    // Components: depth = max(depth of rails it consumes) + 1
    for (const n of nodes) {
      if (n.kind !== "component") continue;
      const incomingPower = edges.filter(e => e.kind === "powers" && e.targetId === n.id);
      const decoupleTargets = edges.filter(e => e.kind === "decouples" && e.sourceId === n.id);
      let d = depth.get(n.id);
      if (incomingPower.length > 0) {
        const maxD = Math.max(...incomingPower.map(e => depth.get(e.sourceId) ?? -Infinity));
        if (maxD !== -Infinity) {
          const nd = maxD + 1;
          if (d == null || d < nd) { depth.set(n.id, nd); changed = true; }
        }
      } else if (decoupleTargets.length > 0 && n.role === "decoupling") {
        // Decoupling caps sit at the depth of the rail they decouple.
        const maxD = Math.max(...decoupleTargets.map(e => depth.get(e.targetId) ?? -Infinity));
        if (maxD !== -Infinity) {
          if (d == null || d < maxD) { depth.set(n.id, maxD); changed = true; }
        }
      }
    }
    // Rails with source: depth = depth(source) + 1
    for (const n of nodes) {
      if (n.kind !== "rail") continue;
      if (!n.source_refdes) continue;
      const sd = depth.get(`comp:${n.source_refdes}`);
      if (sd != null) {
        const nd = sd + 1;
        const d = depth.get(n.id);
        if (d == null || d < nd) { depth.set(n.id, nd); changed = true; }
      }
    }
  }
  // Orphans → depth 0.
  for (const n of nodes) if (!depth.has(n.id)) depth.set(n.id, 0);

  // --- 4. Criticality score (blast radius) per node ------------------
  // Walk "produces" + "powers" forward from every node, count the
  // downstream cascade. Normalize so the max-impact SPOF is 1.0.
  const blastRadius = new Map();
  const forwardAdj = new Map();
  for (const e of edges) {
    if (e.kind !== "powers" && e.kind !== "produces") continue;
    if (!forwardAdj.has(e.sourceId)) forwardAdj.set(e.sourceId, []);
    forwardAdj.get(e.sourceId).push(e.targetId);
  }
  for (const n of nodes) {
    const dead = new Set();
    const stack = [n.id];
    while (stack.length) {
      const c = stack.pop();
      for (const nxt of forwardAdj.get(c) || []) {
        if (!dead.has(nxt)) { dead.add(nxt); stack.push(nxt); }
      }
    }
    blastRadius.set(n.id, dead.size);
  }
  const maxBlast = Math.max(1, ...blastRadius.values());
  const totalNodes = nodes.length || 1;
  for (const n of nodes) {
    const br = blastRadius.get(n.id) || 0;
    n.blastRadius = br;
    n.impactPct = Math.round(1000 * br / totalNodes) / 10;
    n.criticality = br / maxBlast;     // 0..1 relative
  }
  // Flag top-5 SPOFs visually.
  const sortedByBlast = [...nodes].sort((a, b) => b.blastRadius - a.blastRadius);
  const spofCutoff = Math.min(5, sortedByBlast.length);
  for (let i = 0; i < spofCutoff; i++) {
    if (sortedByBlast[i].blastRadius >= 2) sortedByBlast[i].isSpof = true;
  }

  // Totals for stat ratios (displayed/total, so the tech sees how much
  // is filtered vs. what the pack actually contains).
  const totals = {
    components: Object.keys(components).length,
    rails: Object.keys(rails).length,
    sources: Object.values(rails).filter(r => r.source_refdes).length,
    phases: (graph.boot_sequence || []).length,
    signals_available: (graph.typed_edges || []).filter(e =>
      ["enables", "clocks", "resets", "produces_signal",
       "consumes_signal", "feedback_in"].includes(e.kind)
    ).length,
  };

  return { rails, boot, nodes, nodeById, edges, depth,
           bootSource: source, analyzerMeta: analyzed || null,
           maxBlast, totalNodes, totals };
}

/* ---------------------------------------------------------------------- *
 * LAYOUT — phase × voltage grid. Each node sits at (phaseCol, voltageRow)
 * with force-based refinement inside each cell for collision avoidance.
 * ---------------------------------------------------------------------- */

const COL_W = 320;      // per-phase column width
const ROW_H = 170;      // per-voltage-row height
const GRID_TOP = 110;   // y of the first row's center
const GRID_LEFT = 180;  // x of the first column's center
const TIMELINE_H = 148; // reserved at bottom for boot timeline (must match CSS)

// Voltage rows, top→bottom. Signal-only nodes fall into the last row.
const V_ROWS = [
  { id: "vHi",   label: "≥ 12 V",  min: 12,        max: Infinity },
  { id: "v5_11", label: "5–11 V",  min: 5,         max: 11.999   },
  { id: "v3v3",  label: "3V3",     min: 3,         max: 4.999    },
  { id: "v1v8",  label: "1V8–2V5", min: 1.2001,    max: 2.999    },
  { id: "vCore", label: "≤ 1V2",   min: 0.01,      max: 1.2      },
  { id: "vSig",  label: "Signaux", min: null,      max: null     },
];

function voltageRowFor(v) {
  if (v == null) return "vSig";
  for (const r of V_ROWS) {
    if (r.min == null) continue;
    if (v >= r.min && v <= r.max) return r.id;
  }
  return "vSig";
}

function primaryPowerRailLabel(pinsList, rails) {
  // Prefer role=power_in, then any pin touching a non-GND rail.
  for (const p of pinsList || []) {
    if (p.role === "power_in" && p.net_label && rails[p.net_label]) return p.net_label;
  }
  for (const p of pinsList || []) {
    if (p.net_label && rails[p.net_label] && p.net_label !== "GND") return p.net_label;
  }
  return null;
}

function assignGridCoords(model) {
  // For rails: voltageRow is its voltage_nominal bucket.
  // For sources (producing a rail X): voltage of X.
  // For consumers: voltage of their primary input rail.
  // For decoupling caps: voltage of the rail they decouple.
  //
  // Phase assignment: Opus only classifies *active* components (ICs,
  // regulators, connectors). Passives (decoupling caps, series resistors)
  // never "boot" so they have phase==null and would otherwise land in the
  // Pré-boot column with a long flyout arrow across the graph. Fix: we
  // inherit a passive's phase from the rail/IC it's attached to so it
  // sits next to its logical anchor.
  const rails = model.rails || {};
  const railPhase = new Map();
  for (const n of model.nodes) {
    if (n.kind === "rail") railPhase.set(n.label, n.phase);
  }
  const componentPhase = new Map();
  for (const n of model.nodes) {
    if (n.kind === "component") componentPhase.set(n.refdes, n.phase);
  }

  for (const n of model.nodes) {
    if (n.kind === "rail") {
      n.voltageRow = voltageRowFor(n.voltage_nominal);
      continue;
    }
    if (n.role === "source") {
      const prodEdge = (model.edges || []).find(e => e.kind === "produces" && e.sourceId === n.id);
      const prodRail = prodEdge ? rails[prodEdge.netLabel] : null;
      n.voltageRow = voltageRowFor(prodRail?.voltage_nominal);
      // A source IC should sit in the same phase as the rail it produces
      // (so the producer → rail arrow is short and in-cell).
      if (n.phase == null && prodEdge) {
        const inherited = railPhase.get(prodEdge.netLabel);
        if (inherited != null) n.phase = inherited;
      }
      continue;
    }
    if (n.role === "decoupling") {
      const decEdge = (model.edges || []).find(e => e.kind === "decouples" && e.sourceId === n.id);
      const decRail = decEdge ? rails[decEdge.netLabel] : null;
      n.voltageRow = voltageRowFor(decRail?.voltage_nominal);
      // Decoupling caps live wherever their rail lives — stabilises the
      // rail's local supply, it has no "boot phase" of its own.
      if (decEdge) {
        const inherited = railPhase.get(decEdge.netLabel);
        if (inherited != null) n.phase = inherited;
      }
      continue;
    }
    // consumer — look at its primary power rail
    const pinsList = Array.isArray(n.pinsAll) ? n.pinsAll : [];
    let railLabel = primaryPowerRailLabel(pinsList, rails);
    // Fallback: if the component has no identified power pin but is
    // listed as a consumer of one or more rails (Opus-derived), pick the
    // first rail it belongs to from the rails map. Keeps the node out of
    // the orphan strip even when its pin roles are underspecified.
    if (!railLabel) {
      for (const [label, r] of Object.entries(rails)) {
        if ((r.consumers || []).includes(n.refdes)) { railLabel = label; break; }
      }
    }
    n.voltageRow = voltageRowFor(railLabel ? rails[railLabel]?.voltage_nominal : null);
    n.rail_primary = railLabel;  // used by the power-tree layout anchor
    // Consumers without an explicit phase inherit from their primary rail.
    if (n.phase == null && railLabel) {
      const inherited = railPhase.get(railLabel);
      if (inherited != null) n.phase = inherited;
    }
  }
}

function computeGridLayout(model) {
  assignGridCoords(model);

  // Discover which phases are actually present. Phases come from
  // model.boot (Opus-analyzed when available) — each node has .phase set
  // during buildModel. Nodes with .phase==null (not assigned to any
  // phase) land in a synthetic "pre-boot" column at index -1.
  const phasesPresent = Array.from(new Set(
    model.nodes.map(n => n.phase).filter(p => p != null)
  )).sort((a, z) => a - z);
  if (model.nodes.some(n => n.phase == null)) phasesPresent.unshift(null);

  const phaseColIndex = new Map();
  phasesPresent.forEach((p, i) => phaseColIndex.set(p, i));

  const rowIndex = new Map();
  V_ROWS.forEach((r, i) => rowIndex.set(r.id, i));

  const colX = (phase) => GRID_LEFT + (phaseColIndex.get(phase) ?? 0) * COL_W;
  const rowY = (vr) => GRID_TOP + (rowIndex.get(vr) ?? V_ROWS.length - 1) * ROW_H;

  // Assign target positions.
  for (const n of model.nodes) {
    const cx = colX(n.phase ?? null);
    const cy = rowY(n.voltageRow);
    // Offset by role: rails centered; sources slightly left; consumers spread right.
    let ox = 0;
    if (n.kind === "component") {
      if (n.role === "source") ox = -COL_W * 0.22;
      else if (n.role === "consumer") ox = COL_W * 0.18;
      else if (n.role === "decoupling") ox = COL_W * 0.32;
    }
    n._tx = cx + ox;
    n._ty = cy;
    n.x = n._tx + (Math.random() - 0.5) * 30;
    n.y = n._ty + (Math.random() - 0.5) * 30;
  }

  // Force refinement — strong X/Y anchors (keep the grid structure) +
  // collide to avoid overlap within a cell.
  const simEdges = model.edges.map(e => ({
    source: e.sourceId, target: e.targetId, kind: e.kind,
  }));
  const radius = (d) => {
    const w = d.width || 40, h = d.height || 40;
    return Math.max(w, h) / 2 + 7;
  };
  const sim = d3.forceSimulation(model.nodes)
    .force("x", d3.forceX(d => d._tx).strength(0.6))
    .force("y", d3.forceY(d => d._ty).strength(0.5))
    .force("collide", d3.forceCollide(radius).strength(1).iterations(3))
    .force("link", d3.forceLink(simEdges).id(d => d.id)
      .distance(d => d.kind === "decouples" ? 30 : 60)
      .strength(d => d.kind === "decouples" ? 0.35 : 0.08))
    .stop();
  for (let i = 0; i < 350; i++) sim.tick();

  const TOTAL_W = GRID_LEFT + phasesPresent.length * COL_W + 60;
  const TOTAL_H = GRID_TOP + V_ROWS.length * ROW_H + 40;
  const xs = model.nodes.map(n => n.x);
  const ys = model.nodes.map(n => n.y);
  model.bounds = {
    minX: Math.min(...xs, 40) - 80,
    maxX: Math.max(...xs, TOTAL_W) + 80,
    minY: Math.min(...ys, 40) - 80,
    maxY: Math.max(...ys, TOTAL_H) + 80,
  };
  model.phasesPresent = phasesPresent;
  model.phaseColIndex = phaseColIndex;
  model.colX = colX;
  model.rowY = rowY;
  model.rows = V_ROWS;
  model.layoutMode = "grid";
}

/* ---------------------------------------------------------------------- *
 * POWER-TREE LAYOUT — 1 axis (voltage). Each rail gets its own horizontal
 * "bus" with its regulator to the left, decouples as small dots, and
 * consumer chips laid out to the right. No 2D grid, no edge spaghetti.
 * ---------------------------------------------------------------------- */

const PT_ROW_H = 56;
const PT_TOP = 80;
const PT_RAIL_X = 140;
const PT_SOURCE_X = 280;
const PT_CONSUMER_START_X = 470;
const PT_CONSUMER_STEP_X = 70;
const PT_CONSUMERS_PER_LINE = 14;
const PT_DECOUP_Y_OFFSET = 20;
const PT_DECOUP_STEP_X = 16;

function computePowertreeLayout(model) {
  assignGridCoords(model); // keeps voltageRow for consistency + fallback

  const rails = model.rails || {};

  // 1) Stack rails vertically, ordered by voltage descending.
  const railNodes = model.nodes.filter(n => n.kind === "rail");
  railNodes.sort((a, z) => {
    const va = a.voltage_nominal ?? -1;
    const vz = z.voltage_nominal ?? -1;
    if (vz !== va) return vz - va;
    return a.label.localeCompare(z.label);
  });
  railNodes.forEach((rail, i) => {
    rail._tx = PT_RAIL_X;
    rail._ty = PT_TOP + i * PT_ROW_H;
  });

  // 2) Anchor each component to one rail (primary power relation).
  const producesByRefdes = new Map();
  for (const [label, r] of Object.entries(rails)) {
    if (r.source_refdes) {
      if (!producesByRefdes.has(r.source_refdes)) producesByRefdes.set(r.source_refdes, []);
      producesByRefdes.get(r.source_refdes).push(label);
    }
  }

  const byRailRole = new Map();
  const anchorKey = (rail, role) => `${rail || "__orphan__"}|${role}`;

  for (const n of model.nodes) {
    if (n.kind !== "component") continue;
    let anchor = null;
    if (n.role === "source") {
      anchor = producesByRefdes.get(n.refdes)?.[0] ?? null;
    } else if (n.role === "decoupling") {
      const e = (model.edges || []).find(x => x.kind === "decouples" && x.sourceId === n.id);
      anchor = e?.netLabel ?? null;
    } else if (n.role === "consumer") {
      anchor = n.rail_primary ?? null;
    }
    n._anchorRail = anchor;
    const key = anchorKey(anchor, n.role);
    if (!byRailRole.has(key)) byRailRole.set(key, []);
    byRailRole.get(key).push(n);
  }
  for (const [, arr] of byRailRole) arr.sort((a, z) => a.refdes.localeCompare(z.refdes));

  // 3) Place components on their anchor rail's row.
  for (const rail of railNodes) {
    const y = rail._ty;

    // Source (regulator) just left of the rail hexagon. We compact to
    // 50×36 by default so multiple sources fit side-by-side, BUT when
    // "Toutes pins" is on we respect the auto-computed size (built by
    // buildModel from the pin count) so every pin has the room to be
    // drawn with a leader line.
    const sources = byRailRole.get(anchorKey(rail.label, "source")) || [];
    sources.forEach((s, i) => {
      s._tx = PT_SOURCE_X - i * 90; // stack leftward if multiple sources
      s._ty = y;
      if (!STATE.showAllPins) { s.width = 50; s.height = 36; }
    });

    // Decoupling caps — small dots just below the rail line.
    const decs = byRailRole.get(anchorKey(rail.label, "decoupling")) || [];
    decs.forEach((d, i) => {
      d._tx = PT_CONSUMER_START_X + i * PT_DECOUP_STEP_X;
      d._ty = y + PT_DECOUP_Y_OFFSET;
      d.width = 10; d.height = 10;
    });

    // Consumers — chips to the right, wrap onto extra rows if many.
    // Same logic: compact by default, full size when all pins are shown.
    const consumers = byRailRole.get(anchorKey(rail.label, "consumer")) || [];
    consumers.forEach((c, i) => {
      const col = i % PT_CONSUMERS_PER_LINE;
      const row = Math.floor(i / PT_CONSUMERS_PER_LINE);
      c._tx = PT_CONSUMER_START_X + col * PT_CONSUMER_STEP_X;
      c._ty = y - 14 + row * 26;
      if (!STATE.showAllPins) { c.width = 56; c.height = 28; }
    });
  }

  // 4) Orphans — components without a power-rail anchor. With the "Passifs
  // signal" toggle on, this is where the ~380 routing passives land. Dense
  // grid (20 per row, 28px vertical) so we can show a LOT without
  // exploding the canvas.
  const orphans = model.nodes.filter(n => n.kind === "component" && n._tx == null);
  // Sort by refdes letter then number so R1...R200, then C1...C200, etc.
  orphans.sort((a, z) => (a.refdes || "").localeCompare(z.refdes || "", undefined, { numeric: true }));
  const orphanTop = PT_TOP + railNodes.length * PT_ROW_H + 60;
  const perRow = 22;
  orphans.forEach((n, i) => {
    n._tx = 160 + (i % perRow) * 52;
    n._ty = orphanTop + Math.floor(i / perRow) * 30;
    // Slim them down — they're tiny signal passives, make them unobtrusive.
    n.width = Math.min(n.width || 20, 22);
    n.height = Math.min(n.height || 20, 16);
  });
  model._orphanStripY = orphans.length ? orphanTop - 30 : null;
  model._orphanCount = orphans.length;

  // 5) Commit positions (no force simulation — the grid IS the layout).
  for (const n of model.nodes) {
    n.x = n._tx ?? PT_RAIL_X;
    n.y = n._ty ?? PT_TOP;
  }

  // 6) Bounds.
  const xs = model.nodes.map(n => n.x);
  const ys = model.nodes.map(n => n.y);
  model.bounds = {
    minX: Math.min(...xs) - 80,
    maxX: Math.max(...xs) + 180,
    minY: Math.min(...ys) - 100,
    maxY: Math.max(...ys) + 120,
  };
  model.layoutMode = "powertree";
  model.railOrder = railNodes.map(r => r.id);
}

function renderPowertreeHeads(model) {
  const g = d3.select("#schBucketHeads");
  g.selectAll("*").remove();

  const railNodes = model.nodes.filter(n => n.kind === "rail");
  const maxX = model.bounds.maxX - 40;

  // 1) Voltage-class tinted bands — group rails of the same class.
  const byBand = new Map();
  for (const r of railNodes) {
    const band = voltageRowFor(r.voltage_nominal);
    if (!byBand.has(band)) byBand.set(band, []);
    byBand.get(band).push(r);
  }
  for (const [bandId, group] of byBand) {
    if (!group.length) continue;
    const idx = V_ROWS.findIndex(v => v.id === bandId);
    const yTop = Math.min(...group.map(r => r.y)) - PT_ROW_H / 2 + 4;
    const yBot = Math.max(...group.map(r => r.y)) + PT_ROW_H / 2 - 4;
    g.append("rect")
      .attr("class", `sch-vrow-band vrow-${idx % 4}`)
      .attr("x", 40).attr("y", yTop)
      .attr("width", maxX - 20).attr("height", yBot - yTop).attr("rx", 10);
    // Label on the left
    g.append("text")
      .attr("class", "sch-pt-band-label")
      .attr("x", 70).attr("y", (yTop + yBot) / 2 + 4)
      .text(V_ROWS[idx]?.label ?? bandId);
  }

  // 2) Horizontal dashed bus line across each rail's row.
  railNodes.forEach(rail => {
    g.append("line")
      .attr("class", "sch-pt-busline")
      .attr("x1", PT_RAIL_X + 40).attr("x2", maxX - 20)
      .attr("y1", rail.y).attr("y2", rail.y);
  });

  // 3) If there's an orphan strip (signal-only passives activated via the
  // "Passifs signal" toggle), separate it visually with a divider + label.
  if (model._orphanStripY != null && model._orphanCount > 0) {
    g.append("line")
      .attr("class", "sch-pt-orphan-divider")
      .attr("x1", 60).attr("x2", maxX - 20)
      .attr("y1", model._orphanStripY).attr("y2", model._orphanStripY);
    g.append("text")
      .attr("class", "sch-pt-orphan-label")
      .attr("x", 80).attr("y", model._orphanStripY - 8)
      .text(`SIGNAUX / AUTRES — ${model._orphanCount} composants non attachés à un rail power`);
  }
}

/* ---------------------------------------------------------------------- *
 * RAIL-FOCUS LAYOUT — show exactly ONE rail + its source + upstream feed +
 * decoupling caps + direct consumers. Everything else is hidden. Zero long
 * edges, zero overlap, scales to any rail count because we never render
 * more than one rail's neighborhood at a time.
 * ---------------------------------------------------------------------- */

const RF_UPSTREAM_X = 160;
const RF_SOURCE_X = 400;
const RF_RAIL_X = 640;
const RF_CONSUMERS_X = 820;
const RF_CENTER_Y = 260;
const RF_CONSUMER_COL_W = 90;
const RF_CONSUMER_ROW_H = 48;
const RF_CONSUMERS_PER_COL = 9;
const RF_DECOUP_STEP_X = 22;

function computeRailFocusLayout(model, railId) {
  // Start hidden, then progressively reveal the rail's neighborhood.
  for (const n of model.nodes) n._visible = false;
  model.layoutMode = "railfocus";
  model._rfRailId = null;
  model._rfUpstreamId = null;
  model._rfConsumerCount = 0;
  model._rfDecouplingCount = 0;

  const rail = railId ? model.nodeById.get(railId) : null;
  if (!rail) {
    model.bounds = { minX: 0, minY: 0, maxX: 1200, maxY: 560 };
    return;
  }

  rail._visible = true;
  rail._tx = RF_RAIL_X; rail._ty = RF_CENTER_Y;
  rail.width = 140; rail.height = 54;
  model._rfRailId = rail.id;

  // Source IC — the regulator that produces this rail.
  let source = null;
  if (rail.source_refdes) {
    source = model.nodeById.get(`comp:${rail.source_refdes}`);
    if (source) {
      source._visible = true;
      source._tx = RF_SOURCE_X;
      source._ty = RF_CENTER_Y;
      source.width = 92;
      source.height = Math.max(72, source.height || 48);
    }
  }

  // Upstream rail — the rail that feeds the source's input pin.
  let upstream = null;
  if (source) {
    const upE = model.edges.find(e => e.kind === "powers" && e.targetId === source.id);
    if (upE) {
      const cand = model.nodeById.get(upE.sourceId);
      if (cand && cand.id !== rail.id && cand.kind === "rail") {
        upstream = cand;
        upstream._visible = true;
        upstream._tx = RF_UPSTREAM_X;
        upstream._ty = RF_CENTER_Y;
        upstream.width = 110;
        upstream.height = 44;
        model._rfUpstreamId = upstream.id;
      }
    }
  }

  // Consumers — grid to the right of the rail, vertically centered on it.
  const consumers = model.edges
    .filter(e => e.kind === "powers" && e.sourceId === rail.id)
    .map(e => model.nodeById.get(e.targetId))
    .filter(Boolean);
  consumers.sort((a, z) =>
    (a.refdes || "").localeCompare(z.refdes || "", undefined, { numeric: true })
  );
  const nC = consumers.length;
  consumers.forEach((c, i) => {
    c._visible = true;
    const col = Math.floor(i / RF_CONSUMERS_PER_COL);
    const row = i % RF_CONSUMERS_PER_COL;
    const colCount = Math.min(RF_CONSUMERS_PER_COL, nC - col * RF_CONSUMERS_PER_COL);
    const colHeight = (colCount - 1) * RF_CONSUMER_ROW_H;
    c._tx = RF_CONSUMERS_X + col * RF_CONSUMER_COL_W;
    c._ty = RF_CENTER_Y - colHeight / 2 + row * RF_CONSUMER_ROW_H;
    c.width = 64;
    c.height = 34;
    // In this mode the detailed pins aren't useful on consumers — keep the
    // inspector for that. Clean rect + refdes is enough here.
    c.showPins = false;
  });
  model._rfConsumerCount = nC;

  // Decoupling caps — small, centered under the rail on a short strip.
  const decouplings = model.edges
    .filter(e => e.kind === "decouples" && e.targetId === rail.id)
    .map(e => model.nodeById.get(e.sourceId))
    .filter(Boolean);
  decouplings.sort((a, z) =>
    (a.refdes || "").localeCompare(z.refdes || "", undefined, { numeric: true })
  );
  const decoupY = RF_CENTER_Y + 70;
  decouplings.forEach((d, i) => {
    d._visible = true;
    d._tx = RF_RAIL_X + (i - (decouplings.length - 1) / 2) * RF_DECOUP_STEP_X;
    d._ty = decoupY;
    d.width = 12;
    d.height = 14;
  });
  model._rfDecouplingCount = decouplings.length;

  // Commit positions for visible nodes; push the rest way off-canvas so the
  // zoom/fit math doesn't see them.
  for (const n of model.nodes) {
    if (n._visible) { n.x = n._tx; n.y = n._ty; }
    else { n.x = -1e5; n.y = -1e5; }
  }

  const visible = model.nodes.filter(n => n._visible);
  if (visible.length === 0) {
    model.bounds = { minX: 0, minY: 0, maxX: 1200, maxY: 560 };
  } else {
    const xs = visible.map(n => n.x);
    const ys = visible.map(n => n.y);
    model.bounds = {
      minX: Math.min(...xs) - 140,
      minY: Math.min(...ys) - 120,
      maxX: Math.max(...xs) + 140,
      maxY: Math.max(...ys) + 120,
    };
  }
}

function renderRailFocusHeads(model) {
  const g = d3.select("#schBucketHeads");
  g.selectAll("*").remove();

  if (!model._rfRailId) {
    g.append("text")
      .attr("class", "sch-rf-empty")
      .attr("x", 600).attr("y", 260)
      .text("← Sélectionne un rail dans la liste");
    g.append("text")
      .attr("class", "sch-rf-empty-hint")
      .attr("x", 600).attr("y", 288)
      .text("Vue propre — un rail à la fois, zéro spaghetti.");
    return;
  }

  const rail = model.nodeById.get(model._rfRailId);
  const hasUpstream = Boolean(model._rfUpstreamId);
  const hasSource = Boolean(rail.source_refdes);
  const nC = model._rfConsumerCount;
  const railY = rail.y;
  const zoneTop = railY - 210;
  const zoneBot = railY + 210;

  const zones = [];
  if (hasUpstream) zones.push({ x: RF_UPSTREAM_X - 90, w: 180, label: "AMONT" });
  if (hasSource)   zones.push({ x: RF_SOURCE_X - 90, w: 180, label: "SOURCE" });
  zones.push({ x: RF_RAIL_X - 80, w: 160, label: "RAIL" });
  if (nC > 0) {
    const nCols = Math.ceil(nC / RF_CONSUMERS_PER_COL);
    zones.push({
      x: RF_CONSUMERS_X - 40,
      w: 80 + nCols * RF_CONSUMER_COL_W,
      label: "CONSUMERS",
    });
  }
  for (const z of zones) {
    g.append("rect")
      .attr("class", "sch-rf-zoneband")
      .attr("x", z.x).attr("y", zoneTop)
      .attr("width", z.w).attr("height", zoneBot - zoneTop)
      .attr("rx", 8);
    g.append("text")
      .attr("class", "sch-rf-zonelabel")
      .attr("x", z.x + z.w / 2).attr("y", zoneTop - 8)
      .attr("text-anchor", "middle")
      .text(z.label);
  }

  // Horizontal bus from the rail towards the consumer zone.
  if (nC > 0) {
    g.append("line")
      .attr("class", "sch-rf-busline")
      .attr("x1", rail.x + 70).attr("y1", railY)
      .attr("x2", RF_CONSUMERS_X - 10).attr("y2", railY);
  }

  // "External supply" note when the rail has no producer on this board.
  if (!hasSource) {
    g.append("text")
      .attr("class", "sch-rf-upstream-note")
      .attr("x", RF_SOURCE_X).attr("y", railY + 4)
      .attr("text-anchor", "middle")
      .text("Alim externe");
  }
}

function renderRailBar(model) {
  const listEl = el("schRailBarList");
  const countEl = el("schRailBarCount");
  if (!listEl) return;
  listEl.innerHTML = "";

  const rails = model.nodes.filter(n => n.kind === "rail");
  if (countEl) countEl.textContent = String(rails.length);

  if (rails.length === 0) {
    listEl.innerHTML = `<div class="muted" style="padding:20px 14px;font-size:11px;text-align:center">Aucun rail dans ce pack.</div>`;
    return;
  }

  // Group by voltage class, in V_ROWS order (high → low tension).
  const byGroup = new Map();
  for (const r of rails) {
    const gid = voltageRowFor(r.voltage_nominal);
    if (!byGroup.has(gid)) byGroup.set(gid, []);
    byGroup.get(gid).push(r);
  }
  for (const vrow of V_ROWS) {
    const group = byGroup.get(vrow.id);
    if (!group || group.length === 0) continue;
    group.sort((a, z) => {
      const va = a.voltage_nominal ?? -1;
      const vz = z.voltage_nominal ?? -1;
      if (vz !== va) return vz - va;
      return (a.label || "").localeCompare(z.label || "");
    });
    const header = document.createElement("div");
    header.className = "sch-rail-group";
    header.textContent = vrow.label;
    listEl.appendChild(header);
    for (const rail of group) {
      const item = document.createElement("div");
      item.className = "sch-rail-item";
      if (rail.isSpof) item.classList.add("spof");
      if (rail.id === STATE.selectedRailId) item.classList.add("active");
      item.dataset.railId = rail.id;

      const consumerCount = (rail.consumers || []).length;
      const voltageLbl = rail.voltage_nominal != null
        ? `${rail.voltage_nominal} V`
        : "—";
      const sourceLbl = rail.source_refdes
        ? `<span class="sch-rail-source">${escHtml(rail.source_refdes)}</span>`
        : `<span class="sch-rail-source external">externe</span>`;
      const phaseBadge = rail.phase != null
        ? `<span class="sch-rail-phase">Φ${rail.phase}</span>`
        : "";
      const spofBadge = rail.isSpof
        ? `<span class="sch-rail-spof">⚠ ${rail.impactPct}%</span>`
        : "";

      item.innerHTML = `
        <div class="sch-rail-name">${escHtml(rail.label)}</div>
        <div class="sch-rail-voltage">${voltageLbl}</div>
        <div class="sch-rail-meta">
          ${sourceLbl}
          <span class="sch-rail-consumers">→ ${consumerCount}</span>
          ${phaseBadge}
          ${spofBadge}
        </div>
      `;
      item.addEventListener("click", () => setSelectedRail(rail.id));
      listEl.appendChild(item);
    }
  }
}

function setSelectedRail(railId) {
  STATE.selectedRailId = railId || null;
  try { localStorage.setItem("schSelectedRail", railId || ""); } catch (_) {}
  if (!STATE.model || STATE.layoutMode !== "railfocus") return;
  computeRailFocusLayout(STATE.model, STATE.selectedRailId);
  renderRailFocusHeads(STATE.model);
  renderNodes(STATE.model);
  renderEdges(STATE.model);
  document.querySelectorAll("#schRailBarList .sch-rail-item").forEach(it => {
    it.classList.toggle("active", it.dataset.railId === STATE.selectedRailId);
  });
  if (STATE.zoom) fitToBounds(STATE.model);
  if (STATE.selectedRailId) {
    const n = STATE.model.nodeById.get(STATE.selectedRailId);
    if (n) { STATE.selectedId = n.id; updateInspector(n); }
  } else {
    clearFocus();
  }
}

// External-focus bridge — the boardview minimap dispatches this event when
// the user clicks a rail in the mini-graph. If this module is already
// initialized (model built), we switch to rail-focus in place; otherwise
// the paired localStorage write gets picked up on next loadSchematic().
window.addEventListener("schematic:focus-rail", (ev) => {
  const railId = ev.detail?.railId;
  if (!railId) return;
  if (STATE.layoutMode !== "railfocus") {
    STATE.layoutMode = "railfocus";
    try { localStorage.setItem("schLayoutMode", "railfocus"); } catch (_) {}
    if (STATE.graph) fullRender(STATE.graph);
  }
  setSelectedRail(railId);
});

/* ---------------------------------------------------------------------- *
 * KILL-SWITCH — BFS forward through produces + powers edges              *
 * ---------------------------------------------------------------------- */

function computeCascade(model, startId) {
  const dead = new Set([startId]);
  const queue = [startId];
  while (queue.length) {
    const id = queue.shift();
    for (const e of model.edges) {
      if (dead.has(e.targetId)) continue;
      // When a rail dies, its consumers die. When a source dies, its produced rail dies.
      if ((e.kind === "powers" || e.kind === "produces") && e.sourceId === id) {
        dead.add(e.targetId); queue.push(e.targetId);
      }
    }
  }
  return dead;
}

function computeUpstream(model, startId) {
  // Nodes that this one depends on (the chain feeding it).
  const feeds = new Set([startId]);
  const queue = [startId];
  while (queue.length) {
    const id = queue.shift();
    for (const e of model.edges) {
      if (feeds.has(e.sourceId)) continue;
      if ((e.kind === "powers" || e.kind === "produces") && e.targetId === id) {
        feeds.add(e.sourceId); queue.push(e.sourceId);
      }
    }
  }
  return feeds;
}

/* ---------------------------------------------------------------------- *
 * RENDER                                                                 *
 * ---------------------------------------------------------------------- */

/* ---------------------------------------------------------------------- *
 * SCHEMATIC SYMBOLS — draw the standard electronic symbol per component   *
 * type instead of a generic rect. Every renderer attaches elements to the *
 * provided `sel` group; elements are centered on (0,0) with pins extending*
 * to ±w/2 so edges can anchor on the box edge cleanly.                    *
 * ---------------------------------------------------------------------- */

function drawResistor(sel, w, h) {
  const bw = w * 0.72, bh = h * 0.55;
  sel.append("rect").attr("class", "sch-sym-body sch-sym-resistor")
    .attr("x", -bw / 2).attr("y", -bh / 2).attr("width", bw).attr("height", bh).attr("rx", 1);
  sel.append("line").attr("class", "sch-sym-pin")
    .attr("x1", -w / 2).attr("y1", 0).attr("x2", -bw / 2).attr("y2", 0);
  sel.append("line").attr("class", "sch-sym-pin")
    .attr("x1", bw / 2).attr("y1", 0).attr("x2", w / 2).attr("y2", 0);
}

function drawCapacitor(sel, w, h) {
  // Two parallel vertical plates with pins extending left/right.
  const gap = Math.max(2, Math.min(4, w * 0.1));
  const plateH = h * 0.85;
  sel.append("line").attr("class", "sch-sym-body sch-sym-cap")
    .attr("x1", -gap / 2).attr("y1", -plateH / 2).attr("x2", -gap / 2).attr("y2", plateH / 2);
  sel.append("line").attr("class", "sch-sym-body sch-sym-cap")
    .attr("x1", gap / 2).attr("y1", -plateH / 2).attr("x2", gap / 2).attr("y2", plateH / 2);
  sel.append("line").attr("class", "sch-sym-pin")
    .attr("x1", -w / 2).attr("y1", 0).attr("x2", -gap / 2).attr("y2", 0);
  sel.append("line").attr("class", "sch-sym-pin")
    .attr("x1", gap / 2).attr("y1", 0).attr("x2", w / 2).attr("y2", 0);
}

function drawInductor(sel, w, h) {
  // Three arches — the classic coil symbol.
  const arches = 3;
  const aw = (w * 0.8) / arches;
  const startX = -w * 0.4;
  let path = "";
  for (let i = 0; i < arches; i++) {
    const cx = startX + aw * i + aw / 2;
    path += `M${cx - aw / 2} 0 A ${aw / 2} ${aw / 2} 0 0 1 ${cx + aw / 2} 0 `;
  }
  sel.append("path").attr("class", "sch-sym-body sch-sym-inductor").attr("d", path);
  sel.append("line").attr("class", "sch-sym-pin")
    .attr("x1", -w / 2).attr("y1", 0).attr("x2", startX).attr("y2", 0);
  sel.append("line").attr("class", "sch-sym-pin")
    .attr("x1", startX + aw * arches).attr("y1", 0).attr("x2", w / 2).attr("y2", 0);
}

function drawFerrite(sel, w, h) {
  // Rounded rectangle (bead) — distinct from resistor by radius.
  const bw = w * 0.72, bh = h * 0.65;
  sel.append("rect").attr("class", "sch-sym-body sch-sym-ferrite")
    .attr("x", -bw / 2).attr("y", -bh / 2).attr("width", bw).attr("height", bh).attr("rx", bh / 2);
  sel.append("line").attr("class", "sch-sym-pin")
    .attr("x1", -w / 2).attr("y1", 0).attr("x2", -bw / 2).attr("y2", 0);
  sel.append("line").attr("class", "sch-sym-pin")
    .attr("x1", bw / 2).attr("y1", 0).attr("x2", w / 2).attr("y2", 0);
}

function drawDiode(sel, w, h) {
  // Triangle pointing right + vertical bar (cathode).
  const s = Math.min(w * 0.35, h * 0.45);
  sel.append("path").attr("class", "sch-sym-body sch-sym-diode")
    .attr("d", `M${-s} ${-s} L${s} 0 L${-s} ${s} Z`);
  sel.append("line").attr("class", "sch-sym-body sch-sym-diode-bar")
    .attr("x1", s).attr("y1", -s).attr("x2", s).attr("y2", s);
  sel.append("line").attr("class", "sch-sym-pin")
    .attr("x1", -w / 2).attr("y1", 0).attr("x2", -s).attr("y2", 0);
  sel.append("line").attr("class", "sch-sym-pin")
    .attr("x1", s).attr("y1", 0).attr("x2", w / 2).attr("y2", 0);
}

function drawLED(sel, w, h) {
  // Diode + two small outward arrows for "light emitted".
  drawDiode(sel, w, h);
  const s = Math.min(w * 0.35, h * 0.45);
  sel.append("path").attr("class", "sch-sym-body sch-sym-led-ray")
    .attr("d", `M${-s * 0.3} ${-s - 1} l2 -3 M${-s * 0.6} ${-s + 1} l1.5 -2.5`);
  sel.append("path").attr("class", "sch-sym-body sch-sym-led-ray")
    .attr("d", `M${s * 0.2} ${-s - 1} l2 -3 M${-s * 0.1} ${-s + 1} l1.5 -2.5`);
}

function drawFuse(sel, w, h) {
  // Elongated pill with an "F" glyph and pins.
  const bw = w * 0.78, bh = h * 0.55;
  sel.append("rect").attr("class", "sch-sym-body sch-sym-fuse")
    .attr("x", -bw / 2).attr("y", -bh / 2).attr("width", bw).attr("height", bh).attr("rx", bh / 2);
  sel.append("line").attr("class", "sch-sym-pin")
    .attr("x1", -w / 2).attr("y1", 0).attr("x2", -bw / 2).attr("y2", 0);
  sel.append("line").attr("class", "sch-sym-pin")
    .attr("x1", bw / 2).attr("y1", 0).attr("x2", w / 2).attr("y2", 0);
}

function drawTransistor(sel, w, h) {
  // Circle with base line + emitter/collector, NPN convention.
  const r = Math.min(w, h) * 0.38;
  sel.append("circle").attr("class", "sch-sym-body sch-sym-transistor")
    .attr("cx", 0).attr("cy", 0).attr("r", r);
  // base (horizontal line from left to circle)
  sel.append("line").attr("class", "sch-sym-pin")
    .attr("x1", -w / 2).attr("y1", 0).attr("x2", -r * 0.4).attr("y2", 0);
  sel.append("line").attr("class", "sch-sym-body")
    .attr("x1", -r * 0.4).attr("y1", -r * 0.6).attr("x2", -r * 0.4).attr("y2", r * 0.6);
  // emitter (bottom right diagonal) with arrow
  sel.append("line").attr("class", "sch-sym-body")
    .attr("x1", -r * 0.4).attr("y1", r * 0.3).attr("x2", r * 0.55).attr("y2", r * 0.85);
  // collector (top right diagonal)
  sel.append("line").attr("class", "sch-sym-body")
    .attr("x1", -r * 0.4).attr("y1", -r * 0.3).attr("x2", r * 0.55).attr("y2", -r * 0.85);
  // pin stubs out of the circle
  sel.append("line").attr("class", "sch-sym-pin")
    .attr("x1", r * 0.55).attr("y1", -r * 0.85).attr("x2", w / 2).attr("y2", -h / 2);
  sel.append("line").attr("class", "sch-sym-pin")
    .attr("x1", r * 0.55).attr("y1", r * 0.85).attr("x2", w / 2).attr("y2", h / 2);
}

function drawCrystal(sel, w, h) {
  // Rectangle with two small plate lines — the XTAL symbol.
  const bw = w * 0.4, bh = h * 0.65;
  sel.append("rect").attr("class", "sch-sym-body sch-sym-crystal")
    .attr("x", -bw / 2).attr("y", -bh / 2).attr("width", bw).attr("height", bh).attr("rx", 1);
  sel.append("line").attr("class", "sch-sym-body")
    .attr("x1", -bw / 2 - 3).attr("y1", -bh / 2).attr("x2", -bw / 2 - 3).attr("y2", bh / 2);
  sel.append("line").attr("class", "sch-sym-body")
    .attr("x1", bw / 2 + 3).attr("y1", -bh / 2).attr("x2", bw / 2 + 3).attr("y2", bh / 2);
  sel.append("line").attr("class", "sch-sym-pin")
    .attr("x1", -w / 2).attr("y1", 0).attr("x2", -bw / 2 - 3).attr("y2", 0);
  sel.append("line").attr("class", "sch-sym-pin")
    .attr("x1", bw / 2 + 3).attr("y1", 0).attr("x2", w / 2).attr("y2", 0);
}

function drawConnector(sel, w, h) {
  // Trapezoid with teeth on one side suggesting a connector.
  const s = w * 0.45;
  sel.append("path").attr("class", "sch-sym-body sch-sym-connector")
    .attr("d", `M${-s} ${-h * 0.45} L${s} ${-h * 0.3} L${s} ${h * 0.3} L${-s} ${h * 0.45} Z`);
  // 3 pin stubs
  for (let i = -1; i <= 1; i++) {
    sel.append("line").attr("class", "sch-sym-pin")
      .attr("x1", s).attr("y1", i * h * 0.18).attr("x2", w / 2).attr("y2", i * h * 0.18);
  }
}

// Dispatch — returns true if a schematic symbol was drawn (so the caller
// knows to skip the fallback generic shape). Small components below
// MIN_SYMBOL_SIZE fall back to a colored dot so the viz stays readable
// at low zoom.
const MIN_SYMBOL_SIZE = 14;
function drawSchematicSymbol(sel, node) {
  if (node.kind !== "component") return false;
  const w = node.width || 20, h = node.height || 20;
  if (Math.min(w, h) < MIN_SYMBOL_SIZE) return false;
  switch (node.type) {
    case "resistor":   drawResistor(sel, w, h); return true;
    case "capacitor":  drawCapacitor(sel, w, h); return true;
    case "inductor":   drawInductor(sel, w, h); return true;
    case "ferrite":    drawFerrite(sel, w, h); return true;
    case "diode":      drawDiode(sel, w, h); return true;
    case "led":        drawLED(sel, w, h); return true;
    case "fuse":       drawFuse(sel, w, h); return true;
    case "transistor": drawTransistor(sel, w, h); return true;
    case "crystal":
    case "oscillator": drawCrystal(sel, w, h); return true;
    case "connector":  drawConnector(sel, w, h); return true;
    // ic / module / other → keep the generic pinned rectangle (handled
    // by the caller's existing shape switch).
    default: return false;
  }
}

function hexPoints(r) {
  const pts = [];
  for (let i = 0; i < 6; i++) {
    const a = (Math.PI / 3) * i + Math.PI / 6;
    pts.push([r * Math.cos(a), r * Math.sin(a) * 0.7].join(","));
  }
  return pts.join(" ");
}

function pinAnchor(node, pin) {
  const sides = node.pins?.sides;
  if (!sides) return [0, 0];
  for (const side of ["left", "right", "top", "bottom"]) {
    const idx = sides[side].indexOf(pin);
    if (idx < 0) continue;
    const count = sides[side].length;
    const w = node.width, h = node.height, pad = 8;
    if (side === "left") return [-w / 2 - 5, -h / 2 + pad + ((h - 2 * pad) / (count + 1)) * (idx + 1)];
    if (side === "right") return [w / 2 + 5, -h / 2 + pad + ((h - 2 * pad) / (count + 1)) * (idx + 1)];
    if (side === "top") return [-w / 2 + pad + ((w - 2 * pad) / (count + 1)) * (idx + 1), -h / 2 - 5];
    if (side === "bottom") return [-w / 2 + pad + ((w - 2 * pad) / (count + 1)) * (idx + 1), h / 2 + 5];
  }
  return [0, 0];
}

function edgeAnchors(e, model) {
  const s = model.nodeById.get(e.sourceId);
  const t = model.nodeById.get(e.targetId);
  if (!s || !t) return null;
  let sx = s.x, sy = s.y, tx = t.x, ty = t.y;

  const isCleanLayout = model.layoutMode === "powertree" || model.layoutMode === "railfocus";
  // In power-tree / rail-focus modes, skip fine pin-level anchoring (nodes
  // are small, layout is already clean) — anchor on the box edge facing the
  // other endpoint so the line is short and unambiguous.
  if (isCleanLayout) {
    if (s.kind === "component") {
      const w = s.width || 40;
      sx = s.x + (t.x > s.x ? w / 2 : -w / 2);
      sy = s.y;
    }
    if (t.kind === "component") {
      const w = t.width || 40;
      tx = t.x + (s.x > t.x ? w / 2 : -w / 2);
      ty = t.y;
    }
    if (s.kind === "rail") sx = s.x + (t.x > s.x ? 50 : -50);
    if (t.kind === "rail") tx = t.x + (s.x > t.x ? 50 : -50);
    return { x1: sx, y1: sy, x2: tx, y2: ty };
  }

  // Grid mode — pin-level anchors on ICs that expose them.
  if (e.netLabel && s.kind === "component" && s.showPins) {
    const p = (s.pins.sides.left.concat(s.pins.sides.right, s.pins.sides.top, s.pins.sides.bottom)).find(x => x.net_label === e.netLabel);
    if (p) { const [dx, dy] = pinAnchor(s, p); sx = s.x + dx; sy = s.y + dy; }
  }
  if (e.netLabel && t.kind === "component" && t.showPins) {
    const p = (t.pins.sides.left.concat(t.pins.sides.right, t.pins.sides.top, t.pins.sides.bottom)).find(x => x.net_label === e.netLabel);
    if (p) { const [dx, dy] = pinAnchor(t, p); tx = t.x + dx; ty = t.y + dy; }
  }
  if (s.kind === "rail") sx = s.x + (t.x > s.x ? 50 : -50);
  if (t.kind === "rail") tx = t.x + (s.x > t.x ? 50 : -50);
  return { x1: sx, y1: sy, x2: tx, y2: ty };
}

function bezierPath(a) {
  const dx = a.x2 - a.x1;
  const mx = Math.min(Math.max(Math.abs(dx) * 0.5, 30), 180);
  const sign = dx >= 0 ? 1 : -1;
  return `M${a.x1},${a.y1}C${a.x1 + sign * mx},${a.y1} ${a.x2 - sign * mx},${a.y2} ${a.x2},${a.y2}`;
}

function renderGridHeads(model) {
  const g = d3.select("#schBucketHeads");
  g.selectAll("*").remove();

  const phases = model.phasesPresent;
  const rows = model.rows;
  const xFirst = model.colX(phases[0]);
  const xLast = model.colX(phases[phases.length - 1]);
  const yFirst = model.rowY(rows[0].id);
  const yLast = model.rowY(rows[rows.length - 1].id);
  const gridPadX = 40;
  const gridPadY = 60;
  const gridL = xFirst - COL_W / 2 - gridPadX;
  const gridR = xLast + COL_W / 2 + gridPadX;
  const gridT = yFirst - ROW_H / 2 - 30;
  const gridB = yLast + ROW_H / 2 + 40;

  // 1) Voltage-row horizontal bands (backdrop)
  rows.forEach((r, i) => {
    const cy = model.rowY(r.id);
    g.append("rect")
      .attr("class", `sch-vrow-band vrow-${i % 4}`)
      .attr("x", gridL + 60).attr("y", cy - ROW_H / 2 + 4)
      .attr("width", gridR - gridL - 60).attr("height", ROW_H - 8).attr("rx", 10);
  });

  // 2) Phase-column vertical bands (semi-transparent over voltage bands)
  phases.forEach((p) => {
    const cx = model.colX(p);
    g.append("rect")
      .attr("class", `sch-phase-col ${p == null ? "col-none" : ""}`)
      .attr("x", cx - COL_W / 2 + 8).attr("y", gridT + 30)
      .attr("width", COL_W - 16).attr("height", gridB - gridT - 60).attr("rx", 8);
  });

  // 3) Voltage-row labels on the left (sticky-ish: always at the leftmost
  // grid edge — the user can pan but they always live before the first
  // phase column)
  rows.forEach((r) => {
    const cy = model.rowY(r.id);
    const lbl = g.append("g").attr("transform", `translate(${gridL + 30}, ${cy})`);
    lbl.append("rect")
      .attr("class", "sch-vrow-head")
      .attr("x", -44).attr("y", -14).attr("width", 88).attr("height", 28).attr("rx", 6);
    lbl.append("text").attr("class", "sch-vrow-label").attr("y", 4).text(r.label);
  });

  // 4) Phase-column headers on top
  phases.forEach((p) => {
    const cx = model.colX(p);
    const head = g.append("g").attr("transform", `translate(${cx}, ${gridT})`);
    head.append("rect")
      .attr("class", "sch-phase-head")
      .attr("x", -80).attr("y", -16).attr("width", 160).attr("height", 32).attr("rx", 8);
    const label = p == null ? "Pré-boot" : `Φ${p}`;
    head.append("text").attr("class", "sch-phase-label").attr("y", -1).text(label);
    const count = model.nodes.filter(n => (n.phase ?? null) === p).length;
    head.append("text").attr("class", "sch-phase-sub").attr("y", 12).text(`${count} nœud${count > 1 ? "s" : ""}`);
  });
}

function renderNodes(model) {
  const g = d3.select("#schLayerNodes");
  g.selectAll("*").remove();
  const nodesData = model.layoutMode === "railfocus"
    ? model.nodes.filter(n => n._visible)
    : model.nodes;
  const sel = g.selectAll("g.sch-node").data(nodesData, d => d.id).join("g")
    .attr("class", d => `sch-node sch-node-${d.kind} role-${d.role || "rail"} ${d.missing ? "missing" : ""} ${d.populated === false ? "nostuff" : ""} ${d.isSpof ? "spof" : ""}`)
    .attr("transform", d => `translate(${d.x},${d.y})`)
    .attr("data-refdes", d => d.kind === "component" ? (d.refdes ?? null) : null)
    .attr("data-rail",   d => d.kind === "rail" ? (d.label ?? d.id ?? null) : null)
    .on("click", (ev, d) => {
      ev.stopPropagation();
      STATE.selectedId = d.id;
      updateInspector(d);
      applyFocus(d.id, model);
      // Boot phase chip clicks happen via timeline, not here.
    });

  sel.each(function (d) {
    const s = d3.select(this);
    const w = d.width, h = d.height;
    if (d.kind === "rail") {
      s.append("polygon")
        .attr("class", "sch-shape sch-shape-rail")
        .attr("points", `${-w / 2},0 ${-w / 2 + 16},${-h / 2} ${w / 2 - 16},${-h / 2} ${w / 2},0 ${w / 2 - 16},${h / 2} ${-w / 2 + 16},${h / 2}`);
      s.append("text").attr("class", "sch-label sch-label-rail").attr("y", 2).text(d.label);
      if (d.voltage_nominal != null) {
        s.append("text").attr("class", "sch-sub sch-sub-rail").attr("y", h / 2 + 12).text(`${d.voltage_nominal} V`);
      }
      if (d.phase != null) {
        s.append("text").attr("class", "sch-phase-chip").attr("y", -h / 2 - 6).text(`Φ${d.phase}`);
      }
      if (d.isSpof) {
        s.append("text").attr("class", "sch-spof-badge")
          .attr("y", h / 2 + 24).text(`⚠ SPOF · ${d.impactPct}%`);
      }
      // Cascade-dead warning glyph — hidden by default, shown via .sim-cascade.
      s.append("text")
        .attr("class", "sch-cascade-warn")
        .attr("x", 0)
        .attr("y", -h / 2 - 20)
        .attr("text-anchor", "middle")
        .text("⚠");
      return;
    }
    // Component — try the type-specific schematic symbol first; fall back
    // to the generic shape silhouette for ICs and tiny passives.
    if (drawSchematicSymbol(s, d)) {
      // schematic symbol drawn; skip the generic shape branch.
    } else if (d.shape === "rect-big" || d.shape === "rect") {
      s.append("rect").attr("class", "sch-shape sch-shape-comp")
        .attr("x", -w / 2).attr("y", -h / 2).attr("width", w).attr("height", h).attr("rx", 5);
    } else if (d.shape === "capsule") {
      s.append("rect").attr("class", "sch-shape sch-shape-passive")
        .attr("x", -w / 2).attr("y", -h / 4).attr("width", w).attr("height", h / 2).attr("rx", h / 4);
    } else {
      s.append("circle").attr("class", "sch-shape sch-shape-comp").attr("r", Math.max(w, h) / 2);
    }
    if (d.role !== "decoupling") {
      s.append("text").attr("class", "sch-label sch-label-comp").attr("y", 2).text(d.refdes);
      const val = d.value && (d.value.primary || d.value.raw);
      if (val && d.role === "source") {
        s.append("text").attr("class", "sch-sub sch-sub-comp").attr("y", h / 2 + 11).text(String(val).slice(0, 16));
      } else if (d.role === "consumer" && d.type) {
        s.append("text").attr("class", "sch-sub sch-sub-comp").attr("y", h / 2 + 11).text(d.type);
      }
    } else {
      // Small cap value label (e.g. 100nF) inline.
      const val = d.value && (d.value.primary || d.value.raw);
      if (val) {
        s.append("text").attr("class", "sch-sub sch-sub-passive").attr("y", h / 2 + 9).text(String(val).slice(0, 8));
      }
    }
    if (d.isSpof) {
      s.append("text").attr("class", "sch-spof-badge")
        .attr("y", -h / 2 - 7).text(`⚠ SPOF · ${d.impactPct}%`);
    }
    // Cascade-dead warning glyph — hidden by default, shown via .sim-cascade.
    s.append("text")
      .attr("class", "sch-cascade-warn")
      .attr("x", 0)
      .attr("y", -h / 2 - 22)
      .attr("text-anchor", "middle")
      .text("⚠");
    // Pin dots + leader lines for sources & consumers with showPins.
    if (d.showPins) {
      for (const side of ["left", "right", "top", "bottom"]) {
        d.pins.sides[side].forEach(p => {
          const [px, py] = pinAnchor(d, p);
          const pg = s.append("g").attr("class", `sch-pin sch-pin-${side} role-${p.role || "unknown"}`);
          const inward = {
            left: [px + 5, py], right: [px - 5, py],
            top: [px, py + 5], bottom: [px, py - 5],
          }[side];
          pg.append("line").attr("class", "sch-pin-lead")
            .attr("x1", inward[0]).attr("y1", inward[1])
            .attr("x2", px).attr("y2", py);
          pg.append("circle").attr("class", "sch-pin-dot").attr("cx", px).attr("cy", py).attr("r", 2.2);
          if (d.role === "source" && (p.name || p.net_label)) {
            const lbl = (p.name || p.net_label || "").slice(0, 8);
            const tx = side === "left" ? px - 3 : side === "right" ? px + 3 : px;
            const ty = side === "top" ? py - 4 : side === "bottom" ? py + 8 : py + 3;
            const anchor = side === "left" ? "end" : side === "right" ? "start" : "middle";
            pg.append("text").attr("x", tx).attr("y", ty).attr("class", "sch-pin-label").attr("text-anchor", anchor).text(lbl);
          }
        });
      }
    }
  });
}

function renderEdges(model) {
  const g = d3.select("#schLayerLinks");
  g.selectAll("*").remove();
  // All edge kinds are drawn in both layouts — the layout already makes
  // relations spatial, edges make them explicit. In power-tree mode they
  // are short stubs from the horizontal bus line to the attached node so
  // they don't clutter the canvas the way long bezier edges do in a 2D
  // grid.
  // data-signal deferred: edges carry e.netLabel but the simulator's signals
  // state maps user-visible signal names; hook when signal-level sim is added.
  // In rail-focus mode we only draw edges between currently visible nodes.
  const edgesData = model.layoutMode === "railfocus"
    ? model.edges.filter(e => {
        const s = model.nodeById.get(e.sourceId);
        const t = model.nodeById.get(e.targetId);
        return s && t && s._visible && t._visible;
      })
    : model.edges;
  g.selectAll("path").data(edgesData, d => d.id).join("path")
    .attr("class", d => `sch-link sch-link-${d.kind}`)
    .attr("data-subkind", d => d.subkind || null)
    .attr("d", d => {
      const a = edgeAnchors(d, model);
      return a ? bezierPath(a) : null;
    })
    .attr("marker-end", d => d.kind === "produces" ? "url(#sch-arrow-produces)"
      : d.kind === "powers" ? "url(#sch-arrow-powers)"
      : d.kind === "decouples" ? "url(#sch-arrow-decouples)"
      : null);
}

/* ---------------------------------------------------------------------- *
 * BOOT TIMELINE                                                          *
 * ---------------------------------------------------------------------- */

function renderBootTimeline(model) {
  const wrap = el("schBootTimeline");
  if (!wrap) return;
  wrap.innerHTML = "";
  const phases = model.boot || [];
  if (phases.length === 0) {
    wrap.innerHTML = `<div class="sch-boot-empty">Pas de boot_sequence dans le pack.</div>`;
    return;
  }

  // Source badge — "Vérifié Opus" (cyan) vs "Déduit topologique" (amber).
  const headBar = document.createElement("div");
  headBar.className = "sch-boot-headbar";
  const isAnalyzed = model.bootSource === "analyzer";
  const seq = model.analyzerMeta?.sequencer_refdes;
  const conf = model.analyzerMeta?.global_confidence;
  headBar.innerHTML = `
    <span class="sch-boot-src ${isAnalyzed ? 'analyzer' : 'compiler'}">
      ${isAnalyzed ? '✓ Vérifié Opus' : '◆ Déduit topologique'}
    </span>
    ${isAnalyzed && seq ? `<span class="sch-boot-seq">séquenceur: <span class="mono">${escHtml(seq)}</span></span>` : ''}
    ${isAnalyzed && conf != null ? `<span class="sch-boot-conf">confiance: <span class="mono">${conf.toFixed(2)}</span></span>` : ''}
    ${!isAnalyzed ? `<button class="sch-reanalyze" id="schReanalyzeBtn" title="Lancer l'analyse Opus (~$0.25, ~15s)">↻ Analyser avec Opus</button>` : ''}
  `;
  wrap.appendChild(headBar);

  // Pre-compute the board-wide max blast so we can normalize phase-level
  // criticality against the same reference that drives SPOF pulsing.
  const boardMaxBlast = model.maxBlast || 1;

  const grid = document.createElement("div");
  grid.className = "sch-boot-grid";
  grid.style.gridTemplateColumns = `repeat(${phases.length}, minmax(0, 1fr))`;
  phases.forEach((p) => {
    // Find the top SPOF among the nodes of this phase (rails + components).
    const candidates = [
      ...(p.components_entering || []).map(r => model.nodeById.get(`comp:${r}`)),
      ...(p.rails_stable || []).map(r => model.nodeById.get(`rail:${r}`)),
    ].filter(Boolean);
    candidates.sort((a, b) => (b.blastRadius || 0) - (a.blastRadius || 0));
    const top = candidates[0];
    const phaseMaxBlast = top ? top.blastRadius || 0 : 0;
    const phaseMaxPct = top ? top.impactPct || 0 : 0;
    const critLevel = phaseMaxPct >= 25 ? "hi" : phaseMaxPct >= 10 ? "mid" : "lo";
    const critFill = boardMaxBlast > 0 ? Math.min(100, Math.round(100 * phaseMaxBlast / boardMaxBlast)) : 0;

    const col = document.createElement("div");
    col.className = `sch-boot-col crit-${critLevel}`;
    col.dataset.phase = p.index;
    const kindBadge = p.kind ? `<span class="sch-boot-kind kind-${p.kind.replace(/[^a-z]/gi,'')}">${escHtml(p.kind)}</span>` : '';
    const confBadge = p.confidence != null ? `<span class="sch-boot-phase-conf">${p.confidence.toFixed(2)}</span>` : '';
    const critBadge = top ? `
      <div class="sch-boot-crit">
        <div class="sch-boot-crit-bar"><div class="sch-boot-crit-fill crit-${critLevel}" style="width:${critFill}%"></div></div>
        <div class="sch-boot-crit-lbl">
          <span class="sch-boot-crit-icon">${critLevel === "hi" ? "⚠" : critLevel === "mid" ? "●" : "·"}</span>
          SPOF : <span class="mono clickable" data-refdes="${escHtml(top.refdes || top.label)}">${escHtml(top.refdes || top.label)}</span>
          · <strong>${phaseMaxPct}%</strong> du board
        </div>
      </div>
    ` : "";
    // Ultra-compact card: head line (title tronqué), SPOF one-liner,
    // R: chips then C: chips all on single lines with overflow ellipsis.
    col.innerHTML = `
      <div class="sch-boot-head">
        <span class="sch-boot-phase">Φ${p.index}</span>
        <span class="sch-boot-name">${escHtml(p.name || `Phase ${p.index}`)}</span>
        ${kindBadge}
        ${confBadge}
      </div>
      ${top ? `<div class="sch-boot-spof crit-${critLevel}">
        <span class="sch-boot-spof-icon">${critLevel === 'hi' ? '⚠' : critLevel === 'mid' ? '●' : '·'}</span>
        <span class="sch-boot-spof-label">SPOF</span>
        <span class="mono clickable sch-boot-spof-ref" data-refdes="${escHtml(top.refdes || top.label)}">${escHtml(top.refdes || top.label)}</span>
        <span class="sch-boot-spof-pct">${phaseMaxPct}%</span>
      </div>` : ''}
      <div class="sch-boot-line">
        <span class="sch-boot-line-label">R</span>
        ${(p.rails_stable || []).slice(0, 8).map(r => `<span class="mono chip emerald" data-rail="${escHtml(r)}">${escHtml(r)}</span>`).join("")}
        ${(p.rails_stable || []).length > 8 ? `<span class="sch-boot-more">+${p.rails_stable.length - 8}</span>` : ""}
      </div>
      <div class="sch-boot-line">
        <span class="sch-boot-line-label">C</span>
        ${(p.components_entering || []).slice(0, 6).map(c => `<span class="mono chip cyan" data-refdes="${escHtml(c)}">${escHtml(c)}</span>`).join("")}
        ${(p.components_entering || []).length > 6 ? `<span class="sch-boot-more">+${p.components_entering.length - 6}</span>` : ""}
      </div>
    `;
    col.addEventListener("click", () => highlightPhase(model, p.index));
    grid.appendChild(col);
  });
  wrap.appendChild(grid);

  // Re-analyze button fires POST /analyze-boot and reloads when done.
  el("schReanalyzeBtn")?.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    const btn = ev.currentTarget;
    btn.disabled = true;
    btn.textContent = "↻ Analyse en cours…";
    try {
      const res = await fetch(`/pipeline/packs/${encodeURIComponent(STATE.slug)}/schematic/analyze-boot`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      // Poll every 3s until the file appears (max 60s).
      for (let i = 0; i < 20; i++) {
        await new Promise(r => setTimeout(r, 3000));
        const check = await fetch(`/pipeline/packs/${encodeURIComponent(STATE.slug)}/schematic`);
        const body = await check.json();
        if (body.boot_sequence_source === "analyzer") {
          STATE.graph = body;
          fullRender(body);
          return;
        }
      }
      btn.textContent = "↻ Timeout — réessaye";
      btn.disabled = false;
    } catch (err) {
      btn.textContent = `Échec: ${err.message}`;
      btn.disabled = false;
    }
  });
  // Click on individual chip → focus that node.
  wrap.querySelectorAll("[data-rail]").forEach(el => {
    el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const n = model.nodeById.get(`rail:${el.dataset.rail}`);
      if (n) { STATE.selectedId = n.id; updateInspector(n); applyFocus(n.id, model); }
    });
  });
  wrap.querySelectorAll("[data-refdes]").forEach(el => {
    el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const n = model.nodeById.get(`comp:${el.dataset.refdes}`);
      if (n) { STATE.selectedId = n.id; updateInspector(n); applyFocus(n.id, model); }
    });
  });
}

function highlightPhase(model, phaseIdx) {
  const phase = (model.boot || []).find(p => p.index === phaseIdx);
  if (!phase) return;
  const ids = new Set();
  (phase.rails_stable || []).forEach(r => ids.add(`rail:${r}`));
  (phase.components_entering || []).forEach(r => ids.add(`comp:${r}`));

  d3.select("#schGraph").classed("has-focus", true);
  d3.selectAll("#schLayerNodes g.sch-node")
    .classed("focus", d => ids.has(d.id))
    .classed("neighbor", false);
  d3.selectAll("#schLayerLinks path")
    .classed("active-link", d => ids.has(d.sourceId) && ids.has(d.targetId));

  // Update boot col visual.
  el("schBootTimeline").querySelectorAll(".sch-boot-col").forEach(c => {
    c.classList.toggle("active", Number(c.dataset.phase) === phaseIdx);
  });

  // Update inspector to describe the phase.
  const insp = el("schInspector");
  insp.classList.add("open");
  el("schInspType").textContent = "PHASE";
  el("schInspType").className = "sch-type-badge phase";
  el("schInspTitle").textContent = `Φ${phase.index}`;
  el("schInspSub").textContent = phase.name || "";
  el("schInspBody").innerHTML = `
    <section class="sch-insp-section">
      <h3>Rails stabilisés (${(phase.rails_stable || []).length})</h3>
      <div class="sch-chips">
        ${(phase.rails_stable || []).map(r => `<span class="mono chip emerald">${escHtml(r)}</span>`).join("") || "<span class='muted'>Aucun</span>"}
      </div>
    </section>
    <section class="sch-insp-section">
      <h3>Composants qui entrent (${(phase.components_entering || []).length})</h3>
      <div class="sch-chips">
        ${(phase.components_entering || []).map(c => `<span class="mono chip cyan">${escHtml(c)}</span>`).join("") || "<span class='muted'>Aucun</span>"}
      </div>
    </section>
    ${phase.triggers_next && phase.triggers_next.length ? `
    <section class="sch-insp-section">
      <h3>Déclencheurs de la phase suivante</h3>
      ${phase.triggers_next.map(t => {
        if (typeof t === "string") {
          return `<div><span class="mono chip amber">${escHtml(t)}</span></div>`;
        }
        // Analyzer shape: {net_label, from_refdes, rationale}
        const driver = t.from_refdes ? ` ← <span class="mono">${escHtml(t.from_refdes)}</span>` : "";
        const rationale = t.rationale ? `<div class="muted" style="margin-top:4px;font-size:11px">${escHtml(t.rationale)}</div>` : "";
        return `<div style="margin-bottom:8px"><span class="mono chip amber">${escHtml(t.net_label)}</span>${driver}${rationale}</div>`;
      }).join("")}
    </section>` : ""}
    ${phase.evidence && phase.evidence.length ? `
    <section class="sch-insp-section">
      <h3>Évidences (Opus)</h3>
      <ul class="sch-evidence">
        ${phase.evidence.map(ev => `<li>${escHtml(ev)}</li>`).join("")}
      </ul>
    </section>` : ""}
  `;
}

/* ---------------------------------------------------------------------- *
 * FOCUS + INSPECTOR                                                      *
 * ---------------------------------------------------------------------- */

function applyFocus(nodeId, model) {
  d3.select("#schGraph").classed("has-focus", Boolean(nodeId));
  if (!nodeId) return;
  const node = model.nodeById.get(nodeId);
  // Kill-switch mode: highlight the full downstream cascade + upstream chain.
  const dead = computeCascade(model, nodeId);
  const feeds = computeUpstream(model, nodeId);

  d3.selectAll("#schLayerNodes g.sch-node")
    .classed("focus", d => d.id === nodeId)
    .classed("downstream", d => dead.has(d.id) && d.id !== nodeId)
    .classed("upstream", d => feeds.has(d.id) && d.id !== nodeId && !dead.has(d.id))
    .classed("neighbor", false);
  d3.selectAll("#schLayerLinks path")
    .classed("active-link", d =>
      (dead.has(d.sourceId) && dead.has(d.targetId)) ||
      (feeds.has(d.sourceId) && feeds.has(d.targetId))
    );

  // Dim phase highlights.
  el("schBootTimeline")?.querySelectorAll(".sch-boot-col.active").forEach(c => c.classList.remove("active"));
}

function clearFocus() {
  STATE.selectedId = null;
  updateInspector(null);
  d3.select("#schGraph").classed("has-focus", false);
  d3.selectAll("#schLayerNodes g.sch-node").classed("focus", false).classed("downstream", false).classed("upstream", false).classed("neighbor", false);
  d3.selectAll("#schLayerLinks path").classed("active-link", false);
  el("schBootTimeline")?.querySelectorAll(".sch-boot-col.active").forEach(c => c.classList.remove("active"));
}

function updateInspector(node) {
  const insp = el("schInspector");
  if (!node) { insp.classList.remove("open"); return; }
  insp.classList.add("open");

  const typeBadge = el("schInspType");
  const title = el("schInspTitle");
  const sub = el("schInspSub");
  const body = el("schInspBody");

  const critBlock = node.blastRadius != null ? `
      <section class="sch-insp-section sch-criticality ${node.isSpof ? 'spof' : ''}">
        <h3>${node.isSpof ? '⚠ Single-Point-Of-Failure' : 'Criticité (blast radius)'}</h3>
        <div class="sch-crit-row">
          <div class="sch-crit-bar">
            <div class="sch-crit-fill" style="width:${(node.criticality * 100).toFixed(0)}%"></div>
          </div>
          <div class="sch-crit-val">
            <strong>${node.blastRadius}</strong> dépendants · <strong>${node.impactPct}%</strong> du board
          </div>
        </div>
      </section>` : "";

  // Look up the functional domain + one-liner description from the
  // classified-nets overlay (populated by the net classifier, regex or Opus).
  const classified = ((STATE.graph && STATE.graph.net_classification) || {}).nets || {};
  const netMeta = node.kind === "rail" ? classified[node.label] : null;
  const domainBlock = netMeta ? `
      <section class="sch-insp-section sch-domain">
        <h3>Domaine · ${escHtml(netMeta.domain || "misc")}</h3>
        ${netMeta.description ? `<div class="sch-domain-desc">${escHtml(netMeta.description)}</div>` : ""}
        ${netMeta.voltage_level ? `<div class="sch-domain-meta"><span class="k">Niveau</span> <span class="mono">${escHtml(netMeta.voltage_level)}</span></div>` : ""}
      </section>` : "";

  if (node.kind === "rail") {
    typeBadge.textContent = "RAIL";
    typeBadge.className = "sch-type-badge rail";
    title.textContent = node.label;
    sub.textContent = (node.voltage_nominal != null ? `${node.voltage_nominal} V` : "—") + " · " + (node.source_type || "—");

    const cascade = computeCascade(STATE.model, node.id);
    const casDead = Array.from(cascade).filter(id => id !== node.id);

    body.innerHTML = `
      ${critBlock}
      ${domainBlock}
      <section class="sch-insp-section">
        <h3>Alimentation</h3>
        <div class="sch-meta-grid">
          <dt>Producer</dt><dd>${node.source_refdes ? `<span class="mono chip cyan clickable" data-id="comp:${escHtml(node.source_refdes)}">${escHtml(node.source_refdes)}</span>` : "<span class='muted'>externe</span>"}</dd>
          <dt>Type</dt><dd>${escHtml(node.source_type || "—")}</dd>
          <dt>Enable</dt><dd>${node.enable_net ? `<span class="mono">${escHtml(node.enable_net)}</span>` : "—"}</dd>
          <dt>Boot</dt><dd>${node.phase ? `<span class="mono chip amber">Φ${node.phase}</span>` : "—"}</dd>
        </div>
      </section>
      <section class="sch-insp-section">
        <h3>Consumers (${node.consumers.length})</h3>
        ${node.consumers.length === 0 ? "<div class='muted'>Aucun.</div>" : `
          <div class="sch-chips">${node.consumers.map(c => `<span class="mono chip cyan clickable" data-id="comp:${escHtml(c)}">${escHtml(c)}</span>`).join("")}</div>`}
      </section>
      <section class="sch-insp-section">
        <h3>Décuplage (${node.decoupling.length})</h3>
        ${node.decoupling.length === 0 ? "<div class='muted'>Aucun.</div>" : `
          <div class="sch-chips">${node.decoupling.map(c => `<span class="mono chip violet clickable" data-id="comp:${escHtml(c)}">${escHtml(c)}</span>`).join("")}</div>`}
      </section>
      <section class="sch-insp-section">
        <h3>⚡ Cascade si ce rail tombe (${casDead.length} dépendants)</h3>
        ${casDead.length === 0 ? "<div class='muted'>Aucun downstream.</div>" : `
          <div class="sch-chips">${casDead.slice(0, 40).map(id => {
            const n = STATE.model.nodeById.get(id);
            const label = n.kind === "rail" ? n.label : n.refdes;
            const cls = n.kind === "rail" ? "emerald" : "cyan";
            return `<span class="mono chip ${cls} clickable" data-id="${escHtml(id)}">${escHtml(label)}</span>`;
          }).join("")}${casDead.length > 40 ? `<span class="muted">+${casDead.length - 40}</span>` : ""}</div>`}
      </section>
    `;
  } else {
    typeBadge.textContent = (node.type || "COMP").toUpperCase();
    typeBadge.className = `sch-type-badge ${node.role || "component"}`;
    title.textContent = node.refdes;
    const v = node.value && (node.value.primary || node.value.raw);
    sub.textContent = `${v || "—"}${node.value?.package ? ` · ${node.value.package}` : ""}`;

    const producesRails = (STATE.model.edges || []).filter(e => e.kind === "produces" && e.sourceId === node.id).map(e => e.netLabel);
    const consumesRails = (STATE.model.edges || []).filter(e => e.kind === "powers" && e.targetId === node.id).map(e => e.netLabel);
    const decouplesRails = (STATE.model.edges || []).filter(e => e.kind === "decouples" && e.sourceId === node.id).map(e => e.netLabel);

    const cascade = computeCascade(STATE.model, node.id);
    const casDead = Array.from(cascade).filter(id => id !== node.id);

    body.innerHTML = `
      ${critBlock}
      <section class="sch-insp-section">
        <h3>Métadonnées</h3>
        <div class="sch-meta-grid">
          <dt>Rôle</dt><dd><span class="sch-role-badge role-${node.role}">${escHtml(node.role)}</span></dd>
          <dt>Type</dt><dd>${escHtml(node.type || "—")}</dd>
          <dt>Pages</dt><dd>${node.pages && node.pages.length ? `p. ${node.pages.join(", ")}` : "—"}</dd>
          <dt>Populé</dt><dd>${node.populated ? "oui" : "<span class='warn'>NOSTUFF</span>"}</dd>
          <dt>MPN</dt><dd>${node.value?.mpn ? `<span class="mono">${escHtml(node.value.mpn)}</span>` : "—"}</dd>
          <dt>Boot</dt><dd>${node.phase ? `<span class="mono chip amber">Φ${node.phase}</span>` : "—"}</dd>
        </div>
      </section>
      ${producesRails.length ? `
      <section class="sch-insp-section">
        <h3>Produit (${producesRails.length})</h3>
        <div class="sch-chips">${producesRails.map(r => `<span class="mono chip emerald clickable" data-id="rail:${escHtml(r)}">${escHtml(r)}</span>`).join("")}</div>
      </section>` : ""}
      ${consumesRails.length ? `
      <section class="sch-insp-section">
        <h3>Consomme (${consumesRails.length})</h3>
        <div class="sch-chips">${consumesRails.map(r => `<span class="mono chip emerald clickable" data-id="rail:${escHtml(r)}">${escHtml(r)}</span>`).join("")}</div>
      </section>` : ""}
      ${decouplesRails.length ? `
      <section class="sch-insp-section">
        <h3>Décuple</h3>
        <div class="sch-chips">${decouplesRails.map(r => `<span class="mono chip violet clickable" data-id="rail:${escHtml(r)}">${escHtml(r)}</span>`).join("")}</div>
      </section>` : ""}
      <section class="sch-insp-section">
        <h3>⚡ Cascade si ${node.refdes} meurt (${casDead.length} dépendants)</h3>
        ${casDead.length === 0 ? "<div class='muted'>Aucun downstream identifié.</div>" : `
          <div class="sch-chips">${casDead.slice(0, 40).map(id => {
            const n = STATE.model.nodeById.get(id);
            const label = n.kind === "rail" ? n.label : n.refdes;
            const cls = n.kind === "rail" ? "emerald" : "cyan";
            return `<span class="mono chip ${cls} clickable" data-id="${escHtml(id)}">${escHtml(label)}</span>`;
          }).join("")}${casDead.length > 40 ? `<span class="muted">+${casDead.length - 40}</span>` : ""}</div>`}
      </section>
      ${node.pinsAll && node.pinsAll.length ? `
      <section class="sch-insp-section">
        <h3>Pins (${node.pinsAll.length})</h3>
        <table class="sch-pin-table">
          <thead><tr><th>#</th><th>Name</th><th>Role</th><th>Net</th></tr></thead>
          <tbody>
          ${node.pinsAll.map(p => `
            <tr>
              <td class="mono">${escHtml(p.number)}</td>
              <td class="mono">${escHtml(p.name || "—")}</td>
              <td class="mono pin-role">${escHtml(p.role || "unknown")}</td>
              <td class="mono">${p.net_label ? `<span class="chip emerald">${escHtml(p.net_label)}</span>` : "—"}</td>
            </tr>`).join("")}
          </tbody>
        </table>
      </section>` : ""}
    `;
  }

  // --- Observation row (reverse-diagnostic input, contextual per node kind) ---
  const obsKind = node.kind === "component" ? "comp" : node.kind === "rail" ? "rail" : null;
  const obsKey = node.kind === "component" ? node.refdes : node.kind === "rail" ? node.label : null;
  if (obsKind && obsKey) {
    const modesForKind = obsKind === "rail"
      ? [["unknown", "⚪ inconnu"], ["alive", "✅ vivant"], ["dead", "❌ mort"], ["anomalous", "⚠ anomalous"], ["shorted", "⚡ shorté"]]
      : [["unknown", "⚪ inconnu"], ["alive", "✅ vivant"], ["dead", "❌ mort"], ["anomalous", "⚠ anomalous"], ["hot", "🔥 chaud"]];
    const stateMap = obsKind === "rail"
      ? SimulationController.observations.state_rails
      : SimulationController.observations.state_comps;
    const current = stateMap.get(obsKey) || "unknown";

    const row = document.createElement("div");
    row.className = "sim-obs-row";
    const picker = document.createElement("div");
    picker.className = "sim-mode-picker";
    picker.setAttribute("data-kind", obsKind);
    for (const [mode, label] of modesForKind) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.dataset.mode = mode;
      if (mode === current) btn.classList.add("active");
      btn.textContent = label;
      btn.addEventListener("click", () => {
        SimulationController.setObservation(obsKind, obsKey, mode);
        updateInspector(node);
      });
      picker.appendChild(btn);
    }
    row.innerHTML = `<span class="sim-obs-label">Observation</span>`;
    row.appendChild(picker);
    body.appendChild(row);

    // --- Metric input row ---
    const unitForKind = obsKind === "rail" ? "V" : "°C";
    const metricMap = obsKind === "rail"
      ? SimulationController.observations.metrics_rails
      : SimulationController.observations.metrics_comps;
    const existingMetric = metricMap.get(obsKey);

    const metricRow = document.createElement("div");
    metricRow.className = "sim-metric-row";
    // Infer nominal from the rail label if the tech hasn't recorded one yet.
    const inferredNominal = obsKind === "rail" ? inferRailNominalV(obsKey) : null;
    const nominalForDisplay = existingMetric?.nominal ?? inferredNominal;
    metricRow.innerHTML = `
      <span class="sim-obs-label">Mesuré</span>
      <input type="number" class="sim-metric-input" step="0.01" value="${existingMetric?.measured ?? ""}">
      <select class="sim-metric-unit">
        ${["V", "mV", "A", "°C", "Ω", "W"].map(u =>
          `<option value="${u}" ${u === (existingMetric?.unit || unitForKind) ? "selected" : ""}>${u}</option>`
        ).join("")}
      </select>
      <span class="sim-metric-nominal">${nominalForDisplay != null ? `nominal: ${nominalForDisplay}${existingMetric?.unit || unitForKind}` : ""}</span>
      <button type="button" class="sim-metric-record">Enregistrer</button>
    `;
    const inputEl = metricRow.querySelector(".sim-metric-input");
    const unitEl = metricRow.querySelector(".sim-metric-unit");
    const recordBtn = metricRow.querySelector(".sim-metric-record");
    const doRecord = async () => {
      const valueRaw = inputEl.value.trim();
      if (valueRaw === "") return;
      const value = parseFloat(valueRaw);
      if (!Number.isFinite(value)) return;
      const unit = unitEl.value;
      const nominal = existingMetric?.nominal ?? inferredNominal;
      // Client-side auto-classify mirror (same thresholds as Python side).
      const mode = clientAutoClassify(obsKind, value, unit, nominal);
      // Update local state immediately.
      SimulationController.setObservation(obsKind, obsKey, mode || "unknown", {
        measured: value, unit, nominal,
      });
      // POST to the journal if we have a repair_id.
      const slug = STATE.slug;
      const repairId = new URLSearchParams(location.search).get("repair")
        || new URLSearchParams(location.hash.split("?")[1] || "").get("repair");
      if (slug && repairId) {
        try {
          await fetch(
            `/pipeline/packs/${encodeURIComponent(slug)}/repairs/${encodeURIComponent(repairId)}/measurements`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                target: `${obsKind === "comp" ? "comp" : "rail"}:${obsKey}`,
                value, unit, nominal,
              }),
            },
          );
        } catch (err) {
          console.warn("[measurements] POST failed", err);
        }
      }
      updateInspector(node);
    };
    inputEl.addEventListener("keydown", ev => { if (ev.key === "Enter") doRecord(); });
    inputEl.addEventListener("blur", doRecord);
    recordBtn.addEventListener("click", doRecord);
    body.appendChild(metricRow);
  }

  // --- Diagnostiquer / Réinitialiser buttons (reverse-diagnostic) ---
  // Shown whenever at least one observation is recorded, regardless of
  // which node is currently selected in the inspector.
  const obsCount = Object.values(SimulationController.observations).reduce((sum, m) => sum + m.size, 0);
  if (obsCount > 0) {
    const diagBtn = document.createElement("button");
    diagBtn.className = "sim-inspector-action sim-inspector-action--diag";
    diagBtn.textContent = `Diagnostiquer (${obsCount} observation${obsCount > 1 ? "s" : ""})`;
    diagBtn.addEventListener("click", () => SimulationController.hypothesize(STATE.slug));
    body.appendChild(diagBtn);

    const clearBtn = document.createElement("button");
    clearBtn.className = "sim-inspector-action";
    clearBtn.textContent = "Réinitialiser observations";
    clearBtn.addEventListener("click", () => {
      SimulationController.clearObservations();
      updateInspector(node);
    });
    body.appendChild(clearBtn);
  }

  // --- Fault-injection action (behavioral simulator integration) ---
  // Appears only on component nodes. Toggles the refdes into
  // SimulationController.killedRefdes, re-fetches the timeline, and seeks
  // the scrubber to the phase where the board stalls so the tech sees the
  // cascade immediately.
  if (node.kind !== "rail" && node.refdes) {
    const already = SimulationController.killedRefdes.includes(node.refdes);
    const faultBtn = document.createElement("button");
    faultBtn.className = "sim-inspector-action sim-inspector-action--danger";
    faultBtn.textContent = already
      ? `Retirer la panne · ${node.refdes}`
      : `Simuler panne · ${node.refdes}`;
    faultBtn.addEventListener("click", async () => {
      if (already) {
        SimulationController.killedRefdes = SimulationController.killedRefdes.filter(r => r !== node.refdes);
      } else {
        SimulationController.killedRefdes.push(node.refdes);
      }
      await SimulationController.refresh(STATE.slug);
      const tl = SimulationController.timeline;
      if (tl && tl.blocked_at_phase != null) {
        const idx = tl.states.findIndex(s => s.phase_index === tl.blocked_at_phase);
        if (idx >= 0) SimulationController.seek(idx);
        SimulationController.pause();
      }
    });
    body.appendChild(faultBtn);

    // Reset button — only when at least one fault is active.
    if (SimulationController.killedRefdes.length > 0) {
      const resetBtn = document.createElement("button");
      resetBtn.className = "sim-inspector-action";
      resetBtn.textContent = `Réinitialiser la simulation (${SimulationController.killedRefdes.length} panne(s))`;
      resetBtn.addEventListener("click", async () => {
        SimulationController.killedRefdes = [];
        await SimulationController.refresh(STATE.slug);
        SimulationController.seek(0);
      });
      body.appendChild(resetBtn);
    }
  }

  // Wire clickable chips inside the inspector to navigate between nodes.
  body.querySelectorAll(".clickable[data-id]").forEach(el => {
    el.addEventListener("click", () => {
      const id = el.dataset.id;
      const n = STATE.model.nodeById.get(id);
      if (n) { STATE.selectedId = id; updateInspector(n); applyFocus(id, STATE.model); }
    });
  });
}

/* ---------------------------------------------------------------------- *
 * ZOOM / PAN / FIT                                                       *
 * ---------------------------------------------------------------------- */

function initZoom(model) {
  const svg = d3.select("#schGraph");
  const root = d3.select("#schZoomRoot");
  const zoom = d3.zoom().scaleExtent([0.2, 3.5]).on("zoom", (ev) => {
    root.attr("transform", ev.transform);
    el("schZoomLabel").textContent = `× ${ev.transform.k.toFixed(2)}`;
    document.getElementById("schGraph").dataset.zoom =
      ev.transform.k < 0.5 ? "low" : ev.transform.k < 1.2 ? "mid" : "high";
  });
  STATE.zoom = zoom;
  svg.call(zoom);
  fitToBounds(model);
}

function fitToBounds(model) {
  if (!model.bounds) return;
  const canvas = el("schCanvas");
  const W = canvas.clientWidth, H = canvas.clientHeight;
  const { minX, minY, maxX, maxY } = model.bounds;
  const bw = maxX - minX, bh = maxY - minY;
  const scale = Math.min((W - 60) / bw, (H - TIMELINE_H - 60) / bh, 1.4);
  const tx = (W - bw * scale) / 2 - minX * scale;
  const ty = (H - TIMELINE_H - bh * scale) / 2 - minY * scale + 10;
  d3.select("#schGraph").transition().duration(400).call(STATE.zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(scale));
}

// Canonical net domains recognized by the filter. Typing one of these
// highlights every node whose primary net belongs to the domain.
const KNOWN_DOMAINS = new Set([
  "hdmi", "usb", "pcie", "ethernet", "audio", "display",
  "storage", "debug", "power_seq", "power_rail", "clock",
  "reset", "control", "ground", "misc",
]);

// Secondary label-prefix patterns per domain. When Sonnet tags a rail as
// power_rail (e.g. USB_PWR is functionally USB but structurally a rail),
// the substring pattern recovers it so the tech sees the full HDMI / USB /
// etc. family when they query by domain.
const DOMAIN_SUBSTRING = {
  hdmi:     /\b(HDMI|TMDS|DDC|CEC)\b|^(HDMI|TMDS|DDC)_/i,
  usb:      /\bUSB\b|^USB|USB_/i,
  pcie:     /\bPCIE\b|^PCIE/i,
  ethernet: /\b(ETH|RGMII|MII|MDIO|PHY)\b|^(ETH|RGMII|MII|MDIO|PHY)_/i,
  audio:    /\b(I2S|DAC|ADC|SPDIF|AUDIO|MICBIAS|AVDD|DBVDD|DCVDD|SPKVDD)\b|^(I2S|DAC|ADC|SPDIF|AUDIO|MIC)_/i,
  display:  /\b(EDP|DSI|LCD|BACKLIGHT|LVDS|DP_AUX)\b|^(EDP|DSI|LCD|BL_)/i,
  storage:  /\b(SD|EMMC|MMC|SDHC|SDIO)\b|^(SD|EMMC|MMC)_/i,
  debug:    /\b(JTAG|SWD|UART|TDI|TDO|TCK|TMS|SWDIO|SWCLK)\b|^(JTAG|SWD|UART)_/i,
  // power_seq / power_rail / clock / reset / control / ground : pas de
  // prefix-family — on s'en tient au domain classé pour ceux-là.
};

function highlightDomain(model, domain) {
  const graph = STATE.graph || {};
  const classified = (graph.net_classification && graph.net_classification.nets) || {};
  const allNets = graph.nets || {};
  const matchingNets = new Set();

  // 1) Primary — nets whose classified domain matches.
  for (const [label, cn] of Object.entries(classified)) {
    if ((cn.domain || "").toLowerCase() === domain) matchingNets.add(label);
  }

  // 2) Secondary — functional-family substring/prefix match so a net like
  // USB_PWR (classified as power_rail) still lights up when the tech
  // filters by 'usb'. Covers the most common cross-classifications.
  const pattern = DOMAIN_SUBSTRING[domain];
  if (pattern) {
    for (const label of Object.keys(allNets)) {
      if (pattern.test(label)) matchingNets.add(label);
    }
    // Also pick up classified-only nets we haven't enumerated yet.
    for (const label of Object.keys(classified)) {
      if (pattern.test(label)) matchingNets.add(label);
    }
  }

  if (matchingNets.size === 0) {
    el("schFilterStatus").textContent = `${domain} · 0 nets`;
    return false;
  }

  // Find every component whose pins touch at least one net in the domain.
  const matchingComponents = new Set();
  for (const n of model.nodes) {
    if (n.kind !== "component") continue;
    const pins = n.pinsAll || [];
    if (pins.some(p => matchingNets.has(p.net_label))) matchingComponents.add(n.id);
    // Also include rails whose label matches.
  }
  for (const n of model.nodes) {
    if (n.kind === "rail" && matchingNets.has(n.label)) matchingComponents.add(n.id);
  }

  if (matchingComponents.size === 0) {
    el("schFilterStatus").textContent = `${domain} · 0 matches`;
    return true;
  }

  d3.select("#schGraph").classed("has-focus", true);
  d3.selectAll("#schLayerNodes g.sch-node")
    .classed("focus", false)
    .classed("neighbor", d => matchingComponents.has(d.id))
    .classed("downstream", false)
    .classed("upstream", false);
  d3.selectAll("#schLayerLinks path")
    .classed("active-link", d => matchingComponents.has(d.sourceId) && matchingComponents.has(d.targetId));

  el("schFilterStatus").textContent = `${domain} · ${matchingComponents.size} composants`;
  return true;
}

function runFilter(q, model) {
  if (!q) { clearFocus(); el("schFilterStatus").textContent = ""; return; }
  const qu = q.toUpperCase().trim();
  const ql = q.toLowerCase().trim();

  // 1) Recognized functional domain → highlight the whole cluster.
  if (KNOWN_DOMAINS.has(ql)) {
    if (highlightDomain(model, ql)) return;
  }

  // 2) Fall back to refdes / rail label match.
  // IMPORTANT: filter only highlights + zooms. It does NOT open the
  // inspector — otherwise typing "u" while aiming for "usb" would
  // auto-focus USB_PWR and pop its inspector before the user finishes
  // typing the domain keyword. User has to click the node explicitly to
  // open the inspector.
  const hit = model.nodes.find(n => (n.refdes || n.label).toUpperCase() === qu)
    || model.nodes.find(n => (n.refdes || n.label).toUpperCase().startsWith(qu));
  if (!hit) { el("schFilterStatus").textContent = "aucun"; return; }
  el("schFilterStatus").textContent = `→ ${hit.refdes || hit.label}`;
  // Visual highlight only — surface the node's neighbours like a hover
  // would, but keep the inspector closed.
  d3.select("#schGraph").classed("has-focus", true);
  const neighborIds = new Set([hit.id]);
  for (const e of model.edges) {
    if (e.sourceId === hit.id) neighborIds.add(e.targetId);
    if (e.targetId === hit.id) neighborIds.add(e.sourceId);
  }
  d3.selectAll("#schLayerNodes g.sch-node")
    .classed("focus", d => d.id === hit.id)
    .classed("neighbor", d => neighborIds.has(d.id) && d.id !== hit.id)
    .classed("downstream", false)
    .classed("upstream", false);
  d3.selectAll("#schLayerLinks path")
    .classed("active-link", d => d.sourceId === hit.id || d.targetId === hit.id);
  const canvas = el("schCanvas");
  const W = canvas.clientWidth, H = canvas.clientHeight;
  const scale = 1.7;
  const tx = W / 2 - hit.x * scale;
  const ty = (H - TIMELINE_H) / 2 - hit.y * scale;
  d3.select("#schGraph").transition().duration(400).call(STATE.zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(scale));
}

/* ---------------------------------------------------------------------- *
 * STATS + EMPTY                                                          *
 * ---------------------------------------------------------------------- */

function updateStats(model, graph) {
  const compCount = model.nodes.filter(n => n.kind === "component").length;
  const railCount = model.nodes.filter(n => n.kind === "rail").length;
  const sourceShown = model.nodes.filter(
    n => n.kind === "component" && n.role === "source"
  ).length;
  const t = model.totals || {};

  // Stats show "affiché / total" so the tech knows what's filtered. A
  // grey-out helper styles the denominator in CSS.
  const ratio = (shown, total) => total && shown < total
    ? `${shown}<span class="sch-stat-total">/${total}</span>`
    : `${shown}`;
  el("schStatComps").innerHTML  = ratio(compCount,   t.components ?? compCount);
  el("schStatRails").innerHTML  = ratio(railCount,   t.rails      ?? railCount);
  el("schStatRegs").innerHTML   = ratio(sourceShown, t.sources    ?? sourceShown);
  el("schStatPhases").textContent = t.phases ?? (graph.boot_sequence || []).length;
  const q = graph.quality || {};
  el("schStatConf").textContent   = q.confidence_global != null ? q.confidence_global.toFixed(2) : "—";
  el("schStatPages").textContent  = q.pages_parsed != null ? `${q.pages_parsed}/${q.total_pages}` : "—";

  // Dégradé badge + tooltip explaining WHICH threshold triggered it
  // (compiler criteria: confidence_global < 0.7 OR orphan_cross_page > 5).
  const deg = el("schStatDegraded");
  deg.classList.toggle("on", Boolean(q.degraded_mode));
  if (q.degraded_mode) {
    const reasons = [];
    if (q.confidence_global != null && q.confidence_global < 0.7) {
      reasons.push(`confidence globale ${q.confidence_global.toFixed(2)} < 0.7`);
    }
    if (q.orphan_cross_page_refs != null && q.orphan_cross_page_refs > 5) {
      reasons.push(`${q.orphan_cross_page_refs} orphan cross-page refs > 5`);
    }
    if (q.nets_unresolved) {
      reasons.push(`${q.nets_unresolved} nets non résolus`);
    }
    deg.title = "Mode dégradé actif — la viz est exploitable mais quelques "
      + "infos du schematic n'ont pas pu être croisées automatiquement "
      + "entre pages :\n\n• " + reasons.join("\n• ")
      + `\n\nPages parsées : ${q.pages_parsed ?? "?"}/${q.total_pages ?? "?"}`;
  } else {
    deg.title = "";
  }
}

function showEmptyState(title, detail, hint = null) {
  const w = el("schEmptyState");
  if (!w) return;
  w.classList.remove("hidden");
  el("schEmptyTitle").textContent = title;
  el("schEmptyDetail").textContent = detail;
  const h = el("schEmptyHint");
  if (hint) { h.textContent = hint; h.classList.remove("hidden"); }
  else h.classList.add("hidden");
  el("schCanvas").classList.add("hidden");
  el("schBootTimeline")?.classList.add("hidden");
}

function hideEmptyState() {
  el("schEmptyState")?.classList.add("hidden");
  el("schCanvas").classList.remove("hidden");
  el("schBootTimeline")?.classList.remove("hidden");
}

/* ---------------------------------------------------------------------- *
 * PUBLIC                                                                 *
 * ---------------------------------------------------------------------- */

function fullRender(graph) {
  hideEmptyState();
  const model = buildModel(graph);
  STATE.model = model;

  // CSS reacts on the body class — it shows the rail sidebar and shifts the
  // canvas 240px right in railfocus mode.
  document.body.classList.toggle("sch-mode-railfocus", STATE.layoutMode === "railfocus");

  if (STATE.layoutMode === "railfocus") {
    renderRailBar(model);
    // Drop a stale selection if the rail no longer exists in this pack.
    let rid = STATE.selectedRailId;
    if (rid && !model.nodeById.has(rid)) {
      rid = null;
      STATE.selectedRailId = null;
      try { localStorage.removeItem("schSelectedRail"); } catch (_) {}
    }
    computeRailFocusLayout(model, rid);
    renderRailFocusHeads(model);
  } else if (STATE.layoutMode === "powertree") {
    computePowertreeLayout(model);
    renderPowertreeHeads(model);
  } else {
    computeGridLayout(model);
    renderGridHeads(model);
  }
  renderNodes(model);
  renderEdges(model);
  renderBootTimeline(model);
  updateStats(model, graph);
  initZoom(model);
  d3.select("#schGraph").on("click", (ev) => {
    if (ev.target.tagName === "svg" || ev.target.id === "schGraph") clearFocus();
  });
  // Reflect the current mode on the toggle buttons.
  document.querySelectorAll("[data-sch-mode]").forEach(btn => {
    btn.classList.toggle("on", btn.dataset.schMode === STATE.layoutMode);
  });
}

export async function loadSchematic() {
  // Re-read persisted prefs on every section entry — another module (e.g.
  // the boardview minimap) may have flipped layoutMode / selectedRailId
  // between visits, and the module-level STATE init only runs once.
  try {
    const storedMode = localStorage.getItem("schLayoutMode");
    if (storedMode) STATE.layoutMode = storedMode;
    STATE.selectedRailId = localStorage.getItem("schSelectedRail") || null;
  } catch (_) { /* ignore */ }

  const slug = getDeviceSlug();
  STATE.slug = slug;
  if (!slug) {
    showEmptyState("Aucune réparation en cours", "Ouvre une réparation depuis le Journal pour charger son graphe électrique.");
    return;
  }
  const res = await fetchSchematic(slug);
  if (res.missing) {
    showEmptyState("Pas de schematic ingéré", `Aucun graphe électrique compilé pour ${slug}.`,
      `curl -X POST http://localhost:8000/pipeline/ingest-schematic \\\n  -H 'content-type: application/json' \\\n  -d '{"device_slug":"${slug}","pdf_path":"board_assets/${slug}.pdf"}'`);
    return;
  }
  if (res.error) { showEmptyState("Erreur de chargement", res.error); return; }
  STATE.graph = res.graph;
  fullRender(res.graph);
  // Trigger the simulator fetch — the endpoint is fast (< 10ms server-side);
  // we do it unconditionally when a graph has boot_sequence + power_rails.
  if (STATE.graph && STATE.graph.boot_sequence?.length && Object.keys(STATE.graph.power_rails || {}).length) {
    SimulationController.refresh(STATE.slug);
  }
  // Hydrate the observation state from the per-repair measurement journal so
  // the tech's past readings persist across reloads.
  await SimulationController.hydrateFromJournal(slug);
  wireControls();
}

function wireControls() {
  el("schBtnFit")?.addEventListener("click", () => { if (STATE.model) fitToBounds(STATE.model); });
  el("schBtnZoomIn")?.addEventListener("click", () => {
    if (STATE.zoom) d3.select("#schGraph").transition().duration(180).call(STATE.zoom.scaleBy, 1.3);
  });
  el("schBtnZoomOut")?.addEventListener("click", () => {
    if (STATE.zoom) d3.select("#schGraph").transition().duration(180).call(STATE.zoom.scaleBy, 1 / 1.3);
  });
  const filterIn = el("schFilterInput");
  // Debounce 180ms so a rapid-typed "usb" doesn't re-run the filter 3 times
  // (which would each run a full re-highlight before the user finishes).
  let filterDebounceTimer = null;
  filterIn?.addEventListener("input", (ev) => {
    clearTimeout(filterDebounceTimer);
    const value = ev.target.value;
    filterDebounceTimer = setTimeout(() => {
      if (STATE.model) runFilter(value, STATE.model);
    }, 180);
  });
  filterIn?.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      clearTimeout(filterDebounceTimer);
      ev.target.value = "";
      clearFocus();
      el("schFilterStatus").textContent = "";
    }
  });
  // DRY toggle wiring — each button flips a STATE flag, reflects in CSS,
  // and re-renders. The three toggles (Passifs signal / Signaux /
  // Toutes pins) need identical plumbing.
  const wireToggle = (buttonId, stateKey) => {
    const btn = el(buttonId);
    if (!btn) return;
    // Reflect initial state in case it was loaded from elsewhere.
    btn.classList.toggle("on", Boolean(STATE[stateKey]));
    btn.addEventListener("click", (ev) => {
      STATE[stateKey] = !STATE[stateKey];
      ev.currentTarget.classList.toggle("on", STATE[stateKey]);
      if (STATE.graph) fullRender(STATE.graph);
    });
  };
  wireToggle("schTogglePassives", "showPassives");
  wireToggle("schToggleSignals",  "showSignals");
  wireToggle("schToggleAllPins",  "showAllPins");
  document.querySelectorAll("[data-sch-mode]").forEach(btn => {
    btn.addEventListener("click", (ev) => {
      const mode = ev.currentTarget.dataset.schMode;
      if (!mode || mode === STATE.layoutMode) return;
      STATE.layoutMode = mode;
      try { localStorage.setItem("schLayoutMode", mode); } catch (_) { /* ignore quota/denied */ }
      if (STATE.graph) fullRender(STATE.graph);
    });
  });
}

export function closeSchematicInspector() { clearFocus(); }

// Expose SimulationController globally so llm.js (the WS message handler) can
// dispatch simulation.observation_set / simulation.observation_clear events
// without a module import cycle. llm.js dispatches:
//   window.SimulationController?.setObservation(kind, key, mode, measurement)
//   window.SimulationController?.clearObservations()
window.SimulationController = SimulationController;
