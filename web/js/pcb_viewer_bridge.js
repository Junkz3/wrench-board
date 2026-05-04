// Bridge between the SVG/D3 brd_viewer.js call surface
// (window.initBoardview, window.Boardview) and the WebGL viewer in
// pcb_viewer.js. Loaded after brd_viewer.js so its overrides take effect.
//
// Native integration: the canvas + info panel + stats are statically
// laid out in web/index.html under the section data-section-stub="pcb".
// We just instantiate PCBViewerOptimized on first navigation to PCB,
// then call viewer.loadBoard() on every subsequent navigation / pin
// switch.

let viewer = null;
let boardPayload = null;
let _refdesMap = null;
let _netSet = null;
// Track which slug is currently loaded so navigating away and back
// (#pcb → #home → #pcb) doesn't refetch the payload + retrigger XZZ
// decryption + rebuild every InstancedMesh.
let _loadedSlug = null;

function resolveSlug() {
  const qs = new URLSearchParams(window.location.search);
  return qs.get("device") || qs.get("board") || null;
}

function rebuildIndexes(payload) {
  _refdesMap = new Map();
  for (const c of payload.components || []) _refdesMap.set(c.id, c);
  _netSet = new Set();
  for (const p of payload.pins || []) {
    if (p.net && p.net !== "NC") _netSet.add(p.net);
  }
}

async function fetchPayload(slug) {
  const url = `/api/board/render?slug=${encodeURIComponent(slug)}`;
  console.log("[pcb_viewer_bridge] fetching", url);
  const res = await fetch(url, { cache: "no-store" });
  console.log("[pcb_viewer_bridge] render status", res.status);
  if (!res.ok) return null;
  const payload = await res.json();
  console.log(
    "[pcb_viewer_bridge] payload",
    payload.format_type,
    payload.components_count,
    "parts /",
    payload.pins_count,
    "pins"
  );
  return payload;
}

async function ensureViewerAndLoad(slug) {
  const payload = await fetchPayload(slug);
  if (!payload) return false;

  if (typeof THREE === "undefined") {
    console.error("[pcb_viewer_bridge] THREE missing");
    return false;
  }
  if (!window.PCBViewerOptimized) {
    console.error("[pcb_viewer_bridge] PCBViewerOptimized missing");
    return false;
  }

  // The empty-state wrapper hides itself once a board is loaded.
  const empty = document.getElementById("no-file-message");
  if (empty) empty.classList.add("hidden");

  if (!viewer) {
    try {
      console.log("[pcb_viewer_bridge] instantiating viewer");
      viewer = new window.PCBViewerOptimized("pcb-canvas");
    } catch (err) {
      console.error("[pcb_viewer_bridge] viewer constructor failed", err);
      viewer = null;
      return false;
    }
  }

  wireToolbarOnce();

  try {
    viewer.loadBoard(payload);
    console.log("[pcb_viewer_bridge] viewer ready ✓");
  } catch (err) {
    console.error("[pcb_viewer_bridge] loadBoard failed", err);
    return false;
  }

  // After loadBoard the orthographic frustum was sized once — force a
  // resize so the canvas pixel size matches its CSS size now that the
  // section is visible (clientWidth was 0 if we'd init'd while hidden).
  if (viewer.onResize) viewer.onResize();

  boardPayload = payload;
  _loadedSlug = slug;
  rebuildIndexes(payload);

  // Auto-fit on load: invoke reset() so the orthographic frustum sizes
  // itself to the board's actual dimensions instead of leaving the
  // camera at the constructor defaults (visible as a tiny board parked
  // in the bottom-left corner).
  try { window.Boardview.reset(); } catch (_) {}

  return true;
}

console.log(
  "[pcb_viewer_bridge] module loaded — THREE:",
  typeof THREE,
  "PCBViewerOptimized:",
  typeof window.PCBViewerOptimized
);

// Snapshot the SVG initBoardview as a fallback for slugs we can't resolve.
const _svgInitBoardview = window.initBoardview;

window.initBoardview = async function bridgedInit(_containerEl) {
  // _containerEl is the legacy SVG mount target — we ignore it because the
  // native PCB section ships its own canvas / info panel layout.
  const slug = resolveSlug();
  console.log("[pcb_viewer_bridge] bridgedInit slug=", slug);
  if (!slug) {
    if (typeof _svgInitBoardview === "function") return _svgInitBoardview(_containerEl);
    return;
  }
  // Already loaded — just resize the canvas to the now-visible
  // section and bail. The user toggling between #home and #pcb
  // shouldn't pay for a fresh decrypt + rebuild every time.
  if (viewer && boardPayload && _loadedSlug === slug) {
    console.log("[pcb_viewer_bridge] reusing cached viewer for", slug);
    if (viewer.onResize) viewer.onResize();
    if (viewer.requestRender) viewer.requestRender();
    return;
  }
  const ok = await ensureViewerAndLoad(slug);
  if (!ok && typeof _svgInitBoardview === "function") {
    console.warn("[pcb_viewer_bridge] viewer load failed — SVG fallback");
    return _svgInitBoardview(_containerEl);
  }
};

// ---------- window.Boardview override ----------
//
// Keeps the API surface stable for protocol.js / llm.js / WS dispatch.
// Render-driving methods talk to the WebGL viewer; the rest stays as
// no-op stubs that we'll fill in P6.

function _findItem(refdes) {
  if (!viewer || !refdes) return null;
  const items = viewer._hoverableItems || [];
  const target = String(refdes).trim();
  return items.find((it) => it.id === target) || null;
}

// Backend WS events come prefixed `boardview.<verb>` (see
// api/tools/ws_events.py). The agent ships mils for any geometric
// payload (focus.bbox, draw_arrow.from/to, show_pin.pos) — convert
// to mm before handing to the WebGL viewer, which works in mm.
const MIL_TO_MM = 0.0254;

function _milPairToMm(pair) {
  if (!pair) return null;
  const x = Array.isArray(pair) ? pair[0] : pair.x;
  const y = Array.isArray(pair) ? pair[1] : pair.y;
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  return { x: x * MIL_TO_MM, y: y * MIL_TO_MM };
}

window.Boardview = {
  apply(ev) {
    if (!ev || !viewer) return;
    const t = ev.type;
    try {
      switch (t) {
        // ---- legacy / short aliases (kept for any in-flight callers
        // that still hit the un-prefixed name).
        case "highlight":
        case "bv_highlight":
          this.highlight(ev.refdes);
          return;
        case "focus":
        case "bv_focus":
          this.focus(ev.refdes);
          return;
        case "reset":
        case "bv_reset_view":
          this.reset();
          return;

        // ---- canonical backend envelopes (api/tools/ws_events.py)
        case "boardview.highlight":
          this.highlight(ev.refdes);
          return;
        case "boardview.focus":
          this.focus(ev.refdes);
          return;
        case "boardview.reset_view":
          this.reset();
          return;
        case "boardview.flip":
          this.flip(ev.new_side);
          return;
        case "boardview.annotate":
          this.annotate(ev.refdes, ev.label, ev.id);
          return;
        case "boardview.dim_unrelated":
          this.dim_unrelated();
          return;
        case "boardview.highlight_net":
          this.highlight_net(ev.net);
          return;
        case "boardview.show_pin":
          this.show_pin(ev.refdes, ev.pin, ev.pos);
          return;
        case "boardview.draw_arrow":
          // Backend ships {from: [x,y], to: [x,y], id} in mils.
          this.draw_arrow(ev.from, ev.to, ev.id);
          return;
        case "boardview.filter":
          this.filter(ev.prefix);
          return;
        case "boardview.measure":
          this.measure(ev.from_refdes, ev.to_refdes, ev.distance_mm);
          return;
        case "boardview.layer_visibility":
          this.layer_visibility(ev.layer, ev.visible);
          return;
        case "boardview.board_loaded":
          // Renderer already loaded the board through /api/board/render
          // (see ensureViewerAndLoad). The board_loaded WS event from
          // the agent runtime is informational — nothing to do.
          return;
        default:
          // Unknown envelope — silent log for diagnosis.
          console.warn("[Boardview] unknown event type", t);
      }
    } catch (err) {
      console.warn("[Boardview] apply failed", err, ev);
    }
  },
  hasBoard() {
    return !!boardPayload && (boardPayload.components || []).length > 0;
  },
  hasRefdes(refdes) {
    return !!(_refdesMap && _refdesMap.has(String(refdes).trim()));
  },
  hasNet(name) {
    return !!(_netSet && _netSet.has(String(name).trim()));
  },
  highlight(refdes) {
    // Backend ships refdes as an array (Highlight.refdes is list[str]).
    // Pick the first valid match — the WebGL viewer's selectItem only
    // tracks one selectedItem at a time. Multi-highlight is approximated
    // by the agent's bv_scene path which calls this once per refdes.
    const list = Array.isArray(refdes) ? refdes : [refdes];
    for (const r of list) {
      const item = _findItem(r);
      if (item && viewer.selectItem) {
        viewer.selectItem(item);
        return;
      }
    }
  },
  focus(refdes) {
    if (!viewer || !_refdesMap) return;
    const c = _refdesMap.get(String(refdes).trim());
    if (!c) return;
    viewer.camera.position.x = c.x + c.width / 2;
    viewer.camera.position.y = c.y + c.height / 2;
    viewer.frustumSize = Math.max(c.width, c.height, 20) * 4;
    viewer.zoom = 100 / viewer.frustumSize;
    if (viewer.onResize) viewer.onResize();
  },
  focusRefdes(refdes) { this.focus(refdes); },
  reset() {
    if (!viewer || !boardPayload) return;
    if (viewer.clearSelection) viewer.clearSelection();
    // Tear down agent overlays alongside the camera fit so a single
    // bv_reset_view returns the canvas to a clean baseline.
    if (typeof viewer.resetAgentOverlays === "function") {
      viewer.resetAgentOverlays();
    }
    // Defer to the viewer's transform-aware fit: it picks the bbox
    // matching the current side mode (top / both / bottom) and projects
    // its centre through the active rotation, so Fit works correctly
    // after the user has toggled side or rotated the board.
    if (typeof viewer._recentreOnSideMode === "function") {
      viewer._recentreOnSideMode();
      if (viewer.requestRender) viewer.requestRender();
      return;
    }
    viewer.camera.position.x =
      (boardPayload.board_offset_x || 0) + (boardPayload.board_width || 0) / 2;
    viewer.camera.position.y =
      (boardPayload.board_offset_y || 0) + (boardPayload.board_height || 0) / 2;
    viewer.frustumSize =
      Math.max(boardPayload.board_width || 100, boardPayload.board_height || 100) *
      1.2;
    viewer.zoom = 100 / viewer.frustumSize;
    if (viewer.onResize) viewer.onResize();
  },
  // Tally pins by net category for the Tweaks colour panel.
  // Returns null if no board is loaded; otherwise an object keyed by
  // category ('signal', 'power', 'ground', 'clock', 'reset', 'no-net')
  // mapped to instance counts. Drives the pin-count pills next to each
  // color picker — gives the user a sense of how many pins each row
  // actually affects on the current board.
  /**
   * Drop the cached payload for `slug` so the next `initBoardview`
   * call refetches `/api/board/render`. Used by the home dashboard
   * when the technician pins a different boardview version — the
   * URL slug stays the same but the underlying file on disk changed,
   * so the navigation cache (`_loadedSlug`) would otherwise serve
   * the stale parse. Pass no arg or matching slug to invalidate.
   */
  invalidate(slug) {
    if (!slug || _loadedSlug === slug) {
      _loadedSlug = null;
      boardPayload = null;
      _refdesMap = null;
      _netSet = null;
    }
  },
  getPinCounts() {
    if (!viewer || !viewer._hoverableItems) return null;
    const counts = {
      signal: 0, power: 0, ground: 0, clock: 0, reset: 0, 'no-net': 0,
      testPad: 0, via: 0,
    };
    for (const item of viewer._hoverableItems) {
      const t = item._instanceType;
      if (t === 'testPad') {
        counts.testPad++;
        // Also fall through to net-category bucketing so the testpad
        // count is independent of the per-net signal/power/etc bucket.
      } else if (t === 'via') {
        const isMounting = !item.net || item.net === '' || item.net === 'NC';
        if (!isMounting) counts.via++;
        continue;  // mounting holes aren't represented in the picker
      } else if (t !== 'pin' && t !== 'rectPin') {
        continue;
      }
      const cat = item.is_gnd ? 'ground' : viewer._netCategory(item.net || '');
      // _netCategory returns 'default' (named net no special prefix) → bucket as signal,
      // and 'nc' (NC net) → bucket as no-net. Both 'default' and 'nc' fold here so
      // every pin lands in exactly one of the picker rows.
      const key = cat === 'default' ? 'signal'
                : cat === 'nc' ? 'no-net'
                : cat;
      if (key in counts) counts[key]++;
    }
    return counts;
  },
  // ---------- Agent-driven overlays (bv_* tool events) ----------
  flip(newSide) {
    if (!viewer) return;
    // The backend Flip event ships the target side it just flipped to.
    // When supplied, route through setSideMode so the toolbar segment
    // updates with the correct active class. Otherwise toggle.
    if (newSide === "top" || newSide === "bottom") {
      if (typeof viewer.setSideMode === "function") {
        viewer.setSideMode(newSide);
      }
    } else if (typeof viewer.flipSide === "function") {
      viewer.flipSide();
    }
  },
  annotate(refdes, label, id) {
    if (viewer && typeof viewer.addAnnotation === "function") {
      viewer.addAnnotation(refdes, label, id);
    }
  },
  dim_unrelated() {
    if (viewer && typeof viewer.dimUnrelated === "function") {
      viewer.dimUnrelated();
    }
  },
  highlight_net(net) {
    if (viewer && typeof viewer.highlightNetByName === "function") {
      viewer.highlightNetByName(net);
    }
  },
  show_pin(refdes, _pinNumber, posMils) {
    if (!viewer || typeof viewer.showPinAt !== "function") return;
    const pos = _milPairToMm(posMils);
    viewer.showPinAt(refdes, pos);
  },
  draw_arrow(fromMils, toMils, id) {
    if (!viewer || typeof viewer.addAgentArrow !== "function") return;
    const from = _milPairToMm(fromMils);
    const to = _milPairToMm(toMils);
    if (!from || !to) return;
    viewer.addAgentArrow(from, to, id);
  },
  filter(prefix) {
    if (viewer && typeof viewer.setRefdesFilter === "function") {
      viewer.setRefdesFilter(prefix || null);
    }
  },
  measure(fromRefdes, toRefdes, distanceMm) {
    if (!viewer || typeof viewer.addMeasurement !== "function") return;
    const label = Number.isFinite(distanceMm)
      ? `${distanceMm.toFixed(2)} mm`
      : "";
    // Use a deterministic id so repeated calls between the same pair
    // replace the prior measurement instead of stacking.
    const id = `meas-${fromRefdes}-${toRefdes}`;
    viewer.addMeasurement(fromRefdes, toRefdes, label, id);
  },
  layer_visibility(layer, visible) {
    if (viewer && typeof viewer.setLayerVisibility === "function") {
      viewer.setLayerVisibility(layer, !!visible);
    }
  },
  // Override reset() above is the canonical user-facing fit. The agent's
  // bv_reset_view also clears overlays — extend reset() so it tears
  // down annotations/arrows/measurements/dim/filter alongside the camera
  // fit.
  resetAgent() {
    if (viewer && typeof viewer.resetAgentOverlays === "function") {
      viewer.resetAgentOverlays();
    }
  },
  // Protocol badges — driven by protocol.js whenever the active protocol
  // (steps + current_step_id) changes. Delegates to the WebGL viewer's
  // sprite-based renderer; mirrors brd_viewer.js's same-named API so
  // protocol.js stays viewer-agnostic.
  setProtocolBadges(steps, currentId) {
    if (viewer && typeof viewer.setProtocolBadges === "function") {
      viewer.setProtocolBadges(steps, currentId);
    }
  },
  clearProtocolBadges() {
    if (viewer && typeof viewer.clearProtocolBadges === "function") {
      viewer.clearProtocolBadges();
    }
  },
  // Returns viewport (page) pixel coords of the part's bbox-top centre,
  // or null when the refdes isn't loaded / off-screen / hidden face.
  // protocol.js uses this to anchor the floating refdes chip + arrow.
  refdesScreenPos(refdes) {
    if (viewer && typeof viewer.refdesScreenPos === "function") {
      return viewer.refdesScreenPos(refdes);
    }
    return null;
  },
};

// ---------- Net-category color wrapping ----------
//
// brd_viewer.js (SVG) defines window.setBoardviewNetColor /
// window.resetBoardviewColors and main.js wires the Tweaks <input
// type="color"> rows to them. Wrap those globals so any SVG-side colour
// change ALSO recolours the WebGL viewer's pin instances. This keeps
// the picker code (main.js) format-agnostic — no need to know whether
// the WebGL viewer is alive.

(function wrapBoardviewColorAPI() {
  const origSet = window.setBoardviewNetColor;
  if (typeof origSet === "function" && !origSet.__wrappedForWebGL) {
    const wrapped = function (category, hex) {
      try { origSet(category, hex); } catch (_) {}
      if (viewer && typeof viewer.setNetCategoryColor === "function") {
        try { viewer.setNetCategoryColor(category, hex); } catch (_) {}
      }
    };
    wrapped.__wrappedForWebGL = true;
    window.setBoardviewNetColor = wrapped;
  }

  const origReset = window.resetBoardviewColors;
  if (typeof origReset === "function" && !origReset.__wrappedForWebGL) {
    const wrapped = function () {
      try { origReset(); } catch (_) {}
      // After reset, replay each default through the WebGL viewer too.
      if (viewer && typeof viewer.setNetCategoryColor === "function") {
        const defaults = (window.getBoardviewColors && window.getBoardviewColors()) || {};
        for (const cat of Object.keys(defaults)) {
          try { viewer.setNetCategoryColor(cat, defaults[cat]); } catch (_) {}
        }
      }
    };
    wrapped.__wrappedForWebGL = true;
    window.resetBoardviewColors = wrapped;
  }
})();

// ---------- Toolbar wiring (Fit + Top/Bottom) ----------
//
// Idempotent: the first call attaches the listeners and flips a flag
// so subsequent navigations don't double-attach. Buttons live in
// index.html under .brd-toolbar.

let _toolbarWired = false;
function wireToolbarOnce() {
  if (_toolbarWired) return;
  _toolbarWired = true;

  const fitBtn = document.getElementById("brdFitBtn");
  if (fitBtn) fitBtn.addEventListener("click", () => {
    try { window.Boardview.reset(); } catch (_) {}
  });

  const rotL = document.getElementById("brdRotL");
  if (rotL) rotL.addEventListener("click", () => {
    if (viewer && typeof viewer.rotateLeft === "function") viewer.rotateLeft();
  });
  const rotR = document.getElementById("brdRotR");
  if (rotR) rotR.addEventListener("click", () => {
    if (viewer && typeof viewer.rotateRight === "function") viewer.rotateRight();
  });

  const topBtn = document.getElementById("brdLayerTop");
  const bothBtn = document.getElementById("brdLayerBoth");
  const bottomBtn = document.getElementById("brdLayerBottom");
  // 3-state segment: TOP / BOTH / BOTTOM. Drives `viewer.setSideMode`,
  // which on dual-outline boards (XZZ side-by-side / stacked) filters
  // entity visibility by face and recentres the camera. On single-
  // outline boards every entity has `_side === null` and the toggle is
  // effectively a no-op except for centering.
  const setMode = (mode) => {
    if (!viewer) return;
    if (typeof viewer.setSideMode === "function") {
      viewer.setSideMode(mode);
    }
    if (topBtn) topBtn.classList.toggle("active", mode === "top");
    if (bothBtn) bothBtn.classList.toggle("active", mode === "both");
    if (bottomBtn) bottomBtn.classList.toggle("active", mode === "bottom");
  };
  if (topBtn) topBtn.addEventListener("click", () => setMode("top"));
  if (bothBtn) bothBtn.addEventListener("click", () => setMode("both"));
  if (bottomBtn) bottomBtn.addEventListener("click", () => setMode("bottom"));

  // DFM-alternate (DNP) overlay toggle. The button stays active (cyan
  // border) while the dashed-outline layer is visible.
  const dnpBtn = document.getElementById("brdToggleDnp");
  if (dnpBtn) dnpBtn.addEventListener("click", () => {
    if (!viewer || typeof viewer.setShowDnp !== "function") return;
    const next = !viewer._showDnp;
    viewer.setShowDnp(next);
    dnpBtn.classList.toggle("active", next);
    dnpBtn.setAttribute("aria-pressed", next ? "true" : "false");
  });

  // Vias toggle. OFF by default — sync the viewer's `_showVias` flag
  // with the button's initial state (no `active` class).
  const viasBtn = document.getElementById("brdToggleVias");
  if (viasBtn) {
    if (viewer && typeof viewer.setShowVias === "function") {
      viewer.setShowVias(false);
    }
    viasBtn.addEventListener("click", () => {
      if (!viewer || typeof viewer.setShowVias !== "function") return;
      const next = !viewer._showVias;
      viewer.setShowVias(next);
      viasBtn.classList.toggle("active", next);
      viasBtn.setAttribute("aria-pressed", next ? "true" : "false");
    });
  }

  // Traces toggle. OFF by default. Mirrors the vias toggle pattern —
  // calls `viewer.toggleTraces()` which flips `showTraces` and shows /
  // hides every copper line/arc mesh in `meshGroups.traces` (outline
  // traces stay visible regardless, see pcb_viewer.js:3489).
  const tracesBtn = document.getElementById("brdToggleTraces");
  if (tracesBtn) {
    tracesBtn.addEventListener("click", () => {
      if (!viewer || typeof viewer.toggleTraces !== "function") return;
      viewer.toggleTraces();
      const on = !!viewer.showTraces;
      tracesBtn.classList.toggle("active", on);
      tracesBtn.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }
}

// Drain any events queued during module load.
const _pending = (window.Boardview && window.Boardview.__pending) || [];
_pending.forEach((ev) => {
  try { window.Boardview.apply(ev); } catch (_) {}
});
