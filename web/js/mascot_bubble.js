// Mascot speech bubble — a single glass tooltip anchored to a target element,
// used by the onboarding orchestrator to let the mascot "talk" and point at a
// zone of the landing. Framework-free, one bubble at a time (singleton).
//
// The visual styling (glass surface, arrow, controls) lives in
// `web/styles/onboarding.css` under `.mascot-bubble`. This module only owns
// DOM creation, positioning (with viewport-edge auto-flip) and the
// Next/Skip control wiring — same popover-flip spirit as the Pickr pickers.
//
// API:
//   showBubble({ anchor, text, next, skip, nextLabel, skipLabel, placement })
//   hideBubble()
//
//   anchor      : Element | DOMRect | {x,y} — what the arrow points at
//   text        : string (already localized) shown as the body
//   next        : () => void   — when set, renders a primary "Next" button
//   skip        : () => void   — when set, renders a quiet "Skip" link
//   placement   : "top" | "bottom" | "left" | "right" (preferred; auto-flips)

import { t } from "./i18n.js";

const MARGIN = 12; // gap between bubble and anchor / viewport edge

let _bubble = null;
let _spotlight = null;  // dim-everything-but-this overlay (opt-in via spotlight:true)
let _reposition = null; // bound listener so we can remove it on hide

// Spotlight = a transparent box over the anchor with a huge dark box-shadow, so
// everything around it dims and the anchored zone reads as "lit". The same div is
// reused across bubbles so the lit hole MORPHS from zone to zone (CSS transition).
function _applySpotRect(rect) {
  const pad = 8;
  const s = _spotlight.style;
  s.left = `${Math.round(rect.left - pad)}px`;
  s.top = `${Math.round(rect.top - pad)}px`;
  s.width = `${Math.round(rect.width + pad * 2)}px`;
  s.height = `${Math.round(rect.height + pad * 2)}px`;
}

function _placeSpotlight(rect) {
  if (!_spotlight) {
    _spotlight = document.createElement("div");
    _spotlight.className = "mascot-spotlight";
    _spotlight.setAttribute("aria-hidden", "true");
    document.body.appendChild(_spotlight);
    _applySpotRect(rect);                 // position before fade-in (no slide from 0,0)
    const sp = _spotlight;
    requestAnimationFrame(() => { if (_spotlight === sp) sp.classList.add("is-shown"); });
  } else {
    _applySpotRect(rect);                 // morph to the new zone
  }
}

function _removeSpotlight() {
  if (!_spotlight) return;
  const sp = _spotlight;
  _spotlight = null;
  sp.classList.remove("is-shown");
  setTimeout(() => sp.remove(), 240);     // after the fade-out
}

function _rectOf(anchor) {
  if (!anchor) return null;
  if (anchor instanceof Element) return anchor.getBoundingClientRect();
  if (typeof anchor.left === "number") return anchor; // already a DOMRect-like
  if (typeof anchor.x === "number") {
    return { left: anchor.x, top: anchor.y, right: anchor.x, bottom: anchor.y, width: 0, height: 0 };
  }
  return null;
}

// Pick a placement that fits, starting from the preferred one. Returns the
// chosen side; the caller reads it back to position the arrow.
function _choosePlacement(preferred, rect, bw, bh) {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const fits = {
    bottom: rect.bottom + MARGIN + bh <= vh,
    top: rect.top - MARGIN - bh >= 0,
    right: rect.right + MARGIN + bw <= vw,
    left: rect.left - MARGIN - bw >= 0,
  };
  const order = [preferred, "bottom", "top", "right", "left"];
  for (const side of order) {
    if (fits[side]) return side;
  }
  return preferred || "bottom";
}

function _position(rect, placement) {
  const el = _bubble;
  const bw = el.offsetWidth;
  const bh = el.offsetHeight;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const side = _choosePlacement(placement, rect, bw, bh);

  let left;
  let top;
  const cx = rect.left + rect.width / 2;
  const cy = rect.top + rect.height / 2;

  if (side === "bottom" || side === "top") {
    left = cx - bw / 2;
    top = side === "bottom" ? rect.bottom + MARGIN : rect.top - MARGIN - bh;
  } else {
    top = cy - bh / 2;
    left = side === "right" ? rect.right + MARGIN : rect.left - MARGIN - bw;
  }

  // Clamp into the viewport, keeping the arrow aimed at the anchor centre.
  left = Math.max(MARGIN, Math.min(left, vw - bw - MARGIN));
  top = Math.max(MARGIN, Math.min(top, vh - bh - MARGIN));

  el.style.left = `${Math.round(left)}px`;
  el.style.top = `${Math.round(top)}px`;
  el.dataset.placement = side;

  // Arrow offset along the bubble edge, pointing back at the anchor centre.
  const arrow = el.querySelector(".mascot-bubble-arrow");
  if (arrow) {
    if (side === "bottom" || side === "top") {
      const ax = Math.max(14, Math.min(cx - left, bw - 14));
      arrow.style.left = `${Math.round(ax)}px`;
      arrow.style.top = "";
    } else {
      const ay = Math.max(14, Math.min(cy - top, bh - 14));
      arrow.style.top = `${Math.round(ay)}px`;
      arrow.style.left = "";
    }
  }
}

function _teardownBubble() {
  if (_reposition) {
    window.removeEventListener("resize", _reposition);
    window.removeEventListener("scroll", _reposition, true);
    _reposition = null;
  }
  if (_bubble) {
    _bubble.remove();
    _bubble = null;
  }
}

export function hideBubble() {
  _teardownBubble();
  _removeSpotlight();
}

export function showBubble({ anchor, text, next, skip, nextLabel, skipLabel, placement = "bottom", spotlight = false }) {
  // Drop the previous bubble but KEEP the spotlight so it can morph to the new
  // zone (or get removed below if this bubble doesn't want one).
  _teardownBubble();

  const rect = _rectOf(anchor) || {
    left: window.innerWidth / 2, top: window.innerHeight / 2,
    right: window.innerWidth / 2, bottom: window.innerHeight / 2, width: 0, height: 0,
  };

  const el = document.createElement("div");
  el.className = "mascot-bubble";
  el.setAttribute("role", "dialog");
  el.setAttribute("aria-live", "polite");

  const body = document.createElement("p");
  body.className = "mascot-bubble-body";
  body.textContent = text || "";
  el.appendChild(body);

  if (next || skip) {
    const actions = document.createElement("div");
    actions.className = "mascot-bubble-actions";

    if (skip) {
      const s = document.createElement("button");
      s.type = "button";
      s.className = "mascot-bubble-skip";
      s.textContent = skipLabel || t("onboarding.skip");
      s.addEventListener("click", () => skip());
      actions.appendChild(s);
    }
    if (next) {
      const n = document.createElement("button");
      n.type = "button";
      n.className = "mascot-bubble-next";
      n.textContent = nextLabel || t("onboarding.next");
      n.addEventListener("click", () => next());
      actions.appendChild(n);
    }
    el.appendChild(actions);
  }

  const arrow = document.createElement("span");
  arrow.className = "mascot-bubble-arrow";
  arrow.setAttribute("aria-hidden", "true");
  el.appendChild(arrow);

  document.body.appendChild(el);
  _bubble = el;

  _position(rect, placement);

  // Spotlight only makes sense over a real element zone (not a centred,
  // anchorless bubble). Morphs from the previous zone; removed if not wanted.
  const wantSpot = spotlight && anchor instanceof Element;
  if (wantSpot) _placeSpotlight(rect);
  else _removeSpotlight();

  // Keep the bubble (and the spotlight) glued to its (possibly moving) anchor.
  _reposition = () => {
    const r = _rectOf(anchor);
    if (r) {
      _position(r, placement);
      if (wantSpot) _placeSpotlight(r);
    }
  };
  window.addEventListener("resize", _reposition);
  window.addEventListener("scroll", _reposition, true);

  return el;
}
