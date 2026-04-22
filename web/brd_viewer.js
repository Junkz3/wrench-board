// Board fixture selection via `?board=<slug>` query param. Default = MNT Reform.
// Known slugs map to files under /boards/. Unknown slugs fall back to default.
const BOARD_FIXTURES = {
  'mnt-reform':     '/boards/mnt-reform-motherboard.kicad_pcb',
  'mnt-reform-brd': '/boards/mnt-reform-motherboard.brd',
  'bilayer':        '/boards/bilayer_minimal.brd',
  // 0BSD reference fixture from whitequark/kicad-boardview — 245 parts,
  // 165 top + 80 bottom, canonical production-grade bilayer test board.
  'whitequark':     '/boards/whitequark-example.brd',
};
const DEFAULT_BOARD = 'mnt-reform';
function resolveBoardUrl() {
  const slug = new URLSearchParams(window.location.search).get('board');
  return BOARD_FIXTURES[slug] || BOARD_FIXTURES[DEFAULT_BOARD];
}

const BRD_URL  = resolveBoardUrl();
const PARSE_URL = '/api/board/parse';

const state = {
  board: null,
  partsSorted: null,
  partBodyBboxes: null,
  pinsByNet: null,        // Map<netName, number[]>  pin indices grouped by net
  netCategory: null,      // Map<netName, 'power' | 'ground' | 'signal'>
  selectedPinIdx: null,   // currently highlighted pin (index into board.pins)
  hoveredPinIdx: null,    // pin under the cursor (for click-affordance outline)
};

const RATNEST_MAX_PINS = 50;  // skip drawing fly-lines for huge nets (GND has ~500)
const PIN_HIT_TOLERANCE_PX = 4;  // extra margin around the pad rect for easier clicks at low zoom

// whitequark/kicad-boardview (for BRD2 / Test_Link) uses module.GetBoundingBox()
// which includes silkscreen + reference text + value text, so PART bboxes from
// those sources are ~5x bigger than the actual component body. Our native
// KiCad parser (source_format='kicad_pcb') already emits pads-only bboxes in
// board coords, so no correction is needed there — see needsBodyBboxCorrection.
function computeBodyBbox(part, pinsById) {
  const pins = (part.pin_refs || []).map(i => pinsById[i]).filter(Boolean);
  if (pins.length === 0) {
    return part.bbox;
  }
  let x0 = pins[0].pos.x, x1 = pins[0].pos.x;
  let y0 = pins[0].pos.y, y1 = pins[0].pos.y;
  for (const p of pins) {
    if (p.pos.x < x0) x0 = p.pos.x;
    if (p.pos.x > x1) x1 = p.pos.x;
    if (p.pos.y < y0) y0 = p.pos.y;
    if (p.pos.y > y1) y1 = p.pos.y;
  }
  // Pad with a fixed 15 mils (~0.4 mm) so 2-pad passives (0603/1210) stay
  // visible in the axis orthogonal to the pad separation, and single-pin
  // mounting holes render as a 30x30 mil dot. No percentage padding — it
  // inflates big connectors (J3, U1, etc.) visibly beyond their real size.
  const pad = 15;
  return [
    { x: x0 - pad, y: y0 - pad },
    { x: x1 + pad, y: y1 + pad },
  ];
}

// Source formats that need the pin-derived bbox correction. KiCad native emits
// pads-only bboxes directly; BRD2 / Test_Link emit inflated module bboxes.
function needsBodyBboxCorrection(board) {
  return board.source_format !== 'kicad_pcb';
}

// Map part.refdes -> body bbox (pin-derived). Computed once per board when
// the source format needs the correction; returns null otherwise.
function computeAllBodyBboxes(board) {
  if (!needsBodyBboxCorrection(board)) return null;
  const pinsById = board.pins || [];
  const out = new Map();
  for (const p of board.parts || []) {
    out.set(p.refdes, computeBodyBbox(p, pinsById));
  }
  return out;
}

// Classify each net as 'power' (+3V3, +5V, VBAT, VCC…), 'ground' (GND, VSS…),
// or 'signal' (everything else) so we can colour pins distinctively — a
// technician spots power / ground topology at a glance instead of hunting
// through a uniform cyan sea.
function computeNetCategory(board) {
  const out = new Map();
  for (const n of board.nets || []) {
    if (n.is_power) out.set(n.name, 'power');
    else if (n.is_ground) out.set(n.name, 'ground');
    else out.set(n.name, 'signal');
  }
  return out;
}

// Index pins by net name so we can highlight / trace a whole net from one click.
function computePinsByNet(board) {
  const out = new Map();
  const pins = board.pins || [];
  for (let i = 0; i < pins.length; i++) {
    const net = pins[i].net;
    if (!net) continue;
    if (!out.has(net)) out.set(net, []);
    out.get(net).push(i);
  }
  return out;
}

// Hit-test: given screen-px coords, return the index of the pin under the cursor.
// Considers each pin's actual pad bbox (size × zoom), with a small tolerance margin
// so very small pads remain clickable at low zoom. Among overlapping hits (dense
// clusters) pick the smallest pad — it's visually on top in our draw order.
// Only pins on the active side (plus BOTH) are considered.
function hitTestPin(sx, sy, tolerancePx = PIN_HIT_TOLERANCE_PX) {
  if (!state.board) return null;
  const pins = state.board.pins || [];
  const bb = outlineBbox(state.board);
  const boardW = bb.x1 + bb.x0;
  let bestIdx = null;
  let bestArea = Infinity;
  for (let i = 0; i < pins.length; i++) {
    const pin = pins[i];
    if (pin.layer !== LAYER_BOTH) {
      if (activeSide === LAYER_TOP    && pin.layer !== LAYER_TOP)    continue;
      if (activeSide === LAYER_BOTTOM && pin.layer !== LAYER_BOTTOM) continue;
    }
    const p = milsToScreen(pin.pos.x, pin.pos.y, boardW);
    const sizeMils = pin.pad_size || [30, 30];
    const halfW = Math.max(sizeMils[0] * vp.zoom / 2, 2) + tolerancePx;
    const halfH = Math.max(sizeMils[1] * vp.zoom / 2, 2) + tolerancePx;
    if (sx >= p.x - halfW && sx <= p.x + halfW && sy >= p.y - halfH && sy <= p.y + halfH) {
      const area = halfW * halfH;
      if (area < bestArea) {
        bestArea = area;
        bestIdx = i;
      }
    }
  }
  return bestIdx;
}

// Sort parts by descending bbox area so big packages (SoM connectors, BGA SoCs)
// are drawn first and dense clusters of small passives on top of them remain
// visible. Uses bodyBboxes when provided (BRD2 / Test_Link sources), otherwise
// falls back to part.bbox (already pads-only for kicad_pcb source).
function sortPartsByAreaDesc(parts, bodyBboxes) {
  const bboxOf = (p) => (bodyBboxes && bodyBboxes.get(p.refdes)) || p.bbox;
  return [...parts].sort((a, b) => {
    const ab = bboxOf(a);
    const bb = bboxOf(b);
    const aw = ab[1].x - ab[0].x;
    const ah = ab[1].y - ab[0].y;
    const bw = bb[1].x - bb[0].x;
    const bh = bb[1].y - bb[0].y;
    return (bw * bh) - (aw * ah);
  });
}

// layer IntFlag values
const LAYER_TOP    = 1;
const LAYER_BOTTOM = 2;
const LAYER_BOTH   = 3;

// viewport: mils-to-pixel transform
const vp = { panX: 0, panY: 0, zoom: 1 };

// render state
let canvas = null, ctx = null;
let dirty = false;
let animFrame = null;
let activeSide = LAYER_TOP;   // LAYER_TOP or LAYER_BOTTOM
let cursorMils = null;        // {x, y} or null
let showAnnotations = true;   // silkscreen labels / logos (0-pin footprints)

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// --- board bbox ---
function outlineBbox(board) {
  const pts = board.outline;
  if (!pts || pts.length === 0) return { x0: 0, y0: 0, x1: 1000, y1: 1000 };
  let x0 = pts[0].x, y0 = pts[0].y, x1 = pts[0].x, y1 = pts[0].y;
  for (const p of pts) {
    if (p.x < x0) x0 = p.x;
    if (p.y < y0) y0 = p.y;
    if (p.x > x1) x1 = p.x;
    if (p.y > y1) y1 = p.y;
  }
  return { x0, y0, x1, y1 };
}

// --- fit viewport to board outline bbox, 8% padding ---
function fitToBoard() {
  if (!canvas || !state.board) return;
  const bb = outlineBbox(state.board);
  const bw = bb.x1 - bb.x0;
  const bh = bb.y1 - bb.y0;
  const cw = canvas.clientWidth;
  const ch = canvas.clientHeight;
  if (bw <= 0 || bh <= 0 || cw <= 0 || ch <= 0) return;
  const pad = 0.08;
  const scaleX = (cw * (1 - pad * 2)) / bw;
  const scaleY = (ch * (1 - pad * 2)) / bh;
  vp.zoom = Math.min(scaleX, scaleY);
  vp.panX = (cw - bw * vp.zoom) / 2 - bb.x0 * vp.zoom;
  vp.panY = (ch - bh * vp.zoom) / 2 - bb.y0 * vp.zoom;
  requestRedraw();
}

// --- coordinate helpers ---
// milsToScreen: apply pan/zoom, then mirror if on bottom side
function milsToScreen(mx, my, boardW) {
  if (activeSide === LAYER_BOTTOM) {
    // X-axis mirror: reflect around board centre x
    mx = boardW - mx;
  }
  return {
    x: mx * vp.zoom + vp.panX,
    y: my * vp.zoom + vp.panY,
  };
}

function screenToMils(sx, sy) {
  const bb = outlineBbox(state.board);
  const boardW = bb.x1 - bb.x0 + bb.x0 * 2; // full width in mils coords
  let mx = (sx - vp.panX) / vp.zoom;
  const my = (sy - vp.panY) / vp.zoom;
  if (activeSide === LAYER_BOTTOM) {
    mx = boardW - mx;
  }
  return { x: mx, y: my };
}

// --- drawing ---
function draw() {
  animFrame = null;
  dirty = false;
  if (!canvas || !ctx || !state.board) return;

  const dpr = window.devicePixelRatio || 1;
  const cw  = canvas.clientWidth;
  const ch  = canvas.clientHeight;

  // Resize backing store if needed
  if (canvas.width !== Math.round(cw * dpr) || canvas.height !== Math.round(ch * dpr)) {
    canvas.width  = Math.round(cw * dpr);
    canvas.height = Math.round(ch * dpr);
  }

  // HiDPI base transform — everything drawn in CSS pixels, DPR applied once here
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  // background
  ctx.fillStyle = cssVar('--bg') || '#0a1120';
  ctx.fillRect(0, 0, cw, ch);

  const board = state.board;
  const bb    = outlineBbox(board);
  // board width in mils (used for mirror transform)
  const boardW = bb.x1 + bb.x0;  // mirror: x' = boardW - x

  // ---- outline ----
  const outline = board.outline;
  if (outline && outline.length > 1) {
    ctx.beginPath();
    const p0 = milsToScreen(outline[0].x, outline[0].y, boardW);
    ctx.moveTo(p0.x, p0.y);
    for (let i = 1; i < outline.length; i++) {
      const p = milsToScreen(outline[i].x, outline[i].y, boardW);
      ctx.lineTo(p.x, p.y);
    }
    ctx.closePath();
    ctx.strokeStyle = cssVar('--text-3') || '#6e7d96';
    ctx.lineWidth   = 1;
    ctx.stroke();
  }

  // ---- parts (skip 0-pin footprints — those are silkscreen annotations
  //                drawn separately below as labels) ----
  const parts = state.partsSorted || board.parts || [];
  ctx.lineWidth = 1;
  for (const part of parts) {
    if (!part.pin_refs || part.pin_refs.length === 0) continue;
    // layer filter: skip parts that don't belong to the active side
    if (part.layer !== LAYER_BOTH) {
      if (activeSide === LAYER_TOP    && part.layer !== LAYER_TOP)    continue;
      if (activeSide === LAYER_BOTTOM && part.layer !== LAYER_BOTTOM) continue;
    }
    // Prefer the pin-derived body bbox (tighter, matches physical component)
    // over the BRD2 bbox which is inflated by silkscreen + ref/value text.
    const bbox = state.partBodyBboxes?.get(part.refdes) || part.bbox;
    if (!bbox || bbox.length < 2) continue;

    const a = milsToScreen(bbox[0].x, bbox[0].y, boardW);
    const b = milsToScreen(bbox[1].x, bbox[1].y, boardW);
    const rx = Math.min(a.x, b.x);
    const ry = Math.min(a.y, b.y);
    const rw = Math.abs(b.x - a.x);
    const rh = Math.abs(b.y - a.y);

    ctx.fillStyle   = 'rgba(56,189,248,0.12)';
    ctx.strokeStyle = 'rgba(56,189,248,0.7)';
    ctx.fillRect(rx, ry, rw, rh);
    ctx.strokeRect(rx, ry, rw, rh);
  }

  // ---- pins ----
  // Each pin is drawn at its real pad size and shape (from KiCad).
  // Rects are axis-aligned (part rotation not applied to the pad rect yet —
  // accepted imprecision for rotated packages at MVP scope).
  const pins = board.pins || [];
  const pinFillDefault   = 'rgba(169, 182, 204, 0.9)';   // --text-2 at slight transparency
  const pinStrokeDefault = 'rgba(230, 237, 247, 1)';     // --text, sharp edge
  const pinFillDim       = 'rgba(169, 182, 204, 0.22)';
  const pinStrokeDim     = 'rgba(169, 182, 204, 0.35)';
  const pinFillNet       = 'rgba(52, 211, 153, 0.95)';   // --emerald (selected)
  const pinStrokeNet     = 'rgba(160, 240, 200, 1)';
  // Net-category colours (applied only when no net is explicitly selected —
  // the emerald selection override takes priority to keep the trace readable)
  const pinFillPower     = 'rgba(245, 158, 11, 0.90)';   // --amber
  const pinStrokePower   = 'rgba(252, 180, 60, 1)';
  const pinFillGround    = 'rgba(110, 125, 150, 0.55)';  // dim gray (GND is everywhere)
  const pinStrokeGround  = 'rgba(140, 155, 180, 0.7)';

  // Determine the selected net (if any) from state.selectedPinIdx
  const selectedPin = state.selectedPinIdx != null ? pins[state.selectedPinIdx] : null;
  const selectedNet = selectedPin && selectedPin.net ? selectedPin.net : null;
  const netPinSet = selectedNet ? new Set(state.pinsByNet?.get(selectedNet) || []) : null;

  ctx.lineWidth = 1;
  for (let i = 0; i < pins.length; i++) {
    const pin = pins[i];
    if (pin.layer !== LAYER_BOTH) {
      if (activeSide === LAYER_TOP    && pin.layer !== LAYER_TOP)    continue;
      if (activeSide === LAYER_BOTTOM && pin.layer !== LAYER_BOTTOM) continue;
    }
    const s = milsToScreen(pin.pos.x, pin.pos.y, boardW);

    // pad_size is in mils, convert to screen via zoom. Fallback to 30x30 mils
    // (~0.75mm) for pins lacking size (BRD2 / Test_Link don't carry it).
    const sizeMils = pin.pad_size || [30, 30];
    const sw = sizeMils[0] * vp.zoom;
    const sh = sizeMils[1] * vp.zoom;
    // Clamp to at least 2 px so pins stay visible when zoomed out hard.
    const w = Math.max(sw, 2);
    const h = Math.max(sh, 2);

    // Pin colour: selected-net override > category > default
    if (netPinSet) {
      if (netPinSet.has(i)) {
        ctx.fillStyle = pinFillNet;
        ctx.strokeStyle = pinStrokeNet;
      } else {
        ctx.fillStyle = pinFillDim;
        ctx.strokeStyle = pinStrokeDim;
      }
    } else {
      const category = pin.net ? state.netCategory?.get(pin.net) : null;
      if (category === 'power') {
        ctx.fillStyle   = pinFillPower;
        ctx.strokeStyle = pinStrokePower;
      } else if (category === 'ground') {
        ctx.fillStyle   = pinFillGround;
        ctx.strokeStyle = pinStrokeGround;
      } else {
        ctx.fillStyle   = pinFillDefault;
        ctx.strokeStyle = pinStrokeDefault;
      }
    }

    // Apply this pin's own pad rotation — each pad carries its own orientation
    // independent of the footprint's placement rotation. On multi-row packages
    // (QFP / BGA) the pads on the sides are rotated 90° relative to the
    // top/bottom pads, so using the footprint rotation for every pin is wrong.
    // KiCad reports CCW-positive angles in an X-right/Y-up math frame; canvas
    // is CW-positive in an X-right/Y-down frame — invert the sign.
    const rotDeg = pin.pad_rotation_deg || 0;
    const rotRad = -rotDeg * Math.PI / 180;

    const shape = pin.pad_shape || 'circle';
    ctx.save();
    ctx.translate(s.x, s.y);
    if (rotDeg) ctx.rotate(rotRad);

    if (shape === 'rect' || shape === 'roundrect' || shape === 'trapezoid') {
      ctx.fillRect(-w / 2, -h / 2, w, h);
      if (vp.zoom >= 1.5) ctx.strokeRect(-w / 2, -h / 2, w, h);
    } else if (shape === 'oval') {
      ctx.beginPath();
      ctx.ellipse(0, 0, w / 2, h / 2, 0, 0, Math.PI * 2);
      ctx.fill();
      if (vp.zoom >= 1.5) ctx.stroke();
    } else {
      // circle / custom / fallback (rotation-invariant)
      const r = Math.max(w, h) / 2;
      ctx.beginPath();
      ctx.arc(0, 0, r, 0, Math.PI * 2);
      ctx.fill();
      if (vp.zoom >= 1.5) ctx.stroke();
    }

    // Hover affordance — same shape as the pad, inflated by a 3 px gap.
    if (i === state.hoveredPinIdx && i !== state.selectedPinIdx) {
      ctx.strokeStyle = 'rgba(56, 189, 248, 0.95)';   // --cyan
      ctx.lineWidth = 1.5;
      const gap = 3;
      if (shape === 'rect' || shape === 'roundrect' || shape === 'trapezoid') {
        ctx.strokeRect(-w / 2 - gap, -h / 2 - gap, w + gap * 2, h + gap * 2);
      } else if (shape === 'oval') {
        ctx.beginPath();
        ctx.ellipse(0, 0, w / 2 + gap, h / 2 + gap, 0, 0, Math.PI * 2);
        ctx.stroke();
      } else {
        const ringR = Math.max(w, h) / 2 + gap;
        ctx.beginPath();
        ctx.arc(0, 0, ringR, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.lineWidth = 1;
    }

    ctx.restore();
  }

  // ---- ratnest fly-lines (selected net only, skip huge nets like GND) ----
  if (selectedNet && netPinSet && netPinSet.size <= RATNEST_MAX_PINS && state.selectedPinIdx != null) {
    const anchor = pins[state.selectedPinIdx];
    const anchorScr = milsToScreen(anchor.pos.x, anchor.pos.y, boardW);
    ctx.strokeStyle = 'rgba(52, 211, 153, 0.55)';   // --emerald ~55% alpha
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    for (const pinIdx of netPinSet) {
      if (pinIdx === state.selectedPinIdx) continue;
      const other = pins[pinIdx];
      const scr = milsToScreen(other.pos.x, other.pos.y, boardW);
      ctx.moveTo(anchorScr.x, anchorScr.y);
      ctx.lineTo(scr.x, scr.y);
    }
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // ---- silkscreen annotations (0-pin footprints: logos, labels, badges) ----
  // Rendered as text at the footprint centre, respecting rotation. Matches
  // what is physically printed on the PCB silkscreen layer.
  if (showAnnotations) {
    ctx.fillStyle = cssVar('--text-3') || '#6e7d96';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    for (const part of parts) {
      if (part.pin_refs && part.pin_refs.length > 0) continue;  // only 0-pin
      if (part.layer !== LAYER_BOTH) {
        if (activeSide === LAYER_TOP    && part.layer !== LAYER_TOP)    continue;
        if (activeSide === LAYER_BOTTOM && part.layer !== LAYER_BOTTOM) continue;
      }
      const bbox = part.bbox;
      if (!bbox || bbox.length < 2) continue;

      const label = (part.value || part.refdes || '').replace(/^LABEL_|^LOGO_/, '');
      if (!label) continue;

      const cxMils = (bbox[0].x + bbox[1].x) / 2;
      const cyMils = (bbox[0].y + bbox[1].y) / 2;
      const wMils = Math.abs(bbox[1].x - bbox[0].x);
      const hMils = Math.abs(bbox[1].y - bbox[0].y);
      const center = milsToScreen(cxMils, cyMils, boardW);

      // Fit text to the LONG axis of the bbox (the KiCad footprint rotation
      // is already implicit in the bbox proportions — portrait bboxes want
      // rotated text to match the side they're printed along).
      const landscape = wMils >= hMils;
      const longPx  = (landscape ? wMils : hMils) * vp.zoom;
      const shortPx = (landscape ? hMils : wMils) * vp.zoom;
      if (longPx < 14) continue;  // too small to render readably

      // Font size: fit to the short axis, clamped by the long axis / char count
      let fontSize = Math.min(shortPx * 0.7, (longPx * 1.5) / Math.max(label.length, 1));
      fontSize = Math.max(8, Math.min(fontSize, 48));
      ctx.font = `500 ${fontSize}px 'JetBrains Mono', ui-monospace, monospace`;

      ctx.save();
      ctx.translate(center.x, center.y);
      if (!landscape) ctx.rotate(-Math.PI / 2);
      ctx.fillText(label, 0, 0);
      ctx.restore();
    }
  }
}

function requestRedraw() {
  if (dirty) return;
  dirty = true;
  animFrame = requestAnimationFrame(draw);
}

// --- toolbar DOM helpers ---
function updateZoomReadout(toolbar) {
  const el = toolbar.querySelector('.brd-zoom');
  if (el) el.textContent = vp.zoom.toFixed(2) + '×';
}

function updateCursorBadge(badge) {
  const el = badge.querySelector('.brd-cursor');
  if (!el) return;
  if (cursorMils) {
    el.textContent = `x: ${cursorMils.x.toFixed(0)}  y: ${cursorMils.y.toFixed(0)}`;
  } else {
    el.textContent = '—';
  }
}

function updateNetReadout(toolbar) {
  const el = toolbar.querySelector('.brd-net');
  if (!el) return;
  if (state.selectedPinIdx == null || !state.board) {
    el.textContent = '';
    el.style.display = 'none';
    return;
  }
  const pin = state.board.pins[state.selectedPinIdx];
  const net = pin && pin.net;
  if (!net) {
    el.textContent = `${pin.part_refdes}.${pin.index} · no-net`;
  } else {
    const count = state.pinsByNet?.get(net)?.length || 1;
    el.textContent = `${net} · ${count} pins`;
  }
  el.style.display = '';
}

// --- interaction handlers ---
function attachInteraction(containerEl, toolbar, badge) {
  let dragging   = false;
  let dragStartX = 0, dragStartY = 0;
  let panStartX  = 0, panStartY  = 0;
  let dragMoved  = false;        // did the cursor move meaningfully since mousedown?

  canvas.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    // zoom toward cursor position
    const rect   = canvas.getBoundingClientRect();
    const cx     = ev.clientX - rect.left;
    const cy     = ev.clientY - rect.top;
    const factor = ev.deltaY < 0 ? 1.1 : 1 / 1.1;
    const newZ   = Math.max(0.05, Math.min(20, vp.zoom * factor));
    // keep world point under cursor fixed: worldX = (cx - panX) / zoom
    vp.panX = cx - ((cx - vp.panX) / vp.zoom) * newZ;
    vp.panY = cy - ((cy - vp.panY) / vp.zoom) * newZ;
    vp.zoom = newZ;
    updateZoomReadout(toolbar);
    requestRedraw();
  }, { passive: false });

  canvas.addEventListener('mousedown', (ev) => {
    if (ev.button !== 0) return;
    dragging   = true;
    dragMoved  = false;
    dragStartX = ev.clientX;
    dragStartY = ev.clientY;
    panStartX  = vp.panX;
    panStartY  = vp.panY;
    canvas.style.cursor = 'grabbing';
  });

  window.addEventListener('mousemove', (ev) => {
    if (dragging) {
      const dx = ev.clientX - dragStartX;
      const dy = ev.clientY - dragStartY;
      if (!dragMoved && (dx * dx + dy * dy) > 16) dragMoved = true;  // >4px threshold
      vp.panX = panStartX + dx;
      vp.panY = panStartY + dy;
      requestRedraw();
    }
    // cursor readout + pin-hover — only when mouse is over the canvas
    const rect = canvas.getBoundingClientRect();
    const inside = ev.clientX >= rect.left && ev.clientX <= rect.right &&
                   ev.clientY >= rect.top  && ev.clientY <= rect.bottom;
    if (inside) {
      const sx = ev.clientX - rect.left;
      const sy = ev.clientY - rect.top;
      cursorMils = screenToMils(sx, sy);
      // Skip hit-test while actively dragging — otherwise pinpoint flicker
      if (!dragging) {
        const hover = hitTestPin(sx, sy);
        if (hover !== state.hoveredPinIdx) {
          state.hoveredPinIdx = hover;
          canvas.style.cursor = hover != null ? 'pointer' : 'grab';
          requestRedraw();
        }
      }
    } else {
      cursorMils = null;
      if (state.hoveredPinIdx != null) {
        state.hoveredPinIdx = null;
        requestRedraw();
      }
    }
    updateCursorBadge(badge);
  });

  window.addEventListener('mouseup', (ev) => {
    if (!dragging) return;
    dragging = false;
    canvas.style.cursor = 'grab';
    // A click (no meaningful drag) selects a pin or clears the selection
    if (!dragMoved) {
      const rect = canvas.getBoundingClientRect();
      if (ev.clientX >= rect.left && ev.clientX <= rect.right &&
          ev.clientY >= rect.top  && ev.clientY <= rect.bottom) {
        const sx = ev.clientX - rect.left;
        const sy = ev.clientY - rect.top;
        const hit = hitTestPin(sx, sy);
        state.selectedPinIdx = hit;  // null clears
        updateNetReadout(toolbar);
        requestRedraw();
      }
    }
  });

  // Escape clears selection
  window.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && state.selectedPinIdx != null) {
      state.selectedPinIdx = null;
      updateNetReadout(toolbar);
      requestRedraw();
    }
  });

  canvas.addEventListener('mouseleave', () => {
    cursorMils = null;
    if (state.hoveredPinIdx != null) {
      state.hoveredPinIdx = null;
      canvas.style.cursor = 'grab';
      requestRedraw();
    }
    updateCursorBadge(badge);
  });
}

// --- loading skeleton ---
function renderSkeleton(root) {
  root.innerHTML = `
    <div class="summary-card" style="opacity:.5;pointer-events:none">
      <div class="sc-row"><span class="sc-label">board_id</span><span class="sc-value">—</span></div>
      <div class="sc-row"><span class="sc-label">format</span><span class="sc-value">—</span></div>
      <div class="sc-row"><span class="sc-label">composants</span><span class="sc-value">—</span></div>
      <div class="sc-row"><span class="sc-label">pins</span><span class="sc-value">—</span></div>
      <div class="sc-row"><span class="sc-label">nets</span><span class="sc-value">—</span></div>
      <div class="sc-row"><span class="sc-label">sha256</span><span class="sc-value">—</span></div>
      <div class="sc-status">Chargement…</div>
    </div>`;
}

function renderError(root, detail) {
  const code = (detail && detail.detail)  || 'ERREUR';
  const msg  = (detail && detail.message) || 'Erreur inconnue';
  root.innerHTML = `
    <div class="error-card">
      <div class="ec-code">${code}</div>
      <div class="ec-msg">${msg}</div>
    </div>`;
}

// --- main canvas setup ---
function mountCanvas(containerEl, board) {
  containerEl.innerHTML = '';

  const partCount = (board.parts || []).length;
  const pinCount  = (board.pins  || []).length;

  // Canvas element — fills container absolutely
  canvas = document.createElement('canvas');
  canvas.className = 'brd-canvas';
  canvas.style.cursor = 'grab';
  containerEl.appendChild(canvas);
  ctx = canvas.getContext('2d');

  // Toolbar — top-right floating glass
  const toolbar = document.createElement('div');
  toolbar.className = 'brd-toolbar';
  toolbar.innerHTML = `
    <div class="brd-seg">
      <button class="brd-seg-btn active" data-side="top">Top</button>
      <button class="brd-seg-btn" data-side="bottom">Bottom</button>
    </div>
    <button class="brd-btn" id="brd-annot-btn" title="Afficher / masquer les annotations sérigraphie" aria-pressed="true">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
        <path d="M6 6h12M6 18h12M10 6v12M14 6v12"/>
      </svg>
    </button>
    <button class="brd-btn" id="brd-fit-btn" title="Ajuster à la vue">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round">
        <path d="M4 9V5h4M20 9V5h-4M4 15v4h4M20 15v4h-4"/>
      </svg>
    </button>
    <span class="brd-net" style="display:none;font-family:var(--mono);font-size:11px;color:var(--emerald);padding:0 8px;border-left:1px solid var(--border);margin-left:4px;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>
    <span class="brd-zoom" style="font-family:var(--mono);font-size:11px;color:var(--text-2);min-width:42px;text-align:right">1.00×</span>`;
  containerEl.appendChild(toolbar);

  // Badge — bottom-left floating glass
  const badge = document.createElement('div');
  badge.className = 'brd-badge';
  badge.innerHTML = `
    <span class="brd-cursor" style="font-family:var(--mono);font-size:11px;color:var(--text-2)">—</span>
    <span style="font-family:var(--mono);font-size:10.5px;color:var(--text-3)">${partCount} composants · ${pinCount} pins</span>`;
  containerEl.appendChild(badge);

  // Layer-flip buttons
  toolbar.querySelectorAll('.brd-seg-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      toolbar.querySelectorAll('.brd-seg-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeSide = btn.dataset.side === 'bottom' ? LAYER_BOTTOM : LAYER_TOP;
      requestRedraw();
    });
  });

  // Fit button
  toolbar.querySelector('#brd-fit-btn').addEventListener('click', fitToBoard);

  // Annotations toggle
  const annotBtn = toolbar.querySelector('#brd-annot-btn');
  annotBtn.addEventListener('click', () => {
    showAnnotations = !showAnnotations;
    annotBtn.setAttribute('aria-pressed', String(showAnnotations));
    annotBtn.classList.toggle('active', showAnnotations);
    requestRedraw();
  });
  annotBtn.classList.add('active');  // default ON

  // ResizeObserver — keeps canvas sharp on window resize
  const ro = new ResizeObserver(() => {
    requestRedraw();
  });
  ro.observe(containerEl);

  // Interaction (pan / zoom / cursor)
  attachInteraction(containerEl, toolbar, badge);

  // Initial fit + render
  fitToBoard();
}

export async function initBoardview(containerEl) {
  if (state.board) {
    // Board already loaded — re-mount canvas (container may have been rebuilt)
    mountCanvas(containerEl, state.board);
    return;
  }
  if (!containerEl) return;

  renderSkeleton(containerEl);

  let blob;
  try {
    const res = await fetch(BRD_URL);
    if (!res.ok) throw { detail: 'FETCH_FAILED', message: `HTTP ${res.status} sur ${BRD_URL}` };
    blob = await res.blob();
  } catch (err) {
    renderError(containerEl, err.detail ? err : { detail: 'FETCH_FAILED', message: String(err) });
    return;
  }

  // Preserve the original filename (extension drives parser dispatch in
  // the backend — .kicad_pcb must not become .brd here or content-sniffing
  // will route to the wrong parser).
  const filename = BRD_URL.split('/').pop() || 'upload.brd';
  const form = new FormData();
  form.append('file', blob, filename);

  let board;
  try {
    const res  = await fetch(PARSE_URL, { method: 'POST', body: form });
    const data = await res.json();
    if (!res.ok) {
      // FastAPI wraps HTTPException body in a top-level `detail` key, so the
      // structured error is at data.detail (shape: {detail, message, ...}).
      renderError(containerEl, data.detail || data);
      return;
    }
    board = data;
  } catch (err) {
    renderError(containerEl, { detail: 'PARSE_FAILED', message: String(err) });
    return;
  }

  state.board = board;
  state.partBodyBboxes = computeAllBodyBboxes(board);
  state.partsSorted = sortPartsByAreaDesc(board.parts || [], state.partBodyBboxes);
  state.pinsByNet = computePinsByNet(board);
  state.netCategory = computeNetCategory(board);
  state.selectedPinIdx = null;
  mountCanvas(containerEl, board);
}

window.initBoardview = initBoardview;
