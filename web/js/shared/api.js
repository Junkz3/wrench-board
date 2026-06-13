// web/js/shared/api.js
// Single fetch wrapper for the front. Centralises the ok-check, JSON parsing
// and a normalised error. Stays owner-agnostic: the front does not send a
// tenant header today (tenancy lives in the cloud repo). Add interceptors here
// (not at call sites) if that ever changes.

export class ApiError extends Error {
  constructor(status, message, body) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

// A 401 on a UI call means the session lapsed (hosted deployment) — the engine
// itself is auth-agnostic and can't re-auth, so we fire a global event the
// hosting layer (cloud auth shim) can act on (e.g. redirect to /login). In
// self-host there's no listener, so this is a no-op. Exported so the few views
// that still own a raw fetch (stock) can report the same signal.
export function notifyUnauthorized() {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("wb:unauthorized"));
  }
}

async function _parse(res) {
  if (!res.ok) {
    let body = null;
    try { body = await res.json(); } catch { /* non-JSON error body */ }
    if (res.status === 401) notifyUnauthorized();
    throw new ApiError(res.status, body?.detail || res.statusText, body);
  }
  return res.json();
}

// GET → parsed JSON.
export function apiGet(path, init = {}) {
  return fetch(path, { ...init, method: "GET" }).then(_parse);
}

// POST/PUT/... with a body. `body` may be a string, FormData, or URLSearchParams.
export function apiSend(path, { method = "POST", body, headers } = {}) {
  return fetch(path, { method, body, headers }).then(_parse);
}
