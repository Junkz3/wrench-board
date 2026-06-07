// web/js/services/diagnosticSocket.js
// Single transport for the diagnostic-agent WebSocket (/ws/diagnostic/{slug}).
// Extracted from llm.js (Phase D.4 / B.3). llm.js keeps the message dispatcher
// as the onMessage consumer; only the socket plumbing (ws/wss URL build,
// new WebSocket, open/close/error wiring) and the active-socket reference live
// here. That active reference replaces the former window.__diagnosticWS global,
// so other surfaces (Protocol.send in main.js, the dashboard "mark fixed"
// button) can post on the live session without importing llm.js.
//
// Cloud-safe: the WS is relayed byte-for-byte by the hosted front-door, so the
// URL is unchanged — same-origin, root-relative, built at call time.

let current = null;   // the live diagnostic socket, or null

// Build the diagnostic WS URL. tier / repairId / conv are optional query params,
// mirroring the query contract llm.js used before the extraction.
function buildURL(slug, { tier, repairId, conv } = {}) {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const params = new URLSearchParams();
  if (tier) params.set("tier", tier);
  if (repairId) params.set("repair", repairId);
  if (conv) params.set("conv", conv);
  const q = params.toString() ? `?${params.toString()}` : "";
  return `${scheme}://${window.location.host}/ws/diagnostic/${encodeURIComponent(slug)}${q}`;
}

// Open a diagnostic socket and make it the active one. The open/close/error
// status listeners are wired from the supplied callbacks; the caller attaches
// its own "message" listener to the returned socket (the dispatcher stays in
// llm.js). Throws if the URL is invalid — the caller wraps the call in
// try/catch, exactly as it did around `new WebSocket(url)` before.
export function connectDiagnostic(slug, opts = {}, { onOpen, onClose, onError } = {}) {
  const ws = new WebSocket(buildURL(slug, opts));
  current = ws;
  if (onOpen) ws.addEventListener("open", onOpen);
  if (onClose) ws.addEventListener("close", onClose);
  if (onError) ws.addEventListener("error", onError);
  return ws;
}

// The live diagnostic socket (or null). For surfaces that read/post on the
// current session without owning its lifecycle.
export function getDiagnosticWS() {
  return current;
}

// Send a JSON payload on the live socket. No-op returning false when no socket
// is open, so callers don't need to guard readyState themselves (and never hit
// the InvalidStateError that send() throws on a CLOSING/CLOSED socket).
export function sendDiagnostic(payload) {
  if (!current || current.readyState !== WebSocket.OPEN) return false;
  try {
    current.send(JSON.stringify(payload));
    return true;
  } catch (err) {
    console.warn("[diagnosticSocket] send failed", err);
    return false;
  }
}
