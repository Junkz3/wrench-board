// Cross-device onboarding flags. Source of truth = the technician profile
// (server-side, tenant-scoped via X-Owner-Ref under the cloud front-door).
// localStorage stays as a fast pre-gate cache so the landing hero never flashes
// before the staged reveal, but it is no longer authoritative: a guided tour
// completed on one device must not replay on another.
//
// Two flags mirror the two one-shot tours:
//   onboarding_seen  -> landing cockpit guided tour        (legacy LS wb_onboarding_seen)
//   first_diag_seen  -> first-diagnostic workspace coaching (legacy LS wb_first_diag_seen)
//
// All HTTP goes through shared/api.js so the cloud front-door's fetch wrap and
// X-Owner-Ref injection apply — never a raw fetch here.

import { apiGet, apiSend } from "./shared/api.js";

const LS_KEY = {
  onboarding_seen: "wb_onboarding_seen",
  first_diag_seen: "wb_first_diag_seen",
};

// In-memory cache, hydrated once at boot from /profile. null until then, so the
// synchronous gates fall back to the localStorage pre-gate pre-hydration.
let _state = null;

function _lsGet(flag) {
  try { return !!localStorage.getItem(LS_KEY[flag]); } catch { return false; }
}
function _lsSet(flag) {
  try { localStorage.setItem(LS_KEY[flag], "1"); } catch { /* private mode */ }
}

// Fetch the profile once, seed the cache, reconcile with localStorage. Returns
// the /profile envelope (or null) so the caller can reuse it (e.g. language).
//
// Migration rule: a flag set LOCALLY but absent server-side (a user who
// onboarded before this shipped) is treated as seen AND promoted to the server
// — we never clear a local flag just because the server hasn't recorded it yet
// (server-negative means "never written", not "explicitly unseen"). From the
// promote on, the flag is cross-device.
export async function hydrateOnboardingState() {
  let env = null;
  try {
    env = await apiGet("/profile");
  } catch {
    _state = null; // stay on the localStorage-only fallback
    return null;
  }
  const srv = env?.profile?.state || {};
  const next = {
    onboarding_seen: !!srv.onboarding_seen,
    first_diag_seen: !!srv.first_diag_seen,
  };
  const promote = {};
  for (const flag of Object.keys(LS_KEY)) {
    if (next[flag]) {
      _lsSet(flag); // server says seen → keep the fast pre-gate in sync
    } else if (_lsGet(flag)) {
      next[flag] = true;    // pre-feature local flag is the truth…
      promote[flag] = true; // …promote it server-side so it sticks cross-device
    }
  }
  _state = next;
  if (Object.keys(promote).length) {
    apiSend("/profile/state", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(promote),
    }).catch((err) => console.warn("[onboarding] promote local flags failed", err));
  }
  return env;
}

// Synchronous read for the one-shot gates. The hydrated server cache wins; before
// hydration, fall back to the localStorage pre-gate.
export function hasSeenOnboarding(flag) {
  if (_state) return !!_state[flag];
  return _lsGet(flag);
}

// Mark a tour as seen: in-memory + localStorage cache (instant, this session) and
// the server (source of truth, fire-and-forget — the UI never blocks on it). The
// PUT patches only this flag, so it can't clobber the other tour's state.
export function markOnboardingSeen(flag) {
  if (!_state) _state = { onboarding_seen: false, first_diag_seen: false };
  _state[flag] = true;
  _lsSet(flag);
  apiSend("/profile/state", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ [flag]: true }),
  }).catch((err) => console.warn("[onboarding] persist state failed", err));
}
