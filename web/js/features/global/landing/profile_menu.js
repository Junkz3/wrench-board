// Landing profile pill — the always-present avatar button in the cockpit's
// top-right tools nav. Clicking it opens the profile config modal in place
// (profile_modal.js); it never navigates away. Renders identity from
// GET /profile and pulses when the profile is still incomplete.
//
// Kept named initProfileMenu/refreshProfileMenu for the existing call sites in
// landing/index.js.

import { apiGet } from "../../../shared/api.js";
import { openProfileModal } from "./profile_modal.js";

function _pill() { return document.getElementById("landingProfilePill"); }

// Fetch the profile and paint the pill. Returns the envelope so callers can
// reuse it without a second request.
export async function refreshProfileMenu() {
  const root = document.getElementById("landingProfile");
  const avatar = document.getElementById("landingProfileAvatar");
  const nameEl = document.getElementById("landingProfileName");
  if (!root) return null;

  let env = null;
  try {
    env = await apiGet("/profile");
  } catch (err) {
    console.warn("[profile_menu] load profile failed", err);
  }
  const id = env?.profile?.identity || {};
  const incomplete = !id.name;

  if (avatar) {
    avatar.textContent = id.avatar || (id.name ? id.name.slice(0, 2).toUpperCase() : "?");
  }
  if (nameEl) nameEl.textContent = id.name || "";
  root.dataset.incomplete = incomplete ? "1" : "";
  return env;
}

export function initProfileMenu() {
  const pill = _pill();
  if (!pill) return;
  pill.addEventListener("click", (ev) => {
    ev.stopPropagation();
    openProfileModal();
  });
  // The modal (or the onboarding step) saves and broadcasts; re-paint the pill.
  document.addEventListener("wb:profile-updated", () => refreshProfileMenu());
}
