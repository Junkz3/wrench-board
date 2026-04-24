// Relations-schematic minimap.
//
// Glass overlay on the boardview that surfaces the 1-hop schematic
// neighborhood of the clicked selection — the bridge between physical PCB
// (boardview) and logical power tree (schematic).
//
// Two modes based on what the user clicked in brd_viewer:
//   - COMPONENT mode  — center shows the IC, pins grouped by role, rails
//     consumed/produced/decoupled on each side. The question answered:
//     "what does this part do electrically?"
//   - NET mode (pin click) — center shows the net, every other pin on the
//     same net arranged around it: source IC (if a power rail), consumer
//     ICs, decoupling caps. The question answered: "what else is on this
//     signal?"
//
// Data source: `/pipeline/packs/{slug}/schematic` — the compiled
// ElectricalGraph, cached in-module after the first fetch.

import { ICON_WARNING } from './icons.js';

let schematicCache = null;       // { slug, data }
let fetchInFlight = null;        // { slug, promise }
let wiredUI = false;
let enabled = (() => {
  try { return localStorage.getItem("bvMinimapEnabled") !== "false"; }
  catch (_) { return true; }
})();
let lastSelection = null;

const SVG_NS = "http://www.w3.org/2000/svg";

// Per-role display metadata — labels in FR for the body section, icons are
// simple ASCII glyphs so the text stays aligned in the mono column.
const ROLE_META = {
  power_in:        { label: "VIN",  glyph: "◄" },
  power_out:       { label: "VOUT", glyph: "►" },
  switch_node:     { label: "SW",   glyph: "~" },
  ground:          { label: "GND",  glyph: "⏚" },
  enable_in:       { label: "EN",   glyph: "◄" },
  enable_out:      { label: "EN↑",  glyph: "►" },
  reset_in:        { label: "RST",  glyph: "◄" },
  reset_out:       { label: "RST↑", glyph: "►" },
  power_good_out:  { label: "PG",   glyph: "►" },
  feedback_in:     { label: "FB",   glyph: "◄" },
  clock_in:        { label: "CLK",  glyph: "◄" },
  clock_out:       { label: "CLK↑", glyph: "►" },
  signal_in:       { label: "SIG",  glyph: "◄" },
  signal_out:      { label: "SIG↑", glyph: "►" },
};
const POWER_ROLES = new Set([
  "power_in", "power_out", "switch_node",
  "enable_in", "enable_out",
  "reset_in", "reset_out",
  "power_good_out", "feedback_in",
  "clock_in", "clock_out",
]);

// A few DOM shortcuts.
function el(id) { return document.getElementById(id); }
function mkSvg(tag, attrs, text) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  if (text != null) node.textContent = text;
  return node;
}
function mkEl(tag, attrs, text) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else node.setAttribute(k, v);
  }
  if (text != null) node.textContent = text;
  return node;
}

function getSlug() {
  const qs = new URLSearchParams(window.location.search);
  return qs.get("device") || qs.get("board") || null;
}

async function loadSchematic(slug) {
  if (!slug) return null;
  if (schematicCache && schematicCache.slug === slug) return schematicCache.data;
  if (fetchInFlight && fetchInFlight.slug === slug) return fetchInFlight.promise;
  const promise = fetch(`/pipeline/packs/${encodeURIComponent(slug)}/schematic`)
    .then(res => (res.ok ? res.json() : null))
    .then(data => {
      if (data) schematicCache = { slug, data };
      fetchInFlight = null;
      return data;
    })
    .catch(() => { fetchInFlight = null; return null; });
  fetchInFlight = { slug, promise };
  return promise;
}

/* ---------------------------------------------------------------------- *
 * RELATION EXTRACTION                                                    *
 * ---------------------------------------------------------------------- */

// Classify a net by its relationship to the power domain. `rail` when it's
// listed in power_rails, `gnd` when label matches ground pattern, else
// `signal`. Also resolves voltage_nominal when available so the UI can
// badge chips with "3.3V" etc.
function classifyNet(data, netLabel) {
  if (!netLabel) return { kind: "signal" };
  if (netLabel === "GND" || /^(AGND|DGND|PGND|GROUND)$/i.test(netLabel)) {
    return { kind: "gnd", label: netLabel };
  }
  const rail = (data.power_rails || {})[netLabel];
  if (rail) {
    return {
      kind: "rail",
      label: netLabel,
      voltage: rail.voltage_nominal,
      source: rail.source_refdes,
      consumers: rail.consumers || [],
      decoupling: rail.decoupling || [],
    };
  }
  return { kind: "signal", label: netLabel };
}

function relationsForComponent(data, refdes) {
  const comp = (data.components || {})[refdes];
  if (!comp) return null;
  const rails = data.power_rails || {};
  const nets = data.nets || {};
  const pins = Array.isArray(comp.pins) ? comp.pins : [];

  // Group pins by role (collapsed) — we want to display rows like
  // "VIN (3) → +5V · 12 other pins" rather than listing every individual
  // ground pin.
  const byRole = new Map();
  for (const pin of pins) {
    const r = pin.role || "signal_in";
    if (!byRole.has(r)) byRole.set(r, []);
    byRole.get(r).push(pin);
  }

  // Rails this component participates in — driven by the rails index, more
  // reliable than scanning typed_edges since Opus sometimes fails to
  // edge-classify but still populates source_refdes / consumers correctly.
  const consumed = [];
  const produced = [];
  const decoupled = [];
  for (const [label, rail] of Object.entries(rails)) {
    if (rail.source_refdes === refdes) {
      produced.push({ label, rail });
    }
    if (Array.isArray(rail.consumers) && rail.consumers.includes(refdes)
        && rail.source_refdes !== refdes) {
      consumed.push({ label, rail });
    }
    if (Array.isArray(rail.decoupling) && rail.decoupling.includes(refdes)) {
      decoupled.push({ label, rail });
    }
  }

  return {
    mode: "component",
    refdes,
    type: comp.type,
    mpn: comp.value?.mpn || comp.value?.primary,
    populated: comp.populated !== false,
    pinsByRole: byRole,
    netIndex: nets,
    data, // handy for downstream lookups
    consumed, produced, decoupled,
  };
}

function relationsForNet(data, netLabel, clickedPinRef) {
  if (!netLabel) return null;
  const net = (data.nets || {})[netLabel];
  const classification = classifyNet(data, netLabel);
  // nets[].connects is a flat list of "refdes.pin" strings. We expand it
  // into structured objects with role so the render can group consumers
  // versus decoupling versus the producer.
  const members = [];
  const raw = net?.connects || [];
  for (const token of raw) {
    const [refdes, pinNum] = token.split(".");
    if (!refdes) continue;
    const comp = (data.components || {})[refdes];
    let role = "signal_in";
    let pinName = null;
    if (comp && Array.isArray(comp.pins)) {
      const hit = comp.pins.find(p => String(p.number) === String(pinNum));
      if (hit) { role = hit.role || role; pinName = hit.name; }
    }
    members.push({
      refdes, pinNum, pinName, role,
      type: comp?.type || null,
      self: (clickedPinRef && clickedPinRef.refdes === refdes
             && String(clickedPinRef.pinNumber) === String(pinNum)),
    });
  }

  return {
    mode: "net",
    netLabel,
    classification,
    members,
    data,
    clickedPinRef,
  };
}

/* ---------------------------------------------------------------------- *
 * UI WIRING                                                              *
 * ---------------------------------------------------------------------- */

function ensureUI() {
  if (wiredUI) return;
  el("bvMinimapClose")?.addEventListener("click", hideMinimap);
  wiredUI = true;
}

function hideMinimap() {
  const mm = el("bvMinimap");
  if (mm) { mm.classList.add("hidden"); mm.setAttribute("aria-hidden", "true"); }
}
function showMinimap() {
  const mm = el("bvMinimap");
  if (mm) { mm.classList.remove("hidden"); mm.setAttribute("aria-hidden", "false"); }
}

function clearSvg() {
  const ng = el("bvMinimapNodes");
  const lg = el("bvMinimapLinks");
  if (ng) while (ng.firstChild) ng.removeChild(ng.firstChild);
  if (lg) while (lg.firstChild) lg.removeChild(lg.firstChild);
}
function clearBody() {
  const b = el("bvMinimapBody");
  if (b) b.innerHTML = "";
}

function setHeader(kind, ref, sub) {
  el("bvMinimapKind").textContent = kind;
  el("bvMinimapRef").textContent = ref || "—";
  el("bvMinimapSub").textContent = sub || "";
  const mm = el("bvMinimap");
  if (mm) mm.dataset.kind = kind === "NET" ? "net" : "component";
}

function renderEmpty(kind, ref, msg) {
  setHeader(kind, ref, "");
  clearSvg(); clearBody();
  const svg = el("bvMinimapSvg"); if (svg) svg.style.display = "none";
  const body = el("bvMinimapBody"); if (body) body.classList.add("hidden");
  const empty = el("bvMinimapEmpty");
  if (empty) { empty.style.display = "block"; empty.textContent = msg; }
}

function prepareRender() {
  clearSvg(); clearBody();
  const svg = el("bvMinimapSvg"); if (svg) svg.style.display = "block";
  const body = el("bvMinimapBody"); if (body) body.classList.remove("hidden");
  const empty = el("bvMinimapEmpty"); if (empty) empty.style.display = "none";
}

/* ---------------------------------------------------------------------- *
 * RENDER — COMPONENT MODE                                                *
 * ---------------------------------------------------------------------- */

// Small hexagon for rails inside the svg graph. Returns a <g>.
function hexRail(cx, cy, entry, opts = {}) {
  const { scale = 1, isDecouple = false, onClick = null, tooltip = null } = opts;
  const w = 62 * scale, h = 22 * scale;
  const pts = [
    [cx - w/2, cy], [cx - w/2 + 6, cy - h/2], [cx + w/2 - 6, cy - h/2],
    [cx + w/2, cy], [cx + w/2 - 6, cy + h/2], [cx - w/2 + 6, cy + h/2],
  ].map(p => p.join(",")).join(" ");
  const g = mkSvg("g", {
    class: `bv-mm-node kind-${isDecouple ? "decouples" : "rail"} ${onClick ? "clickable" : ""}`,
  });
  if (tooltip) g.appendChild(mkSvg("title", {}, tooltip));
  g.appendChild(mkSvg("polygon", {
    class: `bv-mm-rail-shape${isDecouple ? " decouples" : ""}`, points: pts,
  }));
  const lbl = entry.label.length > 11 ? entry.label.slice(0, 10) + "…" : entry.label;
  g.appendChild(mkSvg("text", { class: "bv-mm-rail-label", x: cx, y: cy - 2 }, lbl));
  const volt = entry.rail?.voltage_nominal ?? entry.voltage;
  if (volt != null && scale >= 1) {
    g.appendChild(mkSvg("text", { class: "bv-mm-rail-sub", x: cx, y: cy + 9 }, `${volt} V`));
  }
  if (onClick) g.addEventListener("click", (ev) => { ev.stopPropagation(); onClick(); });
  return g;
}

function bezier(x1, y1, x2, y2) {
  const mx = (x1 + x2) / 2;
  return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
}

// Shared function to navigate to schematic rail-focus on a given rail.
function openRailInSchematic(railLabel) {
  const railId = `rail:${railLabel}`;
  try {
    localStorage.setItem("schLayoutMode", "railfocus");
    localStorage.setItem("schSelectedRail", railId);
  } catch (_) {}
  window.dispatchEvent(new CustomEvent("schematic:focus-rail", {
    detail: { railId, railLabel },
  }));
  if (window.location.hash !== "#schematic") window.location.hash = "schematic";
}

function renderComponent(relations) {
  const { refdes, type, mpn, populated, pinsByRole, consumed, produced, decoupled, data } = relations;
  setHeader("COMP", refdes, [mpn, type].filter(Boolean).join(" · ") + (populated ? "" : " · NOSTUFF"));
  prepareRender();

  // --- SVG graph: compact IC + rail flanking ---
  const nodesG = el("bvMinimapNodes");
  const linksG = el("bvMinimapLinks");
  const cx = 180, cy = 90;

  // Center IC
  const center = mkSvg("g", { class: "bv-mm-node kind-center" });
  center.appendChild(mkSvg("rect", {
    class: "bv-mm-center-shape",
    x: cx - 34, y: cy - 17, width: 68, height: 34, rx: 5,
  }));
  center.appendChild(mkSvg("text", { class: "bv-mm-center-label", x: cx, y: cy - 2 }, refdes));
  center.appendChild(mkSvg("text", { class: "bv-mm-center-sub", x: cx, y: cy + 10 }, type || "comp"));
  nodesG.appendChild(center);

  // Rails consumed — LEFT
  const placeCol = (entries, colX, kind) => {
    const N = entries.length;
    const step = N <= 1 ? 0 : Math.min(36, 130 / (N - 1));
    entries.slice(0, 5).forEach((entry, i) => {
      const ry = cy + (i - (Math.min(N, 5) - 1) / 2) * step;
      const tip = entry.rail?.voltage_nominal != null
        ? `${entry.label} · ${entry.rail.voltage_nominal} V — ouvrir dans le schematic`
        : `${entry.label} — ouvrir dans le schematic`;
      nodesG.appendChild(hexRail(colX, ry, entry, {
        onClick: () => openRailInSchematic(entry.label),
        tooltip: tip,
      }));
      const [x1, y1, x2, y2] = kind === "powers"
        ? [colX + 31, ry, cx - 34, cy]
        : [cx + 34, cy, colX - 31, ry];
      linksG.appendChild(mkSvg("path", {
        class: `bv-mm-link bv-mm-link-${kind}`,
        d: bezier(x1, y1, x2, y2),
        "marker-end": `url(#bv-mm-arrow-${kind})`,
      }));
    });
    if (N > 5) {
      const last_y = cy + ((Math.min(N, 5) - 1) / 2 + 1) * step;
      nodesG.appendChild(mkSvg("text", {
        class: "bv-mm-more", x: colX, y: last_y, "text-anchor": "middle",
      }, `+${N - 5}`));
    }
  };
  placeCol(consumed, 55, "powers");
  placeCol(produced, 305, "produces");

  // Decoupling — row below
  if (decoupled.length) {
    const yDec = cy + 62;
    const spanW = Math.min(240, decoupled.length * 44);
    const step = decoupled.length === 1 ? 0 : spanW / (decoupled.length - 1);
    decoupled.slice(0, 6).forEach((entry, i) => {
      const x = cx + (i - (Math.min(decoupled.length, 6) - 1) / 2) * step;
      nodesG.appendChild(hexRail(x, yDec, entry, {
        scale: 0.8, isDecouple: true,
        onClick: () => openRailInSchematic(entry.label),
        tooltip: `${entry.label} — cap de découplage sur ce rail`,
      }));
      linksG.appendChild(mkSvg("path", {
        class: "bv-mm-link bv-mm-link-decouples",
        d: `M${cx},${cy + 17} L${x},${yDec - 10}`,
        "marker-end": "url(#bv-mm-arrow-decouples)",
      }));
    });
  }

  // Center-fallback text when the part has no power relations at all —
  // avoids a dangling IC box with nothing around it.
  if (consumed.length + produced.length + decoupled.length === 0) {
    nodesG.appendChild(mkSvg("text", {
      class: "bv-mm-nodata", x: 180, y: cy + 52,
    }, "Pas de rôle power identifié"));
  }

  // --- Body text: pins by role + rail consumer counts ---
  const body = el("bvMinimapBody");

  // SPOF badge if this component or any of its produced rails is a SPOF.
  // Pull from power_rails — no blast-radius on the raw graph, but the
  // presence of produced rails with many consumers is a proxy.
  const producedRailWithMost = produced
    .map(e => (e.rail.consumers || []).length)
    .reduce((a, b) => Math.max(a, b), 0);

  if (producedRailWithMost >= 3) {
    const spofRow = mkEl("div", { class: "bv-mm-row" });
    spofRow.appendChild(mkEl("span", {
      class: "bv-mm-spof",
      html: `${ICON_WARNING} alimente ${producedRailWithMost} consumer${producedRailWithMost > 1 ? "s" : ""}`,
    }));
    body.appendChild(spofRow);
  }

  // Pins section — one row per role, listing pin#s and their nets.
  const pinSection = mkEl("div", { class: "bv-mm-section" });
  pinSection.appendChild(mkEl("div", { class: "bv-mm-section-head" }, `Pins (${pinCount(pinsByRole)})`));
  const renderedRoles = [];
  // Order roles by importance for power-focused diagnosis.
  const roleOrder = [
    "power_in", "power_out", "switch_node", "enable_in", "enable_out",
    "power_good_out", "reset_in", "reset_out", "feedback_in",
    "clock_in", "clock_out", "signal_in", "signal_out", "ground",
  ];
  for (const role of roleOrder) {
    const pins = pinsByRole.get(role);
    if (!pins || !pins.length) continue;
    renderedRoles.push(role);
    pinSection.appendChild(renderPinRoleRow(role, pins, data, refdes));
  }
  // Any role not in the canonical order (future extensions) — render at end.
  for (const [role, pins] of pinsByRole) {
    if (renderedRoles.includes(role)) continue;
    pinSection.appendChild(renderPinRoleRow(role, pins, data, refdes));
  }
  body.appendChild(pinSection);

  // Rail details — for each consumed/produced rail, list the IC peers
  // ("other big consumers on this rail") so the tech knows what else
  // shares the signal. Excludes caps from the peer list (they live in
  // the dedicated decouples section above).
  if (consumed.length) body.appendChild(renderRailContextSection("Alimenté par", consumed, refdes, data));
  if (produced.length) body.appendChild(renderRailContextSection("Alimente", produced, refdes, data));
  if (decoupled.length) {
    const s = mkEl("div", { class: "bv-mm-section" });
    s.appendChild(mkEl("div", { class: "bv-mm-section-head" }, `Découple (${decoupled.length})`));
    const row = mkEl("div", { class: "bv-mm-row" });
    decoupled.slice(0, 8).forEach(e => {
      const chip = mkEl("span", { class: "bv-mm-chip rail" }, e.label);
      chip.addEventListener("click", () => openRailInSchematic(e.label));
      row.appendChild(chip);
    });
    if (decoupled.length > 8) row.appendChild(mkEl("span", { class: "bv-mm-more" }, `+${decoupled.length - 8}`));
    s.appendChild(row);
    body.appendChild(s);
  }
}

function pinCount(byRole) {
  let n = 0; for (const v of byRole.values()) n += v.length; return n;
}

// Render a single pin-role row like:  ◄ VIN  3 → +5V  (16 autres sur ce net)
function renderPinRoleRow(role, pins, data, selfRefdes) {
  const meta = ROLE_META[role] || { label: role.toUpperCase(), glyph: "·" };
  const row = mkEl("div", { class: "bv-mm-row" });
  row.appendChild(mkEl("span", { class: "bv-mm-pinlabel" }, `${meta.glyph} ${meta.label}`));
  // For ground, collapse into "GND · 4 pins" to avoid bloat.
  if (role === "ground") {
    const pinNums = pins.map(p => p.number).join(", ");
    row.appendChild(mkEl("span", { class: "bv-mm-pinnum" }, `${pins.length} pin${pins.length > 1 ? "s" : ""}`));
    row.appendChild(mkEl("span", { class: "bv-mm-muted" }, `(${pinNums})`));
    return row;
  }
  // Group pins by net_label so identical nets appear once: "2, 4 → +1V8"
  const byNet = new Map();
  for (const p of pins) {
    const nl = p.net_label || "(no-net)";
    if (!byNet.has(nl)) byNet.set(nl, []);
    byNet.get(nl).push(p.number);
  }
  const entries = [...byNet.entries()];
  entries.forEach(([netLabel, pinNums], i) => {
    if (i > 0) row.appendChild(mkEl("span", { class: "bv-mm-muted" }, "·"));
    row.appendChild(mkEl("span", { class: "bv-mm-pinnum" }, pinNums.join(",")));
    row.appendChild(mkEl("span", { class: "bv-mm-arrow" }, "→"));
    const cls = classifyNet(data, netLabel);
    const chip = mkEl("span", {
      class: `bv-mm-chip ${cls.kind === "rail" ? "rail" : cls.kind === "gnd" ? "gnd" : "signal"}`,
    }, netLabel);
    if (cls.kind === "rail") {
      chip.addEventListener("click", () => openRailInSchematic(netLabel));
    } else {
      chip.addEventListener("click", () => showNetFromLabel(netLabel));
    }
    row.appendChild(chip);
    // Peer count on this net — total pins minus pins belonging to the
    // component we're currently viewing.
    const netConnects = data.nets?.[netLabel]?.connects || [];
    const selfCount = netConnects.filter(tok => tok.startsWith(selfRefdes + ".")).length;
    const peerCount = Math.max(0, netConnects.length - selfCount);
    if (peerCount > 0) {
      row.appendChild(mkEl("span", { class: "bv-mm-muted" }, `(${peerCount} autres)`));
    }
  });
  return row;
}

function renderRailContextSection(title, railEntries, selfRef, data) {
  const section = mkEl("div", { class: "bv-mm-section" });
  section.appendChild(mkEl("div", { class: "bv-mm-section-head" }, `${title} (${railEntries.length})`));
  railEntries.forEach(entry => {
    const row = mkEl("div", { class: "bv-mm-row" });
    const chip = mkEl("span", { class: "bv-mm-chip rail" }, entry.label);
    chip.addEventListener("click", () => openRailInSchematic(entry.label));
    row.appendChild(chip);
    // Count of IC peers on the rail (excludes caps) — what else runs off
    // this rail?
    const peers = (entry.rail.consumers || []).filter(r => r !== selfRef);
    const source = entry.rail.source_refdes;
    if (source && source !== selfRef) {
      const srcChip = mkEl("span", { class: "bv-mm-chip comp producer" }, `src: ${source}`);
      row.appendChild(srcChip);
    }
    const shownPeers = peers.slice(0, 5);
    shownPeers.forEach(r => {
      const comp = (data.components || {})[r];
      const cchip = mkEl("span", { class: "bv-mm-chip comp" }, r);
      if (comp?.type) cchip.title = `${r} · ${comp.type}${comp.value?.mpn ? " · " + comp.value.mpn : ""}`;
      row.appendChild(cchip);
    });
    if (peers.length > 5) row.appendChild(mkEl("span", { class: "bv-mm-more" }, `+${peers.length - 5}`));
    if (peers.length === 0 && !source) {
      row.appendChild(mkEl("span", { class: "bv-mm-muted" }, "aucun pair listé"));
    }
    section.appendChild(row);
  });
  return section;
}

/* ---------------------------------------------------------------------- *
 * RENDER — NET MODE (pin click)                                          *
 * ---------------------------------------------------------------------- */

function renderNet(relations) {
  const { netLabel, classification, members, clickedPinRef, data } = relations;
  const subBits = [];
  if (classification.kind === "rail" && classification.voltage != null) subBits.push(`${classification.voltage} V`);
  if (classification.kind === "rail") subBits.push("rail");
  else if (classification.kind === "gnd") subBits.push("ground");
  else subBits.push("signal");
  subBits.push(`${members.length} pin${members.length > 1 ? "s" : ""}`);
  setHeader("NET", netLabel, subBits.join(" · "));
  prepareRender();

  // --- SVG: net hexagon centered, member chips arranged radially ---
  const nodesG = el("bvMinimapNodes");
  const linksG = el("bvMinimapLinks");
  const cx = 180, cy = 95;

  const netEntry = { label: netLabel, rail: classification.kind === "rail" ? { voltage_nominal: classification.voltage } : null };
  const netNode = hexRail(cx, cy, netEntry, {
    scale: 1.15,
    isDecouple: classification.kind === "gnd",
    onClick: classification.kind === "rail" ? () => openRailInSchematic(netLabel) : null,
    tooltip: classification.kind === "rail" ? "Ouvrir dans le schematic en focus rail" : null,
  });
  nodesG.appendChild(netNode);

  // Members around the hex — sort by role importance.
  const rolePriority = {
    power_out: 0, switch_node: 0,        // producers first
    power_in: 1,
    enable_in: 2, enable_out: 2, power_good_out: 2, reset_in: 2, reset_out: 2,
    feedback_in: 3,
    clock_in: 4, clock_out: 4,
    signal_in: 5, signal_out: 5,
    ground: 9,
  };
  const sorted = [...members].sort((a, b) => {
    const pa = rolePriority[a.role] ?? 6, pb = rolePriority[b.role] ?? 6;
    if (pa !== pb) return pa - pb;
    return a.refdes.localeCompare(b.refdes, undefined, { numeric: true });
  });

  // Place up to 8 chips in a semicircle below the hex.
  const visible = sorted.slice(0, 8);
  const R = 62;
  const arcStart = Math.PI * 0.15, arcEnd = Math.PI * 0.85;
  visible.forEach((m, i) => {
    const t = visible.length === 1 ? 0.5 : i / (visible.length - 1);
    const theta = arcStart + (arcEnd - arcStart) * t;
    const mx = cx - Math.cos(theta) * R * 1.8;
    const my = cy + Math.sin(theta) * R;
    const clamped = Math.max(28, Math.min(332, mx));
    // A small rect chip
    const isProd = (m.role === "power_out" || m.role === "switch_node");
    const chipClass = m.self
      ? "bv-mm-rail-shape"  // re-use rail-shape for colored-ring look
      : "bv-mm-rail-shape";
    const g = mkSvg("g", {
      class: `bv-mm-node ${m.self ? "kind-self" : isProd ? "kind-producer" : "kind-comp"} clickable`,
    });
    g.appendChild(mkSvg("title", {}, `${m.refdes}.${m.pinNum} · ${m.pinName || m.role}${m.type ? " · " + m.type : ""}`));
    const w = 44, h = 18;
    g.appendChild(mkSvg("rect", {
      x: clamped - w/2, y: my - h/2, width: w, height: h, rx: 3,
      fill: m.self ? "rgba(245,158,11,.16)"
            : isProd ? "rgba(245,158,11,.12)"
            : "rgba(56,189,248,.12)",
      stroke: m.self ? "oklch(0.82 0.16 75)"
            : isProd ? "oklch(0.78 0.16 75)"
            : "oklch(0.78 0.14 210)",
      "stroke-width": m.self ? "1.8" : "1.2",
    }));
    g.appendChild(mkSvg("text", {
      class: "bv-mm-rail-label", x: clamped, y: my - 1,
      fill: m.self ? "oklch(0.92 0.14 75)" : isProd ? "oklch(0.88 0.15 75)" : "oklch(0.88 0.13 210)",
    }, `${m.refdes}.${m.pinNum}`));
    g.appendChild(mkSvg("text", {
      class: "bv-mm-rail-sub", x: clamped, y: my + 8,
      fill: "var(--text-3)",
    }, m.pinName || (ROLE_META[m.role]?.label ?? "")));
    // Flyline from net hex to this chip
    linksG.appendChild(mkSvg("path", {
      class: `bv-mm-link bv-mm-link-${isProd ? "produces" : "powers"}`,
      d: `M${cx},${cy} L${clamped},${my}`,
    }));
    nodesG.appendChild(g);
  });
  if (members.length > visible.length) {
    nodesG.appendChild(mkSvg("text", {
      class: "bv-mm-more", x: 180, y: 178, "text-anchor": "middle",
    }, `+${members.length - visible.length} autres pins sur ${netLabel}`));
  }

  // --- Body: grouped list by role ---
  const body = el("bvMinimapBody");

  // SPOF / rail context banner
  if (classification.kind === "rail") {
    const ctx = mkEl("div", { class: "bv-mm-row" });
    ctx.appendChild(mkEl("span", { class: "bv-mm-pinlabel" }, "Rail"));
    const chip = mkEl("span", { class: "bv-mm-chip rail" }, "Ouvrir dans le schematic");
    chip.addEventListener("click", () => openRailInSchematic(netLabel));
    ctx.appendChild(chip);
    if (classification.source) {
      const src = mkEl("span", { class: "bv-mm-chip comp producer" }, `src: ${classification.source}`);
      ctx.appendChild(src);
    }
    body.appendChild(ctx);
  }

  // Pins grouped by role
  const byRole = new Map();
  for (const m of members) {
    if (!byRole.has(m.role)) byRole.set(m.role, []);
    byRole.get(m.role).push(m);
  }
  const roleOrder = [
    "power_out", "switch_node", "power_in", "enable_in", "enable_out",
    "power_good_out", "reset_in", "reset_out", "feedback_in",
    "clock_in", "clock_out", "signal_in", "signal_out", "ground",
  ];
  for (const role of roleOrder) {
    const arr = byRole.get(role);
    if (!arr || !arr.length) continue;
    body.appendChild(renderNetRoleRow(role, arr, data));
  }
  for (const [role, arr] of byRole) {
    if (roleOrder.includes(role)) continue;
    body.appendChild(renderNetRoleRow(role, arr, data));
  }
}

function renderNetRoleRow(role, members, data) {
  const meta = ROLE_META[role] || { label: role.toUpperCase(), glyph: "·" };
  const section = mkEl("div", { class: "bv-mm-section" });
  section.appendChild(mkEl("div", { class: "bv-mm-section-head" },
    `${meta.glyph} ${meta.label} (${members.length})`));
  const row = mkEl("div", { class: "bv-mm-row" });
  // Consolidate by refdes so an IC with multiple pins on the same net shows
  // once: "U14 · pins 3,5"
  const byRef = new Map();
  for (const m of members) {
    if (!byRef.has(m.refdes)) byRef.set(m.refdes, { refdes: m.refdes, type: m.type, pins: [], self: false });
    byRef.get(m.refdes).pins.push(m.pinNum);
    if (m.self) byRef.get(m.refdes).self = true;
  }
  const entries = [...byRef.values()].slice(0, 14);
  entries.forEach(e => {
    const type = e.type || "";
    const isCap = type === "capacitor";
    const cls = e.self ? "self" : (role === "power_out" || role === "switch_node") ? "producer" : "";
    const chip = mkEl("span", {
      class: `bv-mm-chip ${isCap ? "cap" : "comp"} ${cls}`,
      title: `${e.refdes}${type ? " · " + type : ""} — pin${e.pins.length > 1 ? "s" : ""} ${e.pins.join(", ")}`,
    }, e.pins.length === 1 ? `${e.refdes}.${e.pins[0]}` : `${e.refdes}·${e.pins.length}`);
    row.appendChild(chip);
  });
  if (byRef.size > entries.length) {
    row.appendChild(mkEl("span", { class: "bv-mm-more" }, `+${byRef.size - entries.length}`));
  }
  section.appendChild(row);
  return section;
}

// Switch minimap to a net-centric view given only a net label (no clicked
// pin context). Used when clicking a chip inside the component-mode body.
function showNetFromLabel(netLabel) {
  const data = schematicCache?.data;
  if (!data) return;
  const relations = relationsForNet(data, netLabel, null);
  if (!relations) return;
  renderNet(relations);
}

/* ---------------------------------------------------------------------- *
 * DISPATCH                                                               *
 * ---------------------------------------------------------------------- */

async function handleSelection(detail) {
  ensureUI();
  const refdes = detail?.refdes;
  if (refdes) lastSelection = detail;
  if (!enabled) { hideMinimap(); return; }
  if (!refdes) { hideMinimap(); return; }
  showMinimap();

  const slug = getSlug();
  if (!slug) { renderEmpty("COMP", refdes, "Pas de device dans l'URL."); return; }
  // Skeleton while loading.
  setHeader("COMP", refdes, "chargement…");
  clearSvg(); clearBody();
  const svg = el("bvMinimapSvg"); if (svg) svg.style.display = "none";
  const empty = el("bvMinimapEmpty"); if (empty) { empty.style.display = "block"; empty.textContent = "Chargement…"; }

  const data = await loadSchematic(slug);
  if (!data) { renderEmpty("COMP", refdes, "Pas de schematic ingéré pour ce device."); return; }

  // Pin-click path — the user clicked on a specific pin of the component.
  // Show the NET of that pin with all its other members.
  if (detail.pinNet) {
    const relations = relationsForNet(data, detail.pinNet, {
      refdes, pinNumber: detail.pinNumber,
    });
    if (relations && relations.members.length) {
      renderNet(relations);
      return;
    }
    // Fall through to component mode if the net isn't known.
  }

  const relations = relationsForComponent(data, refdes);
  if (!relations) { renderEmpty("COMP", refdes, `Inconnu dans le schematic : ${refdes}`); return; }
  renderComponent(relations);
}

window.addEventListener("bv:selection", (ev) => {
  handleSelection(ev.detail || {});
});

window.addEventListener("bv:minimap-toggle", (ev) => {
  enabled = Boolean(ev.detail?.enabled);
  if (!enabled) { hideMinimap(); return; }
  if (lastSelection?.refdes) handleSelection(lastSelection);
});
