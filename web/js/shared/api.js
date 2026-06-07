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

async function _parse(res) {
  if (!res.ok) {
    let body = null;
    try { body = await res.json(); } catch { /* non-JSON error body */ }
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
