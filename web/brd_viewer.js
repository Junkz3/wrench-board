// Board fixture selection via `?board=<slug>` query param. Default = MNT Reform.
// Known slugs map to files under /boards/. Unknown slugs fall back to default.
const BOARD_FIXTURES = {
  'mnt-reform': '/boards/mnt-reform-motherboard.brd',
  'bilayer': '/boards/bilayer_minimal.brd',
};
const DEFAULT_BOARD = 'mnt-reform';
function resolveBoardUrl() {
  const slug = new URLSearchParams(window.location.search).get('board');
  return BOARD_FIXTURES[slug] || BOARD_FIXTURES[DEFAULT_BOARD];
}

const BRD_URL  = resolveBoardUrl();
const PARSE_URL = '/api/board/parse';

const state = { board: null, partsSorted: null };

// Sort parts by descending bbox area so big packages (SoM connectors, BGA SoCs)
// are drawn first and dense clusters of small passives on top of them remain
// visible. Bbox is guaranteed normalized by the BRD2 parser post-fix.
function sortPartsByAreaDesc(parts) {
  return [...parts].sort((a, b) => {
    const aw = a.bbox[1].x - a.bbox[0].x;
    const ah = a.bbox[1].y - a.bbox[0].y;
    const bw = b.bbox[1].x - b.bbox[0].x;
    const bh = b.bbox[1].y - b.bbox[0].y;
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

  // ---- parts ----
  const parts = state.partsSorted || board.parts || [];
  ctx.lineWidth = 1;
  for (const part of parts) {
    // layer filter: skip parts that don't belong to the active side
    // BOTH (3) always drawn; TOP (1) only on TOP; BOTTOM (2) only on BOTTOM
    if (part.layer !== LAYER_BOTH) {
      if (activeSide === LAYER_TOP    && part.layer !== LAYER_TOP)    continue;
      if (activeSide === LAYER_BOTTOM && part.layer !== LAYER_BOTTOM) continue;
    }
    const bbox = part.bbox;
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

  // ---- pins (only when zoom >= 2) ----
  if (vp.zoom >= 2) {
    const pins = board.pins || [];
    const pinColor = cssVar('--text-2') || '#a9b6cc';
    ctx.fillStyle = pinColor;
    for (const pin of pins) {
      if (pin.layer !== LAYER_BOTH) {
        if (activeSide === LAYER_TOP    && pin.layer !== LAYER_TOP)    continue;
        if (activeSide === LAYER_BOTTOM && pin.layer !== LAYER_BOTTOM) continue;
      }
      const s = milsToScreen(pin.pos.x, pin.pos.y, boardW);
      ctx.beginPath();
      ctx.arc(s.x, s.y, 1.2, 0, Math.PI * 2);
      ctx.fill();
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

// --- interaction handlers ---
function attachInteraction(containerEl, toolbar, badge) {
  let dragging   = false;
  let dragStartX = 0, dragStartY = 0;
  let panStartX  = 0, panStartY  = 0;

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
    dragStartX = ev.clientX;
    dragStartY = ev.clientY;
    panStartX  = vp.panX;
    panStartY  = vp.panY;
    canvas.style.cursor = 'grabbing';
  });

  window.addEventListener('mousemove', (ev) => {
    if (dragging) {
      vp.panX = panStartX + (ev.clientX - dragStartX);
      vp.panY = panStartY + (ev.clientY - dragStartY);
      requestRedraw();
    }
    // cursor readout — only when mouse is over the canvas
    const rect = canvas.getBoundingClientRect();
    if (ev.clientX >= rect.left && ev.clientX <= rect.right &&
        ev.clientY >= rect.top  && ev.clientY <= rect.bottom) {
      const sx = ev.clientX - rect.left;
      const sy = ev.clientY - rect.top;
      cursorMils = screenToMils(sx, sy);
    } else {
      cursorMils = null;
    }
    updateCursorBadge(badge);
  });

  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    canvas.style.cursor = 'grab';
  });

  canvas.addEventListener('mouseleave', () => {
    cursorMils = null;
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
    <button class="brd-btn" id="brd-fit-btn" title="Ajuster à la vue">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round">
        <path d="M4 9V5h4M20 9V5h-4M4 15v4h4M20 15v4h-4"/>
      </svg>
    </button>
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

  const form = new FormData();
  form.append('file', blob, 'mnt-reform-motherboard.brd');

  let board;
  try {
    const res  = await fetch(PARSE_URL, { method: 'POST', body: form });
    const data = await res.json();
    if (!res.ok) {
      renderError(containerEl, data);
      return;
    }
    board = data;
  } catch (err) {
    renderError(containerEl, { detail: 'PARSE_FAILED', message: String(err) });
    return;
  }

  state.board = board;
  state.partsSorted = sortPartsByAreaDesc(board.parts || []);
  mountCanvas(containerEl, board);
}

window.initBoardview = initBoardview;
